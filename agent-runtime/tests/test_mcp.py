"""MCP 소스: 토큰 갱신, allow 필터, 지문 변화, 툴별 타임아웃, 규약 판, 입력 요청 거절, 도구 서버가
늦게 뜰 때의 기동(T18)."""

from __future__ import annotations

import asyncio
import time

import httpx2
import pytest
from fastmcp import Context, FastMCP
from mcp.types import ElicitRequest, ElicitRequestFormParams, InputRequiredResult

from runtime import state as state_module
from runtime.config import ConfigError, parse_config
from runtime.mcp import BearerAuth, ClientCredentialsAuth, ToolSources, build_auth, build_client
from runtime.state import Settings
from tests.app_fixtures import decide, with_subagent
from tests.conftest import chat, event_names, run_app, serve, wait_for

# fastmcp 의 기본 mode="auto" 가 server/discover 로 먼저 맺는 판이다. fake MCP 도 같은 FastMCP 라
# 이 판으로 붙는다
MODERN_PROTOCOL = "2026-07-28"
ASK_SCHEMA = {"type": "object", "properties": {"value": {"type": "string"}}}


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def test_client_credentials_refreshes_before_expiry() -> None:
    clock = FakeClock()
    issued: list[dict] = []
    seen_auth: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/token":
            form = dict(httpx2.QueryParams(request.content.decode()))
            issued.append(form)
            return httpx2.Response(
                200, json={"access_token": f"tok{len(issued)}", "expires_in": 100}
            )
        seen_auth.append(request.headers.get("authorization", ""))
        if request.url.path == "/reject":
            return httpx2.Response(401 if len(seen_auth) == 1 else 200)
        return httpx2.Response(200, json={"ok": True})

    transport = httpx2.MockTransport(handler)
    token_http = httpx2.AsyncClient(transport=transport, base_url="http://idp")
    auth = ClientCredentialsAuth(
        token_url="http://idp/token",
        client_id="daegam-agent",
        client_secret="s",
        scope="mcp",
        http=token_http,
        now=clock,
    )
    async with httpx2.AsyncClient(transport=transport, auth=auth, base_url="http://mcp") as client:
        await client.get("/tools")
        assert issued == [
            {
                "grant_type": "client_credentials",
                "client_id": "daegam-agent",
                "client_secret": "s",
                "scope": "mcp",
            }
        ]
        assert seen_auth[-1] == "Bearer tok1"

        # 만료 60초 전까지는 같은 토큰이다
        clock.t += 30
        await client.get("/tools")
        assert len(issued) == 1 and seen_auth[-1] == "Bearer tok1"

        # 만료 60초 안으로 들어오면 새로 받는다
        clock.t += 15
        await client.get("/tools")
        assert len(issued) == 2 and seen_auth[-1] == "Bearer tok2"

        # 401 이면 한 번 새로 받아 다시 보낸다
        seen_auth.clear()
        response = await client.get("/reject")
        assert response.status_code == 200
        assert len(issued) == 3 and seen_auth == ["Bearer tok2", "Bearer tok3"]
    await token_http.aclose()


async def test_scope_omitted_when_empty() -> None:
    issued: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        issued.append(dict(httpx2.QueryParams(request.content.decode())))
        return httpx2.Response(200, json={"access_token": "t"})

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    auth = ClientCredentialsAuth(
        token_url="http://idp/token", client_id="c", client_secret="s", http=http
    )
    assert await auth.token() == "t"
    assert "scope" not in issued[0]
    await http.aclose()


def test_build_auth_requires_secret_env(monkeypatch) -> None:
    auth = {
        "kind": "oauth_client_credentials",
        "client_id": "c",
        "token_url": "http://idp/token",
        "scope": "",
        "secret_env": "DAEGAM_AGENT_CLIENT_SECRET",
    }
    monkeypatch.delenv("DAEGAM_AGENT_CLIENT_SECRET", raising=False)
    with pytest.raises(ConfigError) as info:
        build_auth(auth)
    assert info.value.code == "tool_secret_missing"
    monkeypatch.setenv("DAEGAM_AGENT_CLIENT_SECRET", "s")
    assert isinstance(build_auth(auth), ClientCredentialsAuth)
    assert build_auth({"kind": "none"}) is None


