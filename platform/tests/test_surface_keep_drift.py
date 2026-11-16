"""surface.py 의 minify keep 목록이 설치본 spec2openapi.minify 와 같은지 대조한다.

표면 보호는 스키마 안 외부 `x-*` 삭제를 허용하되 keep 목록은 지킨다. spec2openapi 가 keep 항목을
더하면 우리 목록이 그것을 "외부 확장 삭제" 로 오판해 refine 을 거부하거나 minify 를 통과시키므로,
두 값을 여기서 맞춘다. 상수가 비공개(_KEEP_*)라 이름을 모듈에서 패턴으로 모은다. 상류가 이름을
바꾸면 이 테스트가 먼저 깨져 알려 준다.
"""

from __future__ import annotations

import spec2openapi.minify as minify

from daegam.services.spec_generator.surface import KEEP_EXTENSIONS_EXACT, KEEP_EXTENSIONS_PREFIX

RENAMED = "spec2openapi.minify 에 {prefix}* 상수가 없다. 이름이 바뀌었으면 이 테스트를 고친다"


def _upstream(prefix: str) -> dict[str, object]:
    found = {name: getattr(minify, name) for name in dir(minify) if name.startswith(prefix)}
    assert found, RENAMED.format(prefix=prefix)
    return found


def _drift(label: str, theirs: frozenset[str], ours: frozenset[str], names: list[str]) -> str:
    return (
        f"keep {label} 목록이 다르다. upstream 에만: {sorted(theirs - ours)}, "
        f"surface.py 에만: {sorted(ours - theirs)} (upstream 상수: {names})"
    )


def test_keep_exact_matches_spec2openapi() -> None:
    upstream = _upstream("_KEEP_EXACT")
    theirs = frozenset().union(*upstream.values())
    ours = KEEP_EXTENSIONS_EXACT
    assert theirs == ours, _drift("정확 일치", theirs, ours, sorted(upstream))


def test_keep_prefix_matches_spec2openapi() -> None:
    upstream = _upstream("_KEEP_PREFIXES")
    theirs = frozenset(p for value in upstream.values() for p in value)
    ours = frozenset(KEEP_EXTENSIONS_PREFIX)
    assert theirs == ours, _drift("접두사", theirs, ours, sorted(upstream))
