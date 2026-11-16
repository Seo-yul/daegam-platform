"""플랫폼과 agent-runtime 의 장기 기억 네임스페이스가 어긋나지 않게 막는다 (D34).

런타임은 `(tenant, instance_id, sha256(세션 소유자 sub))` 로 쓰고 플랫폼은 같은 값을 다시 만들어
스키마를 만들고 지운다. 두 레포가 lock 과 이미지 컨텍스트를 따로 가져 공용 패키지 대신 같은 규칙을
두 벌 둔다. 한쪽만 바뀌면 지우는 API 가 아무것도 못 지우거나(해시가 다르다) 런타임이
`long_term_schema_missing` 으로 기동을 못 한다(스키마 이름이 다르다). 둘 다 조용히 틀린다.
`test_oidc_copy.py`·`test_toolname_copy.py` 와 같은 이유의 드리프트 테스트다.

해시는 문장 모양이 아니라 결과를 비교한다. 인자 이름이 서로 다르므로(런타임 `owner_sub`, 플랫폼
`user_sub`) AST 로 대조하면 뜻이 같아도 어긋난 것으로 읽힌다.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from daegam.services import agent_memory
from tests.helpers.source import AGENT_RUNTIME, exec_excerpt, py_constant, read_source

RUNTIME_LONG_TERM = AGENT_RUNTIME / "long_term.py"
RUNTIME_DSN = AGENT_RUNTIME / "dsn.py"
SUBS = ("alice@example.com", "auth0|user.name", "sub-with-%_glob", "한글")


def _function(path: Path, name: str) -> Any:
    """모듈 최상위 함수 하나를 떼어 돌린다. 결과로 비교하려는 것이다."""
    return exec_excerpt(path, {"hashlib": hashlib}, functions=(name,))[name]


def test_postgres_schema_name_matches() -> None:
    assert agent_memory.LONG_TERM_SCHEMA == py_constant(RUNTIME_LONG_TERM, "POSTGRES_SCHEMA")


def test_user_label_hash_matches() -> None:
    runtime_label = _function(RUNTIME_LONG_TERM, "user_label")
    for sub in SUBS:
        assert agent_memory.user_label(sub) == runtime_label(sub)


def test_redis_store_prefix_matches() -> None:
    """런타임은 `store_prefix=f"{redis_prefix(instance_id)}store"` 로 준다."""
    redis_prefix = _function(RUNTIME_DSN, "redis_prefix")
    instance_id = "ai-support-0001"
    assert agent_memory.redis_store_prefix(instance_id) == f"{redis_prefix(instance_id)}store"
    source = read_source(RUNTIME_LONG_TERM)
    assert 'store_prefix=f"{redis_prefix(instance_id)}store"' in source, (
        "런타임이 store_prefix 를 다르게 준다. 플랫폼의 redis_store_prefix 도 함께 고친다"
    )


def test_namespace_label_order_matches() -> None:
    """네임스페이스는 (tenant, instance_id, 사용자 해시) 순이고 langgraph 가 점으로 잇는다."""
    source = read_source(RUNTIME_LONG_TERM)
    assert "return (self.tenant, self.instance_id, user_label(owner_sub))" in source, (
        "런타임의 네임스페이스 순서가 바뀌었다. agent_memory.namespace_prefix 도 함께 고친다"
    )
    assert agent_memory.NAMESPACE_SEPARATOR == "."


def test_runtime_does_not_create_the_schema() -> None:
    """스키마를 만드는 것은 플랫폼 몫이다. 런타임이 만들기 시작하면 이 자리가 중복이 된다."""
    source = read_source(RUNTIME_LONG_TERM)
    assert "CREATE SCHEMA" not in source
    assert "long_term_schema_missing" in source
