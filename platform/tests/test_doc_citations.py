"""현재 상태 문서의 코드 인용, 상대 링크, 앵커가 실재하는지 보고 이 저장소의 줄 인용을 거절한다.

문서가 코드 위치를 정확히 짚는 것이 이 저장소의 규칙인데, 리팩터링 때마다 조용히 썩는다. 여기서
잡는 것은 기계적으로 판정할 수 있는 것뿐이다.

- `경로` `심볼`: 파일이 있고 그 안에 그 이름이 있다. 파이썬은 AST 의 이름이고 메서드는 맨 이름과
  `Class.method` 를 둘 다 받는다. TS·TSX 는 선언한 이름, YAML 은 키, Makefile 은 타깃·변수·define
  이다. `경로` `A`·`B`, `C` and `D` 처럼 이어 적은 심볼도 모두 본다. 다른 저장소의 코드는 검사가
  찾지 못하므로 이 모양으로 붙여 적지 않는다.
- `경로:줄`, `경로:줄-줄`, 링크의 `경로#L줄`: 이 저장소의 파일이면 거절한다. 줄 번호는 커밋마다
  밀리므로 심볼로 적는다. 경로의 맨 앞 디렉터리가 저장소의 것(`platform/`, `ui/` 등)이면 지운
  파일이어도 이 저장소의 것이고, 그 경로로 끝나는 저장소 파일이 있어도 확장자와 상관없이 그렇다
  (`services/agents.py`, `agents.py`, `nginx.conf`). 검토 기록과 조사 노트가 고정한 판의
  줄로 적는 상류 라이브러리 근거는 패키지나 저장소 뿌리부터 적으므로(`deepagents/graph.py`,
  `pkg/auth/token.go`) 어느 쪽에도 걸리지 않아 통과한다. 맨 파일 이름이 저장소의 파일과 겹치면
  상류 근거도 걸리므로 그때는 경로를 더 적는다.
- 상대 링크와 `#앵커`: 가리키는 파일이 있고, 마크다운이면 그 제목이나 `<a id>` 가 있다.

심볼·링크 검사는 docs/guide, docs/design, docs 밖의 README, 루트 CLAUDE.md 와 docs/plan 바로 아래의
진행 중인 계획을 본다. 줄 인용 거절은 여기에 진행 중인 계획의 검토 기록(docs/plan/reviews/)과 조사
노트 (docs/research/)를 더한다. 이 둘은 날짜가 박힌 기록이라 지운 파일을 가리켜도 정상이어서
심볼·링크 검사에서는 뺀다. docs/archive/ 의 끝난 단계 설계·검토 기록은 어느 검사도 보지 않는다. 코드
블록 안은 인용이 아니라 예시라 보지 않는다.
"""

from __future__ import annotations

import ast
import functools
import os
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote

import pytest

from tests.helpers.consts import REPO
from tests.helpers.markdown import HEADING
from tests.helpers.repo import repo_files

# 공개 저장소로 내보내는 트리에는 docs/ 가 없다(P82). 문서를 읽는 검사는 문서가 있는
# 저장소에서만 돈다
pytestmark = pytest.mark.skipif(
    not (REPO / "docs").is_dir(), reason="docs/ 가 없는 트리다 (공개 내보내기)"
)

# 보는 문서(저장소 기준 경로). docs/plan 바로 아래에는 진행 중인 계획만 있고, 끝난 단계 설계와
# 그 검토 기록은 docs/archive/ 에 있어 어느 패턴에도 걸리지 않는다. 루트 CLAUDE.md 도 코드 위치를
# 짚는 현재 상태 문서라 같이 본다
CURRENT_DOCS = re.compile(r"docs/(?:guide|design)/.+\.md|(?!docs/)(?:.+/)?README\.md|CLAUDE\.md")
PLANS = re.compile(r"docs/plan/[^/]+\.md")
# 줄 인용 거절만 보는 기록: 진행 중인 계획의 검토 기록과 조사 노트
RECORDS = re.compile(r"docs/(?:plan/reviews|research)/.+\.md")
FENCE = re.compile(r"^\s*(?:```|~~~)")

