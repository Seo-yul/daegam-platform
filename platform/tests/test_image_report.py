"""`make image-report` 의 판정, 수용 목록, 이미지 줄의 서명 칸 (D55, D66), CPython 묶음 (P58, D70).

판정은 scripts/lib/image_report.py 에 있고 image-report.sh 가 zot 에서 받은 CVE 목록을
넘긴다. 여기서는 zot 모양의 CVE 목록을 손으로 만들어 넘기고, 저장소의 수용 목록 둘
(security/vex/base-images.openvex.json, 코드 실행 작업 공간의 sandbox.openvex.json)이 Trivy `--vex`
가 읽는 OpenVEX 모양인지와, 대상마다 자기 범위의 목록으로만 판정하는지도 본다.
CPython 묶음은 python.org 와 NVD 의 응답을 손으로 만들어 조회 함수에 넘긴다. 네트워크에 닿지 않는다.
"""

from __future__ import annotations

import ast
import json
import re
import urllib.request
from collections.abc import Callable
from pathlib import Path

import public_fetch
import pytest
from cpython_builds import JUDGED
from image_report import (
    ACCEPTED,
    CPE,
    FIXABLE,
    NEW,
    NVD_URL,
    PYTHON_RELEASES_URL,
    UNKNOWN_LABEL,
    VEX_FILES,
    Advisories,
    Finding,
    Judge,
    Judges,
    bundled,
    closing,
    compare_versions,
    display_width,
    image_line,
    load_accepted,
    newer_cpython,
    nvd_cves,
    purl_package,
    stamp_pins,
    step_summary,
    summary,
    vex_scope,
    widest,
)

from tests.helpers.consts import REPO
from tests.helpers.images import product_dockerfiles
from tests.helpers.source import imported_modules, parse_python

VEX = REPO / "security" / "vex" / "base-images.openvex.json"
# 코드 실행 작업 공간(daegam-sandbox, sandbox/uv.lock, debian:trixie-slim)만 판정하는 목록 (P40)
SANDBOX_VEX = REPO / "security" / "vex" / "sandbox.openvex.json"
# OpenVEX v0.2.0 의 not_affected 사유
JUSTIFICATIONS = {
    "component_not_present",
    "vulnerable_code_not_present",
    "vulnerable_code_not_in_execute_path",
    "vulnerable_code_cannot_be_controlled_by_adversary",
    "inline_mitigations_already_exist",
}
UTIL_LINUX = "2.41.5-0+deb13u1"
SYSTEMD = "257.13-1~deb13u1"
ACCEPTED_UTIL_LINUX = {
    "CVE-2026-76642": {("libblkid1", UTIL_LINUX), ("bsdutils", f"1:{UTIL_LINUX}")},
}


def _pkg(name: str, version: str, fixed: str = "Not Specified") -> dict:
    return {"Name": name, "InstalledVersion": version, "FixedVersion": fixed}


def _cve(cid: str, *pkgs: dict, severity: str = "HIGH") -> dict:
    return {"Id": cid, "Severity": severity, "PackageList": list(pkgs)}


def _statement(cid: str, purl: str) -> dict:
    return {"vulnerability": {"name": cid}, "products": [{"@id": purl}], "status": "not_affected"}


def _util_linux(version: str = UTIL_LINUX) -> dict:
    return _cve("CVE-2026-76642", _pkg("libblkid1", version), _pkg("bsdutils", f"1:{version}"))


def test_every_package_in_the_list_is_accepted() -> None:
    [finding] = Judge(ACCEPTED_UTIL_LINUX).findings([_util_linux()])

    assert (finding.kind, finding.blocking) == (ACCEPTED, False)
    assert finding.detail == "bsdutils, libblkid1"


def test_cve_missing_from_the_list_is_new_and_noted_not_blocking() -> None:
    [finding] = Judge(ACCEPTED_UTIL_LINUX).findings([_cve("CVE-2026-1", _pkg("libc6", "2.41-12"))])

    assert (finding.kind, finding.blocking, finding.noted) == (NEW, False, True)
    assert finding.detail == "libc6 2.41-12"


def test_changed_version_is_new_and_the_old_one_is_unseen() -> None:
    judge = Judge(ACCEPTED_UTIL_LINUX)
    [finding] = judge.findings([_util_linux("2.41.6-1")])

    assert (finding.kind, finding.detail) == (NEW, "bsdutils 1:2.41.6-1, libblkid1 2.41.6-1")
    assert judge.unseen() == [f"   CVE-2026-76642  bsdutils 1:{UTIL_LINUX}, libblkid1 {UTIL_LINUX}"]


def test_only_the_packages_outside_the_list_are_named() -> None:
    cve = _util_linux()
    cve["PackageList"].append(_pkg("fdisk", UTIL_LINUX))

    [finding] = Judge(ACCEPTED_UTIL_LINUX).findings([cve])

    assert (finding.kind, finding.detail) == (NEW, f"fdisk {UTIL_LINUX}")


def test_one_pypi_statement_accepts_the_image_and_the_lock_spelling() -> None:
    """zot 는 깔린 dist-info 의 이름(`Authlib`)을, Trivy 는 lock 의 이름(`authlib`)을 댄다."""
    judge = Judge({"CVE-2026-20": {purl_package("pkg:pypi/Authlib@1.6.5")}})

    image = judge.findings([_cve("CVE-2026-20", _pkg("Authlib", "1.6.5"))])
    lock = judge.findings([_cve("CVE-2026-20", _pkg("authlib", "1.6.5"))])

    assert [f.kind for f in image + lock] == [ACCEPTED, ACCEPTED]
    assert judge.unseen() == []


@pytest.mark.parametrize(
    ("purl", "name"),
    [
        ("pkg:pypi/pyyaml@6.0.2", "PyYAML"),
        ("pkg:pypi/PyYAML@6.0.2", "pyyaml"),
        ("pkg:pypi/py-yaml@6.0.2", "Py_YAML"),
        ("pkg:pypi/zope.interface@6.0.2", "zope-interface"),
        ("pkg:pypi/zope_interface@6.0.2", "Zope..Interface"),
    ],
)
def test_pypi_names_match_by_their_pep_503_form(purl: str, name: str) -> None:
    judge = Judge({"CVE-2026-21": {purl_package(purl)}})

    [finding] = judge.findings([_cve("CVE-2026-21", _pkg(name, "6.0.2"))])

    assert finding.kind == ACCEPTED


