"""alias 폭탄을 문에서 막는 YAML 로더 (yamlsafe.py)."""

from __future__ import annotations

import time

import pytest
import yaml

from daegam import yamlsafe


def bomb(depth: int, fan: int = 10) -> str:
    """깊이 depth, 갈래 fan 의 alias 폭탄. 펼치면 fan**depth 잎이다. 깊이 6 이면 백만이다."""
    lines = [f"l0: &l0 [{','.join(['x'] * fan)}]"]
    for d in range(1, depth):
        lines.append(f"l{d}: &l{d} [{','.join([f'*l{d - 1}'] * fan)}]")
    return "\n".join(lines) + "\n"


def test_bomb_is_rejected_before_it_is_expanded():
    """300B 문서가 펼치면 백만 노드다. 문을 통과시키면 json.dumps 와 key_paths 에서 수 GB 가
    된다. 읽는 도중에 끊어야 하므로 시간도 본다."""
    doc = bomb(6)
    assert len(doc) < 400
    started = time.monotonic()
    with pytest.raises(yamlsafe.YAMLTooLarge):
        yamlsafe.safe_load(doc)
    assert time.monotonic() - started < 1.0
    # YAMLError 자손이라 호출자들의 `except yaml.YAMLError` 가 그대로 받아 422 로 바꾼다
    with pytest.raises(yaml.YAMLError) as e:
        yamlsafe.safe_load(bomb(9))
    assert isinstance(e.value, yamlsafe.YAMLTooLarge)


def test_moderate_alias_use_loads_like_safe_load():
    """anchor 로 공통 파라미터를 나누는 정상 스펙은 그대로 열린다. 결과도 같다."""
    normal = (
        "defaults: &d {timeout: 5, retries: 2}\n"
        "svc1:\n  <<: *d\n  url: a\n"
        "svc2:\n  <<: *d\n  url: b\n"
        "list: [*d, *d]\n"
        "nums: [1, 2.5, true, null, 2024-01-01]\n"
    )
    assert yamlsafe.safe_load(normal) == yaml.safe_load(normal)
    # 깊이 4 (만 노드) 는 큰 스펙 정도라 열린다
    assert yamlsafe.safe_load(bomb(4))["l3"]


def test_deep_nesting_is_too_large_not_a_crash():
    """composer 가 중첩마다 재귀해 수백 겹이면 RecursionError 다. YAMLError 로 바꿔야 호출자가
    422 로 옮긴다."""
    deep = "a: " + "[" * 5000 + "]" * 5000
    with pytest.raises(yamlsafe.YAMLTooLarge):
        yamlsafe.safe_load(deep)
    shallow = "a: " + "[" * 20 + "]" * 20
    assert yamlsafe.safe_load(shallow) == yaml.safe_load(shallow)


def test_no_tag_execution():
    """SafeLoader 자손이다. python 태그는 여전히 막힌다."""
    with pytest.raises(yaml.YAMLError):
        yamlsafe.safe_load("!!python/object/apply:os.system ['true']")
