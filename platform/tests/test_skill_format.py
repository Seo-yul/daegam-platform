"""Skill 형식 검사 (D64, P39 1절). 표의 경계값 양쪽, CRLF·BOM 맞추기, deepagents 와 같은 머리말
정규식, 모르는 키와 스크립트 경고, 모든 파일의 보이지 않는 글자, 머리말 YAML 이스케이프가 풀린
글자, zip 풀기, 고정물 digest.

zip 의 링크·암호화·`..`·폭탄은 `read_zip` 의 테스트(test_zipsafe.py)가 보고, 그 오류가
`invalid_skill` 의 issue 로 옮겨지는 것은 test_skills_api.py 의 가져오기가 본다. 고정물
`agent-runtime/schema/fixtures/skills/` 는 런타임 테스트가 `SkillsMiddleware` 로 같은 파일을 읽어
같은 목록과 digest 인지 본다.
"""

from __future__ import annotations

import json
import re
import time

import pytest

from daegam.contracts import agent_schema
from daegam.names import K8S_NAME_RE, NAME_MAX
from daegam.services.agent_skills import MAX_PER_AGENT, MAX_PER_GRAPH
from daegam.services.emoji_ranges import is_extended_pictographic
from daegam.services.skill_format import (
    MAX_FILES,
    SKILL_MD,
    InvalidSkill,
    check_skill,
    is_script,
    read_upload,
    renamed,
    skill_digest,
)
from daegam.units import KIB
from tests.helpers.consts import REPO
from tests.helpers.documents import zip_of

FIXTURES = REPO / "agent-runtime" / "schema" / "fixtures" / "skills"
EMOJI = "⚠\ufe0f ✔\ufe0f 👨\u200d💻 1\ufe0f\u20e3 👩🏽\u200d💻 ❤\ufe0f\u200d🔥"


def _md(body: str = "# Body\n", **front: str) -> str:
    """머리말 줄을 그대로 적은 SKILL.md. 값은 YAML 원문이다."""
    head = {"name": "demo", "description": "Does a thing.", **front}
    lines = "".join(f"{k}: {v}\n" for k, v in head.items() if v is not None)
    return f"---\n{lines}---\n{body}"


def _errors(files, **kw) -> list[str]:
    return [i.code for i in check_skill(files, **kw).errors]


def _ok(files, **kw) -> bool:
    return not check_skill(files, **kw).errors


# -- 파일 수와 크기 ---------------------------------------------------------------------


def test_file_count_boundary():
    others = [(f"f{n}.md", "x") for n in range(MAX_FILES - 1)]
    assert _ok([(SKILL_MD, _md()), *others])
    assert "too_many_files" in _errors([(SKILL_MD, _md()), *others, ("one-more.md", "x")])


def test_file_size_boundaries():
    head = _md()
    fill = 64 * KIB - len(head.encode())
    assert _ok([(SKILL_MD, head + "a" * fill)])
    assert _errors([(SKILL_MD, head + "a" * (fill + 1))]) == ["file_too_large"]
    assert _ok([(SKILL_MD, head), ("big.md", "a" * 256 * KIB)])
    assert _errors([(SKILL_MD, head), ("big.md", "a" * (256 * KIB + 1))]) == ["file_too_large"]


def test_revision_total_boundary():
    head = _md()
    rest = 256 * KIB - len(head.encode())
    files = [(SKILL_MD, head), ("a.md", "a" * 256 * KIB), ("b.md", "b" * rest)]
    assert _ok(files)
    assert _errors([*files[:2], ("b.md", "b" * (rest + 1))]) == ["revision_too_large"]


# -- 경로 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["references/a.md", "a/b/c/d.md", "A-b_c.1", "x" * 100, "/".join(["y" * 49] * 4)],
)
def test_paths_accepted(path):
    assert _ok([(SKILL_MD, _md()), (path, "x")]), path


