"""Agent 그래프에 붙은 Skill (D64, P39 5·6절).

에이전트 노드(메인과 서브)의 data 에 `skills: [{id, revision}]` 로 붙고 붙일 때 리비전이 핀된다.
노드 종류를 더하지 않는다. 그래프 검증의 Skill 규칙과 문구, 문서 기능 `skills` 의 붙이는 함수,
상태 경고 `skill_access_revoked` 가 여기 한 곳에 있다. 이 모듈은 `agent_graph` 를 import 하지
않는다(그쪽이 이것을 부른다).

검증이 읽는 리비전 메타(`PinnedSkill`)는 그래프가 가리키는 핀만 미리 읽어 문맥에 싣는다. 컴파일이
스레드풀에서 돌아 Mongo 를 부르지 못하기 때문이다. 스토어를 읽는 일(검증 문맥의 메타, consume
판정, 상태 경고, init 의 Skill 받기)은 `AgentSkillStore` 하나가 하고 `AgentService` 는 거기에
위임한다.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, NamedTuple

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from daegam.auth.hub_access import can, revoked_lines, visible
from daegam.auth.principal import Principal
from daegam.errors import ServiceError
from daegam.services.skill_format import is_script
from daegam.store.agents import AgentSpec
from daegam.store.skills import MongoSkillStore, Skill, SkillRevision, SkillRevisionMeta
from daegam.units import MIB

# 에이전트 하나에 붙일 수 있는 Skill 수다. 이름과 설명이 모든 모델 호출에 실린다
# (agent.schema.json 의 agent_skills)
MAX_PER_AGENT = 10
# 그래프 전체의 서로 다른 Skill 수(agent.schema.json 의 최상위 skills)
MAX_PER_GRAPH = 20
# 서로 다른 핀의 본문 합과, init 이 에이전트마다 펼쳐 쓰는 합의 상한
MAX_DISTINCT_BYTES = 4 * MIB
MAX_EXPANDED_BYTES = 16 * MIB

# 오류·경고 코드. 캔버스 검증(ui/src/lib/agentGraph.ts)이 같은 코드를 낸다
SKILLS_FILESYSTEM_DISABLED = "skills_filesystem_disabled"
TOO_MANY_SKILLS = "too_many_skills"
SKILL_REVISION_MISMATCH = "skill_revision_mismatch"
SKILL_NAME_CONFLICT = "skill_name_conflict"
UNKNOWN_SKILL = "unknown_skill"
SKILLS_TOO_LARGE = "skills_too_large"
SKILL_SCRIPTS_INERT = "skill_scripts_inert"
SKILL_BEHIND = "skill_behind"
# 보이는데 consume 이 없는 Skill 을 붙인 저장·배포·재배포·검사(422)
SKILL_NOT_CONSUMABLE = "skill_not_consumable"
# 배포가 핀한 Skill 에서 이 테넌트가 consume 경계 밖으로 빠졌다(상태 경고)
SKILL_ACCESS_REVOKED = "skill_access_revoked"

Pin = tuple[str, int]


class SkillNotConsumable(ServiceError):
    """보이는 Skill 인데 consume 이 없어 에이전트에 붙이지 못한다(422)."""

    def __init__(self, skill: Skill):
        super().__init__(
            422,
            SKILL_NOT_CONSUMABLE,
            f"{skill.name}: no permission to attach this skill to an agent",
            skill_id=skill.id,
        )


class SkillPin(BaseModel):
    """에이전트 노드에 붙인 Skill 하나. 붙일 때의 리비전이 핀된다."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    revision: int = Field(ge=1)


def _distinct(pins: list[SkillPin]) -> list[SkillPin]:
    ids = [p.id for p in pins]
    if len(set(ids)) != len(ids):
        raise ValueError("skills contains duplicates")
    return pins


# `AgentData.skills` 의 모양이다. 한 노드에 같은 Skill 은 한 번이다
SkillPins = Annotated[list[SkillPin], AfterValidator(_distinct)]
_PINS: TypeAdapter[list[SkillPin]] = TypeAdapter(SkillPins)


@dataclass(frozen=True)
class AgentSkills:
    """에이전트 노드 하나와 그 노드가 붙인 Skill 이다."""

    node_id: str
    role: str
    name: str
    pins: tuple[SkillPin, ...]


