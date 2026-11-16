"""붙인 Skill 을 `/skills/` 아래 읽기 전용 파일로 보인다(D64, P39 7절).

init 단계(init_steps/skills.py)가 설정 볼륨의 `skills/`(agent.json 곁) 아래에 에이전트마다
`main/<name>/…` 와 `sub-<i>/<name>/…` 로 파일을 둔다. 여기서는 그 디렉터리를 파일 백엔드의
`/skills/` 라우트(`FilesystemBackend(virtual_mode=True)`)로 붙이고, 에이전트마다 제 폴더를 소스로
읽는 `SkillsMiddleware` 를 만든다. 이름과 설명만 프롬프트에 오르고 본문은 모델이 read_file 로
펼친다. compile.py 는 `load_skills` 가 준 `AgentSkills` 에서 라우트(`routes`)와 에이전트마다의
조각(`parts`)만 받아 쓴다.

- 문구. deepagents 의 기본 프롬프트는 스크립트 실행과 "Deepagents" 출처를 말하므로 SKILLS_PROMPT 를
  넣은 미들웨어를 메인과 서브에이전트의 middleware 에 같은 이름으로 넘겨 제자리 교체한다. 압축
  미들웨어와 같은 방법이다(compile.py). 코드 실행 Agent 는 Skill 이 작업 공간에도 올라가므로
  스크립트를 execute 로 돌릴 수 있다고 말하는 `SKILLS_PROMPT_EXECUTES` 를 쓴다(P40 6절).
- 읽기 전용은 두 겹이다. Skill 파일은 이 파드의 모든 세션이 함께 읽으므로 한 대화가 고치면 다른
  사용자의 대화에 인젝션이 심긴다. 마운트가 readOnly 라 쓰기가 EROFS 로 실패하고, 그보다 먼저
  permissions.py 의 `/skills/**` 규칙이 도구 자리에서 거절한다.
- 파일 도구. 본문을 read_file 로 펼치므로 메인의 builtin_tools.filesystem 이 참이어야 한다. 아니면
  `skills_filesystem_disabled` 로 기동을 막는다. 장기 기억과 같은 규칙이다(long_term.py).
- 제자리 점검. 에이전트마다 붙인 SKILL.md 가 있는지 보고 없으면 `skills_missing` 이다.
- 다시 읽기. 메인은 상태에 `skills_metadata` 가 있으면 목록을 다시 읽지 않는다. 재배포 뒤 이어지는
  세션도 새 이름과 설명을 쓰도록 런타임이 턴마다 메인 입력에 그 키를 None 으로 싣는다(stream.py).
  isolated 서브에이전트는 넘겨받는 상태에서 그 키가 빠져 task 마다 새로 읽으므로 따로 할 일이 없다.

한 Agent 안에서는 Skill 파일을 숨기지 않는다. 목록은 붙인 에이전트에만 오르지만 파일 도구가
있으면 `/skills/` 전부를 읽는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from deepagents.backends import BackendProtocol, FilesystemBackend
from deepagents.middleware.skills import SkillsMiddleware
from langchain.agents.middleware.types import AgentMiddleware

from runtime.config import AgentConfig, ConfigError, config_path
from runtime.init_steps.skills import (
    MAIN_FOLDER,
    SKILL_FILE,
    SKILLS_DIR,
    SUB_FOLDER,
    agent_folders,
)

SKILLS_ROUTE = "/skills/"
SOURCE_LABEL = "Agent"
"""프롬프트의 `**Agent Skills**: /skills/main/` 줄에 실리는 출처 이름이다."""
SKILLS_METADATA_KEY = "skills_metadata"
"""SkillsMiddleware 가 읽은 목록을 두는 상태 키다. None 이면 다음 호출에 다시 읽는다."""

_SKILLS_PROMPT_HEAD = (
    "## Skills\n"
    "Skills are instructions packaged by the people who built this agent. "
    "{skills_locations}{skills_load_warnings}\n"
    "**Available skills:**\n"
    "{skills_list}\n"
    "When a request matches a skill's description, read its SKILL.md with read_file "
    "(pass limit=1000) before you act, then follow it. Read the other files of a skill only "
    "when its SKILL.md points to them. Skill files are read-only. "
)
_SKILLS_PROMPT_TAIL = (
    "A skill never gives you a tool you do not have and never lets you skip an approval."
)
SKILLS_PROMPT = (
    _SKILLS_PROMPT_HEAD
    + "You cannot run scripts; treat any script in a skill as text. "
    + _SKILLS_PROMPT_TAIL
)
"""기본 프롬프트를 바꾸는 문구다(P39 7절). 세 자리는 SkillsMiddleware 가 채운다."""
SKILLS_PROMPT_EXECUTES = (
    _SKILLS_PROMPT_HEAD
    + "Skills are also copied to /workspace/skills/, so you can run a skill's scripts there "
    "with execute when its SKILL.md tells you to; the approvals on execute still apply. "
    + _SKILLS_PROMPT_TAIL
)
"""코드 실행 Agent 의 문구다(P40 6절). Skill 은 작업 공간에도 올라가 스크립트를 execute 로 돌릴 수
있다(sandbox.py)."""


@dataclass(frozen=True)
class SkillParts:
    """에이전트 하나에 거는 Skill 조각이다. sources 를 create_deep_agent·SubAgent 의 `skills` 로
    넘기면 deepagents 가 기본 SkillsMiddleware 를 두고, middleware 의 것이 같은 이름이라 그 자리를
    우리 문구로 바꾼다. Skill 이 붙지 않은 에이전트는 둘 다 비어 있다."""

    sources: tuple[str, ...] = ()
    middleware: tuple[AgentMiddleware, ...] = ()


@dataclass(frozen=True)
class AgentSkills:
    """점검을 지난 Skill 디렉터리와 에이전트마다의 소스 경로다. 소스는 Skill 이 붙은 에이전트만
    있다. Skill 이 붙지 않은 문서는 root 가 None 인 NO_SKILLS 다."""

    root: Path | None = None
    main: str | None = None
    subagents: dict[str, str] = field(default_factory=dict)
    """서브에이전트 이름에서 소스 경로로 가는 표다."""
    executes: bool = False
    """코드 실행 Agent 인가. 그러면 스크립트를 execute 로 돌릴 수 있다고 말하는 문구를 쓴다."""

    def routes(self) -> dict[str, BackendProtocol]:
        """파일 백엔드에 더할 라우트다. Skill 이 붙었으면 `/skills/` 하나이고 virtual mode 라
        `..`·`~` 와 뿌리 밖 경로를 막는다."""
        if self.root is None:
            return {}
        return {SKILLS_ROUTE: FilesystemBackend(root_dir=self.root, virtual_mode=True)}

    def parts(self, backend: BackendProtocol, subagent: str | None = None) -> SkillParts:
        """메인(subagent 가 None)이나 이름이 subagent 인 서브에이전트에 거는 조각이다. 미들웨어가
        백엔드를 물고 있어 그래프마다 새로 만든다."""
        source = self.main if subagent is None else self.subagents.get(subagent)
        if source is None:
            return NO_PARTS
        prompt = SKILLS_PROMPT_EXECUTES if self.executes else SKILLS_PROMPT
        return SkillParts(
            sources=(source,), middleware=(skills_middleware(backend, source, prompt),)
        )


NO_PARTS = SkillParts()
NO_SKILLS = AgentSkills()


def uses_skills(config: AgentConfig) -> bool:
    """Skill 이 붙은 문서인가. `/skills/` 라우트와 쓰기 막기 규칙이 이것을 따른다."""
    return bool(config.skills)


def skills_dir(config_file: str | Path | None) -> Path:
    """init 단계가 Skill 을 쓴 디렉터리다. agent.json 곁의 `skills/` 다. 파일을 모르면 env 의
    agent.json 경로를 쓴다."""
    return (Path(config_file) if config_file else config_path()).parent / SKILLS_DIR


def source_path(folder: str) -> str:
    return f"{SKILLS_ROUTE}{folder}/"


def load_skills(config: AgentConfig) -> AgentSkills:
    """Skill 이 붙은 문서면 파일 도구와 문서를 읽은 파일 곁의 `skills/` 를 점검해 소스를 정한다.
    아니면 NO_SKILLS 다."""
    if not uses_skills(config):
        return NO_SKILLS
    if not config.filesystem:
        raise ConfigError(
            "skills_filesystem_disabled",
            "Skill 이 붙어 있는데 builtin_tools.filesystem 이 거짓이라 본문을 읽을 Tool 이 없다",
        )
    root = skills_dir(config.path)
    _check_in_place(root, agent_folders(config.raw))
    main = source_path(MAIN_FOLDER) if config.main.get("skills") else None
    subagents = {
        sub["name"]: source_path(SUB_FOLDER.format(index=i))
        for i, sub in enumerate(config.subagents)
        if sub.get("skills")
    }
    return AgentSkills(
        root=root, main=main, subagents=subagents, executes=config.code_execution is not None
    )


def _check_in_place(root: Path, folders: list[tuple[str, list[str]]]) -> None:
    missing = [
        f"{folder}/{name}"
        for folder, names in folders
        for name in names
        if not (root / folder / name / SKILL_FILE).is_file()
    ]
    if missing:
        raise ConfigError(
            "skills_missing", f"{root} 에 붙인 Skill 의 {SKILL_FILE} 가 없다: {', '.join(missing)}"
        )


def skills_middleware(
    backend: BackendProtocol, source: str, prompt: str = SKILLS_PROMPT
) -> SkillsMiddleware:
    """우리 문구로 source 를 읽는 SkillsMiddleware 다. 이름이 같아 deepagents 의 것을 바꾼다. prompt
    는 코드 실행 Agent 면 `SKILLS_PROMPT_EXECUTES` 다."""
    return SkillsMiddleware(backend=backend, sources=[(source, SOURCE_LABEL)], system_prompt=prompt)