def test_bearer_auth_attaches_the_env_token_without_refresh(monkeypatch) -> None:
    """bearer(D41)는 파드 env 의 고정 토큰을 붙이고 갱신하지 않는다. 비면 tool_secret_missing."""
    monkeypatch.delenv("DAEGAM_MCP_TOKEN", raising=False)
    with pytest.raises(ConfigError) as info:
        build_auth({"kind": "bearer", "secret_env": "DAEGAM_MCP_TOKEN"})
    assert info.value.code == "tool_secret_missing"
    monkeypatch.setenv("DAEGAM_MCP_TOKEN", "svc.jwt")
    auth = build_auth({"kind": "bearer", "secret_env": "DAEGAM_MCP_TOKEN"})
    assert isinstance(auth, BearerAuth)
    request = httpx2.Request("POST", "http://mcp/mcp")
    flow = auth.auth_flow(request)
    sent = next(flow)
    assert sent.headers["Authorization"] == "Bearer svc.jwt"
    # 401 이 와도 다시 보내지 않는다 (env 는 파드 수명 동안 같다. 갱신은 파드를 굴린다)
    with pytest.raises(StopIteration):
        flow.send(httpx2.Response(401, request=sent))


async def test_tool_sources_load_filters_allow_and_detects_change(support_doc) -> None:
    support_doc["tools"][0]["allow"] = ["Add", "Nope"]
    sources = ToolSources(parse_config(support_doc))
    assert sources.refs == ["tools-calculator"]
    assert await sources.load() is True
    assert [t.name for t in sources.tools_by_ref["tools-calculator"]] == ["Add"]
    assert await sources.load() is False
    assert sources.loaded_at is not None and sources.loaded_at <= time.time()
    assert await sources.refresh() is False and sources.last_error is None


async def test_tool_sources_close_closes_the_token_clients(support_doc, monkeypatch) -> None:
    """client credentials 소스가 만든 토큰 조회 클라이언트는 앱이 닫힐 때 닫힌다."""
    monkeypatch.setenv("DAEGAM_AGENT_CLIENT_SECRET", "s")
    support_doc["tools"][0]["auth"] = {
        "kind": "oauth_client_credentials",
        "client_id": "c",
        "token_url": "http://idp/token",
        "scope": "",
        "secret_env": "DAEGAM_AGENT_CLIENT_SECRET",
    }
    sources = ToolSources(parse_config(support_doc))
    (auth,) = sources._auths
    http = auth._client()
    await sources.close()
    assert http.is_closed


async def test_tool_sources_refresh_keeps_old_tools_on_failure(support_doc) -> None:
    sources = ToolSources(parse_config(support_doc))
    await sources.load()
    _point_at(sources, REF, UNREACHABLE)
    assert await sources.refresh() is False
    assert sources.last_error is not None and sources.last_error.startswith(f"tools[{REF}]: ")
    # 그래프가 읽는 것은 ref 별 목록이다. 갱신이 실패해도 전에 읽은 도구와 규약 판이 그대로 남는다
    assert [t.name for t in sources.tools_by_ref["tools-calculator"]] == ["Add"]
    assert sources.protocols == {"tools-calculator": MODERN_PROTOCOL}


def _asking_server() -> FastMCP:
    """첫 호출은 InputRequiredResult 로 사용자 입력을 묻고(2026-07-28 의 guard 꼴), 답을 받은 호출은
    그 action 을 돌려주는 서버다."""
    server = FastMCP("asker")

    @server.tool(name="Ask")
    def ask(ctx: Context) -> str | InputRequiredResult:
        answers = ctx.input_responses
        if answers is None:
            params = ElicitRequestFormParams(message="이름은?", requested_schema=ASK_SCHEMA)
            return InputRequiredResult(input_requests={"name": ElicitRequest(params=params)})
        return f"action={answers['name'].action}"

    return server


async def test_input_required_is_declined_not_interrupted(support_doc) -> None:
    """서버가 사용자 입력을 물으면 거절이 도구에 닿는다. 처리기 없는 클라이언트는 어댑터가 복제해
    LangGraph interrupt 로 답하게 하는데, 그래프 밖인 여기서는 interrupt 가 곧 예외라 거절이 도구
    결과로 닿는 것이 그 길이 닫혔다는 뜻이다."""
    async with serve(_asking_server().http_app()) as base_url:
        support_doc["tools"][0]["url"] = f"{base_url}/mcp"
        support_doc["tools"][0]["allow"] = ["Ask"]
        sources = ToolSources(parse_config(support_doc))
        # 판은 목록을 읽는 연결이 열려 있는 동안 읽는다. 읽기 전에는 None 이다
        assert sources.protocols == {"tools-calculator": None}
        await sources.load()
        assert sources.protocols == {"tools-calculator": MODERN_PROTOCOL}
        (ask,) = sources.tools_by_ref["tools-calculator"]
        message = await ask.ainvoke({"type": "tool_call", "id": "c1", "name": "Ask", "args": {}})
    assert message.status == "success"
    assert "action=decline" in str(message.content)


