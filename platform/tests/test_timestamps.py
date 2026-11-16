"""응답으로 나가는 시각은 UTC 임을 밝히고 저장은 naive UTC 로 둔다.

시간대 표시가 없는 ISO 문자열을 받으면 브라우저가 로컬 시각으로 읽는다. KST 에서는 화면의 모든
시각이 아홉 시간 어긋나고 감사 로그의 Since·Until 필터까지 틀린다. 그렇다고 저장을 aware 로
바꾸면 이미 들어 있는 naive 값과 비교할 때 TypeError 가 나므로, 경계에서만 시간대를 붙인다
(`store/base.py` 의 `iso_utc`·`UtcDatetime`).
"""

from __future__ import annotations

import importlib
import pkgutil
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

import daegam.audit.store
import daegam.store
from daegam.store.base import iso_utc

# 응답으로 나가지 않는 시각. 워커끼리만 보는 리스 만료라 규칙에서 뺀다
INTERNAL = {
    ("daegam.store.knowledge", "Lease", "until"),
    ("daegam.store.spec_jobs", "Lease", "until"),
}


def test_iso_utc_moves_other_zones_to_utc() -> None:
    kst = datetime(2026, 9, 10, 11, 38, 2, 85000, tzinfo=timezone(timedelta(hours=9)))
    assert iso_utc(kst) == "2026-09-10T02:38:02.085000+00:00"
    assert iso_utc(datetime(2026, 9, 10, 2, 38, 2, 85000, tzinfo=UTC)).endswith("+00:00")


def _annotation_parts(annotation: object) -> tuple[list[object], list[object]]:
    """중첩된 Annotated·Union 을 풀어 (타입들, 메타데이터들) 로 나눈다."""
    types: list[object] = []
    meta: list[object] = []
    stack = [annotation]
    while stack:
        node = stack.pop()
        if get_origin(node) is Annotated:
            args = get_args(node)
            stack.append(args[0])
            meta.extend(args[1:])
            continue
        args = get_args(node)
        if args:
            stack.extend(a for a in args if a is not type(None))
        else:
            types.append(node)
    return types, meta


def _is_datetime(field: FieldInfo) -> bool:
    types, _ = _annotation_parts(field.annotation)
    return any(t is datetime for t in types)


def _marks_utc(field: FieldInfo) -> bool:
    _, meta = _annotation_parts(field.annotation)
    return any(getattr(m, "func", None) is iso_utc for m in [*meta, *field.metadata])


def _models() -> list[tuple[str, type[BaseModel]]]:
    modules = [daegam.audit.store]
    for info in pkgutil.iter_modules(daegam.store.__path__):
        modules.append(importlib.import_module(f"daegam.store.{info.name}"))
    out: list[tuple[str, type[BaseModel]]] = []
    for module in modules:
        for name in dir(module):
            obj = getattr(module, name)
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseModel)
                and obj.__module__ == module.__name__
            ):
                out.append((module.__name__, obj))
    return out


def test_every_stored_timestamp_declares_utc_on_the_way_out() -> None:
    """새 시각 필드를 `datetime` 으로 적으면 여기서 걸린다. `UtcDatetime` 으로 적어야 한다."""
    models = _models()
    assert len(models) > 30, "store 모델을 하나도 못 찾았다"
    plain = [
        f"{module}.{model.__name__}.{name}"
        for module, model in models
        for name, field in model.model_fields.items()
        if _is_datetime(field)
        and not _marks_utc(field)
        and (module, model.__name__, name) not in INTERNAL
    ]
    assert not plain, (
        "응답으로 나갈 수 있는 시각 필드는 daegam.store.base.UtcDatetime 으로 적는다:\n  "
        + "\n  ".join(plain)
    )


def test_models_serialize_timestamps_with_a_zone_but_store_them_naive() -> None:
    """`model_dump(mode="json")` 은 `.isoformat()` 을 거치지 않는다. 필드 타입이 규칙을 태운다."""
    from daegam.store.clusters import Preflight

    p = Preflight(ok=True)
    assert p.model_dump(mode="json")["checked_at"].endswith("+00:00")
    assert p.model_dump()["checked_at"].tzinfo is None
