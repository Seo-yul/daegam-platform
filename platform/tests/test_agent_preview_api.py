"""미리 보기 라우트 (P44b 6절, D107).

미리 보기는 원본 Agent 아래의 경로로만 쓴다. 원본은 `get_agent_or_404` 로 찾으므로 Agent 의 by-id
라우트는 모두 미리 보기의 id 에 404 다. 미리 보기는 원본과 호출자의 sub 로 찾으므로 시작한 사람만
자기 것을 쓰고, `agent:deploy` 가 요청마다 있어야 한다. 열린 미리 보기 목록과 끝내기는
`agent:deploy` 를 가진 누구나 쓴다. 채팅과 결정과 다시 생성은 원본의 일일 예산을 원장으로 본다.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from daegam.store.base import utcnow
from daegam.store.usage import TokenUsage, UsageProvider, UsageRecord, UsageSource
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.agents import agent_body, node_of, template_for
from tests.helpers.auth import admin_headers, bearer, login
from tests.helpers.consts import AG, DEFAULT_NS, TENANT_HEADER
from tests.helpers.http import detail_code
from tests.helpers.oauth import join, make_user, password_of
from tests.helpers.previews import plain_spec
from tests.helpers.runtime import fake_runtime


def _preview_url(agent_id: str) -> str:
    return f"{AG}/{agent_id}/preview"


def _start(client, agent_id: str, **kw: Any):
    return client.post(_preview_url(agent_id), **kw)


def _resolve(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if ref is not None:
        return _resolve(spec, spec["components"]["schemas"][ref.rsplit("/", 1)[-1]])
    return schema


def _example(spec: dict[str, Any], schema: dict[str, Any]) -> Any:
    """스키마의 필수 칸만 채운 값. 본문 검증(422)을 지나 로더까지 닿게 한다."""
    schema = _resolve(spec, schema)
    if "enum" in schema:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            options = [o for o in schema[key] if o.get("type") != "null"]
            return _example(spec, options[0])
    kind = schema.get("type")
    if kind == "array":
        return [_example(spec, schema.get("items") or {})] * int(schema.get("minItems") or 0)
    if kind == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        return {k: _example(spec, props[k]) for k in schema.get("required") or []}
    return {"string": "x", "integer": 1, "number": 1, "boolean": False}.get(kind)


# 필수 칸이 없어 비어 있으면 로더 앞에서 422 인 본문이다
BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("patch", f"/api{AG}/{{agent_id}}/schedules/{{schedule_id}}"): {"cron": "0 * * * *"},
    ("patch", f"/api{AG}/{{agent_id}}/triggers/{{trigger_id}}"): {"name": "n"},
}


def test_every_agent_by_id_route_answers_404_for_a_preview_id(client, fake_clusters, mongo):
    """미리 보기 경로와 인스턴스 토큰의 기계 경로 밖의 `/agents/{agent_id}` 경로는 모두 미리
    보기의 id 에 404 다. 그 문서를 아는 사람도 Agent 로 다룰 수 없다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False, spec=plain_spec)
    r = _start(client, agent.id)
    assert r.status_code == 202, r.text
    preview_id = r.json()["id"]
    spec = client.get("/openapi.json").json()
    checked = []
    for path, ops in spec["paths"].items():
        if not path.startswith(f"/api{AG}/{{agent_id}}") or "/preview" in path:
            continue
        if path.endswith(("/agent.json", "/skills/{name}")):
            continue
        for method, op in ops.items():
            params = {
                p["name"]: _example(spec, p["schema"])
                for p in op.get("parameters") or []
                if p["in"] == "path"
            }
            params["agent_id"] = preview_id
            url = path.removeprefix("/api")
            for name, value in params.items():
                url = url.replace(f"{{{name}}}", str(value))
            kw: dict[str, Any] = {}
            body = (op.get("requestBody") or {}).get("content") or {}
            if (method, path) in BODIES:
                kw["json"] = BODIES[method, path]
            elif "application/json" in body:
                kw["json"] = _example(spec, body["application/json"]["schema"])
            elif "multipart/form-data" in body:
                kw["files"] = {"file": ("a.txt", b"hi", "text/plain")}
            res = client.request(method.upper(), url, **kw)
            checked.append((method, path))
            assert res.status_code == 404, (method, path, res.status_code, res.text)
    assert len(checked) > 50
    # 미리 보기는 그대로 있다. Agent 목록과 고르개에도 없다
    assert mongo["agents"].find_one({"_id": preview_id}) is not None
    assert [a["id"] for a in client.get(AG).json()] == [agent.id]
    assert [a["id"] for a in client.get(f"{AG}/summary").json()] == [agent.id]


