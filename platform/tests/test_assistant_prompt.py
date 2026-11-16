"""F3. 시스템 프롬프트와 knowledge/ 안내문이 서로, 그리고 도구 표와 맞는지 본다."""

from __future__ import annotations

import re
from pathlib import Path

from daegam.api.schemas.assistant import AssistantUiContext
from daegam.services.agent_changes import PATCH_FIELDS
from daegam.services.assistant.catalog import CATALOG
from daegam.services.assistant.prompt import (
    KNOWLEDGE_TOPICS,
    SYSTEM_PROMPT,
    UNSAVED_EDITS_LINE,
    build_system_prompt,
    system_content,
)
from daegam.services.assistant.tools import KNOWLEDGE_DIR

TOOL_NAMES = {spec.name for spec in CATALOG.tools}


def test_knowledge_files_match_the_topic_list() -> None:
    files = sorted(p.stem for p in KNOWLEDGE_DIR.glob("*.md"))
    assert files == sorted(KNOWLEDGE_TOPICS)
    for topic in KNOWLEDGE_TOPICS:
        assert topic in SYSTEM_PROMPT, f"프롬프트가 read_guide topic {topic} 을 알리지 않는다"


def test_knowledge_files_are_short_and_titled() -> None:
    for path in KNOWLEDGE_DIR.glob("*.md"):
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith("# "), f"{path.name} 첫 줄은 제목이어야 한다"
        assert len(lines) <= 60, f"{path.name} 는 {len(lines)}줄이다. 60줄 이내로 줄인다"
        assert "확인 필요" not in "\n".join(lines), f"{path.name} 에 미확인 표시가 남았다"


TOOL_PATTERN = re.compile(
    r"\b((?:list|get|create|update|delete|deploy|redeploy|verify|validate|publish|unpublish|check|refresh|set|revoke|preflight|convert|cluster)_[a-z_]+)\b"
)


def _unknown_tools(text: str) -> set[str]:
    """글이 부르는 도구 꼴(동사_리소스)의 이름 가운데 표에 없는 것이다. 오류 코드·필드 이름
    (cluster_id, verify_incomplete 등)과 바꾸기 제안의 패치 인자(set_nodes 등)는 도구가 아니다."""
    unknown = set()
    for name in set(TOOL_PATTERN.findall(text)):
        verb = name.split("_", 1)[0]
        if verb in {"cluster", "verify", "check"} and name not in TOOL_NAMES:
            continue
        if name in PATCH_FIELDS:
            continue
        if name not in TOOL_NAMES:
            unknown.add(name)
    return unknown


def test_knowledge_files_only_name_real_tools() -> None:
    """안내문이 부르는 도구 이름은 표에 있어야 한다. 없는 이름을 가르치면 모델이 헛손질한다."""
    for path in KNOWLEDGE_DIR.glob("*.md"):
        unknown = _unknown_tools(path.read_text(encoding="utf-8"))
        assert not unknown, f"{path.name} 가 표에 없는 도구 {sorted(unknown)} 를 가르친다"


def test_prompt_only_names_real_tools() -> None:
    """시스템 프롬프트가 부르는 도구 이름도 표에 있어야 한다(validate_agent_graph 등)."""
    assert not _unknown_tools(SYSTEM_PROMPT)


def test_prompt_states_the_rules_the_spec_requires() -> None:
    for phrase in (
        "ask_user",
        "navigate",
        "read_guide",
        "credential_secret",
        "합니다체",
        "Backend API → MCP Server",
        # Agent 정의는 제안이고 바꿀 노드는 읽은 것만 보낸다 (P48 6.8절)
        "get_agent_definition",
    ):
        assert phrase in SYSTEM_PROMPT


def test_ui_context_is_appended_as_one_line() -> None:
    text = build_system_prompt(
        {
            "route": "/mcp-servers/abc",
            "resource": {"kind": "mcp-server", "id": "abc", "name": "petstore"},
        }
    )
    assert text.endswith(
        "사용자는 지금 mcp-server petstore(abc) 상세를 보고 있다(경로 /mcp-servers/abc)."
    )
    assert build_system_prompt(None) == SYSTEM_PROMPT


