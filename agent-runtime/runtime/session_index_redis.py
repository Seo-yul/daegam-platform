"""Redis 8 위의 세션 색인과 체크포인터 열기다(D33).

키는 모두 인스턴스 접두어(`daegam-agent:{instance_id}:`, dsn.py 의 redis_prefix) 아래라 같은 Redis
를 나눠 쓰는 다른 인스턴스와 섞이지 않는다. 체크포인터(redis_saver.py 의
ChainRedisSaver)·색인·카운터·전사가 클라이언트 하나를 나눠 쓴다(전사는 transcript.py 의
RedisTranscripts 다). 첨부 저장소(uploads.py 의 RedisUploads)만 본문을 바이트 그대로 담으려고
디코드하지 않는 클라이언트를 따로 연다. 색인의 인덱스는 여기서 만들지 않고 체크포인터의 asetup() 이
만든다.

TTL 과 기록 보존 기간을 가르는 규칙(D46)도 Postgres 와 같다. `expire` 가 세션을 닫고
`closed_before`·`delete` 가 보존 기간이 지난 행을 지운다. `expire` 는 리스 키가 있는 세션을 닫지
않는다(P41 9절).

백그라운드 실행의 세션(P41 9절)은 `run:{run_id}` STRING 이 세션 id 를 들고 실행 id 하나에 세션
하나를 지킨다. 만들기 스크립트(CREATE_LUA)가 그 키를 SET NX 로 잡은 뒤에만 해시와 정렬 집합을
쓰므로 같은 실행 id 의 두 만들기 가운데 하나만 이긴다. 키는 색인 행을 지울 때 함께 지운다. 실행
세션은 최근 순·사용자별 정렬 집합을 따로 둔다(`sessions:runs:*`). Agent 하나가 하루 500 번 돌고
보존 기간 동안 남으므로 채팅 목록이 실행 세션을 훑지 않게 하려는 것이다.

턴이 리스를 풀지 못하고 끝난 세션(P79 3절)은 해시 칸 `turn_open`(잡은 턴의 토큰)이 리스 키 없이
남는다. 잡기 스크립트가 그 칸을 적고 세션 id 를 `turns:open` 집합에 넣으며, 풀기와 되살리기
스크립트가 둘을 지운다. 잡기는 해시가 있고 그 칸이 없을 때만 된다. 시도 기록이 적고 마감이 지우는
실행 표식은 해시 칸 `turn_run`(JSON)이다. 세션 한 줄을 읽는 파이프라인의 리스 EXISTS 가 끊긴 표시를
함께 알리고(`orphan`), 그 사이에 바뀐 것은 되살리기 스크립트가 토큰과 표식을 다시 보아 거른다. 옛
런타임의 스크립트는 세 가지 모두 모르고 건드리지 않는다.

체크포인터의 `adelete_thread` 는 한 번에 다 지우지 못할 수 있어 `delete_redis_thread` 가 되풀이한다.
sweep 과 세션 지우기가 이 도우미를 같이 쓴다(sessions.py 의 `SessionStore.delete_checkpoints`). 남은
문서 수는 `adelete_thread` 가 찾는 것과 같은 redisvl 질의로 센다.
"""

from __future__ import annotations

import heapq
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.redis.util import to_storage_safe_id
from redis.exceptions import ResponseError
from redisvl.query import CountQuery
from redisvl.query.filter import Tag

from runtime.config import ConfigError
from runtime.counters import RedisCounters
from runtime.dsn import MEMORY_CODES, check_redis, redis_prefix, turn_lease_key
from runtime.fence import HELD_LEASE_LUA, fence_redis_client, refused
from runtime.sessions import (
    COMPUTED_FIELDS,
    RECORD_INT_FIELDS,
    RUNS_EXCLUDE,
    RUNS_INCLUDE,
    RUNS_ONLY,
    STATUS_ACTIVE,
    STATUS_CLOSED,
    STATUS_PENDING,
    STATUSES,
    CheckpointsRemain,
    DrainMarked,
    RepairPlan,
    RunsFilter,
    RunTaken,
    SessionRecord,
    SessionStore,
    check_runs,
    check_updatable,
    runs_match,
    turn_record_fields,
)
from runtime.transcript import TRANSCRIPT_LIMIT, RedisTranscripts, dumps, transcript_key
from runtime.turn_record import RunMarker, title_matches
from runtime.uploads import RedisUploads

if TYPE_CHECKING:
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

# saver·색인·카운터가 나눠 쓰는 클라이언트 하나의 공통 인자. socket_timeout 이 없으면 Redis 가
# 멎을 때 턴이 턴 타임아웃까지 매달린다
REDIS_CLIENT_ARGS: dict[str, Any] = {
    "decode_responses": True,
    "socket_connect_timeout": 3,
    "socket_timeout": 10,
    "health_check_interval": 30,
}
# asetup() 이 다른 파드와 겹쳐 `Index already exists` 가 나면 다시 부르는 상한. 인덱스 둘을
# 차례로 만들어 두 번째에서 다시 겹칠 수 있다(replicas 2, 100 라운드 실측: 1 회는 6 실패, 3 은 0)
REDIS_SETUP_RETRIES = 5
# totals 가 파이프라인 하나에 싣는 세션 수
REDIS_TOTALS_CHUNK = 500
# 제목 검색과 상태 목록의 실행 거르기(P41 9절)가 정렬 집합을 한 번에 읽는 줄 수
REDIS_SEARCH_PAGE = 200
# 이 인스턴스의 리스 키를 찾는 SCAN 한 번의 COUNT 다. 기본 10 이면 체크포인트 키가 많은 Redis 에서
# 왕복이 수천 번이다
SCAN_COUNT = 1000
# 승인자의 전체 목록을 제목으로 거르거나 상태 목록을 실행으로 거를 때 훑는 줄 수의 상한. 넘으면
# 그때까지 찾은 것만 준다
REDIS_SEARCH_SCAN_LIMIT = 10_000
# `adelete_thread` 를 되풀이하는 상한. 한 번에 체크포인트와 쓰기를 각각 1만 개까지만 찾으므로 쓰기
# 20만 개, 실행으로 약 1만 4천 번까지 지운다(F0 5번은 실행 한 번에 쓰기 14개였다)
REDIS_DELETE_ROUNDS = 20

# expire 가 후보 sid 마다 부르는 스크립트다. 점수를 보지 않는 ZREM 은 후보를 읽은 뒤 다른 파드가
# 턴을 끝낸 활성 세션을 닫으므로 점수가 아직 before 앞일 때만 닫는다. 리스 키가 있으면 도는 턴이
# 있는 것이라 닫지 않는다(P41 9절). KEYS 는 status:{active|pending}·status:closed·session:{sid}·
# turn_lease:{sid}, ARGV 는 sid 와 before 의 epoch 초다. 두 파드의 sweep 이 같은 sid 를 둘 다 얻지
# 못한다
EXPIRE_LUA = """if redis.call('EXISTS', KEYS[4]) == 1 then
  return 0
end
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if score and tonumber(score) < tonumber(ARGV[2]) then
  redis.call('ZREM', KEYS[1], ARGV[1])
  redis.call('ZADD', KEYS[2], score, ARGV[1])
  redis.call('HSET', KEYS[3], 'status', 'closed')
  return 1
end
return 0
"""

