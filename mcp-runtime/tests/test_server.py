"""인메모리 FastMCP Client 로 서브셋·오버라이드·SOAP 브릿지를 확인한다."""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from spec2openapi.bridge import BridgeOptions

from runtime.main import build_server
from runtime.profile import ProfileConfig, ProfileError, parse_profile
from tests.conftest import load_fixture

# petstore 픽스처에 있는 operation 다섯이다
PETSTORE_TOOLS = ["addPet", "delete_pets_petId", "getPet", "get_pets", "uploadPhoto"]


async def tool_names(mcp) -> list[str]:
    async with Client(mcp) as c:
        return sorted(t.name for t in await c.list_tools())


@pytest.mark.parametrize(
    ("spec", "soap", "tools", "names"),
    [
        ("calculator.openapi.yaml", True, ["Add"], ["Add"]),
        ("calculator.openapi.yaml", True, [], ["Add", "Divide"]),
        ("petstore.openapi.yaml", False, ["addPet", "getPet"], ["addPet", "getPet"]),
        ("petstore.openapi.yaml", False, [], PETSTORE_TOOLS),
    ],
    ids=["soap-subset", "soap-all", "rest-subset", "rest-all"],
)
async def test_tool_subset(spec, soap, tools, names):
    """tools 에 든 operationId 만 툴이 되고, tools 가 비면 스펙의 operation 을 모두 노출한다."""
    profile = ProfileConfig(tools=tools, soap=soap)
    mcp = build_server(load_fixture(spec), profile, BridgeOptions())
    assert await tool_names(mcp) == names


async def test_excluded_tool_is_unknown(calculator_spec):
    mcp = build_server(calculator_spec, parse_profile({"tools": ["Add"], "soap": True}))
    async with Client(mcp) as c:
        with pytest.raises(ToolError) as ei:
            await c.call_tool("Divide", {"numerator": 1, "denominator": 2})
    assert "unknown tool" in str(ei.value).lower()


async def test_unknown_tool_name_in_profile_is_ignored(calculator_spec, caplog):
    mcp = build_server(calculator_spec, parse_profile({"tools": ["Add", "Nope"], "soap": True}))
    assert await tool_names(mcp) == ["Add"]
    assert "Nope" in caplog.text


async def test_override_name_and_description(calculator_spec, mock_soap):
    profile = parse_profile(
        {
            "tools": ["Add"],
            "tool_overrides": {"Add": {"name": "add_numbers", "description": "두 정수를 더한다"}},
            "soap": True,
        }
    )
    mcp = build_server(calculator_spec, profile, BridgeOptions(endpoint=f"{mock_soap}/calc"))
    async with Client(mcp) as c:
        tools = await c.list_tools()
        assert [t.name for t in tools] == ["add_numbers"]
        assert tools[0].description == "두 정수를 더한다\nReturns {result}."
        res = await c.call_tool("add_numbers", {"a": 3, "b": 4})
        assert res.structured_content == {"result": 7}
        with pytest.raises(ToolError):
            await c.call_tool("Add", {"a": 3, "b": 4})  # original name is gone


async def test_override_description_only_keeps_name(calculator_spec):
    profile = parse_profile({"tool_overrides": {"Divide": {"description": "나눗셈"}}, "soap": True})
    mcp = build_server(calculator_spec, profile)
    async with Client(mcp) as c:
        by_name = {t.name: t for t in await c.list_tools()}
    assert set(by_name) == {"Add", "Divide"}
    # 주인이 바꾼 설명에도 결과 키 줄이 붙는다(P56 6.9절)
    assert by_name["Divide"].description == "나눗셈\nReturns {result}."
    assert by_name["Add"].description == "Adds two integers and returns the sum.\nReturns {result}."


def test_invalid_override_name_fails_before_server(calculator_spec):
    with pytest.raises(ProfileError) as ei:
        parse_profile({"tool_overrides": {"Add": {"name": "add numbers"}}})
    assert ei.value.status == 422


async def test_soap_add_reaches_mock(calculator_spec, mock_soap):
    profile = parse_profile({"tools": ["Add"], "soap": True, "endpoint": f"{mock_soap}/calc"})
    # endpoint from profile.json is used when the env override is absent
    mcp = build_server(calculator_spec, profile, BridgeOptions())
    async with Client(mcp) as c:
        res = await c.call_tool("Add", {"a": 3, "b": 4})
    assert res.structured_content == {"result": 7}