SKIP_DIRS = {".git", "node_modules", ".venv", "ui_dist", ".claude", "__pycache__", "dist"}
SUFFIXES = {".py", ".ts", ".tsx", ".sh", ".yaml", ".json"}
# 줄 인용: `경로:줄`, `경로:줄-줄` 과 링크의 `경로#L줄`. 백틱 밖에 적어도 잡는다. 경로는 확장자가
# 있는 파일이나 Makefile·Dockerfile 이다
LINE_CITATION = re.compile(
    r"(?:\.\.?/)*"  # 링크 앞머리의 ../ 는 떼고 읽는다
    r"((?:[\w.-]+/)*(?:[\w-][\w.-]*\.[A-Za-z]\w*|Makefile|Dockerfile))"
    r"(?::|#L)\d+(?:-L?\d+)?"
)
# 문서가 짧게 적는 경로를 풀 때 붙여 보는 접두어
PREFIXES = (
    "",
    "platform/daegam/",
    "platform/",
    "agent-runtime/",
    "mcp-runtime/",
    "ui/",
    "deploy/",
    "charts/",
)


# 인용이 가리킬 수 있는 소스 뿌리. 저장소 전체를 훑으면 node_modules 때문에 느리다
SOURCE_ROOTS = (
    "platform/daegam",
    "platform/tests",
    "agent-runtime/runtime",
    "agent-runtime/schema",
    "mcp-runtime/runtime",
    "ui/src",
    "ui/e2e",
    "deploy",
    "charts",
    "scripts",
)

# 백틱 경로 뒤의 백틱 토큰이 심볼이다(`경로` `Class.method`). `·`, `,`, and 로 이어 적은 토큰도 같은
# 파일의 심볼로 읽고, 줄이 바뀌어도 이어 본다. 이어진 토큰은 lookahead 로만 읽으므로 그 가운데
# 경로는 다음 인용의 시작으로 다시 맞는다
CODE_PATH = r"[\w./-]+\.(?:py|ts|tsx|ya?ml)|(?:[\w./-]+/)?Makefile"
TOKEN = r"`[^`\n]+`"
CHAIN_SEPARATOR = r"\s*[·,]\s*(?:and\s+)?|\s+and\s+"
SYMBOL_CITATION = re.compile(rf"`({CODE_PATH})`\s+(?=({TOKEN}(?:(?:{CHAIN_SEPARATOR}){TOKEN})*))")
# 심볼 모양. 백틱 토큰이 명령·값이거나 다음 인용의 경로(`a.py` `b.py`)면 심볼 인용이 아니다
SYMBOL = re.compile(rf"(?!(?:{CODE_PATH})$)[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*")
# 심볼에 붙여 쓴 호출 괄호: `provider()`, `run_once(now=)`
CALL = re.compile(r"\(.*\)$")
DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
# 파이썬 밖 파일에서 인용할 수 있는 이름. Makefile 타깃은 한 줄에 여럿일 수 있어 공백으로 나눈다
TS_DECLARATION = re.compile(
    r"\b(?:function\*?|class|interface|type|enum|const|let|var|namespace)\s+([A-Za-z_$][\w$]*)"
)
YAML_KEY = re.compile(r"^[ \t]*(?:-[ \t]+)*([\w.-]+)[ \t]*:(?:[ \t]|$)", re.M)
MAKE_TARGET = re.compile(r"^([\w.%/-]+(?:[ \t]+[\w.%/-]+)*)[ \t]*::?(?!=)", re.M)
MAKE_VARIABLE = re.compile(
    r"^(?:export[ \t]+|override[ \t]+)?([A-Za-z_][\w.-]*)[ \t]*[?:+!]?=", re.M
)
MAKE_DEFINE = re.compile(r"^define[ \t]+([\w.-]+)", re.M)
NAME_PATTERNS = {
    ".ts": (TS_DECLARATION,),
    ".tsx": (TS_DECLARATION,),
    ".yaml": (YAML_KEY,),
    ".yml": (YAML_KEY,),
    "Makefile": (MAKE_TARGET, MAKE_VARIABLE, MAKE_DEFINE),
}

