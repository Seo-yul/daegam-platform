"""Advanced 편집 잠금 테스트가 함께 쓰는 파드 템플릿 표(test_k8s_merge_locks.py,
test_k8s_merge_render_wipe.py)."""

import pytest

from daegam.k8s.merge import AGENT_LOCKED_PATHS, LOCKED_PATHS, VMCP_LOCKED_PATHS

# 플랫폼이 렌더하는 세 파드 템플릿 (파드 템플릿 키, 잠금 표, 플랫폼 컨테이너 이름, id).
# MCPServer·vMCP 의 podTemplateSpec 과 Agent Deployment 의 template 이다. 새 템플릿은 여기에만
# 더한다
_ROWS = [
    ("podTemplateSpec", LOCKED_PATHS, "mcp", "mcpserver"),
    ("podTemplateSpec", VMCP_LOCKED_PATHS, "vmcp", "vmcp"),
    ("template", AGENT_LOCKED_PATHS, "agent", "agent-deployment"),
]
# (파드 템플릿 키, 잠금 표)
TEMPLATES = [pytest.param(key, table, id=i) for key, table, _c, i in _ROWS]
# (파드 템플릿 키, 잠금 표, 컨테이너 이름)
PLATFORM_CONTAINERS = [pytest.param(key, table, c, id=i) for key, table, c, i in _ROWS]
# 옛 AppArmor 주석. 컨테이너 이름(side)이 키에 들어간다
APPARMOR_ANNOTATION = "container.apparmor.security.beta.kubernetes.io/side"
