"""공용 픽스처와 앱을 띄우는 도우미다. 여럿이 함께 쓰는 그 밖의 도우미는 주제별 모듈에 있고, 테스트
모듈은 서로 import 하지 않는다.

- fake_mcp: fastmcp 의 FastMCP 로 Add 툴 하나를 in-process streamable-http 로 띄운다
  (uvicorn, 임의 포트). 서버는 pytest 세션에 하나이고 테스트마다 호출 수(calls)만 0 으로 되돌린다.
- support_doc / plain_doc / files_doc / minimal_doc: schema/fixtures 를 읽어 테스트용으로 손본
  dict 다. offline_doc 은 MCP 서버 없이 컴파일하는 테스트가 쓰는 표본 문서다(기본은 support).
- run_app: create_app 을 lifespan 과 함께 띄우고 ASGI 전송의 httpx 클라이언트를 준다.
  over_http 면 serve 로 uvicorn 소켓에 띄운다. ASGI 전송은 응답을 다 모아 돌려주므로 끊김처럼
  실제 연결이 필요한 테스트가 쓴다. RunningApp.observed 는 stdout 관측 이벤트를 이름으로 고른다.
- chat / parse_sse / with_sse / events_named / event_names: /chat 한 턴과 그 SSE 를 (event, data)
  목록으로 푼 것, 응답과 그 SSE 의 짝(재개·다시 생성도 쓴다), 이름으로 고른 data, 이벤트 이름의
  차례다.
- serve: ASGI 앱 하나를 uvicorn 임의 포트에 띄우고 주소를 준다.
- wait_for: 배경 루프(도구 다시 읽기, 저장소 다시 열기)가 바꿀 상태를 기다린다.
- PG_DSN / REDIS_URL / needs_pg / needs_redis / store_address: 실제 저장소의 주소와, 주소가 없으면
  건너뛰는 표시·도우미다. `make verify-runtime` 이 게이트 전용 컨테이너를 띄우고 그 주소를
  `DAEGAM_TEST_PG_DSN`·`DAEGAM_TEST_REDIS_URL` 로 넘긴다(Makefile `RUNTIME_TEST_ENV`). 게이트는
  `DAEGAM_TEST_REQUIRE_STORES=1` 을 함께 넘기고, 그때 주소가 비어 있으면 pytest 가 시작부터
  멈춘다(pytest_configure).
- termination_log: 모든 테스트에서 기동 실패의 이유를 임시 파일에 쓰게 한다. 실제
  `/dev/termination-log` 를 건드리지 않는다.

주제별 모듈은 이렇다.

- app_fixtures.py: 세션 id, 승인 결정과 재개, 다시 생성, 화면 전사와 체크포인트 읽기, 끊긴 턴과
  진행 중인 턴, 문서(장기 기억·서브에이전트·가드레일 규칙), 계산기 도구와 도구 호출 메시지, 스텁
  모델(JsonModel·FailOnceModel·FileToolModel·ToolThenSleepModel), 장기 기억을 켠 앱, 인스턴스 토큰,
  CORS 출처, 경로 검사가 거절하는 키, 인용 표본이다.
- store_fixtures.py: 실제 저장소 열기(real_store·redis_store·pg_schema·schema_store·real_memory),
  파드 안 저장소(pod_store), 계약 테스트의 저장소(contract_store), 저장소 셋의 계약이다.
- attachment_fixtures.py(첨부), idp_fixtures.py(외부 OIDC), skill_fixtures.py(Skill 고정물과 가짜
  fetch), redis_fakes.py(Redis 가짜), pg_fakes.py(Postgres 풀 가짜), web_fixtures.py(가짜 웹 Tool
  서버, 웹 문서와 차례대로 부르는 모델), repair_fixtures.py(끊긴 턴과 그 되살리기의 파드, P79)다.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from anyio.from_thread import start_blocking_portal
from fastmcp import (
    FastMCP,  # mcp 2.x 에서 mcp.server.fastmcp 가 없어졌다. mcp-runtime 과 같은 것을 쓴다 (T6)
)
from langchain_core.language_models import BaseChatModel
from sse_starlette.sse import AppStatus

from runtime import boot, error_codes
from runtime.config import AgentConfig, parse_config
from runtime.knowledge import KnowledgeTools
from runtime.long_term import LongTermMemory
from runtime.main import create_app
from runtime.sessions import SessionStore
from runtime.state import Settings

# 도우미를 빌려 주는 모듈이다. 테스트 모듈이 아니므로 그 안의 단정이 실패할 때 값을 보이도록
# assert rewrite 에 따로 넣는다. redis_fakes 와 store_fixtures 는 픽스처가 있어 pytest_plugins 로
# 싣고, 플러그인은 pytest 가 스스로 assert rewrite 에 넣는다
pytest.register_assert_rewrite(
    "tests.app_fixtures",
    "tests.attachment_fixtures",
    "tests.idp_fixtures",
    "tests.pg_fakes",
    "tests.repair_fixtures",
    "tests.sandbox_fixtures",
    "tests.skill_fixtures",
)
pytest_plugins = ("tests.redis_fakes", "tests.store_fixtures", "tests.web_fixtures")

FIXTURES = Path(__file__).resolve().parent.parent / "schema" / "fixtures"
# 실제 저장소별로 주소를 받는 env 다
STORE_ENVS = {"postgres": "DAEGAM_TEST_PG_DSN", "redis": "DAEGAM_TEST_REDIS_URL"}
STORE_ADDRESSES = {kind: os.environ.get(env) for kind, env in STORE_ENVS.items()}
PG_DSN = STORE_ADDRESSES["postgres"]
REDIS_URL = STORE_ADDRESSES["redis"]


def _unset(kind: str) -> str:
    """주소가 없어 건너뛰는 사유다."""
    return f"{STORE_ENVS[kind]} 이 없다"


needs_pg = pytest.mark.skipif(not PG_DSN, reason=_unset("postgres"))
needs_redis = pytest.mark.skipif(not REDIS_URL, reason=_unset("redis"))


def store_address(kind: str) -> str:
    """실제 저장소(postgres·redis)의 주소다. 없으면 부른 테스트를 건너뛴다."""
    address = STORE_ADDRESSES[kind]
    if not address:
        pytest.skip(_unset(kind))
    return address


def pytest_configure(config: pytest.Config) -> None:
    """게이트(make verify-runtime, CI)는 `DAEGAM_TEST_REQUIRE_STORES=1` 을 주소와 함께 넘긴다.
    그때 주소가 하나라도 비면 pytest 가 실제 저장소 테스트를 모두 조용히 건너뛰고 게이트는 통과해
    버린다. 그래서 건너뛰는 대신 시작부터 멈춘다. 오류 코드표의 검사(error_codes.STRICT)도 여기서
    켠다."""
    unset = [env for kind, env in STORE_ENVS.items() if not STORE_ADDRESSES[kind]]
    # 모든 응답을 오류 코드표와 맞춰 본다 (P74 2.2절)
    error_codes.STRICT = True
    if os.environ.get("DAEGAM_TEST_REQUIRE_STORES") == "1" and unset:
        raise pytest.UsageError(f"DAEGAM_TEST_REQUIRE_STORES=1 인데 {'·'.join(unset)} 이 비어 있다")


@pytest.fixture(autouse=True)
def termination_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """기동 실패의 이유가 쓰이는 임시 파일이다. 쓰였는지 보려는 테스트가 받아 쓴다."""
    path = tmp_path / "termination-log"
    monkeypatch.setattr(boot, "TERMINATION_LOG", str(path))
    return path


@pytest.fixture(autouse=True)
def sse_not_exiting() -> None:
    """sse-starlette 의 프로세스 전역 종료 표시를 테스트마다 내린다.

    그 표시는 uvicorn 서버 하나가 내려갈 때 서고 다시 내려가지 않는다. 선 뒤에는 같은 프로세스의
    모든 SSE 응답이 곧바로 끝나므로, 서버를 내리는 테스트 뒤에 도는 MCP 세션(initialize 의 답이
    SSE 다)이 `SSE stream ended without a response` 로 실패한다. 파드에서는 프로세스가 함께
    내려가므로 문제가 아니다."""
    AppStatus.should_exit = False


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@dataclass
class FakeMcp:
    url: str
    calls: dict[str, int]


@pytest.fixture(scope="session")
def fake_mcp_server() -> Iterator[FakeMcp]:
    """Add 툴 하나를 가진 MCP 서버를 pytest 세션에 한 번 띄운다. 호출 수를 calls 에 센다.

    테스트마다 띄우고 내리면 한 번 내릴 때 0.2초 가까이 기다린다(uvicorn 이 should_exit 를 0.1초마다
    확인하고, 내리면서 0.1초를 더 쉰다). 테스트마다 바뀌는 이벤트 루프보다 오래 살아야 하므로 따로
    도는 스레드의 루프에서 serve 로 띄운다. 테스트는 calls 를 되돌리는 fake_mcp 를 받는다."""
    calls = {"Add": 0}
    server = FastMCP("calc")

    @server.tool(name="Add")
    def add(a: int, b: int) -> int:
        """두 수를 더한다."""
        calls["Add"] += 1
        return a + b

    with (
        start_blocking_portal(name="fake-mcp") as portal,
        portal.wrap_async_context_manager(serve(server.http_app())) as base_url,
    ):
        yield FakeMcp(url=f"{base_url}/mcp", calls=calls)


@pytest.fixture
def fake_mcp(fake_mcp_server: FakeMcp) -> FakeMcp:
    """pytest 세션에 하나인 MCP 서버다. 앞 테스트의 호출이 섞이지 않게 calls 를 0 으로 되돌린다."""
    calls = fake_mcp_server.calls
    calls.update(dict.fromkeys(calls, 0))
    return fake_mcp_server


@asynccontextmanager
async def serve(app: Any) -> AsyncIterator[str]:
    """ASGI 앱을 uvicorn 으로 임의 포트에 띄우고 주소를 준다. lifespan 도 uvicorn 이 돌린다."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    uv = uvicorn.Server(config)
    task = asyncio.create_task(uv.serve())
    try:
        while not uv.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        port = uv.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        uv.should_exit = True
        await task