def test_a_pep_503_match_still_needs_the_version() -> None:
    judge = Judge({"CVE-2026-22": {purl_package("pkg:pypi/PyYAML@6.0.2")}})

    [finding] = judge.findings([_cve("CVE-2026-22", _pkg("pyyaml", "6.0.3"))])

    assert (finding.kind, finding.detail) == (NEW, "pyyaml 6.0.3")


def test_cve_without_packages_is_not_accepted() -> None:
    [finding] = Judge(ACCEPTED_UTIL_LINUX).findings([_cve("CVE-2026-76642")])

    assert finding.kind == NEW


def test_the_list_accepts_a_fixable_finding_and_names_the_fix() -> None:
    """distroless 바탕에 고친 판이 아직 닿지 않았을 때 검토한 statement 가 받는 길이다."""
    cve = _util_linux()
    cve["PackageList"][0]["FixedVersion"] = "2.41.6-1"
    cve["Severity"] = "CRITICAL"

    [finding] = Judge(ACCEPTED_UTIL_LINUX).findings([cve])

    assert (finding.kind, finding.blocking, finding.noted, finding.fixed) == (
        ACCEPTED,
        False,
        False,
        True,
    )
    assert finding.detail == "bsdutils, libblkid1 (고친 판: libblkid1 2.41.6-1)"


def test_a_fixable_package_outside_the_list_is_fixable() -> None:
    cve = _util_linux()
    cve["PackageList"].append(_pkg("fdisk", UTIL_LINUX, "2.41.6-1"))

    [finding] = Judge(ACCEPTED_UTIL_LINUX).findings([cve])

    assert (finding.kind, finding.detail) == (FIXABLE, "fdisk (고친 판: fdisk 2.41.6-1)")


def test_a_multi_package_critical_with_one_fix_is_fixable_and_blocks() -> None:
    cve = _cve(
        "CVE-2026-9",
        _pkg("libssl3t64", "3.5.4-1", "3.5.5-1"),
        _pkg("openssl-provider-legacy", "3.5.4-1"),
        severity="CRITICAL",
    )

    [finding] = Judge({}).findings([cve])

    assert (finding.kind, finding.blocking, finding.noted) == (FIXABLE, True, False)
    assert finding.detail == "libssl3t64 (고친 판: libssl3t64 3.5.5-1)"


@pytest.mark.parametrize(
    ("severity", "fixed", "blocking"),
    [
        ("CRITICAL", "2.41-13", True),
        ("CRITICAL", "Not Specified", True),
        ("HIGH", "2.41-13", False),
        ("HIGH", "Not Specified", False),
    ],
)
def test_a_critical_outside_the_list_blocks_and_a_high_never(
    severity: str, fixed: str, blocking: bool
) -> None:
    """날마다 새 CVE 가 공개되므로 HIGH 는 막지 않고 끝 줄에 센다."""
    cve = _cve("CVE-2026-4", _pkg("libc6", "2.41-12", fixed), severity=severity)

    [finding] = Judge({}).findings([cve])

    assert (finding.blocking, finding.noted) == (blocking, not blocking)


def test_a_bundled_critical_outside_the_list_blocks() -> None:
    nvd = {
        ("sqlite", "3.53.1"): [
            _nvd(
                "CVE-2026-90001",
                _match(SQLITE, versionEndExcluding="3.53.2"),
                severity="CRITICAL",
            )
        ]
    }
    judge = Judge({})

    bundled(judge, _advisories(_answers(nvd=nvd)), _builds(), PINS)

    [finding] = [f for f in judge.judged if f.cve == "CVE-2026-90001"]
    assert (finding.kind, finding.severity, finding.blocking) == (NEW, "CRITICAL", True)


def test_closing_counts_high_and_fails_on_a_critical_or_a_missing_scan() -> None:
    judge = Judge(ACCEPTED_UTIL_LINUX)
    judge.findings(
        [
            _util_linux(),
            _cve("CVE-2026-5", _pkg("libc6", "2.41-12", "2.41-13")),
            _cve("CVE-2026-6", _pkg("libc6", "2.41-12")),
            _cve("CVE-2026-7", _pkg("libc6", "2.41-12")),
        ]
    )

    lines, code = closing(judge.judged, 0, "vex.json")

    assert code == 0
    assert lines[0].startswith(
        "image-report: 막지 않은 발견 3 건(고칠 수 있다 HIGH 1, 새로 나옴 HIGH 2). "
        "릴리스 노트에 적는다."
    )
    assert "vex.json" in lines[0]
    assert lines[-1] == "image-report: 막는 것 0, 막지 않은 것 3, 수용됐지만 고친 판 있음 0. 통과"

    judge.findings(
        [
            _cve("CVE-2026-8", _pkg("libc6", "2.41-12", "2.41-13"), severity="CRITICAL"),
            _cve("CVE-2026-10", _pkg("libc6", "2.41-12"), severity="CRITICAL"),
        ]
    )
    lines, code = closing(judge.judged, 1, "vex.json")

    assert code == 1
    assert lines[1].startswith(
        "image-report: 막는 발견 2 건(고칠 수 있다 CRITICAL 1, 새로 나옴 CRITICAL 1)."
    )
    assert lines[2] == "image-report: 스캔 결과가 없는 이미지 1 개"
    assert lines[-1] == "image-report: 막는 것 3, 막지 않은 것 3, 수용됐지만 고친 판 있음 0. 실패"


def test_closing_counts_a_finding_shared_by_two_images_once() -> None:
    judge = Judge({})
    shared = _cve("CVE-2026-11", _pkg("zlib1g", "1:1.3.dfsg-3+b1"))
    critical = _cve("CVE-2026-12", _pkg("libc6", "2.41-12"), severity="CRITICAL")
    judge.findings([shared, critical])  # platform
    judge.findings([shared, critical])  # mcp-runtime

    lines, code = closing(judge.judged, 0, "vex.json")

    assert code == 1
    assert lines[-1] == "image-report: 막는 것 1, 막지 않은 것 1, 수용됐지만 고친 판 있음 0. 실패"


def test_closing_counts_accepted_findings_that_have_a_fix() -> None:
    """바탕 고정을 옮기거나 statement 를 뺄 때가 왔다는 표시다. 막지는 않는다."""
    judge = Judge(ACCEPTED_UTIL_LINUX)
    cve = _util_linux()
    cve["PackageList"][0]["FixedVersion"] = "2.41.6-1"
    judge.findings([cve, _util_linux()])

    lines, code = closing(judge.judged, 0, "vex.json")

    assert code == 0
    assert lines[0].startswith("image-report: 수용됐지만 고친 판이 있는 것 1 건.")
    assert "make base-pins-check" in lines[0]
    assert lines[-1] == "image-report: 막는 것 0, 막지 않은 것 0, 수용됐지만 고친 판 있음 1. 통과"
    body, warning = step_summary(judge.judged, lines)
    assert body.count("CVE-2026-76642") == 1 and warning is not None


