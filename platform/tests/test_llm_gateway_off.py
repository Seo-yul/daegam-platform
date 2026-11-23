"""게이트웨이를 끈 묶음 A 배포는 Agent 파드 템플릿을 바꾸지 않는다 (P38 Goals 1, Tests).

묶음 A(F1~F4)는 게이트웨이로 호출을 하나도 보내지 않는다. 그래서 Agent 의 Deployment·토큰
Secret·Service·NetworkPolicy 렌더는 이 단계 앞 커밋의 렌더와 바이트까지 같아야 한다. 스냅샷은
그 커밋에서 `tests/helpers/agent_pod_snapshot.py` 가 낸 값이다. 그 뒤 일부러 바꾼 것은 스냅샷에도
옮겼다. P44 8절의 준비 검사(첫 1초부터 2초마다, 실패 문턱 15)와, Gateway 소스가 있는 파드의 init
주소가 싣는 Gateway 인증 방식이다.
"""

from __future__ import annotations

import json

from tests.helpers.agent_pod_snapshot import snapshot
from tests.helpers.consts import REPO

BEFORE = REPO / "platform" / "tests" / "fixtures" / "agent-pod-templates-before-p38.json"


def test_agent_renders_match_the_render_before_this_phase():
    before = json.loads(BEFORE.read_text())
    now = json.loads(json.dumps(snapshot(), sort_keys=True, ensure_ascii=False))
    assert sorted(now) == sorted(before)
    for name in before:
        assert now[name] == before[name], name