def test_cached_system_content_marks_only_the_fixed_prompt() -> None:
    """Claude 의 캐시 표지는 고정 프롬프트 블록에만 달린다. 턴마다 바뀌는 문맥 줄은 표지 뒤
    블록이라 화면을 옮겨도 표지까지의 앞부분이 같다 (T80). 표지가 없으면 지금 문자열 그대로다."""
    mark = {"type": "ephemeral"}
    here = {"route": "/agents"}
    there = {"route": "/mcp-servers/abc", "resource": {"kind": "mcp-server", "id": "abc"}}
    first, second = system_content(here, mark), system_content(there, mark)
    assert first[0] == second[0] == {"type": "text", "text": SYSTEM_PROMPT, "cache_control": mark}
    assert first[1] == {"type": "text", "text": "사용자는 지금 /agents 화면을 보고 있다."}
    assert "cache_control" not in second[1]
    assert system_content(None, mark) == [first[0]]
    assert system_content(there) == build_system_prompt(there)


def test_unsaved_canvas_edits_ask_the_person_first() -> None:
    """캔버스에 저장하지 않은 변경이 있으면 제안하기 전에 저장하거나 버려 달라고 먼저 말한다
    (P48 6.5절)."""
    text = build_system_prompt(
        {
            "route": "/agents/ag-1",
            "resource": {"kind": "agent", "id": "ag-1", "name": "support"},
            "unsaved_edits": True,
        }
    )
    seen = "사용자는 지금 agent support(ag-1) 상세를 보고 있다(경로 /agents/ag-1)."
    assert text.endswith(f"{seen} {UNSAVED_EDITS_LINE}")
    assert build_system_prompt({"route": "/agents/new", "unsaved_edits": True}).endswith(
        "사용자는 지금 /agents/new 화면을 보고 있다. " + UNSAVED_EDITS_LINE
    )
    assert UNSAVED_EDITS_LINE not in build_system_prompt(
        {"route": "/agents/new", "unsaved_edits": False}
    )
    # 화면이 보내는 문맥의 모양이다. 모르는 칸은 받지 않는다
    assert AssistantUiContext(route="/agents/new", unsaved_edits=True).unsaved_edits is True
    assert AssistantUiContext(route="/agents/new").unsaved_edits is False


def test_snapshot_of_the_prompt_head() -> None:
    """프롬프트 첫 문장은 정체 선언이다. 바뀌면 의도한 것인지 되묻게 스냅샷으로 둔다."""
    assert SYSTEM_PROMPT.startswith('당신은 플랫폼 대감 안에서 일하는 비서 "청지기" 입니다.')
    assert Path(KNOWLEDGE_DIR).is_dir()


def test_prompt_keeps_the_lessons_from_the_live_run() -> None:
    """실제 provider 실측(2026-09-08)에서 본 세 가지. 승인 카드 앞에 ask_user 로 되묻기, 거절을
    플랫폼 오류로 말하기, options 를 입력 항목 목록으로 쓰기."""
    assert "ask_user 로 다시 묻지 않습니다" in SYSTEM_PROMPT
    assert "rejected 면 사용자가 카드에서 거절한 것" in SYSTEM_PROMPT
    assert "options 에는 사용자가 고를 답만" in SYSTEM_PROMPT


def test_prompt_keeps_the_lessons_from_the_proposal_run() -> None:
    """제안 프롬프트가 막는 실수들 (P48). 제안의 쓰기 Tool 에 승인을 빠뜨리기, 이름만 댄 Agent
    찾기(청지기는 안내를 읽기 전에 이름부터 찾는다), 만들어 달라는 Agent 를 기존 Agent 의 바꾸기로
    돌리기. 사람이 승인을 빼 달라는 요청은 캔버스로 보낸다(명세 6.3절)."""
    assert "빠짐없이 approval 을 둡니다" in SYSTEM_PROMPT
    assert "그 쓰기 Tool 을 뺀 제안을 보내고 뺐다고 말합니다" in SYSTEM_PROMPT
    assert "캔버스에서 직접" in SYSTEM_PROMPT
    assert "Agent, MCP Server, Gateway 목록에서" in SYSTEM_PROMPT
    assert "비슷한 Agent 가 있어도 새로 제안합니다" in SYSTEM_PROMPT