def test_closing_takes_another_rule_and_names() -> None:
    """deps-audit 는 고칠 수 있는 HIGH 도 막는다. 같은 closing 에 규칙과 이름을 넘긴다."""
    judge = Judge({})
    judge.findings([_cve("CVE-2026-5", _pkg("oauthlib", "3.2.2", "4.0.1"))])

    lines, code = closing(
        judge.judged,
        1,
        "vex.json",
        what="Trivy 가 읽지 못한 lock",
        name="deps-audit",
        blocks=lambda f: f.kind == FIXABLE,
    )

    assert code == 1
    assert lines[-2:] == [
        "deps-audit: Trivy 가 읽지 못한 lock 1 개",
        "deps-audit: 막는 것 2, 막지 않은 것 0, 수용됐지만 고친 판 있음 0. 실패",
    ]


def test_closing_without_findings_is_one_line() -> None:
    assert closing([], 0, "vex.json") == (
        ["image-report: 막는 것 0, 막지 않은 것 0, 수용됐지만 고친 판 있음 0. 통과"],
        0,
    )
    lines, code = closing([], 2, "vex.json")
    assert code == 1 and lines == [
        "image-report: 스캔 결과가 없는 이미지 2 개",
        "image-report: 막는 것 2, 막지 않은 것 0, 수용됐지만 고친 판 있음 0. 실패",
    ]


def test_the_step_summary_holds_the_findings_once_and_warns_once() -> None:
    judge = Judge(ACCEPTED_UTIL_LINUX)
    shared = _cve("CVE-2026-11", _pkg("zlib1g", "1:1.3.dfsg-3+b1"))
    judge.findings([_util_linux(), shared])
    judge.findings([shared])
    lines, _ = closing(judge.judged, 0, "vex.json")

    body, warning = step_summary(judge.judged, lines)

    assert body.count("CVE-2026-11") == 1 and "CVE-2026-76642" not in body
    assert body.startswith("### image-report\n\n```\n") and lines[-1] in body
    assert warning == (
        "::warning title=image-report::image-report: 막는 것 0, 막지 않은 것 1, "
        "수용됐지만 고친 판 있음 0. 통과. 단계 요약에 발견이 있다"
    )


def test_the_step_summary_does_not_warn_without_findings() -> None:
    judge = Judge(ACCEPTED_UTIL_LINUX)
    judge.findings([_util_linux()])
    lines, _ = closing(judge.judged, 0, "vex.json")

    assert step_summary(judge.judged, lines)[1] is None


def test_only_high_and_critical_are_judged() -> None:
    cves = [
        _cve("CVE-2026-2", _pkg("libc6", "2.41-12"), severity="MEDIUM"),
        _cve("CVE-2026-3", _pkg("libc6", "2.41-12"), severity="CRITICAL"),
    ]

    assert [f.cve for f in Judge({}).findings(cves)] == ["CVE-2026-3"]


def test_unseen_counts_every_image_of_the_run() -> None:
    judge = Judge(ACCEPTED_UTIL_LINUX)
    judge.findings([])  # UI 이미지처럼 없는 이미지
    assert len(judge.unseen()) == 1

    judge.findings([_util_linux()])
    assert judge.unseen() == []


def test_severity_column_lines_up_for_every_kind() -> None:
    """줄 머리의 표시 폭이 달라도 심각도 열은 같은 칸에서 시작한다. 한글은 두 칸이다."""
    lines = [Finding(kind, "HIGH", "CVE-1", "x").line() for kind in (FIXABLE, ACCEPTED, NEW)]

    assert len({display_width(line.split("HIGH")[0]) for line in lines}) == 1
    assert display_width("수용됨 a") == 8


def test_signature_column_lines_up_for_every_image() -> None:
    """이미지 이름의 길이와 서명 여부가 달라도 서명 칸과 결과는 같은 칸에서 시작한다."""
    ui, agent = "daegam/daegam-ui:12d1948", "daegam/daegam-agent-runtime:80db28a"
    width = widest((ui, agent))

    signed = image_line(ui, width, True, "취약점 없음")
    unsigned = image_line(agent, width, False, "취약점 없음")

    assert signed == f"{ui.ljust(width)}  서명 확인됨      취약점 없음"
    assert unsigned == f"{agent}  서명 확인 안 됨  취약점 없음"


def test_summary_counts_in_severity_order() -> None:
    cves = [_cve("CVE-1", severity=s) for s in ("LOW", "HIGH", "LOW", "UNKNOWN", "CRITICAL")]

    assert summary(cves) == "CRITICAL 1, HIGH 1, LOW 2, UNKNOWN 1"
    assert summary([]) == "없음"


@pytest.mark.parametrize(
    ("purl", "package"),
    [
        ("pkg:deb/debian/libblkid1@2.41.5-0%2Bdeb13u1", ("libblkid1", UTIL_LINUX)),
        ("pkg:deb/debian/bsdutils@2.41.5-0%2Bdeb13u1?epoch=1", ("bsdutils", f"1:{UTIL_LINUX}")),
        ("pkg:deb/debian/libudev1@257.13-1~deb13u1?arch=arm64&epoch=0", ("libudev1", SYSTEMD)),
        ("pkg:pypi/oauthlib@3.2.2", ("oauthlib", "3.2.2")),
        ("pkg:pypi/PyJWT@2.10.1", ("pyjwt", "2.10.1")),
        ("pkg:pypi/zope.interface@7.2", ("zope-interface", "7.2")),
        ("pkg:npm/dompurify@3.4.8", ("dompurify", "3.4.8")),
        ("pkg:npm/%40tanstack/react-query@5.90.0", ("@tanstack/react-query", "5.90.0")),
    ],
)
def test_purl_version_matches_the_installed_version(purl: str, package: tuple[str, str]) -> None:
    assert purl_package(purl) == package


@pytest.mark.parametrize("purl", ["pkg:deb/debian/libblkid1", "libblkid1@2.41.5"])
def test_purl_without_version_is_refused(purl: str) -> None:
    with pytest.raises(ValueError):
        purl_package(purl)


