"""P17 F1·F2b·F5: 장기 기억의 검증·컴파일과 지우는 모양 (D34).

검증 셋(`long_term_needs_memory`·`long_term_filesystem_disabled`·보관 기간 1 이상)과 memory 의
Redis db 0(`redis_db_not_zero`, F5), 사다리 `"3"` 의 컴파일, 그리고 플랫폼이 저장소에 직접 붙어
만들고 지우는 SQL·Redis 명령을 든다. 배포한 Agent 에서는 provision 이 파드 전에 스키마를 만드는 것,
삭제와 사용자별 삭제가 기억을 지우는 것, 해제·경합·사라진 리비전이 기억을 남기는 것(D42)을 fake
클러스터 위의 API 로 든다. 실제 DB 는 쓰지 않는다. psycopg 는 `tests/helpers/fake_pg.py` 의 목이고
redis 는 이 파일의 목이다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import psycopg
import pytest

from daegam.services import agent_graph, agent_memory
from daegam.store.agents import AgentSpec
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.agents import (
    CTX_TENANT,
    FAQ_ID,
    agent_env,
    codes,
    compile_spec,
    create_agent,
    ctx_with,
    deploy_agent,
    issues_of,
    kb,
    make_ctx,
    node_of,
    template_for,
    template_spec,
    with_knowledge,
)
from tests.helpers.consts import AG
from tests.helpers.fake_pg import FakeConn

INSTANCE = "ag-support-0001"
SUB = "alice@example.com"


def with_long_term(
    spec: dict[str, Any] | None = None,
    *,
    retention_days: int | None = 30,
    filesystem: bool = True,
) -> dict[str, Any]:
    """템플릿의 memory 노드에 장기 기억을 켜고 메인의 파일 도구를 켠 spec."""
    spec = spec or template_spec()
    node_of(spec, "memory")["data"]["long_term"] = {"retention_days": retention_days}
    node_of(spec, "agent-main")["data"]["builtin_tools"] = {"filesystem": filesystem}
    return spec


# -- 검증 ------------------------------------------------------------------------------------


def test_long_term_on_a_valid_graph_has_no_issue():
    assert codes(issues_of(with_long_term())) == set()
    # 보관 기간을 안 두는 것(만료 없음)도 켠 것이다
    assert codes(issues_of(with_long_term(retention_days=None))) == set()


def test_long_term_needs_the_memory_node_attached_to_main():
    """저장소는 그 memory 노드가 고른 Database 다. 안 붙으면 켜 봐야 남길 곳이 없다."""
    spec = with_long_term()
    spec["graph"]["edges"] = [e for e in spec["graph"]["edges"] if e["id"] != "e-memory"]
    issues = issues_of(spec)
    assert "long_term_needs_memory" in codes(issues)
    issue = next(i for i in issues if i["code"] == "long_term_needs_memory")
    assert issue["node_id"] == "memory"
    assert "not attached to the main agent" in issue["message"]


def test_long_term_needs_the_file_tools():
    issues = issues_of(with_long_term(filesystem=False))
    assert "long_term_filesystem_disabled" in codes(issues)
    issue = next(i for i in issues if i["code"] == "long_term_filesystem_disabled")
    assert issue["node_id"] == "memory" and "builtin_tools.filesystem" in issue["message"]


def test_retention_days_is_at_least_one():
    """0 이나 음수는 노드 형식이 막는다(`invalid_node_data`). 키가 없으면 꺼진 것이다."""
    for bad in (0, -1):
        issues = issues_of(with_long_term(retention_days=bad))
        assert "invalid_node_data" in codes(issues)
    plain = template_spec()
    assert agent_graph.MemoryData.model_validate(node_of(plain, "memory")["data"]).long_term is None


def test_memory_redis_must_be_db_zero():
    """RediSearch 가 db != 0 에서 인덱스를 못 만든다. 장기 기억과 무관하게 체크포인터도 그렇다."""
    spec = template_spec(ds_id="ds-redis")
    ctx = make_ctx(redis_checkpointer="ready")
    assert "redis_db_not_zero" not in codes(issues_of(spec, ctx))
    ctx.data_services["ds-redis"].db = 3
    issues = issues_of(spec, ctx)
    assert "redis_db_not_zero" in codes(issues)
    issue = next(i for i in issues if i["code"] == "redis_db_not_zero")
    assert issue["node_id"] == "memory" and "db 0" in issue["message"]


# -- 컴파일 ----------------------------------------------------------------------------------


def test_compile_with_long_term_is_the_version_3_document():
    """사다리 `"3"` 은 knowledge 키 셋(없으면 빈 목록)과 memory.long_term 을 함께 가진다."""
    doc = compile_spec(with_long_term())
    assert doc["schema_version"] == "3"
    assert doc["memory"]["long_term"] == {"retention_days": 30}
    # knowledge 노드가 하나도 없어도 키 셋이 있어야 스키마의 allOf 사다리를 지난다
    assert doc["knowledge"] == [] and doc["main"]["knowledge"] == []
    assert all(s["knowledge"] == [] for s in doc["subagents"])
    assert doc["main"]["builtin_tools"] == {"filesystem": True}
    # compile 이 스키마로 자기 결과를 검증하므로 여기까지 왔으면 계약을 지났다
    assert agent_graph.check_schema(doc) == []
    # 보관 기간을 안 두면(만료 없음) null 그대로 싣는다
    unlimited = compile_spec(with_long_term(retention_days=None))
    assert unlimited["memory"]["long_term"] == {"retention_days": None}


def test_compile_with_long_term_and_knowledge_stays_version_3():
    spec = with_long_term(with_knowledge())
    doc = compile_spec(spec, ctx_with(kb(FAQ_ID)))
    assert doc["schema_version"] == "3"
    assert len(doc["knowledge"]) == 1 and doc["memory"]["long_term"]["retention_days"] == 30


def test_long_term_memory_accessor():
    """provision 과 삭제가 문맥 없이 "켜졌는가, 어느 Database 인가" 만 읽는 자리다."""
    spec = AgentSpec.model_validate(with_long_term())
    found = agent_graph.long_term_memory(spec)
    assert found is not None and found[0] == "ds-pg" and found[1].retention_days == 30
    assert agent_graph.long_term_memory(AgentSpec.model_validate(template_spec())) is None
    # 메인에 안 붙은 노드는 저장소가 아니므로 없는 것으로 본다
    loose = with_long_term()
    loose["graph"]["edges"] = [e for e in loose["graph"]["edges"] if e["id"] != "e-memory"]
    assert agent_graph.long_term_memory(AgentSpec.model_validate(loose)) is None


# -- 네임스페이스 ------------------------------------------------------------------------------


def test_namespace_prefix_and_labels():
    label = agent_memory.user_label(SUB)
    assert len(label) == 64 and label.isalnum() and "." not in label
    assert agent_memory.namespace_prefix(CTX_TENANT, INSTANCE) == f"{CTX_TENANT}.{INSTANCE}"
    assert (
        agent_memory.namespace_prefix(CTX_TENANT, INSTANCE, SUB)
        == f"{CTX_TENANT}.{INSTANCE}.{label}"
    )
    assert agent_memory.redis_store_prefix(INSTANCE) == f"daegam-agent:{INSTANCE}:store"


# -- Postgres ---------------------------------------------------------------------------------


def test_delete_postgres_escapes_like_metacharacters():
    """테넌트 이름에 밑줄이 오면 그대로 두는 순간 남의 네임스페이스까지 걷는다."""
    conn = FakeConn(rowcount=0)
    asyncio.run(agent_memory.delete_postgres(conn, tenant="a_b%c", instance_id=INSTANCE))
    sql, params = conn.executed[-1]
    assert "prefix = %s OR prefix LIKE %s ESCAPE" in sql
    assert params[1] == f"a\\_b\\%c.{INSTANCE}.%"


def test_delete_postgres_is_a_noop_before_the_runtime_made_the_table():
    conn = FakeConn({"to_regclass": [(False,)]})
    assert (
        asyncio.run(agent_memory.delete_postgres(conn, tenant=CTX_TENANT, instance_id=INSTANCE))
        == 0
    )
    assert len(conn.executed) == 1  # to_regclass 만 물었다


def test_delete_postgres_raises_the_driver_error():
    conn = FakeConn(fail=psycopg.errors.InsufficientPrivilege("nope"))
    with pytest.raises(psycopg.Error):
        asyncio.run(agent_memory.delete_postgres(conn, tenant=CTX_TENANT, instance_id=INSTANCE))


# -- Redis ------------------------------------------------------------------------------------


class FakePipeline:
    def __init__(self, client):
        self.client = client
        self.keys: list[str] = []

    def execute_command(self, _command: str, key: str, _path: str) -> None:
        self.keys.append(key)

    async def execute(self) -> list[Any]:
        return [self.client.docs.get(k) for k in self.keys]


class FakeRedis:
    """`store:<ULID>` 키와 그 JSON 문서의 prefix 필드만 흉내 낸다."""

    def __init__(self, docs: dict[str, str], page: int = 2):
        self.docs = docs
        self.page = page
        self.deleted: list[str] = []
        # 훑는 동안 지우므로 커서가 목록을 따라 움직이면 안 된다. 첫 SCAN 에서 한 번 뜬다
        self._seen: list[str] = []

    async def scan(self, cursor: int = 0, match: str = "*", count: int = 10):
        prefix = match.rstrip("*")
        if cursor == 0:
            self._seen = [k for k in sorted(self.docs) if k.startswith(prefix)]
        chunk = self._seen[cursor : cursor + self.page]
        nxt = cursor + self.page
        return (nxt if nxt < len(self._seen) else 0), chunk

    async def delete(self, *keys: str) -> int:
        self.deleted.extend(keys)
        for k in keys:
            self.docs.pop(k, None)
        return len(keys)

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)


# langgraph 의 RedisStore 는 prefix 필드를 redisvl TokenEscaper 로 적는다(store/redis/base.py
# _namespace_to_text). 점과 하이픈 앞에 역슬래시가 붙는다
_REDISEARCH_PUNCT = re.compile(r"([,.<>{}\[\]\\\"':;!@#$%^&*()\-+=~/ ?])")


def _stored(namespace: str) -> str:
    """RedisJSON 이 `$.prefix` 로 돌려주는 꼴. 실제 저장소처럼 문장 부호를 이스케이프한다."""
    return json.dumps([_REDISEARCH_PUNCT.sub(r"\\\1", namespace)])


def _redis_docs() -> dict[str, str]:
    mine = agent_memory.namespace_prefix(CTX_TENANT, INSTANCE, SUB)
    other = agent_memory.namespace_prefix(CTX_TENANT, INSTANCE, "bob@example.com")
    store = agent_memory.redis_store_prefix(INSTANCE)
    return {
        f"{store}:01": _stored(mine),
        f"{store}:02": _stored(f"{mine}.deeper"),
        f"{store}:03": _stored(other),
        f"{store}:04": _stored(mine),
        # 다른 인스턴스의 키는 접두어가 달라 SCAN 에 걸리지 않는다
        "daegam-agent:other:store:05": _stored(mine),
    }


def test_delete_redis_takes_only_that_users_documents():
    docs = _redis_docs()
    store = agent_memory.redis_store_prefix(INSTANCE)
    # 저장 꼴이 실제처럼 하이픈을 이스케이프해야 아래 고르기가 뜻이 있다
    assert "\\-" in docs[f"{store}:01"]
    client = FakeRedis(docs)
    n = asyncio.run(
        agent_memory.delete_redis(client, tenant=CTX_TENANT, instance_id=INSTANCE, user_sub=SUB)
    )
    assert n == 3
    assert sorted(client.deleted) == [f"{store}:01", f"{store}:02", f"{store}:04"]
    assert f"{store}:03" in client.docs and "daegam-agent:other:store:05" in client.docs


def test_delete_redis_takes_the_whole_instance_without_reading_documents():
    client = FakeRedis(_redis_docs())
    n = asyncio.run(agent_memory.delete_redis(client, tenant=CTX_TENANT, instance_id=INSTANCE))
    assert n == 4 and "daegam-agent:other:store:05" in client.docs


def test_delete_redis_leaves_documents_it_cannot_read():
    """모양이 다른 응답은 남의 것일 수 있다. 지우느니 남긴다."""
    store = agent_memory.redis_store_prefix(INSTANCE)
    client = FakeRedis({f"{store}:01": "not json", f"{store}:02": None})
    assert (
        asyncio.run(
            agent_memory.delete_redis(client, tenant=CTX_TENANT, instance_id=INSTANCE, user_sub=SUB)
        )
        == 0
    )


# -- 배포한 Agent 의 기억: provision·삭제·해제 (D34, D42) -------------------------------------


def _long_term_spec(gw, ds) -> dict[str, Any]:
    """agent_env 가 깐 도구 소스와 Database 로 채운 템플릿에 장기 기억을 켠 spec."""
    return with_long_term(template_for(gw, ds))


def test_deploy_fails_when_the_schema_cannot_be_made(client, fake_clusters, mongo, fake_pg):
    """파드가 이유 없이 재시작하는 것보다 Deploy 가 이유를 말하며 멈추는 쪽이 낫다."""
    cid, ds, gw = agent_env(client, mongo)
    fake_pg.conn.fail = psycopg.errors.InsufficientPrivilege("permission denied")
    p = create_agent(client, _long_term_spec(gw, ds)).json()
    r = deploy_agent(client, p["id"])
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "long_term_schema_failed"
    assert "daegam_store" in r.json()["detail"]["detail"]


def test_delete_keeps_going_when_the_memory_store_is_unreachable(
    client, fake_clusters, mongo, fake_pg, caplog
):
    """기억을 못 지웠다고 문서가 안 지워지면 사용자가 손쓸 데가 없다."""
    inst = deployed_agent(client, fake_clusters, mongo, spec=_long_term_spec, runtime=False).doc
    fake_pg.error = psycopg.OperationalError("connection refused")
    with caplog.at_level(logging.WARNING, logger="daegam.agents"):
        assert client.delete(f"{AG}/{inst['id']}").status_code == 200
    assert any("long-term memory not deleted" in r.message for r in caplog.records)
    assert mongo["agents"].find_one({"_id": inst["id"]}) is None


def test_delete_memories_is_a_noop_when_long_term_is_off(client, fake_clusters, mongo, fake_pg):
    """장기 기억이 꺼져 있으면 provision 도 기억 지우기도 Database 에 붙지 않는다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    r = client.delete(f"{agent.url}/memories/alice@example.com")
    assert r.status_code == 200 and r.json()["deleted"] == 0
    assert fake_pg.calls == []


