"""비서 세션 문서 (D31, P14 F1b). 컬렉션 `assistant_sessions`.

세션은 테넌트·사용자 소유이고 두 기록을 나눠 둔다. `context` 는 모델 문맥(`message_to_dict`
결과. 요약 + 최근 구간)이고 `transcript` 는 화면용 ChatItem 투영이다. 둘을 나누는 이유는 문서
크기다. 도구 결과 4KB 에 턴당 24회면 턴 하나가 100KB 에 이를 수 있어 `context` 는 루프의 자르기
규칙으로, `transcript` 는 항목 500개 상한으로 막는다. `files` 는 `fetch_url` 이 가져온 본문이고
합쳐서 1MB 까지다. 본문 옆에 출처 `url` 을 함께 두어 승인 카드가 무엇을 심는지 보여 줄 수 있게
한다(D31). 옛 문서에는 `url` 이 없으므로 없으면 None 이고 마이그레이션은 필요 없다. 승인 대기는
`pending` 하나뿐이다(LangGraph 체크포인터를 두지 않는다).

턴의 리스는 이 문서에 없다. `store/leases.py` 의 이름 단위 `Lease` 가 세션당·사용자당
하나를 센다(D30 "공통은 아래 계층에"). 색인은 (tenant, sub, updated_at) 과 `updated_at` TTL 7일이다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from daegam import llm_http
from daegam.store.base import (
    Document,
    UtcDatetime,
    ensure_ttl_index,
    iso_utc,
    new_id,
    utcnow,
)

SESSIONS = "assistant_sessions"
SESSION_TTL_DAYS = 7
TRANSCRIPT_LIMIT = 500
FILES_MAX_BYTES = 1024 * 1024
DEFAULT_TITLE = "새 대화"

SessionState = Literal["idle", "running", "pending"]
PendingKind = Literal["confirm", "question"]


class FetchedFile(BaseModel):
    """`fetch_url` 이 가져온 본문 하나. `url` 은 출처이고 승인 카드가 무엇을 심는지 보여 준다.
    본문에 견주면 URL 은 무시할 만한 크기라 문서가 커지지 않는다. 옛 문서에는 없어 None 이다."""

    model_config = ConfigDict(extra="forbid")

    text: str
    url: str | None = None


class PendingCall(BaseModel):
    """승인(또는 답)을 기다리는 tool_call 하나. id 는 모델이 준 tool_call id 이고 결정의 id 와
    같다."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    risk: str


class Pending(BaseModel):
    """`confirm` 은 승인 대상 여럿, `question` 은 `ask_user` 하나다. kind 는 항상 하나다."""

    model_config = ConfigDict(extra="forbid")

    kind: PendingKind
    calls: list[PendingCall]