def test_only_not_affected_statements_accept(tmp_path) -> None:
    product = [{"@id": "pkg:deb/debian/libc6@2.41-12"}]
    doc = {
        "statements": [
            {"vulnerability": {"name": "CVE-1"}, "products": product, "status": "not_affected"},
            {"vulnerability": {"name": "CVE-2"}, "products": product, "status": "affected"},
        ]
    }
    path = tmp_path / "vex.json"
    path.write_text(json.dumps(doc))

    assert load_accepted(str(path)) == {"CVE-1": {("libc6", "2.41-12")}}


@pytest.mark.parametrize("vex", [VEX, SANDBOX_VEX], ids=lambda path: path.name)
def test_repository_list_is_openvex_that_trivy_reads(vex: Path) -> None:
    doc = json.loads(vex.read_text(encoding="utf-8"))

    assert doc["@context"] == "https://openvex.dev/ns/v0.2.0"
    assert doc["@id"] and doc["author"] and doc["timestamp"] and isinstance(doc["version"], int)
    names = [s["vulnerability"]["name"] for s in doc["statements"]]
    assert len(names) == len(set(names)), "statement 는 CVE 마다 하나다"
    for statement in doc["statements"]:
        assert statement["status"] == "not_affected"
        assert statement["justification"] in JUSTIFICATIONS
        assert statement["impact_statement"].strip()
        for product in statement["products"]:
            purl = product["@id"]
            generic = tuple(f"pkg:generic/{c}@" for c in JUDGED)
            assert purl.startswith("pkg:deb/debian/") or purl.startswith(generic), purl
            purl_package(purl)
    assert set(load_accepted(str(vex))) == set(names)


# -- 목록의 범위 (P40) -------------------------------------------------------------------------


def test_each_product_is_judged_by_one_list() -> None:
    """여섯 이미지는 base-images 목록으로, 코드 실행 작업 공간의 이미지·lock·바탕은 sandbox
    목록으로만 판정한다. 그 이미지에서 도는 코드는 사용자의 것이라 다른 이미지의 근거가 서지
    않는다."""
    scopes = {
        path.relative_to(REPO).as_posix(): vex_scope(path.relative_to(REPO).as_posix())
        for path in product_dockerfiles()
    }
    assert scopes.pop("sandbox/Dockerfile") == "sandbox"
    assert set(scopes.values()) == {"base"} and len(scopes) == 6
    assert vex_scope("sandbox/uv.lock") == "sandbox" and vex_scope("") == "base"
    assert {k: REPO / v for k, v in VEX_FILES.items()} == {"base": VEX, "sandbox": SANDBOX_VEX}


def test_a_list_never_accepts_for_another_scope(tmp_path) -> None:
    only_base = tmp_path / "base.json"
    only_base.write_text(json.dumps({"statements": [_statement("CVE-1", "pkg:generic/sqlite@3")]}))
    judges = Judges({"base": str(only_base)})
    cve = _cve("CVE-1", _pkg("sqlite", "3"))

    [platform] = judges.for_path("platform/Dockerfile").findings([cve])
    [sandbox] = judges.for_path("sandbox/Dockerfile").findings([cve])

    assert (platform.kind, sandbox.kind) == (ACCEPTED, NEW)
    assert len(judges.judged) == 2 and judges.hint() == str(only_base)


def test_the_sandbox_list_accepts_nothing_the_server_reaches() -> None:
    """서버(PID 1)가 닿는 OpenSSL(hashlib·hmac·ssl), libffi(ctypes), glibc 의 발견은 막는다. 그래서
    sandbox 목록에는 그 statement 가 없다. 사용자 코드만 닿는다는 까닭은 서버가 싣는 모듈을 고정한
    시험에 기댄다."""
    doc = json.loads(SANDBOX_VEX.read_text(encoding="utf-8"))
    names = {purl_package(p["@id"])[0] for s in doc["statements"] for p in s["products"]}
    reached = {"openssl", "libssl3t64", "libffi8", "libc6", "libc-bin"}
    assert names & reached == set()
    for statement in doc["statements"]:
        if statement["justification"] == "inline_mitigations_already_exist":
            assert "sandbox/tests/test_imports.py" in statement["impact_statement"]
            assert "uid 1000" in statement["impact_statement"]


# -- CPython 묶음 (P58, D70) -----------------------------------------------------------------

PINS = ("0.12.23", "3.12.15")
VERSIONS = {
    "cpython": "3.12.15",
    "openssl": "3.5.9",
    "sqlite": "3.53.1",
    "zlib": "1.3.2",
    "expat": "2.8.5",
}


def _builds(**versions: str) -> dict:
    merged = {**VERSIONS, **versions, "ncurses": "6.5.20240427"}
    return {
        (*PINS, platform): {"release": "20261003", "versions": merged}
        for platform in ("linux/amd64", "linux/arm64")
    }


def _match(criteria: str, vulnerable: bool = True, **bounds: str) -> dict:
    return {"criteria": criteria, "vulnerable": vulnerable, **bounds}


def _nvd(cid: str, *matches: dict, severity: str = "HIGH", metrics: dict | None = None) -> dict:
    if metrics is None:
        metrics = {"cvssMetricV31": [{"type": "Primary", "cvssData": {"baseSeverity": severity}}]}
    return {
        "cve": {
            "id": cid,
            "metrics": metrics,
            "configurations": [{"nodes": [{"cpeMatch": list(matches)}]}],
        }
    }


SQLITE = "cpe:2.3:a:sqlite:sqlite:*:*:*:*:*:*:*:*"
PYTHON_LIST = [{"name": "Python 3.12.15"}]


def _page(items: list, total: int | None = None) -> bytes:
    """NVD 응답 한 쪽. total 을 주지 않으면 items 가 전부다."""
    count = len(items) if total is None else total
    return json.dumps({"vulnerabilities": items, "totalResults": count}).encode()


def _answers(*, python: list | None = None, nvd: dict | None = None) -> dict:
    """주소 -> 응답 본문. nvd 는 (구성요소, 판) -> vulnerabilities 항목들이다."""
    out = {PYTHON_RELEASES_URL: json.dumps(python or PYTHON_LIST).encode()}
    for (component, version), items in (nvd or {}).items():
        out[NVD_URL.format(*CPE[component], version)] = _page(items)
    return out


def _advisories(answers: dict, calls: list | None = None) -> Advisories:
    """answers 로 답하는 조회. 없는 주소는 걸리는 것이 없는 NVD 답이다."""

    def fetch(url: str, timeout: float) -> bytes:
        assert 0 < timeout <= 10
        if calls is not None:
            calls.append(url)
        return answers.get(url, _page([]))

    return Advisories(fetch)


def test_nvd_keeps_only_vulnerable_matches_that_cover_the_version() -> None:
    items = [
        _nvd("CVE-in-range", _match(SQLITE, versionEndExcluding="3.53.2")),
        _nvd("CVE-fixed-before", _match(SQLITE, versionEndExcluding="3.53.1")),
        _nvd("CVE-exact", _match("cpe:2.3:a:sqlite:sqlite:3.53.1:*:*:*:*:*:*:*")),
        _nvd("CVE-other-exact", _match("cpe:2.3:a:sqlite:sqlite:3.39.1:*:*:*:*:*:*:*")),
        # 미리 보기 판(update 칸)만 걸리는 CPE 는 정식 판을 덮지 않는다
        _nvd("CVE-beta", _match("cpe:2.3:a:sqlite:sqlite:3.53.1:beta1:*:*:*:*:*:*")),
        # 다른 제품이 그 위에서 돌 때만 걸리는 조합: sqlite 쪽은 vulnerable 이 거짓이다
        _nvd("CVE-runs-on", _match(SQLITE, vulnerable=False, versionStartIncluding="3.39.2")),
        _nvd(
            "CVE-other-product",
            _match("cpe:2.3:a:php:php:*:*:*:*:*:*:*:*", versionEndExcluding="9"),
        ),
    ]

    found = nvd_cves(items, "sqlite", "3.53.1")

    assert [c["Id"] for c in found] == ["CVE-in-range", "CVE-exact"]
    assert found[0]["PackageList"] == [
        {"Name": "sqlite", "InstalledVersion": "3.53.1", "FixedVersion": ""}
    ]


def test_a_newer_cpython_of_the_same_minor_counts() -> None:
    releases = [
        {"name": n} for n in ("Python 3.12.16", "Python 3.12.15", "Python 3.13.9", "Python 3.12.9")
    ]

    assert newer_cpython(releases, "3.12.15") == "3.12.16"
    assert newer_cpython(releases, "3.12.16") is None


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        ("3.13.0rc1", "3.13.0"),
        ("1.0.0-rc1", "1.0.0"),
        ("3.13.0a1", "3.13.0b1"),
        ("3.13.0b1", "3.13.0rc1"),
        ("3.13.0rc1", "3.13.0rc2"),
        ("1.1.1", "1.1.1v"),
        ("1.1.1v", "1.1.1w"),
        ("3.53.1", "3.53.2"),
        ("3.9.9", "3.10.0"),
    ],
)
def test_versions_order_pre_releases_before_the_release(older: str, newer: str) -> None:
    assert compare_versions(older, newer) == -1
    assert compare_versions(newer, older) == 1


def test_versions_with_fewer_parts_are_padded_with_zero() -> None:
    assert compare_versions("1.0", "1.0.0") == 0
    assert compare_versions("3.53.1", "3.53.1") == 0


@pytest.mark.parametrize(
    ("metrics", "severity"),
    [
        # V4.0 의 Secondary 가 MEDIUM 이어도 V3.1 의 Primary 가 HIGH 면 HIGH 다
        (
            {
                "cvssMetricV40": [{"type": "Secondary", "cvssData": {"baseSeverity": "MEDIUM"}}],
                "cvssMetricV31": [{"type": "Primary", "cvssData": {"baseSeverity": "HIGH"}}],
            },
            "HIGH",
        ),
        # Secondary 둘이 LOW 와 CRITICAL 이면 CRITICAL 이다
        (
            {
                "cvssMetricV31": [
                    {"type": "Secondary", "cvssData": {"baseSeverity": "LOW"}},
                    {"type": "Secondary", "cvssData": {"baseSeverity": "CRITICAL"}},
                ]
            },
            "CRITICAL",
        ),
        # V2 는 심각도를 cvssData 밖에 둔다
        ({"cvssMetricV2": [{"type": "Primary", "baseSeverity": "HIGH"}]}, "HIGH"),
        ({}, "UNKNOWN"),
    ],
)
def test_the_severity_is_the_highest_of_every_cvss_row(metrics: dict, severity: str) -> None:
    item = _nvd("CVE-1", _match(SQLITE), metrics=metrics)

    assert nvd_cves([item], "sqlite", "3.53.1")[0]["Severity"] == severity


@pytest.mark.parametrize(
    "answer",
    [
        b"{}",
        b'{"vulnerabilities": []}',  # totalResults 가 없다
        b'{"totalResults": 0}',  # vulnerabilities 가 없다
        b'{"vulnerabilities": [], "totalResults": 2}',  # 말한 수만큼 주지 않았다
        b'{"vulnerabilities": [{}, {}], "totalResults": 1}',  # 말한 수보다 많이 줬다
        b'{"vulnerabilities": [{"cve": {}}], "totalResults": 1}',  # cve.id 가 없다
        b'"vulnerabilities"',
        b"<html>",  # JSON 이 아니다
    ],
)
def test_a_wrong_shaped_nvd_answer_is_unknown_not_clean(answer: bytes) -> None:
    advisories = Advisories(lambda url, timeout: answer)

    with pytest.raises(LookupError):
        advisories.cves("sqlite", "3.53.1")


def test_nvd_answers_are_read_page_by_page_until_every_result_is_in() -> None:
    url = NVD_URL.format(*CPE["sqlite"], "3.53.1")
    first = _page([_nvd("CVE-1", _match(SQLITE))], total=2)
    second = _page([_nvd("CVE-2", _match(SQLITE))], total=2)
    calls: list[str] = []
    advisories = _advisories({url: first, f"{url}&startIndex=1": second}, calls)

    assert [c["Id"] for c in advisories.cves("sqlite", "3.53.1")] == ["CVE-1", "CVE-2"]
    assert calls == [url, f"{url}&startIndex=1"]


@pytest.mark.parametrize(
    "answer",
    [
        [],
        [{"name": "Python 3.12.16"}],  # 기록된 판이 없는 잘린 목록
        {"results": [{"name": "Python 3.12.15"}]},
        ["Python 3.12.15"],
    ],
)
def test_a_python_list_without_the_recorded_release_is_unknown(answer: object) -> None:
    advisories = Advisories(lambda url, timeout: json.dumps(answer).encode())

    with pytest.raises(LookupError, match="python.org"):
        advisories.cves("cpython", "3.12.15")


def test_an_old_bundled_version_with_a_fixed_release_is_new_and_noted() -> None:
    nvd = {
        ("sqlite", "3.39.1"): [_nvd("CVE-2022-35737", _match(SQLITE, versionEndExcluding="3.39.2"))]
    }
    answers = _answers(python=[{"name": "Python 3.12.16"}, *PYTHON_LIST], nvd=nvd)
    judge = Judge({})

    lines = bundled(judge, _advisories(answers), _builds(sqlite="3.39.1"), PINS)

    assert [(f.kind, f.blocking, f.noted) for f in judge.judged] == [(NEW, False, True)] * 2
    assert "3.12.15 (python-build-standalone 20261003): openssl 3.5.9, sqlite 3.39.1" in lines[0]
    assert Finding(NEW, "HIGH", "Python-3.12.16", "cpython 3.12.15").line() in lines
    assert Finding(NEW, "HIGH", "CVE-2022-35737", "sqlite 3.39.1").line() in lines
    assert "ncurses 6.5.20240427" in lines[-1] and "xz" in lines[-1]


def test_an_accepted_generic_statement_passes() -> None:
    nvd = {
        ("sqlite", "3.53.1"): [_nvd("CVE-2026-11822", _match(SQLITE, versionEndExcluding="3.53.2"))]
    }
    judge = Judge(load_accepted(str(VEX)))

    lines = bundled(judge, _advisories(_answers(nvd=nvd)), _builds(), PINS)

    assert not any(f.noted or f.blocking for f in judge.judged)
    assert Finding(ACCEPTED, "HIGH", "CVE-2026-11822", "sqlite").line() in lines


def test_an_unreachable_source_prints_unknown_and_does_not_fail() -> None:
    def fetch(url: str, timeout: float) -> bytes:
        raise TimeoutError("timed out")

    judge = Judge(load_accepted(str(VEX)))
    lines = bundled(judge, Advisories(fetch), _builds(), PINS)

    unknown = [line for line in lines if UNKNOWN_LABEL in line]
    assert judge.judged == [] and len(unknown) == len(JUDGED)
    assert "sqlite 3.53.1 (TimeoutError: timed out)" in unknown[2]
    # 조회하지 못한 판의 수용 항목은 "이번에 없다" 고 알리지 않는다. 남는 것은 이 빌드에 없는 판과,
    # 이 시험이 재지 않는 이미지의 Debian 패키지다
    unseen = judge.unseen()
    assert not any("pkg" in line for line in unseen)
    assert not any(f" {n} {v}" in line for line in unseen for n, v in VERSIONS.items())