def test_undeploy_keeps_the_memory_and_a_later_delete_still_wipes_it(
    client, fake_clusters, mongo, fake_pg
):
    """해제는 같은 id 로 다시 배포되므로 장기 기억을 남긴다. 그 뒤의 삭제는 배포 쪽이 이미 비었어도
    기억을 지운다.

    합치기 전에는 해제가 인스턴스 삭제였고 정의(프로필)에는 기억이 없어 이 순서가 아예 없었다.
    한 문서가 되면서 해제한 뒤 draft 로 있다가 삭제하는 길이 생겼고, 삭제가 draft 갈래로 빠지면
    사람의 기억이 문서보다 오래 남는다 (D34, D42).
    """
    inst = deployed_agent(client, fake_clusters, mongo, spec=_long_term_spec, runtime=False).doc
    fake_pg.conn.executed.clear()
    assert client.post(f"{AG}/{inst['id']}/undeploy").status_code == 200
    assert client.get(f"{AG}/{inst['id']}").json()["state"] == "draft"
    assert not [t for t, _ in fake_pg.conn.executed if 'DELETE FROM "daegam_store"' in t]
    assert client.delete(f"{AG}/{inst['id']}").status_code == 200
    deletes = [
        (t, p) for t, p in fake_pg.conn.executed if 'DELETE FROM "daegam_store"."store"' in t
    ]
    assert len(deletes) == 1, "해제 뒤 삭제가 장기 기억을 지우지 않았다"
    assert deletes[0][1] == (f"default.{inst['id']}", f"default.{inst['id']}.%")


