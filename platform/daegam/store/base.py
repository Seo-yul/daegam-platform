"""모든 스토어 모델의 공통 베이스. 리소스 모델은 각자의 스토어 파일(store/<리소스>.py)에 있다.

Mongo 문서로는 `to_doc()`(id → _id), 복원은 `from_doc()`(_id → id)로 오간다.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets as pysecrets
import uuid
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, ClassVar, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    PrivateAttr,
    ValidationInfo,
    WithJsonSchema,
    model_validator,
)
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import BulkWriteError, DuplicateKeyError, OperationFailure


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def to_millis(dt: datetime) -> datetime:
    """시각을 Mongo 가 저장하는 밀리초까지 자른다. 쓴 값을 다음 조건부 갱신이 읽은 값과 그대로
    견준다."""
    return dt.replace(microsecond=dt.microsecond // 1000 * 1000)


def iso_utc(dt: datetime) -> str:
    """naive 는 UTC 로 보고 표시를 붙인다. 저장은 naive UTC 이고 밖으로 나갈 때만 명시한다.

    저장·비교는 계속 naive UTC 로 둔다. 그것을 aware 로 바꾸면 이미 들어 있는 값과 비교할 때
    TypeError 가 난다. 대신 응답 경계에서만 시간대를 붙인다. 시간대가 없는 ISO 문자열을 받으면
    브라우저가 로컬 시각으로 읽어 KST 기준으로 아홉 시간 어긋난 값을 보여준다."""
    return (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)).isoformat()


def naive_utc(dt: datetime) -> datetime:
    """시각을 저장 형식(naive UTC)으로 맞춘다. naive 는 이미 UTC 로 본다."""
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo is not None else dt


def from_epoch(seconds: float) -> datetime:
    """epoch 초(토큰의 `exp`·`iat`, 요청 핸들의 `exp`)를 저장 형식(naive UTC)으로 읽는다."""
    return naive_utc(datetime.fromtimestamp(seconds, UTC))


def parse_iso_utc(value: Any) -> datetime | None:
    """ISO 8601 문자열을 저장 형식(naive UTC)으로 읽는다. 문자열이 아니거나 비었거나 읽을 수 없으면
    None 이다. Python 3.12 의 fromisoformat 은 끝의 'Z' 를 받고 여섯 자리를 넘는 소수부를 자른다."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return naive_utc(datetime.fromisoformat(value.strip()))
    except (ValueError, OverflowError):
        return None


UtcDatetime = Annotated[
    datetime,
    PlainSerializer(iso_utc, return_type=str, when_used="json-unless-none"),
    # 직렬화가 str 로 바뀌면 pydantic 이 format 을 떼는데, 값은 여전히 date-time 이다. OpenAPI 가
    # 흔들리면 ui/src/api/schema.d.ts 가 함께 흔들리므로 원래 스키마를 그대로 적어 준다
    WithJsonSchema({"type": "string", "format": "date-time"}, mode="serialization"),
]
"""응답으로 나가는 시각 필드의 타입. `iso_utc` 를 JSON 직렬화 때만 거치므로 `to_doc()` 이 쓰는
python 모드 덤프와 Mongo 에 저장되는 값은 naive UTC 그대로다. pydantic 이 직접 직렬화하는 자리
(`model_dump(mode="json")`, FastAPI 의 `response_model`)가 이 규칙을 타려면 필드 타입을 이것으로
적어야 한다."""

# 도구 호출 타임아웃의 상한(초) (P50 7절). Traefik·vMCP·프록시 러너를 지나는 300초 호출이 끝까지
# 답했다(ToolHive 0.53.0 실측, P50 11절, docs/archive/reviews/2026-10-21-p50-053.md)
TIMEOUT_MAX_SECONDS = 300
TimeoutSeconds = Annotated[int, Field(ge=1, le=TIMEOUT_MAX_SECONDS)]
"""사용자가 정하는 도구 호출 타임아웃(초). MCP Server 의 backend 와 Gateway 의 백엔드가 쓴다."""