@pytest.mark.parametrize(
    "path",
    [
        "a/b/c/d/e.md",  # 깊이 5
        "x" * 101,  # 조각 101자
        "/".join(["y" * 50] * 4) + "z",  # 전체 204자
        "../a.md",
        "a/../b.md",
        "a/./b.md",
        ".hidden",
        "a/.git/x",
        "a//b.md",
        "/abs.md",
        "a\\b.md",
        "a b.md",
        "a/",
        "",
    ],
)
def test_paths_rejected(path):
    assert "path_invalid" in _errors([(SKILL_MD, _md()), (path, "x")]), path


def test_case_only_duplicates_and_file_folder_clash():
    assert _errors([(SKILL_MD, _md()), ("Refs.md", "x"), ("refs.md", "y")]) == ["path_duplicate"]
    assert _errors([(SKILL_MD, _md()), ("refs", "x"), ("REFS/a.md", "y")]) == ["path_duplicate"]
    assert _errors([(SKILL_MD, _md()), ("a/b", "x"), ("a/b/c.md", "y")]) == ["path_duplicate"]


def test_skill_md_must_sit_at_the_root():
    assert _errors([("skill.md", _md())]) == ["skill_md_missing"]
    assert _errors([("docs/SKILL.md", _md())]) == ["skill_md_missing"]
    assert _errors([]) == ["skill_md_missing"]


def test_path_rule_comes_from_the_schema():
    """경로 규칙의 원본은 `$defs/skill_path` 하나이고, `skill_name` 은 D24 이름 규칙과 같다."""
    defs = agent_schema()["$defs"]
    assert defs["skill_name"]["pattern"] == K8S_NAME_RE.pattern
    assert defs["skill_name"]["maxLength"] == NAME_MAX
    assert defs["skill_path"]["maxLength"] == 200
    assert re.fullmatch(defs["skill_path"]["pattern"], "references/a.md")


def test_count_limits_match_the_schema():
    """손으로 옮긴 개수 상한이 스키마의 maxItems 와 같다. 에이전트별 목록, 최상위 `skills`, Skill
    하나의 파일 수다."""
    schema = agent_schema()
    assert schema["$defs"]["agent_skills"]["maxItems"] == MAX_PER_AGENT
    assert schema["properties"]["skills"]["maxItems"] == MAX_PER_GRAPH
    assert schema["$defs"]["skill_bundle"]["properties"]["files"]["maxItems"] == MAX_FILES


# -- 내용 ----------------------------------------------------------------------------


def test_crlf_and_bom_are_normalized_before_storing():
    lf = _md("line one\nline two\n")
    crlf = "\ufeff" + lf.replace("\n", "\r\n")
    a = check_skill([(SKILL_MD, lf), ("n.md", "a\rb")])
    b = check_skill([(SKILL_MD, crlf.encode()), ("n.md", b"a\r\nb")])
    assert not a.errors and not b.errors
    assert [f.content for f in b.files] == [f.content for f in a.files]
    assert a.files[1].content == "a\nb" and a.digest == b.digest
    assert b.files[0].bytes == len(lf.encode())


def test_not_text_and_control_characters():
    assert _errors([(SKILL_MD, _md()), ("bin", b"\xff\xfe")]) == ["not_text"]
    assert _errors([(SKILL_MD, _md()), ("half", "\ud800")]) == ["not_text"]
    assert _errors([(SKILL_MD, _md()), ("nul", "a\x00b")]) == ["control_chars"]
    assert _errors([(SKILL_MD, _md()), ("bell", "a\x07b")]) == ["control_chars"]
    assert _errors([(SKILL_MD, _md()), ("del", "a\x7fb")]) == ["control_chars"]
    assert _errors([(SKILL_MD, _md()), ("c1", "a\x85b")]) == ["control_chars"]
    assert _errors([(SKILL_MD, _md()), ("c1", "a\x9bb")]) == ["control_chars"]
    assert _ok([(SKILL_MD, _md()), ("tab", "a\tb\n")])


