#!/usr/bin/env bash
# make verify-changed 의 몸통. 마지막으로 게이트를 통과한 뒤에 바뀐 파일만 보고 갈래를 고른다.
#
#   scripts/verify_changed.sh          바뀐 파일로 갈래를 골라 make 로 돌리고, 통과하면 그 자리를 적는다
#   scripts/verify_changed.sh record   지금 자리를 통과한 자리로 적는다(make verify 가 끝에 부른다)
#   scripts/verify_changed.sh store-lanes
#                                      표준 입력의 파일 목록으로 DB 시험 갈래를 platform=, platform_tests=, agent_runtime=
#                                      세 줄로 찍는다. PR 의 pr-check 가 이것으로 platform.yml·agent-runtime.yml 을 부를지와
#                                      platform.yml 이 돌릴 시험(빈 값은 전부)을 고른다
#   scripts/verify_changed.sh runtime-link-tests
#                                      verify-runtime-links 가 돌리는 platform 시험의 경로(platform/ 기준)를 한 줄에 하나씩
#                                      찍는다. Makefile 과 store-lanes 가 이 하나로 고른다
#   scripts/verify_changed.sh lanes    표준 입력의 파일 목록으로 고른 make 대상을 한 줄로 찍는다
#
# 통과한 자리는 worktree 마다 git 디렉터리 안의 파일 하나다. 첫 줄이 그때의 HEAD 이고, 다음 줄부터는 그때
# 커밋하지 않았던 파일(추적 안 되는 파일 포함)의 내용 해시와 경로다. 다음 판정은 그 커밋 이후의 변경에서,
# 그때와 내용이 같은 파일을 빼고 본다. 적힌 커밋이 HEAD 의 조상이 아니면(브랜치를 옮겼으면) 묶음 브랜치가 갈라져
# 나온 origin/develop 과의 merge-base 로 돌아가고, origin/develop 이 없으면 origin/main 과의 merge-base 다.
#
# 갈래. ui/ 는 verify-ui 와 verify-ui-links, docs/·루트 *.md·.claude/skills/ 는 verify-docs,
# agent-runtime/(schema 제외)·mcp-runtime/·web-tools/(schema 제외)·sandbox/ 는 verify-runtime 과 verify-runtime-links 다. ops/
# 는 verify-ops 다. ops/ 는 운영자의 저장소에만 있고 verify-ops 는 그 안의 ops/*/*.mk 가 둔다(P85 9절). deploy/dev/lab/f0/ 는
# 공개 트리에 없는 측정 도구라 어느 갈래의 시험도 그것을 읽지 않으므로 verify-docs 의 주소 검사만 받는다. 그 밖(platform/,
# agent-runtime/schema/, deploy/, scripts/, charts/, Makefile 등)이 하나라도 있으면 verify 전부다.
# DB 가 있어야 도는 갈래(플랫폼 pytest, agent-runtime 의 실제 저장소 시험)는 시험용 DB 주소가 있을 때만 이 PC 에서 돌고,
# 없으면 Makefile 이 건너뛴 것을 찍는다. 그 갈래는 PR 의 pr-check 가 돌린다.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
# 앞서 통과한 커밋의 기록이다. 지우면 다음 실행은 기준 없이 바뀐 것 전체를 본다
STAMP="$(git rev-parse --git-path daegam-verify-pass)"
MAKE="${MAKE:-make}"

content_hash() { # 경로 -> 내용 해시. 없는 파일은 deleted
  if [ -e "$1" ]; then git hash-object -- "$1"; else echo deleted; fi
}

dirty_files() { # 커밋하지 않은 변경과 추적 안 되는 파일
  { git diff --name-only HEAD; git ls-files --others --exclude-standard; } | sort -u | sed '/^$/d'
}

record() {
  { git rev-parse HEAD
    dirty_files | while IFS= read -r f; do printf '%s %s\n' "$(content_hash "$f")" "$f"; done
  } >"$STAMP"
}

base_commit() {
  local passed
  passed="$(head -1 "$STAMP" 2>/dev/null || true)"
  if [ -n "$passed" ] && git merge-base --is-ancestor "$passed" HEAD 2>/dev/null; then
    echo "$passed"
  else
    git merge-base HEAD origin/develop 2>/dev/null || git merge-base HEAD origin/main 2>/dev/null || git rev-parse HEAD
  fi
}

changed_files() { # 통과한 자리 이후에 바뀐 파일. 그때 커밋 안 된 채로 통과한 내용 그대로인 것은 뺀다
  local base f
  base="$(base_commit)"
  { git diff --name-only "$base"; git ls-files --others --exclude-standard; } | sort -u | sed '/^$/d' |
    while IFS= read -r f; do
      if [ -f "$STAMP" ] && grep -qxF "$(content_hash "$f") $f" <(tail -n +2 "$STAMP"); then continue; fi
      echo "$f"
    done
}

