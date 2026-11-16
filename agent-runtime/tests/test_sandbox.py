"""코드 실행 샌드박스 백엔드다(P40 F4, 1절·5절·6절·9절).

가짜 플랫폼과 가짜 파드(sandbox_fixtures.py)로 본다. 세션 둘이 한 인스턴스에서 갈리는 것, 취소가
연결을 닫아 명령을 멈추는 것, 명령과 열기의 시한이 턴의 남은 시간으로 잘리는 것, 연결마다의 증명,
답 본문의 상한, 끊긴 호출을 다시 하지 않는 것, 회수·잃음·메모리 문장, 넣기, 세션 파일이 샌드박스를
열지 않는 것, 내려받기 거절, 메인과 서브에이전트의 execute 승인, 파일 Tool 이 꺼진 문서의 거절,
닫기의 길, 비동기 턴이 동기 메서드를 부르지 않는 것이다.
"""

from __future__ import annotations

import asyncio
import base64
import io
import tarfile
import threading
import time
import zipfile
import zlib
from pathlib import Path
from typing import Any

import pytest
from deepagents.backends import CompositeBackend, StateBackend

from runtime import downloads as sb_downloads
from runtime import sandbox as sb
from runtime.compile import ARTIFACTS_ROOT, build_backend, compile_agent
from runtime.config import ConfigError, parse_config
from runtime.downloads import download_name, normalized_name, refused_download
from runtime.sandbox import (
    NEXT_MESSAGE,
    OOM_KEPT,
    PROMPT_LINE,
    PlatformSandboxes,
    SandboxBackend,
    Sandboxes,
    SandboxProblem,
    command_seconds,
    open_sandboxes,
)
from runtime.sessions import CLOSE_DELETED, CLOSE_RUN, CLOSE_SESSION
from runtime.state import Settings
from runtime.tool_safety import ASK_RULE, SKIP
from runtime.uploads import KIND_IMAGE, KIND_ORIGINAL, KIND_TEXT, StoredUpload, TurnUploads
from tests.app_fixtures import (
    FileToolModel,
    checkpoint_humans,
    sid_of,
    status_and_code,
    subagent_spec,
    tool_message,
)
from tests.attachment_fixtures import document, send
from tests.conftest import chat, events_named, offline_doc, run_app
from tests.sandbox_fixtures import FakePlatform, SandboxRecord, code_doc, fake_sandbox
from tests.store_fixtures import pod_store


def sandboxes_for(platform: FakePlatform, **kwargs: Any) -> Sandboxes:
    return Sandboxes(PlatformSandboxes(platform.url, None), **kwargs)


@pytest.fixture
def short_restart_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """끊긴 호출 뒤 다시 뜨지 않는 파드를 다시 찾는 시간을 4초에서 0.3초로 줄인다. 끊김으로 끝나는
    길은 같다."""
    monkeypatch.setattr(sb, "RESTART_POLL_SECONDS", (0.05,))
    monkeypatch.setattr(sb, "RESTART_WAIT_SECONDS", 0.3)


def upload(kind: str, name: str, content: bytes, path: str | None = None) -> StoredUpload:
    from runtime.sessions import utcnow

    return StoredUpload(
        upload_id=f"u-{kind}-{name}",
        kind=kind,
        name=name,
        path=path or f"/uploads/{name}",
        size=len(content),
        sha256="0" * 64,
        content=content,
        created_at=utcnow(),
    )


# --- 세션마다, 시한, 증명 ----------------------------------------------------------------------


async def test_two_sessions_on_one_instance_stay_apart() -> None:
    async with fake_sandbox() as platform:
        boxes = sandboxes_for(platform)
        a = await boxes.ensure("s-a", deadline=None)
        b = await boxes.ensure("s-b", deadline=None)
        assert a.sandbox_id == "sbx-s-a-1" and b.sandbox_id == "sbx-s-b-1"
        assert boxes.cached("s-a") is a and boxes.cached("s-b") is b
        # 캐시에 있으면 다시 열지 않는다
        await boxes.ensure("s-a", deadline=None)
        assert [c["session_id"] for c in platform.calls("open")] == ["s-a", "s-b"]
        await boxes.aclose()


async def test_every_connection_proves_itself_before_the_token() -> None:
    async with fake_sandbox() as platform:
        boxes = sandboxes_for(platform)
        await boxes.execute("s", "echo hi", timeout=None, deadline=None)
        await boxes.execute("s", "echo again", timeout=None, deadline=None)
        # 호출마다 새 연결이고 첫 요청은 토큰 없는 증명이다
        assert len(platform.pod.connections) == 3  # healthz, execute 둘
        for conn in platform.pod.connections:
            assert conn.paths[0] == "/proof" and conn.authorized[0] is False
        await boxes.aclose()


@pytest.mark.usefixtures("short_restart_wait")
async def test_a_wrong_proof_sends_no_token_and_is_not_retried() -> None:
    async with fake_sandbox() as platform:
        boxes = sandboxes_for(platform)
        await boxes.ensure("s", deadline=None)
        platform.pod.wrong_proof = True
        with pytest.raises(SandboxProblem):
            await boxes.execute("s", "echo hi", timeout=None, deadline=None)
        last = platform.pod.connections[-1]
        assert last.paths == ["/proof"] and not any(last.authorized)
        assert platform.pod.executed == []
        # 캐시를 버렸다. 옛 주소를 다시 쓰지 않는다
        assert boxes.cached("s") is None
        await boxes.aclose()


