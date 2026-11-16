"""image-report.sh 의 판정과 줄 모양 (D55, D66). zot 레지스트리가 준 CVE 목록과 수용 목록(OpenVEX)만 본다.

image-report.sh 의 파이썬 조각이 `PYTHONPATH="$HERE/lib" python3 -` 로 읽고
platform/tests/test_image_report.py 가 단위 테스트한다. 맥의 시스템 python3(3.9)에서도
돌아야 하므로 3.10 이후 문법을 쓰지 않는다.

HIGH·CRITICAL 하나는 셋 가운데 하나다. 모든 패키지가 수용 목록에 있으면 "수용됨" 이고, 아니면서
고친 판이 나온 패키지가 있으면 "고칠 수 있다", 없으면 "새로 나옴" 이다. 수용은 (CVE, 바이너리
패키지 이름, 설치 판) 셋이 모두 맞을 때만이라 판이 바뀌면 다시 검토하게 된다. 목록은 고친 판이 나온
것도 받는다. 고친 판이 바탕(distroless)에 아직 닿지 않았을 때 검토한 statement 로 넘기는 길이다.
판정을 실패로 만드는 것은 수용되지 않은 CRITICAL 이다. HIGH 는 막지 않는다. 날마다 새 CVE 가
공개되므로 HIGH 까지 막으면 코드가 그대로여도 후보가 멈춘다. 막지 않은 HIGH 는 줄로 찍고
closing 이 끝 줄에 세어 릴리스 노트가 옮겨 적게 한다. 막을 것은 릴리스 전 미리 보기(옛 후보의
이미지)가 먼저 찾는다.

이미지 줄에는 서명 칸이 있다(D66). 서명은 image-report.sh 가 확인하고 여기서는 그 결과를 칸에
맞춰 찍기만 한다. 서명이 확인되지 않아도 판정은 실패하지 않는다. 그 이미지는 resolve-images.sh 가
거절한다.

CPython 을 싣는 다섯 이미지는 스캔이 보지 못하는 CPython 과 그 안에 묶인 라이브러리도 본다(P58, D70).
이미지 도장의 Dockerfile 핀으로 기록(security/cpython-builds.json, lib/cpython_builds.py)에서 판을
찾고, CPython 은 python.org 의 릴리스 목록에서 더 새 3.12 판을, OpenSSL·SQLite·zlib·expat 은 NVD
에서 그 판에 걸리는 CVE 를 찾는다. 찾은 것은 고친 판이 없는 것으로 보아 "새로 나옴" 이고
CRITICAL 이면 막는다. 수용 목록의 `pkg:generic/<구성요소>@<판>` statement 가 받으면 "수용됨" 이다.
조회는 요청마다 10초, 요청이 쓴 시간을 모두 합쳐 60초 안에서 한 번씩만 하고 자격 증명을 싣지
않는다. 조회가 실패하거나 답의 모양이 다르거나 시간이 다하면 그 구성요소는 "확인 못 함" 이고
판정을 실패시키지 않는다."""

from __future__ import annotations

import collections
import json
import re
import time
import unicodedata
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from cpython_builds import JUDGED, PLATFORMS, UNCHECKED, dockerfile_pins

# zot 의 심각도. 요약 줄이 이 순서로 찍고 판정은 앞의 둘만 본다
SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
SEVERE = SEVERITY_ORDER[:2]
# 이 상태의 statement 만 수용으로 센다. affected·under_investigation 은 수용이 아니다
ACCEPTING_STATUS = "not_affected"
# zot 가 값이 없는 칸에 넣는 글자
UNSPECIFIED = "Not Specified"

FIXABLE, ACCEPTED, NEW = "fixable", "accepted", "new"
# PEP 503 이 하나로 보는 이름 구분자의 연속
_PEP503 = re.compile(r"[-_.]+")
# 줄 머리. 셋의 표시 폭이 달라 Finding.line 이 가장 넓은 것에 맞춰 채운다
LABELS = {FIXABLE: "고칠 수 있다", ACCEPTED: "수용됨", NEW: "새로 나옴"}
# CPython 묶음의 줄 머리(P58). 판 줄, 조회하지 못한 구성요소, 판정하지 않는 구성요소다
BUILD_LABEL, UNKNOWN_LABEL, UNCHECKED_LABEL = "CPython 빌드", "확인 못 함", "보지 않음"
UNKNOWN_VERSIONS = "판 모름"
# 이미지 줄의 서명 칸. True 는 공개 키(deploy/cosign.pub)와 이미지 이름으로 서명이 확인된 이미지다.
# 서명이 없는 것, 다른 키로 서명된 것, 다른 이미지로 서명된 것은 모두 "서명 확인 안 됨" 이다
SIGNATURE = {True: "서명 확인됨", False: "서명 확인 안 됨"}