async def test_env_endpoint_wins_over_profile(calculator_spec, mock_soap):
    profile = parse_profile({"tools": ["Add"], "soap": True, "endpoint": "http://127.0.0.1:9/x"})
    mcp = build_server(calculator_spec, profile, BridgeOptions(endpoint=f"{mock_soap}/calc"))
    async with Client(mcp) as c:
        res = await c.call_tool("Add", {"a": 1, "b": 2})
    assert res.structured_content == {"result": 3}


def test_rest_with_wsse_rejected(petstore_spec):
    with pytest.raises(ProfileError) as ei:
        build_server(petstore_spec, parse_profile({"auth_kind": "wsse"}), BridgeOptions())
    assert ei.value.status == 422 and ei.value.code == "wsse_unsupported_for_rest"


async def test_soap_with_wsse_allowed(calculator_spec):
    mcp = build_server(
        calculator_spec, parse_profile({"auth_kind": "wsse", "soap": True}), BridgeOptions()
    )
    assert await tool_names(mcp) == ["Add", "Divide"]


def test_soap_flag_mismatch_warns(calculator_spec, caplog):
    build_server(calculator_spec, ProfileConfig(soap=False), BridgeOptions())
    assert "profile.soap=False" in caplog.text


# -- backends 합성 (복수 backend profile.json) --------------------------------------


def _composite(calculator_spec, petstore_spec, mock_soap=None, calc_tools=None):
    from runtime.main import build_composite_server

    profile = parse_profile(
        {
            "backends": [
                {"spec": "openapi-0.yaml", "tools": calc_tools or ["Add"], "soap": True},
                {"spec": "openapi-1.yaml", "tools": ["addPet", "getPet"]},
            ]
        }
    )
    options = [
        BridgeOptions(endpoint=f"{mock_soap}/calc" if mock_soap else None),
        BridgeOptions(),
    ]
    return build_composite_server([calculator_spec, petstore_spec], profile, options)


async def test_composite_merges_backends_flat(calculator_spec, petstore_spec):
    """프리픽스 없이 평평하게 합쳐진다. 이름 유일성은 플랫폼이 보장한다."""
    mcp = _composite(calculator_spec, petstore_spec)
    assert await tool_names(mcp) == ["Add", "addPet", "getPet"]


async def test_composite_call_reaches_soap_backend(calculator_spec, petstore_spec, mock_soap):
    mcp = _composite(calculator_spec, petstore_spec, mock_soap=mock_soap)
    async with Client(mcp) as c:
        res = await c.call_tool("Add", {"a": 3, "b": 4})
    assert res.structured_content == {"result": 7}


async def test_composite_overrides_per_backend(calculator_spec, petstore_spec):
    from runtime.main import build_composite_server

    profile = parse_profile(
        {
            "backends": [
                {
                    "spec": "openapi-0.yaml",
                    "tools": ["Add"],
                    "soap": True,
                    "tool_overrides": {"Add": {"name": "add_numbers", "description": "더한다"}},
                },
                {"spec": "openapi-1.yaml", "tools": ["getPet"]},
            ]
        }
    )
    mcp = build_composite_server(
        [calculator_spec, petstore_spec], profile, [BridgeOptions(), BridgeOptions()]
    )
    async with Client(mcp) as c:
        tools = {t.name: t for t in await c.list_tools()}
    assert set(tools) == {"add_numbers", "getPet"}
    assert tools["add_numbers"].description == "더한다\nReturns {result}."


async def test_composite_single_entry_matches_flat(calculator_spec):
    """항목이 하나뿐인 backends 형식도 단일 경로와 같은 툴을 노출한다."""
    from runtime.main import build_composite_server

    profile = parse_profile({"backends": [{"spec": "s.yaml", "tools": ["Add"], "soap": True}]})
    mcp = build_composite_server([calculator_spec], profile, [BridgeOptions()])
    assert await tool_names(mcp) == ["Add"]


def test_composite_wsse_rest_rejected(petstore_spec):
    from runtime.main import build_composite_server

    profile = parse_profile({"backends": [{"spec": "s.yaml", "auth_kind": "wsse"}]})
    with pytest.raises(ProfileError) as ei:
        build_composite_server([petstore_spec], profile, [BridgeOptions()])
    assert ei.value.code == "wsse_unsupported_for_rest"


def test_composite_length_mismatch(petstore_spec):
    from runtime.main import build_composite_server

    profile = parse_profile({"backends": [{"spec": "a"}, {"spec": "b"}]})
    with pytest.raises(ProfileError):
        build_composite_server([petstore_spec], profile, [BridgeOptions()])