@pytest.mark.usefixtures("short_restart_wait")
async def test_an_oversized_answer_is_not_read() -> None:
    async with fake_sandbox() as platform:
        boxes = sandboxes_for(platform)
        await boxes.ensure("s", deadline=None)
        platform.pod.oversized = sb.EXECUTE_BODY_LIMIT + 1
        with pytest.raises(SandboxProblem) as e:
            await boxes.execute("s", "echo hi", timeout=None, deadline=None)
        assert e.value.message == sb.BROKEN
        await boxes.aclose()


async def test_a_broken_call_is_not_retried_and_names_a_memory_kill() -> None:
    async with fake_sandbox() as platform:
        boxes = sandboxes_for(platform)
        await boxes.ensure("s", deadline=None)
        platform.pod.drop_execute = True
        rec = platform.open["s"]
        rec.restarts, rec.last_termination = 1, {"reason": "OOMKilled", "exit_code": 137}
        with pytest.raises(SandboxProblem) as e:
            await boxes.execute("s", "python hog.py", timeout=None, deadline=None)
        assert e.value.message == OOM_KEPT
        assert len(platform.pod.executed) == 1
        await boxes.aclose()


async def test_a_memory_kill_the_kubelet_reports_late_still_says_files_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 컨테이너가 죽은 바로 뒤의 찾기는 아직 준비됐고 재시작도 없다고 답한다. 세 번째 찾기에서야
    # kubelet 이 OOMKilled 와 재시작을 알린다. 런타임은 그때까지 다시 찾고 파일이 남았다고 말한다
    monkeypatch.setattr(sb, "RESTART_POLL_SECONDS", (0.01, 0.02, 0.05))
    async with fake_sandbox() as platform:
        boxes = sandboxes_for(platform)
        await boxes.ensure("s", deadline=None)
        platform.pod.drop_execute = True
        rec = platform.open["s"]

        def kubelet_reports_late() -> None:
            if len(platform.calls("lookup")) >= 3:
                rec.restarts, rec.last_termination = 1, {"reason": "OOMKilled", "exit_code": 137}

        platform.on_lookup = kubelet_reports_late
        with pytest.raises(SandboxProblem) as e:
            await boxes.execute("s", "python hog.py", timeout=None, deadline=None)
        assert e.value.message == OOM_KEPT
        assert len(platform.calls("lookup")) == 3
        assert len(platform.pod.executed) == 1
        await boxes.aclose()


async def test_a_broken_call_on_a_healthy_pod_waits_only_within_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 다시 뜨지 않는 파드면 끊김이다. 다시 찾는 시간은 RESTART_WAIT_SECONDS 와 턴의 남은 시간을
    # 넘지 않는다
    monkeypatch.setattr(sb, "RESTART_POLL_SECONDS", (0.05,))
    monkeypatch.setattr(sb, "RESTART_WAIT_SECONDS", 0.3)
    async with fake_sandbox() as platform:
        boxes = sandboxes_for(platform)
        await boxes.ensure("s", deadline=None)
        platform.pod.drop_execute = True
        started = time.monotonic()
        with pytest.raises(SandboxProblem) as e:
            await boxes.execute("s", "echo hi", timeout=None, deadline=None)
        assert e.value.message == sb.BROKEN
        assert time.monotonic() - started < 2.0
        assert 2 <= len(platform.calls("lookup")) <= 8
        lookups = len(platform.calls("lookup"))
        # 남은 턴이 1.2초면 다시 찾는 시간은 거의 없다
        await boxes.ensure("s", deadline=None)
        monkeypatch.setattr(sb, "RESTART_WAIT_SECONDS", 30.0)
        started = time.monotonic()
        with pytest.raises(SandboxProblem):
            await boxes.execute("s", "echo hi", timeout=None, deadline=time.monotonic() + 1.2)
        assert time.monotonic() - started < 3.0
        assert len(platform.calls("lookup")) > lookups
        await boxes.aclose()


def test_command_timeouts_are_clamped_to_the_turn() -> None:
    now = time.monotonic()
    assert command_seconds(None, now + 1000) == sb.DEFAULT_COMMAND_SECONDS
    assert command_seconds(900, now + 1000) == sb.MAX_COMMAND_SECONDS
    assert command_seconds(0, now + 1000) == sb.MAX_COMMAND_SECONDS
    assert 9 <= command_seconds(None, now + 10) <= 10
    with pytest.raises(SandboxProblem) as e:
        command_seconds(30, now + 0.2)
    assert e.value.message == sb.NO_TIME


async def test_commands_and_the_open_wait_get_the_turn_left() -> None:
    async with fake_sandbox() as platform:
        boxes = sandboxes_for(platform)
        deadline = time.monotonic() + 20
        await boxes.execute("s", "echo hi", timeout=200, deadline=deadline)
        assert 0 < platform.calls("open")[0]["wait_seconds"] <= 20
        assert 0 < platform.pod.executed[0]["timeout"] <= 20
        await boxes.aclose()


