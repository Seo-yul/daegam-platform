"""장기 기억 보기와 하나 지우기다(P36 8절, D34). 이미 연 저장소에서 호출자 자신의 기억만 다룬다.

  GET    /memories                   {enabled, retention_days, files: [{path, size, created_at,
                                     modified_at}]}
  GET    /memories/files/{key:path}  {path, encoding, content}
  DELETE /memories/files/{key:path}  {path, deleted: true}

네임스페이스는 언제나 호출자 자신의 것(`(tenant, instance_id, sha256(sub))`)이다. 외부 경로는 토큰의
sub, 프록시 경로는 쿼리 `user_sub` 이고 없으면 422 `user_sub_required` 다. 승인자 헤더도 남의 기억을
열지 못한다. 저장소 모양(Postgres `store` 행, RedisJSON 문서, deepagents `FileData` 값)을 아는 곳을
플랫폼에 하나 더 두지 않으려고 여기에 둔다. 전체 지우기는 런타임 없이도 도는 D34 의 플랫폼 라우트다.

`path` 는 에이전트가 보는 경로(`/memories/prefs.md`)이고 `key` 는 그 밑의 경로(`prefs.md`)다.
StoreBackend 의 저장소 키는 라우트 접두어를 뗀 `/<key>` 다(`deepagents/backends/composite.py`). 키는
글자 그대로 맞춰 찾으므로 디렉터리를 거슬러 오를 길이 없지만, `.`·`..` 조각과 NUL, 512자 넘는 키는
422 `invalid_path` 로 먼저 거절한다. 세션 파일(session_files.py)과 같은 검사(`checked_file_key`)다.

하나 지우기는 `StoreBackend` 의 지우기가 아니라 저장소 키 하나를 바로 지운다(`BaseStore.adelete`).
`StoreBackend` 의 지우기는 그 키를 접두어로 삼는 다른 파일까지 함께 지우기 때문이다. 지운 뒤 관측
이벤트 `memory_deleted` 를 남기되 경로는 싣지 않는다. 플랫폼 프록시 경로로 지우면 플랫폼 감사가 경로
URL 로 무엇을 지웠는지 남기지만, 외부 경로(Ingress `/memories`)로 지우면 이 이벤트의 주체만 남는다.
읽기는 만료를 늘리지 않고(`refresh_ttl=False`), 만료된 파일은 저장소의 `omit_expired` 가 목록과
읽기에서 뺀다.
"""

from __future__ import annotations

from typing import Any

from deepagents.backends.utils import file_data_to_string
from fastapi import APIRouter, HTTPException
from langgraph.store.base import Item

from runtime import events as ev
from runtime.auth import Caller, CallerDep, require_owner_sub
from runtime.long_term import MEMORY_ROUTE, LongTermMemory
from runtime.state import Runtime, RuntimeState
from runtime.turn_gate import checked_file_key, error

router = APIRouter()

# 목록이 한 번에 싣는 파일 수의 상한이다
MEMORY_FILES_MAX = 1000
# 내용을 읽어 줄 수 있는 파일 크기의 상한(UTF-8 바이트)이다
MEMORY_FILE_MAX_BYTES = 1024 * 1024


def _own_memory(state: RuntimeState, caller: Caller) -> tuple[LongTermMemory, tuple[str, ...]]:
    """호출자 자신의 저장소와 네임스페이스다. 프록시 경로에 주체가 없으면 422, 장기 기억이 꺼져
    있으면 404 다."""
    sub = require_owner_sub(caller)
    memory = state.long_term
    if memory is None:
        raise error(404, "long_term_off", "이 에이전트는 장기 기억을 쓰지 않는다")
    return memory, memory.owner_namespace(sub)


def _path(store_key: str) -> str:
    """저장소 키를 에이전트가 보는 경로로 옮긴다."""
    return MEMORY_ROUTE + store_key.lstrip("/")


def _content(item: Item) -> str | None:
    """파일 내용이다. deepagents 의 옛 줄 목록 모양은 잇고, 내용이 글자가 아니면 None 이다."""
    try:
        return file_data_to_string(item.value)
    except (KeyError, TypeError):
        return None


def _not_found() -> HTTPException:
    return error(404, "memory_not_found", "그 기억 파일이 없다")


async def _owned_item(
    memory: LongTermMemory, namespace: tuple[str, ...], store_key: str
) -> tuple[Item, str]:
    """호출자 네임스페이스의 파일 하나와 그 내용이다. 없거나 내용이 글자가 아니면 404 다."""
    item = await memory.store.aget(namespace, store_key, refresh_ttl=False)
    content = _content(item) if item is not None else None
    if item is None or content is None:
        raise _not_found()
    return item, content


def _file_row(item: Item, content: str) -> dict[str, Any]:
    return {
        "path": _path(item.key),
        "size": len(content.encode("utf-8")),
        "created_at": item.created_at.isoformat(),
        "modified_at": item.updated_at.isoformat(),
    }


@router.get("/memories")
async def list_memories(caller: CallerDep, state: Runtime) -> dict[str, Any]:
    memory, namespace = _own_memory(state, caller)
    items = await memory.store.asearch(namespace, limit=MEMORY_FILES_MAX, refresh_ttl=False)
    rows = [_file_row(item, content) for item in items if (content := _content(item)) is not None]
    rows.sort(key=lambda row: row["path"])
    # 장기 기억이 꺼져 있으면 _own_memory 가 이미 404 로 끝냈다
    retention_days = state.config.long_term["retention_days"]
    return {"enabled": True, "retention_days": retention_days, "files": rows}


@router.get("/memories/files/{key:path}")
async def read_memory(key: str, caller: CallerDep, state: Runtime) -> dict[str, Any]:
    memory, namespace = _own_memory(state, caller)
    item, content = await _owned_item(memory, namespace, checked_file_key(key))
    if len(content.encode("utf-8")) > MEMORY_FILE_MAX_BYTES:
        raise error(
            413,
            "file_too_large",
            f"기억 파일이 {MEMORY_FILE_MAX_BYTES} 바이트를 넘어 내용을 보내지 않는다",
        )
    encoding = item.value.get("encoding") or "utf-8"
    return {"path": _path(item.key), "encoding": encoding, "content": content}


@router.delete("/memories/files/{key:path}")
async def delete_memory(key: str, caller: CallerDep, state: Runtime) -> dict[str, Any]:
    memory, namespace = _own_memory(state, caller)
    store_key = checked_file_key(key)
    if await memory.store.aget(namespace, store_key, refresh_ttl=False) is None:
        raise _not_found()
    await memory.store.adelete(namespace, store_key)
    state.events.emit(ev.MEMORY_DELETED, user_sub=caller.sub)
    return {"path": _path(store_key), "deleted": True}
