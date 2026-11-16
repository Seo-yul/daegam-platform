"""/config/agent.json 을 읽어 스키마와 참조 무결성을 검사한다.

경로는 env `DAEGAM_AGENT_CONFIG` 로 덮어쓴다. 스키마는 이 저장소의
`agent-runtime/schema/agent.schema.json` 이며 런타임은 그 계약을 바꾸지 않는다.
schema_version 은 사다리다. "1" 은 knowledge 도 장기 기억도 없고, "2" 는 knowledge 키 셋을
가지며(D32), "3" 은 knowledge 키 셋(없으면 빈 목록)과 memory.long_term 을 함께 가진다(D34).
사다리는 "3" 에서 멈추고 새 런타임 기능은 선택 키 `features` 에 이름으로 선언한다(D65). 모르는
이름은 스키마 검사 전에 `runtime_image_too_old` 로 잡아 옛 이미지가 이름 있는 오류로 멈춘다.
기능 `skills`(D64)의 키 셋(최상위 skills, main.skills, subagents[].skills)은 그 이름이 있을 때만
실린다. 접근자는 키가 없을 때 빈 목록이나 None 을 준다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from runtime.features import (
    CODE_EXECUTION,
    SKILLS,
    UNKNOWN_FEATURE_CODE,
    WEB_GUARD,
    unknown_features,
)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "agent.schema.json"
DEFAULT_CONFIG_PATH = "/config/agent.json"
ENV_CONFIG_PATH = "DAEGAM_AGENT_CONFIG"
KNOWLEDGE_VERSIONS = ("2", "3")
"""knowledge 키 셋을 가진 schema_version 이다. "1" 에는 그 키가 없다(D32, D34 의 사다리)."""


class ConfigError(Exception):
    """agent.json 을 받아들일 수 없을 때 낸다.

    code 는 기동 로그와 플랫폼의 경고 표시가 읽는 계약이라 값을 바꾸지 않는다. 런타임이 낼 수
    있는 것이 아래 전부다. 새 code 를 더하는 사람이 이 목록을 지나가므로 같은 뜻에 다른 이름이
    붙는 것이 눈에 띈다(knowledge 의 임베딩 키는 모델 쪽과 같은 provider_key_missing 이다).

    문서(config.py): schema · invalid_json · unreadable · duplicate_tool_ref ·
        duplicate_subagent · reserved_subagent · unknown_tool_ref · unknown_subagent ·
        duplicate_knowledge_ref · unknown_knowledge_ref · duplicate_skill · unknown_skill_ref ·
        runtime_image_too_old · web_guard_unpinned_source
    모델(models.py): provider_key_missing · unknown_provider_kind · harness_key
    컴파일(compile.py): harness_profile_not_applied · tool_name_conflict · knowledge_missing
        (한 에이전트의 두 웹 소스가 같은 Tool 이름을 주는 것도 tool_name_conflict 다)
    가드레일(guards.py): guardrail_pattern_invalid · guardrail_rule_unknown ·
        guardrail_rule_position
    판정 모델(judge.py): injection_check_without_tool_safety
    도구(mcp.py): tool_secret_missing
    memory(sessions.py): memory_dsn_missing · memory_dsn_invalid · memory_unreachable ·
        memory_rejected · memory_setup_failed · memory_redis_modules_missing
    knowledge(knowledge.py): provider_key_missing · knowledge_dsn_missing ·
        knowledge_db_unreachable · knowledge_db_rejected
    장기 기억(long_term.py): long_term_memory_missing · long_term_filesystem_disabled ·
        long_term_tenant_missing · long_term_tenant_reserved · long_term_instance_missing ·
        long_term_dsn_missing · long_term_unreachable · long_term_rejected ·
        long_term_schema_missing
        (setup 실패는 체크포인터와 같은 memory_setup_failed·memory_redis_modules_missing 이다)
    Skill(skills.py): skills_filesystem_disabled · skills_missing
    코드 실행(sandbox.py): code_execution_filesystem_disabled · sandbox_url_missing
    인증(auth.py): duplicate_issuer (두 env 묶음이 같은 발급자를 가리킨다)
    기동(boot.py): startup_failed (자원을 다시 열다 ConfigError 가 아닌 예외가 났다)
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def check_features(doc: Any) -> None:
    """문서의 `features` 에 이 이미지가 모르는 이름이 있으면 ConfigError(runtime_image_too_old) 다.

    스키마 검사보다 먼저다. 새 기능이 문서에 새 키를 더하면 옛 이미지는 그 키를 스키마 위반으로
    보는데, 그러면 이유가 `schema` 로 읽혀 이미지가 낡았다는 사실이 가려진다(D65)."""
    missing = unknown_features(doc)
    if missing:
        raise ConfigError(
            UNKNOWN_FEATURE_CODE, f"이 이미지가 모르는 기능이다: {', '.join(sorted(missing))}"
        )