# 색인 한 줄 만들기다. 해시를 쓰고 정렬 집합 셋에 넣는다. 실행 세션이면 먼저 실행 키를 SET NX 로
# 잡고, 이미 있으면 아무것도 쓰지 않고 그 키가 든 세션 id 를 돌려준다(P41 9절). 만들었으면 nil 이다.
# KEYS 는 session:{sid}·최근 순 집합·사용자 집합(둘 다 세션이 속한 쪽의 것)·
# sessions:status:{status} 에 실행 세션이면 run:{run_id} 를 더한 것이고, ARGV 는 sid, 해시 칸(JSON
# 객체), 점수(last_turn_at)다
CREATE_LUA = """if KEYS[5] and not redis.call('SET', KEYS[5], ARGV[1], 'NX') then
  return redis.call('GET', KEYS[5])
end
for field, value in pairs(cjson.decode(ARGV[2])) do
  redis.call('HSET', KEYS[1], field, value)
end
for i = 2, 4 do
  redis.call('ZADD', KEYS[i], ARGV[3], ARGV[1])
end
return false
"""

# 턴 리스 잡기다(D65). 세션 해시가 있고 열린 표시(turn_open)가 없을 때만 SET NX PX 로 잡는다(P79
# 1절). 잡았으면 같은 스크립트 안에서 세션 해시의 turn_id 를 지우고(새 시도가 제 id 를 적기 전에 앞
# 시도의 이름으로 온 중지가 새 턴을 멈추지 않게 한다, P78 2절) 열린 표시를 적으며 turns:open 에
# 넣는다. 지우기의 잡기(ARGV[4] 가 '1')만은 끊긴 열린 표시 위에서도 잡고, 그 토큰의 실행 표식을 지워
# 돌려준다. 인스턴스의 drain 표시가 서 있으면 무엇보다 먼저 물러선다(P44 7절). 돌려주는 것은
# {0}(못 잡음), {1}(잡음), {1, 표식}, {2}(drain 표시)다. KEYS 는 turn_lease:{sid}·session:{sid}·
# turns:open·drain 이고 ARGV 는 토큰, 만료(ms), 세션 id, 지우기 표시다
CLAIM_LUA = """if redis.call('EXISTS', KEYS[4]) == 1 then
  return {2}
end
if redis.call('EXISTS', KEYS[2]) == 0 then
  return {0}
end
local open = redis.call('HGET', KEYS[2], 'turn_open')
if open and ARGV[4] ~= '1' then
  return {0}
end
if not redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2]) then
  return {0}
end
redis.call('HDEL', KEYS[2], 'turn_id')
redis.call('HSET', KEYS[2], 'turn_open', ARGV[1])
redis.call('SADD', KEYS[3], ARGV[3])
if not open then
  return {1}
end
local run = redis.call('HGET', KEYS[2], 'turn_run')
if run and cjson.decode(run)['token'] == open then
  redis.call('HDEL', KEYS[2], 'turn_run')
  return {1, run}
end
return {1}
"""

# drain 표시 세우기다(P44 7절). 표시의 id 를 값으로 만료(ms)와 함께 세우고(이미 있으면 id 와 만료를
# 바꾼다) 같은 스크립트에서 이 인스턴스의 살아 있는 리스를 센다. 리스는 잡기가 turns:open 에 넣은
# 세션 가운데 리스 키가 있는 것이다. 리스 키는 세션마다 달라 KEYS 로 미리 줄 수 없으므로
# 접두어(ARGV[2])에서 만든다. 단일 Redis 라 받는다. 잡기도 스크립트 하나라 세기 앞에 잡혀 수에
# 들거나 표시를 보고 물러선다. KEYS 는 drain·turns:open 이고 ARGV 는 만료(ms), 리스 키의 접두어,
# 표시의 id 다
DRAIN_LUA = """redis.call('SET', KEYS[1], ARGV[3], 'PX', ARGV[1])
local live = 0
for _, sid in ipairs(redis.call('SMEMBERS', KEYS[2])) do
  live = live + redis.call('EXISTS', ARGV[2] .. sid)
end
return live
"""

# drain 표시 지우기다(P44 7절). 값이 그 id(ARGV[1])일 때만 지운다. KEYS 는 drain 이다
CLEAR_DRAIN_LUA = """if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

# 중지 요청이다(D65). 살아 있는 리스의 토큰을 표시 키에 옮겨 적되 리스의 남은 만료를 그대로 주고,
# 그 토큰을 돌려준다. KEYS 는 turn_lease:{sid}·turn_cancel:{sid}·session:{sid} 이고 ARGV 는 시도의
# id 다. 비어 있지 않으면 세션 해시의 turn_id 가 그것일 때만 적는다(P78 2절). 리스가 없으면 false
# 라 호출자가 409 를 낸다
CANCEL_LUA = """local token = redis.call('GET', KEYS[1])
if not token then
  return false
end
if ARGV[1] ~= '' and redis.call('HGET', KEYS[3], 'turn_id') ~= ARGV[1] then
  return false
end
local ttl = redis.call('PTTL', KEYS[1])
if ttl > 0 then
  redis.call('SET', KEYS[2], token, 'PX', ttl)
else
  redis.call('SET', KEYS[2], token)
end
return token
"""

# 리스 풀기다. 값이 제 토큰일 때만 리스와 표시를 함께 지운다. 만료 뒤 다른 턴이 잡은 리스는
# 건드리지 않는다. 열린 표시도 제 토큰일 때만 지우고 turns:open 에서 뺀다(P79 3절). KEYS 는
# CANCEL_LUA 의 앞 둘에 session:{sid}·turns:open 을 더한 것이고 ARGV 는 토큰과 세션 id 다
RELEASE_LUA = """if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('DEL', KEYS[1], KEYS[2])
end
if redis.call('HGET', KEYS[3], 'turn_open') == ARGV[1] then
  redis.call('HDEL', KEYS[3], 'turn_open')
  redis.call('SREM', KEYS[4], ARGV[2])
end
return 1
"""

# 리스 갱신이다(P79 1절). 리스가 제 토큰이고 세션 해시가 있을 때만 리스와, 같은 토큰의 중지
# 표시를 늘린다. 표시를 늘려야 중지가 턴만큼 산다. 아니면 0 이고 아무 키도 만들지 않는다. KEYS 는
# CANCEL_LUA 와 같고 ARGV 는 토큰과 만료(ms)다
RENEW_LUA = """if redis.call('GET', KEYS[1]) ~= ARGV[1] or redis.call('EXISTS', KEYS[3]) == 0 then
  return 0
end
redis.call('PEXPIRE', KEYS[1], ARGV[2])
if redis.call('GET', KEYS[2]) == ARGV[1] then
  redis.call('PEXPIRE', KEYS[2], ARGV[2])