@pytest.mark.parametrize(
    "text",
    [
        "a\u202eb",  # bidi 재정렬
        "a\u2067b",
        "a\U000e0041b",  # 태그 문자
        "a\U000e0100b",  # 변형 선택자 보충
        "a\u200bb",  # 폭 없는 공백
        "a\u200cb",
        "a\u2060b",
        "a\ufeffb",  # 첫 글자가 아닌 BOM
        "a\u00adb",  # 소프트 하이픈(서식 문자 Cf)
        "a\u180eb",  # 몽골어 모음 분리자(Cf)
        "a\u2061b",  # 보이지 않는 수학 연산자(Cf)
        "a\U0001d173b",  # 악보 서식(Cf)
        "a\u3164b",  # 한글 채움 문자
        "a\u115fb",
        "a\u1160b",
        "a\uffa0b",
        "a\ufe0fb",  # 떨어진 VS16
        "⚠\ufe0f\ufe0f",  # 이모지 뒤 두 번째 VS16
        "a\u200db",  # 떨어진 ZWJ
        "\U0001f468\u200db",  # 이모지와 글자 사이의 ZWJ
        "1\ufe0f",  # 키캡 없는 VS16
    ],
)
def test_invisible_characters_are_rejected_in_every_file(text):
    assert _errors([(SKILL_MD, _md()), ("notes.md", text)]) == ["invisible_chars"]
    assert _errors([(SKILL_MD, _md(text))]) == ["invisible_chars"]


def test_common_emoji_pass():
    assert _ok([(SKILL_MD, _md(f"{EMOJI}\n", description=f'"{EMOJI}"')), ("n.md", EMOJI)])
    # 이모지(Extended_Pictographic) 표의 표본이다. 키캡의 바탕 글자·ZWJ·VS16·피부색 수식자는
    # 이모지가 아니다
    for ch in "⚠✔❤©™👨💻🔥🧑":
        assert is_extended_pictographic(ch), ch
    for ch in "a1#\u200d\ufe0f\U0001f3fd한":
        assert not is_extended_pictographic(ch), ch


def test_issue_points_at_the_line():
    issue = check_skill([(SKILL_MD, _md("ok\nbad\u200b\n"))]).errors[0]
    assert issue.path == SKILL_MD and "U+200B at line 6" in issue.message


# -- 머리말 --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("# no frontmatter\n", "frontmatter_missing"),
        ("---\nname: demo\ndescription: d\n---", "frontmatter_missing"),  # 닫는 줄 뒤 줄바꿈
        ("---\nname: [demo\n---\nbody\n", "frontmatter_invalid"),
        ("---\n- a\n- b\n---\nbody\n", "frontmatter_invalid"),
    ],
)
def test_frontmatter_shape(text, code):
    assert _errors([(SKILL_MD, text)]) == [code]


@pytest.mark.parametrize(
    "text",
    [
        "---" + "\n" * 60000,  # 닫는 줄이 없다
        "---" + "\n" * 60000 + "---",  # 닫는 줄 뒤 줄바꿈이 없다
        "---" + "\n" * 500_000,  # 상한을 넘은 SKILL.md 는 머리말을 읽지 않는다
    ],
)
def test_frontmatter_search_is_linear(text):
    """정규식이 `\\s*` 를 되돌리며 끝까지 다시 훑으면 이 글들에 수 초에서 수 분이 걸린다."""
    started = time.perf_counter()
    checked = check_skill([(SKILL_MD, text)])
    assert time.perf_counter() - started < 0.5
    assert checked.frontmatter is None and checked.errors


def test_frontmatter_after_blank_lines_matches_like_deepagents():
    text = "---" + "\n" * 1000 + "name: demo\ndescription: d\n---\nbody\n"
    assert _ok([(SKILL_MD, text)])


