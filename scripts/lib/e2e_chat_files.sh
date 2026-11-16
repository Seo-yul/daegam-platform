# shellcheck shell=bash
# P36 묶음 B(첨부와 세션 파일, D61) e2e 의 한 벌. e2e_chat 의 B 단계와 install 갈래, e2e_sandbox 가 함께 쓴다.
# `. "$HERE/lib/e2e_chat_files.sh"` 로 읽는다. 부르는 쪽이 ROOT·WORK·K(kubectl 배열)·PLATFORM_NS 와 lib/e2e_api.sh(api·jq_)·
# json_of·die(STEP MSG)를 둔다.
#
# chat_samples ARGS...
#   표본 파일을 만든다. deploy/dev/lab/chat_samples.py 를 플랫폼 venv 로 부르고, 문서 표본은 플랫폼 테스트의 표본 만들기
#   (platform/tests/helpers/documents.py)다. 인자는 그 파일 머리에 있다
# upload AGENT_ID FILE [NAME]
#   그 Agent 에 파일 하나를 업로드하고 상태 코드를 찍는다. 본문은 $WORK/body, 응답 헤더는 $WORK/hdr 다. NAME 을 주면 그
#   이름으로 보낸다
# upload_ok STEP AGENT_ID FILE EXPR [NAME]
#   업로드가 201 이고 응답이 파이썬 식 EXPR 을 참으로 만들어야 한다. 아니면 die STEP 이다. 업로드 id 를 UPLOAD_ID 에 남긴다.
#   값을 echo 로 돌려주지 않는 것은 $( ) 안의 die 가 부른 쪽을 멈추지 못해서다
# download PATH
#   GET PATH 의 상태 코드를 찍는다. 본문은 $WORK/download 와 $WORK/body, 응답 헤더는 $WORK/hdr 다
# download_headers_off MEDIA_TYPE NAME
#   $WORK/hdr 가 내려받기 헤더 다섯(D61)과 어긋난 것을 찍는다. 같으면 빈 줄이다. 고정 헤더 셋은 플랫폼과 런타임 테스트가
#   함께 대는 표본(DOWNLOAD_FIXTURE)에서 읽는다. NAME 은 ASCII 이름이라 대체 이름이 그대로다
# docparse_pod STEP
#   문서 파싱 서비스 daegam-docparse 의 격리를 실제 파드에서 본다. Deployment 가 SA 토큰을 붙이지 않고 플랫폼 설정을
#   envFrom 으로 받지 않으며 env 가 토큰 Secret 의 키 하나뿐이다. 파드 안에 SA 토큰 파일이 없고 env 에 플랫폼 비밀 묶음의
#   이름이 하나도 없다. kubectl exec 로 부른 selfcheck 의 다섯 칸이 모두 참이다. 어긋나면 die STEP 이고 맞으면
#   DOCPARSE_NOTE 를 남긴다

