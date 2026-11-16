"""`make deps-audit` 와 `make base-scan` 의 판정, 그것이 읽는 lock 과 바탕, 고정한 Trivy (D55).

판정은 scripts/lib/trivy_judge.py 에 있고 deps-audit.sh·base-scan.sh 가 Trivy 의 JSON 을 넘긴다.
여기서는 Trivy 모양의 답을 손으로 만들어 넘긴다. Trivy 도 레지스트리도, python.org·NVD 도 부르지
않는다.
"""

from __future__ import annotations

import json
import re

import pytest
from base_pins import split_ref
from image_report import ACCEPTED, FIXABLE, NEW, Advisories
from trivy_judge import (
    base,
    base_vex,
    deps,
    deps_blocks,
    final_bases,
    trivy_cves,
    without_upgraded,
)

from tests.helpers.consts import REPO
from tests.helpers.images import product_dockerfiles
from tests.helpers.source import read_source

LOCKS = ["platform/uv.lock", "ui/package-lock.json"]
BUILDS = str(REPO / "security" / "cpython-builds.json")
UTIL_LINUX = "2.41.5-0+deb13u1"


def _vuln(
    cid: str, pkg: str, severity: str = "HIGH", fixed: str = "", version: str = "1.0.0"
) -> dict:
    return {
        "VulnerabilityID": cid,
        "PkgName": pkg,
        "InstalledVersion": version,
        "FixedVersion": fixed,
        "Severity": severity,
    }


def _suppressed(vuln: dict) -> dict:
    return {"Type": "vulnerability", "Status": "not_affected", "Finding": vuln}


def _doc(**by_dir: list[dict]) -> dict:
    return {
        "Results": [
            {"Target": t, "Vulnerabilities": by_dir.get(t.split("/")[0], [])} for t in LOCKS
        ]
    }


@pytest.fixture
def vex(tmp_path) -> str:
    """oauthlib 3.2.2 의 CVE 하나를 받는 수용 목록. 고친 판이 있어도 받는 길을 본다."""
    doc = {
        "@context": "https://openvex.dev/ns/v0.2.0",
        "statements": [
            {
                "vulnerability": {"name": "CVE-2026-9"},
                "products": [{"@id": "pkg:pypi/oauthlib@3.2.2"}],
                "status": "not_affected",
            },
            {
                "vulnerability": {"name": "CVE-2026-76642"},
                "products": [{"@id": "pkg:deb/debian/libblkid1@2.41.5-0%2Bdeb13u1"}],
                "status": "not_affected",
            },
        ],
    }
    path = tmp_path / "vex.json"
    path.write_text(json.dumps(doc))
    return str(path)


def test_clean_locks_pass(vex: str) -> None:
    lines, code = deps(_doc(), vex, LOCKS)

    assert code == 0
    assert lines[-1] == "deps-audit: 막는 것 0, 막지 않은 것 0, 수용됐지만 고친 판 있음 0. 통과"


@pytest.mark.parametrize(
    ("severity", "fixed", "blocks"),
    [
        ("HIGH", "6.1.0", True),
        ("CRITICAL", "6.1.0", True),
        ("CRITICAL", "", True),
        ("HIGH", "", False),
    ],
)
def test_deps_block_a_fixable_finding_or_any_critical(
    vex: str, severity: str, fixed: str, blocks: bool
) -> None:
    lines, code = deps(_doc(ui=[_vuln("CVE-2026-2", "lxml", severity, fixed)]), vex, LOCKS)

    assert code == int(blocks)
    expected = "막는 것 1, 막지 않은 것 0" if blocks else "막는 것 0, 막지 않은 것 1"
    assert expected in lines[-1]


def test_deps_take_the_reviewed_list_and_count_an_accepted_fix(vex: str) -> None:
    """Trivy 가 --vex 로 숨긴 것은 --show-suppressed 로 돌아오고 Judge 가 같은 목록으로 받는다."""
    doc = _doc()
    doc["Results"][0]["ExperimentalModifiedFindings"] = [
        _suppressed(_vuln("CVE-2026-9", "oauthlib", "CRITICAL", "4.0.1", "3.2.2"))
    ]

    lines, code = deps(doc, vex, LOCKS)

    assert code == 0
    assert "수용됨" in lines[1] and "oauthlib (고친 판: oauthlib 4.0.1)" in lines[1]
    assert lines[-1] == "deps-audit: 막는 것 0, 막지 않은 것 0, 수용됐지만 고친 판 있음 1. 통과"


