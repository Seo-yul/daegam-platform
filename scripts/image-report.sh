#!/usr/bin/env bash
# zot 레지스트리가 Trivy 로 잰 제품 이미지의 취약점과 이미지마다의 서명을 보고한다 (D55, D66). 읽기만 한다.
#
#   make image-report                           지금 도장의 일곱 이미지
#   make image-report REPORT_IMAGES="ui"        고른 것만
#   make image-report REPORT_TAG=1.1.0-rc11     도장 대신 그 릴리스 태그의 이미지(릴리스 전 미리 보기)
# Env: REGISTRY(설치 값 파일의 REGISTRY) REPORT_WAIT(스캔 결과를 기다리는 초, 180)
#      REPORT_REFS="daegam-ui:12d1948 ..."  도장 대신 이름:태그 를 직접 준다(이미 올린 옛 태그를 볼 때)
#      COSIGN_PUB(deploy/cosign.pub)  서명을 확인할 공개 키
#
# 판정은 HIGH·CRITICAL 만 본다. 수용 목록은 이미지마다 하나다. daegam-sandbox 는 security/vex/sandbox.openvex.json, 나머지
# 여섯은 security/vex/base-images.openvex.json 이다(lib/image_report.py 의 VEX_FILES). 그 목록과 (CVE, 패키지, 설치 판)으로 맞대 모두
# 맞으면 "수용됨", 아니면 고친 판(FixedVersion)이 있으면 "고칠 수 있다", 없으면 "새로 나옴" 이다. 실패는 수용되지 않은
# CRITICAL 과 스캔 결과가 없는 이미지다. HIGH 는 막지 않고 줄로 찍어 끝 줄에 센다. 같은 발견은 이미지가 여럿이어도 한 번
# 센다. 마지막 줄은 `막는 것 n, 막지 않은 것 m` 이다. GITHUB_ACTIONS 에서는 수용되지 않은 발견과 끝 줄을 단계 요약
# (GITHUB_STEP_SUMMARY)에 더하고 경고(::warning::)를 하나 낸다. 목록에 있지만 이번에 잰 어느 이미지에도 없는 것은 알리기만
# 한다. 판정은 lib/image_report.py 가 한다.
# 서명은 resolve-images.sh 가 값 파일을 쓰기 전에 하는 것과 같은 확인(lib/signing.sh 의 verify_image)으로 보되 서명에 실린 도장은
# 맞대지 않고 이미지 이름만 맞댄다. REPORT_REFS 의 태그는 도장이 아닐 수 있기 때문이다. 결과는 이미지 줄의 서명 칸에
# "서명 확인됨" 이나 "서명 확인 안 됨" 으로 찍는다. 확인되지 않아도 이 보고는 실패하지 않는다. 그 이미지는 배포가 거절한다.
# cosign 이나 공개 키가 없으면 레지스트리에 묻기 전에 멈춘다.
# zot 레지스트리는 올라온 이미지를 비동기로 잰다. 결과가 없으면 REPORT_WAIT 초까지 기다린다.
# 자격 증명은 docker login 이 둔 macOS 키체인이다(push-images.sh 와 같다). 읽기는 lib/registry_auth.py 가 하고 cosign 도
# 같은 자격 증명을 쓴다.
# CPython 을 싣는 다섯 이미지(platform·runtime·agent·web-tools·sandbox)는 스캔이 보지 못하는 CPython 과 묶인 라이브러리도 본다(P58, D70). 이미지 태그(도장이나 릴리스
# 태그) 시점의 Dockerfile 핀으로 security/cpython-builds.json 에서 판을 찾고, python.org 와 NVD 에 묻는다. 고친 판이 없는
# 것으로 보아 "새로 나옴" 이고 CRITICAL 이면 막는다. 수용 목록의 pkg:generic statement 가 받으면 "수용됨" 이다. 조회는 요청마다 10초, 요청이 쓴
# 시간을 모두 합쳐 60초이고 자격 증명을 싣지 않는다. 받기는 lib/public_fetch.py 가 하고, DAEGAM_CA_FILE 이 있으면 그 CA 도
# 믿는다. 조회하지 못했거나 답의 모양이 다른 구성요소는 "확인 못 함" 이고 판정을 실패시키지 않는다. DAEGAM_CA_FILE 을 읽지
# 못해도 그렇다. 판정은 lib/image_report.py 의 bundled 가 한다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# 레지스트리(호스트/프로젝트)는 환경의 REGISTRY 이고, 없으면 설치 값 파일(scripts/lib/hosts.sh 의 install_env_file)의 값이다
. "$HERE/lib/hosts.sh"
REGISTRY="${REGISTRY:-$(install_env_value REGISTRY)}"
[ -n "$REGISTRY" ] || { echo "REGISTRY 가 비었다 (환경이나 설치 값 파일의 REGISTRY)" >&2; exit 1; }
BUILDS="security/cpython-builds.json"
# COMPONENTS 와 image_ref(구성요소 -> `이름:도장`)는 여기에 있다
. "$HERE/lib/images.sh"
# verify_ready(cosign 과 공개 키 확인)와 verify_image(서명 확인)는 여기에 있다
. "$HERE/lib/signing.sh"

# shellcheck disable=SC2086  # 구성요소 이름을 낱말로 나눠 인자로 편다
[ "$#" -gt 0 ] || set -- $COMPONENTS
refs=()
if [ -n "${REPORT_REFS:-}" ]; then
  read -r -a refs <<<"$REPORT_REFS"
