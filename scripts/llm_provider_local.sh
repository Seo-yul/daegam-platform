#!/usr/bin/env bash
# 로컬 배포본에 LLM provider 를 등록한다 (D25, make llm-provider-local).
# 셸에 있는 키마다 플랫폼 네임스페이스에 Secret daegam-llm-<출처>(키 api_key) 를 만들거나 갱신하고, GET /api/settings
# 로 지금 목록을 읽어 provider local-<출처> 를 넣거나 갱신한 뒤 PATCH /api/settings 로 저장한다. 출처는 넷이다.
#   OPENAI_API_KEY      local-openai      kind openai
#   ANTHROPIC_API_KEY   local-anthropic   kind anthropic
#   OPENROUTER_API_KEY  local-openrouter  kind openrouter. 새로 만들면 주소와 모델은 kind 기본(OpenRouter,
#                                         Kimi K3)이고 OPENROUTER_BASE_URL·OPENROUTER_MODEL 을 주면 그 값을
#                                         적는다. 이미 있는 provider 는 두 값을 주지 않으면 적힌 값을 둔다.
#                                         업체 선호(routing)가 비어 있으면 개발 기본값(본가 moonshotai 먼저,
#                                         요청 인자를 다 지원하는 업체만)을 넣는다
#   ZAI_API_KEY         local-zai         kind zai. 주소와 모델은 openrouter 와 같은 규칙이고 env 는
#                                         ZAI_BASE_URL·ZAI_MODEL 이다. kind 기본은 Z.AI 종량제 주소와 glm-5.3 이다
# defaults.spec_generator 가 비어 있으면 첫 provider 로 채운다. 넷 다 없으면 안내만 하고 0 으로 끝난다. 키 값은
# kubectl 의 stdin 으로만 보내 명령줄(ps)에 남지 않는다.
# 인증은 header 모드(X-Daegam-Tenant) 전제이고, oidc 배포본이면 DAEGAM_TOKEN 을 Bearer 로 붙인다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib/hosts.sh"
KUBE_CONTEXT="${KUBE_CONTEXT:-orbstack}"
PLATFORM_NS="${PLATFORM_NS:-daegam-system}"
API="${DAEGAM_API:-http://$(local_host)/api}"
TENANT="${DAEGAM_TENANT:-default}"
KUBECTL="kubectl --context $KUBE_CONTEXT"

# die MSG 는 여기에 있다
. "$HERE/lib/die.sh"
step() { echo ">> $*"; }

# 출처 이름, kind, base_url, 모델. 비운 칸(-)은 provider 의 그 값을 건드리지 않는다
sources=()
[ -n "${OPENAI_API_KEY:-}" ] && sources+=("openai openai - -")
[ -n "${ANTHROPIC_API_KEY:-}" ] && sources+=("anthropic anthropic - -")
[ -n "${OPENROUTER_API_KEY:-}" ] && sources+=("openrouter openrouter ${OPENROUTER_BASE_URL:--} ${OPENROUTER_MODEL:--}")
[ -n "${ZAI_API_KEY:-}" ] && sources+=("zai zai ${ZAI_BASE_URL:--} ${ZAI_MODEL:--}")
if [ ${#sources[@]} -eq 0 ]; then
  echo "OPENAI_API_KEY·ANTHROPIC_API_KEY·OPENROUTER_API_KEY·ZAI_API_KEY 가 셸에 없다. 키를 export 한 뒤 다시 실행하면"
  echo "Secret daegam-llm-<출처> 를 만들고 provider local-<출처> 를 $API 에 등록한다."
  exit 0
fi

curl_args=(-fsS -H "X-Daegam-Tenant: $TENANT" -H "Content-Type: application/json")
[ -n "${DAEGAM_TOKEN:-}" ] && curl_args+=(-H "Authorization: Bearer $DAEGAM_TOKEN")

curl "${curl_args[@]}" -m 5 -o /dev/null "$API/settings" \
  || die "$API/settings 에 닿지 않는다 (배포본이 떠 있는지, /etc/hosts 매핑이 있는지 확인)"

names=()
for entry in "${sources[@]}"; do
  read -r source _ <<<"$entry"
  case "$source" in
    openai) key="$OPENAI_API_KEY" ;;
    anthropic) key="$ANTHROPIC_API_KEY" ;;
    openrouter) key="$OPENROUTER_API_KEY" ;;
    zai) key="$ZAI_API_KEY" ;;
  esac
  names+=("local-$source")
  step "Secret daegam-llm-$source (api_key) in $PLATFORM_NS"
  # --from-file=api_key=/dev/stdin: 값이 명령줄에 나타나지 않는다. dry-run + apply 라 갱신도 된다
  printf '%s' "$key" \
    | $KUBECTL -n "$PLATFORM_NS" create secret generic "daegam-llm-$source" \
        --from-file=api_key=/dev/stdin --dry-run=client -o yaml \
    | $KUBECTL apply -f - >/dev/null