def offline_doc(name: str = "support-agent.json") -> dict[str, Any]:
    """표본 문서 name 에서 Postgres 를 뺀 문서다. 도구 소스는 표본의 클러스터 주소 그대로라
    MCP 서버 없이 도구를 `compile_agent(tools_by_ref=)` 로 넘기는 테스트가 쓴다. `local_doc` 은
    support 표본의 이것을 로컬 MCP 서버에 붙인 것이다."""
    doc = load_fixture(name)
    doc["memory"] = None
    return doc


def local_doc(url: str) -> dict[str, Any]:
    """support-agent.json 을 url 의 로컬 MCP 서버·무인증·Postgres 없음으로 바꾼다. 승인 툴과
    인사말은 없다. 인사말은 새 세션의 첫 이벤트라 두면 첫 이벤트를 보는 테스트마다 자리가 밀린다.
    보는 테스트가 따로 켠다."""
    doc = offline_doc()
    doc["entry"]["greeting"] = None
    source = doc["tools"][0]
    source.update(url=url, auth={"kind": "none"}, allow=["Add"], approval={}, timeouts={})
    return doc


@pytest.fixture
def support_doc(fake_mcp: FakeMcp) -> dict[str, Any]:
    """`local_doc` 을 가짜 MCP 서버에 붙인 것이다."""
    return local_doc(fake_mcp.url)


