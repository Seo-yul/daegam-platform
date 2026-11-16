"""플랫폼 `WebFetchSettings` 와 web-tools `webtools/settings.py` 의 맞대기 (P39b 13절). 플랫폼이
복제본 env 로 렌더하는 이름(`fetch_env`)을 web-tools 가 모두 읽고, 설정의 범위와 기본값이 web-tools
가 다시 자르는 상한과 기본값과 같아야 한다. 어긋나면 저장은 받은 값을 파드가 조용히 바꾼다.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from types import ModuleType

import pytest

from daegam.k8s.templates.web_tools import CA_ENV, fetch_env
from daegam.store.settings import WebFetchSettings
from tests.helpers.consts import REPO

WEB_TOOLS = REPO / "web-tools"


@pytest.fixture
def webtools_settings(monkeypatch) -> Iterator[ModuleType]:
    """web-tools 의 settings 모듈이다. 표준 라이브러리만 쓰므로 플랫폼 venv 에서 읽힌다."""
    monkeypatch.syspath_prepend(str(WEB_TOOLS))
    try:
        yield importlib.import_module("webtools.settings")
    finally:
        for name in [n for n in sys.modules if n == "webtools" or n.startswith("webtools.")]:
            del sys.modules[name]


def _bounds(field: str) -> tuple[object, object]:
    """WebFetchSettings 필드의 (ge, le) 다."""
    metadata = WebFetchSettings.model_fields[field].metadata
    ge = next(m.ge for m in metadata if hasattr(m, "ge"))
    le = next(m.le for m in metadata if hasattr(m, "le"))
    return ge, le


def test_every_rendered_env_name_is_read(webtools_settings) -> None:
    rendered = {e["name"] for e in fetch_env(WebFetchSettings())}
    source = (WEB_TOOLS / "webtools" / "settings.py").read_text(encoding="utf-8")
    assert all(f'"{name}"' in source for name in rendered), rendered
    loaded = webtools_settings.load({e["name"]: e["value"] for e in fetch_env(WebFetchSettings())})
    assert loaded == webtools_settings.FetchSettings(), "기본값을 렌더하면 기본값으로 읽는다"


def test_settings_bounds_match_the_caps(webtools_settings) -> None:
    w = webtools_settings
    assert _bounds("max_bytes") == (1024, w.MAX_BYTES_CAP)
    assert _bounds("max_chars") == (500, w.MAX_CHARS_CAP)
    assert _bounds("timeout_seconds") == (1, w.TIMEOUT_CAP)
    assert _bounds("max_redirects") == (0, w.MAX_REDIRECTS_CAP)
    assert _bounds("per_minute") == (1, w.PER_MINUTE_CAP)
    ports = WebFetchSettings.model_fields["allowed_ports"].metadata
    cidrs = WebFetchSettings.model_fields["blocked_cidrs"].metadata
    assert next(m.max_length for m in ports if hasattr(m, "max_length")) == w.MAX_PORTS
    assert next(m.max_length for m in cidrs if hasattr(m, "max_length")) == w.MAX_CIDRS


def test_the_ca_env_name_is_the_one_web_tools_reads(webtools_settings) -> None:
    """복제본의 CA 는 `fetch_env` 밖에서 붙는다(`render_fetch_clone`). 이름이 갈리면 파드가 CA 없이
    가져온다."""
    assert webtools_settings.CA_ENV == CA_ENV