async def test_cancel_closes_the_connection_and_stops_the_command() -> None:
    async with fake_sandbox() as platform:
        boxes = sandboxes_for(platform)
        await boxes.ensure("s", deadline=None)
        task = asyncio.create_task(boxes.execute("s", "sleep 600", timeout=300, deadline=None))
        while not platform.pod.executed:
            await asyncio.sleep(0.01)
        cancelled_at = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        conn = platform.pod.connections[-1]
        for _ in range(300):
            if conn.disconnected_at is not None:
                break
            await asyncio.sleep(0.01)
        assert conn.disconnected_at is not None and conn.disconnected_at - cancelled_at < 3
        await boxes.aclose()


async def test_a_server_of_another_api_is_refused() -> None:
    async with fake_sandbox() as platform:
        platform.pod.api = 2
        boxes = sandboxes_for(platform)
        with pytest.raises(SandboxProblem) as e:
            await boxes.ensure("s", deadline=None)
        assert "API 2" in e.value.message and boxes.cached("s") is None
        await boxes.aclose()


# --- 회수, 잃음, 거절 ----------------------------------------------------------------------


async def test_a_reclaim_seen_by_touch_answers_once_then_opens_a_new_generation() -> None:
    async with fake_sandbox() as platform:
        boxes = sandboxes_for(platform)
        await boxes.execute("s", "echo hi", timeout=None, deadline=None)
        platform.close_record("s", "idle")
        await boxes.touch("s")
        executed = len(platform.pod.executed)
        with pytest.raises(SandboxProblem) as e:
            await boxes.execute("s", "echo again", timeout=None, deadline=None)
        assert e.value.message == sb.GONE_MESSAGES["idle"] + NEXT_MESSAGE
        assert len(platform.pod.executed) == executed
        await boxes.execute("s", "echo new", timeout=None, deadline=None)
        assert platform.open["s"].generation == 2
        await boxes.aclose()


async def test_a_reclaim_found_after_a_restart_is_told_once() -> None:
    async with fake_sandbox() as platform:
        platform.closed_reason["s"] = "reclaimed"
        boxes = sandboxes_for(platform)
        with pytest.raises(SandboxProblem) as e:
            await boxes.ensure("s", deadline=None)
        assert e.value.message.startswith(sb.GONE_MESSAGES["reclaimed"])
        ref = await boxes.ensure("s", deadline=None)
        assert ref.generation == 1 and platform.calls("open")
        await boxes.aclose()


async def test_a_failed_open_never_counts_as_a_lost_workspace() -> None:
    async with fake_sandbox() as platform:
        platform.closed_reason["s"] = "limit_person"
        boxes = sandboxes_for(platform)
        assert (await boxes.ensure("s", deadline=None)).generation == 1
        await boxes.aclose()


@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        (429, {"code": "sandbox_limit", "cap": "person"}, sb.LIMIT_MESSAGES["person"]),
        (429, {"code": "sandbox_limit", "cap": "cluster"}, sb.LIMIT_MESSAGES["cluster"]),
        (409, {"code": "sandbox_gate_closed", "reason": "unaccepted"}, "(unaccepted)"),
        (503, {"code": "sandbox_starting"}, sb.STARTING),
        (503, {"code": "sandbox_open_failed", "reason": "oom"}, "could not start (oom)"),
        (503, {"code": "sandbox_namespace_policy", "policy": "allow-all"}, "allow-all"),
        # 함께 열던 다른 열기가 실패해 닫은 기록이다. 잃은 것과 같다
        (404, {"code": "sandbox_not_found", "closed_reason": "failed_oom"}, "was lost, together"),
    ],
)
async def test_open_refusals_reach_the_model_as_messages(
    status: int, detail: dict[str, Any], expected: str
) -> None:
    async with fake_sandbox() as platform:
        platform.refusal = (status, detail)
        boxes = sandboxes_for(platform)
        with pytest.raises(SandboxProblem) as e:
            await boxes.ensure("s", deadline=None)
        assert expected in e.value.message
        # 다음 호출은 새로 연다
        assert (await boxes.ensure("s", deadline=None)).generation == 1
        await boxes.aclose()


# --- 넣기와 세션 파일 ------------------------------------------------------------------------


async def test_attachments_and_skills_are_copied_once_per_generation(tmp_path: Path) -> None:
    (tmp_path / "main" / "report").mkdir(parents=True)
    (tmp_path / "main" / "report" / "SKILL.md").write_text("# report")
    async with fake_sandbox() as platform:
        boxes = sandboxes_for(platform, skills_root=tmp_path)
        files = [
            upload(KIND_TEXT, "a.pdf", b"text", path="/uploads/a.pdf.txt"),
            upload(KIND_ORIGINAL, "a.pdf", b"%PDF-original"),
            upload(KIND_TEXT, "b.csv", b"x,y"),
            upload(KIND_ORIGINAL, "b.csv", b"x;y"),
            upload(KIND_IMAGE, "c.png", b"\x89PNG"),
        ]
        turn = TurnUploads(files=files)
        await boxes.execute("s", "echo hi", timeout=None, deadline=None, uploads=turn)
        assert copies(platform) == {
            "/workspace/uploads/a.pdf.txt": b"text",
            "/workspace/uploads/a.pdf": b"%PDF-original",
            # 같은 자리의 원본이 글을 이긴다
            "/workspace/uploads/b.csv": b"x;y",
            "/workspace/uploads/c.png": b"\x89PNG",
            "/workspace/skills/main/report/SKILL.md": b"# report",
        }
        platform.pod.files.clear()
        newer = TurnUploads(files=[*files, upload(KIND_IMAGE, "d.png", b"\x89PNG2")])
        await boxes.execute("s", "echo again", timeout=None, deadline=None, uploads=newer)
        assert copies(platform) == {"/workspace/uploads/d.png": b"\x89PNG2"}
        # 새 세대는 처음부터 다시 올린다
        platform.close_record("s", "idle")
        await boxes.touch("s")
        with pytest.raises(SandboxProblem):
            await boxes.execute("s", "echo x", timeout=None, deadline=None, uploads=newer)
        platform.pod.files.clear()
        await boxes.execute("s", "echo y", timeout=None, deadline=None, uploads=newer)
        assert len(copies(platform)) == 6
        await boxes.aclose()


