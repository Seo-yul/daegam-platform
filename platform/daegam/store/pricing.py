"""가격표 (P37 6절, D62). `settings` 컬렉션의 `_id: "pricing"` 문서다.

Platform Settings 문서와 따로 둔다. 설정 PATCH 가 커지지 않게 하려는 것이고, 설치 id 문서
(`INSTALL_DOC_ID`)와 같은 꼴이다. 표 전체를 한 번에 바꾸는 사람의 저장(P68)이라 `from_stored` 로
읽고 `upsert_by_seq` 로 쓰며, 저장된 `seq` 가 화면이 읽은 판(`version`)이 아니면 맞지 않는다. 기본
가격은 `daegam/pricing_defaults.json` 이고 문서에 들지 않는다.
"""

from __future__ import annotations

import json
from datetime import date
from functools import cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, model_validator
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import SeqModel, UtcDatetime, from_stored, new_id, upsert_by_seq
from daegam.store.settings import SETTINGS

PRICING_DOC_ID = "pricing"
# 한 표의 규칙 수 상한
MAX_PRICE_RULES = 500
DEFAULTS_FILE = Path(__file__).resolve().parents[1] / "pricing_defaults.json"
# 기본 가격의 id 앞머리. 관리자 규칙은 이 앞머리를 쓰지 못한다
DEFAULT_ID_PREFIX = "default:"

# 가격을 매기는 호출 계열. LLM 은 SDK 계열(`LLMProvider.sdk_kind`)이라 openrouter 는 openai 로
# 매기고 provider_name 으로 가른다. web_search 는 P39 의 검색 호출이다
PriceKind = Literal["openai", "anthropic", "stub", "web_search"]
# 백만 토큰당 달러
Usd = Annotated[float, Field(ge=0, le=10_000)]
ModelPattern = Annotated[str, Field(pattern=r"^[A-Za-z0-9._:/-]{1,128}\*?$")]
# 문서에는 ISO 날짜 글로 둔다. BSON 은 시각 없는 날짜를 받지 않는다
IsoDate = Annotated[date, PlainSerializer(date.isoformat, return_type=str)]


class PriceTier(BaseModel):
    """긴 프롬프트의 값. 요청 하나의 input 토큰(캐시 읽기·쓰기를 품는다)이
    `above_input_tokens` 보다 많으면 그 요청의 토큰 전부를 이 값으로 매긴다. Claude Haiku 5.5 의
    100,000 토큰 문턱이 그렇다."""

    model_config = ConfigDict(extra="forbid")

    above_input_tokens: int = Field(ge=1, le=10**9)
    input: Usd
    output: Usd
    cache_read: Usd | None = None
    cache_write: Usd | None = None


class PriceRule(BaseModel):
    """가격 규칙 한 줄. 금액은 백만 토큰당 달러이고 cache 값이 비면 input 가격이다."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, max_length=200)  # 비어 오면 저장이 정한다
    provider_kind: PriceKind
    provider_name: str | None = Field(default=None, max_length=64)
    model: ModelPattern  # 정확한 이름 또는 `*` 로 끝나는 앞머리
    input: Usd = 0
    output: Usd = 0
    cache_read: Usd | None = None
    cache_write: Usd | None = None
    request: Annotated[float, Field(ge=0, le=100)] | None = None  # 요청당 달러 (web_search)
    # 긴 프롬프트의 값. 화면 양식에 칸은 없고 복사와 고침이 그대로 옮긴다
    tier: PriceTier | None = None
    effective_from: IsoDate
    note: str | None = Field(default=None, max_length=200)

    @property
    def key(self) -> tuple[str, str | None, str, date]:
        """같으면 안 되는 칸 묶음."""
        return (self.provider_kind, self.provider_name, self.model, self.effective_from)

    @property
    def is_prefix(self) -> bool:
        return self.model.endswith("*")

    def matches_model(self, model: str) -> bool:
        if self.is_prefix:
            return model.startswith(self.model[:-1])
        return model == self.model


class DefaultPriceRule(PriceRule):
    """기본 가격. 출처와 확인한 날짜를 함께 든다. 화면이 확인 날짜를 늘 보인다."""

    source: str
    checked_at: IsoDate


class PricingTable(SeqModel):
    """관리자가 고친 규칙들. 판은 저장된 `seq` 이고 저장할 때마다 하나씩 오른다."""

    rules: list[PriceRule] = Field(default_factory=list)
    updated_at: UtcDatetime | None = None
    updated_by: str | None = None

    @property
    def version(self) -> int:
        """화면이 읽고 저장에 돌려주는 판. 저장된 `seq` 다."""
        return self._seq

    @model_validator(mode="after")
    def _check_rules(self) -> PricingTable:
        if len(self.rules) > MAX_PRICE_RULES:
            raise ValueError(f"at most {MAX_PRICE_RULES} rules")
        keys = [r.key for r in self.rules]
        if len(keys) != len(set(keys)):
            raise ValueError("two rules share provider_kind, provider_name, model, effective_from")
        ids = [r.id for r in self.rules if r.id is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("two rules share an id")
        if any(i.startswith(DEFAULT_ID_PREFIX) for i in ids):
            raise ValueError(f"rule ids starting with {DEFAULT_ID_PREFIX!r} belong to the defaults")
        return self


def _default_id(rule: dict[str, str]) -> str:
    """기본 가격의 id 는 규칙의 열쇠에서 나온다. 파일의 줄 순서가 바뀌어도 같은 규칙은 같은
    id 다."""
    parts = (rule["provider_kind"], rule.get("provider_name") or "", rule["model"])
    return DEFAULT_ID_PREFIX + ":".join((*parts, rule["effective_from"]))


@cache
def default_rules() -> tuple[DefaultPriceRule, ...]:
    """`pricing_defaults.json` 의 기본 가격. 파일은 이미지에 함께 실리고 바뀌지 않는다."""
    rows = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))["rules"]
    return tuple(DefaultPriceRule.model_validate({**r, "id": _default_id(r)}) for r in rows)


class MongoPricingStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[SETTINGS]

    async def init_schema(self) -> None:
        return None

    async def get(self) -> PricingTable:
        doc = await self.coll.find_one({"_id": PRICING_DOC_ID})
        return PricingTable() if doc is None else from_stored(PricingTable, doc)

    async def replace(
        self, rules: list[PriceRule], *, expected_version: int, updated_by: str | None
    ) -> PricingTable | None:
        """표 전체를 바꾼다(사람의 저장, P68). 저장된 판이 expected_version 일 때만 맞고 판을 하나
        올린다. 아직 문서가 없으면 판은 0 이다. 맞지 않으면 None 이고, 표가 틀렸으면 pydantic
        `ValidationError` 다. id 가 없는 규칙에는 새 id 를 준다."""
        current = await self.get()
        current._seq = expected_version
        named = [r if r.id else r.model_copy(update={"id": new_id()}) for r in rules]
        doc = await upsert_by_seq(self.coll, PRICING_DOC_ID, current, {"rules": named}, updated_by)
        return None if doc is None else from_stored(PricingTable, doc)
