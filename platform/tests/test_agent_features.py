"""P35 F2a: `features[]` 틀 (D65).

3절 표의 칸(기록 있음·없음, 배포 422 와 경고, init 409 와 last_error, `attached` 의 빼기와 경고),
이름 머리의 상한과 꼴, 모르는 이름을 적지 않는 것, 스냅숏 이미지가 다르면 기록을 쓰지 않는 것,
409 로 끝나도 기록이 먼저 남는 것, 서비스 `compile` 의 최종 문서 검사, 두 목록의 파리티를 든다.
`FETCH_SCRIPT` 는 로컬 HTTP 서버에 대고 실제 파이썬으로 돌린다. 이 판에는 문서 기능이 없어
`required`·`attached` 는 monkeypatch 로 만든다. fake 클러스터를 쓰고 실제 클러스터는 쓰지 않는다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from daegam.k8s.templates.agent import (
    CONFIG_FILE,
    CONFIG_PATH_ENV,
    FETCH_SCRIPT,
    agent_config_base,
    agent_json_url,
    render_agent_deployment,
)
from daegam.services import agent_features
from daegam.services.agent_features import (
    CANCEL,
    CAPABILITIES,
    DOCUMENT_FEATURES,
    FEATURES_HEADER,
    IMAGE_HEADER,
    IN_POD_STATE,
    KNOWN,
    MAX_FEATURES,
    MAX_HEADER_BYTES,
    PROVIDER_EXTRA_BODY,
    PROVIDER_STREAM_USAGE,
    TOOL_SAFETY,
    WEB_GUARD,
    AgentFeatures,
    check_features,
    deploy_check,
    finish_document,
    parse_reported,
)
from daegam.services.agents import AgentError
from daegam.services.instance_status import apply_pod_waiting
from daegam.store.agents import AgentRuntime
from daegam.store.settings import LLMProvider, ProviderRouting
from tests.helpers.agent_runtime import deployed_agent, minimal_agent_doc
from tests.helpers.agents import (
    FAQ_ID,
    agent_env,
    attach,
    compile_spec,
    ctx_with,
    deploy_spec,
    kb,
    knowledge_node,
    make_ctx,
    pinned_document,
    template_for,
    template_spec,
)
from tests.helpers.consts import AG, IMAGE
from tests.helpers.docs import agent_doc, effective_config
from tests.helpers.source import AGENT_RUNTIME, runtime_module

ALWAYS = AgentFeatures(attached=frozenset({TOOL_SAFETY}))
"""어느 에이전트에든 붙는 기능이다(P43 8절)."""

# -- 이름 목록 ----------------------------------------------------------------------------


def test_platform_and_runtime_lists_are_the_same() -> None:
    """두 목록의 파리티다. 런타임이 아는 이름과 플랫폼이 아는 이름이 갈라지면 init 이 알린 이름을
    플랫폼이 버리거나 없는 이름을 요구한다."""
    runtime = runtime_module(AGENT_RUNTIME / "features.py")
    assert tuple(runtime.DOCUMENT_FEATURES) == DOCUMENT_FEATURES
    assert tuple(runtime.CAPABILITIES) == CAPABILITIES
    assert runtime.KNOWN == KNOWN
    assert runtime.NAME_PATTERN == agent_features.NAME_PATTERN
    names = [*DOCUMENT_FEATURES, *CAPABILITIES]
    assert len(names) == len(set(names))
    assert all(agent_features.NAME_RE.match(name) for name in names)


def test_scale_to_zero_is_a_capability_not_a_document_feature() -> None:
    """런타임 능력이라 문서의 features 에 싣지 않는다. 재우기(P44 6절 `runtime_image`)는 이미지가
    알린 기록으로 판정한다."""
    assert agent_features.SCALE_TO_ZERO in CAPABILITIES
    assert agent_features.SCALE_TO_ZERO not in DOCUMENT_FEATURES


# -- 머리 읽기 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "Bad-Name",
        "cancel,,UPPER",
        "a" * 33,
        ",".join(f"f{i}" for i in range(MAX_FEATURES + 1)),
        "x" * (MAX_HEADER_BYTES + 1),
    ],
    ids=["shape", "upper", "too_long_name", "too_many", "too_big"],
)
def test_malformed_header_counts_as_absent_with_a_warning(raw: str) -> None:
    reported = parse_reported({FEATURES_HEADER: raw, IMAGE_HEADER: IMAGE})
    assert reported.names == frozenset()
    assert [w.code for w in reported.warnings] == ["runtime_features_invalid"]


# -- 요구 기능 검사 -----------------------------------------------------------------------


def test_deploy_check_needs_a_record_of_the_same_image() -> None:
    doc = agent_doc(spec=template_spec())
    wanted = AgentFeatures(required=frozenset({"a"}))
    # 기록이 없으면(첫 배포) 판정을 init 에 맡기고 경고만 남긴다
    check, warnings = deploy_check(doc, IMAGE, wanted)
    assert check is None and [w.code for w in warnings] == ["runtime_features_unknown"]
    # 다른 이미지의 기록도 같다(이미지를 바꾼 배포)
    doc.runtime = AgentRuntime(image="other:1", features=["a"])
    assert deploy_check(doc, IMAGE, wanted)[0] is None
    # 같은 이미지의 기록이면 판정한다
    doc.runtime = AgentRuntime(image=IMAGE, features=[])
    check, warnings = deploy_check(doc, IMAGE, wanted)
    assert check is not None and check.missing == ("a",) and warnings == []
    # 붙이는 기능만이면 init 이 이미지의 이름으로 빼므로 경고하지 않는다
    fresh = agent_doc(spec=template_spec())
    assert deploy_check(fresh, IMAGE, AgentFeatures(attached=frozenset({"b"}))) == (None, [])
    # 요구 기능이 없으면 확인할 것이 없어 경고도 없다
    assert deploy_check(agent_doc(spec=template_spec()), IMAGE, AgentFeatures()) == (None, [])


def test_provider_extra_body_is_attached_only_for_a_routed_provider() -> None:
    """업체 선호(T19)가 있는 provider 를 모델 노드가 쓰면 `provider_extra_body` 를 붙인다. 허락되면
    붙이는 함수가 그 provider 자리에 `extra_body` 를 싣고, 허락되지 않으면(옛 이미지) 칸도 이름도
    없어 문서가 예전과 같다. 능력은 허락돼도 문서에 실리지 않고, compile 이 싣지 않는 떠 있는
    모델 노드는 업체 선호가 있어도 이름을 켜지 않는다."""
    ctx = make_ctx(keyed=("router",))
    router = LLMProvider(name="router", kind="openrouter", api_key_secret="k")
    ctx.providers["router"] = router
    spec = template_spec(provider="router")
    doc = agent_doc(spec=spec)
    base = compile_spec(spec, ctx)
    assert agent_features.agent_features(doc, doc.current, ctx.providers) == ALWAYS
    routed = router.model_copy(update={"routing": ProviderRouting(order=["moonshotai"])})
    ctx.providers["router"] = routed
    wanted = agent_features.agent_features(doc, doc.current, ctx.providers)
    assert wanted == AgentFeatures(attached=frozenset({PROVIDER_EXTRA_BODY, TOOL_SAFETY}))
    # 컴파일 결과에는 칸이 없다. 붙이는 차례가 허락된 기능의 칸만 싣는다
    assert compile_spec(spec, ctx) == base
    final = finish_document(base, frozenset({PROVIDER_EXTRA_BODY, CANCEL}), ctx, doc.current)
    assert final["features"] == [PROVIDER_EXTRA_BODY]
    assert final["main"]["model"]["provider"]["extra_body"] == {
        "provider": {"order": ["moonshotai"]}
    }
    assert "extra_body" not in base["main"]["model"]["provider"]  # 받은 문서는 그대로다
    assert finish_document(base, frozenset(), ctx, doc.current) == base
    # 메인은 업체 선호가 없는 provider 를 쓰고, 업체 선호 provider 는 떠 있는 모델 노드에만 있다
    loose = template_spec(provider="local-openai")
    loose["graph"]["nodes"].append(
        {"id": "model-loose", "type": "model", "data": {"name": "loose", "provider": "router"}}
    )
    loose_doc = agent_doc(spec=loose)
    floating = agent_features.agent_features(loose_doc, loose_doc.current, ctx.providers)
    assert floating == ALWAYS


def test_a_provider_without_stream_usage_attaches_stream_usage_false() -> None:
    """스트림 사용량을 끈 openai 계열 provider 를 쓰면 `provider_stream_usage` 를 붙이고, 허락되면
    그 provider 자리에 `stream_usage: false` 를 싣는다. 최종 문서는 스키마를 지난다(P37 18절)."""
    ctx = make_ctx(keyed=("router",))
    ctx.providers["router"] = LLMProvider(
        name="router", kind="openrouter", api_key_secret="k", stream_usage=False
    )
    spec = template_spec(provider="router")
    doc = agent_doc(spec=spec)
    wanted = agent_features.agent_features(doc, doc.current, ctx.providers)
    assert wanted == AgentFeatures(attached=frozenset({PROVIDER_STREAM_USAGE, TOOL_SAFETY}))
    base = compile_spec(spec, ctx)
    final = finish_document(base, frozenset({PROVIDER_STREAM_USAGE}), ctx, doc.current)
    assert final["features"] == [PROVIDER_STREAM_USAGE]
    assert final["main"]["model"]["provider"]["stream_usage"] is False
    assert "stream_usage" not in base["main"]["model"]["provider"]
    assert finish_document(base, frozenset(), ctx, doc.current) == base


def test_tool_safety_marks_are_attached_only_when_allowed() -> None:
    """허락된 `tool_safety` 는 소스마다 `inside`·`read` 를, Knowledge 항목마다 `outside` 를 싣는다.
    이미지가 모르는 기능이라 빼고 돌면 그 칸도 이름도 없어 컴파일한 문서 그대로다."""
    ctx = ctx_with(kb(FAQ_ID))
    spec = template_spec()
    spec["graph"]["nodes"].append(knowledge_node())
    attach(spec, "agent-main", "kb-faq")
    rev, base = pinned_document(spec, ctx)
    final = finish_document(base, frozenset({TOOL_SAFETY}), ctx, rev)
    assert final["features"] == [TOOL_SAFETY]
    assert final["tools"] and all({"inside", "read"} <= s.keys() for s in final["tools"])
    assert final["knowledge"] and all("outside" in k for k in final["knowledge"])
    dropped = finish_document(base, frozenset(), ctx, rev)
    assert dropped == base and "features" not in dropped
    assert not any({"inside", "read"} & s.keys() for s in dropped["tools"])
    assert not any("outside" in k for k in dropped["knowledge"])


# -- init 받기와 기록 -----------------------------------------------------------------------


def _record(mongo, agent_id: str) -> dict[str, Any] | None:
    return mongo["agents"].find_one({"_id": agent_id})["runtime"]


def _want(monkeypatch, **kw: set[str]) -> None:
    wanted = AgentFeatures(**{k: frozenset(v) for k, v in kw.items()})
    monkeypatch.setattr(agent_features, "agent_features", lambda doc, rev, providers, **_: wanted)


def test_service_compile_checks_the_final_document(client, fake_clusters, mongo, monkeypatch):
    """features 를 얹은 최종 문서를 서비스가 검사한다. 이름 꼴이 틀린 기능이 허락되면 500
    compile_failed 다(agent_graph.compile 은 검사하지 않는다). provision 도 설정 지문의 컴파일에서
    같은 검사를 지나므로(P35 13절) 배포한 뒤에 기능을 꾸민다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    monkeypatch.setattr(agent_features, "DOCUMENT_FEATURES", ("Bad-Name",))
    _want(monkeypatch, required={"Bad-Name"})
    svc = client.app.state.agent_service

    async def compile_via() -> dict[str, Any]:
        return await svc.compile(await svc.agents.get(agent.id), token_envs=None)

    with pytest.raises(AgentError) as info:
        client.portal.call(compile_via)
    assert info.value.status == 500 and info.value.code == "compile_failed"
    assert any("features" in m for m in info.value.extra["errors"])


