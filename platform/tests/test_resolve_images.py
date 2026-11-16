"""scripts/resolve-images.sh: 제품 이미지 여섯을 digest 로 고정하고 서명을 확인한 차트 값 파일
(P85 6절, D53, D66).

레지스트리는 이 컴퓨터의 평문 http 레지스트리, cosign 은 기록하는 대역이다
(tests/helpers/registry.py). 도장은 고정 도장을 찍는 트리에서 읽으므로 작업 트리에 커밋 안 된 변경이
있어도 같다.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.helpers.chart import render
from tests.helpers.registry import (
    IMAGES,
    STAMPS,
    checkout,
    docker_home,
    fake_registry,
    stand_in_bin,
)

# 차트의 이미지 값 이름 (charts/daegam/values.yaml 의 images.<이름>)
VALUE_KEYS = {
    "platform": "platform",
    "ui": "ui",
    "runtime": "runtime",
    "agent": "agent",
    "web-tools": "webTools",
    "sandbox": "sandbox",
}


def _run(tree: Path, home: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    base = {
        "PATH": f"{tree / 'bin'}:{os.environ['PATH']}",
        "HOME": str(home),
        "COSIGN_LOG": str(home / "cosign.log"),
        "COSIGN_PUB": str(tree / "cosign.pub"),
        "INSTALL_ENV_FILE": str(home / "install.env"),
    }
    return subprocess.run(
        ["bash", str(tree / "scripts" / "resolve-images.sh"), *args],
        env={**base, **env},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    t = checkout(tmp_path / "tree")
    stand_in_bin(t / "bin")
    return t


def test_every_product_image_is_pinned_and_its_signature_checked(tree, tmp_path):
    out = tmp_path / "images.json"
    with fake_registry() as reg:
        digests = reg.push_products("amd64", "arm64")
        docker_home(tmp_path, reg)
        run = _run(tree, tmp_path, "--registry", reg.registry, "--arch", "arm64", "--out", str(out))
    assert run.returncode == 0, run.stderr
    values = json.loads(out.read_text())["images"]
    assert values["registry"] == reg.registry
    for component, key in VALUE_KEYS.items():
        assert values[key] == {"tag": STAMPS[component], "digest": digests[component]}
    # 서명은 고정한 digest 마다, 그 이미지의 이름과 도장으로 확인한다
    log = (tmp_path / "cosign.log").read_text().splitlines()
    assert len(log) == len(IMAGES)
    for component, name in IMAGES.items():
        ref = f"{reg.registry}/{name}:{STAMPS[component]}@{digests[component]}"
        line = next(entry for entry in log if entry.endswith(ref))
        assert f"-a dev.daegam.image={name} -a dev.daegam.stamp={STAMPS[component]}" in line
        assert f"--key {tree / 'cosign.pub'}" in line


def test_the_values_file_pins_what_the_chart_renders(tree, tmp_path):
    """값 파일을 -f 로 주면 차트가 digest 로 렌더하고 IfNotPresent 로 당긴다."""
    out = tmp_path / "images.json"
    with fake_registry() as reg:
        digests = reg.push_products("arm64")
        docker_home(tmp_path, reg)
        assert (
            _run(
                tree, tmp_path, "--arch", "arm64", "--out", str(out), REGISTRY=reg.registry
            ).returncode
            == 0
        )
    docs = render(out)
    platform = next(
        d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "daegam-platform"
    )
    [container] = platform["spec"]["template"]["spec"]["containers"]
    want = f"{reg.registry}/daegam-platform:{STAMPS['platform']}@{digests['platform']}"
    assert container["image"] == want and container["imagePullPolicy"] == "IfNotPresent"


@pytest.mark.parametrize(
    ("arches", "arch", "reason"),
    [(("amd64",), "arm64", "arm64 없음"), (("arm64",), "amd64", "amd64 없음")],
)
def test_an_index_without_the_nodes_architecture_writes_nothing(
    tree, tmp_path, arches, arch, reason
):
    out = tmp_path / "images.json"
    with fake_registry() as reg:
        reg.push_products(*arches)
        docker_home(tmp_path, reg)
        run = _run(tree, tmp_path, "--registry", reg.registry, "--arch", arch, "--out", str(out))
    assert run.returncode != 0 and reason in run.stderr
    assert not out.exists()
    assert not (tmp_path / "cosign.log").exists(), "고정하지 못하면 서명을 보지 않는다"


def test_a_missing_tag_or_a_bad_signature_writes_nothing(tree, tmp_path):
    out = tmp_path / "images.json"
    with fake_registry() as reg:
        reg.push_products("arm64")
        del reg.manifests[("daegam-sandbox", STAMPS["sandbox"])]
        docker_home(tmp_path, reg)
        run = _run(tree, tmp_path, "--registry", reg.registry, "--arch", "arm64", "--out", str(out))
        assert run.returncode != 0 and f"daegam-sandbox:{STAMPS['sandbox']} (404)" in run.stderr
        reg.push_products("arm64")
        run = _run(
            tree,
            tmp_path,
            "--registry",
            reg.registry,
            "--arch",
            "arm64",
            "--out",
            str(out),
            COSIGN_FAIL="daegam-ui:",
        )
    assert run.returncode != 0 and "daegam-ui" in run.stderr and "서명" in run.stderr
    assert not out.exists()


def test_a_dirty_stamp_is_refused_before_the_registry(tree, tmp_path):
    stamp = tree / "scripts" / "build-stamp.sh"
    stamp.write_text(stamp.read_text().replace(STAMPS["agent"], STAMPS["agent"] + "-dirty"))
    with fake_registry() as reg:
        reg.push_products("arm64")
        docker_home(tmp_path, reg)
        run = _run(
            tree,
            tmp_path,
            "--registry",
            reg.registry,
            "--arch",
            "arm64",
            "--out",
            str(tmp_path / "o"),
        )
        assert run.returncode != 0 and "-dirty" in run.stderr
        assert reg.calls == []


@pytest.mark.parametrize("args", [(), ("--out", "x"), ("--arch", "", "--out", "x"), ("--arch",)])
def test_the_architecture_and_the_output_have_no_default(tree, tmp_path, args):
    run = _run(tree, tmp_path, *args, REGISTRY="registry.example.com/daegam")
    assert run.returncode != 0


def test_a_dry_run_writes_stamp_tags_without_asking_anyone(tree, tmp_path):
    out = tmp_path / "images.json"
    run = _run(
        tree,
        tmp_path,
        "--registry",
        "registry.example.com/daegam",
        "--arch",
        "arm64",
        "--out",
        str(out),
        DRY_RUN="1",
    )
    assert run.returncode == 0, run.stderr
    values = json.loads(out.read_text())["images"]
    assert values["platform"] == {"tag": STAMPS["platform"]}
    assert not (tmp_path / "cosign.log").exists()