def test_the_owner_starts_reads_chats_and_ends_their_preview(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False, spec=plain_spec)
    r = _start(client, agent.id)
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["source_id"] == agent.id and out["owner"] == "header" and out["revision"] == 1
    assert out["state"] == "deployed" and out["idle_minutes"] == 30 and out["max_hours"] == 2
    assert out["started_at"].endswith("+00:00") and out["ends_at"] > out["idle_ends_at"]
    got = client.get(_preview_url(agent.id))
    assert got.status_code == 200 and got.json()["id"] == out["id"]
    # 미리 보기 파드의 가짜 런타임은 미리 보기의 인스턴스 토큰을 받는다
    secret = agent.fake.get("v1", "Secret", f"agent-inst-{out['id']}-token", DEFAULT_NS)
    runtime = fake_runtime(secret["stringData"]["token"])
    agent.fake.service_app = runtime
    chat = client.post(f"{_preview_url(agent.id)}/chat", json={"message": "안녕"})
    assert chat.status_code == 200, chat.text
    (called,) = [c for c in runtime.state.calls if c["path"] == "/chat"]
    assert called["body"]["user_sub"] == "header"
    listed = client.get(f"{AG}/{agent.id}/previews")
    assert [p["id"] for p in listed.json()["previews"]] == [out["id"]]
    ended = client.delete(_preview_url(agent.id))
    assert ended.status_code == 200 and ended.json() == {"id": out["id"], "ended": True}
    gone = client.get(_preview_url(agent.id))
    assert detail_code(gone) == (404, "preview_not_found")
    assert gone.json()["detail"]["ended"]["reason"] == "ended"
    assert client.get(f"{AG}/{agent.id}/previews").json() == {"previews": []}


@pytest.fixture
def people(local_client, mongo) -> dict[str, dict[str, str]]:
    """local 모드의 관리자와, 테넌트 default 의 개발자 둘과 뷰어 하나의 제어면 헤더다."""
    admin = admin_headers(local_client, mongo)
    out = {"admin": {**admin, TENANT_HEADER: "default"}}
    for name, role in (("dev-a", "developer"), ("dev-b", "developer"), ("viewer", "viewer")):
        user = make_user(local_client, admin, name, mongo)
        join(local_client, admin, "default", (user["id"], role))
        r = login(local_client, name, password_of(name))
        assert r.status_code == 200, r.text
        out[name] = {**bearer(r.json()["access_token"]), TENANT_HEADER: "default"}
    return out


def test_only_the_starter_uses_a_preview_and_every_deployer_sees_and_ends_it(
    local_client, fake_clusters, mongo, people
):
    client = local_client
    client.headers.update(people["admin"])
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False, spec=plain_spec)
    mine = _start(client, agent.id, headers=people["dev-a"])
    assert mine.status_code == 202, mine.text
    # 다른 사람은 그 미리 보기를 찾지 못한다. 자기 것을 시작하면 따로다
    assert detail_code(client.get(_preview_url(agent.id), headers=people["dev-b"])) == (
        404,
        "preview_not_found",
    )
    chat = client.post(
        f"{_preview_url(agent.id)}/chat", json={"message": "x"}, headers=people["dev-b"]
    )
    assert detail_code(chat) == (404, "preview_not_found")
    # agent:deploy 가 없으면 아무것도 못 한다
    for method, url in (
        ("POST", _preview_url(agent.id)),
        ("GET", _preview_url(agent.id)),
        ("GET", f"{AG}/{agent.id}/previews"),
        ("DELETE", f"{AG}/{agent.id}/previews/{mine.json()['id']}"),
    ):
        res = client.request(method, url, headers=people["viewer"])
        assert res.status_code == 403, (method, url, res.text)
    # 다른 개발자는 목록에서 보고 끝낸다. 시작한 사람의 서랍은 다른 사람이 끝냈다고 읽는다
    listed = client.get(f"{AG}/{agent.id}/previews", headers=people["dev-b"]).json()["previews"]
    assert [(p["id"], p["owner"]) for p in listed] == [(mine.json()["id"], mine.json()["owner"])]
    ended = client.delete(f"{AG}/{agent.id}/previews/{mine.json()['id']}", headers=people["dev-b"])
    assert ended.json()["ended"] is True
    gone = client.get(_preview_url(agent.id), headers=people["dev-a"])
    assert gone.json()["detail"]["ended"]["reason"] == "ended_by_other"
    missing = client.delete(f"{AG}/{agent.id}/previews/nope", headers=people["dev-b"])
    assert detail_code(missing) == (404, "preview_not_found")