# -- 배포·재배포 전 검사 --------------------------------------------------------------------


def test_record_written_during_provision_survives_the_finish(
    client, fake_clusters, mongo, monkeypatch
):
    """새 파드의 init 이 Deployment 적용 뒤에 적은 기록은 provision 의 마지막 상태 전이가 덮지
    않는다. 상태 전이는 기록을 쓰지 않기 때문이다. 첫 배포와, 문서에 다른 이미지의 기록을 든 채 도는
    재배포가 같다."""
    _cid, ds, gw = agent_env(client, mongo)
    fake = fake_clusters.by_name["edge"]
    apply = fake.apply
    reported: list[str] = []

    def apply_then_report(obj: dict[str, Any], **kw: Any) -> dict[str, Any]:
        applied = apply(obj, **kw)
        agent = mongo["agents"].find_one({"k8s.name": obj["metadata"]["name"]})
        if obj["kind"] == "Deployment" and agent is not None and not kw.get("dry_run"):
            record = AgentRuntime(image=IMAGE, features=[CANCEL]).model_dump()
            snapshot = {"_id": agent["_id"], "effective_config.images.agent_image": IMAGE}
            mongo["agents"].update_one(snapshot, {"$set": {"runtime": record}})
            reported.append(agent["_id"])
        return applied

    monkeypatch.setattr(fake, "apply", apply_then_report)
    _p, inst = deploy_spec(client, template_for(gw, ds), "support")
    assert reported == [inst["id"]]
    assert _record(mongo, inst["id"])["features"] == [CANCEL]
    stale = AgentRuntime(image="daegam-agent-runtime:0.0.9").model_dump()
    mongo["agents"].update_one({"_id": inst["id"]}, {"$set": {"runtime": stale}})
    r = client.post(f"{AG}/{inst['id']}/redeploy")
    assert r.status_code == 200, r.text
    assert reported == [inst["id"], inst["id"]]
    assert _record(mongo, inst["id"])["image"] == IMAGE
    st = client.get(f"{AG}/{inst['id']}").json()["status"]
    assert st["runtime"] == {"image": IMAGE, "features": [CANCEL]}


