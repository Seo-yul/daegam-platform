"""Backend API 도메인. 스펙을 받아 검사하고 버전으로 저장하는 규칙이 여기 있다.

라우터와 Spec Generator 워커가 이 모듈을 부른다. "이 스펙을 받아도 되는가" 판정은 여기 하나뿐이라
HTTP 요청 없이도 같은 규칙으로 부를 수 있다.

오류는 다른 서비스와 같은 `ServiceError` 다. 앱의 전역 핸들러가 응답으로 낸다.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError
from starlette.concurrency import run_in_threadpool

from daegam.config import Settings
from daegam.error_codes import status_of
from daegam.errors import ServiceError, revision_conflict
from daegam.pipeline import convert as pipeline
from daegam.pipeline.verdict import Verdict, verdict_of
from daegam.services.references import BACKEND_API_DELETE, TOKEN_GUARD, ReferenceService
from daegam.services.spec_generator import minify as minify_svc
from daegam.services.spec_generator import projection, refine, surface
from daegam.store.backend_apis import (
    AUTH_KINDS,
    BackendAPI,
    BackendAPIStore,
    BackendAPIVersion,
    VersionOrigin,
)
from daegam.store.base import utcnow
from daegam.store.files import SpecFiles
from daegam.store.leases import holding_lease
from daegam.urls import is_http_url

logger = logging.getLogger("daegam.backend_apis")

_SECRET_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")  # K8s Secret 이름 규칙


class BackendAPIError(ServiceError):
    """Backend API 도메인 오류."""


# -- 연결 정보 검증 ---------------------------------------------------------------


def validate_endpoint(endpoint: str | None) -> str | None:
    if endpoint is None:
        return None
    endpoint = endpoint.strip()
    if not endpoint:
        return None
    if not is_http_url(endpoint):
        raise BackendAPIError(422, "invalid_endpoint", "endpoint must be an http(s) URL")
    return endpoint


def validate_auth(auth: str | None) -> str:
    if auth is None or not auth.strip():
        return "none"
    auth = auth.strip().lower()
    if auth not in AUTH_KINDS:
        raise BackendAPIError(422, "invalid_auth", f"auth must be one of {', '.join(AUTH_KINDS)}")
    return auth


def validate_secret(secret: str | None) -> str | None:
    if secret is None:
        return None
    secret = secret.strip()
    if not secret:
        return None
    if not _SECRET_NAME_RE.fullmatch(secret):
        raise BackendAPIError(
            422, "invalid_credential_secret", "credential_secret must be a K8s Secret name"
        )
    return secret


def validate_soap_headers_secret(secret: str | None) -> str | None:
    """`soap_headers_secret` 칸 (T60). 자격증명 Secret 과 같은 이름 규칙이고 빈 값은 None 이다."""
    secret = (secret or "").strip() or None
    if secret is not None and not _SECRET_NAME_RE.fullmatch(secret):
        raise BackendAPIError(
            422, "invalid_soap_headers_secret", "soap_headers_secret must be a K8s Secret name"
        )
    return secret


def check_auth_consistency(auth: str, credential_secret: str | None) -> None:
    if auth != "none" and not credential_secret:
        raise BackendAPIError(422, "credential_required", f"auth={auth} requires credential_secret")


# -- 스펙 파이프라인 (verify·convert·upload·apply-draft 가 공유) ----------------------


async def inspect_spec(
    content: bytes, filename: str | None, settings: Settings
) -> pipeline.Inspection:
    """감지·변환·verify. `verify()` 의 FastMCP 라운드트립은 도는 이벤트 루프 안에서 skip 되므로
    워커 스레드에서 돌려 complete=True 를 보장한다."""
    return await run_in_threadpool(
        pipeline.inspect, content, filename=filename, max_bytes=settings.max_upload_bytes
    )


def _rejected(err: pipeline.PipelineError, verdict: Verdict) -> BackendAPIError:
    """파이프라인 오류를 오류 코드표의 상태(대개 422)로 바꾸고 판정을 함께 싣는다."""
    d = err.to_dict()
    code = d.pop("code", "verify")
    return BackendAPIError(
        status_of(code, 422), code, d.pop("detail", ""), **d, verdict=verdict.to_dict()
    )


def accept_or_reject(insp: pipeline.Inspection, verdict: Verdict) -> pipeline.PipelineResult:
    """업로드 수락 규칙. verify 판정이 error 면 반드시 거부한다(기존 코드 체계 + verdict).

    verdict 가 error 가 아닌데 finalize 가 실패하는 경우는 verify_incomplete(선택 의존성 누락으로
    skip 이 남은 경우) 뿐이고, 이는 배포 문제라 그대로 거부다."""
    if verdict.level == "error":
        err = insp.failure() or pipeline.PipelineError(
            "verify", "verdict has errors: " + ", ".join(sorted(verdict.codes)), insp.report
        )
        raise _rejected(err, verdict)
    try:
        return pipeline.finalize(insp)
    except pipeline.PipelineError as e:
        raise _rejected(e, verdict) from e


async def inspect_and_accept(
    content: bytes, filename: str | None, settings: Settings
) -> pipeline.PipelineResult:
    """감지·변환·verify 를 돌려 저장 가능한 결과를 만든다. 판정이 error 면 거부."""
    insp = await inspect_spec(content, filename, settings)
    return accept_or_reject(insp, verdict_of(insp))


def source_ext(source_format: str, filename: str | None, content: bytes) -> str:
    """저장할 원본의 확장자. WSDL 이면 wsdl, 아니면 파일 이름 또는 본문으로 정한다."""
    if source_format == "wsdl":
        return "wsdl"
    if filename:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in ("json", "yaml", "yml"):
            return ext
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return "yaml"  # UTF-8 로 읽히지 않으면 JSON 일 수 없다
    return "json" if text.lstrip().startswith("{") else "yaml"


def _number(v: BackendAPIVersion | None) -> int:
    """버전 번호. 버전이 없으면 0 이다."""
    return v.version if v else 0


def version_lease_name(profile_id: str) -> str:
    """프로필 하나의 버전 쓰기와 삭제를 한 번에 하나로 묶는 리스의 이름."""
    return f"backend-api-version:{profile_id}"


# -- 프로필 쓰기 (P75 2절) -----------------------------------------------------------


@dataclass(frozen=True)
class UploadConnection:
    """업로드가 함께 보낸 접속 정보. 보내지 않은 칸은 `*_sent` 가 거짓이다(있는 프로필은 보낸 칸만
    바뀐다, D24). 본문을 읽기 전에 검사하려고 라우터가 먼저 만든다."""

    endpoint: str | None
    endpoint_sent: bool
    auth: str | None
    credential_secret: str | None
    credential_secret_sent: bool
    soap_headers_secret: str | None = None
    soap_headers_secret_sent: bool = False

    @classmethod
    def of(
        cls,
        endpoint: str | None,
        auth: str | None,
        credential_secret: str | None,
        soap_headers_secret: str | None = None,
    ) -> UploadConnection:
        """보낸 값을 endpoint, credential_secret, auth, soap_headers_secret 차례로 검사한다
        (PATCH 와 같은 검사 함수를 쓴다)."""
        endpoint_v = validate_endpoint(endpoint)
        secret_v = validate_secret(credential_secret)
        auth_v = validate_auth(auth) if auth is not None else None
        soap_v = validate_soap_headers_secret(soap_headers_secret)
        return cls(
            endpoint_v,
            endpoint is not None,
            auth_v,
            secret_v,
            credential_secret is not None,
            soap_v,
            soap_headers_secret is not None,
        )


@dataclass(frozen=True)
class BackendAPIUpload:
    """업로드 한 번. 이름과 표시 이름은 라우터가 검사했다."""

    name: str
    display_name: str
    display_name_sent: bool
    connection: UploadConnection
    content: bytes
    filename: str | None


# 사람이 고치는 칸 (P68 2절)
_EDIT_FIELDS = frozenset(
    {"display_name", "endpoint", "auth", "credential_secret", "soap_headers_secret"}
)


class BackendAPIService:
    """Backend API 프로필의 업로드·수정·삭제. 스펙 검사와 버전 저장은 위의 함수들이다."""

    def __init__(
        self,
        store: BackendAPIStore,
        files: SpecFiles,
        references: ReferenceService,
        settings: Settings,
        db: AsyncDatabase,
    ):
        self.store = store
        self.files = files
        self.references = references
        self.settings = settings
        self.db = db

    async def upload(
        self, tenant: str, up: BackendAPIUpload, *, user_sub: str
    ) -> tuple[BackendAPI, BackendAPIVersion, bool]:
        """스펙을 올려 새 버전을 만든다. 프로필이 없으면 접속 정보와 함께 만들고, 있으면 보낸
        접속 정보 칸만 덮어쓴다. 원본이 최신 버전과 같으면 새 버전 없이 그 버전을 돌려주고 셋째
        값이 거짓이다.

        있는 프로필의 바뀐 접속 정보는 검사를 통과한 뒤 버전 리스 안에서 본문을 쓰기 직전에
        저장한다. 그래서 검사에서 거절되거나 리스·최신 버전 확인에서 409 로 끝난 업로드는 접속
        정보를 바꾸지 않고, 그 저장이 다른 저장과 엇갈리면 아무것도 쓰지 않고 409
        revision_conflict 다. 저장 뒤의 버전 쓰기가 실패하면(리스를 잃었거나 넣기가 부딪쳤으면)
        접속 정보는 저장된 채 남는다."""
        conn = up.connection
        source_sha256 = hashlib.sha256(up.content).hexdigest()

        profile = await self.store.get_by_name(tenant, up.name)
        latest = await self.store.latest_version(profile.id) if profile else None
        changed: set[str] = set()
        if profile is not None:
            if up.display_name_sent and profile.display_name != up.display_name:
                profile.display_name = up.display_name
                changed.add("display_name")
            if conn.endpoint_sent and profile.endpoint != conn.endpoint:
                profile.endpoint = conn.endpoint
                changed.add("endpoint")
            if conn.auth is not None and profile.auth != conn.auth:
                profile.auth = conn.auth
                changed.add("auth")
            if conn.credential_secret_sent and profile.credential_secret != conn.credential_secret:
                profile.credential_secret = conn.credential_secret
                changed.add("credential_secret")
            if (
                conn.soap_headers_secret_sent
                and profile.soap_headers_secret != conn.soap_headers_secret
            ):
                profile.soap_headers_secret = conn.soap_headers_secret
                changed.add("soap_headers_secret")
            check_auth_consistency(profile.auth, profile.credential_secret)
            if latest and latest.source_sha256 == source_sha256:
                await self._save_upload_fields(profile, changed, user_sub)
                return profile, latest, False
        else:
            check_auth_consistency(conn.auth or "none", conn.credential_secret)

        result = await inspect_and_accept(up.content, up.filename, self.settings)
        if profile is None:
            profile = BackendAPI(
                updated_by=user_sub,
                tenant=tenant,
                name=up.name,
                display_name=up.display_name,
                endpoint=conn.endpoint,
                auth=conn.auth or "none",
                credential_secret=conn.credential_secret,
                soap_headers_secret=conn.soap_headers_secret,
            )
            try:
                await self.store.create(profile)
            except DuplicateKeyError as e:
                raise BackendAPIError(
                    409, "name_conflict", f"name {up.name!r} already exists"
                ) from e
        save_fields = functools.partial(self._save_upload_fields, profile, changed, user_sub)
        version = await self.add_version(
            profile, up.content, up.filename, result, seen=latest, before_write=save_fields
        )
        return profile, version, True

    async def _save_upload_fields(
        self, profile: BackendAPI, changed: set[str], user_sub: str
    ) -> None:
        """업로드가 바꾼 접속 정보 칸을 저장한다. 바뀐 칸이 없으면 쓰지 않는다."""
        if changed:
            profile.updated_at = utcnow()
            profile.updated_by = user_sub
            await self._save(profile, changed)

    async def add_version(
        self,
        profile: BackendAPI,
        content: bytes,
        filename: str | None,
        result: pipeline.PipelineResult,
        *,
        seen: BackendAPIVersion | None,
        origin: VersionOrigin | None = None,
        before_write: Callable[[], Awaitable[None]] | None = None,
    ) -> BackendAPIVersion:
        """검사를 통과한 결과를 프로필의 다음 버전으로 저장한다 (업로드와 apply-draft 가 공유).
        content 는 이 버전의 원본(source) 이고 result.yaml 은 변환 본문(openapi.yaml) 이다. seen 은
        부른 쪽이 같은 원본인지 볼 때 읽은 최신 버전이다. before_write 는 리스 안에서 본문을 쓰기
        직전에 부른다(업로드의 접속 정보 저장).

        본문은 버전 기록보다 먼저 이름으로 쓰므로, 두 저장이 같은 번호를 잡으면 진 쪽이 이긴 쪽의
        본문을 덮어쓴다. 그래서 번호 정하기, 본문 쓰기, 기록 넣기를 프로필마다 리스 하나로 묶는다.
        다른 저장이 리스를 쥐고 있거나, 최신 버전이 seen 에서 바뀌었거나, 본문 쓰기나 기록 넣기
        앞에서 리스를 늘려 보니 남에게 넘어갔으면 409 revision_conflict 다. 넣기가 버전 색인에
        부딪쳐도 409 다.

        리스는 울타리가 아니다. 늘려 본 뒤 본문 쓰기가 그 번호의 파일을 지우기 전에 기한을 넘겨
        멈추면 남이 넣은 버전의 본문을 지울 수 있고, 리스 없이 쓰는 옛 판 파드(롤링 업데이트 중)는
        본문을 바꾼 뒤에야 넣기에서 부딪친다. 그 409 는 이미 바뀐 본문을 되돌리지 않는다. 늘려
        보기는 이 틈을 좁힐 뿐 닫지 않는다."""
        action = "upload" if origin is None else "apply-draft"
        conflict = revision_conflict(BackendAPIError, "backend api", profile.name, action)
        async with holding_lease(
            self.db, self.settings.pod_id, version_lease_name(profile.id), conflict
        ) as held:
            latest = await self.store.latest_version(profile.id)
            if _number(latest) != _number(seen):
                raise conflict
            next_version = _number(latest) + 1
            ext = source_ext(result.source_format, filename, content)
            if before_write is not None:
                await before_write()
            # 본문 쓰기는 그 번호의 파일을 먼저 지운다. 리스를 잃었으면 그 파일은 남이 넣은 버전의
            # 본문일 수 있어 쓰기 직전에 늘려 본다. 이 확인과 지우기 사이의 틈은 남는다
            if not await held.renew():
                raise conflict
            await self.files.write(
                profile.id,
                next_version,
                openapi_yaml=result.yaml,
                source=content,
                source_ext=ext,
                kind=result.kind,
            )
            if not await held.renew():
                raise conflict
            try:
                return await self.store.add_version(
                    BackendAPIVersion(
                        backend_profile_id=profile.id,
                        version=next_version,
                        kind=result.kind,
                        source_format=result.source_format,
                        source_ext=ext,
                        source_sha256=hashlib.sha256(content).hexdigest(),
                        output_sha256=hashlib.sha256(result.yaml.encode("utf-8")).hexdigest(),
                        s2o_version=result.s2o_version,
                        tool_count=result.tool_count,
                        tool_names=result.tool_names,
                        tool_details=result.tool_details,
                        report=result.report,
                        origin=origin,
                    )
                )
            except DuplicateKeyError as e:
                raise conflict from e

    async def update(
        self, profile: BackendAPI, changes: dict[str, Any], *, user_sub: str
    ) -> BackendAPI:
        """보낸 칸만 바꾼다. 표시 이름은 라우터가 이미 검사했고, 나머지는 예전 라우터와 같은
        차례로 검사한다(endpoint, auth, credential_secret, 그다음 soap_headers_secret)."""
        if "display_name" in changes:
            profile.display_name = changes["display_name"]
        if "endpoint" in changes:
            profile.endpoint = validate_endpoint(changes["endpoint"])
        if "auth" in changes:
            auth = BackendAPIError.not_null(changes["auth"], "auth", code="invalid_auth")
            profile.auth = validate_auth(auth)
        if "credential_secret" in changes:
            profile.credential_secret = validate_secret(changes["credential_secret"])
        check_auth_consistency(profile.auth, profile.credential_secret)
        if "soap_headers_secret" in changes:
            profile.soap_headers_secret = validate_soap_headers_secret(
                changes["soap_headers_secret"]
            )
        profile.updated_at = utcnow()
        profile.updated_by = user_sub
        await self._save(profile, changes.keys() & _EDIT_FIELDS)
        return profile

    async def _save(self, profile: BackendAPI, names: set[str]) -> None:
        """보낸 칸만 서버가 읽은 `seq` 를 조건으로 쓴다. 그사이 다른 저장이 지나갔으면 409
        revision_conflict 다 (P68 2절)."""
        if not await self.store.save_edit(profile, {*names, "updated_at", "updated_by"}):
            raise revision_conflict(BackendAPIError, "backend api", profile.name)

    async def delete(self, profile: BackendAPI, *, force: bool) -> list[str]:
        """참조 가드 뒤 프로필과 스펙 본문을 지운다. 지우며 함께 폐기한 읽기 토큰 id 를 돌려준다.
        버전 쓰기와 같은 리스를 쥐므로, 버전을 쓰는 중이면 409 revision_conflict 다. 겹치면 지운
        문서 아래에 버전 기록과 본문이 남는다."""
        refs = await self.references.refuse_first(
            "backend-api", profile.id, BACKEND_API_DELETE, BackendAPIError
        )
        active = TOKEN_GUARD.ids(refs)
        if active and not force:
            raise BackendAPIError(
                409, "tokens_active", TOKEN_GUARD.detail, **{TOKEN_GUARD.key: active}
            )
        conflict = revision_conflict(BackendAPIError, "backend api", profile.name, "delete")
        async with holding_lease(
            self.db, self.settings.pod_id, version_lease_name(profile.id), conflict
        ):
            await self.store.delete(profile.id)
            await self.files.delete_spec_bodies(profile.id)
        return active


# -- 저장된 버전 읽기 --------------------------------------------------------------


def _verified(body: bytes | None, expected_sha256: str, what: str, v: BackendAPIVersion) -> bytes:
    """GridFS 에서 읽은 본문. 없거나 기록된 sha256 과 다르면 integrity_error 다. `what` 은 로그와
    오류 문구에 들어가는 본문 이름이다("body" 는 변환 스펙, "source body" 는 올린 원본)."""
    if body is None:
        logger.error(
            "%s missing in GridFS: profile=%s version=%s", what, v.backend_profile_id, v.version
        )
        raise BackendAPIError(500, "integrity_error", f"version {v.version} {what} missing")
    # 파일 저장소가 없으므로 본문 무결성은 읽는 자리에서만 확인할 수 있다
    actual = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(actual, expected_sha256):
        logger.error(
            "%s sha256 mismatch: profile=%s version=%s expected=%s actual=%s",
            what,
            v.backend_profile_id,
            v.version,
            expected_sha256,
            actual,
        )
        raise BackendAPIError(500, "integrity_error", f"version {v.version} {what} mismatch")
    return body


async def verified_spec_body(files: SpecFiles, v: BackendAPIVersion) -> bytes:
    """GridFS 의 변환 스펙 본문. 없거나 기록된 output_sha256 과 다르면 integrity_error 다."""
    body = await files.read_openapi(v.backend_profile_id, v.version)
    return _verified(body, v.output_sha256, "body", v)


async def verified_source_body(files: SpecFiles, v: BackendAPIVersion) -> bytes:
    """GridFS 의 올린 그대로의 원본 본문. 없거나 기록된 source_sha256 과 다르면 integrity_error 다.
    이 본문은 "편집기로 가져오기" 로 새 버전이 되어 되돌아오므로 깨진 것을 그대로 보이면 그대로
    등록된다."""
    body = await files.read_source(v.backend_profile_id, v.version)
    return _verified(body, v.source_sha256, "source body", v)


async def original_spec(files: SpecFiles, v: BackendAPIVersion) -> dict[str, Any]:
    """저장된 openapi.yaml 을 JSON 호환 dict 로. refine job 의 input.json 과 같은 변환이다."""
    try:
        return refine.spec_from_yaml(await verified_spec_body(files, v))
    except ValueError as e:
        logger.error(
            "spec body unusable: profile=%s version=%s: %s", v.backend_profile_id, v.version, e
        )
        raise BackendAPIError(500, "integrity_error", f"version {v.version} body: {e}") from e


# -- minify·표면 보호 --------------------------------------------------------------


def run_minify(
    original: dict[str, Any], options: minify_svc.MinifyOptions
) -> tuple[dict[str, Any], list[Any]]:
    """(초안, changes). 표면 검사를 changes 보다 먼저 한다. 키 순서가 바뀐 초안은 채택 단위가 될 수
    없어(projection.changes) 그 전에 422 surface_changed 로 막는다 (P73 2절)."""
    try:
        draft = minify_svc.minify_draft(original, options)
    except minify_svc.MinifyError as e:
        raise BackendAPIError(422, "invalid_minify_options", str(e)) from e
    check_surface(original, draft, surface.ALLOW_MINIFY)
    return draft, projection.changes(original, draft)


def check_surface(original: dict[str, Any], draft: dict[str, Any], rules: surface.Rules) -> None:
    """초안이 허용된 표면만 건드렸는지 본다. 아니면 surface_changed 다."""
    violations = surface.guard(original, draft, rules=rules)
    if violations:
        raise BackendAPIError(
            422,
            "surface_changed",
            f"{len(violations)} change(s) outside the allowed fields ({rules.name})",
            violations=[v.to_dict() for v in violations[:50]],
        )


def select_changes(changes: list[Any], accepted_ids: list[str]) -> list[Any]:
    """accepted_ids 가 비어 있으면 전부. 모르는 id 는 unknown_change 다."""
    if not accepted_ids:
        return list(changes)
    known = {c.id for c in changes}
    unknown = [i for i in accepted_ids if i not in known]
    if unknown:
        raise BackendAPIError(
            422,
            "unknown_change",
            f"{len(unknown)} accepted id(s) are not in the change list",
            ids=unknown,
        )
    accepted = set(accepted_ids)
    return [c for c in changes if c.id in accepted]