# 링크 주소. 인라인 코드 조각을 먼저 삼켜 그 안의 `[글](주소)` 는 링크로 보지 않는다
LINK = re.compile(r"`[^`\n]*`|\]\(([^)\s]+)")
URL = re.compile(r"[A-Za-z][\w+.-]*:")
# 제목으로 앵커를 만들기 전에 걷어 낼 것: HTML 태그와 링크 주소
HEADING_MARKUP = re.compile(r"<[^>]*>|\]\([^)]*\)")
HTML_ANCHOR = re.compile(r"""<a\s+(?:id|name)=["']([^"']+)["']""")
# GitHub 제목 앵커에 남는 글자다. Unicode 범주 L(letter)·M(combining mark)·N(number)·Pc(connector
# punctuation, `_` 등)와 공백, `-` 가 남는다
SLUG_KEEP = ("L", "M", "N", "Pc")
# 검사마다 적어도 이만큼은 보아야 한다. 이보다 적으면 정규식이 문서 형식과 어긋난 것이다.
# 줄 인용 거절은 걸린 것이 없어야 하므로 하한 대신 test_line_citation_rule 이 정규식과 판정을 지킨다
MIN_SYMBOL_CITATIONS = 50
MIN_LINKS = 100


def _docs(*patterns: re.Pattern[str]) -> list[Path]:
    """저장소 기준 경로가 patterns 가운데 하나에 맞는 문서."""
    return [REPO / p for p in repo_files() if any(rx.fullmatch(p) for rx in patterns)]


def _prose(doc: Path) -> str:
    """코드 블록 줄을 비운 문서. 줄 번호는 그대로다."""
    lines, fenced = [], False
    for line in doc.read_text(encoding="utf-8").splitlines():
        fence = bool(FENCE.match(line))
        fenced ^= fence
        lines.append("" if fenced or fence else line)
    return "\n".join(lines)


def _matches(
    pattern: re.Pattern[str], docs: list[Path]
) -> Iterator[tuple[Path, str, re.Match[str]]]:
    """문서마다 pattern 이 맞은 자리와 그 위치(`문서:줄`)."""
    for doc in docs:
        text = _prose(doc)
        for m in pattern.finditer(text):
            lineno = text.count("\n", 0, m.start()) + 1
            yield doc, f"{doc.relative_to(REPO)}:{lineno}", m


@functools.cache
def _index() -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = defaultdict(list)
    for root in SOURCE_ROOTS:
        for f in (REPO / root).rglob("*"):
            # 저장소 상대 경로로만 본다. worktree 가 .claude/worktrees/ 아래 있으면 절대 경로에
            # .claude 가 들어 색인이 비고 모든 인용이 "없다" 로 판정된다
            rel_parts = f.relative_to(REPO).parts
            if f.suffix in SUFFIXES and f.is_file() and not any(p in SKIP_DIRS for p in rel_parts):
                out[f.name].append(f)
    return out


@functools.cache
def _top_dirs() -> frozenset[str]:
    """저장소 맨 위 디렉터리 이름."""
    return frozenset(p.split("/", 1)[0] for p in repo_files() if "/" in p)


def _candidates(path: str) -> list[Path]:
    """path 로 읽히는 저장소 파일. 저장소 뿌리나 PREFIXES 아래의 그 경로가 먼저고, 없으면 그
    경로로 끝나는 소스 파일이다."""
    hits = [f for prefix in PREFIXES if (f := REPO / (prefix + path)).is_file()]
    return hits or [f for f in _index().get(Path(path).name, []) if str(f).endswith(path)]


@functools.cache
def _tracked_by_name() -> dict[str, list[str]]:
    """저장소 파일 전부를 맨 이름으로 묶는다. 줄 인용 판정은 확장자를 가리지 않는다."""
    out: dict[str, list[str]] = defaultdict(list)
    for p in repo_files():
        out[p.rsplit("/", 1)[-1]].append(p)
    return out