elif [ -n "${REPORT_TAG:-}" ]; then
  for c in "$@"; do
    line="$(spec "$c")" || { echo "image-report: 모르는 구성요소 '$c' ($COMPONENTS)" >&2; exit 2; }
    refs+=("${line%% *}:$REPORT_TAG")
  done
else
  for c in "$@"; do
    ref="$(image_ref "$c")" || exit 2
    refs+=("$ref")
  done
fi

# 서명 칸(D66). 서명이 확인된 이미지만 모아 파이썬 조각에 넘긴다. 칸에는 까닭을 싣지 않으므로 cosign 이 대는 까닭은 버린다
verify_ready || exit 1
signed=()
for ref in "${refs[@]}"; do
  if verify_image "$REGISTRY/$ref" "${ref%%:*}" 2>/dev/null; then signed+=("$ref"); fi
done

# 이미지마다 그것을 굽는 Dockerfile 과 그 판을 읽을 git 이름을 `이름:태그=경로=판` 으로 넘긴다. 모르는 이름이면 경로가
# 비고 CPython 묶음을 보지 않는다. 도장은 트리에서 나와(scripts/build-stamp.sh) commit 이 아니므로, 태그가 HEAD 의
# 도장이면 HEAD 에서 읽고 그 밖(릴리스 태그, 트리 도장 이전의 commit 도장)은 태그 이름으로 읽는다
pairs=()
for ref in "${refs[@]}"; do
  name="${ref%%:*}" tag="${ref#*:}" rev="${ref#*:}"
  for c in $COMPONENTS; do
    read -r cname _ <<<"$(spec "$c")"
    if [ "$cname" = "$name" ] && [ "$tag" = "$(sh "$ROOT/scripts/build-stamp.sh" "$c" 2>/dev/null)" ]; then rev=HEAD; fi
  done
  pairs+=("$ref=$(dockerfile_of "$name" || true)=$rev")
done

PYTHONPATH="$HERE/lib" python3 - "$REGISTRY" "${REPORT_WAIT:-180}" "$ROOT" "$BUILDS" "${signed[*]-}" "${pairs[@]}" <<'PY'
import json, os, subprocess, sys, time, urllib.parse, urllib.request

from cpython_builds import load_builds
from image_report import (
    VEX_FILES, Advisories, Judges, bundled, closing, image_line, stamp_pins, step_summary, summary, widest,
)
from public_fetch import fetch
from registry_auth import Registry

registry, wait, root, builds_path = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
signed = set(sys.argv[5].split())
triples = [arg.rsplit("=", 2) for arg in sys.argv[6:]]
refs = [ref for ref, _, _ in triples]
dockerfiles = {ref: (dockerfile, rev) for ref, dockerfile, rev in triples}
reg = Registry(registry)
# 수용 목록의 경로는 저장소 뿌리 기준이다. 이미지마다 그 Dockerfile 범위의 목록으로 판정한다
os.chdir(root)
judges = Judges(VEX_FILES)
builds = load_builds(builds_path)
advisories = Advisories(fetch)


def git_show(spec):
    done = subprocess.run(["git", "-C", root, "show", spec], capture_output=True, text=True)
    return done.stdout if done.returncode == 0 else None


# 서명 칸이 이미지마다 같은 칸에 오도록 이미지 이름을 가장 긴 것에 맞춰 채운다
images = [(ref, f"{reg.project}/{ref}") for ref in refs]
width = widest(image for _, image in images)


def search(query):
    req = reg.request("/v2/_zot/ext/search?query=" + urllib.parse.quote(query))
    body = json.load(urllib.request.urlopen(req, timeout=60))
    if body.get("errors"):
        raise RuntimeError(body["errors"])
    return body["data"]


missing = 0
for ref, image in images:
    deadline = time.time() + wait
    while True:
        try:
            cves = search(
                f'{{ CVEListForImage(image: "{image}", requestedPage: {{limit: 1000, offset: 0}}) '
                "{ CVEList { Id Severity PackageList { Name InstalledVersion FixedVersion } } } }"
            )["CVEListForImage"]["CVEList"]
            break
        except Exception as e:  # 아직 재지 않았거나 태그가 없다
            if time.time() > deadline:
                print(image_line(image, width, ref in signed, f"스캔 결과가 없다 ({e})"))
                missing += 1
                cves = None
                break
            time.sleep(10)
    if cves is None:
        continue
    print(image_line(image, width, ref in signed, f"취약점 {summary(cves)}"))
    dockerfile, rev = dockerfiles.get(ref) or ("", "")
    judge = judges.for_path(dockerfile)
    for finding in judge.findings(cves):
        print(finding.line())
    if dockerfile:
        tag = ref.split(":", 1)[1]
        current = open(f"{root}/{dockerfile}", encoding="utf-8").read()
        found = stamp_pins(tag, current, git_show(f"{rev}:{dockerfile}"))
        if found is not None:
            print("\n".join(bundled(judge, advisories, builds, found[0], found[1])))
unseen = judges.unseen()
if unseen:
    print("\n수용 목록에 있지만 이번에 잰 이미지에 없다. 고쳐졌거나 판이 바뀌었으면 뺀다:")
    print("\n".join(unseen))
lines, code = closing(judges.judged, missing, judges.hint())
print("\n" + "\n".join(lines))
# GitHub Actions 에서는 로그를 열지 않아도 보이게 단계 요약과 경고 한 줄을 남긴다
if os.environ.get("GITHUB_ACTIONS") == "true":
    body, warning = step_summary(judges.judged, lines)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(body)
    if warning:
        print(warning)
sys.exit(code)
PY
