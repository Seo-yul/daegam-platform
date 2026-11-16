# shellcheck shell=bash
# lab 의 Postgres·Redis(make lab-up, 네임스페이스 $SPIKE_NS)를 Database 로 등록하고 그 안을 들여다보는 한 벌 (e2e_agent·
# e2e_chat·e2e_instance·e2e_knowledge). `. "$HERE/lib/e2e_lab.sh"` 로 읽는다. 부르는 쪽이 읽기 전에 SPIKE_NS 를 두고,
# K(kubectl 배열)·HOME_ID 와 lib/e2e_api.sh(api·jq_·secret_field·register_data_service)·die(STEP MSG)를 둔다.
#
# psql_ SQL
#   lab Postgres 안에서 슈퍼유저로 한 줄 SQL 을 돌려 공백을 뺀 결과를 찍는다
# rcli ARGS...
#   lab Redis 안에서 redis-cli 를 돌린 raw 출력이다(--scan 은 키를 한 줄에 하나). 비밀번호는 파드 env 에서 풀어 쓰고 밖으로
#   꺼내지 않는다
# rcount PATTERN
#   그 패턴의 키 수를 찍는다
# rdel PATTERN
#   그 패턴의 키를 DEL 한 번으로 지우고 지운 수를 찍는다. 키가 없으면 0, DEL 이 실패하면 ? 다. 멈추지 않는다
# rforget INSTANCE_ID [SESSION_ID...]
#   그 Agent 의 Redis 키(daegam-agent:<id>:*)와 RediSearch 인덱스 셋, 넘긴 세션마다 체크포인터의 쓰기 레지스트리
#   (write_keys_zset:<sid>:*, 인스턴스 접두어 밖이다)를 지운다. Agent 삭제는 Redis 를 건드리지 않으므로(D33) 정리 단계가
#   부른다. 멈추지 않는다
# lab_ds_body KIND NAME DISPLAY
#   lab 의 KIND(postgres·redis)를 Database NAME 으로 등록하는 POST /data-services 본문을 찍는다. 자격은 lab Secret 에서 읽는다
# lab_database STEP KIND NAME DISPLAY
#   lab 의 KIND(postgres·redis) 를 Database NAME 으로 register_data_service 에 맡겨 등록하고 verify 한다. 이름이 이미
#   있으면(409) 자격을 lab Secret 에 맞춘다. postgres 는 pgvector 가 있어야 하고(Knowledge Base 가 쓴다) redis 는 체크포인터가
#   ready 여야 한다. DATA_SERVICE_ID 를 남긴다

LAB_PG_HOST="postgres.${SPIKE_NS}.svc"; LAB_PG_SECRET="postgres-credentials"
LAB_REDIS_HOST="redis.${SPIKE_NS}.svc"; LAB_REDIS_SECRET="redis-credentials"

psql_() { # SQL
  "${K[@]}" -n "$SPIKE_NS" exec deploy/postgres -- sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$0"' "$1" 2>/dev/null \
    | tr -d '[:space:]'
}

rcli() { # ARGS...
  "${K[@]}" -n "$SPIKE_NS" exec deploy/redis -- sh -c 'exec redis-cli -a "$REDIS_PASSWORD" --no-auth-warning "$@"' _ "$@"
}

rcount() { rcli --scan --pattern "$1" 2>/dev/null | grep -c . || true; } # PATTERN

# Agent 하나가 lab Redis 에 만드는 RediSearch 인덱스다. 체크포인터 둘과 장기 기억의 store 다(D33·D34)
LAB_REDIS_INDEXES="checkpoint checkpoint_write store"

rdel() { # PATTERN
  local keys n
  keys="$(rcli --scan --pattern "$1" 2>/dev/null | tr '\n' ' ' || true)"
  case "$keys" in *[![:space:]]*) ;; *) echo 0; return 0 ;; esac
  # 키는 uuid·hex·__empty__ 로만 이뤄져 단어 분리가 안전하고, set -f 로 글롭만 막는다
  set -f
  # shellcheck disable=SC2086  # 키 여럿을 낱말로 나눠 DEL 하나에 넘긴다
  n="$(rcli DEL $keys 2>/dev/null || true)"
  set +f
  echo "${n:-?}"
}

rforget() { # INSTANCE_ID [SESSION_ID...]
  local prefix="daegam-agent:$1:" idx sid
  shift
  rdel "${prefix}*" >/dev/null
  for sid in "$@"; do rdel "write_keys_zset:$sid:*" >/dev/null; done
  for idx in $LAB_REDIS_INDEXES; do rcli FT.DROPINDEX "$prefix$idx" >/dev/null 2>&1 || true; done
}

lab_ds_body() { # KIND NAME DISPLAY
  local kind="$1"
  if [ "$kind" = redis ]; then
    python3 -c 'import json,sys
print(json.dumps({"name": sys.argv[1], "display_name": sys.argv[2], "cluster_id": sys.argv[3], "kind": "redis",
  "host": sys.argv[4], "port": 6379, "db": 0, "password": sys.argv[5]}, ensure_ascii=False))' \
      "$2" "$3" "$HOME_ID" "$LAB_REDIS_HOST" "$(secret_field "$SPIKE_NS" "$LAB_REDIS_SECRET" password)"
  else
    python3 -c 'import json,sys
print(json.dumps({"name": sys.argv[1], "display_name": sys.argv[2], "cluster_id": sys.argv[3], "kind": "postgres",
  "host": sys.argv[4], "port": 5432, "database": sys.argv[5], "username": sys.argv[6], "password": sys.argv[7]},
  ensure_ascii=False))' \
      "$2" "$3" "$HOME_ID" "$LAB_PG_HOST" "$(secret_field "$SPIKE_NS" "$LAB_PG_SECRET" database)" \
      "$(secret_field "$SPIKE_NS" "$LAB_PG_SECRET" username)" "$(secret_field "$SPIKE_NS" "$LAB_PG_SECRET" password)"
  fi
}

lab_database() { # STEP KIND NAME DISPLAY
  local step="$1" kind="$2" name="$3" body secret
  secret="$LAB_PG_SECRET"; [ "$kind" != redis ] || secret="$LAB_REDIS_SECRET"
  "${K[@]}" -n "$SPIKE_NS" get svc "$kind" -o name >/dev/null 2>&1 && "${K[@]}" -n "$SPIKE_NS" get secret "$secret" -o name >/dev/null 2>&1 \
    || die "$step" "no Service $kind / Secret $secret in $SPIKE_NS (make lab-up)"
  body="$(lab_ds_body "$kind" "$name" "$4")"
  register_data_service "$step" "$name" "$body"
  if [ "$kind" = redis ]; then
    [ "$(jq_ '(d.get("redis") or {}).get("checkpointer")')" = ready ] \
      || die "$step" "lab Redis checkpointer is not ready (Redis 8 with RediSearch, make lab-up): $(cat "$WORK/body")"
  else
    [ "$(jq_ 'd["extensions"]["vector"]["available"]')" = True ] \
      || die "$step" "lab Postgres has no pgvector (pgvector/pgvector:pg16, make lab-up): $(cat "$WORK/body")"
  fi
}