end
return 1
"""

# 색인 한 줄의 쓰기다. 해시 칸을 쓰고 지우며 정렬 집합을 옮기고, 쓴 뒤의 해시와 리스 키가 있는지를
# 돌려준다. 해시가 없으면 0 이고 아무것도 만들지 않는다. 턴 자신의 쓰기(`update_held`)는
# 앞머리(HELD_LEASE_LUA)가 리스의 토큰을 보고 아니면 0 이다(P79 2절). KEYS 는 session:{sid}·
# turn_lease:{sid}·최근 순 집합·사용자 집합(둘 다 세션이 속한 쪽의 것)·
# sessions:status:{active,pending,closed} 이고, ARGV 는 토큰(턴의 쓰기가 아니면 빈 값), 세션 id,
# 쓸 칸(JSON 객체), 지울 칸(JSON 배열), 옮길 상태 집합의 KEYS 차례(없으면 빈 값), 점수
# (last_turn_at, 없으면 빈 값), 마감 표시('1' 이면 이 토큰의 실행 표식 turn_run 을 지운다, P79
# 3절)다. 시도 기록의 실행 표식은 쓸 칸의 turn_run 으로 온다
WRITE_LUA = (
    HELD_LEASE_LUA
    + """if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
local sid = ARGV[2]
for field, value in pairs(cjson.decode(ARGV[3])) do
  redis.call('HSET', KEYS[1], field, value)
end
for _, field in ipairs(cjson.decode(ARGV[4])) do
  redis.call('HDEL', KEYS[1], field)
end
if ARGV[7] == '1' then
  local run = redis.call('HGET', KEYS[1], 'turn_run')
  if run and cjson.decode(run)['token'] == ARGV[1] then
    redis.call('HDEL', KEYS[1], 'turn_run')
  end
end
if ARGV[5] ~= '' then
  for i = 5, #KEYS do
    redis.call('ZREM', KEYS[i], sid)
  end
  redis.call('ZADD', KEYS[tonumber(ARGV[5])], ARGV[6], sid)
end
if ARGV[6] ~= '' then
  redis.call('ZADD', KEYS[3], ARGV[6], sid)
  redis.call('ZADD', KEYS[4], ARGV[6], sid)
  if ARGV[5] == '' then
    for i = 5, #KEYS do
      redis.call('ZADD', KEYS[i], 'XX', ARGV[6], sid)
    end
  end
end
local row = redis.call('HGETALL', KEYS[1])
table.insert(row, 1, redis.call('EXISTS', KEYS[2]))
return row
"""
)


# 되살리기와 상태 맞추기 스크립트의 앞머리다. KEYS[1] 은 session:{sid}, KEYS[2] 는 turn_lease:{sid},
# KEYS[3..5] 는 sessions:status:{active,pending,closed} 이고, ARGV[1] 은 세션 id, ARGV[2] 는 새
# 상태(빈 값이면 그대로), ARGV[3] 은 새로 보류가 될 때의 pending_since, ARGV[4] 는 상태 집합의 점수
# (last_turn_at)다. set_status 는 닫힌 세션을 건드리지 않는다. row 는 WRITE_LUA 처럼 리스 키가
# 있는지와 해시를 돌려준다
STATUS_LUA = """local function set_status()
  if ARGV[2] == '' or redis.call('HGET', KEYS[1], 'status') == 'closed' then
    return
  end
  redis.call('HSET', KEYS[1], 'status', ARGV[2])
  if ARGV[2] ~= 'pending' then
    redis.call('HDEL', KEYS[1], 'pending_since')
  elseif redis.call('HEXISTS', KEYS[1], 'pending_since') == 0 then
    redis.call('HSET', KEYS[1], 'pending_since', ARGV[3])
  end
  local target = {active = 3, pending = 4, closed = 5}
  for i = 3, 5 do
    redis.call('ZREM', KEYS[i], ARGV[1])
  end
  redis.call('ZADD', KEYS[target[ARGV[2]]], ARGV[4], ARGV[1])
end
local function row()
  local fields = redis.call('HGETALL', KEYS[1])
  table.insert(fields, 1, redis.call('EXISTS', KEYS[2]))
  return fields
end
"""

# 끊긴 열린 표시의 되살리기다(P79 3절 4단계). 리스 키가 없고 열린 표시가 그 토큰이며 실행 표식과
# last_turn_at 이 읽은 값 그대로일 때만 쓴다. last_turn_at 을 보는 것은 읽은 뒤 옛 파드가 표시를
# 모른 채 잡고 마감한 턴(배포 도중)을 놓치지 않으려는 것이다. 상태를 맞추고, 열린 표시·실행 표식·
# 중지 표시를 지우고 turns:open 에서 빼며, 정한 항목을 전사에 붙이고 상한으로 자른다. 진 쪽은
# 아무것도 쓰지 않고 0 이다. KEYS[6..8] 은 turn_cancel:{sid}·turns:open·transcript:{sid} 이고
# ARGV[5..] 는 그 토큰, 읽은 실행 표식(없으면 빈 값), 읽은 last_turn_at, 전사 상한, 붙일 항목들이다
REPAIR_LUA = (
    STATUS_LUA
    + """if redis.call('EXISTS', KEYS[2]) == 1
   or redis.call('HGET', KEYS[1], 'turn_open') ~= ARGV[5] then
  return 0
end
if (redis.call('HGET', KEYS[1], 'turn_run') or '') ~= ARGV[6]
   or redis.call('HGET', KEYS[1], 'last_turn_at') ~= ARGV[7] then
  return 0
end
set_status()
redis.call('HDEL', KEYS[1], 'turn_open', 'turn_run')
redis.call('DEL', KEYS[6])
redis.call('SREM', KEYS[7], ARGV[1])
if #ARGV >= 9 then
  redis.call('RPUSH', KEYS[8], unpack(ARGV, 9))
  redis.call('LTRIM', KEYS[8], -tonumber(ARGV[8]), -1)
end
return row()
"""
)

# P79 앞에 막힌 세션의 상태 맞추기다(P79 3절). 리스 키도 열린 표시도 없고 last_turn_at 이 읽은
# 값(ARGV[5])이며 닫히지 않았고 다른 상태일 때만 옮긴다. 아니면 0 이다
ALIGN_LUA = (
    STATUS_LUA
    + """if redis.call('EXISTS', KEYS[2]) == 1
   or redis.call('HEXISTS', KEYS[1], 'turn_open') == 1 then
  return 0
end
if redis.call('HGET', KEYS[1], 'last_turn_at') ~= ARGV[5] then
  return 0
end
local status = redis.call('HGET', KEYS[1], 'status')
if status == 'closed' or status == ARGV[2] then
  return 0
end
set_status()
return row()
"""
)

# sweep 의 turns:open 정리다(P79 3절). 세션 해시가 사라졌거나(옛 파드의 지우기와 정리) 열린 표시가
# 없으면 그 줄을 빼고 1 이다. KEYS 는 session:{sid}·turns:open 이고 ARGV 는 세션 id 다
PRUNE_OPEN_LUA = """if redis.call('EXISTS', KEYS[1]) == 0
   or redis.call('HEXISTS', KEYS[1], 'turn_open') == 0 then
  redis.call('SREM', KEYS[2], ARGV[1])
  return 1
end
return 0
"""


def _epoch(when: datetime) -> float:
    """ZSET 점수다. tz 가 없는 값은 UTC 로 본다."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.timestamp()


