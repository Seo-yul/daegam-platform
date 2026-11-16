"""파일 도구의 쓰기 막기 규칙이다(P36 9절 D61, P39 7절 D64).

`create_deep_agent(permissions=...)` 에 넘기는 목록을 한 곳에서 만든다. deepagents 는 이 규칙을 파일
도구 자리에서 보고, 서브에이전트는 제 규칙을 두지 않으면 메인의 목록을 물려받으므로 메인과
서브에이전트가 같은 목록을 받는다. 임시 턴 그래프도 같은 조립(compile.py 의 `build`)을 지난다.

`/uploads/` 는 사용자가 올린 첨부라 에이전트가 쓰거나 고치거나 지우지 못한다. 지우기도 deepagents
가 `write` 로 분류하므로 같은 규칙에 걸린다. 이 규칙이 도구 자리에서 먼저 막고, 백엔드(uploads.py
의 `UploadsBackend`)가 쓰기를 한 번 더 거절한다.

`/skills/` 는 이 파드의 모든 세션이 함께 읽는 Skill 이라 같은 규칙으로 막고 읽기 전용 마운트가
한 번 더 막는다(skills.py). 부모 경로를 통째로 지우는 `delete("/")` 는 deepagents 의 겹침 검사가
막는다. 맨 `/skills` 는 라우트 뿌리(디렉터리)로 가서 쓰기가 EISDIR 로 실패하므로 규칙에 넣지
않는다. 이 규칙도 `/skills/` 라우트가 있는 문서에만 더한다. deepagents 는 실행 backend 에 권한
규칙을 걸 때 모든 경로가 라우트 접두어로 시작해야 받아 주므로, 라우트가 없는 경로가 하나라도
있으면 그 검사를 통과하지 못한다.
"""

from __future__ import annotations

from deepagents import FilesystemPermission

from runtime.config import AgentConfig
from runtime.skills import SKILLS_ROUTE, uses_skills
from runtime.uploads import UPLOADS_ROUTE

WRITE = "write"
DENY = "deny"


def build_permissions(config: AgentConfig) -> list[FilesystemPermission]:
    """메인과 서브에이전트가 받는 파일 도구 규칙이다. 앞의 것이 먼저 맞는다."""
    routes = [UPLOADS_ROUTE, *([SKILLS_ROUTE] if uses_skills(config) else [])]
    paths = [f"{route}**" for route in routes]
    return [FilesystemPermission(operations=[WRITE], paths=paths, mode=DENY)]
