"""이미지에 실리는 CPython 과 그 안에 묶인 라이브러리의 판 기록 (P58, D70).

플랫폼, MCP 런타임, Agent 런타임 이미지는 uv 가 받는 python-build-standalone 의 CPython 을
싣는다(D55). uv 판과 CPYTHON_VERSION 두 핀이 받을 빌드를 정하고, 그 빌드가 OpenSSL·SQLite·zlib·expat
같은 라이브러리의 판을 정한다. 레지스트리의 스캔은 이것을 보지 못하므로 저장소의 기록
`security/cpython-builds.json` 이 (uv 판, CPython 판, 플랫폼)마다 그 판을 든다. make
cpython-record(scripts/cpython-record.sh)가 기록을 쓰고, make image-report(lib/image_report.py)가
읽는다.

amd64 에서는 uv 가 빌드하는 기계의 CPU 에 맞춰 x86_64·x86_64_v2·v3·v4 가운데 하나를 받는다. 한
python-build-standalone 릴리스의 변형은 같은 라이브러리 판으로 굽히므로 판은 기준 변형 하나를 돌려
재고, 받을 수 있는 변형마다 주소와 sha256 을 함께 적는다. 받기와 풀기는 핀의 uv 가 이미지 빌드처럼
`uv python install` 로 하고, uv 가 남긴 파일로 sha256 을 맞댄다(cache_name).

cpython-record.sh 의 파이썬 조각이 `PYTHONPATH="$HERE/lib" python3 -` 로 읽고
platform/tests/test_cpython_builds.py 가 단위 테스트한다. 맥의 시스템 python3(3.9)에서도 돌아야
하므로 3.10 이후 문법을 쓰지 않는다."""

from __future__ import annotations

import json
import re
import urllib.parse

# 이미지를 올리는 두 플랫폼과 uv 의 아키텍처 이름
PLATFORMS = {"linux/amd64": "x86_64", "linux/arm64": "aarch64"}
# image-report 가 판정하는 구성요소. cpython 은 판을 비교하고 나머지는 공개 취약점 목록으로 본다
JUDGED = ("cpython", "openssl", "sqlite", "zlib", "expat")
# 묶여 오지만 판정하지 않는 구성요소. 판을 아는 것은 판과 함께 찍는다
UNCHECKED = ("xz", "bzip2", "libffi", "ncurses", "readline", "tcl_tk", "uuid")
# 컨테이너 안에서 기준 변형의 인터프리터가 찍는 판. 이미지 안내서 4절의 파드 한 줄과 같은 식이다
VERSION_SCRIPT = (
    "import json, platform, ssl, zlib, pyexpat, sqlite3, curses, tkinter\n"
    "print(json.dumps({"
    '"cpython": platform.python_version(), "openssl": ssl.OPENSSL_VERSION, '
    '"zlib": zlib.ZLIB_RUNTIME_VERSION, "expat": pyexpat.EXPAT_VERSION, '
    '"sqlite": sqlite3.sqlite_version, '
    '"ncurses": ".".join(map(str, curses.ncurses_version)), '
    '"tcl_tk": str(tkinter.TkVersion)}))'
)
_UV_PIN = re.compile(r"\buv==([0-9][0-9A-Za-z.+-]*)")
_CPYTHON_PIN = re.compile(r"^ARG CPYTHON_VERSION=([0-9][0-9.]*)\s*$", re.MULTILINE)
_OPENSSL = re.compile(r"^OpenSSL (\S+)")
_EXPAT = re.compile(r"^expat_(\S+)$")


def dockerfile_pins(text: str) -> tuple[str, str] | None:
    """Dockerfile -> (uv 판, CPython 판). 둘 중 하나라도 없으면 None 이다(CPython 을 싣지 않는
    이미지)."""
    uv, cpython = _UV_PIN.search(text), _CPYTHON_PIN.search(text)
    if uv is None or cpython is None:
        return None
    return uv.group(1), cpython.group(1)


def downloads(uv_list: list[dict], cpython: str, family: str) -> list[dict]:
    """`uv python list --all-platforms --all-arches --output-format json` 의 항목 가운데 그 CPython
    판의 linux gnu 기본 빌드로 아키텍처가 family(x86_64 면 _v2 같은 변형까지)인 것 -> [{key, url}].
    """
    out = []
    for e in uv_list:
        arch = e.get("arch") or ""
        if (
            e.get("implementation") == "cpython"
            and e.get("version") == cpython
            and e.get("os") == "linux"
            and e.get("libc") == "gnu"
            and e.get("variant") == "default"
            and e.get("url")
            and (arch == family or arch.startswith(family + "_v"))
        ):
            out.append({"key": e["key"], "url": e["url"]})
    return sorted(out, key=lambda d: d["key"])


def release_of(url: str) -> str:
    """python-build-standalone 다운로드 주소 -> 릴리스 태그(`.../releases/download/<태그>/...`)."""
    parts = urllib.parse.urlparse(url).path.split("/")
    return parts[parts.index("download") + 1]


def sums_url(url: str) -> str:
    """다운로드 주소와 같은 릴리스의 SHA256SUMS 주소."""
    return url.rsplit("/", 1)[0] + "/SHA256SUMS"


def file_of(url: str) -> str:
    """다운로드 주소의 파일 이름(퍼센트 인코딩을 푼다)."""
    return urllib.parse.unquote(url.rsplit("/", 1)[1])


def cache_name(url: str, sha256: str) -> str:
    """uv 가 UV_PYTHON_CACHE_DIR 에 남기는 파일 이름(uv 0.12.23 에서 잰 꼴). 앞의 9자는 uv 가
    품은 sha256 의 앞자리이고 파일 이름의 `+` 는 `-` 다. 기록의 sha256 으로 만든 이 이름의 파일이
    있으면 uv 가 품은 값과 앞자리가 같다."""
    return f"{sha256[:9]}-{file_of(url).replace('+', '-')}"


def parse_sums(text: str) -> dict[str, str]:
    """`<sha256>  <파일 이름>` 줄들 -> {파일 이름: sha256}."""
    out = {}
    for line in text.splitlines():
        digest, _, name = line.strip().partition("  ")
        if digest and name:
            out[name.strip()] = digest
    return out


def normalize(raw: dict[str, str]) -> dict[str, str]:
    """인터프리터가 찍은 판 -> 기록하는 판. `OpenSSL 3.5.9 29 Sep 2026` 은 `3.5.9`, `expat_2.8.5` 는
    `2.8.5` 다. 나머지는 그대로다. 취약점 목록과 수용 목록의 purl 이 이 꼴을 쓴다."""
    out = dict(raw)
    m = _OPENSSL.match(raw.get("openssl", ""))
    if m:
        out["openssl"] = m.group(1)
    m = _EXPAT.match(raw.get("expat", ""))
    if m:
        out["expat"] = m.group(1)
    return out


def entry_key(entry: dict) -> tuple[str, str, str]:
    return entry["uv"], entry["cpython"], entry["platform"]


def load_builds(path: str) -> dict[tuple[str, str, str], dict]:
    """기록 파일 -> {(uv 판, CPython 판, 플랫폼): 항목}. 파일이 없으면 빈 기록이다."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return {}
    return {entry_key(e): e for e in doc.get("builds") or []}


def dump_builds(builds: dict[tuple[str, str, str], dict]) -> str:
    """기록 -> 파일 내용. (uv 판, CPython 판, 플랫폼) 순이라 같은 핀을 다시 써도 같다."""
    entries = [builds[k] for k in sorted(builds)]
    return json.dumps({"builds": entries}, indent=2, ensure_ascii=False) + "\n"