@pytest.fixture
def plain_doc(support_doc: dict[str, Any]) -> dict[str, Any]:
    """support_doc 에서 도구를 뺀 문서다. 스텁이 받은 글을 그대로 답한다(첨부 테스트)."""
    support_doc["main"]["tools"] = []
    return support_doc


@pytest.fixture
def files_doc(support_doc: dict[str, Any]) -> dict[str, Any]:
    """파일 도구가 보이는 문서다(첨부와 세션 파일 테스트). harness profile 등록은 모델 키마다
    합집합이라, 같은 프로세스의 다른 테스트가 stub:stub 에서 파일 도구를 숨겼어도 타지 않게 모델
    이름을 따로 쓴다."""
    support_doc["main"]["model"]["name"] = "stub-uploads"
    return support_doc


@pytest.fixture
def minimal_doc() -> dict[str, Any]:
    return load_fixture("minimal.json")


@dataclass
class RunningApp:
    client: httpx.AsyncClient
    app: Any
    config: AgentConfig
    log: io.StringIO

    @property
    def runtime(self) -> Any:
        return self.app.state.runtime

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log.getvalue().splitlines() if line.strip()]

    def observed(self, *names: str) -> list[dict[str, Any]]:
        """stdout 관측 이벤트 가운데 이름(`daegam_event`)이 names 의 하나인 것을 차례대로 고른다."""
        return [event for event in self.events() if event["daegam_event"] in names]

    def ended(self, status: str) -> int:
        """그 status 로 끝난 턴(`turn_end`)의 수다. 턴의 결과는 파드가 세지 않고 플랫폼이 이
        이벤트로 센다(P37 11절)."""
        return sum(1 for event in self.observed("turn_end") if event["status"] == status)

    def rejections(self) -> list[str]:
        """입구에서 거절된 요청의 코드다. 플랫폼이 거절로 세는 이벤트와 같은 규칙이다(플랫폼
        services/telemetry/normalize.py 의 `rejected_code`). 그 규칙의 셋째 사본이다. 둘째는 플랫폼
        tests/test_telemetry_parity.py 가 런타임 어휘와 맞대는 normalize 의 상수다."""
        codes = []
        for event in self.events():
            name = event["daegam_event"]
            if name in ("rejected", "rate_limited"):
                codes.append(event.get("code", name))
            elif name == "budget" and event["action"] == "rejected":
                codes.append(f"{event['scope']}_budget_exceeded")
            elif name == "guardrail" and (event["position"], event["action"]) == ("input", "block"):
                codes.append("guardrail_blocked")
        return codes