def test_a_preview_turn_is_refused_once_the_source_spent_its_daily_budget(
    client, fake_clusters, mongo
):
    def with_budget(gw, ds):
        spec = template_for(gw, ds)
        spec["service"]["budget"]["daily_tokens"] = 100
        return spec

    agent = deployed_agent(client, fake_clusters, mongo, runtime=False, spec=with_budget)
    assert _start(client, agent.id).status_code == 202
    record = UsageRecord(
        id="r-1",
        ts=utcnow(),
        writer="runtime",
        route="direct",
        tenant="default",
        source=UsageSource(kind="agent", id=agent.id),
        provider=UsageProvider(kind="openai"),
        model="m",
        usage=TokenUsage(input_tokens=60, output_tokens=40),
    )
    mongo["usage_records"].insert_one(record.to_doc())
    url = _preview_url(agent.id)
    chat = client.post(f"{url}/chat", json={"message": "x"})
    assert detail_code(chat) == (429, "daily_budget_exceeded")
    assert int(chat.headers["Retry-After"]) >= 1
    regen = client.post(f"{url}/sessions/s1/regenerate", json={})
    assert detail_code(regen) == (429, "daily_budget_exceeded")
    body = {"decisions": [{"type": "approve"}]}
    decide = client.post(f"{url}/sessions/s1/decisions", json=body)
    assert detail_code(decide) == (429, "daily_budget_exceeded")


def test_a_start_moves_an_existing_preview_and_the_start_answers_in_progress_states(
    client, fake_clusters, mongo
):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False, spec=plain_spec)
    first = _start(client, agent.id).json()
    changed = copy.deepcopy(agent.spec())
    node_of(changed, "entry")["data"]["greeting"] = "옮긴 인사"
    assert client.put(agent.url, json=agent_body(changed)).status_code == 200
    moved = _start(client, agent.id).json()
    assert moved["id"] == first["id"] and moved["revision"] == 2
    assert moved["ends_at"] == first["ends_at"]


def test_preview_uploads_take_the_attachment_body_limit():
    """미리 보기의 업로드도 채팅 첨부의 본문 상한을 받는다. 인용 원문 읽기는 감사에 남는다."""
    from daegam.api.agent_chat import UPLOAD_BODY_LIMIT
    from daegam.audit.middleware import _audited_read

    pattern, _limit = UPLOAD_BODY_LIMIT
    assert pattern.match("/api/agents/a1/preview/uploads")
    assert pattern.match("/api/agents/a1/uploads")
    assert not pattern.match("/api/agents/a1/preview/uploads/u1")
    assert _audited_read("/api/agents/a1/preview/sessions/s1/citations/1/source")


def test_template_spec_with_memory_runs_as_a_preview_through_the_api(client, fake_clusters, mongo):
    """승인과 분당 한도와 memory 가 있는 원본도 미리 보기로 뜬다. 파드에는 Database 바인딩이
    없다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False, spec=template_for)
    out = _start(client, agent.id)
    assert out.status_code == 202, out.text
    stored = mongo["agents"].find_one({"_id": out.json()["id"]})
    assert stored["bindings"] == [] and stored["k8s"]["namespace"] == DEFAULT_NS