@dataclass(frozen=True)
class PinnedSkill:
    """그래프가 핀한 Skill 리비전 하나의 메타다. 본문은 없고 `latest` 는 그 Skill 의 최신
    번호다."""

    id: str
    name: str
    revision: int
    latest: int
    digest: str
    bytes: int
    scripts: bool


class Finding(NamedTuple):
    """검사 결과 한 줄. `agent_graph` 가 이슈로 옮긴다."""

    code: str
    message: str
    level: Literal["error", "warning"] = "error"
    node_id: str | None = None


# -- 그래프 읽기 ---------------------------------------------------------------------------


def attached_skills(spec: AgentSpec) -> list[AgentSkills]:
    """에이전트 노드마다 붙인 Skill. graph.nodes 순서다. `skills` 모양이 틀린 노드는 건너뛴다
    (검증이 `invalid_node_data` 로 막는다)."""
    out: list[AgentSkills] = []
    for node in spec.graph.nodes:
        if node.type != "agent":
            continue
        try:
            pins = _PINS.validate_python(node.data.get("skills") or [])
        except ValidationError:
            continue
        role, name = str(node.data.get("role")), str(node.data.get("name"))
        out.append(AgentSkills(node.id, role, name, tuple(pins)))
    return out


def skill_pins(spec: AgentSpec) -> dict[str, int]:
    """그래프가 핀한 Skill id 와 리비전. 같은 Skill 을 노드마다 다른 리비전으로 핀한 그래프는 검증이
    거절하므로 처음 본 것을 쓴다."""
    pins: dict[str, int] = {}
    for agent in attached_skills(spec):
        for pin in agent.pins:
            pins.setdefault(pin.id, pin.revision)
    return pins


def pinned_refs(spec: AgentSpec) -> list[Pin]:
    """그래프가 가리키는 (id, 리비전) 전부. 노드마다 리비전이 달라도 모두 든다. 문맥이 이것의
    메타를 미리 읽는다."""
    refs = [(p.id, p.revision) for agent in attached_skills(spec) for p in agent.pins]
    return list(dict.fromkeys(refs))


def pinned_skills(
    skills: Mapping[str, Skill], metas: Iterable[SkillRevisionMeta]
) -> dict[Pin, PinnedSkill]:
    """스토어에서 읽은 Skill 과 리비전을 검증 문맥의 메타로 옮긴다. Skill 이 없는 리비전과 최신보다
    큰 번호는 뺀다. 그 번호는 저장 중이거나 멈춘 저장이 남긴 것이라 나중에 다른 내용으로 채워질 수
    있으므로 검증이 `unknown_skill` 로 막는다."""
    return {
        (m.skill_id, m.revision): PinnedSkill(
            id=m.skill_id,
            name=m.frontmatter.name,
            revision=m.revision,
            latest=skills[m.skill_id].revision,
            digest=m.digest,
            bytes=m.bytes,
            scripts=any(is_script(f.path) for f in m.files),
        )
        for m in metas
        if m.skill_id in skills and m.revision <= skills[m.skill_id].revision
    }


def check_attachable(
    skills: Mapping[str, Skill], principal: Principal, tenant: str
) -> dict[str, Skill]:
    """주체가 이 테넌트에서 볼 수 있는 Skill 이다. 빠진 것은 검증이 `unknown_skill` 로 알린다.
    보이는데 consume 이 없으면 `SkillNotConsumable` 이다."""
    shown = {s.id: s for s in visible(principal, tenant, skills.values())}
    for skill in shown.values():
        if not can(principal, tenant, skill, "consume"):
            raise SkillNotConsumable(skill)
    return shown


# -- 검증 (5절 표) -------------------------------------------------------------------------


def check_skills(
    agents: Sequence[AgentSkills], *, filesystem: bool, revisions: Mapping[Pin, PinnedSkill]
) -> list[Finding]:
    """5절 표의 규칙 전부다. `filesystem` 은 메인의 파일 도구 스위치이고 `revisions` 는 문맥이 미리
    읽은 메타다. 표식은 그 Skill 을 붙인 에이전트 노드에 붙고, 그래프 전체의 상한은 표식이 없다."""
    attached = [a for a in agents if a.pins]
    out: list[Finding] = []
    first: dict[str, tuple[int, str]] = {}  # Skill id → (처음 본 리비전, 노드)
    owners: dict[str, str] = {}  # Skill 이름 → 그 이름의 Skill id
    for agent in attached:
        out.extend(_agent_findings(agent, filesystem))
        for pin in agent.pins:
            out.extend(_pin_findings(agent.node_id, pin, revisions, first, owners))
    out.extend(_total_findings(attached, revisions))
    return out