def test_memories_can_be_deleted_after_undeploy(client, fake_clusters, mongo, fake_pg):
    """해제한 문서의 기억도 사용자가 지울 수 있다. 해제는 기억을 남기므로 draft 에서 지울 길이
    없으면 "내 기억을 지워 달라" 에 통째 삭제 말고는 답이 없다 (D42)."""
    inst = deployed_agent(client, fake_clusters, mongo, spec=_long_term_spec, runtime=False).doc
    assert client.post(f"{AG}/{inst['id']}/undeploy").status_code == 200
    assert client.get(f"{AG}/{inst['id']}").json()["state"] == "draft"
    fake_pg.conn.executed.clear()
    r = client.delete(f"{AG}/{inst['id']}/memories/alice@example.com")
    assert r.status_code == 200, r.text
    assert r.json() == {"agent_id": inst["id"], "deleted": 1}
    _text, params = fake_pg.conn.executed[-1]
    label = agent_memory.user_label("alice@example.com")
    assert params == (f"default.{inst['id']}.{label}", f"default.{inst['id']}.{label}.%")


def test_draft_delete_keeps_the_memory_when_a_deploy_wins_the_race(
    client, fake_clusters, mongo, fake_pg
):
    """draft 삭제와 배포가 겹치면 삭제가 409 로 진다. 그때 기억을 이미 지웠으면 도는 에이전트의
    사람 기억이 사라진 채 되돌릴 길이 없다. 조건부 삭제가 먼저다 (D42)."""
    inst = deployed_agent(client, fake_clusters, mongo, spec=_long_term_spec, runtime=False).doc
    assert client.post(f"{AG}/{inst['id']}/undeploy").status_code == 200
    svc = client.app.state.agent_service
    store = client.app.state.agents
    stale = client.portal.call(store.get, inst["id"])  # 삭제가 들고 온 draft 사본
    assert deploy_agent(client, inst["id"]).status_code in (200, 201)
    fake_pg.conn.executed.clear()

    async def losing_delete():
        try:
            await svc.delete(stale)
        except Exception as e:  # noqa: BLE001
            return getattr(e, "code", type(e).__name__)
        return None

    assert client.portal.call(losing_delete) == "state_conflict"
    assert not [t for t, _ in fake_pg.conn.executed if 'DELETE FROM "daegam_store"' in t]
    assert client.get(f"{AG}/{inst['id']}").json()["state"] == "deployed"


def test_memories_are_not_touched_when_the_pinned_revision_is_gone(
    client, fake_clusters, mongo, fake_pg
):
    """핀한 리비전이 사라지면 기억이 어느 Database 에 있는지 알 수 없다. 현재 리비전으로 떨어지면
    그 사이 memory 노드가 다른 Database 를 가리키게 됐을 때 남의 기억을 지운다. 409 로 멈춘다
    (D42)."""
    inst = deployed_agent(client, fake_clusters, mongo, spec=_long_term_spec, runtime=False).doc
    # 핀은 그대로 두고 그 리비전만 없앤다. 정상 경로로는 생기지 않는 상태다
    mongo["agents"].update_one({"_id": inst["id"]}, {"$set": {"deployed_revision": 99}})
    fake_pg.conn.executed.clear()
    r = client.delete(f"{AG}/{inst['id']}/memories/alice@example.com")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "unknown_revision"
    assert not [t for t, _ in fake_pg.conn.executed if 'DELETE FROM "daegam_store"' in t]