def copies(platform: FakePlatform) -> dict[str, bytes]:
    """작업 공간의 사본이다. 넣기의 표지는 뺀다."""
    return {p: d for p, d in platform.pod.files.items() if not p.startswith(sb.MARKER_DIR)}


async def test_another_replica_does_not_copy_over_the_models_edits(tmp_path: Path) -> None:
    """넣기는 작업 공간의 표지를 본다. 다른 레플리카나 다시 뜬 파드가 같은 세대에 다시 올려 모델이
    고친 사본을 덮지 않는다(7)."""
    (tmp_path / "main" / "report").mkdir(parents=True)
    (tmp_path / "main" / "report" / "SKILL.md").write_text("# report")
    async with fake_sandbox() as platform:
        turn = TurnUploads(files=[upload(KIND_TEXT, "a.csv", b"x,y")])
        first = sandboxes_for(platform, skills_root=tmp_path)
        await first.execute("s", "echo hi", timeout=None, deadline=None, uploads=turn)
        platform.pod.files["/workspace/uploads/a.csv"] = b"edited"
        platform.pod.files["/workspace/skills/main/report/SKILL.md"] = b"edited"
        second = sandboxes_for(platform, skills_root=tmp_path)
        await second.execute("s", "echo again", timeout=None, deadline=None, uploads=turn)
        assert platform.pod.files["/workspace/uploads/a.csv"] == b"edited"
        assert platform.pod.files["/workspace/skills/main/report/SKILL.md"] == b"edited"
        await first.aclose()
        await second.aclose()


async def test_session_files_never_open_a_sandbox() -> None:
    async with fake_sandbox() as platform:
        boxes = sandboxes_for(platform)
        assert await boxes.list_files("s") == []
        assert await boxes.read_file("s", "/workspace/a.png") is None
        assert platform.calls("open") == []
        await boxes.ensure("s", deadline=None)
        platform.pod.files.update(
            {"/workspace/out.png": b"png", "/workspace/uploads/a.csv": b"x", "/tmp/x": b"t"}
        )
        listed = await boxes.list_files("s")
        assert [f["path"] for f in listed] == ["/workspace/out.png"]
        found = await boxes.read_file("s", "/workspace/out.png")
        assert found is not None and found.content == b"png"
        await boxes.aclose()


# --- 내려받기 거절 (6절 표) ------------------------------------------------------------------


def zipped(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return out.getvalue()


def tarred(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("tool.exe", b"anything"),
        ("tool.exe. . ", b"anything"),
        ("TOOL.EXE", b"x"),
        ("hidden", b"MZ\x90\x00"),
        ("lib", b"\x7fELF\x02"),
        ("macho", b"\xcf\xfa\xed\xfe"),
        ("App.class", b"\xca\xfe\xba\xbe"),
        ("link.lnk", b"x"),
        ("short", b"L\x00\x00\x00\x01\x14\x02\x00rest"),
        ("disk.iso", b"x"),
        ("disk", b"\0" * 0x8001 + b"CD001" + b"\0" * 10),
        ("disk", b"\0" * 1024 + b"koly" + b"\0" * 508),
        ("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
        ("m.docm", b"x"),
        ("o.rtf", b"{\\rtf1 {\\object x}}"),
        ("doc.docx", zipped({"word/document.xml": b"x", "word/vbaProject.bin": b"x"})),
        ("sheet.xlsx", zipped({"xl/embeddings/oleObject1.bin": b"x"})),
        ("app.jar", zipped({"META-INF/MANIFEST.MF": b"x"})),
        ("bundle.zip", zipped({"run.exe": b"x"})),
        ("bundle.zip", zipped({"inner.zip": zipped({"a.txt": b"a"})})),
        ("bundle.zip", zipped({"blob": b"MZ\x90"})),
        ("bundle.tar.gz", tarred({"bin/tool": b"\x7fELF"})),
        ("bundle.tgz", tarred({"inner.7z": b"x"})),
        ("x.7z", b"x"),
        ("x.rar", b"x"),
        ("data", b"7z\xbc\xaf\x27\x1c"),
        ("data.csv.gz", b"\x1f\x8b\x08"),
        ("evil‮gnp.exe", b"x"),
        ("clean‮.png", b"\x89PNG"),
    ],
)
def test_refused_downloads(name: str, content: bytes) -> None:
    assert refused_download(name, content)


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("chart.png", b"\x89PNG\r\n\x1a\n"),
        ("run.sh", b"#!/bin/sh\necho hi\n"),
        ("script.py", b"print('hi')\n"),
        ("data.csv", b"a,b\n1,2\n"),
        ("report.docx", zipped({"word/document.xml": b"<w/>", "word/media/a.png": b"\x89PNG"})),
        ("bundle.zip", zipped({"a.csv": b"a", "b/c.txt": b"c"})),
        ("bundle.tar.gz", tarred({"a.csv": b"a"})),
        ("Mz.txt", b"Mzzz"),
    ],
)
def test_allowed_downloads(name: str, content: bytes) -> None:
    assert not refused_download(name, content)