def test_frontmatter_alias_bomb_and_deep_nesting_are_too_large_to_read():
    """둘 다 YAML 오류이지만 문법이 틀린 것과 다른 문장이다. 깊은 중첩은 파서의 재귀 한도에 걸려
    YAMLError 가 아닌 예외로 새던 것이다."""
    bomb = "a: &a [x, x, x, x, x, x, x, x, x, x]\n" + "".join(
        f"{c}: &{c} [*{p}, *{p}, *{p}, *{p}, *{p}, *{p}, *{p}, *{p}, *{p}, *{p}]\n"
        for p, c in zip("abcdef", "bcdefg", strict=True)
    )
    deep = "x: " + "[" * 5000 + "]" * 5000 + "\n"
    for head in (bomb, deep):
        [issue] = check_skill([(SKILL_MD, f"---\n{head}---\nbody\n")]).issues
        assert issue.code == "frontmatter_invalid" and "too large to read" in issue.message
    [issue] = check_skill([(SKILL_MD, "---\nname: [demo\n---\nbody\n")]).issues
    assert "too large" not in issue.message


@pytest.mark.parametrize(
    ("front", "code"),
    [
        ({"name": "Demo"}, "name_invalid"),
        ({"name": "a" * (NAME_MAX + 1)}, "name_invalid"),
        ({"name": "123"}, "name_invalid"),  # YAML 정수다
        ({"name": "yes"}, "name_invalid"),  # YAML 1.1 불리언이다
        ({"name": "2024-01-01"}, "name_invalid"),  # YAML 날짜다
        ({"name": None}, "name_invalid"),
        ({"name": '"demo\\n"'}, "name_invalid"),  # 끝 줄바꿈 하나도 이름 규칙 밖이다
        ({"description": None}, "description_missing"),
        ({"description": '"  "'}, "description_missing"),
        ({"description": "a" * 1025}, "description_too_long"),
        ({"compatibility": "a" * 501}, "compatibility_too_long"),
        ({"license": "a" * 201}, "license_too_long"),
        ({"license": "[MIT]"}, "frontmatter_invalid"),
        ({"metadata": "x"}, "metadata_invalid"),
        ({"metadata": "{a: 1}"}, "metadata_invalid"),
        ({"metadata": "{" + ", ".join(f"k{n}: v" for n in range(21)) + "}"}, "metadata_invalid"),
        ({"metadata": "{" + "k" * 65 + ": v}"}, "metadata_invalid"),
        ({"metadata": "{k: " + "v" * 257 + "}"}, "metadata_invalid"),
        ({"allowed-tools": "{a: b}"}, "allowed_tools_invalid"),
        ({"allowed-tools": "[a, 1]"}, "allowed_tools_invalid"),
        ({"allowed-tools": " ".join(f"t{n}" for n in range(51))}, "allowed_tools_invalid"),
    ],
)
def test_frontmatter_fields_rejected(front, code):
    assert _errors([(SKILL_MD, _md(**front))]) == [code]


def test_frontmatter_fields_at_their_limits():
    checked = check_skill(
        [
            (
                SKILL_MD,
                _md(
                    name="a" * NAME_MAX,
                    description="d" * 1024,
                    compatibility="c" * 500,
                    license="l" * 200,
                    metadata="{" + ", ".join(f"k{n}: v" for n in range(19)) + ", "
                    f"{'k' * 64}: {'v' * 256}" + "}",
                    **{"allowed-tools": " ".join(f"t{n}" for n in range(50))},
                ),
            )
        ]
    )
    assert not checked.errors
    assert checked.frontmatter is not None
    assert len(checked.frontmatter.allowed_tools) == 50 and len(checked.frontmatter.metadata) == 20


def test_allowed_tools_take_a_string_or_a_list():
    fm = check_skill([(SKILL_MD, _md(**{"allowed-tools": "Read, Bash(git:*)  Grep"}))]).frontmatter
    assert fm is not None and fm.allowed_tools == ["Read", "Bash(git:*)", "Grep"]
    fm = check_skill([(SKILL_MD, _md(**{"allowed-tools": "[read_file, ' grep ']"}))]).frontmatter
    assert fm is not None and fm.allowed_tools == ["read_file", "grep"]