@asynccontextmanager
async def run_app(
    doc: dict[str, Any],
    *,
    settings: Settings | None = None,
    model: BaseChatModel | None = None,
    store: SessionStore | None = None,
    knowledge: KnowledgeTools | None = None,
    long_term: LongTermMemory | None = None,
    oidc_http_client: httpx.AsyncClient | None = None,
    over_http: bool = False,
) -> AsyncIterator[RunningApp]:
    settings = settings or Settings()
    # 문서를 settings 의 agent.json 에서 읽은 것으로 둔다. init 이 받은 Skill 이 그 곁에 있다
    path = Path(settings.config_path) if settings.config_path else None
    config = parse_config(doc, path=path)
    out = io.StringIO()
    app = create_app(
        config,
        settings,
        model=model,
        store=store,
        knowledge=knowledge,
        long_term=long_term,
        event_out=out,
        oidc_http_client=oidc_http_client,
    )
    async with _client_for(app, over_http=over_http) as client:
        yield RunningApp(client=client, app=app, config=config, log=out)


@asynccontextmanager
async def _client_for(app: Any, *, over_http: bool) -> AsyncIterator[httpx.AsyncClient]:
    """앱에 붙는 클라이언트다. 기본은 ASGI 전송이고 over_http 면 uvicorn 소켓이다."""
    if over_http:
        async with serve(app) as base_url, httpx.AsyncClient(base_url=base_url) as client:
            yield client
        return
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    """SSE 본문을 (event, data) 목록으로 푼다."""
    out: list[tuple[str, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        event = None
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: ") :])
        if event is not None:
            out.append((event, json.loads("\n".join(data_lines)) if data_lines else {}))
    return out


def with_sse(
    response: httpx.Response,
) -> tuple[httpx.Response, list[tuple[str, dict[str, Any]]]]:
    """응답과 그 본문을 parse_sse 로 푼 것이다. 200 이 아니면 SSE 는 빈 목록이다."""
    return response, parse_sse(response.text) if response.status_code == 200 else []


def events_named(sse: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    """parse_sse 결과에서 이름이 name 인 이벤트의 data 만 차례대로 고른다."""
    return [data for event, data in sse if event == name]


def event_names(sse: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """parse_sse 결과의 이벤트 이름을 차례대로 준다."""
    return [event for event, _ in sse]


async def chat(
    running: RunningApp,
    message: str,
    *,
    session_id: str | None = None,
    user_sub: str = "alice",
    ephemeral: bool = False,
    attachments: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
    approval_choices: list[dict[str, str]] | None = None,
    run_id: str | None = None,
    untrusted: bool = False,
    task: bool = False,
) -> tuple[httpx.Response, list[tuple[str, dict[str, Any]]]]:
    body: dict[str, Any] = {"message": message, "user_sub": user_sub}
    if session_id:
        body["session_id"] = session_id
    if ephemeral:
        body["ephemeral"] = True
    if attachments is not None:
        body["attachments"] = attachments
    if approval_choices is not None:
        body["approval_choices"] = approval_choices
    if run_id is not None:
        body["run_id"] = run_id
    if untrusted:
        body["untrusted"] = True
    if task:
        body["task"] = True
    return with_sse(await running.client.post("/chat", json=body, headers=headers or {}))


async def wait_for(
    check: Callable[[], Awaitable[bool]], *, what: str = "기다린 상태", tries: int = 100
) -> None:
    """check 가 참이 될 때까지 0.05초 간격으로 기다린다. 끝내 오지 않으면 무엇을 기다렸는지 말하며
    실패한다."""
    for _ in range(tries):
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"오지 않았다: {what}")
