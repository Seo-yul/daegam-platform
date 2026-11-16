"""P47 6.9절: Teams 봇의 글과 조사.

두 언어가 같은 열쇠와 같은 자리표시자를 갖는다. Python 조사(`services/channels/particle.py`)는
화면의 `ui/src/lib/particle.ts` 와 같은 낱말에 같은 답을 낸다. 사례는 화면의 시험
(`ui/src/test/particle.test.ts`)에서 읽어 두 구현이 한 표를 본다.
"""

from __future__ import annotations

import re

import pytest

from daegam.services.channels.package import COMMANDS
from daegam.services.channels.particle import PAIRS, has_final_consonant, particle
from daegam.services.channels.texts import EN, KO, PLACEHOLDER, placeholders, text
from tests.helpers.consts import REPO

PLATFORM = REPO / "platform" / "daegam"
# 봇의 글을 고르는 코드. 여기 적힌 열쇠가 표에 있어야 한다. 올리는 곳은 KeyError 를 잡지 않는다
TEXT_USERS = [
    *sorted((PLATFORM / "services" / "channels").glob("*.py")),
    PLATFORM / "api" / "channels.py",
]
_KEY = re.compile(r'"((?:bot|card)\.[A-Za-z.]+)"')

UI_CASES = REPO / "ui" / "src" / "test" / "particle.test.ts"
_CASE = re.compile(r'\["([^"]*)", "([^"]+)", "([^"]+)"\]')
_FINAL = re.compile(r'\["([^"]*)", (true|false)\]')


def _ui_cases() -> list[tuple[str, str, str]]:
    source = UI_CASES.read_text(encoding="utf-8")
    block = source.split("const CASES", 1)[1].split("];", 1)[0]
    cases = _CASE.findall(block)
    assert len(cases) >= 15, "화면 시험의 사례를 읽지 못했다"
    return cases


def _ui_finals() -> list[tuple[str, bool]]:
    source = UI_CASES.read_text(encoding="utf-8")
    block = source.split("hasFinalConsonant(%j)", 1)[0].rsplit("test.each([", 1)[1]
    return [(w, v == "true") for w, v in _FINAL.findall(block)]


def test_every_particle_pair_is_known():
    for template in KO.values():
        for m in PLACEHOLDER.finditer(template):
            assert m.group(2) is None or m.group(2) in PAIRS, template


def test_every_key_the_code_names_is_in_the_table():
    used = {key for f in TEXT_USERS for key in _KEY.findall(f.read_text(encoding="utf-8"))}
    assert "bot.welcome" in used and "bot.relinked" in used
    assert used <= set(KO), used - set(KO)
    for command in COMMANDS:
        assert f"manifest.commands.{command}" in KO
        assert f"manifest.commands.{command}.description" in KO


def test_values_are_escaped_for_markdown_but_links_are_not():
    """Teams 는 봇의 글을 Markdown 으로 그린다. IdP 가 준 이름의 글자가 꾸밈이 되지 않는다."""
    got = text("bot.linked", "ko", username="kim_*lee*[x](y)")
    assert "kim\\_\\*lee\\*\\[x\\]\\(y\\)" in got
    # 조사는 원래 글자로 고른다
    assert text("bot.agentChosen", "ko", agent="Agent_1").startswith("Agent\\_1을 ")
    link = "https://daegam.example.com/a_b?x=1"
    assert text("bot.truncated", "en", link=link).endswith(link)


def test_both_languages_hold_the_same_keys_and_placeholders():
    assert set(KO) == set(EN)
    for key in KO:
        assert placeholders(KO[key]) == placeholders(EN[key]), key
    # 영어는 조사를 붙이지 않는다
    assert not any("|" in m for v in EN.values() for m in re.findall(r"\{[^}]*\}", v))


@pytest.mark.parametrize(("word", "pair", "expected"), _ui_cases())
def test_particle_matches_the_screen(word, pair, expected):
    assert particle(word, pair) == expected


def test_final_consonant_matches_the_screen():
    finals = _ui_finals()
    assert finals
    for word, expected in finals:
        assert has_final_consonant(word) is expected, word


def test_the_screens_euro_ro_cases_hold():
    assert [f"{w}{particle(w, '으로/로')}" for w in ["r1", "r2", "r3", "r7", "r10"]] == [
        "r1로",
        "r2로",
        "r3으로",
        "r7로",
        "r10으로",
    ]
    assert [particle(w, "으로/로") for w in ["서울", "학교", "집"]] == ["로", "로", "으로"]


def test_text_fills_values_and_particles():
    assert text("bot.agentChosen", "ko", agent="사내 규정") == (
        "사내 규정을 선택했습니다. 메시지를 보내 주세요."
    )
    assert text("bot.agentChosen", "ko", agent="MCP Server").startswith("MCP Server를 ")
    assert text("bot.agentChosen", "en", agent="Helper") == "Selected Helper. Send a message."
    assert text("bot.linked", None, username="kim").startswith("대감 계정(kim)")
    # 모르는 언어는 한국어다
    assert text("bot.help", "fr") == KO["bot.help"]
    with pytest.raises(KeyError):
        text("bot.linked", "ko")


def test_manifest_command_titles_fit_teams_limits():
    """Teams 매니페스트의 명령 제목은 32자, 설명은 128자까지다 [inferred: Teams 스키마]."""
    for table in (KO, EN):
        for key, value in table.items():
            if key.startswith("manifest.commands."):
                limit = 128 if key.endswith(".description") else 32
                assert len(value) <= limit, key
        assert len(table["manifest.short"]) <= 80 and len(table["manifest.name"]) <= 30