def hash_token(token: str) -> str:
    """제시된 토큰을 저장·비교할 때 쓰는 해시. 평문은 어디에도 남기지 않는다.

    Backend API 의 스펙 토큰과 Agent 의 인스턴스 토큰이 같은 규칙을 써야 해서 여기 하나만 둔다.
    예전에는 api/deps.py 와 store/agent_instance_tokens.py 에 한 벌씩 있었고, 서비스 계층이
    앞의 것을 쓰느라 api 를 import 하는 계층 역전이 있었다."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token() -> tuple[str, str]:
    """(평문, sha256). 평문은 호출자가 한 번 쓰고 버린다."""
    plain = pysecrets.token_urlsafe(32)
    return plain, hash_token(plain)


class HashedToken(Protocol):
    token_sha256: str


def match_token[T: HashedToken](tokens: Iterable[T], presented: str) -> T | None:
    """제시된 평문과 해시가 맞는 첫 토큰. 없으면 None 이다. 스펙 읽기 토큰과 Agent 인스턴스
    토큰의 인증이 함께 쓴다.

    비교는 상수 시간이고, 매치 여부와 무관하게 전부 돌아 타이밍이 토큰 순서를 드러내지 않게 한다."""
    presented_hash = hash_token(presented)
    matched: T | None = None
    for tok in tokens:
        if hmac.compare_digest(tok.token_sha256, presented_hash) and matched is None:
            matched = tok
    return matched


# `set_fields_where` 가 스스로 정하거나 다른 조건의 몫인 칸. `also` 로 넘기면 거절한다. `seq` 는
# 읽은 판의 조건(P68)이 따로 다루고, `$or` 는 살아 있는 클레임 같은 갈래 조건이 따로 더한다
RESERVED_CONDITIONS: frozenset[str] = frozenset({"_id", "state", "$or", "seq"})


async def set_fields_where(
    coll: Any,
    doc_id: str,
    fields: dict[str, Any],
    *,
    expect_state: Collection[str] | None = None,
    also: Mapping[str, Any] | None = None,
    push: Mapping[str, Any] | None = None,
) -> bool:
    """fields 만 `$set` 한다. `expect_state` 가 있으면 저장된 `state` 가 그 안에 있을 때만 쓴다.
    `also` 는 함께 맞아야 하는 칸과 값이다(인스턴스 스토어의 읽은 리비전·배포·overrides).
    조건에 안 맞거나 문서가 없으면 False 다. 문서 전체를 쓰지 않는 조건부 저장의 공통 꼴이다.

    `also` 가 이 함수가 정하는 칸(`RESERVED_CONDITIONS`)을 담으면 조용히 덮어쓰지 않고 ValueError
    다. 그 칸은 이 함수의 인자나 호출자가 따로 더하는 조건의 몫이다. 질의는
    `set_fields_guarded` 한 곳에서 만든다. push 는 같은 갱신의 `$push` 다."""
    return await set_fields_guarded(
        coll, doc_id, fields, expect_state=expect_state, also=also, push=push
    )


async def revoke_once(coll: Any, query: dict[str, Any], also: dict[str, Any] | None = None) -> bool:
    """query 에 맞는 토큰 하나를 회수한다. 토큰이 있으면 이미 회수됐어도 True 이고, 처음 회수한
    시각은 덮지 않는다. also 는 처음 회수할 때 함께 적을 필드다(OAuth 연결의 사유와 지울 해시)."""
    doc = await coll.find_one(query)
    if doc is None:
        return False
    if doc.get("revoked_at") is None:
        await coll.update_one(
            {"_id": doc["_id"], "revoked_at": None},
            {"$set": {"revoked_at": utcnow(), **(also or {})}},
        )
    return True


SEQ_FIELD = "seq"
"""사람의 저장이 하나씩 올리는 정수 (P68 2절). 응답 모델에 실리지 않도록 모델 필드가 아니라 스토어가
`SeqModel._seq` 로 들고 다닌다. 없는 문서는 0 이다."""

CLAIM_FIELD = "claim"
"""비밀을 먼저 쓰는 저장이 잡는 편집 클레임 `{id, until}` (P68 2절). P66 의 문서 클레임(리스)과는
다르다. 살아 있는 동안 다른 사람의 저장, 다른 편집 클레임, 검증 결과는 맞지 않는다. 마지막 저장이
지운다."""


class SeqModel(BaseModel):
    """읽은 `seq` 를 들고 다니는 모델의 섞개 (P68). `Document` 와 두 설정 모델이 상속한다."""

    # 저장된 `seq` (P68). 모델 필드가 아니라서 응답 모델과 OpenAPI 에 나가지 않는다
    _seq: int = PrivateAttr(default=0)


def stored_body(doc: Mapping[str, Any]) -> dict[str, Any]:
    """`_id` 와 P68 의 저장 쪽 칸(`seq`·`claim`)을 뺀 문서. 모르는 키를 거절하는 설정 모델
    (`extra="forbid"`)이 다음 판이 쓴 `seq` 를 읽고도 뜨게 한다 (P68 B1)."""
    return {k: v for k, v in doc.items() if k not in ("_id", SEQ_FIELD, CLAIM_FIELD)}


# 편집 클레임의 기한. 클레임을 잡은 요청이 하는 Kubernetes 호출 둘(읽기와 쓰기)이 P65 시한과
# 재시도 한 번을 다 써도(쓰기 하나가 최대 (10+45)×2 초) 끝나는 길이다. 멈춘 요청만 이보다 오래 산다
EDIT_CLAIM_TTL = timedelta(minutes=5)


def seq_of(doc: Mapping[str, Any]) -> int:
    """저장된 문서의 `seq`. 필드가 없는 옛 문서는 0 이다."""
    return int(doc.get(SEQ_FIELD) or 0)


def from_stored[M: SeqModel](
    model: type[M], doc: Mapping[str, Any], body: dict[str, Any] | None = None
) -> M:
    """저장된 문서를 model 로 읽고 그 `seq` 를 `_seq` 에 둔다. body 는 검증할 본문이고, 없으면
    `stored_body(doc)` 다 (읽기 전에 본문을 고치는 스토어가 넘긴다)."""
    obj = model.model_validate(stored_body(doc) if body is None else body)
    obj._seq = seq_of(doc)
    return obj


def seq_query(seq: int) -> dict[str, Any]:
    """저장된 `seq` 가 seq 인 문서. 0 은 필드가 없는 옛 문서도 맞는다 (P68 2절, 이전 작업 없음)."""
    return {SEQ_FIELD: {"$in": [0, None]}} if seq == 0 else {SEQ_FIELD: seq}


def unclaimed_query(now: datetime) -> dict[str, Any]:
    """살아 있는 편집 클레임이 없는 문서. 기한이 지난 클레임은 없는 것으로 본다."""
    return {"$or": [{CLAIM_FIELD: None}, {f"{CLAIM_FIELD}.until": {"$lte": now}}]}


def guarded_filter(
    doc_id: str,
    *,
    expect_state: Collection[str] | None = None,
    expect_seq: int | None = None,
    unclaimed: bool = False,
    also: Mapping[str, Any] | None = None,
    expect_claim: str | None = None,
) -> dict[str, Any]:
    """P68 조건부 저장의 질의. 인자의 뜻은 `set_fields_guarded` 와 같다. 갱신을 따로 짓는 조건부
    쓰기(`store/instances.py` `mark_stale`)도 이것으로 질의를 만든다."""
    clash = RESERVED_CONDITIONS.intersection(also or {})
    if clash:
        raise ValueError(f"also may not set {sorted(clash)}")
    conditions: list[dict[str, Any]] = [{"_id": doc_id, **(also or {})}]
    if expect_state is not None:
        conditions.append({"state": {"$in": list(expect_state)}})
    if expect_seq is not None:
        conditions.append(seq_query(expect_seq))
    if unclaimed:
        conditions.append(unclaimed_query(utcnow()))
    if expect_claim is not None:
        conditions.append({f"{CLAIM_FIELD}.id": expect_claim})
    return {"$and": conditions}


def _guarded_update(
    fields: dict[str, Any],
    *,
    raise_seq: bool = False,
    unset: Collection[str] = (),
    push: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """P68 조건부 저장의 갱신. 쓸 것이 없으면 ValueError 다."""
    update: dict[str, Any] = {"$set": fields} if fields else {}
    if raise_seq:
        update["$inc"] = {SEQ_FIELD: 1}
    if unset:
        update["$unset"] = dict.fromkeys(unset, "")
    if push:
        update["$push"] = dict(push)
    if not update:
        raise ValueError("nothing to write: no fields, no raise_seq, no unset, no push")
    return update


async def set_fields_guarded(
    coll: Any,
    doc_id: str,
    fields: dict[str, Any],
    *,
    expect_state: Collection[str] | None = None,
    expect_seq: int | None = None,
    unclaimed: bool = False,
    also: Mapping[str, Any] | None = None,
    expect_claim: str | None = None,
    raise_seq: bool = False,
    unset: Collection[str] = (),
    push: Mapping[str, Any] | None = None,
) -> bool:
    """`set_fields_where` 에 P68 조건을 더한 조건부 저장 (P68 2절). `expect_seq` 는 저장된 `seq`,
    `unclaimed` 는 살아 있는 클레임이 없음, `also` 는 함께 맞아야 하는 칸과 값(null 이어야 하는
    칸은 None)이고 `set_fields_where` 처럼 `RESERVED_CONDITIONS` 를 담으면 ValueError 다.
    `expect_claim` 은 편집 클레임 id 다. `raise_seq` 는 `seq` 를 하나 올리고 `unset` 은 필드를
    지우며 `push` 는 같은 갱신의 `$push` 다(Agent 의 배포 기록, P45 6절). 쓸 것이 없으면(빈
    fields, `raise_seq`·`unset`·`push` 없음) ValueError 다. 조건에 안 맞거나 문서가 없으면
    False 다."""
    query = guarded_filter(
        doc_id,
        expect_state=expect_state,
        expect_seq=expect_seq,
        unclaimed=unclaimed,
        also=also,
        expect_claim=expect_claim,
    )
    update = _guarded_update(fields, raise_seq=raise_seq, unset=unset, push=push)
    res = await coll.update_one(query, update)
    return res.matched_count == 1


async def save_by_seq(coll: Any, doc: Document, fields: dict[str, Any], **conditions: Any) -> bool:
    """사람의 저장 (P68 2절). 읽은 `seq` 이고 살아 있는 클레임이 없을 때만 fields 를 쓰고 `seq` 를
    올린다. 맞으면 doc 의 `_seq` 도 올려 같은 요청이 다음 조건부 저장을 할 수 있게 한다."""
    saved = await set_fields_guarded(
        coll,
        doc.id,  # type: ignore[attr-defined]
        fields,
        expect_seq=doc._seq,
        unclaimed=True,
        raise_seq=True,
        **conditions,
    )
    if saved:
        doc._seq += 1
    return saved


async def upsert_by_seq(
    coll: Any,
    doc_id: str,
    current: SeqModel,
    fields: dict[str, Any],
    updated_by: str | None,
    *,
    merged: Collection[str] = (),
) -> dict[str, Any] | None:
    """싱글턴 설정 문서의 사람의 저장 (P68 2절). current 위에 fields 와 `updated_at`·`updated_by`
    를 얹어 모델 전체를 검증하고, 그 칸만 current 를 읽은 `seq` 일 때 쓰며 `seq` 를 올린다. 문서가
    없으면 나머지 칸도 함께 만든다. 맞지 않는 문서가 있으면 upsert 의 넣기가 같은 `_id` 로
    `DuplicateKeyError` 를 내고, 그것이 충돌이라 None 이다. 맞으면 저장된 문서다. 검증이 실패하면
    pydantic `ValidationError` 다. merged 의 절은 통째로 바꾸지 않고 안쪽 칸마다 점 경로로 쓴다. 그
    절에서 이 판이 모르는 칸이 문서에 남는다 (P47 6.1절)."""
    model = type(current)
    sections = {**fields, "updated_at": utcnow(), "updated_by": updated_by}
    body = model.model_validate({**current.model_dump(), **sections}).model_dump()
    written: dict[str, Any] = {}
    for k in sections:
        if k in merged:
            written.update(_dotted(k, body[k]))
        else:
            written[k] = body[k]
    update = _guarded_update(written, raise_seq=True)
    rest = {k: v for k, v in body.items() if k not in sections}
    if rest:
        update["$setOnInsert"] = rest
    try:
        return await coll.find_one_and_update(
            guarded_filter(doc_id, expect_seq=current._seq),
            update,
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        return None


def _dotted(prefix: str, value: Any) -> dict[str, Any]:
    """dict 를 끝 칸마다의 점 경로로 편다. 빈 dict 는 그 자리 하나다."""
    if not isinstance(value, dict) or not value:
        return {prefix: value}
    out: dict[str, Any] = {}
    for k, v in value.items():
        out.update(_dotted(f"{prefix}.{k}", v))
    return out


async def take_edit_claim(
    coll: Any,
    doc: Document,
    ttl: timedelta,
    fields: dict[str, Any] | None = None,
    **conditions: Any,
) -> str | None:
    """비밀을 먼저 쓰는 저장의 편집 클레임 (P68 2절 첫째 쓰기). 읽은 `seq` 이고 살아 있는 클레임이
    없을 때만 클레임을 두고 `seq` 를 올린다. fields 는 함께 쓸 값이다(Database 의 `verified_at`
    지우기). conditions 는 `save_by_seq` 의 조건을 더한다(허브 항목의 `expect_state`). 잡으면 클레임
    id, 못 잡으면 None 이다."""
    claim_id = new_id()
    saved = await save_by_seq(
        coll,
        doc,
        {**(fields or {}), CLAIM_FIELD: {"id": claim_id, "until": utcnow() + ttl}},
        **conditions,
    )
    return claim_id if saved else None


async def release_edit_claim(
    coll: Any, doc_id: str, claim_id: str, restore: dict[str, Any] | None = None
) -> None:
    """편집 클레임만 지우고 클레임이 바꾼 칸(restore)을 되돌린다. 비밀을 쓰기 전에 멈춘 요청만
    부른다. 그 요청은 아무것도 바꾸지 않았으므로 다른 저장을 기한까지 막지 않는다. `seq` 는 클레임이
    올린 그대로 두므로 그 전에 읽은 저장은 여전히 맞지 않는다."""
    update: dict[str, Any] = {"$unset": {CLAIM_FIELD: ""}}
    if restore:
        update["$set"] = restore
    await coll.update_one({"_id": doc_id, f"{CLAIM_FIELD}.id": claim_id}, update)


async def finish_edit_claim(
    coll: Any, doc: Document, claim_id: str, fields: dict[str, Any]
) -> bool:
    """편집 클레임의 마지막 저장 (P68 2절 셋째 쓰기). 클레임 id 가 자기 것이고 `seq` 가 클레임이
    둔 값일 때만 fields 를 쓰고 `seq` 를 다시 올리며 클레임을 지운다. 기한이 지난 뒤 다른 저장이
    지나갔으면 `seq` 가 달라 맞지 않는다."""
    saved = await set_fields_guarded(
        coll,
        doc.id,  # type: ignore[attr-defined]
        fields,
        expect_seq=doc._seq,
        expect_claim=claim_id,
        raise_seq=True,
        unset=(CLAIM_FIELD,),
    )
    if saved:
        doc._seq += 1
    return saved


class EditClaimStore:
    """사람이 고치는 문서의 저장을 갖춘 스토어 섞개 (P68 2절). `coll` 을 가진 스토어가 상속한다.
    Database 와 클러스터가 쓴다."""

    coll: Any
    # 클레임을 잡을 때 함께 맞아야 하는 조건 (`set_fields_guarded` 의 인자). 허브 항목은 삭제가
    # 시작된 문서에 클레임을 잡지 않는다 (P55 5절)
    claim_conditions: ClassVar[dict[str, Any]] = {}

    async def save_edit(self, doc: Document, names: Collection[str]) -> bool:
        """사람의 저장. 서버가 읽은 `seq` 이고 살아 있는 편집 클레임이 없을 때만 names 칸만 쓰고
        `seq` 를 올린다. 맞지 않으면 False 다."""
        return await save_by_seq(self.coll, doc, fields_of(doc, names))

    async def claim_edit(self, doc: Document, fields: dict[str, Any] | None = None) -> str | None:
        return await take_edit_claim(
            self.coll, doc, EDIT_CLAIM_TTL, fields, **self.claim_conditions
        )

    async def finish_edit(self, doc: Document, claim_id: str, names: Collection[str]) -> bool:
        return await finish_edit_claim(self.coll, doc, claim_id, fields_of(doc, names))

    async def release_edit(
        self, doc_id: str, claim_id: str, restore: dict[str, Any] | None = None
    ) -> None:
        await release_edit_claim(self.coll, doc_id, claim_id, restore)


class NothingWritten(Exception):
    """`save_with_edit_claim` 의 write 가 아무것도 쓰기 전에 멈췄다. 원래 예외는 `__cause__` 다.
    그러면 바뀐 비밀이 없으므로 클레임을 기한까지 두지 않고 푼다."""


async def save_with_edit_claim[P](
    store: EditClaimStore,
    doc: Document,
    names: Collection[str],
    *,
    prepare: Callable[[], Awaitable[P]],
    write: Callable[[P], Awaitable[None]],
    conflict: Callable[[], Exception],
    claim_fields: dict[str, Any] | None = None,
    restore: dict[str, Any] | None = None,
) -> None:
    """비밀을 먼저 쓰는 저장의 세 걸음 (P68 2절): 편집 클레임, 비밀 쓰기, 마지막 저장.

    `prepare` 는 비밀을 쓰기 전의 일(비밀 저장소·네임스페이스 찾기, 지금 비밀 읽기)이고 `write`
    는 비밀 PUT 과 그에 따른 doc 의 변경이다. `claim_fields` 는 클레임이 함께 쓰는 값이고,
    prepare 가 실패하면 클레임을 풀며 `restore`(클레임 전의 그 값)로 되돌린다. write 가 시작된
    뒤의 실패(시한 초과, 취소 포함)는 비밀이 바뀌었는지 알 수 없으므로 클레임을 두어 기한이
    지나게 한다. 다만 write 가 `NothingWritten` 으로 아무것도 쓰지 않았다고 알리면 prepare 의
    실패처럼 풀고 원래 예외를 던진다. 클레임을 못 잡거나 마지막 저장이 맞지 않으면 conflict() 를
    던진다."""
    claim_id = await store.claim_edit(doc, claim_fields)
    if claim_id is None:
        raise conflict()
    try:
        prepared = await prepare()
    except BaseException:
        await store.release_edit(doc.id, claim_id, restore)  # type: ignore[attr-defined]
        raise
    try:
        await write(prepared)
    except NothingWritten as e:
        await store.release_edit(doc.id, claim_id, restore)  # type: ignore[attr-defined]
        raise (e.__cause__ or e) from None
    if not await store.finish_edit(doc, claim_id, names):
        raise conflict()


async def read_after_miss[D](
    read: Callable[[], Awaitable[D | None]], is_deleting: Callable[[D], bool]
) -> tuple[D | None, bool]:
    """조건부 저장이 맞지 않은 뒤 다시 읽는다. (문서 또는 None, 삭제 중인가) 다. 사람의 저장이
    빗나간 까닭(없음, 삭제 중, 다른 저장)을 가르는 공통 꼴이다 (P68 2절)."""
    fresh = await read()
    return fresh, fresh is not None and is_deleting(fresh)


def new_id() -> str:
    return str(uuid.uuid4())


def is_id(value: str) -> bool:
    """`new_id` 의 꼴(소문자 UUID)인가."""
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def ts_range(since: datetime | None, until: datetime | None) -> dict[str, Any] | None:
    """ts 범위 조건. since 는 포함, until 은 제외다. 둘 다 없으면 None 이다."""
    ts: dict[str, Any] = {}
    if since is not None:
        ts["$gte"] = since
    if until is not None:
        ts["$lt"] = until
    return ts or None


async def insert_new(coll: Any, docs: list[dict[str, Any]]) -> int:
    """`_id` 가 이미 있는 문서는 무시하고 넣는다. 새로 들어간 수를 돌려준다. 수집기가 겹쳐 읽은
    이벤트, 다시 보낸 묶음, 같은 호출의 원장 행이 여기서 걸러진다."""
    return len(await insert_new_docs(coll, docs))


async def facets(
    coll: Any, match: dict[str, Any], facets: dict[str, list[dict[str, Any]]]
) -> dict[str, list[dict[str, Any]]]:
    """match 에 맞는 문서를 facet 마다 모은다. 한 번 읽기로 여러 수를 센다. 맞는 문서가 없으면
    facet 마다 빈 목록이다."""
    pipeline = [{"$match": match}, {"$facet": facets}]
    docs = [d async for d in await coll.aggregate(pipeline)]
    return docs[0] if docs else {name: [] for name in facets}


async def insert_new_docs(coll: Any, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`insert_new` 와 같되 실제로 들어간 문서를 돌려준다. 묶음에서 중복으로 거절된 자리
    (`writeErrors` 의 index)를 뺀 것이다. 감사 이벤트가 새로 들어간 것의 시간만 다시 셀 시간으로
    적는 데 쓴다 (P51 3절)."""
    if not docs:
        return []
    try:
        await coll.insert_many(docs, ordered=False)
        return list(docs)
    except BulkWriteError as e:
        errors = (e.details or {}).get("writeErrors") or []
        if any(err.get("code") != 11000 for err in errors):
            raise
        refused = {err.get("index") for err in errors}
        return [d for i, d in enumerate(docs) if i not in refused]
    except DuplicateKeyError:
        return []