# -- 파드 상태의 종료 메시지 ------------------------------------------------------------------


def test_status_reason_is_the_init_termination_message() -> None:
    """init 이 다시 뜨기를 기다리는 동안 그 컨테이너의 lastState.terminated.message 가 이유다."""
    pod = {
        "metadata": {},
        "status": {
            "initContainerStatuses": [
                {
                    "name": "fetch-agent-config",
                    "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off"}},
                    "lastState": {
                        "terminated": {"exitCode": 1, "message": "runtime_image_too_old: x"}
                    },
                }
            ]
        },
    }
    out = apply_pod_waiting({"phase": "Pending"}, [pod])
    assert out["phase"] == "Failed"
    assert out["message"] == "CrashLoopBackOff: runtime_image_too_old: x"


# -- FETCH_SCRIPT ------------------------------------------------------------------------


class _FakePlatform(BaseHTTPRequestHandler):
    """agent.json 과 그 아래 경로를 답한다. 받은 머리를 플랫폼의 `parse_reported` 로 읽어 남긴다.
    `redirect_to` 가 있으면 무엇이든 그 주소로 302 를 준다. `raw` 가 있으면 agent.json 대신 그
    바이트를 답하고, `short` 면 Content-Length 보다 적게 보내고 끊는다."""

    document: dict[str, Any] = {}
    status = 200
    redirect_to: str | None = None
    raw: bytes | None = None
    short = False
    seen: list[dict[str, Any]] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server 의 이름이다
        headers = {k.lower(): v for k, v in self.headers.items()}
        reported = parse_reported(self.headers)
        type(self).seen.append({"path": self.path, "headers": headers, "reported": reported})
        if self.redirect_to is not None:
            self._answer(302, b"", location=self.redirect_to)
        elif self.status != 200:
            detail = {"code": "runtime_image_too_old", "detail": "missing: core"}
            self._answer(self.status, json.dumps({"detail": detail}).encode())
        elif self.path.endswith(f"/{CONFIG_FILE}"):
            body = self.raw if self.raw is not None else json.dumps(self.document).encode()
            self._answer(200, body, missing=10 if self.short else 0)
        else:
            self._answer(200, b"step-body")

    def _answer(
        self, status: int, body: bytes, *, location: str | None = None, missing: int = 0
    ) -> None:
        self.send_response(status)
        if location is not None:
            self.send_header("Location", location)
        self.send_header("Content-Length", str(len(body) + missing))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


