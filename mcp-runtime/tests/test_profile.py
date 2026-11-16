from __future__ import annotations

import json

import pytest

from runtime.profile import ProfileConfig, ProfileError, load_profile, parse_profile


def test_defaults_when_missing(tmp_path):
    p = load_profile(tmp_path / "nope.json")
    assert p == ProfileConfig()
    assert p.tools == [] and p.tool_overrides == {} and p.auth_kind == "none"
    assert p.soap is False and p.endpoint is None


def test_load_from_file(tmp_path):
    f = tmp_path / "profile.json"
    f.write_text(
        json.dumps(
            {
                "tools": ["Add", "Add"],
                "tool_overrides": {"Add": {"name": "add_numbers", "description": "d"}},
                "endpoint": "http://soap:18080/calc",
                "auth_kind": "basic",
                "soap": True,
            }
        )
    )
    p = load_profile(f)
    assert p.tools == ["Add"]  # dedupe, order kept
    assert p.mcp_names == {"Add": "add_numbers"}
    assert p.descriptions == {"Add": "d"}
    assert p.endpoint == "http://soap:18080/calc" and p.auth_kind == "basic" and p.soap


def test_invalid_json_is_profile_error(tmp_path):
    f = tmp_path / "profile.json"
    f.write_text("{not json")
    with pytest.raises(ProfileError) as ei:
        load_profile(f)
    assert ei.value.status == 422 and ei.value.code == "invalid_profile"


@pytest.mark.parametrize(
    "bad",
    [
        "add numbers",  # space
        "add-numbers",  # dash (FastMCP would turn it into add_numbers)
        "add.numbers",
        "_add",  # leading underscore stripped by FastMCP
        "add__numbers",  # double underscore collapsed by FastMCP
        "a" * 65,  # > 64
        "a" * 57,  # 57..64 passes the regex but FastMCP truncates at 56
        "",
        "덧셈",
    ],
)
def test_override_name_rejected(bad):
    with pytest.raises(ProfileError) as ei:
        parse_profile({"tool_overrides": {"Add": {"name": bad}}})
    assert ei.value.status == 422
    assert "tool_overrides.Add.name" in ei.value.message


@pytest.mark.parametrize("good", ["Add", "add_numbers", "A1_b2", "a" * 56])
def test_override_name_accepted(good):
    p = parse_profile({"tool_overrides": {"Add": {"name": good}}})
    assert p.mcp_names == {"Add": good}


def test_bad_auth_kind():
    with pytest.raises(ProfileError):
        parse_profile({"auth_kind": "oauth"})


def test_backends_profile_parsed():
    p = parse_profile(
        {
            "backends": [
                {"spec": "openapi-0.yaml", "tools": ["Add"], "soap": True, "auth_kind": "basic"},
                {
                    "spec": "openapi-1.yaml",
                    "tools": ["getPet"],
                    "tool_overrides": {"getPet": {"name": "get_pet", "description": "d"}},
                },
            ]
        }
    )
    assert [b.spec for b in p.backends] == ["openapi-0.yaml", "openapi-1.yaml"]
    assert p.backends[0].soap is True and p.backends[0].auth_kind == "basic"
    assert p.backends[1].mcp_names == {"getPet": "get_pet"}
    assert p.tools == []  # 평면 필드는 backends 형식에서 쓰이지 않는다


def test_backends_entry_requires_spec():
    with pytest.raises(ProfileError):
        parse_profile({"backends": [{"tools": ["Add"]}]})
    with pytest.raises(ProfileError):
        parse_profile({"backends": [{"spec": "  "}]})


def test_backends_override_name_rules_apply():
    with pytest.raises(ProfileError) as ei:
        parse_profile(
            {"backends": [{"spec": "s.yaml", "tool_overrides": {"Add": {"name": "bad name"}}}]}
        )
    assert ei.value.status == 422