def _agent_findings(agent: AgentSkills, filesystem: bool) -> list[Finding]:
    node = f"agent {agent.node_id!r}"
    out: list[Finding] = []
    if not filesystem:
        message = (
            f"{node}: skills are attached, but the main agent has builtin_tools.filesystem off, "
            "so the agent has no read_file to open a skill; turn the file tools on or detach "
            "the skills"
        )
        out.append(Finding(SKILLS_FILESYSTEM_DISABLED, message, node_id=agent.node_id))
    if len(agent.pins) > MAX_PER_AGENT:
        message = (
            f"{node} attaches {len(agent.pins)} skills; at most {MAX_PER_AGENT} are allowed "
            "because every skill description goes into each model call"
        )
        out.append(Finding(TOO_MANY_SKILLS, message, node_id=agent.node_id))
    return out


def _pin_findings(
    node_id: str,
    pin: SkillPin,
    revisions: Mapping[Pin, PinnedSkill],
    first: dict[str, tuple[int, str]],
    owners: dict[str, str],
) -> list[Finding]:
    node = f"agent {node_id!r}"
    out: list[Finding] = []
    revision, other = first.setdefault(pin.id, (pin.revision, node_id))
    if revision != pin.revision:
        message = (
            f"{node}: skill {pin.id!r} is pinned at r{pin.revision}, but agent {other!r} pins "
            f"r{revision}; pin one revision everywhere"
        )
        out.append(Finding(SKILL_REVISION_MISMATCH, message, node_id=node_id))
    skill = revisions.get((pin.id, pin.revision))
    if skill is None:
        message = f"{node}: skill {pin.id!r} r{pin.revision} does not exist or is not visible"
        return [*out, Finding(UNKNOWN_SKILL, message, node_id=node_id)]
    owner = owners.setdefault(skill.name, pin.id)
    if owner != pin.id:
        message = (
            f"{node}: skills {owner!r} and {pin.id!r} are both named {skill.name!r}, so their "
            "folders would collide; detach one or clone it under another name"
        )
        out.append(Finding(SKILL_NAME_CONFLICT, message, node_id=node_id))
    if skill.scripts:
        message = (
            f"{node}: skill {skill.name!r} has script files; scripts do not run, so only its "
            "instructions are used"
        )
        out.append(Finding(SKILL_SCRIPTS_INERT, message, "warning", node_id))
    if skill.latest > pin.revision:
        message = (
            f"{node}: skill {skill.name!r} is pinned at r{pin.revision}; "
            f"r{skill.latest} is the latest"
        )
        out.append(Finding(SKILL_BEHIND, message, "warning", node_id))
    return out


def _total_findings(
    attached: Sequence[AgentSkills], revisions: Mapping[Pin, PinnedSkill]
) -> list[Finding]:
    """그래프 전체의 상한이다. 문서의 최상위 `skills[]` 개수와 설정 볼륨에 쓰는 양을 묶는다."""
    distinct = {(p.id, p.revision) for a in attached for p in a.pins}
    out: list[Finding] = []
    count = len({skill_id for skill_id, _ in distinct})
    if count > MAX_PER_GRAPH:
        message = (
            f"the graph attaches {count} different skills; at most {MAX_PER_GRAPH} are allowed"
        )
        out.append(Finding(TOO_MANY_SKILLS, message))

    def size(pin: Pin) -> int:
        skill = revisions.get(pin)
        return skill.bytes if skill is not None else 0

    total = sum(size(pin) for pin in distinct)
    expanded = sum(size((p.id, p.revision)) for a in attached for p in a.pins)
    if total > MAX_DISTINCT_BYTES:
        message = (
            f"the attached skills hold {total} bytes; at most {MAX_DISTINCT_BYTES} are allowed"
        )
        out.append(Finding(SKILLS_TOO_LARGE, message))
    if expanded > MAX_EXPANDED_BYTES:
        message = (
            f"the skill files written for every agent add up to {expanded} bytes; at most "
            f"{MAX_EXPANDED_BYTES} are allowed"
        )
        out.append(Finding(SKILLS_TOO_LARGE, message))
    return out


