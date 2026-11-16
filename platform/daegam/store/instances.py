"""MCP Server·Gateway·Agent 스토어가 함께 쓰는 질의 (P71 2.3절, D42).

세 스토어가 모델 클래스만 바꿔 줄마다 같게 갖던 메서드와 상수, 네 인덱스를 여기 한 벌로 둔다.
종류마다 다른 질의와 인덱스, 저장 단위(SHARED·DEFINITION·DEPLOYMENT_FIELDS)는 각 스토어 파일에
남는다. 인덱스 이름과 컬렉션 이름은 그대로라 옮길 것이 없다.

**문서 전체를 쓰는 저장은 없다.** 저장은 자기 쪽 필드만 `$set` 하고(`set_fields`), 정의 쪽은 읽은
`revision`, 배포 쪽은 `state` 로 조건을 건다. 검사와 쓰기 사이에 다른 요청이 먼저 바꿨으면 0건이라
False 다. 정의 쪽 PUT 과 배포 쪽 저장이 서로를 지우지 않게 하려는 것이다.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Any, ClassVar, Literal

from pymongo import ASCENDING
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.auth_reapply import stale_mark_expr
from daegam.store.base import Document, fields_of, guarded_filter, set_fields_where

# draft 를 뺀 다섯. 클러스터에 무엇인가 있거나 있으려 하는 상태다
DEPLOY_STATES: tuple[str, ...] = ("deploying", "deployed", "failed", "undeploying", "deleting")
# 배포 쪽 문서를 고르는 조건
NOT_DRAFT: dict[str, Any] = {"state": {"$ne": "draft"}}
# 배포 하나가 끝난 결과 (P45 6절). provision 의 끝 전이가 deployed, 배포 경로의 실패 기록이
# failed 다. deployed 는 객체를 적용했다는 뜻이지 파드가 준비됐다는 뜻이 아니다
DeployEnd = Literal["deployed", "failed"]
# 배포 쪽 칸만 읽는 질의가 빼는 큰 칸. 리비전 이력과 저장된 overrides 다. 이 칸이 빠진 문서는
# `current` 를 읽지 못한다
DEPLOYED_ONLY: dict[str, int] = {"revisions": 0, "overrides": 0}


def _expected(
    *,
    revision: int | None = None,
    deployment: datetime | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """조건부 저장이 함께 맞춰 볼 칸. 읽은 리비전, 배포(`deploying_since`), overrides 다."""
    also: dict[str, Any] = {}
    if revision is not None:
        also["revision"] = revision
    if deployment is not None:
        also["deploying_since"] = deployment
    if overrides is not None:
        also["overrides"] = overrides
    return also


class MongoInstanceStore[D: Document]:
    """인스턴스 스토어 셋의 공통 질의. 하위가 `model`·`collection`·`transition_fields` 를 채운다."""

    model: ClassVar[type[Any]]
    collection: ClassVar[str]
    # 상태 전이가 쓰는 배포 쪽 필드. Agent 는 init 의 기록(runtime)을 뺀다
    transition_fields: ClassVar[frozenset[str]]
    # 읽기가 기본으로 빼는 문서의 조건. Agent 는 미리 보기다(P44b 5절). 나머지 둘은 없다
    hidden: ClassVar[dict[str, Any]] = {}
    # 이 보기가 그 문서까지 읽는가. Agent 의 `with_previews()` 가 켠 사본을 준다
    shows_hidden: bool = False

    def __init__(self, db: AsyncDatabase):
        self.coll = db[self.collection]

    async def init_schema(self) -> None:
        """세 종류가 같은 인덱스 넷. 하위가 자기 인덱스를 더한다."""
        await self.coll.create_index(
            [("tenant", ASCENDING), ("name", ASCENDING)], unique=True, name="uq_tenant_name"
        )
        await self.coll.create_index([("cluster_id", ASCENDING)], name="ix_cluster")
        # OAuth 의 resource 풀기가 인증 없이 host 로 찾는다 (P33 6절)
        await self.coll.create_index([("exposure.host", ASCENDING)], name="ix_exposure_host")
        await self.coll.create_index(
            [("state", ASCENDING), ("updated_at", ASCENDING)], name="ix_state_updated"
        )

    def _scoped(self, query: dict[str, Any]) -> dict[str, Any]:
        """읽기 질의에 이 보기의 조건(`hidden`)을 더한다. MCP Server·Gateway 는 그대로이고 Agent 는
        미리 보기를 뺀다(P44b 5절). 리컨실(`stale`)과 이름 검사(`live_by_k8s_name`)는 모든 문서를
        봐야 하므로 이것을 거치지 않는다."""
        if self.shows_hidden or not self.hidden:
            return query
        return {**query, **self.hidden}

    def _bound_to(self, field: str, ds_id: str, tenant: str | None) -> dict[str, Any]:
        """`live_binding_data_service` 의 질의. 배포 쪽 문서 가운데 그 칸이 이 Database 를 가리키는
        것이고, tenant 를 주면 그 테넌트만이다."""
        query: dict[str, Any] = {field: ds_id, **NOT_DRAFT}
        if tenant is not None:
            query["tenant"] = tenant
        return self._scoped(query)

    def _one(self, doc: dict[str, Any] | None) -> D | None:
        return self.model.from_doc(doc) if doc else None

    async def _many(
        self, query: dict[str, Any], sort: Any = None, projection: dict[str, int] | None = None
    ) -> list[D]:
        cursor = self.coll.find(query, projection)
        if sort is not None:
            cursor = cursor.sort(sort)
        return [self.model.from_doc(d) for d in await cursor.to_list()]

    async def create(self, inst: D) -> D:
        await self.coll.insert_one(inst.to_doc())
        return inst

    async def get(self, doc_id: str, tenant: str | None = None) -> D | None:
        q: dict[str, Any] = {"_id": doc_id}
        if tenant is not None:
            q["tenant"] = tenant
        return self._one(await self.coll.find_one(self._scoped(q)))

    async def get_by_name(self, tenant: str, name: str) -> D | None:
        return self._one(await self.coll.find_one(self._scoped({"tenant": tenant, "name": name})))

    async def list(self, tenant: str, *, include_draft: bool = False) -> list[D]:
        """기본은 draft 를 뺀다. 옛 인스턴스 스토어의 `list` 자리에 그대로 들어가야 해서다."""
        q: dict[str, Any] = {"tenant": tenant}
        if not include_draft:
            q.update(NOT_DRAFT)
        return await self._many(self._scoped(q), [("created_at", ASCENDING), ("_id", ASCENDING)])

    async def set_fields(
        self,
        doc_id: str,
        fields: dict[str, Any],
        *,
        expect_state: Collection[str] | None = None,
        expect_revision: int | None = None,
        expect_deployment: datetime | None = None,
        expect_overrides: dict[str, Any] | None = None,
        push: Mapping[str, Any] | None = None,
    ) -> bool:
        """fields 만 `$set` 한다. `expect_state` 가 있으면 저장된 `state` 가 그 안에 있을 때만,
        `expect_revision` 은 저장된 `revision` 이, `expect_deployment` 는 읽은 배포
        (`deploying_since`)가, `expect_overrides` 는 읽은 overrides 가 그대로일 때만 쓴다. 조건에 안
        맞으면 0건이라 False 다. 호출자는 자기 쪽 필드만 넘긴다. 정의 쪽은 `expect_revision`, 배포
        쪽은 `expect_state` 와 짝이다. push 는 같은 갱신의 `$push` 다."""
        also = _expected(
            revision=expect_revision, deployment=expect_deployment, overrides=expect_overrides
        )
        return await set_fields_where(
            self.coll, doc_id, fields, expect_state=expect_state, also=also, push=push
        )

    async def mark_stale(
        self,
        doc_id: str,
        fields: dict[str, Any] | None = None,
        *,
        expect_state: Collection[str],
        expect_deployment: datetime | None = None,
        expect_overrides: dict[str, Any] | None = None,
    ) -> bool:
        """렌더 번호를 다시 렌더 표시로 바꾸고 fields(저장 형식)를 함께 쓴다 (P53 3.5절). 표시는
        저장된 번호에서 같은 조건부 갱신 안에서 만든다(`stale_mark_expr`). 그래서 읽은 뒤 다른
        레플리카가 렌더했어도 그 렌더의 번호를 담는다. 조건은 `set_fields` 와 같고, 맞지 않으면
        False 다."""
        also = _expected(deployment=expect_deployment, overrides=expect_overrides)
        query = guarded_filter(doc_id, expect_state=expect_state, also=also)
        values = {name: {"$literal": value} for name, value in (fields or {}).items()}
        update = [{"$set": {**values, "render_epoch": stale_mark_expr()}}]
        res = await self.coll.update_one(query, update)
        return res.matched_count == 1

    async def transition(
        self,
        inst: D,
        *,
        expect: Collection[str],
        extra: Collection[str] = (),
        push: Mapping[str, Any] | None = None,
    ) -> bool:
        """상태를 바꾸는 저장. 저장된 `state` 가 expect 안에 있을 때만 **배포 쪽 필드만** 쓴다.
        검사와 쓰기 사이에 다른 요청이 상태를 먼저 바꿨으면 0건이라 False 다. 같은 문서에 배포 둘,
        삭제와 배포, 해제와 재배포가 겹칠 때 하나만 지나가게 하는 자리다. 정의 쪽(`spec`·`revision`·
        `revisions`)은 절대 쓰지 않으므로 그 사이 들어온 PUT 의 리비전을 지우지 못한다. extra 는
        같은 저장에서 함께 쓸 저장 단위 밖의 칸이다(Agent 의 배포가 비우는 쉬는 상태, P44). push 는
        같은 갱신의 `$push` 다(Agent 의 배포 기록 한 줄, P45 6절)."""
        return await self.set_fields(
            inst.id,
            fields_of(inst, {*self.transition_fields, *extra}),
            expect_state=expect,
            push=push,
        )

    async def delete(self, doc_id: str) -> bool:
        res = await self.coll.delete_one({"_id": doc_id})
        return res.deleted_count == 1

    async def delete_draft(self, doc_id: str) -> bool:
        """draft 일 때만 지운다. 그 사이 배포가 시작됐으면 False 다."""
        res = await self.coll.delete_one({"_id": doc_id, "state": "draft"})
        return res.deleted_count == 1

    async def get_many(self, ids: Collection[str]) -> dict[str, D]:
        """id → 문서. 허브 항목의 도구 이름을 원본에서 계산할 때 쓴다 (D49).
        항목마다 `get` 을 부르면 N+1 이라 한 번에 읽는다. 없는 id 는 결과에 없다."""
        if not ids:
            return {}
        cursor = self.coll.find(self._scoped({"_id": {"$in": list(ids)}}))
        return {d["_id"]: self.model.from_doc(d) for d in await cursor.to_list()}

    async def existing_ids(self, ids: Collection[str]) -> set[str]:
        """ids 가운데 이 보기에 있는 문서의 id. 문서를 읽지 않고 있는지만 본다."""
        if not ids:
            return set()
        cursor = self.coll.find(self._scoped({"_id": {"$in": list(ids)}}), {"_id": 1})
        return {d["_id"] async for d in cursor}

    async def live(self) -> list[D]:
        """모든 테넌트의 배포 쪽 문서 (서비스 토큰 스윕·overrides 감사·노출 host 검사용)."""
        return await self._many(self._scoped(dict(NOT_DRAFT)))

    async def live_on_host(self, host: str) -> list[D]:
        """노출 host 가 host 인 모든 테넌트의 배포 쪽 문서. 노출이 꺼진 것도 host 가 남아 있으면
        온다."""
        return await self._many(self._scoped({"exposure.host": host, **NOT_DRAFT}))

    async def live_on_cluster(self, cluster_id: str) -> list[D]:
        return await self._many(self._scoped({"cluster_id": cluster_id, **NOT_DRAFT}))

    async def live_by_k8s_name(self, cluster_id: str, namespace: str, name: str) -> D | None:
        """그 클러스터·네임스페이스에서 그 이름을 쓰는 배포 쪽 문서. 이름이 겹치면 안 되므로 보기와
        상관없이 모든 문서를 본다."""
        return self._one(
            await self.coll.find_one(
                {
                    "cluster_id": cluster_id,
                    "k8s.namespace": namespace,
                    "k8s.name": name,
                    **NOT_DRAFT,
                }
            )
        )

    async def stale(self, states: list[str], older_than: datetime) -> list[D]:
        """reconcile 대상: state 가 states 에 있고 updated_at 이 older_than 이전인 문서. 멈춘 문서는
        누구든 몰아가야 하므로 보기와 상관없이 모든 문서를 본다."""
        return await self._many(
            {"state": {"$in": states}, "updated_at": {"$lt": older_than}},
            [("updated_at", ASCENDING)],
        )
