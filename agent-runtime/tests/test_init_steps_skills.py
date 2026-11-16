"""기능 `skills` 의 init 단계다(P39 7절, runtime/init_steps/skills.py).

가짜 `fetch` 로 `run(config_dir, fetch)` 를 불러 본다. Skill 을 하나씩 받아 바로 쓰는 것, 붙인
에이전트마다 따로 쓰는 것, 파일 0444·디렉터리 0555 와 남는 임시 디렉터리가 없는 것, 반쯤 쓰고 실패한
뒤 다시 불러도 같은 결과인 것(잠긴 디렉터리와 임시 디렉터리가 남은 자리), digest 어긋남, 경로와 응답
모양의 거절, 깨진 agent.json, 연결 실패·읽다 끊긴 본문·5xx 의 재시도와 그 밖 오류의 즉시 실패,
쓰기 실패, 단계마다의 termination log 코드, 표준 라이브러리 밖 모듈을 올리지 않는 것을 본다.
agent.json 은 플랫폼과 함께 쓰는 표본(`schema/fixtures/skills-agent.json`)이고, digest 는 같은
고정물(`schema/fixtures/skills`)의 값과 같아야 한다.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from http.client import InvalidURL
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

from runtime.init_steps import skills as step
from tests.conftest import load_fixture
from tests.skill_fixtures import (
    SKILL_FIXTURES,
    CutShort,
    FakeFetch,
    bundle_of,
    fingerprint,
    fixture_bundles,
    write_agent_json,
)

SKILLS_AGENT = "skills-agent.json"
"""플랫폼 테스트(platform/tests/test_agent_schema.py)와 함께 쓰는 agent.json 표본이다."""
RUNTIME_DIR = Path(__file__).resolve().parent.parent


def skills_doc(*, main: list[str], sub: list[str]) -> dict[str, Any]:
    """표본 agent.json 에서 붙인 이름만 바꾼 문서다. 핀은 고정물 폴더 report-style·web-research 를
    가리키고 서브에이전트는 하나다."""
    doc = load_fixture(SKILLS_AGENT)
    doc["main"]["skills"] = main
    doc["subagents"][0]["skills"] = sub
    return doc


def http_error(code: int, detail_code: str | None = None) -> HTTPError:
    body = {"detail": {"code": detail_code, "detail": "플랫폼이 거절했다"}} if detail_code else {}
    return HTTPError("http://platform/x", code, "error", {}, io.BytesIO(json.dumps(body).encode()))


@pytest.fixture
def termination(monkeypatch: pytest.MonkeyPatch, termination_log: Path) -> Path:
    """init 단계도 기동 실패의 이유를 conftest 의 임시 파일(termination_log)에 쓰게 한다."""
    monkeypatch.setattr(step, "TERMINATION_LOG", str(termination_log))
    return termination_log


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """재시도가 쉰 초다. 실제로는 쉬지 않는다."""
    slept: list[float] = []
    monkeypatch.setattr(step.time, "sleep", slept.append)
    return slept


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    path = tmp_path / "config"
    path.mkdir()
    return path


def prepare(config_dir: Path, *, main: list[str], sub: list[str]) -> FakeFetch:
    write_agent_json(config_dir, skills_doc(main=main, sub=sub))
    return FakeFetch(config_dir, {b["name"]: b for b in fixture_bundles()})


def stops_with(termination: Path, action: Callable[[], None]) -> str:
    """action 이 0 이 아닌 값으로 끝나는지 보고 termination log 의 코드를 준다."""
    with pytest.raises(SystemExit) as info:
        action()
    assert info.value.code == 1
    return termination.read_text(encoding="utf-8").split(":", 1)[0]


# --- digest ---------------------------------------------------------------------------------


def test_digest_matches_the_shared_fixtures() -> None:
    """플랫폼이 같은 고정물로 같은 값을 낸다(platform/tests/test_skill_format.py)."""
    expected = load_fixture("skills/digests.json")
    got = {name: bundle_of(name)["digest"] for name in expected}
    assert got == expected


def test_digest_follows_the_schema_rule() -> None:
    files = [("b.md", "둘"), ("SKILL.md", "하나")]
    body = b"".join(
        f"{p}\0{hashlib.sha256(c.encode()).hexdigest()}\n".encode() for p, c in sorted(files)
    )
    assert step.skill_digest(files) == f"sha256:{hashlib.sha256(body).hexdigest()}"
    assert step.skill_digest(reversed(files)) == step.skill_digest(files)


# --- 설치 -----------------------------------------------------------------------------------


def test_each_skill_is_fetched_once_and_written_for_every_agent(config_dir: Path) -> None:
    """Skill 마다 한 번 받아 붙인 에이전트마다 쓴다. 다음 Skill 을 받을 때는 앞 Skill 이 이미
    임시 디렉터리에 있다(한 번에 하나만 메모리에 둔다)."""
    fetch = prepare(config_dir, main=["report-style", "web-research"], sub=["web-research"])
    step.run(str(config_dir), fetch)
    root = config_dir / step.SKILLS_DIR
    assert fetch.calls == ["/skills/report-style", "/skills/web-research"]
    assert fetch.staged[0] == []
    assert fetch.staged[1] == ["main/report-style/SKILL.md", "main/report-style/references/tone.md"]
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    assert files == [
        "main/report-style/SKILL.md",
        "main/report-style/references/tone.md",
        "main/web-research/SKILL.md",
        "main/web-research/scripts/collect.py",
        "sub-0/web-research/SKILL.md",
        "sub-0/web-research/scripts/collect.py",
    ]
    original = SKILL_FIXTURES / "report-style" / "SKILL.md"
    assert (root / "main/report-style/SKILL.md").read_bytes() == original.read_bytes()


def test_the_tree_is_locked_and_no_staging_is_left(config_dir: Path) -> None:
    """붙인 에이전트가 없는 Skill(web-research)은 받지 않는다. 받은 트리는 잠기고 임시
    디렉터리가 남지 않는다."""
    fetch = prepare(config_dir, main=["report-style"], sub=[])
    step.run(str(config_dir), fetch)
    root = config_dir / step.SKILLS_DIR
    assert fetch.calls == ["/skills/report-style"]
    assert not (root / "main" / "web-research").exists()
    modes = {(p.is_dir(), stat.S_IMODE(p.stat().st_mode)) for p in [root, *root.rglob("*")]}
    assert modes == {(True, step.DIR_MODE), (False, step.FILE_MODE)}
    assert list(config_dir.glob(f"{step.STAGING_PREFIX}*")) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root 는 권한 비트를 지나므로 잠금을 볼 수 없다")
def test_rerun_after_a_failure_gives_the_same_tree(config_dir: Path, termination: Path) -> None:
    """반쯤 쓰고 실패한 시도와 잠긴 채 남은 디렉터리 위에서 다시 불러도 처음과 같은 트리다."""
    main, sub = ["report-style", "web-research"], ["web-research"]
    step.run(str(config_dir), prepare(config_dir, main=main, sub=sub))
    clean = fingerprint(config_dir / step.SKILLS_DIR)

    failing = prepare(config_dir, main=main, sub=sub)
    failing.failures["web-research"] = [http_error(404, "skill_not_found")]
    assert stops_with(termination, lambda: step.run(str(config_dir), failing)) == "skill_not_found"
    # 지난 트리는 지워졌고 반쯤 쓴 임시 디렉터리가 남았다. 잠긴 것도 하나 더 둔다
    assert not (config_dir / step.SKILLS_DIR).exists()
    assert len(list(config_dir.glob(f"{step.STAGING_PREFIX}*"))) == 1
    locked = config_dir / f"{step.STAGING_PREFIX}locked" / "main" / "half"
    locked.mkdir(parents=True)
    (locked / "SKILL.md").write_text("반쯤", encoding="utf-8")
    step._lock(config_dir / f"{step.STAGING_PREFIX}locked")

    step.run(str(config_dir), prepare(config_dir, main=main, sub=sub))
    assert fingerprint(config_dir / step.SKILLS_DIR) == clean
    assert list(config_dir.glob(f"{step.STAGING_PREFIX}*")) == []


# --- 거절 -----------------------------------------------------------------------------------


def test_a_digest_mismatch_stops(config_dir: Path, termination: Path) -> None:
    fetch = prepare(config_dir, main=["report-style"], sub=[])
    fetch.bodies["report-style"]["files"][0]["content"] += "덧붙인 줄\n"
    assert stops_with(termination, lambda: step.run(str(config_dir), fetch)) == (
        step.DIGEST_MISMATCH
    )


@pytest.mark.parametrize(
    "path",
    ["../escape.md", "/abs.md", "a//b.md", ".hidden", "a\\b.md", "a/b/c/d/e.md", "a" * 101],
)
def test_paths_outside_the_schema_rule_are_refused(
    config_dir: Path, termination: Path, path: str
) -> None:
    fetch = prepare(config_dir, main=["report-style"], sub=[])
    fetch.bodies["report-style"]["files"].append({"path": path, "content": "x"})
    assert stops_with(termination, lambda: step.run(str(config_dir), fetch)) == step.PATH_INVALID
    assert not (config_dir / step.SKILLS_DIR).exists()


@pytest.mark.parametrize(
    "change",
    [
        lambda b: b.update(extra=1),
        lambda b: b.pop("digest"),
        lambda b: b.update(name="other"),
        lambda b: b.update(revision=9),
        lambda b: b.update(files=[]),
        lambda b: b["files"][0].update(content=None),
        lambda b: b["files"][0].update(content="\ud800"),
        lambda b: b["files"][0].update(mode="0777"),
    ],
    ids=[
        "extra_key",
        "missing_key",
        "name",
        "revision",
        "no_files",
        "not_text",
        "lone_surrogate",
        "file_key",
    ],
)
def test_bundles_of_another_shape_are_refused(
    config_dir: Path, termination: Path, change: Callable[[dict[str, Any]], Any]
) -> None:
    fetch = prepare(config_dir, main=["report-style"], sub=[])
    change(fetch.bodies["report-style"])
    assert stops_with(termination, lambda: step.run(str(config_dir), fetch)) == (
        step.BUNDLE_INVALID
    )


def test_a_body_that_is_not_json_or_too_large_is_refused(
    config_dir: Path, termination: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = prepare(config_dir, main=["report-style"], sub=[])
    fetch.bodies["report-style"] = b"<html>"
    assert stops_with(termination, lambda: step.run(str(config_dir), fetch)) == (
        step.BUNDLE_INVALID
    )
    monkeypatch.setattr(step, "MAX_BUNDLE_BYTES", 16)
    fetch.bodies["report-style"] = bundle_of("report-style")
    assert stops_with(termination, lambda: step.run(str(config_dir), fetch)) == (
        step.BUNDLE_INVALID
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.pop("main"),
        lambda d: d.update(skills=5),
        lambda d: d["skills"].append("report-style"),
        lambda d: d["skills"][0].pop("digest"),
        lambda d: d["skills"][0].update(extra=1),
        lambda d: d["skills"][0].update(name="report-style\n"),
        lambda d: d["skills"][0].update(name="../report-style"),
    ],
    ids=[
        "no_main",
        "skills_not_a_list",
        "pin_not_an_object",
        "pin_missing_key",
        "pin_extra_key",
        "name_with_newline",
        "name_with_slash",
    ],
)
def test_a_broken_agent_json_stops_before_fetching(
    config_dir: Path, termination: Path, change: Callable[[dict[str, Any]], Any]
) -> None:
    """핀의 이름은 디렉터리 이름과 받기 주소가 되므로 `$defs/skill_name` 으로 다시 본다."""
    fetch = prepare(config_dir, main=["report-style"], sub=[])
    doc = skills_doc(main=["report-style"], sub=[])
    change(doc)
    write_agent_json(config_dir, doc)
    assert stops_with(termination, lambda: step.run(str(config_dir), fetch)) == (
        step.BUNDLE_INVALID
    )
    assert fetch.calls == []


def test_an_unreadable_agent_json_stops(config_dir: Path, termination: Path) -> None:
    fetch = prepare(config_dir, main=["report-style"], sub=[])
    (config_dir / step.CONFIG_FILE).write_text("{", encoding="utf-8")
    assert stops_with(termination, lambda: step.run(str(config_dir), fetch)) == (
        step.BUNDLE_INVALID
    )
    (config_dir / step.CONFIG_FILE).unlink()
    assert stops_with(termination, lambda: step.run(str(config_dir), fetch)) == (
        step.BUNDLE_INVALID
    )


def test_a_write_failure_stops_with_its_own_code(
    config_dir: Path, termination: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(skill_dir: Path, files: list[tuple[str, str]]) -> None:
        raise PermissionError(13, "Permission denied", str(skill_dir))

    monkeypatch.setattr(step, "_write", refuse)
    fetch = prepare(config_dir, main=["report-style"], sub=[])
    assert stops_with(termination, lambda: step.run(str(config_dir), fetch)) == (step.WRITE_FAILED)


# --- 재시도 ---------------------------------------------------------------------------------


def test_connection_failures_and_5xx_are_retried(config_dir: Path, sleeps: list[float]) -> None:
    fetch = prepare(config_dir, main=["report-style"], sub=[])
    fetch.failures["report-style"] = [URLError("refused"), http_error(503), CutShort()]
    step.run(str(config_dir), fetch)
    assert sleeps == list(step.RETRY_DELAYS) == [1.0, 2.0, 4.0]
    assert len(fetch.calls) == 4
    assert (config_dir / step.SKILLS_DIR / "main/report-style/SKILL.md").is_file()


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (URLError("refused"), step.UNREACHABLE),
        (CutShort(), step.UNREACHABLE),
        (http_error(502), "http_502"),
    ],
    ids=["connection", "cut_short", "server_error"],
)
def test_retries_run_out(
    config_dir: Path,
    termination: Path,
    sleeps: list[float],
    failure: Exception,
    code: str,
) -> None:
    fetch = prepare(config_dir, main=["report-style"], sub=[])
    fetch.failures["report-style"] = [failure] * 4
    assert stops_with(termination, lambda: step.run(str(config_dir), fetch)) == code
    assert len(fetch.calls) == 4 and len(sleeps) == 3


def test_client_errors_stop_at_once_with_the_platform_code(
    config_dir: Path, termination: Path, sleeps: list[float]
) -> None:
    fetch = prepare(config_dir, main=["report-style"], sub=[])
    fetch.failures["report-style"] = [http_error(401, "invalid_instance_token")]
    code = stops_with(termination, lambda: step.run(str(config_dir), fetch))
    assert code == "invalid_instance_token"
    assert len(fetch.calls) == 1 and sleeps == []
    assert "플랫폼이 거절했다" in termination.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "failure",
    [ValueError("unknown url type: 'platform'"), InvalidURL("control characters")],
    ids=["urllib", "http_client"],
)
def test_a_bad_address_stops_at_once(
    config_dir: Path, termination: Path, sleeps: list[float], failure: Exception
) -> None:
    """주소를 만들지 못한 것은 다시 해도 같으므로 받기 쪽 코드로 바로 멈춘다."""
    fetch = prepare(config_dir, main=["report-style"], sub=[])
    fetch.failures["report-style"] = [failure]
    assert stops_with(termination, lambda: step.run(str(config_dir), fetch)) == step.UNREACHABLE
    assert len(fetch.calls) == 1 and sleeps == []


@pytest.mark.parametrize(
    "body",
    [CutShort(), io.BytesIO(b'{"detail": "Not Found"}')],
    ids=["cut_short", "not_the_platform_shape"],
)
def test_an_unreadable_error_body_keeps_the_status(
    config_dir: Path, termination: Path, body: Any
) -> None:
    fetch = prepare(config_dir, main=["report-style"], sub=[])
    fetch.failures["report-style"] = [HTTPError("http://platform/x", 404, "Not Found", {}, body)]
    assert stops_with(termination, lambda: step.run(str(config_dir), fetch)) == "http_404"


# --- 표준 라이브러리 ------------------------------------------------------------------------


def test_the_module_imports_only_the_standard_library() -> None:
    """init 은 런타임 이미지의 python 으로 돌지만 langchain 을 올릴 까닭이 없다. 자원 한도
    (요청 16Mi, 한도 64Mi)를 지키는 것도 이것이다."""
    script = (
        "import json, sys\n"
        "before = set(sys.modules)\n"
        "import runtime.init_steps.skills\n"
        "added = {m.split('.')[0] for m in set(sys.modules) - before}\n"
        "print(json.dumps(sorted(added - set(sys.stdlib_module_names) - {'runtime'})))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=RUNTIME_DIR,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == []
