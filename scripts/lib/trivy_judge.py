"""make deps-audit 와 make base-scan 의 판정 (D55). 고정한 Trivy 의 답을 이미지 보고의 Judge 로
판정한다.

scripts/deps-audit.sh 와 scripts/base-scan.sh 가 Trivy 의 JSON 을 넘기고
platform/tests/test_trivy_judge.py 가 시험한다. 두 점검은 태그를 밀기 전에 돈다. 이미지 보고는
태그를 민 뒤에 돌므로, 실패할 수 있는 것은 그 전에 여기서 찾아 후보 번호를 버리지 않게 한다.
맥의 시스템 python3(3.9)에서도 돌아야 하므로 3.10 이후 문법을 쓰지 않는다.

- 수용 목록은 이미지 보고와 같은 security/vex/ 의 목록이고 대상의 범위마다 하나다
  (image_report.py `VEX_FILES`, `vex_scope`). 코드 실행 작업 공간(sandbox/)의 lock 과 바탕은
  sandbox.openvex.json 으로만, 나머지는 base-images.openvex.json 으로만 판정한다. Trivy 도 `--vex`
  로 목록을 읽지만 숨긴 발견은 `--show-suppressed` 로 다시 받아 Judge 가 대상의 목록으로 "수용됨"
  을 정하므로, Trivy 에 준 목록은 판정을 바꾸지 않는다.
- deps-audit 는 lock(파이썬 여섯, ui 의 npm)에서 고친 판이 나온 HIGH·CRITICAL 과 수용되지 않은
  CRITICAL 에서 실패한다. 의존을 올리면 없어지기 때문이다.
- base-scan 은 제품 Dockerfile 마지막 단계의 바탕을 고정한 digest 로 보고 이미지 보고의
  규칙(수용되지 않은 CRITICAL)으로 판정한다. ui·mock-oidc·sandbox 는 굽기의 마지막 단계가 OS 패키지를
  올리므로 고친 판이 있는 패키지는 빼고 센다. 다만 nginx.org 에서 온 nginx 패키지는 올리기가 닿지
  않아 그대로 센다. CPython 이미지 다섯의 묶인 라이브러리는 지금 Dockerfile 의 핀으로 bundled 가 본다.

    python3 scripts/lib/trivy_judge.py deps <trivy.json> <lock>...
    python3 scripts/lib/trivy_judge.py bases <Dockerfile>...     바탕마다 `바탕 올림 목록[,목록]`
    python3 scripts/lib/trivy_judge.py base <builds> <바탕=json=올림>... -- <Dockerfile>...

저장소 뿌리에서 부른다. 목록의 경로가 뿌리 기준이다.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping

from base_pins import from_refs
from cpython_builds import dockerfile_pins, load_builds
from image_report import (
    ACCEPTED,
    FIXABLE,
    VEX_FILES,
    Advisories,
    Finding,
    Judges,
    bundled,
    closing,
    fixed_version,
    summary,
    vex_scope,
)

# 굽기의 마지막 단계가 OS 패키지를 올리는 Dockerfile 의 표시. ui 의 apk upgrade 와 mock-oidc·sandbox 의
# apt-get upgrade 앞에 있다
UPGRADE_MARK = "ARG OS_PATCH_DATE"


def upgrade_reaches(name: str) -> bool:
    """굽기의 패키지 올리기가 그 패키지에 닿는가. nginx 와 nginx-module-* 는 닿지 않는다.

    nginx-unprivileged 바탕은 그 패키지를 nginx.org 저장소에서 한 번만 받고(`apk add -X`) 저장소
    목록에 남기지 않는다. 그래서 apk upgrade 는 Alpine 저장소의 패키지만 올리고, nginx 의 고친 판은
    nginx-unprivileged 의 태그를 올려야 온다.
    """
    return name != "nginx" and not name.startswith("nginx-module-")


def deps_blocks(finding: Finding) -> bool:
    """deps-audit 의 막는 규칙: 수용되지 않은 것 가운데 고친 판이 있거나 CRITICAL 인 것."""
    return finding.kind != ACCEPTED and (finding.kind == FIXABLE or finding.severity == "CRITICAL")


def trivy_cves(result: dict) -> list[dict]:
    """Trivy 의 Result 하나 -> zot 모양의 CVE 목록. VEX 가 숨기고 `--show-suppressed` 가 돌려준
    것도 넣는다.

    Trivy 는 패키지마다 한 항목이고 zot 는 CVE 마다 PackageList 를 둔다. (CVE, 심각도) 로 묶는다.
    """
    vulns = list(result.get("Vulnerabilities") or [])
    for modified in result.get("ExperimentalModifiedFindings") or []:
        if modified.get("Type") == "vulnerability":
            vulns.append(modified["Finding"])
    grouped: dict[tuple[str, str], list[dict]] = {}
    for v in vulns:
        packages = grouped.setdefault((v["VulnerabilityID"], v["Severity"]), [])
        packages.append(
            {
                "Name": v["PkgName"],
                "InstalledVersion": v["InstalledVersion"],
                "FixedVersion": v.get("FixedVersion") or "",
            }
        )
    return [
        {"Id": cid, "Severity": severity, "PackageList": packages}
        for (cid, severity), packages in grouped.items()
    ]


def deps(doc: dict, vex: Mapping[str, str] | str, locks: list[str]) -> tuple[list[str], int]:
    """deps-audit 의 Trivy 답 -> (찍을 줄, 종료 코드). Trivy 가 읽지 못한 lock 도 실패다. lock 마다
    그 범위의 수용 목록으로 판정한다(`vex_scope`)."""
    results = {r.get("Target"): r for r in doc.get("Results") or []}
    judges = Judges(vex)
    lines: list[str] = []
    for lock in locks:
        if lock not in results:
            lines.append(f"   읽지 못함  {lock}")
            continue
        found = judges.for_path(lock).findings(trivy_cves(results[lock]))
        lines.append(f"   {lock}  {len(found)} 건")
        lines.extend(f.line() for f in found)
    missing = sum(lock not in results for lock in locks)
    tail, code = closing(
        judges.judged,
        missing,
        judges.hint(),
        what="Trivy 가 읽지 못한 lock",
        name="deps-audit",
        blocks=deps_blocks,
    )
    return lines + tail, code


def base_vex(users: list[str]) -> list[str]:
    """바탕을 쓰는 Dockerfile 들의 수용 목록. Trivy 의 --vex 가 이것을 읽는다. 숨긴 것은
    --show-suppressed 로 돌아오고 판정은 늘 `base` 가 범위마다의 Judge 로 하므로, Trivy 에 준 목록이
    판정을 바꾸지는 않는다."""
    return sorted({VEX_FILES[vex_scope(path)] for path in users}) or [VEX_FILES["base"]]


def final_bases(dockerfiles: dict[str, str]) -> dict[str, tuple[list[str], bool]]:
    """{Dockerfile 경로: 내용} -> {마지막 단계의 바탕: (쓰는 Dockerfile, OS 패키지를 올리는가)}."""
    bases: dict[str, tuple[list[str], bool]] = {}
    for path, text in dockerfiles.items():
        ref = from_refs(text)[-1]
        users, upgrades = bases.get(ref, ([], False))
        bases[ref] = ([*users, path], upgrades or UPGRADE_MARK in text)
    return bases


def without_upgraded(cves: list[dict]) -> tuple[list[dict], int]:
    """굽기가 OS 패키지를 올리는 바탕의 CVE 에서 고친 판이 있는 패키지를 뺀다.

    -> (남은 CVE, 다 빠진 CVE 수). 패키지 올리기가 그 판을 받으므로 이미지에는 남지 않는다.
    고친 판이 없는 패키지와 올리기가 닿지 않는 패키지(upgrade_reaches)는 그대로 판정한다.
    """
    kept, dropped = [], 0
    for cve in cves:
        packages = [
            p for p in cve["PackageList"] if not (fixed_version(p) and upgrade_reaches(p["Name"]))
        ]
        if not packages:
            dropped += 1
        elif len(packages) != len(cve["PackageList"]):
            kept.append({**cve, "PackageList": packages})
        else:
            kept.append(cve)
    return kept, dropped


def base(
    vex: Mapping[str, str] | str,
    builds_path: str,
    scans: list[tuple[str, dict, bool, list[str]]],
    pins: Mapping[str, tuple[str, str]],
    advisories: Advisories,
) -> tuple[list[str], int]:
    """base-scan 의 판정 -> (찍을 줄, 종료 코드).

    scans 는 (바탕, Trivy 답, 굽기가 OS 패키지를 올리는가, 쓰는 Dockerfile) 이고 pins 는 지금
    Dockerfile 마다의 (uv, CPython) 핀이다. 바탕과 CPython 묶음은 그것을 쓰는 Dockerfile 의 범위마다
    그 범위의 수용 목록으로 판정한다. 범위 둘이 같은 바탕을 쓰면 범위마다 한 번씩 판정한다.
    """
    judges = Judges(vex)
    lines: list[str] = []
    for ref, doc, upgrades, users in scans:
        cves = [c for r in doc.get("Results") or [] for c in trivy_cves(r)]
        lines.append(f">> 바탕 {ref} ({', '.join(users)})  취약점 {summary(cves)}")
        if upgrades:
            cves, dropped = without_upgraded(cves)
            if dropped:
                lines.append(f"   고친 판이 있는 {dropped} 건은 굽기의 OS 패키지 올리기가 받는다")
        for user in _one_per_scope(users or [""]):
            lines.extend(f.line() for f in judges.for_path(user).findings(cves))
    builds = load_builds(builds_path)
    by_pins: dict[tuple[str, str], list[str]] = {}
    for path, found in pins.items():
        by_pins.setdefault(found, []).append(path)
    for (uv, cpython), paths in by_pins.items():
        lines.append(f">> CPython 빌드 (지금 Dockerfile 의 핀 uv {uv}, CPython {cpython})")
        for user in _one_per_scope(paths):
            lines.extend(bundled(judges.for_path(user), advisories, builds, (uv, cpython)))
    tail, code = closing(judges.judged, 0, judges.hint(), name="base-scan")
    return lines + tail, code


def _one_per_scope(paths: list[str]) -> list[str]:
    """범위마다 경로 하나. 차례는 처음 나온 차례다."""
    first: dict[str, str] = {}
    for path in paths:
        first.setdefault(vex_scope(path), path)
    return list(first.values())


def _read(paths: list[str]) -> dict[str, str]:
    texts = {}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            texts[path] = f.read()
    return texts


def _main(argv: list[str]) -> int:
    command, args = argv[0], argv[1:]
    if command == "deps":
        with open(args[0], encoding="utf-8") as f:
            out, code = deps(json.load(f), VEX_FILES, args[1:])
    elif command == "bases":
        for ref, (users, upgrades) in final_bases(_read(args)).items():
            print(ref, int(upgrades), ",".join(base_vex(users)))
        return 0
    else:
        from public_fetch import fetch

        split = args.index("--")
        builds_path, specs = args[0], args[1:split]
        texts = _read(args[split + 1 :])
        users = {ref: paths for ref, (paths, _upgrades) in final_bases(texts).items()}
        scans = []
        for spec in specs:
            ref, path, upgrades = spec.rsplit("=", 2)
            with open(path, encoding="utf-8") as f:
                scans.append((ref, json.load(f), upgrades == "1", users.get(ref, [])))
        pins = {p: found for p, t in texts.items() if (found := dockerfile_pins(t)) is not None}
        out, code = base(VEX_FILES, builds_path, scans, pins, Advisories(fetch))
    print("\n".join(out))
    return code


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