class _Elsewhere(_FakePlatform):
    """설정 경로가 아닌 다른 호스트다. 보통으로 답하고 받은 요청을 따로 남긴다."""

    redirect_to = None
    seen: list[dict[str, Any]] = []


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


@pytest.fixture
def fake_platform(monkeypatch):
    """가짜 플랫폼의 주소(렌더의 platform_internal_url)와 그 처리기다. 플랫폼이 아는 이름에 가짜
    이미지의 hello 를 더해 둘 다 기록에 남는지 본다."""
    monkeypatch.setattr(agent_features, "KNOWN", frozenset({"cancel", "hello"}))
    _FakePlatform.seen, _Elsewhere.seen = [], []
    _FakePlatform.status, _FakePlatform.redirect_to = 200, None
    _FakePlatform.raw, _FakePlatform.short = None, False
    _FakePlatform.document = {"features": ["hello", "absent"]}
    with _serve(_FakePlatform) as base:
        yield base, _FakePlatform


def _fake_image(root: Path) -> None:
    """런타임 이미지의 `runtime` 꾸러미를 흉내 낸다."""
    (root / "runtime" / "init_steps").mkdir(parents=True)
    (root / "runtime" / "__init__.py").write_text("")
    (root / "runtime" / "init_steps" / "__init__.py").write_text("")
    (root / "runtime" / "features.py").write_text('KNOWN = frozenset({"hello", "cancel"})\n')
    (root / "runtime" / "init_steps" / "hello.py").write_text(
        textwrap.dedent(
            """
            import os

            def run(config_dir, fetch):
                with fetch("/steps/hello") as response:
                    body = response.read()
                with open(os.path.join(config_dir, "hello.txt"), "wb") as f:
                    f.write(body)
            """
        )
    )


