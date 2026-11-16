"""P36 F7: 내 장기 기억 라우트(`GET /agents/{id}/memories/me`, 파일 하나 읽기와 삭제).

가짜 런타임에 기억 세 라우트(tests/helpers/runtime.py 의 `add_memories`)를 달아 본다. 주체가 언제나
호출자 sub 이고 승인자 헤더를 붙이지 않는 것, 런타임 오류가 플랫폼 형식으로 오는 것, 키를 조각마다
다시 인코딩하는 것, 지우기만 감사에 남는 것을 든다. 기록에 `memory_files` 가 없을 때의 409 와 그
라우트를 모르는 옛 파드의 409 runtime_rolling 은 test_agent_chat_api.py 의 능력 표가 든다.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from daegam.auth import rbac
from daegam.services.agent_runtime import APPROVER_HEADER, memory_file_path
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.auth import as_principal
from tests.helpers.consts import AG
from tests.helpers.http import detail_code
from tests.helpers.runtime import MEMORY_FEATURES, add_memories


def _memory_calls(runtime: FastAPI) -> list[dict[str, Any]]:
    return [c for c in runtime.state.calls if c["path"].startswith("/memories")]


def _memory_url(agent_id: str, key: str | None = None) -> str:
    url = f"{AG}/{agent_id}/memories/me"
    return url if key is None else f"{url}/files/{key}"


def test_memories_are_always_the_callers_own(client, fake_clusters, mongo):
    """주체는 호출자 sub 이고 agent:approve 가 있어도 승인자 헤더를 붙이지 않는다. viewer 도 자기
    기억을 다룬다. 응답은 런타임이 더한 칸까지 그대로다. 하나 지우기는 쓰기라 감사가 경로 URL 로
    무엇을 지웠는지 남기고, 자기 기억 읽기는 세션 상세처럼 남기지 않는다."""
    agent = deployed_agent(client, fake_clusters, mongo, features=MEMORY_FEATURES)
    add_memories(agent.runtime)
    mongo["audit_events"].delete_many({})

    r = client.get(_memory_url(agent.id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["later"] is True and body["files"][0]["later"] is True
    assert (body["retention_days"], body["files"][0]["path"]) == (30, "/memories/header.md")
    r = client.get(_memory_url(agent.id, "notes/today.md"))
    assert r.json() == {
        "path": "/memories/notes/today.md",
        "encoding": "utf-8",
        "content": "# 취향",
    }
    r = client.delete(_memory_url(agent.id, "notes/today.md"))
    assert r.json() == {"path": "/memories/notes/today.md", "deleted": True}
    [event] = list(mongo["audit_events"].find({}))
    assert (event["method"], event["action"]) == ("DELETE", "delete_my_memory")
    assert event["path"].endswith("/memories/me/files/notes/today.md")
    for call in _memory_calls(agent.runtime):
        assert call["params"] == {"user_sub": "header"}
        assert APPROVER_HEADER.lower() not in call["headers"]
    keys = [c["key"] for c in _memory_calls(agent.runtime)]
    assert keys == [None, "notes/today.md", "notes/today.md"]

    with as_principal(client, "viewer-1", rbac.VIEWER):
        [row] = client.get(_memory_url(agent.id)).json()["files"]
        assert row["path"] == "/memories/viewer-1.md"
        assert client.delete(_memory_url(agent.id, "prefs.md")).status_code == 200
        assert _memory_calls(agent.runtime)[-1]["params"] == {"user_sub": "viewer-1"}


def test_memory_refusals_reach_the_screen_in_the_platform_shape(client, fake_clusters, mongo):
    """없는 파일, 1MiB 를 넘는 파일, `..` 조각, 꺼진 장기 기억은 런타임의 코드 그대로다. 키의 `/`
    는 두고 조각마다 다시 인코딩하므로 `..` 조각이 정규화되어 다른 경로가 되지 않고 런타임까지 가서
    422 invalid_path 다. `?`·`#` 도 키에 남는다."""
    assert memory_file_path("notes/today.md") == "/memories/files/notes/today%2Emd"
    assert memory_file_path("a?b#c/..") == "/memories/files/a%3Fb%23c/%2E%2E"
    agent = deployed_agent(client, fake_clusters, mongo, features=MEMORY_FEATURES)
    add_memories(agent.runtime)
    r = client.get(_memory_url(agent.id, "notes/%2E%2E/prefs.md"))
    assert detail_code(r) == (422, "invalid_path")
    assert _memory_calls(agent.runtime)[-1]["key"] == "notes/../prefs.md"
    assert client.get(_memory_url(agent.id, "a%3Fb%23c.md")).status_code == 200
    assert _memory_calls(agent.runtime)[-1]["key"] == "a?b#c.md"
    assert detail_code(client.get(_memory_url(agent.id, "missing.md"))) == (404, "memory_not_found")
    assert detail_code(client.delete(_memory_url(agent.id, "missing.md"))) == (
        404,
        "memory_not_found",
    )
    assert detail_code(client.get(_memory_url(agent.id, "big.md"))) == (413, "file_too_large")
    agent.runtime.state.long_term = False
    assert detail_code(client.get(_memory_url(agent.id))) == (404, "long_term_off")