@pytest.mark.parametrize(
    ("front", "code", "where"),
    [
        ({"description": r'"tag \U000E0049"'}, "invisible_chars", "frontmatter description"),
        ({"description": r'"bidi \u202e"'}, "invisible_chars", "frontmatter description"),
        ({"description": r'"nul \x00"'}, "control_chars", "frontmatter description"),
        ({"description": r'"esc \e"'}, "control_chars", "frontmatter description"),
        ({"description": r'"next line \N"'}, "control_chars", "frontmatter description"),
        ({"description": r'"cr \r"'}, "control_chars", "frontmatter description"),
        ({"license": r'"MIT\u200b"'}, "invisible_chars", "frontmatter license"),
        ({"compatibility": r'"\u3164"'}, "invisible_chars", "frontmatter compatibility"),
        ({"metadata": r'{owner: "\u2066x"}'}, "invisible_chars", "frontmatter metadata"),
        ({"metadata": r'{"\u202ekey": v}'}, "invisible_chars", "frontmatter metadata"),
        ({"allowed-tools": r'[read_file, "gr\u00adep"]'}, "invisible_chars", "allowed-tools"),
        ({"allowed-tools": r'"read_file \x7f"'}, "control_chars", "allowed-tools"),
        ({"extra": r'"\U000E0041"'}, "invisible_chars", "in frontmatter,"),  # 모르는 키의 값
        ({r'"\u202e"': "x"}, "invisible_chars", "in frontmatter,"),  # 모르는 키의 이름
    ],
)
def test_yaml_escapes_are_checked_after_decoding(front, code, where):
    """원문 검사는 이스케이프 전의 글을 보므로 큰따옴표 안의 `\\U000E0049` 는 거기서 걸리지 않는다.
    풀린 매핑의 모든 문자열을 같은 규칙으로 다시 보고, 걸리면 머리말을 돌려주지 않는다. 문장에
    사용자가 지은 키 이름을 옮겨 적지 않는다."""
    checked = check_skill([(SKILL_MD, _md(**front))])
    [issue] = checked.errors
    assert (issue.path, issue.code) == (SKILL_MD, code)
    assert where in issue.message and "written as a YAML escape" in issue.message
    assert checked.frontmatter is None and checked.digest is None


def test_escaped_emoji_and_raw_characters_are_reported_once():
    """이스케이프로 적은 이모지(⚠️)는 원문 이모지와 같이 받는다. 원문에 이미 걸린 코드는 다시 내지
    않는다."""
    assert _ok([(SKILL_MD, _md(description=r'"\u26a0\ufe0f careful"'))])
    raw = _md("body\u200b\n", description=r'"\u202e"')
    assert [(i.code, "line 5" in i.message) for i in check_skill([(SKILL_MD, raw)]).issues] == [
        ("invisible_chars", True)
    ]
    assert check_skill([(SKILL_MD, raw)]).frontmatter is None


def test_name_must_match_the_resource():
    assert _errors([(SKILL_MD, _md())], name="other") == ["name_mismatch"]
    assert _ok([(SKILL_MD, _md())], name="demo")


def test_unknown_keys_and_scripts_warn_but_pass():
    checked = check_skill(
        [
            (SKILL_MD, _md(version="2", owner="me")),
            ("scripts/run.txt", "x"),
            ("tool.sh", "echo"),
            ("notes.md", "x"),
        ]
    )
    assert not checked.errors and checked.digest is not None
    assert [(i.path, i.code, i.severity) for i in checked.issues] == [
        (SKILL_MD, "unknown_frontmatter", "warning"),
        ("scripts/run.txt", "skill_scripts_inert", "warning"),
        ("tool.sh", "skill_scripts_inert", "warning"),
    ]
    assert "owner, version" in checked.issues[0].message
    assert is_script("a.py") and is_script("scripts/x") and not is_script("docs/scripts.md")


