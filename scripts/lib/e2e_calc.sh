# shellcheck shell=bash
# calculator 준비 한 벌 (e2e_mcp_auth STEP 2·3, e2e_pat, P33 e2e_oauth, e2e_agent STEP 3·5g, P36 e2e_chat STEP 1c).
# `. "$HERE/lib/e2e_calc.sh"` 로 읽는다.
# 부르는 쪽이 ROOT·SPIKE_NS 를 두고 lib/e2e_api.sh(api·jq_·listed_id·api_delete·save_doc·deploy_ready)와 die(STEP MSG)를
# 둔다. SOAP 끝점은 lab 의 mock-soap(make lab-up)이다.
#
# calc_backend STEP NAME [DISPLAY]
#   calculator WSDL 을 Backend API NAME(표시 이름 DISPLAY)으로 올리고 endpoint 를 mock-soap 으로 둔다. CALC_BP_ID 를 남기고
#   끝나면 $WORK/body 가 endpoint PATCH 응답이다. 재실행이면 같은 이름에 새 버전(200)이다
# calc_ms_body NAME
#   CALC_BP_ID 위에 도구 Add 하나를 내는 MCP Server NAME 의 정의 본문을 찍는다
# calc_mcp_server STEP BP_NAME MS_NAME DEPLOY_JSON
#   calculator WSDL 을 Backend API BP_NAME 으로 올리고 endpoint 를 mock-soap 으로 둔 뒤, 도구 Add 하나의 MCP Server MS_NAME
#   을 저장하고 DEPLOY_JSON 본문으로 배포해 Ready 를 기다린다. CALC_BP_ID·CALC_MS_ID·CALC_MS_NAME·CALC_MS_K8S_NAME·CALC_NS 를
#   남긴다
# calc_publish STEP
#   CALC_MS_ID 를 손으로 게시한다(D23). 자동 등재가 없는지 먼저 보고, 이전 실행이 게시해 두었으면 200(멱등)도 받는다.
#   CALC_HUB_ID 와 게시 응답 코드 CALC_PUBLISH_CODE 를 남긴다
# calc_gateway STEP GW_NAME DEPLOY_JSON
#   CALC_HUB_ID 를 백엔드(도구 Add)로 하는 Gateway GW_NAME 을 저장하고 DEPLOY_JSON 본문으로 배포해 Ready 를 기다린다.
#   CALC_GW_ID·CALC_GW_K8S_NAME 을 남긴다
# calc_teardown
#   만든 Gateway 와 MCP Server 를 지운다(e2e_pat, e2e_oauth 의 정리). Gateway 가 게시 항목을 붙드므로 Gateway 가 먼저다
#
# 배포하는 셋이 끝나면 $WORK/body 가 그 문서의 GET 응답이다. 재실행의 409 는 save_doc·deploy_doc 이 잇는다(그 까닭은
# lib/e2e_api.sh 머리말에 있다). 이전 실행의 배포를 redeploy 로 이었으면 DOC_REDEPLOYED 가 1 이다.

# shellcheck disable=SC2034  # CALC_* 는 부르는 쪽이 읽는다
CALC_BP_ID=""; CALC_MS_ID=""; CALC_MS_NAME=""; CALC_MS_K8S_NAME=""; CALC_NS=""
CALC_HUB_ID=""; CALC_PUBLISH_CODE=""; CALC_GW_ID=""; CALC_GW_K8S_NAME=""

calc_backend() { # STEP NAME [DISPLAY]
  local step="$1" name="$2" code display=()
  [ -z "${3:-}" ] || display=(-F "display_name=$3")
  code="$(api POST /backend-apis "" -F "name=$name" ${display[@]+"${display[@]}"} -F "file=@$ROOT/platform/tests/fixtures/calculator.wsdl")"
  case "$code" in
    200 | 201) CALC_BP_ID="$(jq_ 'd["id"]')" ;;
    409) listed_id "$step" /backend-apis "$name"; CALC_BP_ID="$LISTED_ID" ;;
    *) die "$step" "POST /backend-apis -> $code" ;;
  esac
  code="$(api PATCH "/backend-apis/$CALC_BP_ID" "{\"endpoint\": \"http://mock-soap.${SPIKE_NS}.svc:18080/calc\"}")"
  [ "$code" = 200 ] || die "$step" "PATCH /backend-apis/$CALC_BP_ID endpoint -> $code"
}

calc_ms_body() { # NAME
  echo "{\"name\": \"$1\", \"backend_profile_id\": \"$CALC_BP_ID\", \"spec_version\": 1, \"tools\": [\"Add\"]}"
}

calc_mcp_server() { # STEP BP_NAME MS_NAME DEPLOY_JSON
  CALC_MS_NAME="$3"
  calc_backend "$1" "$2"
  save_doc "$1" /mcp-servers "$3" "$(calc_ms_body "$3")"
  CALC_MS_ID="$SAVED_ID"
  deploy_ready "$1" /mcp-servers "$CALC_MS_ID" "$4"
  CALC_MS_K8S_NAME="$(jq_ 'd["k8s"]["name"]')"
  CALC_NS="$(jq_ 'd["k8s"]["namespace"]')"
}

calc_publish() { # STEP
  local step="$1" hub_item
  api GET "/mcp-servers/$CALC_MS_ID" >/dev/null
  hub_item="$(jq_ 'd["hub_item"]')"
  if [ "$hub_item" = None ]; then
    api GET /hub >/dev/null
    [ "$(jq_ "[i['id'] for i in d if i['kind'] == 'internal' and i['source']['instance_id'] == '$CALC_MS_ID']")" = "[]" ] \
      || die "$step" "$CALC_MS_NAME was auto-registered in the hub"
  fi
  CALC_PUBLISH_CODE="$(api POST "/mcp-servers/$CALC_MS_ID/publish" '{}')"
  case "$hub_item:$CALC_PUBLISH_CODE" in
    None:201) ;;
    None:*) die "$step" "publish $CALC_MS_NAME -> $CALC_PUBLISH_CODE" ;;
    *:200) ;;
    *) die "$step" "re-publish $CALC_MS_NAME -> $CALC_PUBLISH_CODE" ;;
  esac
  CALC_HUB_ID="$(jq_ 'd["id"]')"
  [ "$(jq_ '" ".join([d["kind"], d["name"], d["source"]["instance_id"]])')" = "internal $CALC_MS_NAME $CALC_MS_ID" ] \
    || die "$step" "published item: $(cat "$WORK/body")"
}

calc_gateway() { # STEP GW_NAME DEPLOY_JSON
  save_doc "$1" /gateways "$2" "{\"name\": \"$2\", \"backends\": [{\"hub_item_id\": \"$CALC_HUB_ID\", \"tools\": [\"Add\"]}]}"
  CALC_GW_ID="$SAVED_ID"
  deploy_ready "$1" /gateways "$CALC_GW_ID" "$3"
  CALC_GW_K8S_NAME="$(jq_ 'd["k8s"]["name"]')"
}

calc_teardown() {
  [ -z "$CALC_GW_ID" ] || api_delete "/gateways/$CALC_GW_ID"
  [ -z "$CALC_MS_ID" ] || api_delete "/mcp-servers/$CALC_MS_ID"
}