DOCPARSE_DEPLOY="daegam-docparse"; DOCPARSE_CONTAINER="docparse"
# 내려받기 헤더 다섯의 표본이다. 플랫폼과 런타임 테스트도 이것을 댄다
DOWNLOAD_FIXTURE="$ROOT/agent-runtime/schema/fixtures/chat/download-headers.json"
DOCPARSE_TOKEN_SECRET="daegam-docparse-token"; PLATFORM_SECRETS="daegam-platform-secrets"
# selfcheck 의 다섯 칸이다(platform/daegam/documents/selfcheck.py). 이름순이다
SELFCHECK_FIELDS="['dumpable_zero', 'fork_blocked', 'group_killed', 'server_environ_unreadable', 'sibling_stdio_unopenable']"
# 파드 안에서 env 의 이름(값은 찍지 않는다)과 SA 토큰 파일이 있는지를 JSON 한 줄로 찍는다
DOCPARSE_PROBE='import json, os
print(json.dumps({"env": sorted(os.environ), "sa_token": os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")}))'
# shellcheck disable=SC2034  # 부르는 쪽이 읽는다
UPLOAD_ID=""; DOCPARSE_NOTE=""

chat_samples() { uv run --quiet --project "$ROOT/platform" python "$ROOT/deploy/dev/lab/chat_samples.py" "$@"; }

upload() { api POST "/agents/$1/uploads" "" -D "$WORK/hdr" -F "file=@$2${3:+;filename=$3}"; } # AGENT_ID FILE [NAME]

upload_ok() { # STEP AGENT_ID FILE EXPR [NAME]
  local code
  code="$(upload "$2" "$3" ${5:+"$5"})"
  { [ "$code" = 201 ] && [ "$(jq_ "$4")" = True ]; } || die "$1" "upload ${5:-${3##*/}} -> $code $(cat "$WORK/body") (expected 201 and $4)"
  # shellcheck disable=SC2034  # 부르는 쪽이 읽는다
  UPLOAD_ID="$(jq_ 'd["id"]')"
}

download() { # PATH
  local code
  # 본문이 비면 curl 이 파일을 만들지 않는다. 앞 응답의 본문이 남지 않게 지우고, 비었으면 빈 파일로 둔다
  rm -f "$WORK/body"
  code="$(api GET "$1" "" -D "$WORK/hdr")"
  [ -f "$WORK/body" ] || : >"$WORK/body"
  cp "$WORK/body" "$WORK/download"
  echo "$code"
}

download_headers_off() { # MEDIA_TYPE NAME
  python3 - "$WORK/hdr" "$DOWNLOAD_FIXTURE" "$1" "$2" <<'PY'
import json, sys, urllib.parse

hdr_file, fixture, media, name = sys.argv[1:5]
got = {}
for line in open(hdr_file, encoding="latin-1"):
    key, sep, value = line.partition(":")
    if sep:
        got[key.strip().lower()] = value.strip()
sample = json.load(open(fixture))["cases"][0]["headers"]
want = {k: sample[k] for k in ("x-content-type-options", "cache-control", "content-security-policy")}
want["content-type"] = media
want["content-disposition"] = f"attachment; filename=\"{name}\"; filename*=UTF-8''{urllib.parse.quote(name)}"
off = {k: {"got": got.get(k), "want": v} for k, v in want.items() if got.get(k) != v}
print(json.dumps(off, ensure_ascii=False) if off else "")
PY
}

docparse_pod() { # STEP
  local step="$1" in_pod=()
  in_pod=("${K[@]}" -n "$PLATFORM_NS" exec "deploy/$DOCPARSE_DEPLOY" -c "$DOCPARSE_CONTAINER" --)
  "${K[@]}" -n "$PLATFORM_NS" get deploy "$DOCPARSE_DEPLOY" -o json >"$WORK/docparse.json" \
    || die "$step" "no deploy/$DOCPARSE_DEPLOY in $PLATFORM_NS"
  # 값은 읽지 않고 키 이름만 받는다. 작은따옴표 안의 $k 는 kubectl 의 go-template 변수다
  # shellcheck disable=SC2016
  "${K[@]}" -n "$PLATFORM_NS" get secret "$PLATFORM_SECRETS" -o go-template='{{range $k, $v := .data}}{{$k}} {{end}}' \
    >"$WORK/platform-secret-keys" || die "$step" "reading the key names of Secret $PLATFORM_SECRETS failed"
  "${in_pod[@]}" python -c "$DOCPARSE_PROBE" >"$WORK/docparse-env.json" \
    || die "$step" "kubectl exec into deploy/$DOCPARSE_DEPLOY failed"
  python3 - "$WORK/docparse.json" "$WORK/docparse-env.json" "$WORK/platform-secret-keys" "$DOCPARSE_CONTAINER" \
    "$DOCPARSE_TOKEN_SECRET" >"$WORK/body" <<'PY' || die "$step" "daegam-docparse isolation: $(cat "$WORK/body")"
import json, sys

deploy, probe_file, keys_file, container, token_secret = sys.argv[1:6]
spec = json.load(open(deploy))["spec"]["template"]["spec"]
box = next(c for c in spec["containers"] if c["name"] == container)
probe = json.load(open(probe_file))
refs = sorted((e["name"], ((e.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name")) for e in box.get("env") or [])
leaked = sorted(set(open(keys_file).read().split()) & set(probe["env"]))
bad = [why for why, failed in [
    ("automountServiceAccountToken is not false", spec.get("automountServiceAccountToken") is not False),
    (f"envFrom {box.get('envFrom')}", bool(box.get("envFrom"))),
    (f"env {refs} (want only DAEGAM_DOCPARSE_TOKEN from {token_secret})", refs != [("DAEGAM_DOCPARSE_TOKEN", token_secret)]),
    ("a service account token is mounted in the pod", probe["sa_token"]),
    (f"platform secret names in the pod env: {leaked}", bool(leaked)),
] if failed]
print("; ".join(bad))
sys.exit(1 if bad else 0)
PY
  # 다섯 칸이 모두 참이 아니면 selfcheck 가 1 로 끝난다. 그때도 JSON 을 찍으므로 그것을 보인다
  "${in_pod[@]}" python -m daegam.documents.selfcheck >"$WORK/body" 2>"$WORK/selfcheck.err" \
    || die "$step" "selfcheck in deploy/$DOCPARSE_DEPLOY failed: $(cat "$WORK/body" "$WORK/selfcheck.err")"
  [ "$(jq_ 'sorted(k for k, v in d.items() if v is True)')" = "$SELFCHECK_FIELDS" ] \
    || die "$step" "selfcheck fields: $(cat "$WORK/body") (want all of $SELFCHECK_FIELDS true)"
  # shellcheck disable=SC2034  # 부르는 쪽이 읽는다
  DOCPARSE_NOTE="daegam-docparse: no SA token, env only DAEGAM_DOCPARSE_TOKEN from $DOCPARSE_TOKEN_SECRET, no $PLATFORM_SECRETS name; selfcheck $(cat "$WORK/body")"
}