lanes_for() { # 바뀐 파일 목록(표준 입력) -> make 대상들
  local ui=0 docs=0 runtime=0 ops=0 full=0 f
  while IFS= read -r f; do
    case "$f" in
      ui/*) ui=1 ;;
      docs/* | .claude/skills/*) docs=1 ;;
      agent-runtime/schema/* | web-tools/schema/*) full=1 ;;
      agent-runtime/* | mcp-runtime/* | web-tools/* | sandbox/*) runtime=1 ;;
      ops/*) ops=1 ;;
      deploy/dev/lab/f0/*) docs=1 ;;
      */*) full=1 ;;
      *.md) docs=1 ;;
      *) full=1 ;;
    esac
  done
  if [ "$full" = 1 ]; then echo verify; return; fi
  local out=()
  [ "$ui" = 1 ] && out+=(verify-ui verify-ui-links)
  [ "$runtime" = 1 ] && out+=(verify-runtime verify-runtime-links)
  [ "$docs" = 1 ] && out+=(verify-docs)
  [ "$ops" = 1 ] && out+=(verify-ops)
  echo "${out[@]}"
}

runtime_link_tests() { # 런타임 파일을 읽는 platform 시험. 시험 파일이 런타임 디렉터리 이름을 글자로 적었거나
  # tests/helpers/source.py 의 AGENT_RUNTIME·MCP_RUNTIME·WEB_TOOLS·SANDBOX_IMAGE 로 런타임 파일을 읽는지, 제품 Dockerfile
  # 전부를 tests/helpers/images.py 의 product_dockerfiles 로 읽는지로 고른다. sandbox 는 플랫폼 시험에도 흔한 낱말이라
  # 글자로 고르지 않는다
  (cd platform &&
    { grep -lE '"(agent-runtime|mcp-runtime|web-tools)"|\b(AGENT|MCP)_RUNTIME\b|\bWEB_TOOLS\b|\bSANDBOX_IMAGE\b|\bproduct_dockerfiles\b' tests/*.py
      echo tests/test_doc_citations.py; }) | sort -u
}

store_lanes() { # 바뀐 파일 목록(표준 입력) -> pr-check 가 부를 DB 시험 갈래와 리눅스 시험 갈래
  local files lanes platform=false agent=false sandbox=false tests=""
  files="$(sed '/^$/d')"
  if [ -n "$files" ]; then lanes=" $(lanes_for <<<"$files") "; else lanes=" "; fi
  case "$lanes" in
    *" verify "*) platform=true ;;
    # 런타임만 바뀌면 런타임 파일을 읽는 platform 시험만 Mongo 와 함께 돈다(로컬 링크 갈래는 그 Mongo 시험을 건너뛴다)
    *" verify-runtime-links "*) platform=true; tests="$(runtime_link_tests | tr '\n' ' ')" ;;
  esac
  # agent-runtime 의 저장소 시험은 agent-runtime/ 이나 그 시험을 부르는 파일이 바뀌었을 때만 돈다. 파이프 대신
  # here-string 이다: grep -q 가 먼저 끝나면 앞의 printf 가 SIGPIPE 로 죽어 pipefail 아래에서 판정이 뒤집힌다
  if grep -qE '^(agent-runtime/|Makefile$|\.github/workflows/(agent-runtime|pr-check)\.yml$)' <<<"$files"; then
    agent=true
  fi
  # 샌드박스 서버의 리눅스 전용 시험(P40 F1)은 macOS 에서 건너뛰므로 sandbox/ 가 바뀌면 pr-check 가 돌린다.
  # Makefile 은 넣지 않는다. 그 시험은 Makefile 을 읽지 않는다
  if grep -qE '^(sandbox/|\.github/workflows/(sandbox|pr-check)\.yml$)' <<<"$files"; then
    sandbox=true
  fi
  echo "platform=$platform"
  echo "platform_tests=${tests% }"
  echo "agent_runtime=$agent"
  echo "sandbox=$sandbox"
}

if [ "${1:-}" = record ]; then record; exit 0; fi
if [ "${1:-}" = store-lanes ]; then store_lanes; exit 0; fi
if [ "${1:-}" = runtime-link-tests ]; then runtime_link_tests; exit 0; fi
if [ "${1:-}" = lanes ]; then lanes_for; exit 0; fi

changed="$(changed_files)"
if [ -z "$changed" ]; then
  echo ">> 마지막으로 통과한 뒤 바뀐 것이 없다"
  record # 새 커밋이 쌓였으면 통과한 자리를 HEAD 로 당겨 다음 비교를 짧게 한다
  exit 0
fi
targets="$(echo "$changed" | lanes_for)"
echo ">> 통과한 자리($(base_commit | cut -c1-7)) 뒤에 바뀐 파일 $(echo "$changed" | wc -l | tr -d ' ')개: $(echo "$changed" | head -3 | tr '\n' ' ')…"
echo ">> 도는 갈래: $targets"
# shellcheck disable=SC2086 # 대상 여럿을 낱말로 나눠 넘긴다
"$MAKE" --no-print-directory $targets
record