def validate_document(doc: Any) -> None:
    """스키마 위반이 있으면 가장 깊은 위반 하나를 골라 ConfigError(schema) 로 낸다."""
    validator = Draft202012Validator(load_schema())
    errors = sorted(validator.iter_errors(doc), key=lambda e: (-len(e.absolute_path), e.message))
    if errors:
        err = errors[0]
        where = "/".join(map(str, err.absolute_path)) or "<root>"
        raise ConfigError("schema", f"{where}: {err.message}")


def check_references(doc: dict[str, Any]) -> list[str]:
    """스키마가 못 보는 문서 안 참조를 검사한다.

    tools[].ref 유일, subagents[].name 유일, main.tools·subagents[].tools 가 가리키는 ref 존재,
    main.subagents 가 가리키는 이름 존재는 오류다. knowledge 키 셋이 있는 문서("2"·"3")면
    knowledge[].ref 유일과 main.knowledge·subagents[].knowledge 가 가리키는 ref 존재도 본다.
    기능 skills 가 있는 문서면 skills[].name 유일과 main.skills·subagents[].skills 가 가리키는
    이름 존재도 본다. approval·timeouts 의 툴
    이름이 allow 밖이면 기동은 막지 않고 경고 문자열로 돌려준다(그 툴은 어차피 모델에 보이지
    않는다).
    """
    _check_pool(doc, "tools", "ref", dup_code="duplicate_tool_ref", unknown_code="unknown_tool_ref")
    names = [s["name"] for s in doc["subagents"]]
    dup = _first_duplicate(names)
    if dup is not None:
        raise ConfigError("duplicate_subagent", f"subagents[].name {dup!r} 가 두 번 나온다")
    if "general-purpose" in names:
        raise ConfigError("reserved_subagent", "general-purpose 는 deepagents 가 예약한 이름이다")

    known_names = set(names)
    for name in doc["main"]["subagents"]:
        if name not in known_names:
            raise ConfigError(
                "unknown_subagent", f"main.subagents 의 {name!r} 가 subagents[].name 에 없다"
            )
    if doc["schema_version"] in KNOWLEDGE_VERSIONS:
        # knowledge 키 셋이 있는 문서다. "1" 에는 그 키가 아예 없다
        _check_pool(
            doc,
            "knowledge",
            "ref",
            dup_code="duplicate_knowledge_ref",
            unknown_code="unknown_knowledge_ref",
        )
    if SKILLS in doc.get("features", []):
        # 이름은 런타임이 디렉터리 이름으로 쓰므로 문서 안에서 유일해야 한다(D64)
        _check_pool(
            doc, "skills", "name", dup_code="duplicate_skill", unknown_code="unknown_skill_ref"
        )
    warnings: list[str] = []
    for tool in doc["tools"]:
        allow = set(tool["allow"])
        if not allow:
            continue
        for key in ("approval", "timeouts"):
            for tool_name in tool[key]:
                if tool_name not in allow:
                    warnings.append(f"tools[{tool['ref']}].{key} 의 {tool_name!r} 가 allow 에 없다")
    return warnings