def test_an_unreadable_ca_file_prints_unknown_instead_of_stopping(monkeypatch, tmp_path) -> None:
    opened: list[object] = []
    monkeypatch.setenv("DAEGAM_CA_FILE", str(tmp_path / "missing.pem"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: opened.append(a))

    judge = Judge({})
    lines = bundled(judge, Advisories(public_fetch.fetch), _builds(), PINS)

    unknown = [line for line in lines if UNKNOWN_LABEL in line]
    assert judge.judged == [] and len(unknown) == len(JUDGED)
    assert all("DAEGAM_CA_FILE" in line for line in unknown)
    assert opened == []


def test_a_spent_budget_prints_unknown_without_asking() -> None:
    now = [0.0]
    calls: list[str] = []

    def fetch(url: str, timeout: float) -> bytes:
        calls.append(url)
        now[0] += 61  # 첫 조회가 예산을 다 쓴다
        return json.dumps(PYTHON_LIST).encode()

    judge = Judge({})
    lines = bundled(judge, Advisories(fetch, clock=lambda: now[0]), _builds(), PINS)

    assert judge.judged == [] and len(calls) == 1
    assert sum("조회 시간 예산을 다 썼다" in line for line in lines) == len(JUDGED) - 1


def test_only_time_inside_the_requests_counts_against_the_budget() -> None:
    now = [0.0]
    calls: list[str] = []

    def fetch(url: str, timeout: float) -> bytes:
        calls.append(url)
        now[0] += 1
        return _page([])

    advisories = Advisories(fetch, clock=lambda: now[0])
    for version in ("3.53.1", "3.39.1", "3.38.0"):
        now[0] += 100  # 이미지 사이에 zot 의 스캔 결과를 기다린다
        assert advisories.cves("sqlite", version) == []

    assert len(calls) == 3 and advisories.spent == 3


def test_each_component_version_is_asked_once_per_report() -> None:
    calls: list[str] = []
    advisories = _advisories(_answers(), calls)

    for _ in range(3):  # 같은 빌드를 싣는 세 이미지
        bundled(Judge({}), advisories, _builds(), PINS)

    assert len(calls) == len(JUDGED)


def test_unknown_pins_print_versions_unknown_and_ask_nothing() -> None:
    calls: list[str] = []

    no_tag = bundled(
        Judge({}),
        _advisories({}, calls),
        _builds(),
        None,
        "태그 0.1.0 시점의 Dockerfile 을 이 저장소에서 찾지 못했다",
    )
    no_reason = bundled(Judge({}), _advisories({}, calls), _builds(), None)
    no_record = bundled(Judge({}), _advisories({}, calls), {}, PINS)

    assert "판 모름 (태그 0.1.0" in no_tag[0]
    assert "판 모름 (이미지 태그의 핀을 읽지 못했다)" in no_reason[0]
    assert (
        "판 모름 (security/cpython-builds.json 에 uv 0.12.23·CPython 3.12.15 기록이 없다)"
        in no_record[0]
    )
    assert calls == []


def test_pins_are_read_at_the_image_tag() -> None:
    old = "RUN pip install --no-cache-dir uv==0.12.20\nARG CPYTHON_VERSION=3.12.14\n"
    now = "RUN pip install --no-cache-dir uv==0.12.23\nARG CPYTHON_VERSION=3.12.15\n"

    assert stamp_pins("abc1234", now, old) == (("0.12.20", "3.12.14"), "")
    assert stamp_pins("0.1.0", now, None) == (
        None,
        "태그 0.1.0 시점의 Dockerfile 을 이 저장소에서 찾지 못했다",
    )
    assert stamp_pins("abc1234", now, "FROM python:3.12-slim-trixie AS build\n") == (
        None,
        "태그 abc1234 시점의 Dockerfile 에 uv 핀이나 CPYTHON_VERSION 이 없다",
    )
    assert stamp_pins("abc1234", "FROM nginxinc/nginx-unprivileged:1.30.5\n", None) is None


# SQLite 수용(CVE-2026-11822·11824·119373·119374·119379)은 이미지의 코드가 SQLite 를 열지 않는다는
# 데 기댄다. mock-oidc 이미지의 Debian libsqlite3-0 에서 나온 119373·119374·119379 도 같은 가정에
# 기댄다
SQLITE_USERS = ("diskcache", "langgraph-checkpoint-sqlite", "aiosqlite")


@pytest.mark.parametrize(
    "lock", ["platform", "mcp-runtime", "agent-runtime", "web-tools", "deploy/dev/mock-oidc"]
)
def test_no_lock_brings_a_package_that_opens_sqlite(lock: str) -> None:
    text = (REPO / lock / "uv.lock").read_text(encoding="utf-8")
    found = [name for name in SQLITE_USERS if f'\nname = "{name}"\n' in text]

    assert found == [], (
        f"{lock}/uv.lock 에 {found} 가 들어왔다. {VEX.name} 의 SQLite statement 를 다시 검토하고 "
        "이 목록을 고친다"
    )


# expat 수용(CVE-2026-113351·115506·117610·118370)은 남이 준 XML 을 lxml 로 읽고(daegam/xmlsafe.py),
# CPython 의 expat 은 DOCTYPE 을 먼저 거절한 xlsx 의 openpyxl 만 쓴다는 데 기댄다. SQLite 수용은
# 우리 코드도 SQLite 를 열지 않는다는 데 기댄다
CPYTHON_CODE = (
    "platform/daegam",
    "mcp-runtime/runtime",
    "agent-runtime/runtime",
    "web-tools/webtools",
)
# mock-oidc 는 python 바탕 이미지의 CPython 으로 돈다. SQLite 와 zlib1g 의 수용은 이 코드도 본다.
# 코드 실행 작업 공간(sandbox/, P40)은 이 목록과 위 lock 목록에 넣지 않는다. 그 이미지에서 도는
# 코드는 사용자의 것이라 "우리 코드가 부르지 않는다" 는 근거가 서지 않는다. 그 이미지와 lock, 바탕은
# base-images 목록이 아니라 security/vex/sandbox.openvex.json 으로만 판정한다(`VEX_FILES`). 그
# 목록의 근거는 서버가 싣는 모듈을 고정한 sandbox/tests/test_imports.py 가 지킨다
MOCK_OIDC_CODE = "deploy/dev/mock-oidc/mock_oidc"
# 표준 라이브러리의 XML 파서(expat)에 닿는 이름이다. import 의 어느 꼴이든, importlib 으로 부르든
# 걸린다. pypdf 의 xmp_metadata(xml.dom)와 langchain 의 XMLOutputParser(ElementTree)도 expat 으로
# 읽는다. defusedxml 은 openpyxl 이 DTD 를 스스로 거절하게 하는 방어라 이름에서 뺐다. 그것을 우리
# 코드가 직접 부르는 일이 없음은 코드 검사(test_only_the_xlsx_reader_uses_openpyxl)가 지킨다
EXPAT_NAMES = re.compile(
    r"\bxml\.(?:etree|dom|sax|parsers)\b|from\s+xml\s+import|\b(?:pyexpat|plistlib|xmlrpc)\b"
    r"|\b(?:xmltodict|xmp_metadata|XMLOutputParser)\b"
)
SQLITE_NAMES = re.compile(r"\b(?:sqlite3|aiosqlite|CachingClient|SqliteCache)\b")
# 남이 준 XML 을 expat 으로 읽는 패키지다. lock 에 들어오면 expat statement 를 다시 검토한다
EXPAT_USERS = ("xmltodict", "feedparser", "untangle", "xmlschema")


def _code_matching(
    found: re.Pattern[str] | Callable[[str], bool], roots: tuple[str, ...] = CPYTHON_CODE
) -> list[str]:
    matches = found.search if isinstance(found, re.Pattern) else found
    return [
        str(path.relative_to(REPO))
        for root in roots
        for path in sorted((REPO / root).rglob("*.py"))
        if matches(path.read_text(encoding="utf-8"))
    ]


def _loads(module: str) -> Callable[[str], bool]:
    """소스가 module 이나 그 아래 모듈을 부르면 참인 검사. import 의 어느 꼴이든(`import os, pkg`,
    `from pkg.sub import x`), importlib 에 넘기는 이름이든(`importlib.import_module("pkg")`) 걸린다.
    주석과 설명 글에 나온 이름은 보지 않는다."""

    def check(source: str) -> bool:
        names = imported_modules(ast.parse(source), strings=True)
        return any(name == module or name.startswith(f"{module}.") for name in names)

    return check


def test_our_code_reads_no_xml_with_the_stdlib_parsers() -> None:
    found = _code_matching(EXPAT_NAMES)

    assert found == [], (
        f"{found} 가 표준 라이브러리의 XML 파서(expat)에 닿는다. daegam/xmlsafe.py 로 읽거나 "
        f"{VEX.name} 의 expat statement 를 다시 검토한다"
    )


def test_openpyxl_refuses_dtds_by_itself() -> None:
    """defusedxml 이 설치돼 있으면 openpyxl 이 DTD 와 엔티티를 스스로 거절한다. 이 환경에서
    꺼져 있으면 ooxml.py 의 DTD 거절 하나만 남으므로 expat statement 의 근거가 약해진다."""
    import openpyxl

    assert openpyxl.DEFUSEDXML is True


def test_only_the_xlsx_reader_uses_openpyxl() -> None:
    """openpyxl 은 expat 으로 읽으므로 DOCTYPE 을 먼저 거절하는 ooxml.py 만 쓴다."""
    assert _code_matching(_loads("openpyxl")) == ["platform/daegam/documents/ooxml.py"]


@pytest.mark.parametrize(
    ("source", "loads"),
    [
        ("import openpyxl", True),
        ("import os, openpyxl as xl", True),
        ("from openpyxl.reader.excel import load_workbook", True),
        ("import importlib\nimportlib.import_module('openpyxl')", True),
        ("x = __import__('openpyxl.reader')", True),
        ("# openpyxl 은 expat 으로 읽는다\nimport io", False),
        ('"""openpyxl 은 쓰지 않는다."""', False),
        ("import openpyxl_like", False),
    ],
)
def test_the_openpyxl_check_sees_every_way_to_load_it(source: str, loads: bool) -> None:
    assert _loads("openpyxl")(source) is loads


# zlib1g 수용(CVE-2026-76844·85091)은 이미지의 어느 코드도 zlib 의 gz* 파일 함수를 부르지
# 않는다는 데 기댄다. CPython 의 zlib 모듈은 메모리 안의 deflate·inflate 만 쓰므로 파이썬 코드가
# gz* 에 닿는 길은 C 함수를 부르는 모듈(ctypes, _ctypes, cffi)뿐이다. 그것을 쓰는 것은 문서 추출의
# 가두기 하나이고, 자기 프로세스(CDLL(None))에서 prctl 만 꺼낸다
SANDBOX = "platform/daegam/documents/sandbox.py"
FFI_MODULES = ("ctypes", "_ctypes", "cffi")
# 가두기가 ctypes 에서 꺼내는 이름과, CDLL(None) 이 연 핸들에서 꺼내는 C 함수
SANDBOX_CTYPES = frozenset({"CDLL", "get_errno"})
SANDBOX_C_FUNCTIONS = frozenset({"prctl"})


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """노드 -> 그 노드를 품은 노드. AST 노드는 값이 아니라 정체로 키가 된다."""
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _stray(node: ast.AST, parents: dict[ast.AST, ast.AST], allowed: frozenset[str]) -> bool:
    """node(이름이나 호출)를 allowed 의 속성을 꺼내는 데(`libc.prctl`)만 쓰지 않으면 참이다.
    첨자(`libc["x"]`), getattr 에 넘기기, 다른 이름에 담기, 인자로 넘기기가 모두 참이다."""
    parent = parents.get(node)
    return not (
        isinstance(parent, ast.Attribute) and parent.value is node and parent.attr in allowed
    )


def _uses(tree: ast.AST, name: str) -> list[ast.Name]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == name]


