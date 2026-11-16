"""WSDL 이 선언한 soap:header 의 값이 파드의 env 에서 SOAP 봉투로 가는가 (T60).

플랫폼은 backend 가 하나면 `SPEC2OPENAPI_SOAP_HEADERS`, 여럿이면 `DAEGAM_BACKEND_<i>_SOAP_HEADERS`
로 JSON 객체를 넣는다. 서버는 파드처럼 옵션 없이 만든다. 받는 쪽은 봉투를 적어 두고 계산기로 답한다.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from xml.etree import ElementTree

import pytest
from fastmcp import Client
from spec2openapi.convert import convert_wsdl

from runtime.main import _backend_options, build_composite_server, build_server
from runtime.profile import BackendEntry, parse_profile
from tests.conftest import FIXTURES, serve
from tests.mock_soap_server import SoapHandler

CALC_NS = "http://example.com/calculator"
SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"
WSDL = FIXTURES / "calculator-auth-header.wsdl"


class _Recording(SoapHandler):
    """받은 봉투를 적어 두고 /calc 로 답한다."""

    envelopes: list[bytes] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).envelopes.append(body)
        self.rfile = io.BytesIO(body)
        self.path = "/calc"
        super().do_POST()


@pytest.fixture
def recording() -> Iterator[tuple[str, list[bytes]]]:
    _Recording.envelopes = []
    with serve(_Recording) as base_url:
        yield base_url, _Recording.envelopes


@pytest.fixture(scope="module")
def header_spec() -> dict:
    return convert_wsdl(str(WSDL))


def _tokens(envelope: bytes) -> list[str]:
    """봉투 Header 의 AuthHeader/token 값들."""
    root = ElementTree.fromstring(envelope)
    tokens = root.findall(
        "s:Header/c:AuthHeader/c:token", namespaces={"s": SOAP_ENV_NS, "c": CALC_NS}
    )
    return [t.text or "" for t in tokens]


async def _add(mcp, name: str = "Add") -> dict:
    async with Client(mcp) as c:
        res = await c.call_tool(name, {"a": 3, "b": 4})
    return res.structured_content


async def test_one_backend_sends_the_header_from_the_spec2openapi_env(
    header_spec, recording, monkeypatch
):
    base_url, envelopes = recording
    monkeypatch.setenv("SPEC2OPENAPI_SOAP_HEADERS", '{"auth": {"token": "t-single"}}')
    profile = parse_profile({"tools": ["Add"], "soap": True, "endpoint": f"{base_url}/x"})
    assert await _add(build_server(header_spec, profile)) == {"result": 7}
    assert _tokens(envelopes[-1]) == ["t-single"]


async def test_each_backend_sends_only_its_own_header(header_spec, recording, monkeypatch):
    """둘째 backend 의 값은 첫째로 새지 않고, 값이 없는 backend 는 헤더 없이 보낸다. 전역 env 는
    여러 backend 에 쓰지 않는다."""
    base_url, envelopes = recording
    monkeypatch.setenv("SPEC2OPENAPI_SOAP_HEADERS", '{"auth": {"token": "global"}}')
    monkeypatch.setenv("DAEGAM_BACKEND_1_SOAP_HEADERS", '{"AuthHeader": {"token": "t-second"}}')
    entry = {"spec": "x.yaml", "tools": ["Add"], "soap": True, "endpoint": f"{base_url}/x"}
    profile = parse_profile(
        {
            "backends": [
                entry,
                {**entry, "tool_overrides": {"Add": {"name": "add_second"}}},
            ]
        }
    )
    mcp = build_composite_server([header_spec, header_spec], profile)
    assert await _add(mcp) == {"result": 7}
    assert _tokens(envelopes[-1]) == []
    assert await _add(mcp, "add_second") == {"result": 7}
    assert _tokens(envelopes[-1]) == ["t-second"]


@pytest.mark.parametrize("raw", ["not json", '["a list"]'])
def test_a_value_that_is_not_a_json_object_is_ignored(raw, monkeypatch, caplog):
    """spec2openapi 가 backend 하나의 env 를 읽는 규칙과 같다. 값은 로그에 남기지 않는다."""
    monkeypatch.setenv("DAEGAM_BACKEND_0_SOAP_HEADERS", raw)
    assert _backend_options(0, BackendEntry(spec="a.yaml")).soap_headers is None
    assert "DAEGAM_BACKEND_0_SOAP_HEADERS" in caplog.text and raw not in caplog.text