class Usage(BaseModel):
    """모델 호출 사용량의 합. `cached_input_tokens` 는 `input_tokens` 가운데 provider 가
    캐시에서 읽은 몫이다(langchain `input_token_details.cache_read`). 캐시된 입력은 단가가
    낮아(OpenAI 4분의 1, Anthropic 10분의 1) 이것 없이는 실제 비용을 셈할 수 없다."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0

    def add(self, meta: Mapping[str, Any] | None) -> None:
        """langchain usage_metadata 를 더한다. total 이 없으면 입력·출력의 합이다. 입력·출력·캐시
        읽기는 원장과 같은 `llm_http.usage_from_metadata` 로 읽는다 (P37 7절)."""
        if not meta:
            return
        counts = llm_http.usage_from_metadata(meta)
        i, o = counts["input_tokens"], counts["output_tokens"]
        self.input_tokens += i
        self.output_tokens += o
        self.total_tokens += int(meta.get("total_tokens") or (i + o))
        self.cached_input_tokens += counts["cache_read_tokens"]


class AssistantSession(Document):
    id: str = Field(default_factory=new_id)
    tenant: str
    sub: str
    title: str = DEFAULT_TITLE
    state: SessionState = "idle"
    # 이 대화에 붙은 LLM provider 이름 (D45). 첫 턴이 고르면 다음 턴도 같은 것으로 간다.
    # 옛 문서에는 없어 None 이고 그때는 설정의 기본 provider 로 돈다
    provider: str | None = None
    context: list[dict[str, Any]] = Field(default_factory=list)
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    pending: Pending | None = None
    files: dict[str, FetchedFile] = Field(default_factory=dict)
    turns: int = 0
    usage: Usage = Field(default_factory=Usage)
    # 현재 턴의 집계. `turns` 요청이 0 으로 되돌리고 결정 재개는 이어 센다 (done 이벤트의 값)
    turn_tool_calls: int = 0
    turn_usage: Usage = Field(default_factory=Usage)
    ui_context: dict[str, Any] | None = None
    # 모델에 싣는 도구 묶음 가운데 이 세션에서 연 것 (D57). 기본 묶음은 늘 열려 있어 적지 않는다
    tool_groups: list[str] = Field(default_factory=list)
    created_at: UtcDatetime = Field(default_factory=utcnow)
    updated_at: UtcDatetime = Field(default_factory=utcnow)

    def to_doc(self) -> dict[str, Any]:
        """파일 이름에 점이 있으므로(`fetched-1.yaml`) files 는 목록으로 저장한다. url 이 없는
        파일은 키 자체를 넣지 않아 옛 문서와 같은 모양으로 남는다."""
        d = super().to_doc()
        d["files"] = [{"name": k, **v.model_dump(exclude_none=True)} for k, v in self.files.items()]
        return d

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> AssistantSession:
        d = dict(doc)
        files = d.get("files")
        if isinstance(files, list):
            d["files"] = {f["name"]: {"text": f["text"], "url": f.get("url")} for f in files}
        return super().from_doc(d)


def files_size(files: Mapping[str, FetchedFile]) -> int:
    return sum(len(f.text.encode("utf-8")) for f in files.values())


class MongoAssistantSessionStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[SESSIONS]

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("tenant", ASCENDING), ("sub", ASCENDING), ("updated_at", DESCENDING)],
            name="ix_tenant_sub_updated",
        )
        ttl = SESSION_TTL_DAYS * 24 * 3600
        await ensure_ttl_index(self.coll, "updated_at", ttl, "ttl_updated_at")

    async def create(self, session: AssistantSession) -> AssistantSession:
        await self.coll.insert_one(session.to_doc())
        return session

    async def get(self, session_id: str) -> AssistantSession | None:
        doc = await self.coll.find_one({"_id": session_id})
        return AssistantSession.from_doc(doc) if doc else None

    async def list(self, tenant: str, sub: str) -> list[dict[str, Any]]:
        """목록은 요약만. transcript·context 는 읽지 않는다."""
        cursor = self.coll.find(
            {"tenant": tenant, "sub": sub},
            {"title": 1, "state": 1, "provider": 1, "updated_at": 1},
        ).sort("updated_at", DESCENDING)
        return [
            {
                "id": d["_id"],
                "title": d.get("title") or DEFAULT_TITLE,
                "state": d.get("state") or "idle",
                "provider": d.get("provider"),
                "updated_at": iso_utc(d["updated_at"]),
            }
            async for d in cursor
        ]

    async def save(self, session: AssistantSession) -> None:
        """턴이 바꾼 것 전부를 문서에 쓴다. 문서가 사라졌으면(TTL·삭제) 다시 만들지 않는다."""
        session.updated_at = utcnow()
        doc = session.to_doc()
        doc.pop("_id")
        doc.pop("created_at", None)
        await self.coll.update_one({"_id": session.id}, {"$set": doc})

    async def delete(self, session_id: str) -> bool:
        r = await self.coll.delete_one({"_id": session_id})
        return r.deleted_count == 1


__all__ = [
    "DEFAULT_TITLE",
    "FILES_MAX_BYTES",
    "SESSIONS",
    "SESSION_TTL_DAYS",
    "TRANSCRIPT_LIMIT",
    "AssistantSession",
    "FetchedFile",
    "MongoAssistantSessionStore",
    "Pending",
    "PendingCall",
    "Usage",
    "files_size",
]
