"""허브 image 항목의 허용과 거부 (P55 6절, D97 초안).

Platform Settings 의 `hub_images` 에 레지스트리 접두사가 있으면 새 image 항목마다 허용을 거친다.
접두사는 공개 키 없이 저장되지 않는다. 이미지는 접두사 아래에 있어야 하고, 거부한
digest 가 아니어야 하며, 그 digest 에 설정한 키로 서명하고 허브 주석을 단 서명이 있어야 한다.
서명을 모으는 일은 `hub_verifier` 가 하고, 여기는 모은 서명으로 정하고(`decide`) 그것을 항목의
기록(`source.admission`)으로 옮긴다.

접두사는 경로 마디로 맞춘다. `hub` 는 `hubx` 를 품지 않는다. 허브 접두사는 어느 클러스터의 제품
이미지 접두사(`images.registry_prefix`)와도 같거나 품거나 그 안에 있을 수 없다. 허브 키가 따로인
것 옆의 이중 방어다. 어느 쪽이든 접두사를 바꿔 저장할 때 보고, home 클러스터의
접두사는 기동마다 환경에서 오므로 설정 카드가 계산한 경고로도 보인다(`overlapping_clusters`).

거부한 digest 는 `hub_access_revoked` 처럼 퍼지는 것만 막는다. 새 항목, Gateway 의
새 리비전과 배포, 접근 넓히기가 거절되고, 도는 항목과 복제본은 그대로 돌며 경고가 붙는다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import anyio

from daegam.config.resolve import image_digest
from daegam.errors import ServiceError
from daegam.secrets import SecretStore, read_secret_data
from daegam.services import hub_verifier
from daegam.services.hub_catalog import (
    DOCKER_HUB,
    REPOSITORY_SEGMENT,
    ImageRef,
    canonical_host,
    is_registry_host,
    parse_image,
)
from daegam.services.hub_verifier import APPROVED, VERIFIED, HubKey, HubSignature
from daegam.services.tool_rules import coded_warning
from daegam.store.base import utcnow
from daegam.store.clusters import Cluster
from daegam.store.hub import HubAdmission, HubCatalogOrigin, HubItem
from daegam.store.settings import HubImagesSettings
from daegam.urlfetch import tls_verify

logger = logging.getLogger("daegam.hub_images")

# 읽기 자격 증명을 두는 관리 Secret 과 그 항목 (D56)
CREDENTIAL_SECRET = "daegam-hub-images-read"
CREDENTIAL_USERNAME = "username"
CREDENTIAL_PASSWORD = "password"
# 허용 하나의 레지스트리 읽기 전체에 거는 시한(초). 읽기마다의 시한(15초)이 요청 수만큼 쌓이지 않게
# 한다
ADMISSION_SECONDS = 60.0

# 항목 상태 경고 (6절). 계산할 뿐 저장하지 않는다
HUB_IMAGE_UNVERIFIED = "hub_image_unverified"
HUB_IMAGE_REFUSED = "hub_image_refused"
HUB_IMAGE_KEY_REVOKED = "hub_image_key_revoked"
# Gateway 상태 경고. 배포한 백엔드의 항목이 거부한 digest 를 든다
HUB_BACKEND_REFUSED = "hub_backend_refused"

_UNVERIFIED_TEXT = "this item's image was registered without a signature check"
_REFUSED_TEXT = (
    "a platform-admin refused this item's image; Gateways using it are refused at their next "
    "redeploy"
)
_KEY_REVOKED_TEXT = "the key that admitted this item's image was revoked"


# -- 접두사 ------------------------------------------------------------------------------


def _segments(prefix: str) -> tuple[str, ...]:
    """접두사를 경로 마디로 나눈다. 호스트는 `canonical_host` 이고 호스트가 없으면 Docker Hub
    다(클러스터의 제품 접두사가 그렇게 쓰일 수 있다)."""
    value = prefix.strip().rstrip("/")
    first, _, rest = value.partition("/")
    if is_registry_host(first):
        host, path = canonical_host(first), rest
    else:
        host, path = DOCKER_HUB, value
    return (host, *[p for p in path.split("/") if p])


def normalize_prefix(value: str) -> str:
    """허브 접두사의 정규형 `host[:port][/path]`. 호스트는 `canonical_host` 이고 끝의 `/` 는 뗀다.
    호스트가 없거나, scheme·태그·digest 가 있거나, 마디가 저장소 이름의 꼴이 아니면(빈 마디와
    `.`·`..` 포함) ValueError 다."""
    value = value.strip().rstrip("/")
    if not value or "://" in value or "@" in value:
        raise ValueError("the hub prefix is host[:port][/path], without a scheme, tag or digest")
    first, _, rest = value.partition("/")
    if not is_registry_host(first):
        raise ValueError("the hub prefix must start with a registry host")
    host = canonical_host(first)
    try:
        parse_image(f"{host}/x")
    except ValueError as e:
        raise ValueError("the hub prefix must start with a valid registry host") from e
    parts = rest.split("/") if rest else []
    if any(not REPOSITORY_SEGMENT.fullmatch(p) for p in parts):
        raise ValueError("each path segment of the hub prefix must be a lower-case repository name")
    return "/".join((host, *parts))


def split_prefix(prefix: str) -> tuple[str, str]:
    """정규화한 접두사의 (호스트, 호스트 뒤 경로). 경로는 비어 있을 수 있다."""
    host, _, path = normalize_prefix(prefix).partition("/")
    return host, path


def prefixes_overlap(a: str, b: str) -> bool:
    """두 접두사가 같거나 한쪽이 다른 쪽을 품는가. 경로 마디로 맞춘다."""
    sa, sb = _segments(a), _segments(b)
    n = min(len(sa), len(sb))
    return sa[:n] == sb[:n]


def overlapping_clusters(prefix: str | None, clusters: Iterable[Cluster]) -> list[Cluster]:
    """제품 이미지 접두사가 허브 접두사와 겹치는 클러스터."""
    if not prefix:
        return []
    return [
        c
        for c in clusters
        if c.images.registry_prefix and prefixes_overlap(prefix, c.images.registry_prefix)
    ]


def under_prefix(prefix: str, ref: ImageRef) -> bool:
    """이미지가 접두사 아래에 있는가. 접두사 마디 뒤에 저장소 마디가 하나 이상 더 있어야 한다."""
    want = _segments(prefix)
    have = (ref.host, *ref.repository.split("/"))
    return len(have) > len(want) and have[: len(want)] == want


# -- 설정 저장 -----------------------------------------------------------------------------


def check_hub_images(
    value: HubImagesSettings,
    clusters: Iterable[Cluster],
    *,
    before: HubImagesSettings,
    error_cls: type[ServiceError],
) -> HubImagesSettings:
    """저장할 `hub_images` 의 뜻을 보고 정규화한 사본을 돌려준다. 접두사의 꼴, 공개 키가 P-256
    인지와 겹치는 키, 접두사가 있는데 키가 없는지(422 invalid_value)를 본다. 제품 이미지 접두사와의
    겹침(422 hub_prefix_overlap)은 접두사를 바꿀 때만 본다. 이미 있는 겹침이 거부 목록 같은 다른
    저장을 막지 않게 하고, 그 겹침은 설정 카드의 경고가 보인다. 목록의 같은 값은 하나로 줄인다."""
    prefix = None
    if value.prefix and value.prefix.strip():
        try:
            prefix = normalize_prefix(value.prefix)
        except ValueError as e:
            raise error_cls(422, "invalid_value", f"hub_images.prefix: {e}") from e
    keys: list[str] = []
    seen: set[str] = set()
    for pem in value.public_keys:
        try:
            key = hub_verifier.load_key(pem)
        except ValueError as e:
            raise error_cls(
                422, "invalid_value", f"hub_images.public_keys: {e}", path="hub_images.public_keys"
            ) from e
        if key.fingerprint in seen:
            continue
        seen.add(key.fingerprint)
        keys.append(pem.strip() + "\n")
    if prefix and not keys:
        raise error_cls(
            422,
            "invalid_value",
            "hub_images.public_keys needs a key while the prefix is set; admission refuses every "
            "image otherwise",
            path="hub_images.public_keys",
        )
    if prefix != before.prefix and (hits := overlapping_clusters(prefix, clusters)):
        c = hits[0]
        raise error_cls(
            422,
            "hub_prefix_overlap",
            f"the hub prefix {prefix} overlaps cluster {c.name}'s product image prefix "
            f"{c.images.registry_prefix}",
            cluster=c.name,
        )
    return value.model_copy(
        update={
            "prefix": prefix,
            "public_keys": keys,
            "revoked_keys": list(dict.fromkeys(value.revoked_keys)),
            "refused_digests": list(dict.fromkeys(value.refused_digests)),
        }
    )


def prefix_host(prefix: str | None) -> str | None:
    """접두사의 호스트. 읽기 자격 증명은 이 호스트에만 간다."""
    return _segments(prefix)[0] if prefix else None


def check_cluster_prefix(
    registry_prefix: str | None,
    hub: HubImagesSettings,
    *,
    cluster: str,
    error_cls: type[ServiceError],
) -> None:
    """클러스터의 제품 이미지 접두사를 바꿔 저장할 때의 겹침 검사 (422 hub_prefix_overlap)."""
    if registry_prefix and hub.prefix and prefixes_overlap(hub.prefix, registry_prefix):
        raise error_cls(
            422,
            "hub_prefix_overlap",
            f"cluster {cluster}'s product image prefix {registry_prefix} overlaps the hub prefix "
            f"{hub.prefix}",
            cluster=cluster,
        )


def key_fingerprints(hub: HubImagesSettings) -> list[str]:
    """설정한 공개 키의 지문. 읽지 못하는 키는 건너뛴다."""
    out = []
    for pem in hub.public_keys:
        try:
            out.append(hub_verifier.load_key(pem).fingerprint)
        except ValueError:
            continue
    return out


def usable_keys(pems: Iterable[str], revoked: Iterable[str]) -> list[HubKey]:
    """서명을 맞춰 볼 키. 회수한 지문의 키는 새 항목을 허용하지 않는다. 이 판이 읽지 못하는 키(뒤
    판이 쓴 것)는 기록하고 건너뛴다. 키 하나 때문에 모든 허용이 멈추지 않고, 그 키로만 맞는 서명은
    모이지 않는다."""
    gone = set(revoked)
    keys = []
    for pem in pems:
        try:
            key = hub_verifier.load_key(pem)
        except ValueError as e:
            logger.warning("a configured hub key is skipped: %s", e)
            continue
        if key.fingerprint not in gone:
            keys.append(key)
    return keys


# -- 경고 ----------------------------------------------------------------------------------


def image_warnings(item: HubItem, hub: HubImagesSettings) -> list[str]:
    """image 항목의 상태 경고 (6절). 허용 기록이 없으면 설정과 상관없이 `hub_image_unverified`,
    digest 가 거부됐으면 `hub_image_refused`, 허용한 키가 회수됐으면 `hub_image_key_revoked` 다."""
    if item.kind != "image":
        return []
    out: list[str] = []
    admission = item.source.admission
    if admission is None:
        out.append(coded_warning(HUB_IMAGE_UNVERIFIED, _UNVERIFIED_TEXT))
    if image_digest(item.source.image) in set(hub.refused_digests):
        out.append(coded_warning(HUB_IMAGE_REFUSED, _REFUSED_TEXT))
    if admission is not None and admission.key in set(hub.revoked_keys):
        out.append(coded_warning(HUB_IMAGE_KEY_REVOKED, _KEY_REVOKED_TEXT))
    return out


def refused_backend_lines(items: Iterable[HubItem], refused: Iterable[str]) -> list[str]:
    """Gateway 상태 경고. 배포한 백엔드 가운데 거부한 digest 를 든 항목이다(`code: 이름`)."""
    gone = set(refused)
    return [
        f"{HUB_BACKEND_REFUSED}: {i.name}"
        for i in items
        if i.kind == "image" and image_digest(i.source.image) in gone
    ]


def refused_items(items: Sequence[HubItem], refused: Iterable[str]) -> dict[str, list[HubItem]]:
    """거부한 digest 마다 그것을 든 항목. 태그로만 적은 항목은 digest 가 없어 들어가지 않는다."""
    out: dict[str, list[HubItem]] = {d: [] for d in refused}
    for item in items:
        d = image_digest(item.source.image)
        if d in out:
            out[d].append(item)
    return out


# -- 정하기 --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """모은 서명 전체로 정한 것 (6절 허용 3·4단계). catalog 는 이름 댄 카탈로그 서버와 그 서명의
    릴리스다. used 는 기록에 쓰는 서명 하나다."""

    provenance: str
    catalog: tuple[str, str] | None
    used: HubSignature
    count: int


def _used_order(sig: HubSignature) -> tuple[bool, bool, str]:
    # verified 먼저, 그다음 카탈로그 서버를 이름 댄 것, 마지막으로 번들 digest 가 낮은 것
    return (sig.provenance != VERIFIED, sig.catalog is None, sig.bundle)


def decide(
    sigs: Sequence[HubSignature], tag: str | None, *, error_cls: type[ServiceError]
) -> Decision:
    """모은 서명으로 정한다. 하나도 없으면 422 image_not_admitted 이고, 사람이 준 태그를 서명한
    upstream 태그로 든 서명이 없어도 그렇다. 카탈로그 규칙과 출처는 준 태그와 상관없이 모은 서명
    전체로 정한다. 하나라도 카탈로그 서버를 이름 대면 그 서버의 규칙이 서고, 서로 다른 서버를 이름
    대면 422 image_catalog_conflict 다. 하나라도 verified 면 verified 다."""
    if not sigs:
        raise error_cls(
            422,
            "image_not_admitted",
            "no signature by a configured hub key with hub annotations was found on this image",
        )
    if tag is not None and not any(s.upstream_ref.tag == tag for s in sigs):
        raise error_cls(
            422,
            "image_not_admitted",
            f"no hub signature vouches for tag {tag}; give the digest or a tag a signature names",
        )
    servers = sorted({s.catalog[0] for s in sigs if s.catalog is not None})
    if len(servers) > 1:
        raise error_cls(
            422,
            "image_catalog_conflict",
            "signatures on this image name different catalog servers: " + ", ".join(servers),
            servers=servers,
        )
    used = min(sigs, key=_used_order)
    catalog = None
    if servers:
        catalog = min((s for s in sigs if s.catalog is not None), key=_used_order).catalog
    provenance = VERIFIED if any(s.provenance == VERIFIED for s in sigs) else APPROVED
    return Decision(provenance=provenance, catalog=catalog, used=used, count=len(sigs))


# -- 허용 ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Admitted:
    """허용한 결과. reference 는 digest 로 고정한 참조이고 catalog 는 서명이 이름 댄 서버다."""

    reference: str
    digest: str
    catalog: HubCatalogOrigin | None
    decision: Decision

    def record(self, by: str) -> HubAdmission:
        used = self.decision.used
        return HubAdmission(
            digest=self.digest,
            provenance=self.decision.provenance,
            upstream=used.upstream,
            key=used.key,
            run=dict(used.run),
            signatures=self.decision.count,
            at=utcnow(),
            by=by,
        )


def refuse_refused(
    digest: str | None, refused: Iterable[str], *, error_cls: type[ServiceError]
) -> None:
    """거부한 digest 로는 새 항목을 만들지 않는다 (422 image_refused)."""
    if digest is not None and digest in set(refused):
        raise error_cls(422, "image_refused", f"a platform-admin refused image digest {digest}")


class HubImageAdmission:
    """새 image 항목의 허용 (6절). 읽기 자격 증명은 부를 때마다 관리 Secret 에서 읽는다."""

    # 테스트가 httpx2.MockTransport 를 꽂는 자리. 운영에서는 없다
    transport: Any = None

    def __init__(self, *, secrets: Callable[[], SecretStore | None], namespace: str):
        self.secrets = secrets
        self.namespace = namespace

    async def credential(
        self, hub: HubImagesSettings, *, error_cls: type[ServiceError]
    ) -> tuple[str, str] | None:
        """읽기 자격 증명. 넣지 않았으면 None 이고 익명으로 읽는다. 넣었는데 Secret 저장소가 없거나
        Secret 을 읽지 못하거나 값이 없으면, Teams 봇의 Client Secret 처럼 503
        hub_images_secret_unavailable 이다. 익명으로 물러나 자격 증명 거절로 보이지 않게 한다."""
        if not hub.credential_set:
            return None
        store = self.secrets()
        if store is None:
            raise error_cls(
                503,
                "hub_images_secret_unavailable",
                "the home cluster's Secret store is not available",
            )
        try:
            data = await read_secret_data(store, CREDENTIAL_SECRET, namespace=self.namespace)
        except Exception as e:  # noqa: BLE001 - Secret 저장소의 어떤 실패든 같은 503 이다
            raise error_cls(
                503, "hub_images_secret_unavailable", "the hub image read credential cannot be read"
            ) from e
        user, password = data.get(CREDENTIAL_USERNAME), data.get(CREDENTIAL_PASSWORD)
        if not (user and password):
            raise error_cls(
                503, "hub_images_secret_unavailable", "the hub image read credential is not stored"
            )
        return user, password

    async def admit(
        self,
        image: str,
        hub: HubImagesSettings,
        *,
        ca_bundle: str | None,
        error_cls: type[ServiceError],
    ) -> Admitted:
        """image 를 허용한다 (6절 허용 1·2·3단계). 참조의 꼴이 틀리면 레지스트리에 묻기 전에 422
        invalid_value, 접두사 밖이면 422 image_outside_registry, 거부한 digest 면 422
        image_refused 다. 태그는 읽기 자격 증명으로 한 번 digest 로 푼다. 서명은
        `hub_verifier.collect` 가 모으고 `decide` 가 정한다. 레지스트리 읽기 전체가
        ADMISSION_SECONDS 를 넘으면 502 registry_unreachable 이다."""
        prefix = hub.prefix or ""
        try:
            ref = parse_image(image)
        except ValueError as e:
            raise error_cls(422, "invalid_value", str(e)) from e
        if not under_prefix(prefix, ref):
            raise error_cls(
                422, "image_outside_registry", f"hub images must be under {prefix}", prefix=prefix
            )
        refuse_refused(ref.digest, hub.refused_digests, error_cls=error_cls)
        keys = usable_keys(hub.public_keys, hub.revoked_keys)
        # 태그도 digest 도 없으면 latest 다. 그 태그도 서명이 들어야 한다
        tag = ref.tag or ("latest" if ref.digest is None else None)
        try:
            with anyio.fail_after(ADMISSION_SECONDS):
                digest, sigs = await self._collect(ref, tag, keys, hub, ca_bundle, error_cls)
        except TimeoutError as e:
            raise error_cls(
                502,
                "registry_unreachable",
                f"the hub registry {ref.host} did not answer within {ADMISSION_SECONDS:.0f} s",
            ) from e
        decision = decide(sigs, tag, error_cls=error_cls)
        catalog = (
            HubCatalogOrigin(server=decision.catalog[0], release=decision.catalog[1])
            if decision.catalog
            else None
        )
        return Admitted(
            reference=f"{ref.host}/{ref.repository}@{digest}",
            digest=digest,
            catalog=catalog,
            decision=decision,
        )

    async def _collect(
        self,
        ref: ImageRef,
        tag: str | None,
        keys: list[HubKey],
        hub: HubImagesSettings,
        ca_bundle: str | None,
        error_cls: type[ServiceError],
    ) -> tuple[str, list[HubSignature]]:
        """태그를 풀고 서명을 모은다. (digest, 모은 서명)."""
        async with hub_verifier.HubRegistry(
            host=ref.host,
            credential=await self.credential(hub, error_cls=error_cls),
            verify=tls_verify(ca_bundle),
            error_cls=error_cls,
            transport=self.transport,
        ) as registry:
            digest = ref.digest
            if digest is None:
                try:
                    _, digest = await registry.manifest(ref.repository, tag or "latest")
                except hub_verifier.NotFound as e:
                    raise error_cls(
                        422, "image_not_admitted", f"{ref.host}/{ref.repository} was not found"
                    ) from e
                refuse_refused(digest, hub.refused_digests, error_cls=error_cls)
            sigs = await hub_verifier.collect(
                registry,
                ref.repository,
                digest,
                keys,
                prefix_path=split_prefix(hub.prefix or "")[1],
                refused=lambda r, why: logger.info(
                    "hub signature %s on %s not collected: %s", r, digest, why
                ),
            )
        return digest, sigs