def test_backend_options_env_priority(monkeypatch):
    from runtime.main import _backend_options
    from runtime.profile import BackendEntry

    entry = BackendEntry(spec="s.yaml", endpoint="http://from-profile", auth_kind="basic")
    monkeypatch.setenv("DAEGAM_BACKEND_1_ENDPOINT", "http://from-env")
    monkeypatch.setenv("DAEGAM_BACKEND_1_USERNAME", "u")
    monkeypatch.setenv("DAEGAM_BACKEND_1_PASSWORD", "pw")
    opts = _backend_options(1, entry)
    assert opts.endpoint == "http://from-env"
    assert (opts.username, opts.password, opts.auth) == ("u", "pw", "basic")
    opts0 = _backend_options(0, entry)
    assert opts0.endpoint == "http://from-profile" and opts0.username is None
    assert opts0.auth == "basic"


@pytest.mark.parametrize("profile_auth", ["basic", "none"])
def test_backend_auth_env_wins_over_the_profile(monkeypatch, profile_auth):
    """플랫폼은 배포 때의 auth 를 DAEGAM_BACKEND_<i>_AUTH 로 낸다. profile.json 은 파드가 뜰 때의
    Backend API 로 만들어지므로, 그 auth_kind 가 배포 뒤에 바뀌었어도 env 가 이긴다."""
    from runtime.main import _backend_options
    from runtime.profile import BackendEntry

    monkeypatch.setenv("DAEGAM_BACKEND_0_AUTH", "wsse")
    opts = _backend_options(0, BackendEntry(spec="s.yaml", auth_kind=profile_auth))
    assert opts.auth == "wsse"


def test_backend_options_take_the_timeout_of_the_profile_entry():
    """여러 backend 의 호출 타임아웃은 profile.json 항목마다의 `timeout_seconds` 이고, 그 칸이 없는
    항목(옛 플랫폼)은 spec2openapi 의 기본값이다."""
    from runtime.main import _backend_options
    from runtime.profile import BackendEntry

    slow = _backend_options(0, BackendEntry(spec="a.yaml", timeout_seconds=90))
    assert slow.timeout == 90
    assert _backend_options(1, BackendEntry(spec="b.yaml")).timeout == BridgeOptions().timeout
    profile = parse_profile({"backends": [{"spec": "a", "timeout_seconds": 45}, {"spec": "b"}]})
    assert [b.timeout_seconds for b in profile.backends] == [45, None]
    with pytest.raises(ProfileError):
        parse_profile({"backends": [{"spec": "a", "timeout_seconds": 0}]})


def test_pod_wide_transport_settings_reach_every_backend(monkeypatch):
    """TLS 검증과 환경 신뢰는 파드 전체의 설정이라 backend 마다 같은 값이다. 타임아웃과 SOAP 머리는
    한 backend 의 값이라 전역 env 가 새지 않는다 (P76 F4)."""
    from runtime.main import _backend_options
    from runtime.profile import BackendEntry

    defaults = _backend_options(0, BackendEntry(spec="a.yaml"))
    assert (defaults.verify, defaults.trust_env) == (True, True)
    monkeypatch.setenv("SPEC2OPENAPI_VERIFY", "false")
    monkeypatch.setenv("SPEC2OPENAPI_TRUST_ENV", "false")
    monkeypatch.setenv("SPEC2OPENAPI_TIMEOUT", "5")
    monkeypatch.setenv("SPEC2OPENAPI_SOAP_HEADERS", '{"X-Only": "one backend"}')
    for i in (0, 1):
        opts = _backend_options(i, BackendEntry(spec=f"{i}.yaml"))
        assert (opts.verify, opts.trust_env) == (False, False)
        assert opts.timeout == BridgeOptions().timeout
        assert not opts.soap_headers


def test_load_backend_specs_relative(tmp_path, calculator_spec):
    import json

    import yaml

    from runtime.main import load_backend_specs
    from runtime.profile import load_profile

    (tmp_path / "openapi-0.yaml").write_text(yaml.safe_dump(calculator_spec))
    (tmp_path / "profile.json").write_text(
        json.dumps({"backends": [{"spec": "openapi-0.yaml", "soap": True}]})
    )
    profile = load_profile(tmp_path / "profile.json")
    specs = load_backend_specs(profile, tmp_path / "profile.json")
    assert len(specs) == 1 and "paths" in specs[0]