async def test_tool_timeout_reaches_the_model_as_an_error(support_doc) -> None:
    """제한 시간을 넘긴 툴은 예외로 턴을 죽이지 않고 status="error" 로 모델에게 간다.

    옛 인터셉터가 isError=True 인 CallToolResult 로 만들던 것과 모델이 보는 모양이 같다.
    langchain.mcp 에는 인터셉터가 없어 도구를 감싸 만든다(T6)."""
    # 스키마가 timeouts 를 1초 이상으로 막으므로 기본값 쪽을 쓴다.
    # 0 이면 첫 await 에서 바로 걸려 흔들리지 않는다
    sources = ToolSources(parse_config(support_doc), default_timeout=0.0)
    await sources.load()
    add = sources.tools_by_ref["tools-calculator"][0]

    message = await add.ainvoke(
        {"type": "tool_call", "id": "c1", "name": "Add", "args": {"a": 1, "b": 2}}
    )
    assert message.status == "error"
    assert "Add" in str(message.content) and "답하지 않았다" in str(message.content)


async def test_tool_under_its_timeout_succeeds(support_doc) -> None:
    support_doc["tools"][0]["timeouts"] = {"Add": 30}
    sources = ToolSources(parse_config(support_doc))
    assert sources._timeouts == {"Add": 30.0}
    await sources.load()
    add = sources.tools_by_ref["tools-calculator"][0]

    message = await add.ainvoke(
        {"type": "tool_call", "id": "c1", "name": "Add", "args": {"a": 3, "b": 4}}
    )
    assert message.status == "success"
    assert "7" in str(message.content)


@pytest.mark.asyncio
async def test_client_credentials_reuses_one_http_client(monkeypatch) -> None:
    """토큰을 다시 받아도 클라이언트를 새로 만들지 않고, 스스로 만든 것은 닫는다.

    조회마다 새로 만들면 갱신과 401 재시도마다 TLS 핸드셰이크를 다시 하고 연결을 재사용하지
    못한다. 기동 때 소스가 여럿이면 그 비용이 한꺼번에 든다.
    """
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(200, json={"access_token": f"t{calls['n']}", "expires_in": 3600})

    made: list[httpx2.AsyncClient] = []
    real_client = httpx2.AsyncClient

    def spy(**kwargs) -> httpx2.AsyncClient:
        made.append(real_client(**kwargs, transport=httpx2.MockTransport(handler)))
        return made[-1]

    # 주입한 것이 없으면 스스로 만든다. 몇 개를 만드는지가 이 테스트의 대상이다
    monkeypatch.setattr(httpx2, "AsyncClient", spy)
    auth = ClientCredentialsAuth(
        token_url="https://idp.example/token",
        client_id="c",
        client_secret="s",
        scope="mcp",
    )
    assert await auth.token() == "t1"
    assert await auth.token(force=True) == "t2"
    assert len(made) == 1, "토큰을 다시 받을 때 클라이언트를 새로 만들었다"
    assert auth.fetches == 2

    await auth.aclose()
    assert made[0].is_closed, "직접 만든 클라이언트는 닫아야 한다"


# -- 도구 서버가 늦게 뜰 때의 기동 (T18) ------------------------------------------------------

REF = "tools-calculator"
UNREACHABLE = "http://127.0.0.1:9/mcp"
ADD_3_4 = '더해 줘 {"tool": "Add", "args": {"a": 3, "b": 4}}'


def _point_at(sources: ToolSources, ref: str, url: str) -> None:
    """소스 하나를 다른 주소로 갈아 끼운다. 소스마다 클라이언트가 하나라 정의와 함께 바꾼다."""
    source = {**sources._sources[ref], "url": url}
    sources._sources[ref] = source
    sources._clients[ref] = build_client(source, None)


async def test_ready_is_true_after_a_first_read_or_without_sources(
    support_doc, minimal_doc
) -> None:
    """준비는 모든 소스의 tools/list 를 한 번 읽은 뒤다. 도구 소스가 없는 문서는 읽을 것이 없어 늘
    준비돼 있다."""
    assert ToolSources(parse_config(minimal_doc)).ready is True
    sources = ToolSources(parse_config(support_doc))
    assert sources.ready is False
    await sources.load()
    assert sources.ready is True


