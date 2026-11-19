# shellcheck shell=bash
# code mode(P56) e2e 도우미. e2e_gateway 의 8c 와 e2e_agent 의 11c 가 쓴다. `. "$HERE/lib/e2e_code_mode.sh"` 로 읽는다.
# 읽을 때 요구하는 변수는 없다.
#
# code mode Gateway 는 광고하는 Tool 이 모두 읽기여야 켜진다. calculator WSDL 의 오퍼레이션은 서버 주인의 표시가 없으면
# 쓰기라서, 두 게이트는 Add 와 Divide 를 읽기로 표시한 MCP Server 를 따로 띄워 그 Gateway 의 백엔드로 쓴다.
#
# code_read_body NAME BP_ID VERSION [DIVIDE_READ]
#   Backend API BP_ID 의 버전 VERSION(calculator WSDL) 위에 Add 와 Divide 를 내는 MCP Server NAME 의 정의 본문을 찍는다.
#   서버 주인의 표시가 Add 를 읽기로, Divide 를 DIVIDE_READ(true·false, 기본 true)로 둔다
# script_args SCRIPT
#   execute_tool_script 에 Starlark SCRIPT 를 보내는 인자 JSON 을 찍는다

# shellcheck disable=SC2034  # 부르는 쪽이 읽는다
SCRIPT_TOOL="execute_tool_script"

code_read_body() { # NAME BP_ID VERSION [DIVIDE_READ]
  local rules="{\"Add\": {\"read_only\": true}, \"Divide\": {\"read_only\": ${4:-true}}}"
  echo "{\"name\": \"$1\", \"backends\": [{\"backend_profile_id\": \"$2\", \"spec_version\": $3, \"tools\": [\"Add\", \"Divide\"], \"tool_rules\": $rules}]}"
}

script_args() { # SCRIPT
  python3 -c 'import json, sys; print(json.dumps({"script": sys.argv[1]}))' "$1"
}
