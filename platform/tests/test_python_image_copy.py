"""플랫폼, MCP 런타임, Agent 런타임, 웹 Tool 이미지가 같은 CPython 을 같은 방법으로 싣는지 본다
(D55, D70).

네 Dockerfile 은 빌드 컨텍스트가 따로라 단계를 나눠 쓰지 못한다. 그래서 독립 실행형 CPython 을 까는
빌드 단계의 앞부분과 그것을 distroless 베이스로 옮기는 런타임 단계의 앞부분을 네 벌 둔다. CPython 과
그 안에 묶인 OpenSSL·SQLite·zlib·expat 의 보안 판은 빌드 단계의 uv 핀과 CPYTHON_VERSION 을 올려야
들어오고, 이미지 스캔은 /opt/python 을 보지 못한다. 그래서 make image-report 는 핀으로
security/cpython-builds.json 에서 판을 찾아 공개 취약점을 본다(P58). 한 파일만 고치면 나머지
이미지가 옛 판을 싣고도 그 판정이 틀리므로 여기서 막는다. 한 파일을 고쳤으면 나머지 세 파일에 그대로
복사하고, 핀을 올렸으면 make cpython-record 로 기록을 쓴다. 기록에 지금 핀의 항목이 없으면 아래
테스트가 실패한다. 개발용 mock-oidc 는 패키지 관리자가 있는 python:3.12-slim-trixie 에서 돌아
굽는 방법이 달라 맞대지 않는다. 코드 실행 작업 공간(sandbox, P40)은 명령에 셸이 있어야 해서 마지막
단계가 Debian slim 이므로 빌드 단계만 맞대고, 그 CPython 과 venv 를 그대로 옮기는지 본다."""

from __future__ import annotations

from pathlib import Path

import pytest
from cpython_builds import JUDGED, PLATFORMS, dockerfile_pins, load_builds

from tests.helpers.consts import REPO
from tests.helpers.source import SANDBOX_IMAGE, read_source

PLATFORM = REPO / "platform" / "Dockerfile"
RUNTIMES = (
    REPO / "mcp-runtime" / "Dockerfile",
    REPO / "agent-runtime" / "Dockerfile",
    REPO / "web-tools" / "Dockerfile",
)
# 런타임 단계의 앞부분은 venv 를 복사하는 이 줄에서 끝난다. 그 뒤의 소스와 환경은 이미지마다 다르다
VENV_COPY = "COPY --from=build /app/.venv /app/.venv"
# 맞대는 줄이 품어야 하는 판 핀. 핀이 그 범위 밖으로 옮겨 가면 맞대기가 핀을 지키지 못한다
PINS = ("uv==", "ARG CPYTHON_VERSION=")
# 핀마다의 CPython 빌드 판 (P58). make cpython-record 가 쓴다
BUILDS = REPO / "security" / "cpython-builds.json"


def _shared_lines(path: Path) -> list[str]:
    """네 벌이 같아야 하는 줄이다. 빌드 단계의 첫 FROM 부터 프로젝트 파일을 복사하기 전까지와
    런타임 단계의 마지막 FROM 부터 venv 를 복사하는 줄까지다."""
    lines = read_source(path).splitlines()
    froms = [i for i, line in enumerate(lines) if line.startswith("FROM ")]
    first_copy = next(i for i, line in enumerate(lines) if line.startswith("COPY "))
    return lines[froms[0] : first_copy] + lines[froms[-1] : lines.index(VENV_COPY) + 1]


def test_the_shared_lines_hold_the_uv_and_cpython_pins() -> None:
    shared = _shared_lines(PLATFORM)
    missing = [pin for pin in PINS if not any(pin in line for line in shared)]
    assert not missing, (
        f"{PLATFORM.relative_to(REPO)} 의 맞대는 줄에 {missing} 가 없다. 맞대는 범위를 다시 잡는다"
    )


@pytest.mark.parametrize("dockerfile", RUNTIMES, ids=lambda path: path.parent.name)
def test_runtime_images_bake_cpython_like_the_platform(dockerfile: Path) -> None:
    assert _shared_lines(dockerfile) == _shared_lines(PLATFORM), (
        f"{dockerfile.relative_to(REPO)} 의 CPython 단계가 platform/Dockerfile 과 다르다. "
        "한 파일에서 고친 것을 나머지 세 Dockerfile 에 그대로 복사한다"
    )


def _build_stage(path: Path) -> list[str]:
    lines = read_source(path).splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith("FROM "))
    first_copy = next(i for i, line in enumerate(lines) if line.startswith("COPY "))
    return lines[first:first_copy]


def test_the_sandbox_bakes_cpython_like_the_platform() -> None:
    sandbox = SANDBOX_IMAGE / "Dockerfile"
    assert _build_stage(sandbox) == _build_stage(PLATFORM), (
        f"{sandbox.relative_to(REPO)} 의 CPython 빌드 단계가 platform/Dockerfile 과 다르다. "
        "한 파일에서 고친 것을 그대로 복사한다"
    )
    lines = read_source(sandbox).splitlines()
    last = max(i for i, line in enumerate(lines) if line.startswith("FROM "))
    tail = lines[last:]
    assert "COPY --from=build /opt/python /opt/python" in tail
    assert VENV_COPY in tail
    # 명령이 돌릴 수 있는 setuid·setgid 프로그램을 남기지 않는다
    assert any("-perm /6000 -exec chmod ug-s" in line for line in tail)


def test_the_build_record_follows_the_pins() -> None:
    """uv 핀이나 CPYTHON_VERSION 을 올리고 기록을 쓰지 않으면 make image-report 가 그 이미지의 판을
    모른다."""
    pins = dockerfile_pins(read_source(PLATFORM))
    assert pins is not None
    builds = load_builds(str(BUILDS))
    missing = [platform for platform in PLATFORMS if (*pins, platform) not in builds]
    assert not missing, (
        f"{BUILDS.relative_to(REPO)} 에 uv {pins[0]}·CPython {pins[1]} 의 {missing} 항목이 없다. "
        "make cpython-record 로 쓴다"
    )
    for platform in PLATFORMS:
        versions = builds[(*pins, platform)]["versions"]
        assert set(JUDGED) <= set(versions), platform
        assert versions["cpython"] == pins[1]