def _init_env(base: str, config: Path) -> dict[str, str]:
    """렌더된 init 컨테이너의 env 그대로다. 플랫폼 주소는 가짜 플랫폼이고, 받을 파일 경로만 이
    테스트의 설정 디렉터리로 옮기며 Secret 참조(인스턴스 토큰)는 "tok" 이다."""
    dep = render_agent_deployment(
        minimal_agent_doc(),
        effective_config(platform_internal_url=base),
        providers=[],
        data_services={},
        agent_oauth=None,
        client_id_claim="cid",
        groups_claim=None,
    )
    (init,) = dep["spec"]["template"]["spec"]["initContainers"]
    env = {e["name"]: e.get("value", "tok") for e in init["env"]}
    env[CONFIG_PATH_ENV] = str(config / CONFIG_FILE)
    return env


def _run_fetch(root: Path, base: str, *, termination_log: Path) -> subprocess.CompletedProcess:
    config = root / "config"
    config.mkdir(exist_ok=True)
    script = FETCH_SCRIPT.replace('"/dev/termination-log"', repr(str(termination_log)))
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env={**os.environ, **_init_env(base, config)},
        capture_output=True,
        text=True,
    )


def _config_path(base: str, suffix: str = CONFIG_FILE) -> str:
    """가짜 플랫폼이 받는 경로다. 렌더가 쓰는 주소 함수에서 뽑는다."""
    return urlsplit(agent_config_base(base, minimal_agent_doc().id)).path + "/" + suffix


def test_fetch_script_sends_both_headers_writes_and_runs_steps(tmp_path, fake_platform) -> None:
    base, platform = fake_platform
    _fake_image(tmp_path)
    result = _run_fetch(tmp_path, base, termination_log=tmp_path / "term")
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "config" / CONFIG_FILE).read_text()) == platform.document
    first = platform.seen[0]
    assert urlsplit(agent_json_url(base, minimal_agent_doc().id)).path == first["path"]
    assert first["headers"]["authorization"] == "Bearer tok"
    reported = first["reported"]
    assert reported.image == effective_config().images.agent_image
    assert reported.names == frozenset({"cancel", "hello"}) and reported.warnings == ()
    # 이름마다 이미지 안 단계를 부르고(같은 머리로 설정 경로 아래를 연다) 없는 것은 건너뛴다
    assert (tmp_path / "config" / "hello.txt").read_bytes() == b"step-body"
    paths = [_config_path(base), _config_path(base, "steps/hello")]
    assert [s["path"] for s in platform.seen] == paths
    assert platform.seen[1]["reported"] == reported
    assert not (tmp_path / "term").exists()


def test_fetch_script_writes_the_error_code_and_stops(tmp_path, fake_platform) -> None:
    base, platform = fake_platform
    platform.status = 409
    _fake_image(tmp_path)
    result = _run_fetch(tmp_path, base, termination_log=tmp_path / "term")
    assert result.returncode == 1
    assert "runtime_image_too_old: missing: core" in result.stderr
    assert (tmp_path / "term").read_text() == "runtime_image_too_old: missing: core"
    assert not (tmp_path / "config" / CONFIG_FILE).exists()
    assert len(platform.seen) == 1


def test_fetch_script_does_not_follow_redirects(tmp_path, fake_platform) -> None:
    """설정 경로가 다른 호스트로 3xx 를 주면 따르지 않고 멈춘다. 인스턴스 토큰은 플랫폼 소유
    비밀이다. init 단계의 `fetch` 도 같은 받기다."""
    base, platform = fake_platform
    _fake_image(tmp_path)
    with _serve(_Elsewhere) as elsewhere:
        platform.redirect_to = f"{elsewhere}/{CONFIG_FILE}"
        result = _run_fetch(tmp_path, base, termination_log=tmp_path / "term")
    assert result.returncode != 0
    assert (tmp_path / "term").read_text().startswith("http_302")
    assert not any("authorization" in s["headers"] for s in _Elsewhere.seen)
    assert not (tmp_path / "config" / CONFIG_FILE).exists()


@pytest.mark.parametrize(
    ("raw", "short", "code"),
    [
        (None, True, "unreachable: IncompleteRead"),  # 읽다 끊긴 본문
        (b"<html>proxy error</html>", False, "agent_config_invalid: JSONDecodeError"),
        (b"[1, 2]", False, "agent_config_invalid: AttributeError"),
    ],
    ids=["incomplete_read", "not_json", "not_an_object"],
)
def test_fetch_script_leaves_a_reason_for_every_failure(
    tmp_path, fake_platform, raw, short, code
) -> None:
    """정해 둔 HTTP·연결 오류 밖의 실패도 termination log 에 `코드: 문구` 를 남긴다. 남기지 않으면
    상태의 이유가 비어 CrashLoopBackOff 만 보인다. 못 읽은 문서는 설정 볼륨에 쓰지 않는다."""
    base, platform = fake_platform
    platform.raw, platform.short = raw, short
    _fake_image(tmp_path)
    result = _run_fetch(tmp_path, base, termination_log=tmp_path / "term")
    assert result.returncode == 1
    assert (tmp_path / "term").read_text().startswith(code)
    assert not (tmp_path / "config" / CONFIG_FILE).exists()


