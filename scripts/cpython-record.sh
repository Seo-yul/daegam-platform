#!/usr/bin/env bash
# 이미지가 싣는 CPython 과 그 안에 묶인 라이브러리의 판을 security/cpython-builds.json 에 적는다 (P58, D70).
#
#   make cpython-record      platform/Dockerfile 의 uv 핀과 CPYTHON_VERSION 으로 두 플랫폼(amd64, arm64)의 항목을 쓴다
#
# 핀을 올렸을 때만 돌린다. 세 Dockerfile 의 핀은 같고(platform/tests/test_python_image_copy.py), 기록에 지금 핀의
# 항목이 없으면 같은 테스트 파일이 실패한다. 하는 일은 넷이다.
#   1. 핀의 uv(`uvx uv==<핀>`)에게 그 CPython 판의 linux 빌드 주소를 묻는다. 이미지 빌드에서 uv 가 받는 주소와 같다
#   2. 그 python-build-standalone 릴리스의 SHA256SUMS 로 변형마다 sha256 을 적는다
#   3. 핀의 uv 가 변형마다 받아 깔게 한다(이미지 빌드와 같은 `uv python install`). uv 는 받은 파일을 자기가 품은 sha256
#      으로 맞대고, UV_PYTHON_CACHE_DIR 에 남긴 파일의 이름 앞자리가 그 값이다. 남긴 파일의 sha256 과 이름이 기록의 값과
#      맞아야 적는다. 압축은 uv 가 푼다
#   4. 기준 변형의 인터프리터로, 빌드 단계의 베이스(Dockerfile 의 `FROM ... AS build`) 컨테이너에서 네트워크 없이
#      판을 찍는다. 이미지를 굽지 않는다
# 다른 핀의 항목은 그대로 둔다. make image-report 가 옛 도장의 이미지를 볼 때 그 도장의 핀으로 찾기 때문이다.
# Env: DAEGAM_CA_FILE(사내 방화벽 CA. 받기에 쓰는 신뢰 목록에 더하고, uv 에는 SSL_CERT_FILE 로 넘긴다)
# 필요: uv(uvx)와 docker. PyPI 와 releases.astral.sh 에 닿는다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
DOCKERFILE="platform/Dockerfile"
RECORD="security/cpython-builds.json"
if [ -n "${DAEGAM_CA_FILE:-}" ]; then export SSL_CERT_FILE="$DAEGAM_CA_FILE"; fi

base="$(sed -n 's/^FROM \([^ ]*\) AS build$/\1/p' "$ROOT/$DOCKERFILE" | head -1)"
[ -n "$base" ] || { echo "cpython-record: $DOCKERFILE 에 'FROM ... AS build' 가 없다" >&2; exit 1; }
pins="$(PYTHONPATH="$HERE/lib" python3 -c '
import sys
from cpython_builds import dockerfile_pins
pins = dockerfile_pins(open(sys.argv[1], encoding="utf-8").read())
if pins is None:
    sys.exit(1)
print(*pins)
' "$ROOT/$DOCKERFILE")" || { echo "cpython-record: $DOCKERFILE 에서 uv== 핀과 ARG CPYTHON_VERSION 을 찾지 못했다" >&2; exit 1; }
read -r uv cpython <<<"$pins"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
echo ">> uv $uv 에게 CPython $cpython 의 빌드 주소를 묻는다"
uvx --quiet "uv==$uv" python list "cpython-$cpython" --all-platforms --all-arches --output-format json >"$work/uv-list.json"

PYTHONPATH="$HERE/lib" python3 - "$ROOT" "$RECORD" "$uv" "$cpython" "$base" "$work" <<'PY'
import hashlib, json, os, subprocess, sys

from cpython_builds import (
    PLATFORMS, VERSION_SCRIPT, cache_name, downloads, dump_builds, entry_key, file_of, load_builds, normalize,
    parse_sums, release_of, sums_url,
)
from public_fetch import fetch

root, record, uv, cpython, base, work = sys.argv[1:]


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


uv_list = json.load(open(os.path.join(work, "uv-list.json"), encoding="utf-8"))
path = os.path.join(root, record)
builds = load_builds(path)
cache = os.path.join(work, "cache")
for platform, family in PLATFORMS.items():
    found = downloads(uv_list, cpython, family)
    baseline = f"cpython-{cpython}-linux-{family}-gnu"
    if baseline not in [d["key"] for d in found]:
        raise SystemExit(f"cpython-record: uv {uv} 가 {platform} 의 CPython {cpython} 기준 빌드를 모른다")
    releases = {release_of(d["url"]) for d in found}
    if len(releases) != 1:
        raise SystemExit(f"cpython-record: {platform} 변형의 릴리스가 하나가 아니다: {sorted(releases)}")
    release = releases.pop()
    sums = parse_sums(fetch(sums_url(found[0]["url"]), 60).decode("utf-8"))
    for d in found:
        d["sha256"] = sums[file_of(d["url"])]
    print(f">> {platform}: uv {uv} 가 {', '.join(d['key'] for d in found)} 를 받아 깐다")
    install = os.path.join(work, "install", family)
    subprocess.run(
        ["uvx", "--quiet", f"uv=={uv}", "python", "install", *[d["key"] for d in found],
         "--install-dir", install, "--no-bin"],
        env={**os.environ, "UV_PYTHON_CACHE_DIR": cache}, check=True,
    )
    for d in found:
        kept = os.path.join(cache, cache_name(d["url"], d["sha256"]))
        if not os.path.exists(kept):
            raise SystemExit(
                f"cpython-record: uv 가 품은 {d['key']} 의 sha256 이 SHA256SUMS 의 값({d['sha256'][:9]}…)과 다르다"
            )
        if sha256_of(kept) != d["sha256"]:
            raise SystemExit(f"cpython-record: uv 가 받은 {file_of(d['url'])} 의 sha256 이 SHA256SUMS 와 다르다")
    dest = os.path.join(install, baseline)
    with open(os.path.join(dest, "BUILD"), encoding="utf-8") as f:
        built = f.read().strip()
    if built != release:
        raise SystemExit(f"cpython-record: {baseline} 의 BUILD 가 {built} 이다 (주소의 릴리스 {release})")
    out = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--platform", platform, "-v", f"{dest}:/py:ro", base,
         "/py/bin/python3", "-I", "-c", VERSION_SCRIPT],
        check=True, capture_output=True, text=True,
    ).stdout
    versions = normalize(json.loads(out))
    if versions["cpython"] != cpython:
        raise SystemExit(f"cpython-record: {platform} 의 인터프리터가 {versions['cpython']} 이다 (핀 {cpython})")
    entry = {
        "uv": uv, "cpython": cpython, "platform": platform, "release": release,
        "versions": versions, "downloads": found,
    }
    builds[entry_key(entry)] = entry
    print(f"   {', '.join(f'{k} {v}' for k, v in versions.items())}")
with open(path, "w", encoding="utf-8") as f:
    f.write(dump_builds(builds))
print(f">> {record} 에 uv {uv}·CPython {cpython} 의 두 플랫폼을 적었다")
PY