def _cdll_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "CDLL"
    ]


def _bound_name(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> str | None:
    """CDLL(...) 의 결과를 담은 이름. `libc = ctypes.CDLL(None)` 처럼 이름 하나에 담는 대입만 센다.
    다른 꼴(여러 대상, 튜플, :=, with ... as, 인자로 넘기기)은 None 이다."""
    parent = parents.get(call)
    if isinstance(parent, ast.Assign) and parent.value is call and len(parent.targets) == 1:
        [target] = parent.targets
        if isinstance(target, ast.Name):
            return target.id
    return None


def test_only_the_sandbox_loads_a_module_that_calls_c() -> None:
    roots = (*CPYTHON_CODE, MOCK_OIDC_CODE)
    found = {module: _code_matching(_loads(module), roots) for module in FFI_MODULES}

    assert found == {"ctypes": [SANDBOX], "_ctypes": [], "cffi": []}, (
        f"C 함수를 부르는 모듈을 쓰는 코드가 바뀌었다({found}). 그것이 libz 의 gz* 함수에 닿는지 "
        f"보고 {VEX.name} 의 zlib1g statement 를 다시 검토한다"
    )


def test_the_sandbox_reaches_ctypes_only_by_its_name() -> None:
    """`import ctypes` 만 쓰고 그 이름에서 SANDBOX_CTYPES 만 꺼낸다. `from ctypes import ...`,
    `import ctypes as c`, `x = ctypes` 는 다른 검사가 보는 이름을 비켜 가므로 거절한다."""
    tree = parse_python(REPO / SANDBOX)
    parents = _parents(tree)
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom) and "ctypes" in imported_modules(node)
    ]

    assert imports and all(
        isinstance(node, ast.Import)
        and [(alias.name, alias.asname) for alias in node.names] == [("ctypes", None)]
        for node in imports
    )
    stray = [
        ast.unparse(parents[use])
        for use in _uses(tree, "ctypes")
        if _stray(use, parents, SANDBOX_CTYPES)
    ]
    assert stray == []


def test_the_sandbox_opens_only_its_own_process() -> None:
    """CDLL 은 그 자리에서 None 으로만 부르고(`f = ctypes.CDLL` 은 거절), 그 결과는 이름 하나에
    담거나 그 자리에서 C 함수를 꺼낸다(`ctypes.CDLL(None).prctl`)."""
    tree = parse_python(REPO / SANDBOX)
    parents = _parents(tree)
    calls = _cdll_calls(tree)
    cdll = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "CDLL"]

    assert calls
    assert all(any(call.func is node for call in calls) for node in cdll), (
        "CDLL 을 부르지 않고 꺼내 둔다"
    )
    assert all(
        call.args and isinstance(call.args[0], ast.Constant) and call.args[0].value is None
        for call in calls
    ), "CDLL 에 None 이 아닌 이름을 준다"
    assert all(
        _bound_name(call, parents) or not _stray(call, parents, SANDBOX_C_FUNCTIONS)
        for call in calls
    ), "CDLL 의 결과를 이름 하나에 담지도, 그 자리에서 C 함수를 꺼내지도 않는다"


def test_the_sandbox_takes_only_prctl_from_the_handle() -> None:
    """핸들의 이름은 SANDBOX_C_FUNCTIONS 를 꺼내는 데만 읽고, CDLL 의 결과 밖의 것을 담지 않는다."""
    tree = parse_python(REPO / SANDBOX)
    parents = _parents(tree)
    binds = {call: _bound_name(call, parents) for call in _cdll_calls(tree)}
    handles = {name for name in binds.values() if name}
    uses = [use for name in sorted(handles) for use in _uses(tree, name)]

    assert handles, "CDLL(None) 을 담는 이름을 찾지 못했다"
    stray = [
        ast.unparse(parents.get(use, use))
        for use in uses
        if isinstance(use.ctx, ast.Load) and _stray(use, parents, SANDBOX_C_FUNCTIONS)
    ]
    assert stray == []
    # 핸들의 이름에 CDLL 의 결과 밖의 것을 담으면 그 값에서 꺼내는 것은 위 검사가 보지 못한다
    rebound = [
        ast.unparse(parents[use])
        for use in uses
        if not isinstance(use.ctx, ast.Load)
        and not any(parents[use] is parents.get(call) for call in binds)
    ]
    assert rebound == []


def test_our_code_opens_no_sqlite() -> None:
    found = _code_matching(SQLITE_NAMES, (*CPYTHON_CODE, MOCK_OIDC_CODE))

    assert found == [], (
        f"{found} 가 SQLite 를 열 수 있다. {VEX.name} 의 SQLite statement 를 다시 검토한다"
    )


@pytest.mark.parametrize("lock", ["platform", "mcp-runtime", "agent-runtime", "web-tools"])
def test_no_lock_brings_a_package_that_parses_xml_with_expat(lock: str) -> None:
    text = (REPO / lock / "uv.lock").read_text(encoding="utf-8")
    found = [name for name in EXPAT_USERS if f'\nname = "{name}"\n' in text]

    assert found == [], (
        f"{lock}/uv.lock 에 {found} 가 들어왔다. {VEX.name} 의 expat statement 를 다시 검토하고 "
        "이 목록을 고친다"
    )