async def test_a_server_that_never_answers_is_cut_by_the_list_timeout(support_doc) -> None:
    """연결은 받고 답하지 않는 서버 앞에서 읽기가 클라이언트 기본(300초)까지 묶이지 않고
    `list_timeout` 에 끊긴다. 기동 중이면 살아 있음 검사보다 먼저 끝나야 하기 때문이다."""

    async def silent(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(30)
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    support_doc["tools"][0]["url"] = f"http://127.0.0.1:{port}/mcp"
    sources = ToolSources(parse_config(support_doc), list_timeout=0.3)
    try:
        async with asyncio.timeout(5):
            assert await sources.refresh() is False
    finally:
        server.close()
    assert sources.last_error == f"tools[{REF}]: TimeoutError"
    assert sources.ready is False


async def test_runtime_starts_without_its_tool_server_and_reads_it_later(
    support_doc, fake_mcp
) -> None:
    """도구 서버가 아직 없어도 런타임은 뜬다. 살아 있음 검사는 200 이고 준비 검사와 턴 입구
    (`/chat`·`/decisions`)는 503 tools_unavailable 이다. 승인 설정과 서브에이전트가 아직 없는 도구를
    가리켜도 기동 컴파일은 된다. 서버가 뜨면 배경 루프가 다시 읽어 그래프를 다시 컴파일하고
    준비된다."""
    support_doc["tools"][0]["url"] = UNREACHABLE
    support_doc["tools"][0]["approval"] = {"Add": ["approve", "reject"]}
    with_subagent(support_doc)
    async with run_app(support_doc, settings=Settings(retry_initial=0.05)) as running:
        client = running.client
        assert (await client.get("/healthz")).status_code == 200
        ready = await client.get("/healthz", params={"probe": "ready"})
        assert ready.status_code == 503 and ready.json()["code"] == "tools_unavailable"
        assert "127.0.0.1" not in ready.text  # kubelet 이 보는 본문에 원문을 싣지 않는다
        health = (await client.get("/healthz")).json()
        assert health["tools"] == [] and health["tools_ready"] is False
        assert health["tools_error"].startswith(f"tools[{REF}]: ")
        assert health["unready"] == {"code": "tools_unavailable", "message": health["tools_error"]}
        refused, _sse = await chat(running, ADD_3_4)
        assert refused.status_code == 503 and refused.json()["code"] == "tools_unavailable"
        assert refused.headers["Retry-After"] == "5"
        decisions = await decide(running, "s-1", [{"type": "approve"}])
        assert decisions.status_code == 503 and decisions.json()["code"] == "tools_unavailable"
        start = running.observed("runtime_start")[0]
        assert any(w.startswith("tools_unavailable: ") for w in start["warnings"])

        _point_at(running.runtime.sources, REF, fake_mcp.url)

        async def has_add() -> bool:
            return (await client.get("/healthz")).json()["tools"] == ["Add"]

        await wait_for(has_add)
        assert (await client.get("/healthz", params={"probe": "ready"})).status_code == 200
        assert running.observed("tools_refreshed")
        response, sse = await chat(running, ADD_3_4)
        assert response.status_code == 200
        # 승인 설정이 이제 실제 도구에 닿아 호출 전에 멈춘다
        assert event_names(sse)[-2:] == ["tool_call", "interrupt"]
        assert "tool_result" not in event_names(sse)


async def test_a_list_that_does_not_compile_keeps_the_pod_unready(
    support_doc, fake_mcp, monkeypatch
) -> None:
    """읽은 목록으로 그래프를 컴파일하지 못하면 도구 없는 그래프인 채로 준비되지 않는다. 오류는
    healthz 상세에 보이고, 다음 읽기에서 다시 컴파일해 되면 그때 준비된다."""
    real_compile = state_module.compile_agent
    broken = {"on": True}

    def compile_agent(*args, **kwargs):
        if broken["on"]:
            raise ConfigError("tool_name_conflict", "도구 이름 Add 가 겹친다")
        return real_compile(*args, **kwargs)

    monkeypatch.setattr(state_module, "compile_agent", compile_agent)
    support_doc["tools"][0]["url"] = UNREACHABLE
    async with run_app(support_doc, settings=Settings(retry_initial=0.05)) as running:
        client = running.client
        _point_at(running.runtime.sources, REF, fake_mcp.url)

        async def compile_failed() -> bool:
            return "tool_name_conflict" in str((await client.get("/healthz")).json()["tools_error"])

        await wait_for(compile_failed)
        assert running.runtime.sources.ready is True
        assert (await client.get("/healthz", params={"probe": "ready"})).status_code == 503
        assert (await chat(running, ADD_3_4))[0].status_code == 503

        broken["on"] = False

        async def ready() -> bool:
            return (await client.get("/healthz", params={"probe": "ready"})).status_code == 200

        await wait_for(ready)
        health = (await client.get("/healthz")).json()
        assert health["tools"] == ["Add"] and health["tools_error"] is None