def test_a_lock_trivy_did_not_read_fails_instead_of_passing(vex: str) -> None:
    lines, code = deps({"Results": [{"Target": "platform/uv.lock"}]}, vex, LOCKS)

    assert code == 1
    assert "   읽지 못함  ui/package-lock.json" in lines
    assert "deps-audit: Trivy 가 읽지 못한 lock 1 개" in lines


def test_the_deps_rule() -> None:
    from image_report import Finding

    assert deps_blocks(Finding(FIXABLE, "HIGH", "C", "x"))
    assert deps_blocks(Finding(NEW, "CRITICAL", "C", "x"))
    assert not deps_blocks(Finding(NEW, "HIGH", "C", "x"))
    assert not deps_blocks(Finding(ACCEPTED, "CRITICAL", "C", "x", fixed=True))


def test_trivy_findings_are_grouped_per_cve_like_zot() -> None:
    result = {
        "Vulnerabilities": [
            _vuln("CVE-1", "libblkid1", version=UTIL_LINUX),
            _vuln("CVE-1", "bsdutils", version=f"1:{UTIL_LINUX}", fixed="1:2.41.6-1"),
        ],
        "ExperimentalModifiedFindings": [
            _suppressed(_vuln("CVE-2", "zlib1g")),
            {"Type": "misconfiguration", "Finding": {}},
        ],
    }

    cves = trivy_cves(result)

    assert [(c["Id"], len(c["PackageList"])) for c in cves] == [("CVE-1", 2), ("CVE-2", 1)]
    assert cves[0]["PackageList"][1]["FixedVersion"] == "1:2.41.6-1"


def test_the_final_bases_are_those_of_the_last_stages() -> None:
    texts = {path.relative_to(REPO).as_posix(): read_source(path) for path in product_dockerfiles()}

    bases = final_bases(texts)

    assert sorted(split_ref(ref)[0] for ref in bases) == [
        "debian:trixie-slim",
        "gcr.io/distroless/cc-debian13:latest",
        "nginxinc/nginx-unprivileged:1.30.6-alpine3.24",
        "python:3.12-slim-trixie",
    ]
    upgrades = {split_ref(ref)[0]: up for ref, (_users, up) in bases.items()}
    assert upgrades == {
        "debian:trixie-slim": True,
        "gcr.io/distroless/cc-debian13:latest": False,
        "nginxinc/nginx-unprivileged:1.30.6-alpine3.24": True,
        "python:3.12-slim-trixie": True,
    }
    distroless = next(users for ref, (users, _) in bases.items() if "distroless" in ref)
    assert len(distroless) == 4


def test_an_upgrading_base_drops_only_the_fixed_packages() -> None:
    cves = [
        {"Id": "C1", "Severity": "CRITICAL", "PackageList": [{"Name": "a", "FixedVersion": "2"}]},
        {
            "Id": "C2",
            "Severity": "HIGH",
            "PackageList": [{"Name": "a", "FixedVersion": "2"}, {"Name": "b", "FixedVersion": ""}],
        },
        {"Id": "C3", "Severity": "HIGH", "PackageList": [{"Name": "b", "FixedVersion": ""}]},
    ]

    kept, dropped = without_upgraded(cves)

    assert dropped == 1
    assert [(c["Id"], [p["Name"] for p in c["PackageList"]]) for c in kept] == [
        ("C2", ["b"]),
        ("C3", ["b"]),
    ]


def test_the_upgrade_does_not_reach_the_nginx_packages() -> None:
    """nginx 와 nginx-module-* 는 nginx.org 에서 왔다. apk upgrade 가 올리지 않는다."""
    cves = [
        {
            "Id": "C5",
            "Severity": "CRITICAL",
            "PackageList": [
                {"Name": "nginx", "FixedVersion": "1.30.7-r1"},
                {"Name": "nginx-module-njs", "FixedVersion": "1.30.7.0.9-r1"},
                {"Name": "libxml2", "FixedVersion": "2.13.9-r0"},
            ],
        }
    ]

    kept, dropped = without_upgraded(cves)

    assert dropped == 0
    assert [p["Name"] for p in kept[0]["PackageList"]] == ["nginx", "nginx-module-njs"]


