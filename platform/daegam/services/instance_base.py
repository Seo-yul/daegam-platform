"""MCP·Gateway·Agent 인스턴스 서비스의 공통 뼈대 (D30, resource-model.md).

세 서비스가 명사만 바꿔 똑같이 갖던 것을 모았다. 상태 가드와 조건부 저장, 클러스터·클라이언트
조회, 실패 기록, overrides dry-run, 삭제 가드 훅, Advanced 편집 위임이다. 오류 메시지의 명사는
`profile_kind` 가 채운다. 렌더·토큰·상태 규칙은 리소스마다 진짜로 달라 각 서비스에 남는다.

Advanced 편집 자체는 `services/advanced_edit.py` 의 `AdvancedEditor` 가 하고 여기서는
그것에 위임만 한다.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from contextlib import asynccontextmanager
from typing import Any, Protocol

from starlette.concurrency import run_in_threadpool

from daegam.error_codes import status_of
from daegam.errors import ServiceError, save_missed
from daegam.k8s.client import ClusterUnreachable, KubeClient
from daegam.k8s.gates import passing_features, require_tenant_secrets
from daegam.k8s.labels import LABEL_ID
from daegam.k8s.merge import format_path, key_paths
from daegam.services.advanced_edit import AdvancedEditor, EditableInstance, api_error_message
from daegam.services.claims import (
    ClaimRun,
    Claims,
    Outcome,
    Taken,
    checkpoint,
    copy_into,
    current_run,
    detached,
    driving,
    holding,
    stopping,
    take_claim,
)
from daegam.services.data_plane_auth import (
    ConsumerAuth,
    auth_rev,
    consumer_auth,
    render_epoch,
)
from daegam.services.instance_status import InstanceStatusMixin
from daegam.services.turn_gates import live_epochs, lowest_epoch
from daegam.store.auth_reapply import AUTH_FIELDS, LIVE_STATES, DeployRender
from daegam.store.base import fields_of, utcnow
from daegam.store.clusters import Cluster
from daegam.store.instances import DeployEnd

logger = logging.getLogger(__name__)

# 다른 동작이 끼어들 수 없는 상태. 이 상태의 문서에 다른 동작을 부르면 409 in_progress 다
IN_PROGRESS: frozenset[str] = frozenset({"deploying", "undeploying", "deleting"})
# 리컨실이 다시 모는 상태. deploying·failed 는 provision, undeploying 은 undeploy, deleting 은
# delete 다
RECONCILE_STATES: tuple[str, ...] = ("deploying", "failed", "undeploying", "deleting")
# 배포 경로(provision)가 도는 상태. 리컨실의 deploying·failed 갈래와 같다
PROVISION_STATES: frozenset[str] = frozenset({"deploying", "failed"})
# 배포 쪽을 고치는 동작(재배포·노출·overrides·drift·프록시)이 되는 상태
EDIT_STATES: frozenset[str] = frozenset({"deployed", "failed"})
# 해제가 모든 리소스에서 남기는 배포 쪽 필드 (P71 2.1절). state 는 골격이 직접 쓰고, 인증 재적용의
# 칸은 다음 배포의 `_stamp_auth` 가 새로 찍는다. 리소스마다 남기는 설정은 `kept_settings` 다
KEPT_ON_UNDEPLOY: frozenset[str] = frozenset(
    {"state", "last_spec_change", "updated_at", *AUTH_FIELDS}
)


async def delete_labeled(
    client: KubeClient, kinds: Collection[tuple[str, str]], namespace: str, instance_id: str
) -> None:
    """`daegam.devops.ai.kr/id` 라벨로 남은 객체를 지운다. 이름이 바뀌었거나 이름으로 지우고 남은
    것을 거두는 폴백이다. kinds 는 (apiVersion, kind) 목록이고 목록 권한이 있는 종류만 넣는다."""
    for api_version, kind in kinds:
        for obj in await run_in_threadpool(
            client.list, api_version, kind, namespace, {LABEL_ID: instance_id}
        ):
            await run_in_threadpool(
                client.delete, api_version, kind, obj["metadata"]["name"], namespace
            )


class LiveInstanceStore[I](Protocol):
    """기반 클래스가 쓰는 스토어 질의. 세 인스턴스 스토어가 모두 갖고 있다."""

    async def get(self, doc_id: str, tenant: str | None = None) -> I | None: ...
    async def set_fields(
        self,
        doc_id: str,
        fields: dict[str, Any],
        *,
        expect_state: Collection[str] | None = None,
        expect_revision: int | None = None,
    ) -> bool: ...
    async def transition(
        self,
        doc: I,
        *,
        expect: Collection[str],
        extra: Collection[str] = (),
        push: Mapping[str, Any] | None = None,
    ) -> bool: ...
    async def delete_draft(self, doc_id: str) -> bool: ...
    async def delete(self, doc_id: str) -> bool: ...
    async def live_on_cluster(self, cluster_id: str) -> list[I]: ...
    async def live_by_k8s_name(self, cluster_id: str, namespace: str, name: str) -> I | None: ...


class BaseInstanceService[I: EditableInstance](InstanceStatusMixin):
    """MCP·Gateway·Agent 인스턴스 서비스가 명사만 바꿔 똑같이 갖던 부분.

    상태 가드(`_require`·`_k8s`·`_pinned`), 조건부 저장(`_transition`·`_save_definition`·
    `_save_deployment`·`_record_error`), draft 삭제, Advanced 편집 위임, 참조 검사다.
    렌더·provision·삭제 절차는 리소스마다 달라 각 서비스에 남는다. 한 곳만 고쳐 갈라지는 일을 막는
    것이 목적이다.

    하위 클래스는 `__init__` 에서 `instances` 와 `editor` 를 채운다."""

    instances: LiveInstanceStore[I]
    editor: AdvancedEditor[I]
    cluster_service: Any  # ClusterService. 순환을 피해 느슨하게 둔다
    tenancy: Any  # TenancyService (clusters 를 들고 있다)
    svc_tokens: Any  # SvcTokens. 내부 홉의 서비스 토큰 발급 창구 (D41), wiring 이 준다

    # 하위가 채우는 셋. 골격은 같고 이것만 다르다
    error_cls: type[ServiceError] = ServiceError
    profile_kind: str = "profile"  # 오류 코드·메시지에 쓰는 이름 ("mcp server" 등)
    # 정의 저장이 쓰는 필드. 하위가 자기 스토어의 DEFINITION_FIELDS 로 채운다 (D42)
    definition_fields: frozenset[str] = frozenset()
    # 배포 쪽을 고치는 동작(재배포·노출·overrides·drift·프록시)이 되는 상태. 셋이 같은 값을 쓰고
    # 좁히려는 하위만 덮어쓴다
    edit_states: frozenset[str] = EDIT_STATES
    # 그 집합 밖에서 나는 409 의 코드. 배포된 것만 받는 동작이 대부분인 Agent 만 not_deployed 다
    edit_outside: str = "already_deployed"
    # 인증 재적용이 부르는 소비자 종류 (services/data_plane_auth.py 의 KIND_*)
    auth_kind: str = ""
    # 재적용이 다시 렌더하는 살아 있는 상태. 허브 항목 재적용은 자기 상태 집합을 쓴다
    live_states: frozenset[str] = LIVE_STATES
    # 문서를 다시 렌더할 때 판과 함께 저장하는 배포 쪽 필드. Agent 는 token_id 를, MCP Server 는
    # 채운 스냅숏(deployed_inputs)과 새 uid(k8s_uid)를 더한다
    consumer_fields: frozenset[str] = frozenset()
    # 배포 쪽 필드. 하위가 자기 스토어의 DEPLOYMENT_FIELDS 로 채운다. 해제가 이 가운데 남기는 것
    # (`KEPT_ON_UNDEPLOY`, `kept_settings`) 밖을 모델의 기본값으로 되돌린다 (P71 2.1절)
    deployment_fields: frozenset[str] = frozenset()
    # 해제가 남기는 설정. 복제가 받고 다음 배포가 다시 쓴다. Agent 는 replicas·resources 를 더한다
    kept_settings: frozenset[str] = frozenset({"exposure"})
    # provision 이 상태를 오류 코드표에서 찾는 예외 (P74 2.2절). 렌더를 provision 안에서 다시 하는
    # MCP Server 만 RenderError 를 채운다
    provision_code_only: tuple[type[Exception], ...] = ()
    # 문서 하나의 클러스터 작업을 묶는 클레임 (P66). 하위가 생성자 인자로 받는다
    claims: Claims
    # 클레임 이름의 종류 (`instance:<kind>:<id>`)
    claim_kind: str = "instance"
    # 배포가 도는 동안 돌 수 있는 렌더의 번호를 적는가 (`DeployRender`, P53 3.5절). 턴 위임
    # 게이트가 읽는 Gateway 와 Agent 다
    records_deploy_render: bool = False

    @property
    def token_auth(self) -> bool:
        """이 배포본이 oidc 로 도는가. 판정 자체는 Settings 가 갖는다."""
        return bool(self.cluster_service.settings.token_auth)

    # -- 세 리소스가 같은 골격을 쓰는 부분 ---------------------------------------------

    def _error(self, status: int, code: str, detail: str, **extra: Any) -> ServiceError:
        """이 서비스의 오류 클래스로 만든다. 라우터가 종류별로 잡으므로 클래스는 유지한다."""
        return self.error_cls(status, code, detail, **extra)

    async def _cluster_of(self, inst: I) -> Cluster:
        """인스턴스가 사는 클러스터. 없으면 422 unknown_cluster 다. 세 서비스가 클러스터를 얻는
        정본이고, 클러스터가 없어도 예외 대신 상태·로그로 끝내야 하는 자리(status·
        `_cleanup_client`)에서만 `tenancy.clusters.get` 을 직접 부른다."""
        cluster = await self.tenancy.clusters.get(inst.cluster_id)
        if cluster is None:
            raise self._error(422, "unknown_cluster", f"cluster {inst.cluster_id} missing")
        return cluster

    async def _writable_cluster_of(self, inst: I) -> Cluster:
        """인스턴스가 사는 클러스터. 테넌트 Secret 을 쓸 수 없다고 확인된 클러스터면 아무것도 쓰기
        전에 422 cluster_rights_missing 이다 (P63 2절). 재배포·노출·설정 편집·인증 재적용처럼 배포된
        문서의 클러스터에 다시 쓰는 길의 입구다."""
        cluster = await self._cluster_of(inst)
        require_tenant_secrets(cluster, self.error_cls)
        return cluster

    async def client_for_doc(self, inst: I) -> KubeClient:
        """인스턴스가 사는 클러스터의 클라이언트. 세 리소스가 같은 절차이고 svc 토큰 스윕도 이것을
        부른다."""
        cluster = await self._cluster_of(inst)
        try:
            return await run_in_threadpool(self.cluster_service.client_for, cluster)
        except ClusterUnreachable as e:
            raise self._error(502, "cluster_unreachable", str(e)) from e

    async def _cleanup_client(self, inst: I) -> KubeClient | None:
        """정리 단계(`_delete_steps`)의 클라이언트. 클러스터가 이미 지워졌으면 지울 것도 없으므로
        경고만 남기고 None 이다. 클라이언트를 못 만드는 ClusterUnreachable 은 그대로 올라가
        `record_failure` 가 502 로 기록한다."""
        cluster = await self.tenancy.clusters.get(inst.cluster_id)
        if cluster is None:
            logger.warning(
                "%s %s: cluster %s gone, skipping K8s cleanup",
                self.profile_kind,
                inst.id,
                inst.cluster_id,
            )
            return None
        return await run_in_threadpool(self.cluster_service.client_for, cluster)

    @asynccontextmanager
    async def record_failure(
        self, inst: I, *, code_only: tuple[type[Exception], ...] = ()
    ) -> AsyncIterator[None]:
        """provision·delete 의 실패를 인스턴스 문서에 남기고 서비스 오류로 올린다.

        블록이 터지면 `_record_error` 가 `last_error` 에 `<code>: <detail>` 을 적고 `updated_at` 을
        찍어 저장한다. 상태는 배포 경로(`deploying`)에서만 `failed` 로 옮긴다. `undeploying`·
        `deleting` 에 멈춘 문서는 reconcile 이 다시 몰아가고 화면은 last_error 로 이유를 읽는다.

        - 서비스 오류(이 서비스의 것, 테넌시의 것 모두): 코드·상태를 그대로 두고 다시 던진다. 오류의
          모양은 하나라(D30) 종류의 클래스로 다시 싸지 않는다 (P71 2.2절)
        - ClusterUnreachable: 502 cluster_unreachable
        - code_only 로 준 예외(MCP 의 RenderError): 그 예외의 `code` 와 오류 코드표의 상태다. 표에
          없으면 422 (P74 2.2절)
        - 그 밖(RBAC 403 등 kubernetes 예외): 502 k8s_error + 메시지
        """
        try:
            yield
        except ServiceError as e:
            await self._record_error(inst, f"{e.code}: {e.detail}")
            raise
        except ClusterUnreachable as e:
            await self._record_error(inst, f"cluster_unreachable: {e}")
            raise self._error(502, "cluster_unreachable", str(e)) from e
        except code_only as e:
            code = str(getattr(e, "code", "invalid_manifest"))
            await self._record_error(inst, f"{code}: {e}")
            raise self._error(status_of(code, 422), code, str(e)) from e
        except Exception as e:  # noqa: BLE001 - RBAC 403 등 K8s 오류를 메시지와 함께 502 로
            msg = api_error_message(e)
            await self._record_error(inst, f"k8s_error: {msg}")
            raise self._error(502, "k8s_error", msg) from e

    async def _record_error(self, inst: I, message: str) -> None:
        """실패의 이유를 문서에 남긴다. 배포 경로의 실패만 `failed` 다. 해제·삭제 도중의 실패는
        상태를 두고 이유만 남긴다. 합친 리소스 셋은 문서 전체를 쓰는 저장이 없으므로(D42) 상태와
        이유만 쓰고, 그 사이 문서가 다른 상태로 갔거나 사라졌으면 남기지 않는다."""
        before = inst.state
        inst.state = "failed" if before == "deploying" else before
        inst.last_error = message
        inst.updated_at = utcnow()
        saved = await self.instances.set_fields(
            inst.id,
            fields_of(inst, {"state", "last_error", "updated_at"}),
            expect_state={before},
        )
        if not saved:
            logger.warning(
                "%s %s: error not recorded (state changed or document gone): %s",
                self.profile_kind,
                inst.id,
                message,
            )
        elif before == "deploying":
            await self._deploy_ended(inst, "failed")

    # -- 소비자 다시 렌더: 서비스 토큰 회전(D41)과 인증 재적용(P32 8절) --------------------------

    async def _consumer_auth(self, tenant: str) -> ConsumerAuth:
        """소비자 렌더의 인증 입력. 소비자 판과 같은 함수에서 나온다."""
        ts = await self.tenancy.tenant_settings.get(tenant)
        return consumer_auth(self.cluster_service.settings, ts)

    async def want_rev(self, doc: I, ts: Any) -> str | None:
        """재적용이 보는 판의 첫 절반. 지금 인증 입력의 소비자 판이다 (`ReapplyKind`)."""
        return auth_rev(self.cluster_service.settings, ts, self.auth_kind, doc)

    async def paced_revs(self, doc: I, ts: Any) -> frozenset[str | None]:
        """기대 판과 표시 하나만큼만 다른 첫 절반들 (`ReapplyKind`). 기본은 없다."""
        return frozenset()

    async def _stamp_auth(self, doc: I, *, rotate: bool = False) -> None:
        """지금 인증 입력의 소비자 판과 그 클러스터에서의 이 종류의 렌더 번호(`render_epoch`)를
        문서에 적고 재적용 기록을 비운다.
        렌더보다 먼저 부른다. 그 사이 입력이 바뀌면 옛 판이 남아 다음 틱이 한 번 더 렌더할
        뿐이고, 새 판 뒤에 옛 렌더가 숨는 일은 없다. 저장은 부르는 쪽의 배포 쪽 저장이 한다.
        rotate 는 서비스 토큰 회전이다. 회전에 렌더하지 않는 객체가 있는 종류(MCP Server)는 그때
        판을 두어 그 객체가 다음 틱에 렌더되게 한다."""
        ts = await self.tenancy.tenant_settings.get(doc.tenant)
        passing = passing_features(await self._cluster_of(doc))
        binds = consumer_auth(self.cluster_service.settings, ts).binds_delegation
        epoch = render_epoch(self.auth_kind, passing, binds_delegation=binds)
        doc.mark_rendered(await self.want_rev(doc, ts), epoch)

    async def rotate_svc_token(self, doc: I) -> bool:
        """서비스 토큰을 새로 내어 소비자를 다시 렌더한다 (리컨실 스윕, D41). 토큰 판이 파드 템플릿
        어노테이션에 들어가므로 다시 렌더하는 것이 곧 롤아웃이다."""
        return await self._rerender(doc, rotate=True, action="rotate svc token")

    async def reapply_auth(self, doc: I) -> bool:
        """지금 인증 설정으로 소비자를 다시 렌더한다 (P32 8절). 서비스 토큰은 Secret 의 것을 다시
        쓰되 audience 가 그 서버에 원하는 것과 다르면 새로 내고(P50 9절), Agent 의 gateway_token 은
        원하는 iss·aud·groups 와 다르면 새로 낸다."""
        return await self._rerender(doc, rotate=False, action="reapply auth")

    def _start_deployment(self, inst: I) -> None:
        """문서를 새 배포의 `deploying` 으로 옮긴다. 저장은 호출자의 전이가 한다. 기록을 적는 종류는
        이 배포 앞에 적용돼 아직 돌 수 있는 렌더 가운데 가장 낮은 번호를 함께 둔다(`DeployRender`).
        draft 에서 배포하면 그런 렌더가 없다."""
        prior = (
            lowest_epoch(live_epochs(self.auth_kind, inst)) if self.records_deploy_render else None
        )
        inst.state = "deploying"
        inst.deploying_since = utcnow()
        if self.records_deploy_render:
            inst.deploy_render = DeployRender(since=inst.deploying_since, prior=prior)

    async def _record_deploy_render(self, inst: I, live: list[int]) -> None:
        """provision 이 렌더를 적용하기 전에 이 배포가 적용하는 번호를 적는다 (P53 3.5절). live 는
        번호를 찍기 전에 이 문서에서 돌 수 있던 렌더의 번호다. 그 사이 상태나 배포가 바뀌었으면 적지
        않는다. 그러면 게이트는 적힌 렌더 번호로 읽는다."""
        if not self.records_deploy_render:
            return
        inst.deploy_render = DeployRender(
            since=inst.deploying_since, epoch=inst.render_epoch, prior=lowest_epoch(live)
        )
        await self.instances.set_fields(
            inst.id,
            fields_of(inst, {"deploy_render"}),
            expect_state={"deploying"},
            expect_deployment=inst.deploying_since,
        )

    async def _rerender(self, doc: I, *, rotate: bool, action: str) -> bool:
        """두 동작의 뼈대. 다시 읽고, 살아 있는 상태인지 보고, 판을 적고, 하위의 `_apply_consumer`
        로 렌더해 적용한 뒤 배포 쪽 필드를 살아 있는 상태 조건으로 저장한다. 클러스터 왕복 사이에
        해제가 끝났으면 문서를 deployed 로 되살리지 않고 False 다. 남은 객체는 다음 해제·리컨실이
        이름으로 정리한다. 그 사이 재배포가 끝났으면 이 렌더가 새 배포의 객체를 옛 스냅숏으로
        되돌렸을 수 있으므로 저장하지 않고 다음 틱이 다시 렌더하게 한다(`_same_deployment`).

        문서의 클레임을 쥐고 돈다. 남이 쥐고 있으면 건너뛰고 False 다(실패가 아니다, P66 F3)."""
        doc = await self._fresh(doc)
        self._require(doc, self.live_states, action=action, outside="not_deployed")
        return await self._claimed_or_skip(
            doc,
            self.live_states,
            action=action,
            work=lambda: self._rerender_claimed(doc, rotate=rotate, action=action),
        )

    async def _rerender_claimed(self, doc: I, *, rotate: bool, action: str) -> bool:
        # 토큰 회전과 인증 재적용은 Secret 을 다시 쓴다. 쓸 수 없다고 확인된 클러스터는 아무것도
        # 쓰기 전에 거절하고, 스윕이 그 실패를 남긴다 (P63 2절)
        await self._writable_cluster_of(doc)
        deployment = doc.deploying_since
        client = await self.client_for_doc(doc)
        await self._stamp_auth(doc, rotate=rotate)
        await self._apply_consumer(doc, client, rotate=rotate)
        doc.updated_at = utcnow()
        if not await self._same_deployment(doc, deployment, action):
            return False
        names = {
            *self.consumer_fields,
            *AUTH_FIELDS,
            "svc_token_rev",
            "svc_token_servers",
            "updated_at",
        }
        ok = await self.instances.set_fields(
            doc.id, fields_of(doc, names), expect_state=self.live_states
        )
        if not ok:
            logger.warning(
                "%s %s: %s done but the document is no longer deployed",
                self.profile_kind,
                doc.id,
                action,
            )
        return ok

    async def _same_deployment(self, doc: I, deployment: Any, action: str) -> bool:
        """렌더를 시작할 때 읽은 배포(`deploying_since`)가 아직 그 문서의 배포인가 (P50 1절).
        아니면 그 사이 재배포가 끝난 것이다. 그 문서가 살아 있으면 렌더 번호를 다시 렌더 표시로 적어
        (`mark_stale`) 다음 틱이 새 배포의 스냅샷으로 다시 렌더하게 하고 False 다. 재렌더는 클레임을
        쥐므로 재배포와의 순서를 클레임이 정하고, 이 검사는 클레임을 잃었을 때만 걸린다 (P66 F3)."""
        fresh = await self.instances.get(doc.id)
        if fresh is None:
            return False
        if fresh.deploying_since == deployment:
            return True
        logger.warning(
            "%s %s: %s raced a new deployment; the next tick renders it again",
            self.profile_kind,
            doc.id,
            action,
        )
        await self.instances.mark_stale(doc.id, expect_state=self.live_states)
        return False

    async def _apply_consumer(self, doc: I, client: KubeClient, *, rotate: bool) -> None:
        """하위가 채우는 훅. 서비스 토큰을 고르고(rotate 면 새로, 아니면 Secret 의 것) 소비자
        객체를 렌더해 적용하며 `svc_token_rev` 를 맞춘다."""
        raise NotImplementedError

    # -- 조건부 저장 (D42) -------------------------------------------------------------
    # 한 문서에 정의 쪽과 배포 쪽이 산다. 문서 전체를 쓰면 낡은 스냅샷이 남의 쪽을 되돌리므로
    # 저장은 자기 쪽 필드만 조건부로 `$set` 하고, 0건이면 409 다. `updated_at` 은 여기서 찍는다

    async def _transition(
        self,
        inst: I,
        expect: Collection[str],
        *,
        action: str,
        extra: Collection[str] = (),
        push: Mapping[str, Any] | None = None,
    ) -> None:
        """상태를 바꾸는 저장. `_require` 는 메모리의 문서를 보고, 이 저장은 DB 의 상태가 아직
        expect 안에 있을 때만 쓴다. 검사와 쓰기 사이에 다른 요청이 먼저 상태를 바꿨으면(같은
        draft 에 배포 둘, 삭제와 배포, 해제와 재배포) 409 state_conflict 다. extra 는 같은 저장에서
        함께 쓸 저장 단위 밖의 칸이고, push 는 같은 갱신의 `$push` 다(Agent 의 배포 기록, P45
        6절)."""
        inst.updated_at = utcnow()
        if not await self.instances.transition(inst, expect=expect, extra=extra, push=push):
            raise self._state_conflict(inst, action)

    async def _save_deployment(
        self, inst: I, names: Collection[str], *, expect_state: Collection[str], action: str
    ) -> None:
        """배포 쪽 필드 가운데 names 와 `updated_at` 만 쓰는 저장. 정의 쪽(PUT)과 겹쳐도, 상태가
        같은 채 겹치는 다른 배포 쪽 저장과 겹쳐도 서로를 지우지 않는다. 배포 필드를 통째로 쓰면
        `expect_state` 가 잡지 못하는 경합이 남는다. 스윕의 토큰 회전이 방금 켠 노출을, 노출 저장이
        방금 저장한 overrides 를 되돌리는 것이 그것이다. 상태가 바뀌었으면 409 state_conflict 다."""
        inst.updated_at = utcnow()
        saved = await self.instances.set_fields(
            inst.id,
            fields_of(inst, {*names, "updated_at"}),
            expect_state=expect_state,
        )
        if not saved:
            raise self._state_conflict(inst, action)

    async def _save_definition(self, inst: I, *, expect_revision: int, action: str) -> None:
        """정의 쪽 필드만 쓰는 저장. 저장된 revision 이 expect_revision 일 때만이라 PUT 둘이 겹치면
        하나만 지나가고(409 revision_conflict), 배포 쪽은 손대지 않으므로 낡은 스냅샷이 도는 중인
        배포를 draft 로 되돌리지 못한다. 맞지 않으면 다시 읽어 답을 고른다(`save_missed`). 그사이
        지워졌으면 404 not_found, 삭제 중이면 409 deleting, 그 밖은 지금의 revision 을 실은 409
        revision_conflict 다 (P48 6.7절)."""
        inst.updated_at = utcnow()
        saved = await self.instances.set_fields(
            inst.id,
            fields_of(inst, self.definition_fields),
            expect_revision=expect_revision,
        )
        if not saved:
            latest = await self.instances.get(inst.id)
            raise save_missed(
                self._error,
                self.profile_kind,
                inst.name,
                found=latest is not None,
                deleting=latest is not None and latest.state == "deleting",
                action=action,
                revision=getattr(latest, "revision", None),
            )

    def _state_conflict(self, inst: I, action: str) -> ServiceError:
        return self._error(
            409,
            "state_conflict",
            f"{self.profile_kind} {inst.name} changed state; retry {action}",
        )

    async def delete_draft(self, inst: I) -> None:
        """draft 문서만 지운다. 그 사이 배포가 시작됐으면 409 state_conflict 다."""
        if not await self.instances.delete_draft(inst.id):
            raise self._state_conflict(inst, "delete")

    # -- 배포·해제·삭제의 골격 (P71 2.1절). 리소스마다 다른 단계는 아래 훅이 채운다 ------------
    #
    # 훅과 도는 자리:
    # - `_provision(inst, **steps)`: provision 의 `record_failure` 안. 클러스터에 적용한다
    # - `_guard_undeploy`: undeploy 의 상태 가드 바로 뒤, 다시 몰아가는 `undeploying` 에서도
    # - `_guard_delete`: delete 의 상태 가드 뒤, draft 갈래 앞. 이미 `deleting` 이면 건너뛴다
    # - `_revoke_access`: undeploy·delete 가 `undeploying`·`deleting` 으로 옮긴 뒤, 지우기 전
    # - `_teardown(inst, deleting=)`: undeploy·delete 의 `record_failure` 안. 기본은 `_delete_steps`
    # - `_after_cleared`: undeploy 가 배포 쪽을 비운 뒤, 끝 전이 전
    # - `_deploy_ended(inst, outcome)`: provision 의 끝 전이 뒤(`deployed`), 배포 경로의 실패를 적은
    #   뒤(`failed`)
    # - `_after_draft_deleted`: delete 가 draft 문서를 지운 뒤
    # - `_after_cluster_removed`: delete 가 클러스터의 것을 지운 뒤, 문서를 지우기 전

    async def provision(self, inst: I, **steps: Any) -> I:
        """`deploying` 문서를 클러스터에 적용하고 `deployed` 로 저장한다. `failed` 면 먼저
        `deploying` 으로 되돌린다. 적용은 하위의 `_provision` 이고 steps 는 그리로 그대로 간다(Agent
        의 방금 발급한 토큰). 실패하면 `failed` 에 last_error 를 남기고 리컨실이 다시 몰아간다.

        문서의 클레임을 쥐고 돈다 (P66 2.1절). 남이 쥐고 있으면 409 in_progress 다. 쥔 뒤 문서를
        다시 읽고 그 상태로 돈다. 도는 사이 삭제가 상태만 옮기고 일을 남겼으면 같은 클레임으로
        삭제를 마치고 409 state_conflict 다. 끝나면(실패해도) 클레임 밖에서 상태를 한 번 더 본다."""
        holder = current_run()
        inherited = holder is not None and holder.doc_id == inst.id
        async with self._claim_scope(inst, PROVISION_STATES, action="provision"):
            await self._provision_claimed(inst, steps)
        if inherited:
            # 바깥 동작(재배포·Basic 편집)이 이어 쓰는 클레임을 배포를 마친 문서에 맞춘다
            self._place_claim(inst)
        return inst

    async def _provision_claimed(self, inst: I, steps: dict[str, Any]) -> None:
        run = current_run()
        assert run is not None
        if inst.state == "failed":
            self._start_deployment(inst)
            await self._transition(inst, {"failed"}, action="provision")
            self._place_claim(inst)
        self._require(inst, {"deploying"}, action="provision")
        live = live_epochs(self.auth_kind, inst)
        await self._stamp_auth(inst)
        await self._record_deploy_render(inst, live)
        try:
            await checkpoint()
            async with self.record_failure(inst, code_only=self.provision_code_only):
                await self._provision(inst, **steps)
            await checkpoint()
            inst.state = "deployed"
            inst.last_error = None
            inst.last_spec_change = utcnow()
            await self._transition(inst, {"deploying"}, action="finish provision")
            await self._deploy_ended(inst, "deployed")
        except ServiceError as e:
            # 조건부 저장이 맞지 않았다. 체크포인트처럼 지금 자리를 보고 남은 일을 마친다
            if e.code != "state_conflict":
                raise
            outcome = await run.verdict()
            if outcome is not Outcome.CONTINUE:
                await self._settle(inst, run, outcome)
            raise

    async def undeploy(self, inst: I, *, wait: bool = True) -> I:
        """클러스터에서 내리고 정의는 남긴다. 가드(`_guard_undeploy`)를 지나 `undeploying` 을
        거쳐 배포 쪽을 비우고 `draft` 로 돌아간다. 실패하면 undeploying + last_error 로 남고
        리컨실이 다시 몰아간다.

        상태를 옮긴 뒤 클레임을 쥔다. 남이 쥐고 있으면 wait 일 때 `CLAIM_WAIT_SECONDS` 까지
        기다리고, 그 사이 홀더가 해제를 마쳤으면 그 문서로 답한다. 그래도 쥐고 있으면 `undeploying`
        문서로 답하고 일은 홀더가 마친다 (P66 2.6절). wait 가 아니면 409 in_progress 다."""
        self._require(inst, {"deployed", "failed", "undeploying"}, action="undeploy")
        await self._guard_undeploy(inst)
        if inst.state != "undeploying":
            inst.state = "undeploying"
            await self._transition(inst, EDIT_STATES, action="undeploy")
        run = await self._take_claim(
            inst,
            {"undeploying"},
            wait=wait,
            done=lambda fresh: fresh is None or fresh.state == "draft",
        )
        if run is Taken.DONE:
            await self._reread_into(inst)
            return inst
        if run is Taken.DEFERRED:
            return inst
        assert not isinstance(run, Taken)
        async with holding(run, {"undeploying"}):
            async with stopping(run) as stop:
                await self._undeploy_claimed(inst, wait=wait)
            if stop.outcome is not None:
                await self._settle(inst, run, stop.outcome)
        if stop.outcome is Outcome.LOST:
            # 새 홀더가 마친다. 메모리에서 비운 배포 쪽 대신 저장된 문서로 답한다
            await self._reread_into(inst)
        elif stop.outcome is not None:
            # 문서가 사라졌거나 옮겨 갔으면 이 해제가 할 일이 아니었다
            raise self._state_conflict(inst, "undeploy")
        return inst

    async def _undeploy_claimed(self, inst: I, *, wait: bool) -> None:
        await checkpoint()
        await self._stop_running(inst, wait=wait)
        await self._revoke_access(inst)
        await self._revoke_unrecorded(inst)
        async with self.record_failure(inst):
            await self._teardown(inst, deleting=False)
        self._clear_deployment(inst)
        await self._after_cleared(inst)
        await checkpoint()
        # 이름의 선점(공개 host, Gateway 의 Entry 이름)은 draft 로 바꾼 뒤 놓는다. 읽어 둔 nonce 로
        # 지우므로 바로 뒤 다시 선점한 배포의 기록은 남는다 (P70 2절)
        held = await self._held_claims(inst)
        inst.state = "draft"
        await self._transition(inst, {"undeploying"}, action="finish undeploy")
        await self._release_claims(held)

    async def delete(self, inst: I) -> bool:
        """정의와 클러스터의 것을 모두 지운다 (`_delete`). 지금 지웠으면 참이고, 남이 쥔 클레임에
        일을 남겼으면 거짓이다(문서는 `deleting` 이다)."""
        return await self._delete(inst) is not None

    async def drive(self, inst: I) -> bool:
        """리컨실이 멈춘 문서를 상태에 맞는 동작으로 몰아간다. 남이 쥔 클레임은 기다리지 않는다
        (리컨실 리스가 그 시간을 버티지 못한다). 일을 홀더에게 남겼으면 거짓이다 (P66 2.6절)."""
        if inst.state in PROVISION_STATES:
            await self.provision(inst)
            return True
        if inst.state == "undeploying":
            return (await self.undeploy(inst, wait=False)).state == "draft"
        return await self._delete(inst, wait=False) is not None

    async def _delete(self, inst: I, *, wait: bool = True) -> int | None:
        """삭제의 골격. 돌려주는 것은 회수한 토큰 수다(`_revoke_access`). None 이면 남이 쥔
        클레임에 일을 남겼다.

        가드(`_guard_delete`)를 지나 draft 는 문서만 지운다. 배포 쪽이 있으면 `deleting` 으로 옮겨
        클레임을 쥐고 클러스터의 것을 지우며 문서는 맨 나중에 지운다. 그 앞에서 죽으면 `deleting`
        문서가 남아 리컨실이 마저 한다. 실패하면 deleting + last_error 로 남는다. 남이 클레임을
        쥐고 있으면 wait 일 때 `CLAIM_WAIT_SECONDS` 까지 기다린다. 그 사이 홀더가 삭제를 마쳤으면
        읽을 때 살아 있던 토큰 가운데 지금 회수된 수로 답한다 (P66 2.6절)."""
        self._require(
            inst, {"draft", "deploying", "deployed", "failed", "deleting"}, action="delete"
        )
        if inst.state != "deleting":
            await self._guard_delete(inst)
        if inst.state == "draft":
            await self.delete_draft(inst)
            await self._after_draft_deleted(inst)
            # 쓸기 전에 남은 선점이 있으면 함께 놓는다 (P70 2절)
            await self._release_all_claims(inst)
            return 0
        if inst.state != "deleting":
            inst.state = "deleting"
            await self._transition(inst, {"deploying", "deployed", "failed"}, action="delete")
        access = await self._active_access(inst)
        run = await self._take_claim(
            inst, {"deleting"}, wait=wait, done=lambda fresh: fresh is None
        )
        if run is Taken.DONE:
            return await self._count_revoked(access)
        if run is Taken.DEFERRED:
            return None
        assert not isinstance(run, Taken)
        revoked: int | None = None
        async with holding(run, {"deleting"}):
            async with stopping(run) as stop:
                revoked = await self._delete_claimed(inst, wait=wait)
            if stop.outcome is not None:
                await self._settle(inst, run, stop.outcome)
        if stop.outcome is None:
            return revoked
        # 멈췄어도 문서가 이미 없으면 지운 것이다
        if await self.instances.get(inst.id) is None:
            return await self._count_revoked(access)
        return None

    async def _delete_claimed(self, inst: I, *, wait: bool) -> int:
        await checkpoint()
        await self._stop_running(inst, wait=wait)
        revoked = await self._revoke_access(inst)
        await self._revoke_unrecorded(inst)
        async with self.record_failure(inst):
            await self._teardown(inst, deleting=True)
        await checkpoint()
        await self._after_cluster_removed(inst)
        await self.instances.delete(inst.id)
        # 지운 문서 뒤에 올 배포가 없으므로 그 리소스의 선점을 모두 놓는다 (P70 2절)
        await self._release_all_claims(inst)
        return revoked

    # -- 클레임 (P66). 범위와 판정은 services/claims.py 에 있고 여기는 결과를 고른다 ----------

    async def _take_claim(
        self,
        inst: I,
        expect: Collection[str],
        *,
        wait: bool = False,
        done: Callable[[Any], bool] | None = None,
    ) -> ClaimRun | Taken:
        """이 문서의 클레임을 쥔다(`take_claim`). 기다리지 않는 자리에서 남이 쥐고 있으면 409
        in_progress, 쥐고 보니 문서가 바뀌었으면 409 state_conflict 다. 돌려주는 Taken 은 DONE 과
        DEFERRED 뿐이다."""
        run = await take_claim(
            self.claims, self.claim_kind, inst, expect, self.instances.get, wait=wait, done=done
        )
        if run is Taken.HELD:
            raise self._error(
                409,
                "in_progress",
                f"{self.profile_kind} {inst.name} is being changed by another action",
            )
        if run is Taken.CHANGED:
            raise self._state_conflict(inst, "the action")
        return run

    @asynccontextmanager
    async def _claim_scope(
        self, inst: I, expect: Collection[str], *, action: str
    ) -> AsyncIterator[None]:
        """문서의 클레임을 쥐는 범위 (P66 2.1절, F3). provision 과 사람의 동작(재배포·Basic 편집·
        Advanced 편집)이 쓰고, 배경 작업은 `_claimed_or_skip` 으로 쓴다.

        남이 쥐고 있으면 409 in_progress, 쥐고 보니 문서가 바뀌었으면 409 state_conflict 다. 같은
        문서의 클레임을 이미 쥐고 있으면 물려받는다. 도는 사이 클레임을 잃거나 문서가 옮겨 가면
        결과를 고른 뒤(`_settle`) 409 state_conflict 다. 스스로 쥔 클레임이면 놓은 뒤(본문이
        실패했어도) 상태를 한 번 더 본다. 그 사이 삭제·해제가 상태만 옮기고 일을 남겼을 수 있다."""
        run = await self._take_claim(inst, expect)
        assert not isinstance(run, Taken)
        fresh_run = run is not current_run()
        try:
            async with holding(run, expect):
                async with stopping(run) as stop:
                    yield
                if stop.outcome is not None:
                    await self._settle(inst, run, stop.outcome)
        finally:
            if fresh_run:
                await self._recheck_after_release(inst)
        if stop.outcome is not None:
            raise self._state_conflict(inst, action)

    async def _claimed_or_skip(
        self,
        inst: I,
        expect: Collection[str],
        *,
        action: str,
        work: Callable[[], Awaitable[bool]],
    ) -> bool:
        """배경 작업(재렌더, 복제본 옮기기, 스윕의 문서 단위 쓰기)의 클레임 범위 (P66 F3). 남이 쥐고
        있거나 도는 사이 문서가 바뀌면 실패가 아니라 건너뜀이라 False 이고 다음 틱이 다시 본다.
        리컨실 틱 안에서는 몰아가는 중(`driving`)으로 돌아, 틱의 리스를 잃어도 이 쓰기는 자기
        클레임으로 마친다 (2.3절)."""
        with driving():
            try:
                async with self._claim_scope(inst, expect, action=action):
                    return await work()
            except ServiceError as e:
                if e.code not in ("in_progress", "state_conflict"):
                    raise
                logger.info("%s %s: %s skipped (%s)", self.profile_kind, inst.id, action, e.code)
                return False

    def _place_claim(self, inst: I) -> None:
        """쥔 클레임의 자리와 기대 상태를 문서의 지금 배포에 맞춘다. 이 동작이 문서를 옮겼을 때
        (재배포의 `deploying` 전이, provision 의 `failed` 되돌리기와 끝) 부른다. 그러지 않으면 다음
        체크포인트가 자기 배포를 남의 배포로 본다."""
        run = current_run()
        assert run is not None and run.doc_id == inst.id, "no claim held for this document"
        run.place(inst)
        run.expect = frozenset({inst.state})

    async def _reread_into(self, inst: I) -> None:
        fresh = await self.instances.get(inst.id)
        if fresh is not None:
            copy_into(inst, fresh)

    async def _settle(self, inst: I, run: ClaimRun, outcome: Any) -> None:
        """멈춘 실행의 결과를 고른다. 옮겨 간 상태의 동작은 같은 클레임으로 마치고, 문서가
        사라졌거나 옮겨 갔으면 이 실행이 만든 것을 거둔다. 그 밖은 멈추기만 한다."""
        try:
            if outcome in (Outcome.DELETING, Outcome.UNDEPLOYING):
                fresh = await self.instances.get(inst.id)
                if fresh is None:
                    return
                run.place(fresh)
                if outcome is Outcome.DELETING:
                    await self._delete(fresh)
                else:
                    await self.undeploy(fresh)
            elif outcome in (Outcome.GONE, Outcome.MOVED):
                await self._compensate(inst, run, outcome)
        except ServiceError as e:
            logger.warning(
                "%s %s: finishing after %s failed: %s", self.profile_kind, inst.id, outcome, e
            )

    async def _compensate(self, inst: I, run: ClaimRun, outcome: Outcome) -> None:
        """클레임을 넘긴 뒤 이 실행이 적용한 것을 이름으로 지우고 낸 것을 회수한다 (P66 2.5절).
        문서가 없거나 삭제 중이면 문서 단위로 회수하고, 옮겨 갔으면 이 실행이 낸 id 만 회수한다.
        새 배포의 토큰도 같은 라벨·scope·인스턴스를 달기 때문이다. 문서가 draft 였다가 그 사이 다시
        배포됐으면 같은 이름을 새 배포가 쓰므로 이름으로 지우지 않는다."""
        with detached():
            fresh = await self.instances.get(inst.id)
            reused = (
                outcome is Outcome.GONE
                and fresh is not None
                and fresh.state not in ("draft", "deleting")
            )
            if inst.k8s is not None and inst.cluster_id is not None and not reused:
                try:
                    await self._teardown(inst, deleting=False)
                except Exception as e:  # noqa: BLE001 - 남은 것은 로그로 알린다
                    logger.warning(
                        "%s %s: leftover cleanup failed: %s", self.profile_kind, inst.id, e
                    )
            if outcome is Outcome.GONE and (fresh is None or fresh.state == "deleting"):
                await self._revoke_access(inst)
                await self._revoke_unrecorded(inst)
            await self._revoke_issued(run.issued)

    async def _recheck_after_release(self, inst: I) -> None:
        """클레임을 놓은 뒤 상태를 한 번 더 본다. 그 사이 삭제·해제가 상태만 옮기고 일을 남겼으면
        다시 쥐고 마친다. 남이 쥐고 있으면 그쪽이 마친다 (P66 2.2절). 실패해도 부른 쪽의 답을
        가리지 않는다."""
        try:
            fresh = await self.instances.get(inst.id)
            if fresh is None or fresh.state not in ("deleting", "undeploying"):
                return
            if fresh.state == "deleting":
                await self._delete(fresh, wait=False)
            else:
                await self.undeploy(fresh, wait=False)
        except Exception as e:  # noqa: BLE001 - 남은 일은 홀더나 리컨실이 마친다
            logger.info("%s %s: left the rest to its holder: %s", self.profile_kind, inst.id, e)

    async def _active_access(self, inst: I) -> list[str]:
        """삭제가 클레임을 기다리기 전 살아 있던 토큰 id. 기본은 없다."""
        return []

    async def _count_revoked(self, ids: list[str]) -> int:
        """ids 가운데 지금 회수된 수. 홀더가 삭제를 마쳤을 때의 답이다. 기본은 0 이다."""
        return 0

    async def _revoke_unrecorded(self, inst: I) -> None:
        """문서에 적기 전에 만들어진 토큰을 문서 단위로 회수한다. 기본은 없다."""

    async def _revoke_issued(self, ids: list[str]) -> None:
        """이 실행이 낸 토큰 id 를 회수한다. 기본은 없다."""

    def _clear_deployment(self, inst: I) -> None:
        """배포 쪽을 비운다. 남기는 것(`KEPT_ON_UNDEPLOY`, `kept_settings`) 밖의 배포 쪽 필드를
        모델의 기본값으로 되돌린다. 나중에 더한 배포 쪽 필드도 남긴다고 적지 않으면 비워진다."""
        fields = type(inst).model_fields
        for name in self.deployment_fields - KEPT_ON_UNDEPLOY - self.kept_settings:
            setattr(inst, name, fields[name].get_default(call_default_factory=True))

    async def _provision(self, inst: I, **steps: Any) -> None:
        """클러스터에 적용하는 단계. 하위가 채운다."""
        raise NotImplementedError

    async def _delete_steps(self, inst: I) -> None:
        """클러스터의 것을 지우는 단계. 하위가 채운다."""
        raise NotImplementedError

    async def _teardown(self, inst: I, *, deleting: bool) -> None:
        """해제·삭제가 `record_failure` 안에서 도는 단계. 기본은 `_delete_steps` 하나다. 삭제에만
        더하는 단계가 있는 하위(Agent 의 장기 기억·답변 피드백)가 덮어쓴다."""
        await self._delete_steps(inst)

    async def _guard_undeploy(self, inst: I) -> None:
        """해제를 막는 가드(게시·참조). 상태를 옮기기 전에 돈다. 기본은 없다."""

    async def _guard_delete(self, inst: I) -> None:
        """삭제를 막는 가드(게시·참조). draft 갈래보다 먼저 돌고, 이미 `deleting` 이면 돌지 않는다.
        기본은 없다."""

    async def _stop_running(self, inst: I, *, wait: bool) -> None:
        """해제와 삭제가 토큰을 회수하기 전에 이 인스턴스에서 도는 일을 멈춘다. wait 이면 API 경로라
        멈출 때까지 잠시 기다리고, 아니면(리컨실) 멈추라고만 한다. 기본은 없다."""

    async def _revoke_access(self, inst: I) -> int:
        """클러스터의 것을 지우기 전에 이 인스턴스의 토큰을 회수한다. 회수한 수다. 기본은 없다."""
        return 0

    async def _deploy_ended(self, inst: I, outcome: DeployEnd) -> None:
        """배포 하나가 끝났다. provision 의 끝 전이(`deployed`)와 배포 경로의 실패 기록(`failed`)
        뒤에 부른다. 리컨실이 기동 뒤에 마치는 배포도 provision 을 거친다. 기본은 없다. Agent 는
        배포 기록의 그 줄에 결과를 적는다 (P45 6절)."""

    async def _after_cleared(self, inst: I) -> None:
        """해제가 배포 쪽을 비운 뒤, 끝 전이 전에 더 저장할 것. 기본은 없다."""

    async def _after_draft_deleted(self, inst: I) -> None:
        """draft 문서를 지운 뒤 함께 지울 것. 기본은 없다."""

    async def _after_cluster_removed(self, inst: I) -> None:
        """삭제가 클러스터의 것을 지운 뒤, 문서를 지우기 전에 정리할 것. 기본은 없다."""

    # 이름의 선점 (P70, D82). 아래 기본은 아무것도 하지 않는다. MCP Server·Gateway·Agent 서비스
    # 셋이 모두 `ExposureValidationMixin` 을 먼저 섞어 이것을 덮는다

    async def _held_claims(self, inst: I) -> list[Any]:
        return []

    async def _release_claims(self, held: list[Any]) -> None:
        return None

    async def _release_all_claims(self, inst: I) -> None:
        return None

    async def _save_overrides(self, inst: I) -> None:
        """AdvancedEditor 의 저장 콜백. overrides 는 배포 쪽 필드다.

        저장은 렌더에 overrides 를 얹은 최종본을 적용한 뒤라 인증 재적용의 막힘(`auth_blocked`)도
        비운다. 막힘은 기대 판이 바뀔 때만 풀리므로, 비우지 않으면 잠긴 경로를 건드리던 overrides 를
        고쳐도 재적용이 멈춘 채 남는다. 원인이 남았으면 다음 틱이 다시 막는다. 같은 저장이 함께 쓸
        칸은 `_override_fields` 가 더한다."""
        inst.auth_blocked = None
        await self._save_deployment(
            inst,
            {"overrides", "last_spec_change", "auth_blocked", *self._override_fields(inst)},
            expect_state=self.edit_states,
            action="save overrides",
        )

    def _override_fields(self, inst: I) -> Collection[str]:
        """overrides 저장이 같은 갱신에서 함께 쓸 칸(점 경로도 된다). 기본은 없다. Agent 는 잠든
        문서를 잠들 수 없게 하는 overrides 의 깨우기를 더한다 (P44 6절)."""
        return ()

    async def _dry_run_overrides(self, inst: I, rendered: dict[str, Any]) -> None:
        """저장된 overrides 가 새 렌더링에 얹히는지 본다. 안 되면 409 overrides_invalid.

        새 렌더링에 overrides 가 맞지 않을 수 있다 (예: 지운 바인딩의 env 참조). 세 리소스의
        재배포가 클러스터를 건드리기 전에 여기서 막고, 어느 경로가 걸린 것인지 `paths` 로
        돌려준다. 저장된 overrides 가 지금 잠긴 경로를 건드리면 dry-run 전에 422 locked_field 로
        멈추고 그 경로를 path 로 돌려준다(`AdvancedEditor.stored_final`)."""
        final = self.editor.stored_final(inst, rendered)
        try:
            await self.editor.dry_run(inst, final)
        except self.error_cls as e:
            raise self._error(
                409,
                "overrides_invalid",
                f"stored overrides do not apply to the new rendering: {e.detail}",
                paths=[format_path(p) for p in key_paths(inst.overrides)],
            ) from e

    # -- 훅: 클러스터를 지우기 전에 살아 있는 문서가 있는지 본다 -------------------------

    async def has_instances_on_cluster(self, cluster_id: str) -> bool:
        return bool(await self.instances.live_on_cluster(cluster_id))

    # -- 상태 가드 ---------------------------------------------------------------------

    def _require(
        self,
        inst: I,
        allowed: Collection[str],
        *,
        action: str,
        outside: str = "already_deployed",
    ) -> None:
        """상태 표 밖의 동작은 409 다. 지우는 중이면 deleting(Knowledge Base 와 같은 코드, P74
        2.3절), 배포·해제 중이면 in_progress, draft 면 not_deployed, 그 밖의 배포 쪽 상태는
        `outside`(배포에는 already_deployed, 게시·채팅처럼 deployed 만 되는 동작에는
        not_deployed)."""
        if inst.state in allowed:
            return
        what = f"{self.profile_kind} {inst.name}"
        if inst.state == "deleting":
            raise self._error(409, "deleting", f"{what} is deleting; cannot {action}")
        if inst.state in IN_PROGRESS:
            raise self._error(409, "in_progress", f"{what} is {inst.state}; cannot {action}")
        if inst.state == "draft":
            raise self._error(409, "not_deployed", f"{what} is not deployed; cannot {action}")
        raise self._error(409, outside, f"{what} is {inst.state}; cannot {action}")

    async def _resolve_target(
        self, tenant: str, cluster_id: str | None, namespace: str | None
    ) -> tuple[Cluster, str]:
        """배포할 클러스터와 네임스페이스 (P71 2.2절). 테넌트가 쓸 수 있는 클러스터와 허용 목록 안의
        네임스페이스이고, 테넌트 Secret 을 쓸 수 없다고 확인된 클러스터는 아무것도 쓰기 전에
        거절한다 (P63 2절). 테넌시의 오류는 코드·상태 그대로 올라간다."""
        cluster = await self.tenancy.resolve_cluster(tenant, cluster_id)
        ns_name = await self.tenancy.check_namespace(tenant, cluster, namespace)
        require_tenant_secrets(cluster, self.error_cls)
        return cluster, ns_name

    def _name_stores(self) -> list[tuple[Any, str]]:
        """같은 네임스페이스에서 K8s 이름이 겹치면 안 되는 문서의 (스토어, 그 문서의 이름). 기본은
        자기 종류다. Agent 는 Deployment 이름을 함께 쓰는 Gateway 를 더한다."""
        return [(self.instances, self.profile_kind)]

    async def _require_name_free(self, inst: I, cluster_id: str, namespace: str, name: str) -> None:
        """그 클러스터·네임스페이스에서 다른 살아 있는 문서가 이 K8s 이름을 쓰면 409
        k8s_name_conflict 다. 세 종류가 같은 코드와 칸으로 답한다 (P74)."""
        for store, noun in self._name_stores():
            clash = await store.live_by_k8s_name(cluster_id, namespace, name)
            if clash is not None and clash.id != inst.id:
                raise self._name_conflict(self.k8s_kind, namespace, name, f"{noun} {clash.name}")

    def _name_conflict(
        self, kind: str, namespace: str, name: str, holder: str, **extra: Any
    ) -> ServiceError:
        """409 k8s_name_conflict. holder 는 그 이름을 이미 쓰는 쪽을 사람이 읽는 말로 적은 것이다.
        세 종류의 CR 이름과 Gateway 의 Entry 이름이 같은 코드와 칸으로 답한다. extra 는 응답에 더할
        칸이다(Entry 이름이면 막는 Gateway 의 gateway_ids)."""
        return self._error(
            409,
            "k8s_name_conflict",
            f"{kind} {namespace}/{name} is already used by {holder}",
            k8s_name=name,
            namespace=namespace,
            **extra,
        )

    def _stamp_deploying(
        self, inst: I, *, cluster: Cluster, k8s: Any, effective: Any, exposure: Any, revision: int
    ) -> None:
        """배포가 문서에 얹는 배포 쪽 스냅샷과 `deploying` 상태. 저장은 호출자의 전이가 한다."""
        inst.cluster_id = cluster.id
        inst.k8s = k8s
        inst.effective_config = effective.model_dump(mode="json")
        inst.exposure = exposure
        inst.deployed_revision = revision
        self._start_deployment(inst)
        inst.last_error = None

    async def _require_namespace_still_allowed(self, tenant: str, cluster: Any, ns: str) -> None:
        """재배포는 문서 자신의 네임스페이스를 그대로 쓴다(D22). 그 네임스페이스가 허용 목록에서
        빠졌으면 409 namespace_no_longer_allowed 다. 배포 본문이 고른 네임스페이스가 목록 밖인 422
        namespace_not_allowed 와 다른 조건이다 (P74 2.3절)."""
        try:
            await self.tenancy.check_namespace(tenant, cluster, ns)
        except ServiceError as e:
            raise self._error(409, "namespace_no_longer_allowed", e.detail) from e

    def _k8s(self, inst: I) -> Any:
        """배포된 K8s 이름 묶음(InstanceK8s·GatewayK8s·AgentK8s). 배포 쪽이 비어 있으면 409
        not_deployed 다."""
        if inst.k8s is None or inst.cluster_id is None:
            raise self._not_deployed(inst)
        return inst.k8s

    def _pinned(self, inst: I) -> Any:
        """배포가 핀한 리비전. 배포 쪽이 비어 있으면 409 not_deployed, 핀이 가리키는 리비전이
        없으면 409 unknown_revision 이다(저장된 핀이 사라진 상태, P74 2.3절)."""
        n = inst.deployed_revision
        if n is None:
            raise self._not_deployed(inst)
        rev = inst.get_revision(n)
        if rev is None:
            raise self._error(
                409,
                "unknown_revision",
                f"{self.profile_kind} {inst.name} has no revision {n}",
            )
        return rev

    def _not_deployed(self, inst: I) -> ServiceError:
        return self._error(
            409,
            "not_deployed",
            f"{self.profile_kind} {inst.name} is not deployed",
        )

    async def _fresh(self, inst: I) -> I:
        """클러스터를 만지기 전에 문서를 다시 읽는다. 라우터가 읽은 뒤 해제·삭제가 끝났으면 조건부
        저장이 막아 주지만 CR 은 이미 한 번 적용된 뒤다. 없으면 409 state_conflict."""
        fresh = await self.instances.get(inst.id)
        if fresh is None:
            raise self._error(409, "state_conflict", f"{self.profile_kind} {inst.name} is gone")
        return fresh

    # -- Advanced 편집: 다시 읽고 상태를 본 뒤 editor 로 넘긴다 --------------------------

    async def render_docs(self, doc: I) -> list[dict[str, Any]]:
        """배포가 핀한 재료로 다시 렌더링한 문서 묶음 (overrides 전). 마지막이 overrides 를 얹는 CR
        이고 그 앞은 먼저 적용할 것이다. 묶음을 내는 종류(MCP Server, Gateway)가 채운다."""
        raise NotImplementedError

    async def render(self, doc: I) -> dict[str, Any]:
        """overrides 를 얹는 CR 만. 묶음(`render_docs`)의 마지막이다. Agent 는 덮는다."""
        return (await self.render_docs(doc))[-1]

    async def set_overrides(
        self,
        inst: I,
        *,
        patch: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        inst = await self._fresh(inst)
        self._require(inst, self.edit_states, action="edit overrides", outside=self.edit_outside)
        async with self._claim_scope(inst, self.edit_states, action="edit overrides"):
            return await self.editor.set_overrides(inst, patch=patch, manifest=manifest)

    async def drift_reset(self, inst: I) -> dict[str, Any]:
        inst = await self._fresh(inst)
        self._require(inst, self.edit_states, action="reset drift", outside=self.edit_outside)
        async with self._claim_scope(inst, self.edit_states, action="reset drift"):
            return await self.editor.drift_reset(inst)

    async def drift_absorb(self, inst: I) -> dict[str, Any]:
        inst = await self._fresh(inst)
        self._require(inst, self.edit_states, action="absorb drift", outside=self.edit_outside)
        async with self._claim_scope(inst, self.edit_states, action="absorb drift"):
            return await self.editor.drift_absorb(inst)
