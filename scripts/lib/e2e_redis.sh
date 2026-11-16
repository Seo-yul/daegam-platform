# shellcheck shell=bash
# 설치본이 플랫폼 네임스페이스에 둔 Redis(StatefulSet redis)와 그것을 memory 로 쓰는 Agent 를 보는 한 벌 (e2e_install·e2e_runs).
# `. "$HERE/lib/e2e_redis.sh"` 로 읽는다. 부르는 쪽이 K(kubectl 배열)·PLATFORM_NS·WORK 를 두고 e2e_json.sh(deployment_env)와
# e2e_report.sh(die)를 먼저 읽는다.
#
# REDIS_TLS_CLI
#   파드 안의 redis-cli 가 TLS 포트로 붙는 명령줄이다 (P61 F3). 서버 인증서의 SAN 은 서비스 이름이고 redis-cli 는 사슬만
#   확인한다
# redis_exec SCRIPT [ARG...]
#   Redis 파드에서 SCRIPT 를 돈다. SCRIPT 안의 cli 는 TLS 포트의 redis-cli, $C 는 그 명령줄이고, ARG 는 $1 부터다
# redis_ds_body NAME CLUSTER_ID
#   플랫폼 네임스페이스의 Redis 를 TLS 끝점(6380, ConfigMap daegam-redis-ca 의 ca.crt)과 함께 Database NAME 으로 등록하는
#   본문을 찍는다 (e2e_install STEP 4, e2e_runs STEP 1). 비밀번호는 kubectl 이 표준 출력으로 넘긴 Secret daegam-redis-auth
#   에서 꺼내므로 프로세스 인자에 오르지 않는다. 읽지 못하면 0 이 아니다
# redis_agent_clean AGENT_ID [SESSION_ID...]
#   Agent 삭제는 Redis 를 건드리지 않는다(D33). 체크포인터가 그 인스턴스 접두어(daegam-agent:<id>:)로 만든 검색 인덱스와
#   키를 걷고, 주면 그 세션들의 접두어 밖 키(write_keys_zset:<세션>:*)도 걷는다. 명령 하나라도 실패하면 끝까지 걷은 뒤
#   0 이 아니다
# wait_memory_tls STEP NS DEPLOYMENT SECONDS
#   런타임이 redis_tls 를 알리면 재적용이 memory 바인딩을 TLS 끝점으로 다시 렌더한다 (P61 F3). 첫 렌더는 런타임 기록이
#   생기기 전이라 평문이고 다음 재적용 틱이 옮긴다. 그 Deployment 의 agent 컨테이너 env 에 <접두어>_TLS=1 이 SECONDS 초
#   안에 서고, 다시 렌더한 파드가 같은 시간 안에 Ready 가 되기를 기다린다. 그 env 줄을 MEMORY_TLS_ENV 에, 마지막으로 읽은
#   Deployment 를 $WORK/agent-deploy.json 에 남긴다. 못 하면 die STEP

REDIS_TLS_CLI='redis-cli --tls --cacert /tls/ca.crt -h 127.0.0.1 -p 6380'

redis_exec() { # SCRIPT [ARG...]
  local script="$1"; shift
  "${K[@]}" -n "$PLATFORM_NS" exec statefulset/redis -c redis -- sh -c \
    "export REDISCLI_AUTH=\"\$REDIS_PASSWORD\"; C='$REDIS_TLS_CLI'; cli() { \$C \"\$@\"; }; $script" sh "$@"
}

redis_ds_body() { # NAME CLUSTER_ID
  local ca
  ca="$("${K[@]}" -n "$PLATFORM_NS" get configmap daegam-redis-ca -o 'jsonpath={.data.ca\.crt}')" || return 1
  "${K[@]}" -n "$PLATFORM_NS" get secret daegam-redis-auth -o json | python3 -c '
import base64, json, sys
password = base64.b64decode(json.load(sys.stdin)["data"]["password"]).decode()
name, cluster, host, ca = sys.argv[1:5]
print(json.dumps({"name": name, "display_name": "설치본 Redis", "cluster_id": cluster, "kind": "redis", "host": host,
                  "port": 6379, "db": 0, "password": password, "tls": {"port": 6380, "ca": ca}}))
' "$1" "$2" "redis.$PLATFORM_NS.svc" "$ca"
}

# 파드의 sh 에는 pipefail 이 없으므로 훑은 키를 변수에 받아 실패를 따로 센다
redis_agent_clean() { # AGENT_ID [SESSION_ID...]
  redis_exec 'agent="$1"; shift; rc=0
    drop() { [ -z "$1" ] || printf "%s\n" "$1" | xargs $C DEL >/dev/null || rc=1; }
    if indexes="$(cli FT._LIST)"; then
      for i in $(printf "%s\n" "$indexes" | grep -F "daegam-agent:$agent:"); do cli FT.DROPINDEX "$i" >/dev/null || rc=1; done
    else
      rc=1
    fi
    if keys="$(cli --scan --pattern "daegam-agent:$agent:*")"; then drop "$keys"; else rc=1; fi
    for s in "$@"; do
      if keys="$(cli --scan --pattern "write_keys_zset:$s:*")"; then drop "$keys"; else rc=1; fi
    done
    exit "$rc"' "$@"
}

# shellcheck disable=SC2034  # MEMORY_TLS_ENV 는 부르는 쪽이 읽는다
MEMORY_TLS_ENV=""
wait_memory_tls() { # STEP NS DEPLOYMENT SECONDS
  local step="$1" ns="$2" name="$3" wait="$4" deadline
  MEMORY_TLS_ENV=""
  deadline=$((SECONDS + wait))
  while [ "$SECONDS" -lt "$deadline" ]; do
    "${K[@]}" -n "$ns" get deployment "$name" -o json >"$WORK/agent-deploy.json"
    MEMORY_TLS_ENV="$(deployment_env "$WORK/agent-deploy.json" agent | grep -E '^[A-Z0-9_]+_TLS=1$' || true)"
    [ -n "$MEMORY_TLS_ENV" ] && break
    sleep 5
  done
  [ -n "$MEMORY_TLS_ENV" ] \
    || die "$step" "the memory binding of $name did not move to the Redis TLS endpoint within ${wait}s"
  "${K[@]}" -n "$ns" rollout status "deployment/$name" --timeout="${wait}s" >/dev/null \
    || die "$step" "the pod of $name rendered with the Redis TLS endpoint did not become Ready"
}