# -- 문서 기능 `skills` (6절) --------------------------------------------------------------


def attach_skills(
    document: dict[str, Any], spec: AgentSpec, revisions: Mapping[Pin, PinnedSkill]
) -> dict[str, Any]:
    """`skills` 가 허락됐을 때의 붙이는 함수다. 최상위 `skills[]` 는 서로 다른 Skill 마다 하나이고
    graph.nodes 순서다. 메인과 서브에이전트에는 붙인 Skill 이름을 싣는다. 검증을 지난 그래프만
    받는다. 받은 문서는 바꾸지 않는다."""
    final = copy.deepcopy(document)
    agents = attached_skills(spec)
    names = {a.name: [revisions[(p.id, p.revision)].name for p in a.pins] for a in agents}
    main = next(a for a in agents if a.role == "main")
    final["main"]["skills"] = names[main.name]
    for sub in final["subagents"]:
        sub["skills"] = names.get(sub["name"], [])
    entries: dict[str, dict[str, Any]] = {}
    for pin in (p for a in agents for p in a.pins):
        skill = revisions[(pin.id, pin.revision)]
        entries.setdefault(
            pin.id,
            {
                "name": skill.name,
                "id": skill.id,
                "revision": skill.revision,
                "digest": skill.digest,
                "bytes": skill.bytes,
            },
        )
    final["skills"] = list(entries.values())
    return final


def skill_bundle(name: str, rev: SkillRevision) -> dict[str, Any]:
    """init 의 Skill 받기 응답이다(7절, agent.schema.json 의 `$defs/skill_bundle`)."""
    return {
        "name": name,
        "id": rev.skill_id,
        "revision": rev.revision,
        "digest": rev.digest,
        "files": [{"path": f.path, "content": f.content} for f in rev.files],
    }


# -- 스토어 읽기 --------------------------------------------------------------------------


class AgentSkillStore:
    """Agent 가 핀한 Skill 을 스토어에서 읽는다. `AgentService` 가 하나를 들고 위임한다."""

    def __init__(self, store: MongoSkillStore):
        self.store = store

    async def pinned(
        self, tenant: str, spec: AgentSpec, principal: Principal | None
    ) -> dict[Pin, PinnedSkill]:
        """그래프가 핀한 Skill 리비전의 메타다(5절). 주체가 있으면(저장·배포·재배포·검사) 보이지
        않는 Skill 은 빼서 검증이 `unknown_skill` 을 내게 하고, 보이는데 consume 이 없으면 422
        `skill_not_consumable` 이다. init 의 컴파일에는 주체가 없어 접근을 다시 보지 않는다. 핀은
        배포 때 허락받은 것이기 때문이다."""
        refs = pinned_refs(spec)
        skills = await self.store.get_many_unscoped(sorted({skill_id for skill_id, _ in refs}))
        if principal is not None:
            skills = check_attachable(skills, principal, tenant)
        metas = await self.store.pinned_metas([ref for ref in refs if ref[0] in skills])
        return pinned_skills(skills, metas)

    async def access_revoked(self, tenant: str, deployed: AgentSpec | None) -> list[str]:
        """배포가 핀한 Skill 가운데 이 테넌트가 consume 경계 밖으로 빠진 것의 상태 경고다(2절). 핀은
        배포 때 허락받은 것이라 뜬 것은 건드리지 않고 다음 저장·재배포에서 422 다. 허브의
        `hub_access_revoked` 와 같은 규칙이다."""
        pins = skill_pins(deployed) if deployed is not None else {}
        skills = await self.store.get_many_unscoped(list(pins))
        return revoked_lines(SKILL_ACCESS_REVOKED, tenant, skills.values())

    async def bundle(self, pinned: AgentSpec, name: str) -> dict[str, Any]:
        """init 의 Skill 받기다(7절). 핀된 리비전이 그 이름으로 붙인 Skill 리비전 하나를
        `$defs/skill_bundle` 모양으로 준다. 핀에 없는 이름은 404 다. 접근은 다시 보지 않는다. 핀은
        배포 때 허락받은 것이라 정책이 좁아져도 파드가 다시 뜬다."""
        rev = await self.store.pinned_named(list(skill_pins(pinned).items()), name)
        if rev is None:
            raise ServiceError(404, "not_found", f"no skill named {name!r} is pinned by this agent")
        return skill_bundle(name, rev)
