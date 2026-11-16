"""Skill 이 붙은 Agent 의 런타임이다(P39 7절, runtime/skills.py·permissions.py·compile.py).

트리는 init 단계(`init_steps.skills.install`)가 가짜 fetch 로 고정물을 받아 만든다. 메인의
`/skills/main/` 목록과 읽기, 쓰기·고치기·지우기가 권한 규칙과 디스크 권한 두 겹에서 막히는 것,
`delete("/")`·`delete("/skills")`·`write_file("/skills")` 가 트리를 건드리지 못하는 것, 메인과
서브에이전트가 제 소스만 목록에 올리는 것, 우리 문구가 시스템 메시지에 들어가고 기본 문구가 없는 것,
메인이 턴마다 목록을 다시 읽는 것, `skills_filesystem_disabled`·`skills_missing`, 임시 턴 그래프의
`/skills/` 라우트, 문서의 Skill 참조 검사를 본다. 같은 고정물을 SkillsMiddleware 가 모두 목록에
올리는 것도 본다(플랫폼 형식 검사가 받는 것이 deepagents 가 받는 것의 부분 집합이다).
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import pytest
from deepagents.backends import CompositeBackend, FilesystemBackend
from langchain_core.messages import BaseMessage, SystemMessage
from pydantic import PrivateAttr

from runtime import compile as runtime_compile
from runtime.compile import build_backend, compile_agent
from runtime.config import ConfigError, load_config, parse_config
from runtime.init_steps import skills as step
from runtime.permissions import build_permissions
from runtime.skills import (
    NO_PARTS,
    NO_SKILLS,
    SKILLS_METADATA_KEY,
    SKILLS_PROMPT,
    SKILLS_ROUTE,
    load_skills,
    skills_dir,
    skills_middleware,
)
from runtime.state import Settings
from runtime.stub import StubChatModel
from tests.app_fixtures import memory_app, memory_doc, sid_of, tool_message, with_subagent
from tests.conftest import chat, events_named, run_app
from tests.skill_fixtures import (
    SKILL_FIXTURES,
    FakeFetch,
    fingerprint,
    fixture_bundles,
    pin_of,
    write_agent_json,
)
from tests.store_fixtures import memory_backend

SKILL_MODEL = "stub-skills"
"""파일 도구를 보이는 모델 키다. harness profile 은 키마다 합집합이라 다른 테스트와 나눠 쓰지
않는다."""
OUR_MARK = "Skills are instructions packaged by the people who built this agent."
DEFAULT_MARKS = ("## Skills System", "Deepagents", "Executing Skill Scripts")
"""deepagents 기본 프롬프트에만 있는 글이다."""
NEW_DESCRIPTION = "회의록에도 같은 문체를 쓴다"
READ_PATH = re.compile(r"-> Read `([^`]+)`")


def task(inner: str) -> str:
    return tool_message("task", description=inner, subagent_type="calc")


def attach(doc: dict[str, Any], bundles: list[dict[str, Any]], **agents: list[str]) -> None:
    """기능 skills 를 켜고 핀을 싣는다. agents 는 `main`, `sub` 에 붙일 이름이다."""
    doc["features"] = ["skills"]
    doc["skills"] = [pin_of(b) for b in bundles]
    doc["main"]["skills"] = agents.get("main", [])
    for sub in doc["subagents"]:
        sub["skills"] = agents.get("sub", [])


def install(config_dir: Path, doc: dict[str, Any], bundles: list[dict[str, Any]]) -> Path:
    """agent.json 을 쓰고 init 단계로 트리를 만든다. agent.json 경로를 준다."""
    config_dir.mkdir(exist_ok=True)
    config_file = write_agent_json(config_dir, doc)
    step.install(config_dir, FakeFetch(config_dir, {b["name"]: b for b in bundles}))
    return config_file


def with_description(bundle: dict[str, Any], description: str) -> dict[str, Any]:
    """SKILL.md 의 설명을 바꾼 새 리비전이다."""
    out = copy.deepcopy(bundle)
    skill_md = next(f for f in out["files"] if f["path"] == step.SKILL_FILE)
    skill_md["content"] = re.sub(
        r"(?m)^description: .*$", f"description: {description}", skill_md["content"]
    )
    out["revision"] += 1
    out["digest"] = step.skill_digest((f["path"], f["content"]) for f in out["files"])
    return out


class PromptRecorder(StubChatModel):
    """런타임 스텁에 호출마다의 시스템 메시지를 적어 두는 것만 더했다."""

    _systems: list[str] = PrivateAttr(default_factory=list)

    def reply(self, messages: Any) -> Any:
        first: BaseMessage | None = messages[0] if messages else None
        self._systems.append(first.text if isinstance(first, SystemMessage) else "")
        return super().reply(messages)

    def take(self) -> list[str]:
        systems, self._systems = self._systems, []
        return systems


@pytest.fixture
def bundles() -> list[dict[str, Any]]:
    return fixture_bundles()


@pytest.fixture
def skills_doc(support_doc: dict[str, Any], bundles: list[dict[str, Any]]) -> dict[str, Any]:
    """메인에 둘, 계산 서브에이전트 calc 에 하나를 붙인 문서다."""
    support_doc["main"]["model"]["name"] = SKILL_MODEL
    support_doc["main"]["tools"] = []
    with_subagent(support_doc)
    attach(support_doc, bundles, main=["report-style", "web-research"], sub=["web-research"])
    return support_doc


@pytest.fixture
def settings(tmp_path: Path, skills_doc: dict[str, Any], bundles: list[dict[str, Any]]) -> Settings:
    return Settings(config_path=str(install(tmp_path / "config", skills_doc, bundles)))


def skills_root(settings: Settings) -> Path:
    return skills_dir(settings.config_path)


async def call(running: Any, message: str, **kw: Any) -> tuple[dict[str, Any], str]:
    """한 턴을 돌려 첫 도구 결과와 세션 id 를 준다."""
    _, sse = await chat(running, message, **kw)
    return events_named(sse, "tool_result")[0], sid_of(sse)


# --- 읽기와 쓰기 막기 -----------------------------------------------------------------------


async def test_the_main_lists_and_reads_its_skills(skills_doc, settings) -> None:
    async with run_app(skills_doc, settings=settings) as running:
        listed, _ = await call(running, tool_message("ls", path="/skills/main/"))
        assert "/skills/main/report-style/" in listed["content"]
        assert "/skills/main/web-research/" in listed["content"]
        path = "/skills/main/report-style/SKILL.md"
        read, _ = await call(running, tool_message("read_file", file_path=path, limit=1000))
        assert read["status"] == "success" and "첫 문단에 결론을" in read["content"]
        side, _ = await call(running, tool_message("read_file", file_path="/skills/../etc/hosts"))
        assert "Path traversal not allowed" in side["content"]


@pytest.mark.parametrize(
    "message",
    [
        tool_message("write_file", file_path="/skills/main/report-style/SKILL.md", content="x"),
        tool_message("write_file", file_path="/skills/main/evil/SKILL.md", content="x"),
        tool_message(
            "edit_file",
            file_path="/skills/main/report-style/SKILL.md",
            old_string="결론",
            new_string="x",
        ),
        tool_message("delete", file_path="/skills/main/report-style"),
        tool_message("delete", file_path="/skills"),
        tool_message("delete", file_path="/"),
    ],
    ids=["overwrite", "new_skill", "edit", "delete_skill", "delete_route", "delete_root"],
)
async def test_tools_cannot_write_the_skills(skills_doc, settings, message) -> None:
    before = fingerprint(skills_root(settings))
    async with run_app(skills_doc, settings=settings) as running:
        result, _ = await call(running, message)
    assert result["status"] == "error"
    assert "permission denied" in result["content"], result
    assert fingerprint(skills_root(settings)) == before


async def test_the_bare_route_root_is_not_a_file(skills_doc, settings) -> None:
    """맨 `/skills` 는 규칙에 없고 라우트 뿌리로 간다. 뿌리가 디렉터리라 쓰기가 실패한다."""
    before = fingerprint(skills_root(settings))
    async with run_app(skills_doc, settings=settings) as running:
        result, _ = await call(
            running, tool_message("write_file", file_path="/skills", content="x")
        )
    assert "Is a directory" in result["content"], result
    assert fingerprint(skills_root(settings)) == before


async def test_subagents_inherit_the_write_block(skills_doc, settings) -> None:
    inner = tool_message("write_file", file_path="/skills/sub-0/web-research/SKILL.md", content="x")
    async with run_app(skills_doc, settings=settings) as running:
        _, sse = await chat(running, task(inner))
    done = events_named(sse, "done")[0]
    assert "permission denied for write on /skills/sub-0/web-research/SKILL.md" in done["text"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root 는 권한 비트를 지나므로 잠금을 볼 수 없다")
def test_the_disk_refuses_writes_behind_the_rules(skills_doc, settings) -> None:
    """권한 규칙을 거치지 않고 라우트 뒤 백엔드를 바로 불러도 디스크가 막는다. 파드에서는 읽기
    전용 마운트라 EROFS 이고 여기서는 권한 비트라 EACCES 다."""
    root = skills_root(settings)
    before = fingerprint(root)
    disk = load_skills(load_config(settings.config_path)).routes()[SKILLS_ROUTE]
    results = [
        disk.write("/main/evil.md", "x"),
        disk.write("/main/report-style/SKILL.md", "x"),
        disk.edit("/main/report-style/SKILL.md", "결론", "x"),
        disk.delete("/main/report-style"),
    ]
    assert all(r.error and "Permission denied" in r.error for r in results), results
    assert fingerprint(root) == before


# --- 목록과 문구 ----------------------------------------------------------------------------


async def test_each_agent_lists_only_its_own_source_with_our_prompt(
    skills_doc, settings, monkeypatch
) -> None:
    main_model = PromptRecorder(model_name=SKILL_MODEL)
    sub_model = PromptRecorder(model_name=SKILL_MODEL)
    monkeypatch.setattr(runtime_compile, "build_chat_model", lambda spec, **kw: sub_model)
    inner = tool_message("read_file", file_path="/skills/main/report-style/SKILL.md")
    async with run_app(skills_doc, settings=settings, model=main_model) as running:
        _, sse = await chat(running, task(inner))
    # 서브도 메인의 Skill 파일을 읽는다. 목록은 붙인 에이전트에만 오른다
    assert "첫 문단에 결론을" in events_named(sse, "done")[0]["text"]
    main_system, sub_system = main_model.take()[0], sub_model.take()[0]
    assert READ_PATH.findall(main_system) == [
        "/skills/main/report-style/SKILL.md",
        "/skills/main/web-research/SKILL.md",
    ]
    assert READ_PATH.findall(sub_system) == ["/skills/sub-0/web-research/SKILL.md"]
    for system, source in ((main_system, "/skills/main/"), (sub_system, "/skills/sub-0/")):
        assert system.count(OUR_MARK) == 1 and system.count("## Skills") == 1
        assert f"**Agent Skills**: `{source}`" in system
        assert not [mark for mark in DEFAULT_MARKS if mark in system]


async def test_the_main_rereads_its_list_every_turn(
    skills_doc, settings, bundles, tmp_path
) -> None:
    """재배포로 트리가 바뀌면 이어지는 세션의 다음 턴이 새 설명을 본다. 턴 입력의
    `skills_metadata: None` 이 없으면 상태에 남은 옛 목록을 그대로 쓴다."""
    model = PromptRecorder(model_name=SKILL_MODEL)
    async with run_app(skills_doc, settings=settings, model=model) as running:
        _, sid = await call(running, tool_message("ls", path="/skills/main/"))
        assert NEW_DESCRIPTION not in model.take()[0]
        assert running.runtime.agent.reloads_skills
        changed = [with_description(bundles[0], NEW_DESCRIPTION), bundles[1]]
        attach(skills_doc, changed, main=["report-style", "web-research"], sub=["web-research"])
        install(tmp_path / "config", skills_doc, changed)
        await call(running, tool_message("ls", path="/skills/main/"), session_id=sid)
        assert NEW_DESCRIPTION in model.take()[0]


def test_only_a_main_with_skills_reloads(support_doc, bundles, tmp_path) -> None:
    support_doc["main"]["model"]["name"] = SKILL_MODEL
    with_subagent(support_doc)
    attach(support_doc, bundles, sub=["web-research"])
    config_file = install(tmp_path / "config", support_doc, bundles)
    agent = compile_agent(
        load_config(config_file),
        tools_by_ref={},
        checkpointer=None,
        model=StubChatModel(model_name=SKILL_MODEL),
    )
    assert not agent.reloads_skills
    assert SKILLS_METADATA_KEY == "skills_metadata"


def test_the_shared_fixtures_all_list() -> None:
    """플랫폼 형식 검사를 지나는 고정물을 deepagents 도 모두 받는다."""
    backend = FilesystemBackend(root_dir=SKILL_FIXTURES, virtual_mode=True)
    update = skills_middleware(backend, "/").before_agent({}, None, {})
    assert update is not None
    assert update["skills_load_errors"] == []
    names = sorted(skill["name"] for skill in update[SKILLS_METADATA_KEY])
    assert names == sorted(p.name for p in SKILL_FIXTURES.iterdir() if p.is_dir())


def test_the_prompt_keeps_the_slots_deepagents_fills() -> None:
    for slot in ("{skills_locations}", "{skills_load_warnings}", "{skills_list}"):
        assert slot in SKILLS_PROMPT


# --- 기동 점검 ------------------------------------------------------------------------------


def test_skills_need_the_filesystem_tools(skills_doc, settings) -> None:
    skills_doc["main"]["builtin_tools"]["filesystem"] = False
    with pytest.raises(ConfigError) as info:
        load_skills(parse_config(skills_doc, path=Path(settings.config_path)))
    assert info.value.code == "skills_filesystem_disabled"


async def test_the_app_refuses_to_start_without_the_tree(
    skills_doc, tmp_path, termination_log
) -> None:
    """이유는 SKILL.md 가 없는 자리를 에이전트마다 든다."""
    settings = Settings(config_path=str(tmp_path / "agent.json"))
    with pytest.raises(ConfigError):
        async with run_app(skills_doc, settings=settings):
            pass
    reason = termination_log.read_text(encoding="utf-8")
    assert reason.startswith("skills_missing:")
    assert "main/report-style" in reason and "sub-0/web-research" in reason


def test_documents_without_skills_keep_their_backend_and_rules(support_doc) -> None:
    config = parse_config(support_doc, path=Path("/nowhere/agent.json"))
    skills = load_skills(config)
    assert skills is NO_SKILLS and skills.parts(build_backend(None)) is NO_PARTS
    assert SKILLS_ROUTE not in build_backend(None).routes
    assert [rule.paths for rule in build_permissions(config)] == [["/uploads/**"]]


def test_skills_add_the_route_and_the_rule(skills_doc, settings) -> None:
    """Skill 트리는 문서를 읽은 agent.json 곁에서 찾는다. 기동과 다시 컴파일이 같은 자리를 본다."""
    config = load_config(settings.config_path)
    skills = load_skills(config)
    assert skills.root == skills_root(settings)
    backend = build_backend(None, skills)
    assert isinstance(backend, CompositeBackend)
    assert isinstance(backend.routes[SKILLS_ROUTE], FilesystemBackend)
    assert [rule.paths for rule in build_permissions(config)] == [["/uploads/**", "/skills/**"]]


def test_the_skills_dir_sits_beside_agent_json(monkeypatch) -> None:
    assert skills_dir("/somewhere/agent.json") == Path("/somewhere/skills")
    monkeypatch.setenv("DAEGAM_AGENT_CONFIG", "/elsewhere/agent.json")
    assert skills_dir(None) == Path("/elsewhere/skills")


async def test_the_ephemeral_graph_keeps_the_route(bundles, tmp_path) -> None:
    """임시 턴은 /memories/ 를 떼지만 /skills/ 는 그대로 읽는다."""
    doc = memory_doc()
    attach(doc, bundles[:1], main=["report-style"])
    settings = Settings(config_path=str(install(tmp_path / "config", doc, bundles[:1])))
    path = "/skills/main/report-style/SKILL.md"
    model = StubChatModel(model_name=SKILL_MODEL)
    async with memory_app(memory_backend(), doc, settings=settings, model=model) as running:
        assert running.runtime.agent.ephemeral_graph is not None
        result, _ = await call(running, tool_message("read_file", file_path=path), ephemeral=True)
    assert result["status"] == "success" and "첫 문단에 결론을" in result["content"]


# --- 문서 참조 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda d: d["skills"].append(copy.deepcopy(d["skills"][0])), "duplicate_skill"),
        (lambda d: d["main"]["skills"].append("unknown-skill"), "unknown_skill_ref"),
        (lambda d: d["subagents"][0]["skills"].append("unknown-skill"), "unknown_skill_ref"),
        (lambda d: d.pop("features"), "schema"),
    ],
    ids=["duplicate", "main_unknown", "sub_unknown", "no_feature"],
)
def test_skill_references_are_checked(skills_doc, change, code) -> None:
    change(skills_doc)
    with pytest.raises(ConfigError) as info:
        parse_config(skills_doc)
    assert info.value.code == code