def _check_pool(
    doc: dict[str, Any], pool: str, key: str, *, dup_code: str, unknown_code: str
) -> None:
    """풀 `pool[].key` 가 유일하고, 메인과 서브에이전트마다의 같은 이름 목록(`main.<pool>`,
    `subagents[].<pool>`)이 그 값만 가리키는지 본다. 도구·knowledge·Skill 이 같은 규칙이다. 선택 키
    (기능 skills 의 목록)는 없으면 빈 목록으로 본다."""
    values = [item[key] for item in doc.get(pool, [])]
    dup = _first_duplicate(values)
    if dup is not None:
        raise ConfigError(dup_code, f"{pool}[].{key} {dup!r} 가 두 번 나온다")
    known = set(values)
    agents = [("main", doc["main"])] + [(f"subagents[{s['name']}]", s) for s in doc["subagents"]]
    for where, agent in agents:
        for ref in agent.get(pool, []):
            if ref not in known:
                raise ConfigError(
                    unknown_code, f"{where}.{pool} 의 {ref!r} 가 {pool}[].{key} 에 없다"
                )


def _first_duplicate(items: list[str]) -> str | None:
    seen: set[str] = set()
    for item in items:
        if item in seen:
            return item
        seen.add(item)
    return None


@dataclass(frozen=True)
class AgentConfig:
    """검증을 통과한 agent.json 이다. 각 절은 스키마 모양 그대로의 dict 다."""

    raw: dict[str, Any]
    warnings: tuple[str, ...] = ()
    path: Path | None = None
    """문서를 읽은 파일이다. init 이 받은 Skill 은 그 곁의 `skills/` 에 있다(skills.py). 파일 없이
    만든 문서(테스트)는 None 이다."""

    @property
    def schema_version(self) -> str:
        return str(self.raw["schema_version"])

    @property
    def profile(self) -> dict[str, Any]:
        return self.raw["profile"]

    @property
    def instance(self) -> dict[str, Any]:
        return self.raw["instance"]

    @property
    def main(self) -> dict[str, Any]:
        return self.raw["main"]

    @property
    def subagents(self) -> list[dict[str, Any]]:
        return self.raw["subagents"]

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self.raw["tools"]

    @property
    def knowledge(self) -> list[dict[str, Any]]:
        """knowledge[] 다. "1" 문서에는 키가 없으므로 빈 목록이다."""
        return self.raw.get("knowledge", [])

    @property
    def memory(self) -> dict[str, Any] | None:
        return self.raw["memory"]

    @property
    def long_term(self) -> dict[str, Any] | None:
        """memory.long_term 이다(D34). 꺼져 있으면 키가 아예 없으므로 None 이다."""
        memory = self.memory
        return memory.get("long_term") if memory else None

    @property
    def guardrails(self) -> list[dict[str, Any]]:
        return self.raw["guardrails"]

    @property
    def entry(self) -> dict[str, Any]:
        return self.raw["entry"]

    @property
    def service(self) -> dict[str, Any]:
        return self.raw["service"]

    @property
    def stub(self) -> bool:
        return bool(self.raw["stub"])

    @property
    def features(self) -> list[str]:
        """문서 기능 이름이다(D65). 선택 키라 없으면 빈 목록이다."""
        return self.raw.get("features", [])

    @property
    def web_data_tools(self) -> bool:
        """그래프의 어느 에이전트든 데이터 Tool 을 가졌는가(기능 web_guard 의 최상위 키, D64). 키가
        없는 문서는 거짓이다."""
        return bool(self.raw.get("web_data_tools", False))

    @property
    def skills(self) -> list[dict[str, Any]]:
        """붙인 Skill 의 핀 skills[] 다(D64). 기능 skills 가 없는 문서에는 키가 없으므로 빈
        목록이다."""
        return self.raw.get("skills", [])

    @property
    def code_execution(self) -> list[str] | None:
        """코드 실행의 execute 승인 결정이다(P40 9절). 기능 code_execution 이 없는 문서는 None 이고,
        켰지만 승인을 묻지 않으면 빈 목록이다."""
        if CODE_EXECUTION not in self.features:
            return None
        return list(self.raw["code_execution"]["approval"])

    @property
    def filesystem(self) -> bool:
        """메인의 내장 파일 도구 스위치다(D26). 장기 기억, 첨부 문서, Skill 은 이 도구로 파일을
        읽으므로 이것이 켜져 있어야 한다."""
        return bool(self.main["builtin_tools"]["filesystem"])

    @property
    def record_bodies(self) -> bool:
        return bool(self.service["observability"]["record_bodies"])

    def tool_source(self, ref: str) -> dict[str, Any]:
        for tool in self.tools:
            if tool["ref"] == ref:
                return tool
        raise KeyError(ref)

    def main_knowledge_refs(self) -> list[str]:
        """main.knowledge 다. "1" 문서에는 키가 없으므로 빈 목록이다."""
        return self.main.get("knowledge", [])

    def subagent_knowledge_refs(self, name: str) -> list[str]:
        """이름이 name 인 서브에이전트의 knowledge 다. "1" 문서에는 키가 없으므로 빈 목록이다."""
        for sub in self.subagents:
            if sub["name"] == name:
                return sub.get("knowledge", [])
        raise KeyError(name)

    def approvals_for(self, refs: list[str]) -> dict[str, list[str]]:
        """참조한 도구 소스들의 approval 을 합친다. 같은 툴 이름이면 허용 결정을 합집합으로 둔다."""
        merged: dict[str, list[str]] = {}
        for ref in refs:
            for name, decisions in self.tool_source(ref)["approval"].items():
                current = merged.setdefault(name, [])
                for decision in decisions:
                    if decision not in current:
                        current.append(decision)
        return merged

    def timeouts_for(self, refs: list[str]) -> dict[str, int]:
        merged: dict[str, int] = {}
        for ref in refs:
            merged.update(self.tool_source(ref)["timeouts"])
        return merged