def display_width(text: str) -> int:
    """터미널에 찍히는 칸 수. 동아시아 넓은 글자(한글 등, W·F)는 두 칸이다."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def widest(texts: Iterable[str]) -> int:
    """texts 가운데 가장 넓은 것의 표시 폭. 칸을 맞출 때 그 폭으로 채운다."""
    return max(display_width(text) for text in texts)


LABEL_WIDTH = widest([*LABELS.values(), BUILD_LABEL, UNKNOWN_LABEL, UNCHECKED_LABEL])
SIGNATURE_WIDTH = widest(SIGNATURE.values())


def _pad(text: str, width: int) -> str:
    """text 뒤를 공백으로 채워 표시 폭 width 칸으로 만든다."""
    return text + " " * (width - display_width(text))


def image_line(image: str, width: int, signed: bool, result: str) -> str:
    """이미지 줄: 이미지, 서명 칸, result(취약점 건수나 결과가 없다는 말).

    이미지를 width 칸으로 채우므로 서명 칸과 result 가 이미지마다 같은 칸에서 시작한다.
    """
    return f"{_pad(image, width)}  {_pad(SIGNATURE[signed], SIGNATURE_WIDTH)}  {result}"


def summary(cves: list[dict]) -> str:
    """이미지 하나의 심각도별 건수(`HIGH 8, MEDIUM 28`). 하나도 없으면 "없음" 이다."""
    counts = collections.Counter(c["Severity"] for c in cves)
    return ", ".join(f"{s} {counts[s]}" for s in SEVERITY_ORDER if counts[s]) or "없음"


def fixed_version(pkg: dict) -> str:
    """zot 패키지 항목의 고친 판. 없으면 빈 글자다."""
    version = pkg.get("FixedVersion") or ""
    return "" if version == UNSPECIFIED else version


def pypi_name(name: str) -> str:
    """PEP 503 의 정규 이름. 소문자로 하고 `-`·`_`·`.` 가 이어진 자리는 `-` 하나로 바꾼다.

    zot 는 이미지에 깔린 dist-info 의 이름(`PyJWT`, `Authlib`)을, lock 을 읽는 Trivy 는 lock 의 이름
    (`pyjwt`, `authlib`)을 대므로 이 이름으로 맞대야 pkg:pypi statement 하나가 둘을 함께 받는다.
    """
    return _PEP503.sub("-", name).lower()


def purl_package(purl: str) -> tuple[str, str]:
    """purl -> (패키지 이름, 설치 판).

    판은 dpkg 처럼 epoch 를 `1:` 로 앞에 붙여 zot 의 InstalledVersion 과 같은 모양으로 만든다.
    Trivy 는 deb 의 epoch 를 판에서 떼어 qualifier `epoch` 로 싣기 때문이다. 판이 없는 purl 은
    모든 판을 덮으므로 받지 않는다. npm 의 이름은 scope 를 붙여(`@scope/name`) Trivy 의 PkgName 과
    같게 한다. pypi 의 이름은 PEP 503 정규 이름(pypi_name)으로 바꾼다. deb 의 namespace(`debian`)는
    배포판이라 이름에 넣지 않는다.
    """
    head, _, query = purl.partition("#")[0].partition("?")
    path, _, version = head.rpartition("@")
    if not path.startswith("pkg:") or not version:
        raise ValueError(f"판이 있는 purl 이 아니다: {purl}")
    if path.startswith("pkg:npm/"):
        name = urllib.parse.unquote(path[len("pkg:npm/") :])
    else:
        name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    if path.startswith("pkg:pypi/"):
        name = pypi_name(name)
    version = urllib.parse.unquote(version)
    epoch = dict(urllib.parse.parse_qsl(query)).get("epoch", "0")
    return name, version if epoch == "0" else f"{epoch}:{version}"


def load_accepted(path: str) -> dict[str, set[tuple[str, str]]]:
    """OpenVEX 파일 -> {CVE: {(패키지 이름, 설치 판)}}. not_affected statement 만 담는다."""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    accepted: dict[str, set[tuple[str, str]]] = {}
    for statement in doc.get("statements") or []:
        if statement.get("status") != ACCEPTING_STATUS:
            continue
        products = accepted.setdefault(statement["vulnerability"]["name"], set())
        products.update(purl_package(p["@id"]) for p in statement.get("products") or [])
    return accepted


# 수용 목록 (D55). 대상(이미지, 그 lock, 그 마지막 단계의 바탕)마다 하나만 판정한다. 코드 실행 작업
# 공간(sandbox/, P40)은 따로 검토한 목록으로만 판정한다. 그 이미지에서 도는 코드는 사용자의 것이라 다른
# 여섯 이미지의 근거("우리 코드가 그것을 부르지 않는다")가 서지 않기 때문이다. 경로는 저장소 뿌리 기준이다
VEX_FILES = {
    "base": "security/vex/base-images.openvex.json",
    "sandbox": "security/vex/sandbox.openvex.json",
}
SANDBOX_DIR = "sandbox/"


def vex_scope(path: str) -> str:
    """대상의 경로(Dockerfile 이나 lock, 저장소 뿌리 기준) -> 그것을 판정하는 수용 목록의 이름."""
    return "sandbox" if path.startswith(SANDBOX_DIR) else "base"


@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str
    cve: str
    detail: str
    # 수용됐지만 고친 판이 나와 있다. 바탕 고정을 옮기거나 statement 를 뺄 때가 왔다는 표시다
    fixed: bool = False

    @property
    def blocking(self) -> bool:
        """이미지 보고에서 판정을 실패로 만드는가. 수용되지 않은 CRITICAL 이다."""
        return self.severity == "CRITICAL" and self.kind != ACCEPTED

    @property
    def noted(self) -> bool:
        """막지 않지만 끝 줄에 세는가. 수용되지 않은 HIGH 다."""
        return self.kind != ACCEPTED and not self.blocking

    def line(self) -> str:
        label = _pad(LABELS[self.kind], LABEL_WIDTH)
        return f"   {label}  {self.severity} {self.cve}  {self.detail}"


class Judge:
    """이미지 여럿의 CVE 목록을 한 수용 목록으로 판정한다.

    어느 이미지에서도 맞지 않은 수용은 unseen() 이 알린다. 내린 판정은 judged 에 모아 closing 이
    끝 줄을 만든다.
    """

    def __init__(self, accepted: dict[str, set[tuple[str, str]]]):
        self.accepted = accepted
        self.seen: set[tuple[str, str, str]] = set()
        self.judged: list[Finding] = []

    def findings(self, cves: list[dict]) -> list[Finding]:
        """zot 의 CVEList 한 이미지 분 -> HIGH·CRITICAL 의 판정."""
        found = [self._judge(c) for c in cves if c["Severity"] in SEVERE]
        self.judged.extend(found)
        return found

    def _match(self, cid: str, name: str, version: str) -> tuple[str, str] | None:
        """(이름, 판) 을 받는 수용 항목. 이름 그대로, 아니면 PEP 503 정규 이름으로 찾는다.

        zot 의 PackageList 에는 생태계가 없어 pypi 인지 모른다. 정규 이름은 pkg:pypi 항목만 쓰므로
        deb 이름이 정규 이름과 우연히 같아도 CVE 와 판까지 맞아야 받는다.
        """
        known = self.accepted.get(cid, set())
        for key in ((name, version), (pypi_name(name), version)):
            if key in known:
                return key
        return None

    def _judge(self, cve: dict) -> Finding:
        cid, pkgs = cve["Id"], cve["PackageList"]
        installed = [(p["Name"], p["InstalledVersion"]) for p in pkgs]
        matched = {pkg: self._match(cid, *pkg) for pkg in installed}
        self.seen.update((cid, *key) for key in matched.values() if key is not None)
        fix = [p for p in pkgs if fixed_version(p)]
        want = ", ".join(sorted({f"{p['Name']} {fixed_version(p)}" for p in fix}))
        outside = sorted({f"{n} {v}" for (n, v), key in matched.items() if key is None})
        # 패키지가 없는 항목은 맞댈 것이 없으므로 수용하지 않는다. 수용한 줄도 고친 판을 적어
        # statement 를 뺄 때가 왔음을 보인다
        if installed and not outside:
            names = ", ".join(sorted({name for name, _ in installed}))
            detail = f"{names} (고친 판: {want})" if fix else names
            return Finding(ACCEPTED, cve["Severity"], cid, detail, fixed=bool(fix))
        if fix:
            names = ", ".join(sorted({p["Name"] for p in fix}))
            return Finding(FIXABLE, cve["Severity"], cid, f"{names} (고친 판: {want})")
        return Finding(NEW, cve["Severity"], cid, ", ".join(outside) or "패키지 정보 없음")

    def unknown(self, name: str, version: str) -> None:
        """이번에 조회하지 못한 (패키지, 판). 그 판의 수용 항목을 본 것으로 쳐 unseen() 이 "없다" 고
        알리지 않게 한다."""
        for cid, products in self.accepted.items():
            if (name, version) in products:
                self.seen.add((cid, name, version))

    def unseen(self) -> list[str]:
        """수용 목록에 있지만 이번에 잰 어느 이미지에도 없던 (패키지, 판) 을 CVE 마다 한 줄로."""
        lines = []
        for cid in sorted(self.accepted):
            gone = sorted(f"{n} {v}" for n, v in self.accepted[cid] if (cid, n, v) not in self.seen)
            if gone:
                lines.append(f"   {cid}  {', '.join(gone)}")
        return lines


class Judges:
    """수용 목록마다의 Judge. 대상의 경로로 고른다(`vex_scope`). files 는 {목록 이름: 경로} 이고 경로
    하나만 주면 그것이 `base` 다. 목록이 없는 범위의 대상은 아무것도 받지 않는 Judge 로 판정한다. 다른
    범위의 목록으로 받지 않게 하려는 것이다."""

    def __init__(self, files: Mapping[str, str] | str):
        self.files = {"base": files} if isinstance(files, str) else dict(files)
        self.by_scope = {scope: Judge(load_accepted(path)) for scope, path in self.files.items()}

    def for_path(self, path: str) -> Judge:
        return self.by_scope.setdefault(vex_scope(path), Judge({}))

    @property
    def judged(self) -> list[Finding]:
        return [f for judge in self.by_scope.values() for f in judge.judged]

    def unseen(self) -> list[str]:
        """목록마다 이번에 잰 어느 대상에도 없던 수용 항목. 목록의 경로를 머리로 한다."""
        lines: list[str] = []
        for scope, path in self.files.items():
            gone = self.by_scope[scope].unseen()
            if gone:
                lines.extend([f"  {path}:", *gone])
        return lines

    def hint(self) -> str:
        """끝 줄이 "검토해 ... 에 넣는다" 고 가리키는 목록들."""
        return " 또는 ".join(self.files.values())


def _kinds(findings: list[Finding]) -> str:
    """발견을 종류와 심각도마다 센 글자(`고칠 수 있다 HIGH 1, 새로 나옴 CRITICAL 2`)."""
    counts = collections.Counter((f.kind, f.severity) for f in findings)
    return ", ".join(
        f"{LABELS[kind]} {severity} {counts[kind, severity]}"
        for kind in (FIXABLE, NEW)
        for severity in SEVERE
        if counts[kind, severity]
    )


def report_blocks(finding: Finding) -> bool:
    """이미지 보고와 바탕 스캔의 막는 규칙: 수용되지 않은 CRITICAL."""
    return finding.blocking


def closing(
    judged: Iterable[Finding],
    missing: int,
    vex: str,
    *,
    what: str = "스캔 결과가 없는 이미지",
    name: str = "image-report",
    blocks: Callable[[Finding], bool] = report_blocks,
) -> tuple[list[str], int]:
    """판정의 끝 줄과 종료 코드. missing 은 결과가 없는 대상(what) 수이고 blocks 가 막는 규칙이다.

    같은 발견이 여러 이미지에 있으면 한 번만 센다. 막는 발견이나 결과가 없는 대상이 있으면 1 이다.
    마지막 줄은 늘 `막는 것 n, 막지 않은 것 m, 수용됐지만 고친 판 있음 k` 라 릴리스 전 점검이
    그 수를 옮겨 적는다. 이미지 보고, make deps-audit, make base-scan 이 함께 쓴다.
    """
    unique = list(dict.fromkeys(judged))
    blocking = [f for f in unique if f.kind != ACCEPTED and blocks(f)]
    noted = [f for f in unique if f.kind != ACCEPTED and not blocks(f)]
    fixed = [f for f in unique if f.kind == ACCEPTED and f.fixed]
    lines = []
    if noted:
        lines.append(
            f"{name}: 막지 않은 발견 {len(noted)} 건({_kinds(noted)}). 릴리스 노트에 적는다. "
            "고칠 수 있는 것은 의존이나 바탕 고정(make base-pins)을 올리고, "
            f"새로 나온 것은 검토해 고치거나 {vex} 에 넣는다"
        )
    if blocking:
        lines.append(
            f"{name}: 막는 발견 {len(blocking)} 건({_kinds(blocking)}). "
            "의존이나 바탕 고정을 올려 다시 굽는다. "
            f"고친 판이 아직 이미지에 닿지 않으면 검토해 {vex} 에 넣는다"
        )
    if fixed:
        lines.append(
            f"{name}: 수용됐지만 고친 판이 있는 것 {len(fixed)} 건. "
            "바탕 패키지면 make base-pins-check 로 고정이 낡았는지 본다. "
            f"고친 판이 이미지에 닿으면 {vex} 의 statement 를 뺀다"
        )
    if missing:
        lines.append(f"{name}: {what} {missing} 개")
    code = 1 if blocking or missing else 0
    verdict = "실패" if code else "통과"
    lines.append(
        f"{name}: 막는 것 {len(blocking) + missing}, 막지 않은 것 {len(noted)}, "
        f"수용됐지만 고친 판 있음 {len(fixed)}. {verdict}"
    )
    return lines, code


def step_summary(judged: Iterable[Finding], lines: list[str]) -> tuple[str, str | None]:
    """GitHub Actions 에서 돌 때 단계 요약에 더할 마크다운과 경고 한 줄.

    요약에는 수용되지 않은 발견과 수용됐지만 고친 판이 있는 발견(한 번씩), 끝 줄을 싣는다.
    경고는 그런 발견이 있을 때만 하나다.
    """
    unique = [f for f in dict.fromkeys(judged) if f.kind != ACCEPTED or f.fixed]
    body = ["### image-report", "", "```", *(f.line().strip() for f in unique), *lines, "```", ""]
    warning = None
    if unique:
        warning = f"::warning title=image-report::{lines[-1]}. 단계 요약에 발견이 있다"
    return "\n".join(body), warning


# -- CPython 과 묶인 라이브러리 (P58, D70) ------------------------------------------------------

# 묶인 라이브러리마다 NVD 의 CPE (vendor, product)
CPE = {
    "openssl": ("openssl", "openssl"),
    "sqlite": ("sqlite", "sqlite"),
    "zlib": ("zlib", "zlib"),
    "expat": ("libexpat_project", "libexpat"),
}
NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0?virtualMatchString=cpe:2.3:a:{}:{}:{}"
PYTHON_RELEASES_URL = (
    "https://www.python.org/api/v2/downloads/release/?is_published=true&pre_release=false"
)
LOOKUP_TIMEOUT = 10.0  # 요청 하나
LOOKUP_BUDGET = 60.0  # 보고 하나의 모든 요청이 쓴 시간의 합
# NVD 의 CVSS 판. 모든 판의 모든 점수를 본다
CVSS_KEYS = ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2")
# CVSS 심각도의 높낮이. 판이나 출처(Primary·Secondary)마다 다르면 가장 높은 것을 쓴다
SEVERITY_RANK = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
# 미리 보기 판의 표시와 그 순서. 이것이 붙은 판은 같은 수의 정식 판보다 앞이다(3.13.0rc1 < 3.13.0)
PRE_RELEASE = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "c": 3, "rc": 3, "pre": 3}
_PYTHON_NAME = re.compile(r"^Python (\d+)\.(\d+)\.(\d+)$")

Fetch = Callable[[str, float], bytes]


def _part_key(part: str) -> tuple:
    """판 마디 하나의 비교 열쇠 (수, 갈래, 미리 보기 순서, 글자, 뒤의 수, 나머지).

    갈래는 미리 보기 마디(`0rc1`, `rc1`, `beta`)가 -1, 수뿐인 마디가 0, 다른 글자가 붙은
    마디(OpenSSL 의 `1w`)가 1 이다. 한 글자 표시(a, b, c)는 뒤에 수가 있을 때만 미리 보기다.
    OpenSSL 의 1.1.1a 는 1.1.1 의 다음 판이기 때문이다."""
    m = re.match(r"(\d*)([A-Za-z]*)(\d*)", part)
    number, letters, tail = m.groups()
    rest = part[m.end() :]
    word = letters.lower()
    if word in PRE_RELEASE and (tail or not number or len(word) > 1):
        return (int(number or 0), -1, PRE_RELEASE[word], "", int(tail or 0), rest)
    return (int(number or 0), 1 if letters or rest else 0, 0, word, int(tail or 0), rest)


_ZERO = _part_key("0")


def version_key(version: str) -> tuple:
    """`3.53.1`, `1.1.1w`, `3.13.0rc1` 같은 판의 비교 열쇠. 마디마다 _part_key 다. CPE 가 판 안의
    기호 앞에 붙이는 `\\` 는 뗀다."""
    return tuple(_part_key(p) for p in re.split(r"[.\-+_]", version.replace("\\", "")))


def compare_versions(a: str, b: str) -> int:
    """a 가 b 보다 앞이면 -1, 같으면 0, 뒤면 1. 짧은 쪽에 0 마디를 채운다(1.0 = 1.0.0)."""
    ka, kb = list(version_key(a)), list(version_key(b))
    width = max(len(ka), len(kb))
    ka += [_ZERO] * (width - len(ka))
    kb += [_ZERO] * (width - len(kb))
    return (ka > kb) - (ka < kb)


def cpe_covers(match: dict, vendor: str, product: str, version: str) -> bool:
    """NVD configurations 의 cpeMatch 하나가 그 제품의 그 판을 취약하다고 하는가.

    `vulnerable` 이 거짓인 것(다른 제품이 그 위에서 돌 때만 걸리는 조합의 한쪽)은 보지 않는다.
    판이 박힌 CPE 는 그 판만 덮고, update 칸이 있으면(`3.5.9:beta1`) 그 미리 보기 판만 덮는다.
    `*` 인 CPE 는 범위가 있을 때 그 범위만 덮는다. 범위도 판도 없는 `*` 는 모든 판을 덮는다고
    적힌 것이라 덮는다고 본다."""
    parts = match.get("criteria", "").split(":")
    if len(parts) < 6 or parts[3] != vendor or parts[4] != product or not match.get("vulnerable"):
        return False
    if parts[5] not in ("*", "-"):
        update = parts[6] if len(parts) > 6 else "*"
        named = parts[5] if update in ("*", "-") else f"{parts[5]}-{update}"
        return compare_versions(named, version) == 0
    if (
        "versionStartIncluding" in match
        and compare_versions(version, match["versionStartIncluding"]) < 0
    ):
        return False
    if (
        "versionStartExcluding" in match
        and compare_versions(version, match["versionStartExcluding"]) <= 0
    ):
        return False
    if (
        "versionEndIncluding" in match
        and compare_versions(version, match["versionEndIncluding"]) > 0
    ):
        return False
    if (
        "versionEndExcluding" in match
        and compare_versions(version, match["versionEndExcluding"]) >= 0
    ):
        return False
    return True


def _severity(cve: dict) -> str:
    """NVD 항목의 심각도. 모든 CVSS 판의 모든 점수(Primary·Secondary) 가운데 가장 높은 것이다.
    판정은 HIGH·CRITICAL 만 보므로 한 출처라도 높게 매겼으면 그것을 따른다."""
    found = []
    for key in CVSS_KEYS:
        for row in (cve.get("metrics") or {}).get(key) or []:
            severity = (row.get("cvssData") or {}).get("baseSeverity") or row.get("baseSeverity")
            if isinstance(severity, str) and severity.upper() in SEVERITY_RANK:
                found.append(severity.upper())
    return max(found, key=SEVERITY_RANK.__getitem__, default="UNKNOWN")


def nvd_page(doc: object) -> tuple[list, int]:
    """NVD CVE API 응답 한 쪽 -> (vulnerabilities, totalResults). 모양이 다르면 LookupError 다."""
    if isinstance(doc, dict):
        items, total = doc.get("vulnerabilities"), doc.get("totalResults")
        if isinstance(items, list) and type(total) is int:
            return items, total
    raise LookupError("NVD 응답에 vulnerabilities 목록과 totalResults 수가 없다")


def nvd_cves(items: list, component: str, version: str) -> list[dict]:
    """NVD 응답의 vulnerabilities 항목들 -> 그 구성요소의 그 판이 취약한 CVE 를 zot 의 CVEList 꼴로.

    고친 판은 비운다. NVD 에 고친 판이 있어도 uv 가 그 판으로 구운 빌드를 받을 수 있다는 뜻은 아니기
    때문이다. cve.id 가 없는 항목이 있으면 LookupError 다."""
    vendor, product = CPE[component]
    out = []
    for item in items:
        cve = item.get("cve") if isinstance(item, dict) else None
        if not isinstance(cve, dict) or not isinstance(cve.get("id"), str):
            raise LookupError("NVD 응답의 vulnerabilities 항목에 cve.id 가 없다")
        matches = [
            m
            for conf in cve.get("configurations") or []
            for node in conf.get("nodes") or []
            for m in node.get("cpeMatch") or []
        ]
        if any(cpe_covers(m, vendor, product, version) for m in matches):
            package = {"Name": component, "InstalledVersion": version, "FixedVersion": ""}
            out.append({"Id": cve["id"], "Severity": _severity(cve), "PackageList": [package]})
    return out


def newer_cpython(releases: object, version: str) -> str | None:
    """python.org 의 릴리스 목록 -> 같은 마이너(3.12)에서 version 보다 새 판 가운데 가장 새것.
    없으면 None 이다. 목록에 기록된 판(`Python <version>`)이 없으면 잘렸거나 다른 답이라
    LookupError 다."""
    if not isinstance(releases, list) or not all(isinstance(r, dict) for r in releases):
        raise LookupError("python.org 의 답이 릴리스 목록이 아니다")
    names = [r.get("name") for r in releases]
    if f"Python {version}" not in names:
        raise LookupError(f"python.org 의 릴리스 목록에 기록된 판(Python {version})이 없다")
    major_minor = version.split(".")[:2]
    best = None
    for name in names:
        m = _PYTHON_NAME.match(name) if isinstance(name, str) else None
        if not m or [m.group(1), m.group(2)] != major_minor:
            continue
        candidate = ".".join(m.groups())
        if compare_versions(candidate, version) > 0 and (
            best is None or compare_versions(candidate, best) > 0
        ):
            best = candidate
    return best


def cpython_cves(newer: str | None, version: str) -> list[dict]:
    """더 새 3.12 판을 HIGH 하나로 적는다. 3.12 갈래는 보안 판만 나오므로 심각도를 찾지 않는다."""
    if newer is None:
        return []
    package = {"Name": "cpython", "InstalledVersion": version, "FixedVersion": ""}
    return [{"Id": f"Python-{newer}", "Severity": "HIGH", "PackageList": [package]}]


class Advisories:
    """보고 하나의 조회. (구성요소, 판)마다 한 번만, 요청마다 LOOKUP_TIMEOUT 안에서 묻는다.

    요청이 쓴 시간을 모두 합쳐 LOOKUP_BUDGET 을 넘기면 더 묻지 않는다. 요청 밖의 시간(zot 의 스캔을
    기다리는 시간 등)은 세지 않는다. fetch(url, timeout) 는 응답 본문을 돌려주고 실패하면 예외를
    던진다. 답이 JSON 이 아니거나 모양이 다르면 실패와 같이 LookupError 다."""

    def __init__(
        self,
        fetch: Fetch,
        budget: float = LOOKUP_BUDGET,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.fetch, self.budget, self.clock = fetch, budget, clock
        self.spent = 0.0
        self.cache: dict[tuple[str, str], object] = {}

    def _get(self, url: str) -> object:
        left = self.budget - self.spent
        if left <= 0:
            raise LookupError("조회 시간 예산을 다 썼다")
        start = self.clock()
        try:
            body = self.fetch(url, min(LOOKUP_TIMEOUT, left))
        finally:
            self.spent += self.clock() - start
        try:
            return json.loads(body)
        except ValueError as e:
            raise LookupError(f"답이 JSON 이 아니다 ({e})") from e

    def _nvd(self, component: str, version: str) -> list:
        """NVD 의 답을 쪽마다 받아 모은다. 모은 수가 totalResults 와 다르면 LookupError 다."""
        url = NVD_URL.format(*CPE[component], version)
        items: list = []
        while True:
            page, total = nvd_page(self._get(f"{url}&startIndex={len(items)}" if items else url))
            items += page
            if not page or len(items) >= total:
                break
        if len(items) != total:
            raise LookupError(f"NVD 가 {total} 건이라 하면서 {len(items)} 건을 줬다")
        return items

    def cves(self, component: str, version: str) -> list[dict]:
        """그 구성요소의 그 판에 걸리는 것. 조회하지 못하면 까닭을 담은 LookupError 다."""
        key = (component, version)
        if key not in self.cache:
            try:
                if component == "cpython":
                    newer = newer_cpython(self._get(PYTHON_RELEASES_URL), version)
                    self.cache[key] = cpython_cves(newer, version)
                else:
                    items = self._nvd(component, version)
                    self.cache[key] = nvd_cves(items, component, version)
            except Exception as e:  # noqa: BLE001 - 조회 실패는 보고를 멈추지 않는다
                # KeyError·IndexError 도 LookupError 라 우리가 낸 것만 까닭을 그대로 싣는다
                why = str(e) if type(e) is LookupError else f"{type(e).__name__}: {e}"
                self.cache[key] = LookupError(why)
        result = self.cache[key]
        if isinstance(result, LookupError):
            raise result
        return result


def note_line(label: str, text: str) -> str:
    """판정이 아닌 줄(판, 확인 못 함, 보지 않음). 판정 줄과 같은 칸에서 시작한다."""
    return f"   {_pad(label, LABEL_WIDTH)}  {text}"


def bundled(
    judge: Judge,
    advisories: Advisories,
    builds: dict[tuple[str, str, str], dict],
    pins: tuple[str, str] | None,
    reason: str = "",
) -> list[str]:
    """CPython 이미지 하나의 묶음 판정 줄. 판정은 judge 의 judged 에 쌓인다.

    pins 가 None 이면 판을 모르는 이미지이고 reason 이 그 까닭이다.
    """
    if pins is None:
        why = reason or "이미지 태그의 핀을 읽지 못했다"
        return [note_line(BUILD_LABEL, f"{UNKNOWN_VERSIONS} ({why})")]
    found = [builds.get((pins[0], pins[1], platform)) for platform in PLATFORMS]
    entries = [e for e in found if e is not None]
    if len(entries) != len(found):
        why = f"security/cpython-builds.json 에 uv {pins[0]}·CPython {pins[1]} 기록이 없다"
        return [note_line(BUILD_LABEL, f"{UNKNOWN_VERSIONS} ({why})")]
    lines: list[str] = []
    # 플랫폼마다 판이 같으면 한 줄, 다르면 줄마다 적고 다른 판은 따로 묻는다
    asked: list[tuple[str, str]] = []
    for e in entries:
        v = e["versions"]
        libs = ", ".join(f"{c} {v[c]}" for c in JUDGED[1:])
        line = note_line(
            BUILD_LABEL, f"{v['cpython']} (python-build-standalone {e['release']}): {libs}"
        )
        if line not in lines:
            lines.append(line)
        for c in JUDGED:
            if (c, v[c]) not in asked:
                asked.append((c, v[c]))
    for component, version in asked:
        try:
            cves = advisories.cves(component, version)
        except LookupError as e:
            judge.unknown(component, version)
            lines.append(note_line(UNKNOWN_LABEL, f"{component} {version} ({e})"))
            continue
        lines.extend(finding.line() for finding in judge.findings(cves))
    versions = entries[0]["versions"]
    unchecked = [f"{c} {versions[c]}" if c in versions else c for c in UNCHECKED]
    lines.append(note_line(UNCHECKED_LABEL, ", ".join(unchecked)))
    return lines


def stamp_pins(
    tag: str, current: str, at_tag: str | None
) -> tuple[tuple[str, str] | None, str] | None:
    """이미지 하나의 핀. current 는 지금 Dockerfile, at_tag 는 이미지 태그(도장이나 릴리스 태그)
    시점의 것이다.

    지금 Dockerfile 에 핀이 없으면 CPython 을 싣지 않는 이미지라 None 이다. 태그 시점의
    Dockerfile 을 이 저장소에서 찾지 못했거나 거기에 핀이 없으면 (None, 까닭) 이고, 있으면
    (그때의 핀, "") 이다."""
    if dockerfile_pins(current) is None:
        return None
    if at_tag is None:
        return None, f"태그 {tag} 시점의 Dockerfile 을 이 저장소에서 찾지 못했다"
    pins = dockerfile_pins(at_tag)
    if pins is None:
        return None, f"태그 {tag} 시점의 Dockerfile 에 uv 핀이나 CPYTHON_VERSION 이 없다"
    return pins, ""