def window_key(subject: str, now: datetime, window: timedelta) -> tuple[str, datetime]:
    """subject 의 지금 칸 id 와 칸이 끝나는 시각. 칸은 1970-01-01 부터 window 길이로 자른다(분 칸,
    날 칸). id 는 `<subject>:<칸 시작의 분>` 이다."""
    seconds = int(window.total_seconds())
    # 저장 형식은 naive UTC 다. naive 의 timestamp() 는 지역 시간으로 읽으므로 UTC 를 붙여 센다
    moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    start = from_epoch(int(moment.timestamp()) // seconds * seconds)
    return f"{subject}:{start:%Y%m%d%H%M}", start + window


async def count_in_window(
    coll: Any, subject: str, now: datetime, window: timedelta, keep: timedelta
) -> tuple[int, datetime]:
    """subject 의 지금 칸에 하나를 더하고 더한 뒤의 수와 칸이 끝나는 시각을 돌려준다. 칸 문서는
    `{_id, count, expires_at}` 이고 TTL 이 칸이 끝난 뒤 keep 만큼 지나 지운다. 칸 id 가 같은 두
    upsert 는 서버가 다시 시도하므로 경합에도 수가 빠지지 않아 복제본 사이에 값 하나가 지켜진다(채팅
    업로드와 웹 검색이 함께 쓴다)."""
    key, ends = window_key(subject, now, window)
    doc = await coll.find_one_and_update(
        {"_id": key},
        {"$inc": {"count": 1}, "$setOnInsert": {"expires_at": ends + keep}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(doc["count"]), ends


async def take_in_window(
    coll: Any, subject: str, now: datetime, window: timedelta, keep: timedelta, cap: int
) -> str | None:
    """subject 의 지금 칸에 cap 아래일 때만 하나를 더한다. 자리를 잡으면 칸 id 를, 이미 cap 이면
    None 을 돌려준다. 칸 문서는 `count_in_window` 와 같다. 칸이 없을 때 두 upsert 가 겹쳐 하나가 키
    충돌로 지면 한 번 더 한다(그때는 칸이 있다)."""
    if cap <= 0:
        return None
    key, ends = window_key(subject, now, window)
    for attempt in range(2):
        try:
            await coll.update_one(
                {"_id": key, "count": {"$lt": cap}},
                {"$inc": {"count": 1}, "$setOnInsert": {"expires_at": ends + keep}},
                upsert=True,
            )
            return key
        except DuplicateKeyError:
            if attempt == 1 or await coll.find_one({"_id": key, "count": {"$lt": cap}}) is None:
                return None
    return None


async def ensure_ttl_index(coll: Any, field: str, ttl_seconds: int, name: str) -> None:
    """TTL 값이 바뀌면 collMod 로 맞춘다 (같은 이름·다른 옵션의 create_index 는 실패한다)."""
    try:
        await coll.create_index([(field, ASCENDING)], name=name, expireAfterSeconds=ttl_seconds)
    except OperationFailure:
        await coll.database.command(
            {"collMod": coll.name, "index": {"name": name, "expireAfterSeconds": ttl_seconds}}
        )


# Mongo 의 IndexNotFound 코드
_INDEX_NOT_FOUND = 27


async def drop_index_if_present(coll: Any, name: str) -> None:
    """이름의 인덱스를 지운다. 없으면 아무것도 하지 않는다. 열쇠를 바꾼 인덱스를 새 이름으로 옮길 때
    쓴다(같은 이름·다른 열쇠의 create_index 는 실패한다)."""
    try:
        await coll.drop_index(name)
    except OperationFailure as e:
        if e.code != _INDEX_NOT_FOUND:
            raise


# `from_doc` 이 검증 문맥으로 넘기는 표시. 저장된 문서를 읽는 검증이라는 뜻이다
_FROM_DOC = "from_doc"


class ItemLease(BaseModel):
    """문서 하나를 처리하는 워커의 리스 `{owner, until}`(`services/lease_worker.py`
    `LeaseWorker`)."""

    model_config = ConfigDict(extra="ignore")

    owner: str
    until: UtcDatetime


def free_lease(now: datetime) -> dict[str, Any]:
    """리스가 없거나 끝난 문서를 고르는 조건."""
    return {"$or": [{"lease": None}, {"lease.until": {"$lt": now}}]}


def take_lease(owner: str, lease_seconds: float, now: datetime) -> dict[str, Any]:
    """문서 하나에 리스를 거는 `$set` 칸."""
    return {"lease": {"owner": owner, "until": now + timedelta(seconds=lease_seconds)}}


async def extend_lease(coll: Any, item_id: str, owner: str, until: datetime) -> bool:
    """owner 가 쥔 리스를 until 까지 늘린다(갱신과 미루기). 남의 것이면 거짓이다."""
    result = await coll.update_one(
        {"_id": item_id, "lease.owner": owner}, {"$set": {"lease.until": until}}
    )
    return result.matched_count == 1


class Document(SeqModel):
    """`id`를 Mongo `_id`로 매핑하는 공통 베이스."""

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _default_display_name(cls, data: Any, info: ValidationInfo) -> Any:
        """`display_name` 필드를 가진 모델을 만들 때 값이 없거나 비면 `name` 으로 채운다 (D24).
        저장된 문서를 읽을 때(`from_doc`)는 채우지 않는다. 저장되는 문서는 만들 때 이미 채워졌으므로
        없으면 이 버전이 받지 않는 옛 문서다."""
        if (
            info.context != _FROM_DOC
            and "display_name" in cls.model_fields
            and isinstance(data, dict)
            and not data.get("display_name")
            and isinstance(data.get("name"), str)
        ):
            data = {**data, "display_name": data["name"]}
        return data

    def to_doc(self) -> dict[str, Any]:
        d = self.model_dump()
        if "id" in d:
            d["_id"] = d.pop("id")
        return d

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> Self:
        d = dict(doc)
        if "_id" in d:
            d["id"] = d.pop("_id")
        obj = cls.model_validate(d, context=_FROM_DOC)
        obj._seq = seq_of(doc)
        return obj


def fields_of(doc: Document, names: Collection[str]) -> dict[str, Any]:
    """문서에서 names 만 저장 형식(`to_doc`)으로 뽑는다. `set_fields` 의 fields 를 만드는 자리다.

    Agent·Gateway·MCP Server 가 같은 것을 저장 단위(SHARED·DEFINITION·DEPLOYMENT)로 나눠 쓴다 (D42).
    셋이 각자 적고 있었는데 몸통이 글자 하나까지 같았다. 이름이 점 경로(`sandbox.image`)면 그 안쪽
    값을 그 경로로 뽑는다. 최상위 칸의 나머지(`sandbox.check`)를 덮지 않고 그 칸만 쓰는 저장이다
    (P40 10절)."""
    body = doc.to_doc()
    out: dict[str, Any] = {}
    for name in names:
        value: Any = body
        for part in name.split("."):
            value = value[part]
        out[name] = value
    return out


async def find_for_sub[D: Document](coll: Any, model: type[D], sub: str) -> list[D]:
    """sub 의 문서, 새것부터. 개인 토큰과 OAuth 연결의 목록이 쓴다(인덱스 `ix_sub_created`)."""
    cursor = coll.find({"sub": sub}).sort("created_at", DESCENDING)
    return [model.from_doc(d) async for d in cursor]


async def ids_for_sub(
    coll: Any, query: dict[str, Any], sub: str, *, tenant: str | None = None
) -> list[str]:
    """query 에 맞는 sub 의 문서 id. tenant 를 주면 그 테넌트의 것만이다. 회수 훅이 개인 토큰과
    OAuth 연결의 살아 있는 것을 고를 때 쓴다."""
    query = {**query, "sub": sub}
    if tenant is not None:
        query["tenant"] = tenant
    return [d["_id"] async for d in coll.find(query, {"_id": 1})]


def error_kind(e: BaseException) -> str:
    """로그에 남기는 예외의 꼴. 종류의 이름과 Mongo 오류 코드만이다. 메시지는 싣지 않는다. Mongo
    메시지는 문서의 값(본문, 토큰 조각)을 품을 수 있다."""
    codes: list[Any] = []
    if (code := getattr(e, "code", None)) is not None:
        codes.append(code)
    details = getattr(e, "details", None)
    if isinstance(details, dict):
        errors = details.get("writeErrors") or []
        codes += [err.get("code") for err in errors if isinstance(err, dict)]
    unique = sorted({str(c) for c in codes if c is not None})
    return f"{type(e).__name__}({','.join(unique)})" if unique else type(e).__name__


async def ensure_partial_index(coll: Any, field: str, *more: tuple[str, int], name: str) -> None:
    """field 가 있는 문서만의 부분 인덱스. `{field: 값}` 같음 조건이 `$exists: true` 를 품어 쿼리
    계획이 이 인덱스를 쓴다."""
    await coll.create_index(
        [(field, ASCENDING), *more], name=name, partialFilterExpression={field: {"$exists": True}}
    )