def check_web_pins(doc: Any) -> None:
    """기능 web_guard 가 든 문서에서 allow 가 빈 소스는 ConfigError(web_guard_unpinned_source) 로
    막는다(D64).

    빈 allow 는 소스가 주는 Tool 전부를 5분마다 다시 읽는다는 뜻이라 배포 뒤 생긴 웹 Tool 이 가드
    없이 들어온다. 플랫폼은 배포 때의 이름을 박아 내므로 이것은 그 검사를 지나온 문서를 위한
    그물이다. 스키마도 같은 것을 막지만 이름 있는 코드로 알리려고 스키마 검사 전에 부른다. 그래서
    문서 모양을 믿지 않는다."""
    if not isinstance(doc, dict) or WEB_GUARD not in (doc.get("features") or []):
        return
    for source in doc.get("tools") or []:
        if isinstance(source, dict) and not source.get("allow"):
            raise ConfigError(
                "web_guard_unpinned_source",
                f"tools[{source.get('ref')}].allow 가 비었다. web_guard 문서는 이름을 박는다",
            )


def parse_config(doc: Any, *, path: Path | None = None) -> AgentConfig:
    check_features(doc)
    check_web_pins(doc)
    validate_document(doc)
    warnings = check_references(doc)
    return AgentConfig(raw=doc, warnings=tuple(warnings), path=path)


def config_path() -> Path:
    return Path(os.environ.get(ENV_CONFIG_PATH) or DEFAULT_CONFIG_PATH)


def load_config(path: str | Path | None = None) -> AgentConfig:
    target = Path(path) if path is not None else config_path()
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError("unreadable", f"{target}: {e}") from e
    except json.JSONDecodeError as e:
        raise ConfigError("invalid_json", f"{target}: {e}") from e
    return parse_config(doc, path=target)
