"""PR 의 pr-check 가 DB 시험 갈래를 고르는 규칙(scripts/verify_changed.sh store-lanes)을 본다.

갈래를 잘못 고르면 그 시험이 로컬에서도 PR 에서도 돌지 않은 채 통과로 보인다. 그래서 문서만,
UI 만, 런타임만, 스키마, .github, 플랫폼 밖으로 옮긴 파일, 64KiB 를 넘는 목록(파이프였다면
SIGPIPE 로 판정이 뒤집히던 길)에서 세 줄의 답을 본다. 러너의 bash 5 와 macOS 의 /bin/bash 3.2
에서 함께 돈다."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.helpers.consts import REPO

SCRIPT = REPO / "scripts" / "verify_changed.sh"
BASH = "/bin/bash" if Path("/bin/bash").exists() else "bash"


def lanes(*files: str) -> dict[str, str]:
    done = subprocess.run(
        [BASH, str(SCRIPT), "store-lanes"],
        input="".join(f"{f}\n" for f in files),
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO,
    )
    return dict(line.split("=", 1) for line in done.stdout.splitlines())


def make_targets(*files: str) -> list[str]:
    done = subprocess.run(
        [BASH, str(SCRIPT), "lanes"],
        input="".join(f"{f}\n" for f in files),
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO,
    )
    return done.stdout.split()


def runtime_link_tests() -> list[str]:
    done = subprocess.run(
        [BASH, str(SCRIPT), "runtime-link-tests"], capture_output=True, text=True, check=True
    )
    return done.stdout.split()


@pytest.mark.parametrize(
    "files",
    [
        (),
        ("docs/guide/local-dev.md",),
        ("README.md", ".claude/skills/example/SKILL.md"),
        ("ui/src/pages/Home.tsx",),
    ],
)
def test_documents_and_screens_run_no_store_lane(files):
    assert lanes(*files) == {
        "platform": "false",
        "platform_tests": "",
        "agent_runtime": "false",
        "sandbox": "false",
    }


@pytest.mark.parametrize(
    "files",
    [
        ("platform/daegam/main.py",),
        ("deploy/dev/values-local.yaml",),
        ("charts/daegam/values.yaml",),
        ("platform/daegam/services/assistant/knowledge/agents.md",),
        ("agent-runtime/schema/agent.schema.json",),
        ("web-tools/schema/x.json",),
    ],
)
def test_a_change_outside_the_light_lanes_runs_the_whole_platform_suite(files):
    got = lanes(*files)
    assert got["platform"] == "true"
    assert got["platform_tests"] == ""


def test_a_runtime_change_runs_only_the_runtime_link_selection_with_mongodb():
    selection = " ".join(runtime_link_tests())
    for files in (("mcp-runtime/runtime/main.py",), ("web-tools/webtools/fetch.py",)):
        assert lanes(*files) == {
            "platform": "true",
            "platform_tests": selection,
            "agent_runtime": "false",
            "sandbox": "false",
        }
    # 샌드박스 서버도 런타임 갈래이고, 리눅스 전용 시험 갈래가 더 돈다 (P40 F1)
    assert lanes("sandbox/daegam_sandbox/server.py") == {
        "platform": "true",
        "platform_tests": selection,
        "agent_runtime": "false",
        "sandbox": "true",
    }
    # 제품 Dockerfile 을 읽는 시험이 그 선택에 든다
    assert "tests/test_python_image_copy.py" in selection.split()
    assert "tests/test_base_pins.py" in selection.split()


def test_the_runtime_link_selection_names_existing_test_files():
    tests = runtime_link_tests()
    assert "tests/test_doc_citations.py" in tests
    assert len(tests) > 5
    assert all((REPO / "platform" / t).is_file() for t in tests)


@pytest.mark.parametrize(
    "files",
    [
        ("agent-runtime/runtime/turn.py",),
        ("Makefile",),
        (".github/workflows/agent-runtime.yml",),
        (".github/workflows/pr-check.yml",),
    ],
)
def test_agent_runtime_store_tests_follow_their_own_inputs(files):
    assert lanes(*files)["agent_runtime"] == "true"


def test_a_platform_only_change_does_not_run_the_agent_runtime_store_tests():
    got = lanes("platform/daegam/main.py", "scripts/verify_changed.sh")
    assert (got["agent_runtime"], got["sandbox"]) == ("false", "false")


@pytest.mark.parametrize("path", [".github/workflows/sandbox.yml", "sandbox/uv.lock"])
def test_the_sandbox_lane_follows_its_own_inputs(path):
    assert lanes(path)["sandbox"] == "true"


def test_a_makefile_change_does_not_run_the_sandbox_lane():
    assert lanes("Makefile")["sandbox"] == "false"


def test_a_file_moved_out_of_platform_still_runs_the_platform_suite():
    # pr-check 는 옮긴 파일의 옛 이름(previous_filename)도 목록에 넣는다
    got = lanes("ui/src/moved.ts", "platform/daegam/moved.py")
    assert (got["platform"], got["platform_tests"]) == ("true", "")


def test_a_list_over_64_kib_keeps_its_agent_runtime_answer():
    generated = [f"ui/src/generated/file_{i:05d}.ts" for i in range(3000)]
    files = ["agent-runtime/runtime/x.py", *generated]
    assert sum(len(f) + 1 for f in files) > 64 * 1024
    assert lanes(*files)["agent_runtime"] == "true"


@pytest.mark.parametrize(
    ("files", "targets"),
    [
        (("ui/src/pages/Home.tsx",), ["verify-ui", "verify-ui-links"]),
        (("docs/guide/e2e.md",), ["verify-docs"]),
        # 비공개 측정 도구는 어느 갈래도 시험하지 않는다. 주소 검사만 받는다
        (("deploy/dev/lab/f0/p44/f03_sweep.py",), ["verify-docs"]),
        (("deploy/dev/lab/profile.json",), ["verify"]),
        # ops/ 는 운영자의 저장소에만 있고 그 갈래 verify-ops 는 ops/*/*.mk 가 둔다 (P85 9절)
        (("ops/site/deploy.sh",), ["verify-ops"]),
        (("ops/site/values.yaml", "docs/guide/e2e.md"), ["verify-docs", "verify-ops"]),
        # 차트를 바꾸면 verify 전부다. make verify 의 VERIFY_EXTRA 가 verify-ops 를 더한다
        (("ops/site/deploy.sh", "charts/daegam/values.yaml"), ["verify"]),
    ],
)
def test_the_lanes_follow_the_changed_paths(files, targets):
    assert make_targets(*files) == targets


def test_an_ops_change_runs_no_store_lane():
    assert lanes("ops/site/deploy.sh") == {
        "platform": "false",
        "platform_tests": "",
        "agent_runtime": "false",
        "sandbox": "false",
    }
