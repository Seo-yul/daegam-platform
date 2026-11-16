"""Skill 형식 검사 (D64, P39 1절). 순수 함수라 표로 테스트한다.

받는 것은 Agent Skills 규격(agentskills.io)의 부분 집합이고 deepagents 의 `SkillsMiddleware` 가 받는
것의 부분 집합이다. 같은 고정물(`agent-runtime/schema/fixtures/skills/`)을 런타임 테스트가
`SkillsMiddleware` 로 읽어 모두 목록에 오르는지 본다. 경로 규칙의 원본은 agent.schema.json 의
`$defs/skill_path` 하나이고 런타임 init 도 같은 패턴을 읽는다.

오류는 한 번에 모두 모아 422 `invalid_skill` 의 `issues` 로 싣는다. 경고(스크립트, 모르는 머리말
키)는 저장을 막지 않고 검사 응답에만 실린다. 보이지 않는 글자는 모든 파일에서 거절한다. 검토
화면에 보이지 않는 글이 모델에게 가는 길을 막으려는 것이고, 흔한 이모지(⚠️·✔️·👨‍💻·1️⃣)에 붙는
변형 선택자·ZWJ·키캡만 예외다. 머리말은 YAML 이 이스케이프를 푼 값도 같은 규칙으로 본다.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any, Literal

import yaml

from daegam.contracts import agent_schema
from daegam.errors import ServiceError
from daegam.names import NAME_RULE, is_valid_name
from daegam.services.emoji_ranges import is_extended_pictographic
from daegam.store.skills import SkillFile, SkillFrontmatter
from daegam.units import KIB, MIB
from daegam.yamlsafe import YAMLTooLarge, safe_load
from daegam.zipsafe import ArchiveError, read_zip

SKILL_MD = "SKILL.md"
MAX_FILES = 32
MAX_SKILL_MD_BYTES = 64 * KIB
MAX_FILE_BYTES = 256 * KIB
MAX_REVISION_BYTES = 512 * KIB
MAX_DESCRIPTION = 1024
MAX_COMPATIBILITY = 500
MAX_LICENSE = 200
MAX_METADATA_KEYS = 20
MAX_METADATA_KEY = 64
MAX_METADATA_VALUE = 256
MAX_ALLOWED_TOOLS = 50

# zip 가져오기. 본문 상한은 라우터가 413 too_large 로 본다. 항목 수는 파일 상한보다 넉넉해
# 디렉터리와 건너뛰는 항목이 들어갈 자리가 있다
MAX_IMPORT_BYTES = 2 * MIB
ZIP_LIMITS: dict[str, Any] = {
    "max_entries": 64,
    "max_total": MAX_REVISION_BYTES,
    "max_entry": MAX_FILE_BYTES,
    "max_ratio": 100,
}
# zip 의 시작 바이트(항목이 있는 것과 빈 것)
ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")

# deepagents 와 같은 머리말 정규식이다(deepagents/middleware/skills.py:401). 닫는 줄이 없는 글에서는
# 제곱 시간이 걸리므로 `_frontmatter_match` 가 줄 단위로 울타리를 먼저 찾은 뒤에만 돌린다
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
# 머리말 울타리 한 줄(`---` 와 뒤의 공백)
_FENCE = re.compile(r"---\s*")
KNOWN_KEYS = ("name", "description", "license", "compatibility", "metadata", "allowed-tools")
# 머리말의 name 줄. 복제가 새 이름으로 바꿔 쓴다
_NAME_LINE = re.compile(r"^name[ \t]*:.*$", re.MULTILINE)

# 스크립트로 보는 파일. 받되 돌지 않는 글이다(P40 전)
SCRIPT_DIR = "scripts/"
SCRIPT_SUFFIXES = (".py", ".sh", ".js", ".ts")

# 탭·줄바꿈 밖의 제어 문자. C0, DEL, C1 이다. CR 은 줄 끝을 LF 로 맞추는 단계에서 이미 줄바꿈이 됐다
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# 한글 채움 문자. 글자로 분류되지만 화면에 아무것도 그리지 않는다
_HANGUL_FILLERS = "\u115f\u1160\u3164\uffa0"


def _char_class(codes: Iterable[int]) -> str:
    """코드 포인트를 정규식 문자 집합으로 적는다. 이어진 것은 범위 하나로 묶는다."""
    spans: list[list[int]] = []
    for c in sorted(set(codes)):
        if spans and spans[-1][1] == c - 1:
            spans[-1][1] = c
        else:
            spans.append([c, c])
    return "[" + "".join(f"\\U{a:08x}-\\U{b:08x}" for a, b in spans) + "]"


# 보이지 않는 글자의 후보. 서식 문자(유니코드 범주 Cf: 폭 없는 문자, bidi 재정렬, 태그, BOM, 소프트
# 하이픈 등) 전부와 변형 선택자(보충 포함), 태그 블록, 한글 채움 문자다. 이모지에 붙은 변형 선택자와
# ZWJ 만 받는다(`_emoji_part`). 범주는 이 파이썬의 유니코드 판을 따른다
_INVISIBLE = re.compile(
    _char_class(
        [
            *(c for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c)) == "Cf"),
            *range(0xFE00, 0xFE10),
            *range(0xE0000, 0xE0080),
            *range(0xE0100, 0xE01F0),
            *map(ord, _HANGUL_FILLERS),
        ]
    )
)
ZWJ = "\u200d"
VS15, VS16 = "\ufe0e", "\ufe0f"
KEYCAP = "\u20e3"
KEYCAP_BASES = frozenset("0123456789#*")
# 피부색 수식자. ZWJ 앞의 이모지를 찾을 때 건너뛴다(👩🏽‍💻)
_SKIN_TONES = range(0x1F3FB, 0x1F400)

Severity = Literal["error", "warning"]
# 검사에 넘기는 파일 (경로, 내용). 내용은 JSON 으로 온 글이거나 가져온 바이트다
RawFiles = Sequence[tuple[str, str | bytes]]


@dataclass(frozen=True)
class SkillIssue:
    path: str | None
    code: str
    message: str
    severity: Severity = "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }


class InvalidSkill(ServiceError):
    """형식 검사에 걸렸다. 오류를 모두 `issues` 에 싣는다."""

    def __init__(self, issues: Sequence[SkillIssue]):
        super().__init__(
            422,
            "invalid_skill",
            "the skill files are not valid; see issues",
            issues=[i.to_dict() for i in issues],
        )


@dataclass(frozen=True)
class ValidSkill:
    """오류 없이 검사를 지난 파일. 리비전 문서가 이것으로 만들어진다."""

    files: tuple[SkillFile, ...]
    frontmatter: SkillFrontmatter
    digest: str
    bytes: int


@dataclass(frozen=True)
class CheckedSkill:
    """검사 결과. 파일은 경로 순이고 글은 저장할 모양으로 맞춘 것이다."""

    files: tuple[SkillFile, ...]
    frontmatter: SkillFrontmatter | None
    issues: tuple[SkillIssue, ...]

    @property
    def errors(self) -> list[SkillIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def bytes(self) -> int:
        return sum(f.bytes for f in self.files)

    @property
    def digest(self) -> str | None:
        """오류가 있으면 None 이다. 저장할 수 없는 글의 digest 는 뜻이 없다."""
        if self.errors:
            return None
        return skill_digest((f.path, f.content) for f in self.files)

    def valid(self) -> ValidSkill:
        """오류가 있으면 InvalidSkill 이다. 머리말이 없으면 늘 오류도 있다(SKILL.md 가 없거나 글이
        아니거나 머리말이 틀렸다)."""
        if self.errors or self.frontmatter is None or self.digest is None:
            raise InvalidSkill(self.errors)
        return ValidSkill(self.files, self.frontmatter, self.digest, self.bytes)


@dataclass(frozen=True)
class Upload:
    """가져온 파일(경로, 바이트)과 건너뛴 zip 항목."""

    files: list[tuple[str, bytes]]
    skipped: tuple[str, ...]


# -- 공개 함수 -----------------------------------------------------------------------


def check_skill(raw: RawFiles, *, name: str | None = None) -> CheckedSkill:
    """파일 (경로, 내용) 을 검사한다. name 이 있으면 머리말 name 이 그것과 같아야 한다(새 리비전).
    없으면 머리말 name 이 리소스 이름이 된다(생성)."""
    issues: list[SkillIssue] = []
    if len(raw) > MAX_FILES:
        issues.append(_err(None, "too_many_files", f"a skill holds at most {MAX_FILES} files"))
    _check_paths([path for path, _ in raw], issues)
    files = [f for path, content in raw if (f := _checked_file(path, content, issues)) is not None]
    total = sum(f.bytes for f in files)
    if total > MAX_REVISION_BYTES:
        issues.append(
            _err(
                None,
                "revision_too_large",
                f"files add up to {total} bytes; limit is {MAX_REVISION_BYTES // KIB} KiB",
            )
        )
    skill_md = next((f for f in files if f.path == SKILL_MD), None)
    # 상한을 넘은 SKILL.md 는 이미 file_too_large 라 머리말을 읽지 않는다
    readable = skill_md is not None and skill_md.bytes <= MAX_SKILL_MD_BYTES
    frontmatter = _frontmatter(skill_md.content, name, issues) if readable else None
    issues.extend(
        _warn(f.path, "skill_scripts_inert", "scripts do not run; the agent reads them as text")
        for f in files
        if is_script(f.path)
    )
    return CheckedSkill(
        files=tuple(sorted(files, key=lambda f: f.path)),
        frontmatter=frontmatter,
        issues=tuple(issues),
    )


def skill_digest(files: Iterable[tuple[str, str]]) -> str:
    """경로 순으로 `경로 NUL sha256(내용) LF` 를 이은 바이트의 sha256 이다. 규칙의 원본은
    agent.schema.json 의 `$defs/skill_digest` 설명이다."""
    h = hashlib.sha256()
    for path, content in sorted(files):
        h.update(f"{path}\0{_sha256(content)}\n".encode())
    return f"sha256:{h.hexdigest()}"


def is_script(path: str) -> bool:
    return path.startswith(SCRIPT_DIR) or path.endswith(SCRIPT_SUFFIXES)


def read_upload(data: bytes) -> Upload:
    """가져오기 본문을 파일로 푼다. zip 이 아니면 SKILL.md 하나다. zip 은 `read_zip` 으로 풀고
    `__MACOSX/`·`.DS_Store` 를 건너뛰며, 모든 항목이 한 폴더 아래 있으면 그 폴더를 벗긴다.
    zip 오류(`archive_*`)는 `invalid_skill` 의 issue 로 옮긴다."""
    if not data.startswith(ZIP_MAGIC):
        return Upload(files=[(SKILL_MD, data)], skipped=())
    try:
        contents = read_zip(data, **ZIP_LIMITS, skip=_os_litter)
    except ArchiveError as e:
        raise InvalidSkill([_err(None, e.code, e.detail)]) from e
    return Upload(files=_strip_folder(contents.files), skipped=contents.skipped)


def renamed(text: str, name: str) -> str:
    """SKILL.md 머리말의 name 줄을 name 으로 바꾼다. 복제가 새 이름의 Skill 을 만들 때 쓴다.
    name 키가 여럿이면 YAML 이 읽는 마지막 것을 바꾼다. name 줄이 없거나 다른 꼴이면 그대로 두고,
    그 결과는 검사가 `name_mismatch` 로 거절한다."""
    match = _frontmatter_match(text)
    if match is None:
        return text
    lines = list(_NAME_LINE.finditer(match.group(1)))
    if not lines:
        return text
    # 이름 규칙 안에도 YAML 이 글이 아닌 값으로 읽는 것(`123`, `true`)이 있어 그때만 따옴표를 친다
    value = name if safe_load(name) == name else json.dumps(name)
    start, end = match.start(1) + lines[-1].start(), match.start(1) + lines[-1].end()
    return f"{text[:start]}name: {value}{text[end:]}"


# -- 경로 ----------------------------------------------------------------------------


@cache
def _path_rule() -> tuple[re.Pattern[str], int]:
    """agent.schema.json 의 `$defs/skill_path` 에서 읽은 (패턴, 최대 길이)."""
    rule = agent_schema()["$defs"]["skill_path"]
    return re.compile(rule["pattern"]), int(rule["maxLength"])


def _check_paths(paths: list[str], issues: list[SkillIssue]) -> None:
    """꼴, 대소문자만 다른 중복, 파일이면서 폴더인 경로, 뿌리의 SKILL.md."""
    pattern, max_len = _path_rule()
    seen: dict[str, str] = {}
    for path in paths:
        if len(path) > max_len or pattern.fullmatch(path) is None:
            issues.append(_err(path, "path_invalid", "use a relative path like references/a.md"))
        elif path.lower() in seen:
            issues.append(_err(path, "path_duplicate", f"same path as {seen[path.lower()]}"))
        else:
            seen[path.lower()] = path
    folders = {key[:i] for key in seen for i, ch in enumerate(key) if ch == "/"}
    issues.extend(
        _err(path, "path_duplicate", "a file and a folder have the same path")
        for key, path in seen.items()
        if key in folders
    )
    if SKILL_MD not in paths:
        issues.append(_err(None, "skill_md_missing", "put SKILL.md at the root of the skill"))


# -- 내용 ----------------------------------------------------------------------------


def _checked_file(path: str, content: str | bytes, issues: list[SkillIssue]) -> SkillFile | None:
    """글로 읽고 맞춘 뒤 글자와 크기를 본다. 글이 아니면 None 이다."""
    text = _text(content)
    if text is None:
        issues.append(_err(path, "not_text", "files must be UTF-8 text"))
        return None
    if (m := _CONTROL.search(text)) is not None:
        issues.append(_err(path, "control_chars", f"control character {_at(text, m.start())}"))
    if (i := _invisible_at(text)) is not None:
        issues.append(_err(path, "invisible_chars", f"invisible character {_at(text, i)}"))
    size = len(text.encode())
    limit = MAX_SKILL_MD_BYTES if path == SKILL_MD else MAX_FILE_BYTES
    if size > limit:
        issues.append(_err(path, "file_too_large", f"{size} bytes; limit is {limit // KIB} KiB"))
    return SkillFile(path=path, content=text, bytes=size, sha256=_sha256(text))


def _text(content: str | bytes) -> str | None:
    """엄격한 UTF-8 로 읽고 첫 BOM 을 떼며 줄 끝을 LF 로 맞춘다. 글이 아니면 None 이다."""
    try:
        text = content.decode() if isinstance(content, bytes) else content
        text.encode()  # JSON 으로 온 짝 없는 서로게이트를 거른다
    except UnicodeError:
        return None
    return text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def _invisible_at(text: str) -> int | None:
    """거절할 보이지 않는 글자의 첫 위치. 이모지에 붙은 변형 선택자·ZWJ·키캡은 받는다."""
    for m in _INVISIBLE.finditer(text):
        i = m.start()
        if not _emoji_part(text, i):
            return i
    return None


def _emoji_part(text: str, i: int) -> bool:
    """i 의 글자가 이모지 표기의 일부인가. 이모지 바로 뒤의 VS15·VS16, 키캡(`0-9#*` VS16 U+20E3)의
    VS16, 두 이모지 사이의 ZWJ 다."""
    ch = text[i]
    prev = text[i - 1] if i > 0 else ""
    after = text[i + 1] if i + 1 < len(text) else ""
    if ch in (VS15, VS16) and prev and is_extended_pictographic(prev):
        return True
    if ch == VS16 and prev in KEYCAP_BASES and after == KEYCAP:
        return True
    if ch == ZWJ and after and is_extended_pictographic(after):
        base = _emoji_before(text, i)
        return base is not None and is_extended_pictographic(base)
    return False


def _emoji_before(text: str, i: int) -> str | None:
    """ZWJ 앞의 바탕 글자. 그 사이의 VS16 과 피부색 수식자를 건너뛴다."""
    j = i - 1
    while j >= 0 and (text[j] == VS16 or ord(text[j]) in _SKIN_TONES):
        j -= 1
    return text[j] if j >= 0 else None


def _at(text: str, i: int) -> str:
    return f"U+{ord(text[i]):04X} at line {text.count(chr(10), 0, i) + 1}"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# -- 머리말 --------------------------------------------------------------------------


def _frontmatter_match(text: str) -> re.Match[str] | None:
    """deepagents 의 정규식으로 머리말을 뗀다. 첫 줄이 울타리이고 그 뒤에 줄바꿈으로 끝나는 울타리
    줄이 있을 때만 정규식을 돌린다. 그런 줄이 없으면 정규식도 맞지 않으므로 결과는 같고, 있으면
    정규식이 처음 몇 번의 시도에서 맞아 글 길이에 비례한 시간에 끝난다."""
    lines = text.split("\n")
    if _FENCE.fullmatch(lines[0]) is None:
        return None
    if not any(_FENCE.fullmatch(line) for line in lines[1:-1]):
        return None
    return FRONTMATTER_RE.match(text)


def _frontmatter(text: str, name: str | None, issues: list[SkillIssue]) -> SkillFrontmatter | None:
    """머리말을 떼어 YAML 매핑으로 읽고 키마다 본다. 오류가 하나라도 있으면 None 이다."""
    match = _frontmatter_match(text)
    if match is None:
        issues.append(_err(SKILL_MD, "frontmatter_missing", "start SKILL.md with --- frontmatter"))
        return None
    try:
        data = safe_load(match.group(1))
    except YAMLTooLarge as e:
        issues.append(
            _err(SKILL_MD, "frontmatter_invalid", f"frontmatter is too large to read: {e}")
        )
        return None
    except yaml.YAMLError:
        data = None
    if not isinstance(data, dict):
        issues.append(_err(SKILL_MD, "frontmatter_invalid", "frontmatter must be a YAML mapping"))
        return None
    escaped = _escaped_chars(data)
    # 원문 검사가 같은 코드를 냈으면 그 글자가 원문에 있다는 뜻이라 다시 내지 않는다. 그래도 그
    # 머리말은 돌려주지 않는다
    reported = {i.code for i in issues if i.path == SKILL_MD}
    found = [i for i in escaped if i.code not in reported]
    unknown = sorted(str(k) for k in data if k not in KNOWN_KEYS)
    if unknown:
        found.append(_warn(SKILL_MD, "unknown_frontmatter", f"unknown keys: {', '.join(unknown)}"))
    fields = {key: check(data.get(raw), found) for key, raw, check in _FIELDS}
    fields["name"] = _name(data.get("name"), name, found)
    issues.extend(found)
    if escaped or any(i.severity == "error" for i in found):
        return None
    return SkillFrontmatter.model_validate(fields)


def _escaped_chars(data: dict[Any, Any]) -> list[SkillIssue]:
    """YAML 이 푼 글자를 다시 본다. 파일 글자 검사(`_checked_file`)는 이스케이프를 풀기 전의 원문을
    보므로 큰따옴표 안의 `\\U000E0049`·`\\u202e`·`\\x00` 은 거기서 걸리지 않고 태그·bidi·NUL 로
    풀려 모델 프롬프트에 간다. 풀린 매핑의 모든 문자열(키, 값, metadata, allowed-tools 목록)을 같은
    규칙으로 보고 SKILL.md 의 `control_chars`·`invisible_chars` 를 코드마다 처음 걸린 것 하나씩
    낸다."""
    out: dict[str, SkillIssue] = {}
    for where, text in _strings(data):
        if "control_chars" not in out and (m := _CONTROL.search(text)) is not None:
            message = f"control character {_escaped_at(text, m.start(), where)}"
            out["control_chars"] = _err(SKILL_MD, "control_chars", message)
        if "invisible_chars" not in out and (i := _invisible_at(text)) is not None:
            message = f"invisible character {_escaped_at(text, i, where)}"
            out["invisible_chars"] = _err(SKILL_MD, "invisible_chars", message)
    return list(out.values())


def _strings(data: dict[Any, Any]) -> Iterator[tuple[str, str]]:
    """머리말 매핑 안의 모든 문자열과 그것이 든 최상위 키. alias 로 같은 객체를 여러 번 가리켜도
    한 번만 본다. 사용자가 지은 키 이름은 자리 표시에 쓰지 않는다(그 이름에 걸린 글자가 있을 수
    있다)."""
    seen: set[int] = set()
    for key, value in data.items():
        where = f"frontmatter {key}" if key in KNOWN_KEYS else "frontmatter"
        stack: list[Any] = [key, value]
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            if isinstance(node, str):
                seen.add(id(node))
                yield where, node
            elif isinstance(node, dict):
                seen.add(id(node))
                stack.extend([*node.keys(), *node.values()])
            elif isinstance(node, list | tuple | set):
                seen.add(id(node))
                stack.extend(node)


def _escaped_at(text: str, i: int, where: str) -> str:
    return f"U+{ord(text[i]):04X} in {where}, written as a YAML escape"


def _name(value: Any, expected: str | None, issues: list[SkillIssue]) -> str | None:
    if value is not None and not isinstance(value, str):
        # `2024`·`yes` 처럼 규칙에 맞는 이름도 YAML 은 글이 아닌 값으로 읽는다
        message = f"YAML reads name as {type(value).__name__}, not text; put the name in quotes"
        issues.append(_err(SKILL_MD, "name_invalid", message))
        return None
    if value is None or not is_valid_name(value):
        issues.append(_err(SKILL_MD, "name_invalid", NAME_RULE))
        return None
    if expected is not None and value != expected:
        issues.append(_err(SKILL_MD, "name_mismatch", f"name must stay {expected!r}"))
    return value


def _description(value: Any, issues: list[SkillIssue]) -> str | None:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        issues.append(_err(SKILL_MD, "description_missing", "write a description"))
    elif len(text) > MAX_DESCRIPTION:
        issues.append(_err(SKILL_MD, "description_too_long", f"at most {MAX_DESCRIPTION} chars"))
    return text


def _short_text(
    key: str, limit: int, too_long: str
) -> Callable[[Any, list[SkillIssue]], str | None]:
    """선택 문자열 키(license·compatibility)의 검사. too_long 은 상한을 넘었을 때의 코드이고 화면의
    문장 표가 글자 그대로 찾으므로 조립하지 않고 받는다."""

    def check(value: Any, issues: list[SkillIssue]) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            issues.append(_err(SKILL_MD, "frontmatter_invalid", f"{key} must be a string"))
            return None
        text = value.strip()
        if len(text) > limit:
            issues.append(_err(SKILL_MD, too_long, f"{key} is at most {limit} chars"))
        return text or None

    return check


def _metadata(value: Any, issues: list[SkillIssue]) -> dict[str, str]:
    if value is None:
        return {}
    ok = (
        isinstance(value, dict)
        and len(value) <= MAX_METADATA_KEYS
        and all(
            isinstance(k, str)
            and isinstance(v, str)
            and len(k) <= MAX_METADATA_KEY
            and len(v) <= MAX_METADATA_VALUE
            for k, v in value.items()
        )
    )
    if not ok:
        issues.append(
            _err(
                SKILL_MD,
                "metadata_invalid",
                f"metadata maps up to {MAX_METADATA_KEYS} string keys to string values; quote "
                "numbers",
            )
        )
        return {}
    return dict(value)


def _allowed_tools(value: Any, issues: list[SkillIssue]) -> list[str]:
    """deepagents 처럼 공백·쉼표로 나눈 문자열이나 문자열 목록이다. 표시뿐이고 권한이 아니다."""
    if value is None:
        return []
    if isinstance(value, str):
        tools = [t for t in re.split(r"[\s,]+", value) if t]
    elif isinstance(value, list) and all(isinstance(t, str) for t in value):
        tools = [t.strip() for t in value if t.strip()]
    else:
        tools = None
    if tools is None or len(tools) > MAX_ALLOWED_TOOLS:
        issues.append(
            _err(
                SKILL_MD,
                "allowed_tools_invalid",
                f"allowed-tools is a list or a space-separated string of up to "
                f"{MAX_ALLOWED_TOOLS} names",
            )
        )
        return []
    return tools


# (모델 필드, 머리말 키, 검사). name 은 기대 이름을 함께 받아 따로 본다
_FIELDS: tuple[tuple[str, str, Callable[[Any, list[SkillIssue]], Any]], ...] = (
    ("description", "description", _description),
    ("license", "license", _short_text("license", MAX_LICENSE, "license_too_long")),
    (
        "compatibility",
        "compatibility",
        _short_text("compatibility", MAX_COMPATIBILITY, "compatibility_too_long"),
    ),
    ("metadata", "metadata", _metadata),
    ("allowed_tools", "allowed-tools", _allowed_tools),
)


# -- zip -----------------------------------------------------------------------------


def _os_litter(name: str) -> bool:
    """운영체제가 zip 에 남기는 것. 건너뛰고 응답의 `skipped` 에 적는다."""
    return name.startswith("__MACOSX/") or name.rsplit("/", 1)[-1] == ".DS_Store"


def _strip_folder(files: dict[str, bytes]) -> list[tuple[str, bytes]]:
    """모든 항목이 한 최상위 폴더 아래 있으면 그 폴더를 벗긴다."""
    tops = {name.split("/", 1)[0] for name in files}
    if len(tops) != 1 or not all("/" in name for name in files):
        return list(files.items())
    prefix = f"{tops.pop()}/"
    return [(name.removeprefix(prefix), body) for name, body in files.items()]


def _err(path: str | None, code: str, message: str) -> SkillIssue:
    return SkillIssue(path, code, message)


def _warn(path: str | None, code: str, message: str) -> SkillIssue:
    return SkillIssue(path, code, message, "warning")


__all__ = [
    "MAX_FILES",
    "MAX_IMPORT_BYTES",
    "MAX_REVISION_BYTES",
    "SKILL_MD",
    "CheckedSkill",
    "InvalidSkill",
    "RawFiles",
    "SkillIssue",
    "Upload",
    "ValidSkill",
    "check_skill",
    "is_script",
    "read_upload",
    "renamed",
    "skill_digest",
]
