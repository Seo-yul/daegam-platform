#!/usr/bin/env bash
# 게이트의 DB 갈래가 쓰는 시험 저장소 셋을 이 머신의 바이너리로 띄우고 내린다. 컨테이너를 쓰지 않는다
# (docs/guide/local-dev.md 7절). 포트는 게이트 전용 컨테이너와 같고(MongoDB 27027, Postgres 5442, Redis 6389) 모두
# 127.0.0.1 에만 묶는다. 데이터는 DAEGAM_TEST_DB_DIR(기본 ~/.local/share/daegam-test-dbs)에 두고 지우지 않는다.
#   scripts/local-test-dbs.sh up      없으면 만들고 띄운 뒤 셋이 답할 때까지 기다린다
#   scripts/local-test-dbs.sh down    셋을 내린다
#   scripts/local-test-dbs.sh env     게이트에 넘길 주소를 export 줄로 찍는다:  eval "$(scripts/local-test-dbs.sh env)"
# 바이너리: mongod(MongoDB 7), Postgres 의 initdb·pg_ctl·pg_isready 와 pgvector, redis-stack-server(RediSearch 가 든
# Redis). PATH 에 없으면 Homebrew 의 자리(PG_BIN, 기본 /opt/homebrew/opt/postgresql@17/bin)에서 찾는다
set -euo pipefail

DATA="${DAEGAM_TEST_DB_DIR:-$HOME/.local/share/daegam-test-dbs}"
MONGO_PORT=27027 PG_PORT=5442 REDIS_PORT=6389 PG_PASSWORD=pw
PG_BIN="${PG_BIN:-/opt/homebrew/opt/postgresql@17/bin}"
PATH="$PATH:$PG_BIN"
# mongodb-community@7.0 은 Homebrew 가 opt 링크를 두지 않을 때가 있어 Cellar 의 판 폴더도 본다
for d in /opt/homebrew/opt/mongodb-community@7.0/bin /opt/homebrew/Cellar/mongodb-community@7.0/*/bin; do
  if [ -d "$d" ]; then PATH="$PATH:$d"; fi
done

die() { echo "local-test-dbs: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 가 없다 ($2)"; }

wait_for() {
  local what="$1"; shift
  for _ in $(seq 1 60); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  die "$what 가 60초 안에 답하지 않았다 (로그는 $DATA)"
}

mongo_ping() { (exec 3<>"/dev/tcp/127.0.0.1/$MONGO_PORT") 2>/dev/null; }

up() {
  need mongod "brew install mongodb/brew/mongodb-community@7.0"
  need pg_ctl "brew install postgresql@17 pgvector"
  need redis-stack-server "brew install --cask redis-stack/redis-stack/redis-stack-server"
  mkdir -p "$DATA/mongo" "$DATA/redis"
  if ! mongo_ping; then
    mongod --dbpath "$DATA/mongo" --port "$MONGO_PORT" --bind_ip 127.0.0.1 --fork --logpath "$DATA/mongo.log" \
      --pidfilepath "$DATA/mongo.pid" >/dev/null
  fi
  if [ ! -f "$DATA/pg/PG_VERSION" ]; then
    initdb -D "$DATA/pg" -U postgres --auth=scram-sha-256 --pwfile=<(printf '%s\n' "$PG_PASSWORD") >/dev/null
  fi
  if ! pg_isready -q -h 127.0.0.1 -p "$PG_PORT"; then
    pg_ctl -D "$DATA/pg" -l "$DATA/pg.log" -o "-p $PG_PORT -c listen_addresses=127.0.0.1 -k $DATA" start >/dev/null
  fi
  if ! redis-cli -p "$REDIS_PORT" ping 2>/dev/null | grep -q PONG; then
    redis-stack-server --port "$REDIS_PORT" --bind 127.0.0.1 --daemonize yes --dir "$DATA/redis" \
      --save "" --appendonly no --logfile "$DATA/redis.log" >/dev/null
  fi
  wait_for MongoDB mongo_ping
  wait_for Postgres pg_isready -q -h 127.0.0.1 -p "$PG_PORT"
  wait_for Redis sh -c "redis-cli -p $REDIS_PORT ping | grep -q PONG"
  echo "local-test-dbs: up (MongoDB $MONGO_PORT, Postgres $PG_PORT, Redis $REDIS_PORT)"
}

down() {
  # macOS 의 mongod 는 --shutdown 을 받지 않는다. pid 파일로 TERM 을 보내고, pid 파일 없이 뜬 것은 데이터 폴더로 찾는다
  local pid=""
  [ -f "$DATA/mongo.pid" ] && pid="$(cat "$DATA/mongo.pid")"
  [ -n "$pid" ] || pid="$(pgrep -f "mongod --dbpath $DATA/mongo" | head -1 || true)"
  if [ -n "$pid" ]; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  fi
  rm -f "$DATA/mongo.pid"
  if [ -f "$DATA/pg/PG_VERSION" ] && command -v pg_ctl >/dev/null 2>&1; then
    pg_ctl -D "$DATA/pg" stop -m fast >/dev/null 2>&1 || true
  fi
  redis-cli -p "$REDIS_PORT" shutdown nosave >/dev/null 2>&1 || true
  echo "local-test-dbs: down"
}

env_lines() {
  echo "export DAEGAM_TEST_MONGO_URL=mongodb://127.0.0.1:$MONGO_PORT"
  echo "export DAEGAM_TEST_PG_DSN='host=127.0.0.1 port=$PG_PORT dbname=postgres user=postgres password=$PG_PASSWORD'"
  echo "export DAEGAM_TEST_REDIS_URL=redis://127.0.0.1:$REDIS_PORT/0"
}

case "${1-}" in
  up) up ;;
  down) down ;;
  env) env_lines ;;
  *) echo "usage: $0 up|down|env" >&2; exit 2 ;;
esac