done

step "PATCH $API/settings: provider ${names[*]} 등록"
current="$(curl "${curl_args[@]}" "$API/settings")"
patch="$(printf '%s' "$current" | python3 -c '
import json, sys
settings = json.load(sys.stdin)
# PATCH 는 목록 전체를 바꾸므로 GET 이 준 항목을 그대로 돌려보낸다 (필드를 고르면 embedding_model 이 지워진다)
providers = settings["llm_providers"]
first = None
for entry in sys.argv[1:]:
    source, kind, base_url, model = entry.split()
    name, secret = f"local-{source}", f"daegam-llm-{source}"
    first = first or name
    wanted = {"kind": kind, "api_key_secret": secret}
    # 주소와 모델은 출처가 정할 때만 맞춘다. 아니면 이미 있는 provider 의 값을 둔다
    if base_url != "-":
        wanted["base_url"] = base_url
    if model != "-":
        wanted["model"] = model
    current = next((p for p in providers if p["name"] == name), None)
    if current is None:
        current = {"name": name}
        providers.append(current)
    # 출처 가운데 임베딩을 받는 kind 는 openai 뿐이다. 나머지는 embedding_model 을 받지 않는다(422).
    # 지울지는 사람이 정한다
    if current.get("embedding_model") and kind != "openai":
        sys.exit("llm_provider_local.sh: FAIL: provider {} 에 embedding_model {} 이 있어 kind {} 로 바꿀 수 없다. "
                 "/admin/settings 에서 그 칸을 비운 뒤 다시 실행한다".format(name, current["embedding_model"], kind))
    if kind == "openrouter" and not current.get("routing"):
        wanted["routing"] = {"order": ["moonshotai"], "require_parameters": True}
    current.update(wanted)
patch = {"llm_providers": providers}
if not settings["defaults"].get("spec_generator"):
    patch["defaults"] = {"spec_generator": first}
print(json.dumps(patch))
' "${sources[@]}")"
result="$(curl "${curl_args[@]}" -X PATCH -d "$patch" "$API/settings")"
printf '%s' "$result" | python3 -c '
import json, sys
s = json.load(sys.stdin)
# macOS 기본 python3(3.9)는 f-string 식 안의 따옴표를 받지 않으므로 format 으로 찍는다
for p in s["llm_providers"]:
    print("   provider {}: kind={} model={} base_url={} secret={} routing={}".format(
        p["name"], p["kind"], p["model"] or "(default)", p["base_url"] or "-", p["api_key_secret"],
        json.dumps(p.get("routing")) if p.get("routing") else "-"))
print("   defaults.spec_generator = {}".format(s["defaults"]["spec_generator"]))
'
for name in "${names[@]}"; do
  step "연결 확인 $name"
  curl "${curl_args[@]}" -X POST "$API/settings/llm-providers/$name/check" || true
  echo
done
