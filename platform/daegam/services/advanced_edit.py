"""Advanced 편집(manifest / overrides / drift)의 공통 흐름 (F7a, resource-model.md 13절).

세 인스턴스 서비스가 같은 순서를 쓴다. 서비스는 인스턴스와
편집 대상 CR 의 렌더 함수, 클러스터 클라이언트, 잠금 경로, 오류 클래스만 넘기고, 여기서는 그것들로
문서 dict 를 만들고 overrides 를 검사·dry-run·적용·저장한다. 어떤 문서를 묶음에 넣을지는
서비스의 `manifest` 가 정하며 `document()` 로 한 문서씩 만든다.

set_overrides 는 삭제 중이면 409 를 내고, render 한 CR 에 (manifest 편집이면 diff 로 만들고 플랫폼이
건 보안 값을 뺀) patch 를 그 렌더와 함께 check_overrides 로 검사한 뒤 apply_overrides 로 얹고
서비스가 준 finish 로 맞춰(`final`) dry-run 하고, 통과하면 apply 해서 last_spec_change 를 갱신하고
응답 dict 를 돌려준다.

저장된 overrides 를 클러스터로 보내는 경로(배포·재배포 전 dry-run·소비자 다시 렌더·드리프트
되돌리기와 비교)는 모두 `stored_final` 을 거친다. 잠금 표가 넓어지기 전에 저장된 overrides 가
지금 잠긴 경로를 건드리면 거기서 422 locked_field 로 멈춘다. 저장된 overrides 의 검사도 얹을
렌더로 본다. 부모를 지우는 검사가 렌더에 실제로 있는 잠긴 값만 보기 때문이다.

편집이 다루는 인스턴스 문서의 모양(`EditableInstance`)과 dry_run 이 오류로 옮기는 kubernetes
메시지 추출기(`api_error_message`)도 여기 있다. 인스턴스 서비스 셋의 공통 뼈대는
`services/instance_base.py` 다.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from typing import Any, Protocol

from starlette.concurrency import run_in_threadpool

from daegam.error_codes import status_of
from daegam.k8s.client import ClusterUnreachable, KubeClient
from daegam.k8s.merge import (
    MergeError,
    Path,
    apply_overrides,
    check_overrides,
    compose,
    diff,
    drop_enforced_security,
    foreign_managers,
    format_path,
    key_paths,
    locked_path_strings,
    strip_volatile,
    wiping_paths,
)
from daegam.store.base import utcnow

# 저장된 overrides 가 거부될 때 오류 detail 의 머리. 새로 보낸 patch 가 거부된 것과 가른다
STORED_PREFIX = "stored overrides touch a platform-managed field: "
# 흡수하려는 drift 가 거부될 때 오류 detail 의 머리
DRIFT_PREFIX = "drift touches a platform-managed field: "
# 저장하는 overrides 의 상한(공백 없는 JSON 바이트). 잠금 검사는 경로 수에 비례해 이벤트 루프 위에서
# 돌고 리컨실이 틱마다 다시 본다. 사이드카 몇 개를 더하는 patch 도 수 KiB 다
MAX_OVERRIDES_BYTES = 64 * 1024


class _K8s(Protocol):
    name: str
    namespace: str


class EditableInstance(Protocol):
    """편집 대상 셋(MCP Server·Gateway·Agent)이 공유하는 편집·생애 필드.
    `deleted_at` 은 문서를 남기고 지우는 둘에만 있다."""

    k8s: _K8s
    cluster_id: str
    state: str
    overrides: dict[str, Any]
    last_error: str | None
    last_spec_change: datetime | None
    updated_at: datetime


def api_error_message(e: Exception) -> str:
    """kubernetes ApiException 의 body 에서 message 만 뽑는다."""
    body = getattr(e, "body", None)
    if body:
        try:
            data = json.loads(body)
            if isinstance(data, dict) and data.get("message"):
                return str(data["message"])
        except ValueError:
            pass
    return f"{type(e).__name__}: {e}"[:1000]


class AdvancedEditor[I: EditableInstance]:
    """인스턴스 하나의 Advanced 편집. 서비스가 `__init__` 에서 하나 만들어 붙인다.

    - kind: overrides 를 얹는 CR 의 kind. 묶음의 다른 문서는 잠금 `["*"]` 이다
    - render(inst): 저장된 스냅샷으로 다시 렌더링한 그 CR (overrides 전)
    - client(inst): 인스턴스 클러스터의 KubeClient. 못 만들면 error_cls 를 던진다
    - save(inst): 인스턴스 문서 저장
    - manifest(inst): 서비스의 문서 묶음. drift_reset·drift_absorb 의 응답이다
    - locked(inst): 잠긴 경로. 노출 여부에 따라 달라질 수 있어 함수다
    - ignored: 오퍼레이터가 덮어써 무시되는 경로
    - error_cls: `(status, code, detail, **extra)` 로 만드는 서비스 오류 클래스
    - apply(inst, client, final): final 을 적용하는 길. 주지 않으면 SSA apply 하나다. MCP Server
      는 테넌트 울타리 설정을 먼저 적용하고 기다리는 길을 준다 (P50 11절)
    - finish(final): overrides 를 얹은 최종본에서 플랫폼이 맞추는 값. Agent 는 턴 drain env 를
      최종본의 grace 에 맞춘다 (P78 5절). 주지 않으면 얹은 그대로다
    """

    def __init__(
        self,
        *,
        kind: str,
        render: Callable[[I], Awaitable[dict[str, Any]]],
        client: Callable[[I], Awaitable[KubeClient]],
        save: Callable[[I], Awaitable[Any]],
        manifest: Callable[[I], Awaitable[dict[str, Any]]],
        locked: Callable[[I], Iterable[Path]],
        ignored: Iterable[Path] = (),
        error_cls: type[Exception],
        apply: Callable[[I, KubeClient, dict[str, Any]], Awaitable[Any]] | None = None,
        finish: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self.kind = kind
        self.render = render
        self.client = client
        self.save = save
        self.manifest = manifest
        self.locked = locked
        self.ignored = tuple(ignored)
        self.error = error_cls
        self.apply = apply
        self.finish = finish

    # -- 검증 -------------------------------------------------------------------------

    async def dry_run(self, inst: I, obj: dict[str, Any]) -> dict[str, Any]:
        """dry-run=server. CRD 구조 스키마 + CEL 까지만 검증된다.
        오퍼레이터 검증은 apply 뒤 status.conditions 로 드러난다."""
        client = await self.client(inst)
        try:
            return await run_in_threadpool(client.dry_run_apply, obj)
        except ClusterUnreachable as e:
            raise self.error(502, "cluster_unreachable", str(e)) from e
        except Exception as e:  # noqa: BLE001 - kubernetes ApiException 등
            raise self.error(422, "invalid_manifest", api_error_message(e)) from e

    def check(
        self,
        inst: I,
        patch: dict[str, Any],
        *,
        prefix: str = "",
        ignored: bool = True,
        rendered: dict[str, Any] | None = None,
        stored: bool = False,
    ) -> None:
        """잠긴 경로·무시되는 경로를 건드리면 422 locked_field / ignored_by_operator.
        ignored=False 면 잠긴 경로만 본다. rendered 는 patch 를 얹을 렌더다. 주면 부모를 지우는
        검사가 그 렌더에 실제로 있는 잠긴 값만 보고, 없으면 엄격하게 본다(`check_overrides`).
        stored 면 저장된 overrides 이고 프록시 라벨의 값은 거부하지 않는다. 렌더가 뺀다."""
        try:
            check_overrides(
                patch,
                locked=self.locked(inst),
                ignored=self.ignored if ignored else (),
                rendered=rendered,
                refuse_proxy_label=not stored,
            )
        except MergeError as e:
            status = status_of(e.code, 422)
            raise self.error(status, e.code, prefix + e.detail, path=format_path(e.path)) from e

    def check_stored(self, inst: I, rendered: dict[str, Any] | None) -> None:
        """저장된 overrides 를 지금의 잠금 표로 다시 본다. 어긋나면 422 locked_field 다.

        overrides 는 저장할 때만 검사하고 `apply_overrides` 는 다시 보지 않는다. 그래서 잠금 표가
        넓어지기 전에 저장된 것이 지금 잠긴 경로를 건드릴 수 있다. 오류의 path 가 그 경로라
        편집기가 그 줄을 가리키고, 배포 경로에서는 `record_failure` 가 이유를 last_error 에 남긴다.
        무시되는 경로는 보지 않는다. 오퍼레이터가 어차피 덮어써 클러스터에 닿지 않는다.
        rendered 는 `check` 와 같다. 기본값을 두지 않아 부르는 쪽이 렌더 여부를 꼭 적는다. 렌더를
        손에 쥐지 않은 자리는 `check_stored_rendered` 를 쓴다."""
        self.check(
            inst,
            inst.overrides,
            prefix=STORED_PREFIX,
            ignored=False,
            rendered=rendered,
            stored=True,
        )

    async def check_stored_rendered(self, inst: I) -> None:
        """렌더를 손에 쥐지 않은 자리의 `check_stored`. 필요할 때만 렌더해 본다
        (`render_if_wiping`)."""
        self.check_stored(inst, await self.render_if_wiping(inst))

    async def render_if_wiping(self, inst: I) -> dict[str, Any] | None:
        """`check_stored` 에 줄 렌더. 렌더가 판정을 바꾸는 것은 부모를 지우는 overrides 뿐이라
        그때만 렌더하고 아니면 None 이다. 렌더 없이 보면 저장 때 지나간 것이 거부되어 배포와
        재적용이 막힌다."""
        return await self.render(inst) if wiping_paths(inst.overrides) else None

    def stored_final(self, inst: I, rendered: dict[str, Any]) -> dict[str, Any]:
        """렌더에 저장된 overrides 를 얹은 최종본. 저장된 overrides 를 클러스터로 보내는 경로는
        모두 이것을 거친다. 거부되면(`check_stored`) 아무것도 만들지 않고 멈춘다."""
        self.check_stored(inst, rendered)
        return self.final(rendered, inst.overrides)

    def final(self, rendered: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
        """렌더에 overrides 를 얹고 `finish` 로 맞춘 최종본. 최종본을 만드는 곳은 모두 이것을
        거친다."""
        final = apply_overrides(rendered, overrides)
        return final if self.finish is None else self.finish(final)

    # -- 문서 --------------------------------------------------------------------------

    async def document(
        self,
        inst: I,
        rendered: dict[str, Any],
        *,
        locked: list[str] | None = None,
        drift: bool = True,
    ) -> dict[str, Any]:
        """manifest 의 문서 하나. kind 가 편집 대상이면 overrides 를 얹고 잠금·무시 경로를 적으며,
        아니면 overrides 없이 전부 잠긴 문서다(locked 로 바꿀 수 있다). 라이브를 읽어 drift 를
        계산하고(drift=False 면 라이브만 읽는다), 클러스터 오류는 error 문자열로 남긴다.

        final 은 저장된 overrides 가 지금 잠긴 경로를 건드려도 그대로 얹어 보인다. 편집기가 그 줄을
        잠긴 줄로 표시해야 사용자가 찾아 뺄 수 있다. 드리프트 기준은 적용 경로와 같은
        `stored_final` 이라 그때는 dry-run 으로도 보내지 않고 거부 이유가 error 에 남는다."""
        editable = rendered["kind"] == self.kind
        overrides = inst.overrides if editable else {}
        final = self.final(rendered, overrides) if editable else rendered
        if locked is None:
            locked = locked_path_strings(self.locked(inst)) if editable else ["*"]
        namespace = inst.k8s.namespace
        doc: dict[str, Any] = {
            "kind": rendered["kind"],
            "name": rendered["metadata"]["name"],
            "namespace": namespace,
            "rendered": rendered,
            "overrides": overrides,
            "final": final,
            "live": None,
            "drift": None,
            "drift_patch": None,
            "drift_managers": [],
            "locked": locked,
            "ignored_by_operator": [format_path(p) for p in self.ignored] if editable else [],
            "error": None,
        }
        try:
            client = await self.client(inst)
            live = await run_in_threadpool(
                client.get, rendered["apiVersion"], rendered["kind"], doc["name"], namespace
            )
            doc["live"] = live
            if live is not None and drift:
                baseline = await self.dry_run(
                    inst, self.stored_final(inst, rendered) if editable else final
                )
                drift_patch = diff(strip_volatile(baseline), strip_volatile(live))
                doc["drift_patch"] = drift_patch
                doc["drift"] = [format_path(p) for p in key_paths(drift_patch)]
                doc["drift_managers"] = foreign_managers(live)
        except self.error as e:
            doc["error"] = f"{e.code}: {e.detail}"
        except ClusterUnreachable as e:
            doc["error"] = f"cluster_unreachable: {e}"
        return doc

    # -- 편집 --------------------------------------------------------------------------

    async def set_overrides(
        self,
        inst: I,
        *,
        patch: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Advanced 저장. patch(RFC 7386 + 키 리스트) 또는 편집한 CR 전체 중 하나.
        잠긴 필드 → 422 locked_field / ignored_by_operator, 상한을 넘는 overrides·dry-run 실패 →
        422 invalid_manifest."""
        # 상태 가드와 같은 코드다(BaseInstanceService._require, P74 2.3절)
        if getattr(inst, "deleted_at", None) is not None or inst.state == "deleting":
            raise self.error(409, "deleting", "instance is being deleted")
        if inst.state == "undeploying":
            raise self.error(409, "in_progress", "instance is being undeployed")
        rendered = await self.render(inst)
        if manifest is not None:
            patch = drop_enforced_security(rendered, diff(rendered, strip_volatile(manifest)))
        patch = patch or {}
        size = len(json.dumps(patch, separators=(",", ":")))
        if size > MAX_OVERRIDES_BYTES:
            raise self.error(
                422,
                "invalid_manifest",
                f"overrides take {size} bytes; the limit is {MAX_OVERRIDES_BYTES}",
            )
        self.check(inst, patch, rendered=rendered)
        final = self.final(rendered, patch)
        await self.dry_run(inst, final)
        inst.overrides = patch
        await self._apply(inst, final)
        return {
            "overrides": patch,
            "final": final,
            "diff": [format_path(p) for p in key_paths(patch)],
            "validation": "schema_only",
            "note": (
                "CRD schema and CEL rules only; "
                "operator validation shows up in status.conditions after apply"
            ),
        }

    async def drift_reset(self, inst: I) -> dict[str, Any]:
        """플랫폼 CR + overrides 를 SSA force 로 재적용.
        외부가 새로 더한 필드는 남는다 (drift_managers 로 표시)."""
        final = self.stored_final(inst, await self.render(inst))
        await self._apply(inst, final)
        return await self.manifest(inst)

    async def drift_absorb(self, inst: I) -> dict[str, Any]:
        """라이브와의 차이를 overrides 에 넣고 재적용해 소유권을 가져온다.
        잠긴 필드의 드리프트는 흡수하지 못한다.

        저장된 overrides 는 문서의 error 보다 먼저 본다. 문서는 그 거부를 error 로 싣기 때문에 먼저
        보지 않으면 422 locked_field 와 그 경로가 아니라 502 cluster_unreachable 로 나간다. 두 검사
        모두 문서의 렌더로 본다."""
        m = await self.manifest(inst)
        doc = next(d for d in m["documents"] if d["kind"] == self.kind)
        rendered = doc["rendered"]
        self.check_stored(inst, rendered)
        if doc["error"]:
            raise self.error(502, "cluster_unreachable", doc["error"])
        if doc["live"] is None:
            raise self.error(
                409, "state_conflict", f"live {self.kind} not found; nothing to absorb"
            )
        drift_patch = doc["drift_patch"] or {}
        if not drift_patch:
            return m
        self.check(inst, drift_patch, prefix=DRIFT_PREFIX, rendered=rendered)
        merged = compose(inst.overrides or {}, drift_patch)
        return await self.set_overrides(inst, patch=merged) | await self.manifest(inst)

    async def _apply(self, inst: I, final: dict[str, Any]) -> None:
        """SSA apply(또는 받은 `apply`) 뒤 last_spec_change·updated_at 을 같은 시각으로 저장한다."""
        client = await self.client(inst)
        if self.apply is not None:
            await self.apply(inst, client, final)
        else:
            await run_in_threadpool(client.apply, final)
        inst.last_spec_change = utcnow()
        inst.updated_at = inst.last_spec_change
        await self.save(inst)