def _hash_value(value: Any) -> str:
    """HASH 필드 값이다. 시각은 isoformat(UTC, tz 포함), 참거짓은 "1"·"0", 정수·문자열은 str
    이다."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return str(value)


def _to_hash(record: SessionRecord) -> dict[str, str]:
    """SessionRecord 를 HASH 필드로 옮긴다. 시각은 isoformat(UTC), 정수는 str,
    None 은 필드 없음이다. 읽을 때 정하는 칸(running)은 쓰지 않는다."""
    return {
        key: _hash_value(value)
        for key, value in asdict(record).items()
        if value is not None and key not in COMPUTED_FIELDS
    }


def _from_hash(mapping: Mapping[str, str], *, running: bool = False) -> SessionRecord:
    """해시 한 줄의 SessionRecord 다. 리스 키가 없는데 열린 표시가 남았으면 그 토큰이 orphan 이다
    (P79 3절). 옛 해시처럼 새 칸이 없으면 예전과 같다."""

    def when(key: str) -> datetime | None:
        raw = mapping.get(key)
        return datetime.fromisoformat(raw) if raw else None

    created_at = when("created_at")
    last_turn_at = when("last_turn_at")
    if created_at is None or last_turn_at is None:
        raise ValueError("세션 해시에 created_at·last_turn_at 이 없다")
    return SessionRecord(
        session_id=mapping["session_id"],
        tenant=mapping.get("tenant"),
        user_sub=mapping["user_sub"],
        created_at=created_at,
        last_turn_at=last_turn_at,
        turns=int(mapping.get("turns") or 0),
        usage_input=int(mapping.get("usage_input") or 0),
        usage_output=int(mapping.get("usage_output") or 0),
        status=mapping.get("status") or STATUS_ACTIVE,
        pending_since=when("pending_since"),
        instance_id=mapping.get("instance_id"),
        running=running,
        orphan=None if running else mapping.get("turn_open"),
        outside_at=when("outside_at"),
        run_id=mapping.get("run_id"),
        untrusted=mapping.get("untrusted") == "1",
        **turn_record_fields(mapping),
    )


# runs 값마다 읽는 정렬 집합이다. 거짓이 채팅 집합, 참이 실행 집합이다(P41 9절)
_SETS_FOR: dict[str, tuple[bool, ...]] = {
    RUNS_EXCLUDE: (False,),
    RUNS_ONLY: (True,),
    RUNS_INCLUDE: (False, True),
}


def _row_of(written: list[Any]) -> SessionRecord:
    """WRITE_LUA·REPAIR_LUA·ALIGN_LUA 가 돌려준 (리스 키가 있는지, 해시) 의 한 줄이다."""
    leased, *pairs = written
    return _from_hash(dict(zip(pairs[::2], pairs[1::2], strict=True)), running=bool(leased))


def _marker_of(raw: str | None, token: str) -> RunMarker | None:
    """해시 칸 turn_run 의 실행 표식이다. 그 토큰의 것이 아니거나 읽을 수 없으면 None 이다."""
    if not raw:
        return None
    try:
        marker = RunMarker.from_json(raw)
    except (ValueError, KeyError, TypeError):
        return None
    return marker if marker.token == token else None


def _newest(found: Iterable[list[SessionRecord]], limit: int) -> list[SessionRecord]:
    """집합마다 최근 순으로 읽은 목록을 최근 순으로 합쳐 앞 limit 줄이다. 두 집합에 함께 든 세션은
    한 번만 싣는다. 실행을 모르는 옛 런타임의 파드가 실행 세션을 고쳐 쓰면 채팅 집합에도 넣는다."""
    out: list[SessionRecord] = []
    seen: set[str] = set()
    for row in heapq.merge(*found, key=lambda row: row.last_turn_at, reverse=True):
        if row.session_id in seen:
            continue
        seen.add(row.session_id)
        out.append(row)
        if len(out) >= limit:
            break
    return out


class RedisSessionIndex:
    """memory 의 Redis 에 두는 색인이다. 파드가 여럿이어도 같은 것을 본다.

    키는 모두 접두어(`daegam-agent:{instance_id}:`) 아래라 같은 Redis 를 나눠 쓰는 다른 인스턴스와
    섞이지 않는다. `session:{sid}` HASH 가 SessionRecord, `sessions:by_turn`·`sessions:user:{sub}`·
    `sessions:status:{active|pending|closed}` ZSET 의 점수는 last_turn_at 의 epoch 초다. 최근 순과
    사용자별 집합은 채팅 집합과 실행 집합으로 나뉜다. 위의 둘은 채팅 세션만,
    `sessions:runs:by_turn`·`sessions:runs:user:{sub}` 는 실행 세션만 든다(P41 9절). 그래서 최근 순
    목록과 사용자 목록은 다른 쪽 세션을 읽지 않는다. 상태 집합은 모든 세션을 들고, 상태 목록은 읽은
    줄을 runs 로 거른다.
    `turn_lease:{sid}` STRING 이 진행 중 턴의 임차(값은 턴 토큰), `turn_cancel:{sid}` STRING 이 중지
    표시다(D65). 해시 칸 `turn_open`·`turn_run` 과 SET `turns:open` 은 열린 표시와 실행 표식이다(P79
    3절). `run:{run_id}` STRING 은 실행 id 를 쥔 세션 id 다(P41 9절). `drain` STRING 은 만료가 있는
    인스턴스의 drain 표시이고 값은 그 id 다. 잡기 스크립트가 맨 먼저 본다(P44 7절). 세션 한 줄의
    running 은 읽을 때 리스 키가 있는지로 정한다(P78 2절). 지우기는 MULTI 하나이고 읽은 값으로
    분기하지 않는다(읽기 뒤 MULTI 는 두 파드 사이에서 한 세션이 두 상태 집합에 남는다).
    만들기(CREATE_LUA), 한 줄의 쓰기(WRITE_LUA)와 값을 보고 갈라야 하는 리스는 Lua 하나다.
    """

    def __init__(self, client: Any, prefix: str, instance_id: str | None = None) -> None:
        # redis.asyncio.Redis 다. 명령마다 반환형이 달라 타입이 얇으므로 Any 로 둔다
        self._client = client
        self._prefix = prefix
        self._instance_id = instance_id
        self._expire_script = client.register_script(EXPIRE_LUA)
        self._cancel_script = client.register_script(CANCEL_LUA)
        self._claim_script = client.register_script(CLAIM_LUA)
        self._drain_script = client.register_script(DRAIN_LUA)
        self._clear_drain_script = client.register_script(CLEAR_DRAIN_LUA)
        self._release_script = client.register_script(RELEASE_LUA)
        self._renew_script = client.register_script(RENEW_LUA)
        self._write_script = client.register_script(WRITE_LUA)
        self._create_script = client.register_script(CREATE_LUA)
        self._repair_script = client.register_script(REPAIR_LUA)
        self._align_script = client.register_script(ALIGN_LUA)
        self._prune_script = client.register_script(PRUNE_OPEN_LUA)

    def _key(self, suffix: str) -> str:
        return f"{self._prefix}{suffix}"

    def _session_key(self, session_id: str) -> str:
        return self._key(f"session:{session_id}")

    def _lease_keys(self, session_id: str) -> list[str]:
        """리스와 중지 표시의 키다. Lua 둘과 delete 가 같은 차례로 쓴다."""
        return [turn_lease_key(self._prefix, session_id), self._key(f"turn_cancel:{session_id}")]

    def _cancel_keys(self, session_id: str) -> list[str]:
        """CANCEL_LUA·RENEW_LUA 의 키다. 리스와 표시에 세션 해시를 더한다(중지는 시도의 id 를
        읽고, 갱신은 해시가 있는지 본다)."""
        return [*self._lease_keys(session_id), self._session_key(session_id)]

    def _status_key(self, status: str) -> str:
        return self._key(f"sessions:status:{status}")

    def _status_keys(self) -> list[str]:
        """상태 집합 셋이다. STATUSES 차례(active, pending, closed)다."""
        return [self._status_key(name) for name in STATUSES]

    def _open_key(self) -> str:
        """끊길 수 있는 열린 표시를 가진 세션의 집합이다(P79 3절). sweep 이 훑지 않고 찾는다."""
        return self._key("turns:open")

    def _drain_key(self) -> str:
        """인스턴스의 drain 표시다(P44 7절). 만료가 있는 STRING 이다."""
        return self._key("drain")

    def _sets(self, run: bool) -> str:
        """최근 순·사용자 집합 이름의 앞머리다. run 이면 실행 세션의 집합이다(P41 9절)."""
        return "sessions:runs:" if run else "sessions:"

    def _user_key(self, user_sub: str, run: bool) -> str:
        return self._key(f"{self._sets(run)}user:{user_sub}")

    def _by_turn_key(self, run: bool) -> str:
        return self._key(f"{self._sets(run)}by_turn")

    def _by_turn_keys(self) -> list[str]:
        """채팅과 실행의 최근 순 집합이다. 합계와 해시 없는 지우기가 둘 다 본다."""
        return [self._by_turn_key(False), self._by_turn_key(True)]

    def _run_key(self, run_id: str) -> str:
        return self._key(f"run:{run_id}")

    async def create(self, record: SessionRecord) -> None:
        """스크립트 하나(CREATE_LUA)에 HSET 과 ZADD 셋이다. 같은 sid 가 다시 오면 덮어쓴다(호출자가
        uuid4 로 만들어 재시도가 없다. Postgres 의 PK 위반과 다른 점이다). 실행 세션이면 같은
        스크립트가 실행 키를 먼저 잡고, 이미 있으면 아무것도 쓰지 않아 RunTaken 이다(P41 9절)."""
        if record.instance_id is None and self._instance_id is not None:
            record = replace(record, instance_id=self._instance_id)
        sid = record.session_id
        keys = [
            self._session_key(sid),
            self._by_turn_key(record.run_id is not None),
            self._user_key(record.user_sub, record.run_id is not None),
            self._status_key(record.status),
        ]
        if record.run_id is not None:
            keys.append(self._run_key(record.run_id))
        holder = await self._create_script(
            keys=keys,
            args=[sid, json.dumps(_to_hash(record)), str(_epoch(record.last_turn_at))],
        )
        if holder is not None:
            raise RunTaken(holder)

    async def get(self, session_id: str) -> SessionRecord | None:
        rows = await self._fetch([session_id])
        return rows[0] if rows else None

    async def find_run(self, run_id: str) -> SessionRecord | None:
        """실행 키가 든 세션 id 의 한 줄이다. 키는 색인 행을 지울 때까지 남아 닫힌 세션도 찾는다."""
        holder = await self._client.get(self._run_key(run_id))
        return await self.get(holder) if holder else None

    async def _fetch(self, session_ids: Iterable[str]) -> list[SessionRecord]:
        """sid 목록의 해시와 리스 키를 파이프라인 하나(HGETALL, EXISTS)로 읽는다. 지워진 해시는
        뺀다."""
        ids = list(session_ids)
        if not ids:
            return []
        pipe = self._client.pipeline(transaction=False)
        for sid in ids:
            pipe.hgetall(self._session_key(sid))
            pipe.exists(self._lease_keys(sid)[0])
        results = await pipe.execute()
        pairs = zip(results[::2], results[1::2], strict=True)
        return [_from_hash(row, running=bool(leased)) for row, leased in pairs if row]

    async def list(
        self,
        *,
        user_sub: str | None = None,
        status: str | None = None,
        limit: int = 100,
        q: str | None = None,
        runs: RunsFilter = RUNS_EXCLUDE,
    ) -> list[SessionRecord]:
        """runs(P41 9절)는 읽을 집합을 고른다. exclude 는 채팅 집합만, only 는 실행 집합만, include
        는 둘을 읽어 최근 순으로 합친다. 상태 목록만은 모든 세션을 든 상태 집합을 읽는다. 어느
        길이든 읽은 줄을 runs 로 다시 거른다. 실행을 모르는 옛 런타임의 파드(배포 도중)가 고쳐 쓴
        실행 세션은 채팅 집합에도 들기 때문이다. 제목 검색은 페이지 단위로 읽는다(`_search`)."""
        check_runs(runs)
        if limit <= 0:
            return []
        sets = _SETS_FOR[runs]

        def titled(row: SessionRecord) -> bool:
            return title_matches(row.title, q)

        def kept(row: SessionRecord) -> bool:
            return status in (None, row.status) and titled(row) and runs_match(row.run_id, runs)

        if user_sub is not None:
            # 한 사람의 채팅 세션은 적으므로 전부 읽고 상태와 제목으로 거른다. 실행 세션은 묶인
            # 사람에게 하루 수백 개씩 쌓일 수 있어 limit 까지만 페이지 단위로 읽는다
            found: list[list[SessionRecord]] = []
            for run in sets:
                key = self._user_key(user_sub, run)
                if run:
                    found.append(await self._search(key, kept, limit))
                    continue
                rows = await self._fetch(await self._client.zrevrange(key, 0, -1))
                found.append([row for row in rows if kept(row)])
            return _newest(found, limit)
        if status is not None:
            return await self._search(self._status_key(status), kept, limit)
        if q is not None:
            searched = [await self._search(self._by_turn_key(run), kept, limit) for run in sets]
            return _newest(searched, limit)
        # 집합마다 앞 limit 줄을 읽어 거른 뒤 최근 순으로 합친다
        recent = [
            [
                row
                for row in await self._fetch(
                    await self._client.zrevrange(self._by_turn_key(run), 0, limit - 1)
                )
                if kept(row)
            ]
            for run in sets
        ]
        return _newest(recent, limit)

    async def _search(
        self, source: str, kept: Callable[[SessionRecord], bool], limit: int
    ) -> list[SessionRecord]:
        """정렬 집합을 최근 순으로 페이지 단위로 읽으며 kept 를 지나는 행을 모은다. limit 개를
        채우거나 REDIS_SEARCH_SCAN_LIMIT 줄을 훑으면 멈춘다."""
        found: list[SessionRecord] = []
        for start in range(0, REDIS_SEARCH_SCAN_LIMIT, REDIS_SEARCH_PAGE):
            ids = await self._client.zrevrange(source, start, start + REDIS_SEARCH_PAGE - 1)
            found.extend(row for row in await self._fetch(ids) if kept(row))
            if len(found) >= limit or len(ids) < REDIS_SEARCH_PAGE:
                break
        return found[:limit]

    async def update(self, session_id: str, **fields: Any) -> SessionRecord | None:
        """해시 칸 쓰기(None 은 지우기)와 정렬 집합 갱신을 스크립트 하나(WRITE_LUA)로 하고 쓴 뒤의
        한 줄을 돌려준다.

        status 가 오면 last_turn_at 도 와야 한다. 상태 ZSET 의 점수가 by_turn·해시와 어긋나지
        않게 하려는 것이다. user_sub 와 run_id(어느 쪽 정렬 집합인가)는 만들 때 정해져 바뀌지
        않으므로 스크립트 앞에 HMGET 으로 읽고, user_sub 가 없으면 sid 가 없는 것이라 None 을
        돌려주고 쓰지 않는다(Postgres 의 0 행과 같다).
        """
        check_updatable(fields)
        if not fields:
            return await self.get(session_id)
        return await self._write(session_id, fields, token="")

    async def update_held(
        self,
        session_id: str,
        token: str,
        *,
        run: RunMarker | None = None,
        settle: bool = False,
        **fields: Any,
    ) -> SessionRecord | None:
        """`update` 와 같은 스크립트가 앞머리에서 리스의 토큰을 본다. 0 이면 리스를 잃은
        것이다(`fence.refused`). 빈 토큰은 스크립트가 검사 없는 쓰기로 읽으므로 받지 않는다. 실행
        표식은 같은 스크립트가 해시 칸 turn_run 에 적거나(run) 이 토큰의 것만 지운다(settle)."""
        if not token:
            raise ValueError("a held write needs the lease token")
        check_updatable(fields)
        row = await self._write(session_id, fields, token=token, run=run, settle=settle)
        if row is None:
            raise refused()
        return row

    async def _write(
        self,
        session_id: str,
        fields: Mapping[str, Any],
        *,
        token: str,
        run: RunMarker | None = None,
        settle: bool = False,
    ) -> SessionRecord | None:
        status = fields.get("status")
        if "status" in fields:
            if status not in STATUSES:
                raise ValueError(f"모르는 상태다: {status!r}")
            if "last_turn_at" not in fields:
                raise ValueError("status 를 바꾸려면 last_turn_at 도 함께 줘야 한다")
        if "last_turn_at" in fields and fields["last_turn_at"] is None:
            raise ValueError("last_turn_at 은 None 일 수 없다 (정렬 집합의 점수다)")
        session_key = self._session_key(session_id)
        user_sub, run_id = await self._client.hmget(session_key, ["user_sub", "run_id"])
        if user_sub is None:
            return None
        values = {key: _hash_value(value) for key, value in fields.items() if value is not None}
        if run is not None:
            values["turn_run"] = run.to_json()
        removed = [key for key, value in fields.items() if value is None]
        written = await self._write_script(
            keys=[
                session_key,
                turn_lease_key(self._prefix, session_id),
                self._by_turn_key(run_id is not None),
                self._user_key(user_sub, run_id is not None),
                *(self._status_key(name) for name in STATUSES),
            ],
            args=[
                token,
                session_id,
                json.dumps(values),
                json.dumps(removed),
                str(5 + STATUSES.index(status)) if status is not None else "",
                str(_epoch(fields["last_turn_at"])) if "last_turn_at" in fields else "",
                "1" if settle else "",
            ],
        )
        return _row_of(written) if written else None

    async def claim_turn(self, session_id: str, *, lease_seconds: float, token: str) -> bool:
        """진행 중 턴을 파드 사이에서 하나로 막는다(CLAIM_LUA). SET NX PX 라 임차를 가져간 쪽만
        참이고 만료가 있어서 파드가 죽어도 세션이 영영 막히지 않는다. 값은 잡은 턴의 토큰이다. 앞
        턴의 중지 표시는 리스와 같은 만료를 받아 이미 사라졌다. 세션 해시가 없거나 열린 표시가
        남았으면 잡지 않는다(P79 1절)."""
        claimed, _ = await self._claim(session_id, lease_seconds, token, over_orphan=False)
        return claimed

    async def claim_delete(
        self, session_id: str, *, lease_seconds: float, token: str
    ) -> tuple[bool, RunMarker | None]:
        """같은 스크립트가 끊긴 열린 표시 위에서도 잡고 그 토큰의 실행 표식을 지워 돌려준다."""
        return await self._claim(session_id, lease_seconds, token, over_orphan=True)

    async def _claim(
        self, session_id: str, lease_seconds: float, token: str, *, over_orphan: bool
    ) -> tuple[bool, RunMarker | None]:
        lease_key, _ = self._lease_keys(session_id)
        result = await self._claim_script(
            keys=[lease_key, self._session_key(session_id), self._open_key(), self._drain_key()],
            args=[token, int(lease_seconds * 1000), session_id, "1" if over_orphan else ""],
        )
        outcome = int(result[0])
        if outcome == 2:
            raise DrainMarked()
        if outcome != 1:
            return False, None
        return True, RunMarker.from_json(result[1]) if len(result) > 1 else None

    async def mark_drain(self, *, mark: str, seconds: float) -> int:
        """스크립트 하나(DRAIN_LUA)다. 표시의 만료는 1ms 보다 짧지 않다."""
        return int(
            await self._drain_script(
                keys=[self._drain_key(), self._open_key()],
                args=[max(1, round(seconds * 1000)), turn_lease_key(self._prefix, ""), mark],
            )
        )

    async def clear_drain_mark(self, mark: str) -> None:
        """스크립트 하나(CLEAR_DRAIN_LUA)가 값을 보고 지운다."""
        await self._clear_drain_script(keys=[self._drain_key()], args=[mark])

    async def drain_marked(self) -> bool:
        return bool(await self._client.exists(self._drain_key()))

    async def release_turn(self, session_id: str, token: str) -> None:
        """제 토큰의 리스와 표시, 열린 표시만 지운다(RELEASE_LUA). 만료 뒤 다른 턴이 잡은 리스는
        건드리지 않는다."""
        await self._release_script(
            keys=[*self._lease_keys(session_id), self._session_key(session_id), self._open_key()],
            args=[token, session_id],
        )

    async def request_cancel(self, session_id: str, turn_id: str | None = None) -> str | None:
        """살아 있는 리스의 토큰을 표시에 옮겨 적고 그 토큰을 돌려준다(CANCEL_LUA, D65). turn_id 를
        주면 그 시도일 때만 적는다(P78 2절). 리스가 없으면 None 이다."""
        return await self._cancel_script(keys=self._cancel_keys(session_id), args=[turn_id or ""])

    async def cancel_all(self) -> int:
        """이 인스턴스의 리스 키를 SCAN 으로 찾아 키마다 CANCEL_LUA 를 부른다(P78 2절). 표시를 적은
        수다. 찾은 뒤 풀린 리스는 0 이라 세지 않는다."""
        lease_prefix = self._key("turn_lease:")
        marked = 0
        async for key in self._client.scan_iter(match=f"{lease_prefix}*", count=SCAN_COUNT):
            sid = key[len(lease_prefix) :]
            marked += await self._cancel_script(keys=self._cancel_keys(sid), args=[""]) is not None
        return marked

    async def cancel_requested(self, session_id: str, token: str) -> bool:
        _, cancel_key = self._lease_keys(session_id)
        return await self._client.get(cancel_key) == token

    async def renew_turns(self, leases: Mapping[str, str], *, lease_seconds: float) -> set[str]:
        """리스마다 RENEW_LUA 를 파이프라인 하나에 실어 한 번에 보낸다. 1 이 돌아온 세션이 갱신된
        것이다."""
        if not leases:
            return set()
        ids = sorted(leases)
        pipe = self._client.pipeline(transaction=False)
        for sid in ids:
            await self._renew_script(
                keys=self._cancel_keys(sid),
                args=[leases[sid], int(lease_seconds * 1000)],
                client=pipe,
            )
        results = await pipe.execute()
        return {sid for sid, result in zip(ids, results, strict=True) if int(result) == 1}

    async def orphans(self) -> list[str]:
        """turns:open 의 줄마다 정리 스크립트와 리스 EXISTS 를 파이프라인 하나에 싣는다. 해시가
        사라졌거나 열린 표시가 없는 줄은 스크립트가 빼고, 리스가 살아 있는 줄은 도는 턴이다."""
        members = sorted(await self._client.smembers(self._open_key()))
        if not members:
            return []
        pipe = self._client.pipeline(transaction=False)
        for sid in members:
            await self._prune_script(
                keys=[self._session_key(sid), self._open_key()], args=[sid], client=pipe
            )
            pipe.exists(self._lease_keys(sid)[0])
        results = await pipe.execute()
        pairs = zip(members, results[::2], results[1::2], strict=True)
        return [sid for sid, pruned, leased in pairs if not int(pruned) and not leased]

    async def repair_turn(
        self,
        session_id: str,
        orphan: str,
        *,
        status: str | None,
        pending_since: datetime,
        plan: RepairPlan | None,
    ) -> tuple[SessionRecord, RunMarker | None] | None:
        """해시, 리스 EXISTS, 전사를 파이프라인 하나로 읽고 plan 으로 붙일 항목을 정한 뒤 스크립트
        하나(REPAIR_LUA)로 쓴다. 스크립트가 토큰과 리스, 읽은 실행 표식과 last_turn_at 을 다시 본다.
        전사는 리스의 토큰을 보는 붙이기만 받으므로 리스가 없는 동안 끊긴 턴의 붙이기가 끼어들지
        못한다. plan 이 None 이면(최소 되살리기) 전사를 읽지 않는다."""
        session_key = self._session_key(session_id)
        lease_key, cancel_key = self._lease_keys(session_id)
        transcript = transcript_key(self._prefix, session_id)
        pipe = self._client.pipeline(transaction=False)
        pipe.hgetall(session_key)
        pipe.exists(lease_key)
        if plan is not None:
            pipe.lrange(transcript, -TRANSCRIPT_LIMIT, -1)
        mapping, leased, *read = await pipe.execute()
        if not mapping or leased or mapping.get("turn_open") != orphan:
            return None
        locked = _from_hash(mapping)
        raw_run = mapping.get("turn_run") or ""
        marker = _marker_of(raw_run, orphan)
        added: list[dict[str, Any]] = []
        if plan is not None:
            added = plan(locked, marker, [json.loads(value) for value in read[0]])
        written = await self._repair_script(
            keys=[
                session_key,
                lease_key,
                *self._status_keys(),
                cancel_key,
                self._open_key(),
                transcript,
            ],
            args=[
                session_id,
                status or "",
                _hash_value(pending_since),
                str(_epoch(locked.last_turn_at)),
                orphan,
                raw_run,
                mapping.get("last_turn_at") or "",
                TRANSCRIPT_LIMIT,
                *[dumps(item) for item in added[-TRANSCRIPT_LIMIT:]],
            ],
        )
        return (_row_of(written), marker) if written else None

    async def align_status(
        self, session_id: str, *, status: str, pending_since: datetime | None, seen: datetime
    ) -> SessionRecord | None:
        """스크립트 하나(ALIGN_LUA)다. 조건을 스크립트가 보고 상태 집합을 함께 옮긴다."""
        written = await self._align_script(
            keys=[
                self._session_key(session_id),
                self._lease_keys(session_id)[0],
                *self._status_keys(),
            ],
            args=[
                session_id,
                status,
                _hash_value(pending_since) if pending_since is not None else "",
                str(_epoch(seen)),
                _hash_value(seen),
            ],
        )
        return _row_of(written) if written else None

    async def expire(self, before: datetime) -> list[str]:
        """status:active·pending 에서 점수가 before 앞인 후보를 읽고 sid 마다 EXPIRE_LUA 를 불러
        1 이 돌아온 sid 만 돌려준다. 후보를 읽은 뒤 다른 파드가 턴을 끝낸 세션과 리스 키가 있는
        세션은 0 이다(P41 9절)."""
        threshold = _epoch(before)
        closed_key = self._status_key(STATUS_CLOSED)
        expired: list[str] = []
        for status in (STATUS_ACTIVE, STATUS_PENDING):
            status_key = self._status_key(status)
            candidates = await self._client.zrangebyscore(status_key, "-inf", f"({threshold}")
            for sid in candidates:
                result = await self._expire_script(
                    keys=[status_key, closed_key, self._session_key(sid), self._lease_keys(sid)[0]],
                    args=[sid, threshold],
                )
                if int(result) == 1:
                    expired.append(sid)
        return expired

    async def closed_before(self, before: datetime) -> list[str]:
        """보존 기간이 지난 닫힌 세션이다(D46). status:closed 의 점수가 last_turn_at 이라
        구간 조회 하나다. 읽기만 하므로 두 파드가 같은 sid 를 볼 수 있는데 뒤따르는 전사·행
        삭제가 둘 다 멱등이라 탈이 없다."""
        return list(
            await self._client.zrangebyscore(
                self._status_key(STATUS_CLOSED), "-inf", f"({_epoch(before)}"
            )
        )

    async def delete(self, session_id: str) -> None:
        """색인 행을 통째로 지운다. 해시·임차·중지 표시·실행 키와 정렬 집합, turns:open 에서 함께
        빠져야 하므로 MULTI 하나다. 사용자 집합과 실행 키의 이름을 알려면 user_sub 와 run_id 가
        필요한데 두 값은 만들 때 정해져 바뀌지 않으므로 MULTI 앞에 읽는다. 해시가 이미 없으면 채팅과
        실행의 최근 순 집합과 상태 집합만 훑고 끝난다."""
        session_key = self._session_key(session_id)
        user_sub, run_id = await self._client.hmget(session_key, ["user_sub", "run_id"])
        pipe = self._client.pipeline(transaction=True)
        pipe.delete(session_key, *self._lease_keys(session_id))
        if run_id is not None:
            pipe.delete(self._run_key(run_id))
        for key in self._by_turn_keys():
            pipe.zrem(key, session_id)
        for name in STATUSES:
            pipe.zrem(self._status_key(name), session_id)
        pipe.srem(self._open_key(), session_id)
        if user_sub is not None:
            # 옛 런타임의 파드가 실행 세션을 채팅 집합에도 넣었을 수 있어 두 사용자 집합에서 뺀다
            for run in (False, True):
                pipe.zrem(self._user_key(user_sub, run), session_id)
        await pipe.execute()

    async def counts(self) -> dict[str, int]:
        pipe = self._client.pipeline(transaction=False)
        for status in STATUSES:
            pipe.zcard(self._status_key(status))
        values = await pipe.execute()
        return {status: int(value) for status, value in zip(STATUSES, values, strict=True)}

    async def totals(self) -> dict[str, int]:
        """채팅과 실행의 최근 순 집합에 든 모든 세션의 합이다."""
        ids = [sid for key in self._by_turn_keys() for sid in await self._client.zrange(key, 0, -1)]
        out = {"sessions": len(ids), "turns": 0, "input_tokens": 0, "output_tokens": 0}
        for start in range(0, len(ids), REDIS_TOTALS_CHUNK):
            pipe = self._client.pipeline(transaction=False)
            for sid in ids[start : start + REDIS_TOTALS_CHUNK]:
                pipe.hmget(self._session_key(sid), *RECORD_INT_FIELDS)
            for turns, usage_input, usage_output in await pipe.execute():
                out["turns"] += int(turns or 0)
                out["input_tokens"] += int(usage_input or 0)
                out["output_tokens"] += int(usage_output or 0)
        return out


async def delete_redis_thread(
    saver: AsyncRedisSaver, thread_id: str, *, rounds: int = REDIS_DELETE_ROUNDS
) -> None:
    """스레드의 체크포인트와 쓰기를 남김없이 지운다(P36 F0 8번).

    `adelete_thread` 는 RediSearch 로 체크포인트와 쓰기를 각각 1만 개까지만 찾아 지우므로 남은
    문서가 없을 때까지 되풀이한다. 남았는지는 두 인덱스에서 그 스레드의 문서 수로 본다. 인스턴스
    접두어로 SCAN 하면 접두어 밖의 `write_keys_zset:*` 키를 놓친다. 상한까지 남으면
    CheckpointsRemain 이다.
    """
    for _ in range(rounds):
        await saver.adelete_thread(thread_id)
        if await _thread_documents(saver, thread_id) == 0:
            return
    raise CheckpointsRemain(f"스레드 {thread_id} 를 {rounds} 번 지워도 체크포인트가 남았다")


async def _thread_documents(saver: AsyncRedisSaver, thread_id: str) -> int:
    """체크포인트와 쓰기 두 인덱스에 남은 그 스레드의 문서 수다. `adelete_thread` 와 같은 태그
    조건으로 개수만 센다."""
    query = CountQuery(Tag("thread_id") == to_storage_safe_id(thread_id))
    total = 0
    for index in (saver.checkpoints_index, saver.checkpoint_writes_index):
        total += int(await index.query(query))
    return total


def find_response_error(error: BaseException) -> ResponseError | None:
    """예외와 `__cause__`·`__context__` 사슬에서 redis 의 ResponseError 를 찾는다.

    redisvl 이 `RedisError` 를 자기 예외로 감싸므로 서버 응답은 사슬 안에 있다. 감싼 예외의 형이
    아니라 사슬의 서버 응답으로 가른다.
    """
    seen: set[int] = set()
    stack: list[BaseException] = [error]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ResponseError):
            return current
        for nxt in (current.__cause__, current.__context__):
            if nxt is not None:
                stack.append(nxt)
    return None


async def setup_redis(
    setup: Callable[[], Awaitable[Any]], *, what: str, retries: int = REDIS_SETUP_RETRIES
) -> None:
    """setup 을 부르고 예외를 셋으로 가른다. 체크포인터와 장기 기억 store 가 같이 쓴다.

    사슬의 ResponseError 메시지에 `unknown command` 가 있으면 모듈이 없는 Redis(7)라
    memory_redis_modules_missing, `Index already exists` 는 다른 파드가 먼저 만든 것이라 나오는
    동안 다시 부르되 상한을 넘으면 memory_setup_failed, 그 밖(NOPERM, 연결 끊김)은
    memory_setup_failed 다. detail 에는 예외 클래스 이름만 싣는다.
    """
    collisions = 0
    while True:
        try:
            await setup()
            return
        except Exception as e:
            response = find_response_error(e)
            message = str(response) if response is not None else ""
            if "unknown command" in message:
                raise ConfigError(
                    "memory_redis_modules_missing",
                    "memory 의 Redis 에 RediSearch·RedisJSON 이 없다. Redis 8 이 필요하다",
                ) from e
            if "Index already exists" in message:
                collisions += 1
                if collisions <= retries:
                    continue
                raise ConfigError(
                    "memory_setup_failed",
                    f"{what} 인덱스 생성이 {collisions} 번 겹쳤다: {type(e).__name__}",
                ) from e
            raise ConfigError(
                "memory_setup_failed", f"{what} setup 실패: {type(e).__name__}"
            ) from e


async def setup_redis_saver(saver: Any, *, retries: int = REDIS_SETUP_RETRIES) -> None:
    """체크포인터의 `asetup()` 이다. 재시도 규칙은 장기 기억 store 와 같다."""
    await setup_redis(saver.asetup, what="체크포인터", retries=retries)


def make_redis_client(conn: dict[str, Any], *, decode_responses: bool = True) -> Any:
    """resolve_redis 가 준 인자로 redis.asyncio 클라이언트를 만든다.

    체크포인터·색인·카운터가 하나를 나눠 쓰고 장기 기억 store 는 자기 것을 따로 연다. Postgres
    쪽이 search_path 때문에 풀을 나눌 수 없어서 두 종류를 같은 모양으로 둔다(여는 곳이 닫는다).
    decode_responses 가 거짓이면 값을 바이트 그대로 주고받는다(첨부 저장소).
    """
    from redis.asyncio import Redis

    args = {**REDIS_CLIENT_ARGS, "decode_responses": decode_responses}
    if "url" in conn:
        # URL 밖의 인자(TLS 의 CA 경로, P61)는 그대로 넘긴다
        extra = {k: v for k, v in conn.items() if k != "url"}
        return Redis.from_url(conn["url"], **extra, **args)
    return Redis(**conn, **args)


async def open_redis_store(conn: dict[str, Any], *, instance_id: str) -> SessionStore:
    """Redis 8 위에 체크포인터·색인·카운터를 연다. 클라이언트 하나를 셋이 나눠 쓴다.

    연결할 수 없으면 memory_unreachable, 비밀번호가 틀렸으면 memory_rejected 이고 detail 에 서버
    원문은 싣지 않는다(`dsn.check_redis`).
    """
    from runtime.redis_saver import ChainRedisSaver

    # 세션 클라이언트와 첨부 클라이언트는 첫 명령 전에 턴의 펜스를 건다. 장기 기억 store 의
    # 클라이언트에 걸지 않는 까닭은 fence.py 에 있다
    client = fence_redis_client(make_redis_client(conn))
    binary_client = fence_redis_client(make_redis_client(conn, decode_responses=False))
    try:
        await check_redis(client, "세션 저장소(Redis)", MEMORY_CODES)
        prefix = redis_prefix(instance_id)
        saver = ChainRedisSaver(
            redis_client=client,
            checkpoint_prefix=f"{prefix}checkpoint",
            checkpoint_write_prefix=f"{prefix}checkpoint_write",
        )
        await setup_redis_saver(saver)
    except BaseException:
        # 다시 열기 도중 앱이 닫혀 취소될 때도 닫는다(T22)
        await client.aclose()
        await binary_client.aclose()
        raise
    return SessionStore(
        checkpointer=saver,
        index=RedisSessionIndex(client, prefix, instance_id),
        counters=RedisCounters(client, prefix),
        shared=True,
        warnings=[],
        transcripts=RedisTranscripts(client, prefix),
        uploads=RedisUploads(binary_client, prefix),
        delete_thread=partial(delete_redis_thread, saver),
        _client=client,
        _binary_client=binary_client,
    )