def test_errors_carry_every_issue_and_no_digest():
    checked = check_skill([(SKILL_MD, _md(name="Bad", description=None)), ("../x", "a\u200b")])
    assert sorted(i.code for i in checked.errors) == [
        "description_missing",
        "invisible_chars",
        "name_invalid",
        "path_invalid",
    ]
    assert checked.frontmatter is None and checked.digest is None
    with pytest.raises(InvalidSkill) as e:
        checked.valid()
    assert e.value.status == 422 and e.value.code == "invalid_skill"
    assert len(e.value.extra["issues"]) == 4
    assert all(i["severity"] == "error" for i in e.value.extra["issues"])


# -- digest·이름 바꾸기 ----------------------------------------------------------------


def test_digest_rule():
    """경로 순으로 `경로 NUL sha256(내용) LF` 를 이은 바이트의 sha256 이다(`$defs/skill_digest`)."""
    import hashlib

    files = [("b.md", "two"), ("SKILL.md", "one")]
    body = b"".join(
        f"{p}\0{hashlib.sha256(c.encode()).hexdigest()}\n".encode() for p, c in sorted(files)
    )
    assert skill_digest(files) == f"sha256:{hashlib.sha256(body).hexdigest()}"
    assert skill_digest(reversed(files)) == skill_digest(files)


def test_fixtures_pass_and_keep_their_digests():
    """플랫폼과 런타임이 같은 고정물로 같은 digest 를 내는지 보는 기준값이다."""
    expected = json.loads((FIXTURES / "digests.json").read_text(encoding="utf-8"))
    got = {}
    for folder in sorted(p for p in FIXTURES.iterdir() if p.is_dir()):
        files = [
            (f.relative_to(folder).as_posix(), f.read_bytes())
            for f in sorted(folder.rglob("*"))
            if f.is_file()
        ]
        checked = check_skill(files, name=folder.name)
        assert not checked.errors, (folder.name, checked.errors)
        got[folder.name] = checked.digest
    assert got == expected


@pytest.mark.parametrize(("name", "line"), [("fresh", "name: fresh"), ("123", 'name: "123"')])
def test_renamed_rewrites_the_name_line(name, line):
    text = renamed(_md(description="keeps"), name)
    assert line in text and "description: keeps" in text
    assert _ok([(SKILL_MD, text)], name=name)


def test_renamed_leaves_text_without_frontmatter():
    assert renamed("# none\n", "x") == "# none\n"


def test_renamed_rewrites_the_last_of_duplicate_name_keys():
    """YAML 은 중복 키의 마지막 값을 읽는다. 첫 줄을 바꾸면 복제본의 머리말 name 이 원본 이름으로
    남아 `name_mismatch` 로 막힌다."""
    text = "---\nname: first\ndescription: d\nname: demo\n---\nbody\n"
    out = renamed(text, "fresh")
    assert out == "---\nname: first\ndescription: d\nname: fresh\n---\nbody\n"
    assert _ok([(SKILL_MD, out)], name="fresh")


# -- 가져오기 ------------------------------------------------------------------------


def test_zip_strips_one_top_folder_and_skips_os_litter():
    data = zip_of(
        {
            "demo/SKILL.md": _md(),
            "demo/references/a.md": "a",
            "demo/.DS_Store": b"\0",
            "__MACOSX/demo/._SKILL.md": b"\0",
        }
    )
    upload = read_upload(data)
    assert sorted(p for p, _ in upload.files) == ["SKILL.md", "references/a.md"]
    assert sorted(upload.skipped) == ["__MACOSX/demo/._SKILL.md", "demo/.DS_Store"]
    assert _ok(upload.files)


def test_zip_without_a_common_folder_keeps_paths():
    upload = read_upload(zip_of({"SKILL.md": _md(), "references/a.md": "a"}))
    assert sorted(p for p, _ in upload.files) == ["SKILL.md", "references/a.md"]
