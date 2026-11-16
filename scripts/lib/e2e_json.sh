# shellcheck shell=bash
# e2e 게이트가 JSON 응답에서 값을 꺼내는 한 벌. `. "$HERE/lib/e2e_json.sh"` 로 읽는다.
#
# json_of FILE|- EXPR
#   FILE 의 JSON 을 d 로 읽고 파이썬 식 EXPR 을 찍는다. FILE 이 `-` 면 표준 입력이다.
#   식은 argv 로 넘긴다. 파이썬 소스에 보간하지 않으므로 따옴표가 든 식도 그대로 들어간다.
#   값이 튜플이면 (`d["a"], d["b"]`) 공백으로 이어 찍는다.
#
# 오류를 삼키지 않는다. 파일이 JSON 이 아니거나 식이 터지면 파이썬 오류가 stderr 로 나가고
# 0 이 아닌 코드로 끝난다. 삼키면 `[ "$(jq_ 'd["id"]')" = "x" ]` 가 서버 오류 본문을 받고도 조용히
# 빈 문자열을 비교하고 실패 메시지가 `expected x, got ` 로 끝나 원인이 안 보인다.
#
# 빈 값도 올바른 답인 자리에서는 호출부가 `|| true` 를 적는다. 아직 준비되지 않은 응답을 다시 읽는 폴링(wait_ready 의
# phase)이나, 바로 다음 줄이 `[ -n "$X" ] || die ...` 처럼 빈 값을 스스로 다루는 자리가 그렇다.
json_of() { # FILE|- EXPR
  python3 -c 'import json,sys
d = json.load(sys.stdin if sys.argv[1] == "-" else open(sys.argv[1]))
v = eval(sys.argv[2])
print(*v) if isinstance(v, tuple) else print(v)' "$1" "$2"
}

# jwt_json TOKEN PART
#   JWT 의 헤더(PART 0)나 클레임(PART 1)을 서명을 보지 않고 JSON 으로 찍는다. `| json_of - EXPR` 로
#   이어 읽는다.
jwt_json() { # TOKEN PART
  python3 -c 'import base64,sys
s = sys.argv[1].split(".")[int(sys.argv[2])]
print(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode())' "$1" "$2"
}

# epoch_of ISO
#   ISO 8601 시각(끝의 Z 도 받는다)을 유닉스 초로 찍는다
epoch_of() { # ISO
  python3 -c 'import sys
from datetime import datetime
print(int(datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00")).timestamp()))' "$1"
}

# mcp_token_env SERVER_ID
#   Agent 파드가 그 MCP Server 를 부르는 소스 토큰의 env 이름(DAEGAM_MCP_TOKEN_<영숫자만 대문자로 한 id>, P50 9절)을 찍는다.
#   토큰 Secret 의 키도 같은 이름이다
mcp_token_env() { # SERVER_ID
  printf 'DAEGAM_MCP_TOKEN_%s\n' "$(printf '%s' "$1" | tr -dc '[:alnum:]' | tr '[:lower:]' '[:upper:]')"
}

# providers_without FILE NAME...
#   GET /settings 본문 FILE 에서 이름이 NAME 인 provider 를 뺀 PATCH 본문 {"llm_providers": [...]} 을 찍는다.
#   PATCH 는 목록 전체를 바꾸므로 남는 provider 는 GET 이 준 그대로 보낸다. 필드를 골라 옮기면
#   고르지 않은 필드(embedding_model 등)가 다른 provider 에서 기본값으로 지워진다.
providers_without() { # FILE NAME...
  python3 -c 'import json,sys
s = json.load(open(sys.argv[1])); names = set(sys.argv[2:])
print(json.dumps({"llm_providers": [p for p in s["llm_providers"] if p["name"] not in names]}))' "$@"
}

# providers_with FILE PROVIDER_JSON...
#   GET /settings 본문 FILE 에 provider 를 더한 PATCH 본문을 찍는다. 같은 이름이 있으면 새 것으로 바꾸고, 남는
#   provider 는 providers_without 과 같은 까닭으로 GET 이 준 그대로 보낸다.
providers_with() { # FILE PROVIDER_JSON...
  python3 -c 'import json,sys
s = json.load(open(sys.argv[1])); added = [json.loads(a) for a in sys.argv[2:]]
names = {p["name"] for p in added}
print(json.dumps({"llm_providers": [p for p in s["llm_providers"] if p["name"] not in names] + added}))' "$@"
}

# CONFIG_SHA_ANNOTATION
#   Agent 파드 템플릿의 설정 지문 어노테이션 이름이다(k8s/templates/agent.py 의 같은 이름, P35 13절). 값 "0" 은 지문이
#   없는 옛 문서다. e2e_agent STEP 14 와 e2e_install STEP 8 이 template_annotation 으로 읽는다.
# shellcheck disable=SC2034  # 읽는 쪽은 이 파일을 읽은 스크립트다
CONFIG_SHA_ANNOTATION="daegam.devops.ai.kr/config-sha"

# template_annotation FILE|- NAME
#   Deployment JSON 에서 파드 템플릿 어노테이션 NAME 의 값을 찍는다. 없으면 빈 줄이다.
template_annotation() { # FILE|- NAME
  python3 -c 'import json,sys
d = json.load(sys.stdin if sys.argv[1] == "-" else open(sys.argv[1]))
print((d["spec"]["template"]["metadata"].get("annotations") or {}).get(sys.argv[2], ""))' "$1" "$2"
}

# deployment_env FILE|- CONTAINER
#   Deployment JSON 에서 그 컨테이너의 env 를 이름 순으로 한 줄에 하나 `이름=값` 으로 찍는다. Secret 참조는
#   `이름=secret:<Secret 이름>` 이다.
deployment_env() { # FILE|- CONTAINER
  python3 -c 'import json,sys
d = json.load(sys.stdin if sys.argv[1] == "-" else open(sys.argv[1]))
for c in d["spec"]["template"]["spec"]["containers"]:
    if c["name"] != sys.argv[2]:
        continue
    for e in sorted(c.get("env") or [], key=lambda e: e["name"]):
        ref = (e.get("valueFrom") or {}).get("secretKeyRef")
        print(e["name"] + "=" + ("secret:" + ref["name"] if ref else str(e.get("value", ""))))' "$1" "$2"
}