def test_names_are_normalized_before_the_test() -> None:
    assert normalized_name("tool.exe. ") == "tool.exe"
    assert normalized_name("é.txt") == "é.txt"
    assert normalized_name("a⁦b") is None


# --- 컴파일 ---------------------------------------------------------------------------------


def code_config(
    approval: list[str] | None = None, *, subagent: bool = False, safety: bool = False
) -> Any:
    doc = offline_doc()
    doc["main"]["tools"] = []
    doc["tools"] = []
    if safety:
        doc["features"] = ["tool_safety"]
    if subagent:
        doc["subagents"] = [subagent_spec(doc, "calc", tools=[])]
        doc["main"]["subagents"] = ["calc"]
    return parse_config(code_doc(doc, approval))


def test_internal_paths_stay_in_state() -> None:
    boxes = Sandboxes(PlatformSandboxes("http://platform.invalid", None))
    backend = build_backend(None, sandbox=SandboxBackend(boxes))
    assert isinstance(backend, CompositeBackend)
    assert isinstance(backend.default, SandboxBackend)
    assert backend.artifacts_root == ARTIFACTS_ROOT
    routes = dict(backend.sorted_routes)
    assert isinstance(routes[f"{ARTIFACTS_ROOT}/"], StateBackend)


@pytest.mark.parametrize("approval", [[], ["approve", "reject"]])
def test_execute_approval_is_compiled_for_main_and_subagents(
    approval: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    from runtime import compile as compile_module

    real = compile_module._subagent

    def spy(*args: Any, **kwargs: Any) -> Any:
        sub = real(*args, **kwargs)
        seen.append(sub)
        return sub

    monkeypatch.setattr(compile_module, "_subagent", spy)
    boxes = Sandboxes(PlatformSandboxes("http://platform.invalid", None))
    compiled = compile_agent(
        code_config(approval, subagent=True),
        tools_by_ref={},
        checkpointer=None,
        model=FileToolModel(model_name="stub-sandbox"),
        sandboxes=boxes,
    )
    for entries in (compiled.interrupt_on, seen[0]["interrupt_on"]):
        if approval:
            assert entries["execute"]["allowed_decisions"][: len(approval)] == approval
        else:
            assert "execute" not in entries or entries["execute"].get("when") is not None
    assert PROMPT_LINE in seen[0]["system_prompt"]


def test_an_untrusted_run_adds_no_approval_on_execute() -> None:
    """execute 는 작업 공간 밖에 쓰지 않으므로 바깥 내용을 가진 대화(웹훅 실행)도 묻지 않는다.
    Agent 가 승인을 걸었으면 그 규칙은 그대로다(9절)."""
    for approval, verdict in (([], SKIP), (["approve", "reject"], ASK_RULE)):
        boxes = Sandboxes(PlatformSandboxes("http://platform.invalid", None))
        compiled = compile_agent(
            code_config(approval, safety=True),
            tools_by_ref={},
            checkpointer=None,
            model=FileToolModel(model_name="stub-sandbox"),
            sandboxes=boxes,
        )
        assert compiled.safety is not None
        call = {"id": "c1", "name": "execute", "args": {"command": "ls"}}
        assert compiled.safety.verdict(call, flagged=True, choices={}) == verdict


def test_code_execution_needs_the_file_tools_and_the_route() -> None:
    doc = code_doc(offline_doc())
    doc["main"]["builtin_tools"]["filesystem"] = False
    with pytest.raises(ConfigError) as e:
        open_sandboxes(parse_config(doc), "http://platform", None)
    assert e.value.code == "code_execution_filesystem_disabled"
    with pytest.raises(ConfigError) as e:
        open_sandboxes(parse_config(code_doc(offline_doc())), None, None)
    assert e.value.code == "sandbox_url_missing"
    assert open_sandboxes(parse_config(offline_doc()), None, None) is None


# --- 닫기의 길 ------------------------------------------------------------------------------


async def test_closing_paths_close_the_sandbox() -> None:
    closed: list[tuple[str, str]] = []

    async def on_close(session_id: str, reason: str) -> None:
        closed.append((session_id, reason))

    async with pod_store(offline_doc()) as store:
        store.on_close = on_close
        await store.drop_state("ttl", CLOSE_SESSION)
        await store.drop_state("run")
        await store.delete_session("deleted")
        # 보존 기간이 지나 기록을 지울 때는 닫을 때 이미 닫았다
        await store.delete_session("purged", close_sandbox=False)
    assert closed == [("ttl", CLOSE_SESSION), ("run", CLOSE_RUN), ("deleted", CLOSE_DELETED)]


# --- 턴 -----------------------------------------------------------------------------------


async def test_a_turn_runs_execute_without_a_sync_call() -> None:
    """동기 메서드는 모두 오류라 턴이 끝까지 돌면 비동기만 부른 것이다. 세션을 지우면 닫는다."""
    async with fake_sandbox() as platform:
        doc = offline_doc()
        doc["main"]["tools"], doc["tools"] = [], []
        doc["entry"]["greeting"] = None
        settings = Settings(sandbox_url=platform.url)
        model = FileToolModel(model_name="stub-sandbox")
        async with run_app(code_doc(doc), settings=settings, model=model) as running:
            response, sse = await chat(running, tool_message("execute", command="echo 안녕"))
            assert response.status_code == 200, response.text
            results = events_named(sse, "tool_result")
            assert results and "안녕" in str(results[0]) and "exit code 0" in str(results[0])
            sid = sid_of(sse, "done")
            # 턴은 열릴 때 쓰기를 알린다. 첫 턴은 아직 샌드박스가 없어 404 이고 아무것도 하지 않는다
            assert platform.calls("touch") == [{"session_id": sid}]
            response, _ = await chat(running, "고마워", session_id=sid)
            assert response.status_code == 200
            assert platform.calls("touch") == [{"session_id": sid}] * 2
            listed = await running.client.get(
                f"/sessions/{sid}/files", params={"user_sub": "alice"}
            )
            assert listed.status_code == 200
            deleted = await running.client.delete(f"/sessions/{sid}", params={"user_sub": "alice"})
            assert deleted.status_code == 200
            assert platform.calls("close")[-1] == {"session_id": sid, "reason": "session_deleted"}


# --- 원본과 작업 공간 파일 (6절) ---------------------------------------------------------------


def code_app_doc() -> dict[str, Any]:
    doc = offline_doc()
    doc["main"]["tools"], doc["tools"] = [], []
    doc["entry"]["greeting"] = None
    return code_doc(doc)


async def test_originals_are_kept_beside_the_text_and_copied_to_the_workspace() -> None:
    """원본은 글 바로 뒤의 `original` 첨부다. 목록과 전사에는 오르지 않고, 안내 줄이 자리를 말하며,
    세션 상한에 들지 않으면 글만 남는다. 다음 호출 전에 작업 공간에 오른다."""
    pdf = b"%PDF-1.7 original"
    big = b"x" * (2 * 1024 * 1024 + 1)
    async with fake_sandbox() as platform:
        settings = Settings(sandbox_url=platform.url)
        model = FileToolModel(model_name="stub-sandbox")
        async with run_app(code_app_doc(), settings=settings, model=model) as running:
            message = tool_message("execute", command="echo 봤다")
            attachments = [
                document("a.pdf", original=base64.b64encode(pdf).decode()),
                document("big.pdf", original=base64.b64encode(big).decode()),
            ]
            response, sse = await send(running, attachments, message)
            assert response.status_code == 200, response.text
            sid = sid_of(sse, "done")
            assert platform.pod.files["/workspace/uploads/a.pdf"] == pdf
            assert platform.pod.files["/workspace/uploads/a.pdf.txt"]
            assert "/workspace/uploads/big.pdf" not in platform.pod.files
            stored = await running.runtime.store.uploads.read(sid)
            assert [u.kind for u in stored] == ["text", "original", "text"]
            human = (await checkpoint_humans(running, sid))[0].text
            assert "commands read the original file at /uploads/a.pdf" in human
            assert "the original file was not kept" in human
            listed = await running.client.get(
                f"/sessions/{sid}/files", params={"user_sub": "alice"}
            )
            paths = [(f["path"], f["origin"]) for f in listed.json()["files"]]
            assert paths == [("/uploads/a.pdf.txt", "upload"), ("/uploads/big.pdf.txt", "upload")]


async def test_an_agent_without_code_execution_drops_originals(plain_doc: dict[str, Any]) -> None:
    async with run_app(plain_doc) as running:
        original = base64.b64encode(b"%PDF").decode()
        response, sse = await send(running, [document("a.pdf", original=original)])
        assert response.status_code == 200, response.text
        stored = await running.runtime.store.uploads.read(sid_of(sse))
        assert [u.kind for u in stored] == ["text"]


async def test_workspace_files_are_listed_and_refused_by_format() -> None:
    async with fake_sandbox() as platform:
        settings = Settings(sandbox_url=platform.url)
        model = FileToolModel(model_name="stub-sandbox")
        async with run_app(code_app_doc(), settings=settings, model=model) as running:
            response, sse = await chat(running, tool_message("execute", command="echo 만든다"))
            sid = sid_of(sse, "done")
            platform.pod.files.update(
                {"/workspace/chart.png": b"\x89PNG\r\n", "/workspace/tool.bin": b"MZ\x90\x00"}
            )
            listed = await running.client.get(
                f"/sessions/{sid}/files", params={"user_sub": "alice"}
            )
            rows = {f["path"]: f["origin"] for f in listed.json()["files"]}
            assert rows == {"/workspace/chart.png": "workspace", "/workspace/tool.bin": "workspace"}
            ok = await running.client.get(
                f"/sessions/{sid}/files/workspace/chart.png", params={"user_sub": "alice"}
            )
            assert ok.status_code == 200 and ok.content == b"\x89PNG\r\n"
            assert ok.headers["content-disposition"].startswith("attachment;")
            refused = await running.client.get(
                f"/sessions/{sid}/files/workspace/tool.bin", params={"user_sub": "alice"}
            )
            assert status_and_code(refused) == (403, "file_refused_executable")
            missing = await running.client.get(
                f"/sessions/{sid}/files/workspace/nope.txt", params={"user_sub": "alice"}
            )
            assert missing.status_code == 404


# --- 내려받기 거절의 경계, 세션 파일의 열기와 닫힘 ---------------------------------------------


def plain_tar() -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        info = tarfile.TarInfo("readme.txt")
        info.size = 2
        archive.addfile(info, io.BytesIO(b"hi"))
    return out.getvalue()


def mz_tar() -> bytes:
    """첫 항목 이름이 `MZ` 라 PE 매직으로 시작하는 tar 다. 뒤에 실행 이미지가 붙은 PE·tar 둘
    겹이다."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        info = tarfile.TarInfo("MZ")
        info.size = 5
        archive.addfile(info, io.BytesIO(b"hello"))
    return out.getvalue() + b"\x00" * 64 + b"PE\x00\x00 rest of an executable image"


@pytest.mark.parametrize("name", ["x.tar.exe", "x.tar.lnk", "x.tar.url", "x.tar.docm"])
def test_a_refused_extension_wins_over_a_tar_body(name: str) -> None:
    """끝 확장자를 먼저 본다. tar 의 속이 깨끗해도 거절한다."""
    assert refused_download(name, plain_tar())


@pytest.mark.parametrize("name", ["x.exe", "x.tar", "x"])
def test_executable_magic_wins_over_a_tar_body(name: str) -> None:
    assert mz_tar()[:2] == b"MZ"
    assert refused_download(name, mz_tar())


def gz_tar_bomb(megabytes: int) -> bytes:
    """크기가 큰 0 바이트 항목 하나의 gzip tar 다. 압축은 작고 풀면 megabytes MiB 다."""
    info = tarfile.TarInfo("a.txt")
    info.size = megabytes << 20
    compressor = zlib.compressobj(9, zlib.DEFLATED, 31)
    out = [compressor.compress(info.tobuf(format=tarfile.USTAR_FORMAT))]
    chunk = b"\x00" * (1 << 20)
    out.extend(compressor.compress(chunk) for _ in range(megabytes))
    out.append(compressor.compress(b"\x00" * 1024))
    out.append(compressor.flush())
    return b"".join(out)


def test_an_archive_bomb_is_refused_within_the_read_cap() -> None:
    """푼 바이트가 64MiB 를 넘으면 거절하고 그 뒤를 풀지 않는다. bz2 tar 도 같은 길이다."""
    bomb = gz_tar_bomb(sb_downloads.MAX_ARCHIVE_READ_BYTES // (1 << 20) + 16)
    assert len(bomb) < 1 << 20
    started = time.monotonic()
    assert refused_download("x.tar.gz", bomb)
    assert time.monotonic() - started < 5
    small = io.BytesIO()
    with tarfile.open(fileobj=small, mode="w:bz2") as archive:
        info = tarfile.TarInfo("a.csv")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"a"))
    assert not refused_download("x.tar.bz2", small.getvalue())


def test_a_zip_with_too_much_to_read_is_refused() -> None:
    """zip 도 항목마다 읽는 머리의 합이 상한을 넘으면 거절한다."""
    per = sb_downloads.ENTRY_HEAD_BYTES
    count = sb_downloads.MAX_ARCHIVE_READ_BYTES // per + 2
    assert count <= sb_downloads.MAX_ARCHIVE_ENTRIES
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for i in range(count):
            archive.writestr(f"f{i}.txt", b"\x00" * per)
    assert refused_download("x.zip", out.getvalue())


def test_the_saved_name_is_what_is_refused() -> None:
    for path in ('/workspace/x.ur"l', "/workspace/x.url\n", "/workspace/x.u\rrl"):
        assert download_name(path) == "x.url"
        assert refused_download(download_name(path), b"[x]")


def test_an_rtf_object_is_found_anywhere_in_the_body() -> None:
    """RTF 의 개체는 머리 창 밖에도 있고 줄바꿈이 제어어를 끊어도 찾는다."""
    assert refused_download("x.rtf", b"{\\rtf1 " + b"a" * 0x9100 + b"{\\object\\objemb}}")
    assert refused_download("x.rtf", b"{\\rtf1 " + b"a" * 10 + b"{\\ob\r\nject}}")
    assert not refused_download("x.rtf", b"{\\rtf1 plain text}")


async def test_the_refusal_sees_the_saved_name_and_runs_off_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """저장될 이름(`download_name`)으로 거절하고, 압축을 풀 수 있어 이벤트 루프 밖에서
    본다."""
    from runtime import session_files

    threads: list[int] = []
    real = session_files.refused_download

    def spy(name: str, content: bytes) -> bool:
        threads.append(threading.get_ident())
        return real(name, content)

    monkeypatch.setattr(session_files, "refused_download", spy)
    async with fake_sandbox() as platform:
        settings = Settings(sandbox_url=platform.url)
        model = FileToolModel(model_name="stub-sandbox")
        async with run_app(code_app_doc(), settings=settings, model=model) as running:
            _, sse = await chat(running, tool_message("execute", command="echo 만든다"))
            sid = sid_of(sse, "done")
            # 저장 이름은 따옴표를 뗀 `x.url` 이다. 줄바꿈이 든 경로는 라우트에 닿지 않는다
            platform.pod.files['/workspace/x.ur"l'] = b"[x]"
            refused = await running.client.get(
                f"/sessions/{sid}/files/workspace/x.ur%22l", params={"user_sub": "alice"}
            )
            assert status_and_code(refused) == (403, "file_refused_executable")
    assert threads and threading.get_ident() not in threads


async def test_an_open_404_is_told_once_then_a_new_generation_opens() -> None:
    """함께 열던 열기가 실패해 닫은 기록의 404 는 한 번 알리고, 다음 호출의 찾기가 같은 닫힘을 다시
    알리지 않고 연다."""
    async with fake_sandbox() as platform:
        platform.open["s"] = SandboxRecord("s", 1, "sbx-s-1", ready=False)
        detail = {"code": "sandbox_not_found", "detail": "x", "closed_reason": "verdict_void"}
        platform.refusal = (404, detail)
        boxes = sandboxes_for(platform)
        with pytest.raises(SandboxProblem) as e:
            await boxes.ensure("s", deadline=None)
        assert "verdict_void" in e.value.message
        platform.close_record("s", "verdict_void")
        ref = await boxes.ensure("s", deadline=None)
        assert ref.generation == 1
        await boxes.aclose()


async def test_the_command_clamp_counts_the_open_and_the_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """명령의 시한은 열기와 넣기 뒤에 남은 시간이다. 느린 당기기 뒤에도 명령의 시한으로 멈춘다(검토
    8)."""
    async with fake_sandbox() as platform:
        boxes = sandboxes_for(platform)
        real = boxes._sync

        async def slow(*args: Any, **kwargs: Any) -> None:
            await asyncio.sleep(1.5)
            await real(*args, **kwargs)

        monkeypatch.setattr(boxes, "_sync", slow)
        await boxes.execute("s", "echo hi", timeout=None, deadline=time.monotonic() + 3)
        assert platform.pod.executed[0]["timeout"] <= 1.6
        await boxes.aclose()


def test_a_lost_workspace_does_not_repeat_its_reason() -> None:
    message = sb.gone_message("lost")
    assert message is not None and "(lost)" not in message
    assert "(verdict_void)" in (sb.gone_message("verdict_void") or "")


def test_the_token_stays_out_of_the_repr() -> None:
    ref = sb.SandboxRef(sandbox_id="sbx", generation=1, host="h", port=1, token="secret-token")
    assert "secret-token" not in repr(ref)


async def test_skills_are_read_off_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "main").mkdir()
    (tmp_path / "main" / "SKILL.md").write_text("# s")
    threads: list[int] = []
    real = sb.skill_files

    def spy(root: Path) -> list[tuple[str, bytes]]:
        threads.append(threading.get_ident())
        return real(root)

    monkeypatch.setattr(sb, "skill_files", spy)
    async with fake_sandbox() as platform:
        boxes = sandboxes_for(platform, skills_root=tmp_path)
        await boxes.execute("s", "echo hi", timeout=None, deadline=None)
        assert threads and threading.get_ident() not in threads
        await boxes.aclose()


async def test_a_session_file_read_does_not_use_up_the_models_report() -> None:
    """세션 파일 읽기의 끊김은 모델에게 한 번만 알릴 닫힘을 쓰지 않는다. 다음 그래프 호출이 그
    닫힘을 듣는다."""
    async with fake_sandbox() as platform:
        boxes = sandboxes_for(platform)
        platform.open["s"] = SandboxRecord("s", 1, "sbx-s-1")
        platform.generations["s"] = 1
        platform.pod.wrong_proof = True
        platform.pod.on_connect = lambda: platform.close_record("s", "idle")
        assert await boxes.read_file("s", "/workspace/a.txt") is None
        platform.pod.wrong_proof, platform.pod.on_connect = False, None
        with pytest.raises(SandboxProblem) as e:
            await boxes.ensure("s", deadline=None)
        assert e.value.message.startswith(sb.GONE_MESSAGES["idle"])
        await boxes.aclose()


async def test_a_platform_dropped_original_gets_the_note() -> None:
    """플랫폼이 한 턴의 원본 상한으로 싣지 않은 원본은 안내 줄이 글만 남았다고 말한다."""
    async with fake_sandbox() as platform:
        settings = Settings(sandbox_url=platform.url)
        model = FileToolModel(model_name="stub-sandbox")
        async with run_app(code_app_doc(), settings=settings, model=model) as running:
            response, sse = await send(running, [document("a.pdf", original_dropped=True)])
            assert response.status_code == 200, response.text
            sid = sid_of(sse)
            human = (await checkpoint_humans(running, sid))[0].text
            assert "the original file was not kept" in human
            stored = await running.runtime.store.uploads.read(sid)
            assert [u.kind for u in stored] == ["text"]


def test_code_execution_skills_may_run_scripts(tmp_path: Path) -> None:
    """코드 실행 Agent 의 Skill 문구는 스크립트를 execute 로 돌릴 수 있다고 말하고, 아니면 글로만
    본다."""
    from runtime import skills as skills_module

    off = skills_module.AgentSkills(root=tmp_path, main="/skills/main/")
    on = skills_module.AgentSkills(root=tmp_path, main="/skills/main/", executes=True)
    backend = StateBackend()
    prompt_off = off.parts(backend).middleware[0].system_prompt_template  # type: ignore[attr-defined]
    prompt_on = on.parts(backend).middleware[0].system_prompt_template  # type: ignore[attr-defined]
    assert "You cannot run scripts" in prompt_off
    assert "You cannot run scripts" not in prompt_on and "execute" in prompt_on