def _in_this_repo(path: str) -> bool:
    """path 가 이 저장소의 파일인가. 맨 앞 디렉터리가 저장소의 것이면 없는 파일이어도 그렇고,
    그 경로로 끝나는 저장소 파일이 있어도 그렇다(`resource-model.md`, `nginx.conf`)."""
    if path.split("/", 1)[0] in _top_dirs():
        return True
    tracked = _tracked_by_name().get(Path(path).name, [])
    return any(f == path or f.endswith("/" + path) for f in tracked)


def _resolve(path: str) -> Path | str:
    """경로를 실제 파일로 옮긴다. 옮기지 못하면 고칠 점을 돌려준다."""
    hits = _candidates(path)
    if len(hits) == 1:
        return hits[0]
    # 같은 이름의 파일이 여럿이면 검사가 불가능해 드리프트가 숨는다. api/ 와 services/ 에
    # backend_apis.py 가 생기며 실제로 그랬고, tests/conftest.py 는 패키지마다 있다
    return "같은 이름의 파일이 여럿이다. 전체 경로로 적는다" if hits else "그런 파일이 없다"


def _python_names(node: ast.AST, scope: str = "", in_function: bool = False) -> Iterator[str]:
    """정의한 이름과 모듈·클래스 몸에서 대입한 이름. 안쪽 것은 `Class.method` 와 맨 이름 둘 다다."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, DEFINITIONS):
            yield from (child.name, scope + child.name)
            inner = not isinstance(child, ast.ClassDef)
            yield from _python_names(child, f"{scope}{child.name}.", inner)
        elif isinstance(child, ast.Name):
            if isinstance(child.ctx, ast.Store) and not in_function:
                yield from (child.id, scope + child.id)
        else:
            yield from _python_names(child, scope, in_function)


@functools.cache
def _names(path: Path) -> frozenset[str]:
    """파일에서 인용할 수 있는 이름."""
    src = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        return frozenset(_python_names(ast.parse(src)))
    patterns = NAME_PATTERNS[path.suffix or path.name]
    return frozenset(name for rx in patterns for found in rx.findall(src) for name in found.split())


def _slug(heading: str) -> str:
    """GitHub 이 제목에 붙이는 앵커. 남길 글자만 두고 공백을 `-` 로 바꾼다."""
    text = HEADING_MARKUP.sub("", heading).strip().lower()
    kept = (c for c in text if c in " -" or unicodedata.category(c).startswith(SLUG_KEEP))
    return "".join(kept).replace(" ", "-")


@functools.cache
def _anchors(doc: Path) -> frozenset[str]:
    """마크다운 문서의 앵커. 제목마다 붙는 것(같은 제목은 -1, -2 …)과 `<a id>` 로 둔 것이다."""
    text = _prose(doc)
    anchors = set(HTML_ANCHOR.findall(text))
    seen: Counter[str] = Counter()
    for _level, heading in HEADING.findall(text):
        slug = _slug(heading)
        anchors.add(f"{slug}-{seen[slug]}" if seen[slug] else slug)
        seen[slug] += 1
    return frozenset(anchors)


def _cited_symbols(chain: str) -> Iterator[str]:
    """경로 뒤에 이어 적은 백틱 토큰 가운데 심볼을 차례로 준다. 호출 괄호는 뗀다. 심볼 모양이 아닌
    토큰(명령, 값, 다음 인용의 경로)에서 멈춘다."""
    for token in chain.split("`")[1::2]:
        symbol = CALL.sub("", token)
        if not SYMBOL.fullmatch(symbol):
            return
        yield symbol


def _assert_clean(what: str, checked: int, minimum: int, problems: list[str]) -> None:
    """what 을 minimum 건보다 많이 보았고 걸린 것이 없다."""
    assert checked > minimum, (
        f"{what} {checked}건밖에 못 찾았다. 정규식이 문서 형식과 어긋났는지 본다"
    )
    assert not problems, f"가리키는 곳이 없는 {what}:\n  " + "\n  ".join(problems)


def test_docs_are_listed() -> None:
    """docs/ 가 있으면 문서가 파일 목록에 있다. 문서를 다른 저장소가 추적하는 개발 폴더에서 그
    저장소를 알리지 않으면(`daegam.sharedGitDir`) 목록이 비어 아래 검사가 문서 없이 지나간다."""
    assert _docs(PLANS), (
        "docs/ 가 있는데 파일 목록에 docs/plan 의 문서가 없다. 문서를 추적하는 저장소의 "
        "git 디렉터리를 git config daegam.sharedGitDir 로 알린다"
    )


@pytest.mark.parametrize(
    ("text", "rejected"),
    [
        ("`platform/daegam/gone.py:3`", True),  # 맨 앞 디렉터리가 저장소의 것이면 지운 파일도
        ("`services/agents.py:758-826`", True),  # PREFIXES 로 읽는 짧은 경로
        ("`agents.py:758`", True),  # 저장소에 같은 이름의 파일이 있는 맨 이름
        ("`resource-model.md:497`", True),  # 소스 밖 확장자의 맨 이름도
        ("`index.css:71-82`", True),
        ("`nginx.conf:83`", True),
        ("`install.env.example:3`", True),
        ("[App](../../ui/src/App.tsx#L5)", True),
        ("`deepagents/middleware/skills.py:428-435`", False),  # 패키지 뿌리부터 적은 상류 근거
        ("`pkg/auth/token.go:1119`", False),
        ("`http://host.internal:9000`", False),  # 주소의 포트
    ],
)
def test_line_citation_rule(text: str, rejected: bool) -> None:
    paths = [m.group(1) for m in LINE_CITATION.finditer(text)]
    assert paths, f"{text} 에서 줄 인용 모양을 못 찾았다"
    assert any(map(_in_this_repo, paths)) is rejected, paths


def test_docs_do_not_cite_this_repo_by_line() -> None:
    problems = [
        f"{where} {m.group(0)}"
        for _, where, m in _matches(LINE_CITATION, _docs(CURRENT_DOCS, PLANS, RECORDS))
        if _in_this_repo(m.group(1))
    ]
    assert not problems, "이 저장소 코드의 줄 인용이다. 경로와 심볼로 적는다:\n  " + "\n  ".join(
        problems
    )


def test_doc_symbols_exist_in_cited_files() -> None:
    checked = 0
    problems: list[str] = []
    for _, where, m in _matches(SYMBOL_CITATION, _docs(CURRENT_DOCS, PLANS)):
        path, symbols = m.group(1), list(_cited_symbols(m.group(2)))
        if not symbols:
            continue
        checked += len(symbols)
        target = _resolve(path)
        if isinstance(target, str):
            problems.append(f"{where} `{path}` -> {target}")
            continue
        problems += [
            f"{where} `{path}` `{symbol}` -> {target.relative_to(REPO)} 에 그런 이름이 없다"
            for symbol in symbols
            if symbol not in _names(target)
        ]
    _assert_clean("심볼 인용", checked, MIN_SYMBOL_CITATIONS, problems)


def _under_docs(path: Path) -> bool:
    return path.is_relative_to(REPO / "docs")


def test_doc_links_and_anchors_resolve() -> None:
    checked = 0
    problems: list[str] = []
    for doc, where, m in _matches(LINK, _docs(CURRENT_DOCS, PLANS)):
        link = m.group(1)
        if link is None or URL.match(link):
            continue  # 코드 조각이거나 바깥 주소다
        checked += 1
        cited = f"{where} ({link})"
        path, _, anchor = unquote(link).partition("#")
        target = Path(os.path.normpath(doc.parent / path)) if path else doc
        if not target.is_relative_to(REPO) or not target.exists():
            problems.append(f"{cited} -> 그런 파일이 없다")
        elif doc.name == "README.md" and not _under_docs(doc) and _under_docs(target):
            problems.append(f"{cited} -> docs/ 밖의 README 는 공개 페이지라 docs/ 로 잇지 않는다")
        elif anchor and target.suffix == ".md" and anchor not in _anchors(target):
            problems.append(f"{cited} -> {target.relative_to(REPO)} 에 그런 앵커가 없다")
    _assert_clean("링크", checked, MIN_LINKS, problems)