def test_base_scan_blocks_an_unaccepted_critical_of_a_pinned_base(vex: str) -> None:
    def fetch(url: str, timeout: float) -> bytes:
        raise TimeoutError("timed out")

    distroless = {
        "Results": [{"Vulnerabilities": [_vuln("CVE-3", "libc6", "CRITICAL", "2.41-13")]}]
    }
    nginx = {"Results": [{"Vulnerabilities": [_vuln("CVE-4", "musl", "CRITICAL", "1.2.6")]}]}
    scans = [
        (
            "gcr.io/distroless/cc-debian13:latest@sha256:1",
            distroless,
            False,
            ["platform/Dockerfile"],
        ),
        ("nginxinc/nginx-unprivileged:1.30@sha256:2", nginx, True, ["ui/Dockerfile"]),
    ]

    pins = {"platform/Dockerfile": ("0.13.6", "3.12.16")}
    lines, code = base(vex, BUILDS, scans, pins, Advisories(fetch))

    assert code == 1
    assert "   고친 판이 있는 1 건은 굽기의 OS 패키지 올리기가 받는다" in lines
    assert any("확인 못 함" in line for line in lines)  # 묶음 조회는 막지 않는다
    assert lines[-1] == "base-scan: 막는 것 1, 막지 않은 것 0, 수용됐지만 고친 판 있음 0. 실패"


def test_the_audit_reads_every_product_lock() -> None:
    """deps-audit.sh 는 표의 Dockerfile 옆에 lock 이 있는 구성요소를 본다. 파이썬 여섯과 ui 다."""
    locks = sorted(
        (path.parent / lock).relative_to(REPO).as_posix()
        for path in product_dockerfiles()
        for lock in ("uv.lock", "package-lock.json")
        if (path.parent / lock).is_file()
    )

    assert locks == [
        "agent-runtime/uv.lock",
        "deploy/dev/mock-oidc/uv.lock",
        "mcp-runtime/uv.lock",
        "platform/uv.lock",
        "sandbox/uv.lock",
        "ui/package-lock.json",
        "web-tools/uv.lock",
    ]


def test_the_trivy_image_is_pinned_and_an_installed_trivy_must_match_it() -> None:
    script = read_source(REPO / "scripts" / "lib" / "trivy.sh")
    version = re.search(r'^TRIVY_VERSION="([^"]+)"$', script, re.M)
    ref = re.search(r'^TRIVY_IMAGE="([^"]+)"$', script, re.M)

    assert version and ref
    image, digest = split_ref(ref.group(1))
    assert image == f"ghcr.io/aquasecurity/trivy:{version.group(1)}" and digest
    assert '= "$TRIVY_VERSION" ]' in script


def test_the_sandbox_base_and_lock_are_judged_by_the_sandbox_list_only(vex: str) -> None:
    """P40: base-images 목록이 받는 CVE 도 코드 실행 작업 공간의 바탕과 lock 에서는 받지 않는다. 그
    범위의 목록(sandbox.openvex.json)으로만 판정한다."""

    def fetch(url: str, timeout: float) -> bytes:
        raise TimeoutError("timed out")

    finding = _vuln("CVE-2026-76642", "libblkid1", "CRITICAL", version=UTIL_LINUX)
    debian = {"Results": [{"Vulnerabilities": [finding]}]}
    scans = [("debian:trixie-slim@sha256:3", debian, False, ["sandbox/Dockerfile"])]

    lines, code = base({"base": vex}, BUILDS, scans, {}, Advisories(fetch))
    assert code == 1 and "새로 나옴" in lines[1]
    lines, code = base({"base": vex, "sandbox": vex}, BUILDS, scans, {}, Advisories(fetch))
    assert code == 0 and "수용됨" in lines[1]

    oauthlib = _vuln("CVE-2026-9", "oauthlib", "CRITICAL", version="3.2.2")
    doc = {"Results": [{"Target": "sandbox/uv.lock", "Vulnerabilities": [oauthlib]}]}
    lines, code = deps(doc, {"base": vex}, ["sandbox/uv.lock"])
    assert code == 1


def test_trivy_gets_the_list_of_each_base_scope() -> None:
    assert base_vex(["sandbox/Dockerfile"]) == ["security/vex/sandbox.openvex.json"]
    assert base_vex(["platform/Dockerfile", "web-tools/Dockerfile"]) == [
        "security/vex/base-images.openvex.json"
    ]
    assert base_vex([]) == ["security/vex/base-images.openvex.json"]
    texts = {str(p.relative_to(REPO)): read_source(p) for p in product_dockerfiles()}
    users = {ref: paths for ref, (paths, _up) in final_bases(texts).items()}
    [debian] = [ref for ref in users if split_ref(ref)[0] == "debian:trixie-slim"]
    assert base_vex(users[debian]) == ["security/vex/sandbox.openvex.json"]