def test_fetch_script_reports_a_step_that_crashes(tmp_path, fake_platform) -> None:
    """기능 단계가 이유를 남기지 못하고 예외로 멈추면 받기 스크립트가 그 이름과 함께 남긴다."""
    base, platform = fake_platform
    platform.document = {"features": ["boom"]}
    _fake_image(tmp_path)
    (tmp_path / "runtime" / "init_steps" / "boom.py").write_text(
        "def run(config_dir, fetch):\n    raise KeyError('name')\n"
    )
    result = _run_fetch(tmp_path, base, termination_log=tmp_path / "term")
    assert result.returncode == 1
    assert (tmp_path / "term").read_text() == "init_step_failed: boom: KeyError('name')"


def test_dropped_attached_features_share_one_warning() -> None:
    """이미지가 모르는 `attached` 기능은 빼고 돈다. 경고는 `runtime_feature_dropped` 하나다."""
    wanted = AgentFeatures(attached=frozenset({TOOL_SAFETY, PROVIDER_EXTRA_BODY}))
    check = check_features(wanted, frozenset())
    assert check.allowed == frozenset() and check.dropped == (PROVIDER_EXTRA_BODY, TOOL_SAFETY)
    assert [w.code for w in check.warnings] == ["runtime_feature_dropped"]
    assert PROVIDER_EXTRA_BODY in check.warnings[0].message
    assert TOOL_SAFETY in check.warnings[0].message
    known = check_features(wanted, frozenset({TOOL_SAFETY}))
    assert known.dropped == (PROVIDER_EXTRA_BODY,)


def test_a_web_graph_requires_tool_safety() -> None:
    """웹 Tool 이 있는 그래프는 웹 내용 뒤의 쓰기를 `tool_safety` 가 묻으므로 요구한다. 그 밖의
    그래프에는 붙인다(P43 8절)."""
    doc = agent_doc(spec=template_spec())
    web = agent_features.agent_features(doc, doc.current, {}, web=True)
    assert web == AgentFeatures(required=frozenset({WEB_GUARD, TOOL_SAFETY}))
    plain = agent_features.agent_features(doc, doc.current, {})
    assert plain == AgentFeatures(attached=frozenset({TOOL_SAFETY}))


def test_in_pod_state_is_attached_only_when_the_preview_asks() -> None:
    """파드 안에서 묻는 미리 보기만 상태가 파드 하나에 산다고 알린다(P44b 7절). 서비스가 정하고
    (`AgentService._preview_asks`) 그 밖에는 붙지 않는다. 붙이는 기능이라 그 이름을 모르는
    이미지에서는 빠지고 `runtime_feature_dropped` 로 남으며, 그 미리 보기는 승인을 거절하며 돈다."""
    doc = agent_doc(spec=template_spec())
    assert IN_POD_STATE not in agent_features.agent_features(doc, doc.current, {}, web=True).wanted
    wanted = agent_features.agent_features(doc, doc.current, {}, in_pod_approvals=True)
    assert wanted == AgentFeatures(attached=frozenset({TOOL_SAFETY, IN_POD_STATE}))
    web = agent_features.agent_features(doc, doc.current, {}, web=True, in_pod_approvals=True)
    assert web.attached == {IN_POD_STATE} and web.required == {WEB_GUARD, TOOL_SAFETY}
    old = check_features(wanted, KNOWN - {IN_POD_STATE})
    assert old.allowed == {TOOL_SAFETY} and old.dropped == (IN_POD_STATE,)
    assert [w.code for w in old.warnings] == ["runtime_feature_dropped"]
