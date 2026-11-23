"""릴리스 후보 태그의 `e2e:` 줄이 고른 게이트를 정한다.

늘 도는 게이트(ALWAYS)는 늘 돈다. 나머지는 태그 메시지의 `e2e:` 줄에 이름이나
`all` 이 있을 때만 돌고, 고르면 막는다(NOT_BLOCKING_YET 은 빼고). 줄이 여럿이면
합치고, 이름은 공백이나 쉼표로 나눈다. 모르는 이름이면 2 로 끝난다. 늘 도는 게이트의
이름은 받되 아무것도 바꾸지 않는다. 표준 입력으로 태그 메시지(또는 `e2e:` 줄)를 받는다.

  e2e_select.py check   고른 것을 조각별로 찍는다(태그를 달기 전에 본다)
  e2e_select.py matrix  e2e-cluster.yml 의 plan 잡이 쓰는 matrix JSON 한 줄
  e2e_select.py install scripts/run_e2e_install_extra.sh 가 돌 게이트(공백으로 나눈 한 줄)

platform/tests/test_e2e_select.py 가 단위 테스트한다. 맥의 시스템 python3(3.9)
에서도 돌아야 하므로 3.10 이후 문법을 쓰지 않는다.
"""

from __future__ import annotations

import json
import re
import sys

# 조각마다 늘 도는 막는 게이트. 워크플로가 단계 하나씩으로 부른다
ALWAYS = {
    "core": ("e2e-instance", "e2e-gateway"),
    "agent": ("e2e-chat", "e2e-agent"),
}
# 태그가 고를 때만 도는 k3d 게이트와 그 시간 상한(분). 조각 안에서 이 차례로 돌고, 실패하면
# 조각을 막는다(docs/guide/e2e.md 20절). e2e-netpol 은 k3d 클러스터를 지운 뒤 따로 돈다.
# auth 조각은 인증 모드를 바꾸는 게이트만 두고, 고른 것이 없으면 뜨지 않는다. sandbox 조각도
# 그렇다. e2e-sandbox 는 플랫폼 ConfigMap 을 고치고 노드의 메모리를 작업 공간에 비우므로
# (플랫폼과 daegam-docparse 를 한 벌씩으로 줄인다) 다른 게이트와 클러스터를 나누지 않는다.
# provisioning 조각도 그렇다. e2e-provisioning(P44)은 local 모드와 쉬는 창, 매일 깨우기의 주기,
# 플랫폼 스위치를 ConfigMap 에서 바꾸므로 그 값이 다른 게이트의 Agent 에 닿지 않게 혼자 돈다.
# e2e-evaluation(P45)이 그 뒤에 같은 조각에서 돈다. 평가 미리 보기도 테넌트의 미리 보기 자리를 쓰므로
# 미리 보기를 띄우는 게이트끼리 한 클러스터에 모은다. e2e-provisioning 의 EXIT trap 이 모드와 설정을
# 되돌린 뒤 header 모드 그대로 돈다.
# exchange 조각도 그렇다. e2e-exchange(P57)는 local 모드로 바꾸고 게시된 Gateway 를 여러 번 다시 배포하며
# 재적용이 사슬을 따라오기를 기다리므로 다른 게이트의 Gateway 와 클러스터를 나누지 않는다.
# 상한은 아래 BEFORE_PICKED·KILL_GRACE 와 합쳐 조각이 잡의 시간 안에 끝나게 잡는다
# (platform/tests/test_e2e_select.py 가 e2e-cluster.yml 의 timeout-minutes 와 맞대 본다)
OPTIONAL = {
    # 상한 36분은 스크립트의 기다림을 더한 값(약 25-35분)이고 아직 재지 않았다. NOT_BLOCKING_YET 에서
    # 빼기 전에 k3d 에서 잰 값으로 고친다
    "core": (("e2e-netpol", 15), ("e2e-llm-gateway", 36)),
    "agent": (
        ("e2e-agent-redis", 10),
        ("e2e-chat-redis", 5),
        ("e2e-knowledge", 6),
        ("e2e-assistant", 6),
        ("e2e-ui-full", 20),
    ),
    "auth": (
        ("e2e-mcp-auth", 11),
        ("e2e-agent-oidc", 13),
        ("e2e-assistant-oidc", 8),
        # 1.1.0-rc6 에서 691 s 였고 P52 의 11r(규칙을 둔 서버를 세 번 굴린다)이 몇 분을 더한다. P56 의 11c(서버·Gateway·
        # Agent 를 하나씩 배포하고 스크립트의 감사 기록을 기다린다)는 아직 재지 않았고 약 4분을 어림해 더했다. 24분이 auth
        # 조각이 잡의 시간 안에 받는 끝이다
        ("e2e-agent-local", 24),
        # 상한 35분은 P53b 8절이 정한 몫이다(auth 조각의 남은 시간 40분에서 KILL 유예를 뺀 것). 발급자를 세 번 바꾸고
        # 그때마다 제어면과 vMCP 를 굴린다. 아직 재지 않았다. NOT_BLOCKING_YET 에서 빼기 전에 k3d 에서 잰 값으로 고친다
        ("e2e-oidc-platform", 35),
        # 개인 MCP 토큰(P32). 설치본 갈래(INSTALL)가 건너뛰는 단계(STEP 5 의 파드 다시 띄우기, STEP 8 의 내부 홉, STEP 9 의
        # 느린 배포 도중의 삭제)를 여기서 본다. local 모드로 바꾸고 STEP 9 가 ToolHive 오퍼레이터를 잠시 0 으로 내리므로
        # 조각의 마지막에 돈다. 상한 18분은 스크립트의 기다림(모드 바꾸기와 되돌리기, 대상 배포, 파드 다시 띄우기 셋, JWKS
        # 에서 키가 빠지기까지 2분, STEP 9 의 5분)을 어림한 값이고 아직 재지 않았다. NOT_BLOCKING_YET 에서 빼기 전에 k3d 에서
        # 잰 값으로 고친다
        ("e2e-pat", 18),
    ),
    "sandbox": (("e2e-sandbox", 30),),
    # 상한 60분은 스크립트의 기다림을 더한 값(약 40분, 깨우기 시간 재기 10번 포함)에 P44b 의 미리 보기
    # 단계(규칙을 둔 서버 굴리기, 미리 보기 둘, 끝내기 워커, 약 10분)를 더한 것이고 아직 재지 않았다.
    # NOT_BLOCKING_YET 에서 빼기 전에 k3d 에서 잰 값으로 고친다
    # e2e-evaluation 의 상한 30분은 스크립트의 기다림(Agent 의 배포와 재배포 다섯, 평가 실행 셋과 그 미리 보기의 기동, 약
    # 15-20분)을 어림한 값이고 아직 재지 않았다. NOT_BLOCKING_YET 에서 빼기 전에 k3d 에서 잰 값으로 고친다
    "provisioning": (("e2e-provisioning", 60), ("e2e-evaluation", 30)),
    # 상한 40분은 스크립트의 기다림(모드 바꾸기, Gateway 다섯의 배포와 다시 배포 넷, 재적용이 소비자와 사슬을
    # 따라오는 틱들, 약 25-30분)을 어림한 값이고 아직 재지 않았다. NOT_BLOCKING_YET 에서 빼기 전에 k3d 에서 잰 값으로 고친다
    "exchange": (("e2e-exchange", 40),),
}
# 함께 고르면 덮는 쪽만 도는 게이트. 덮는 게이트가 같은 단계를 모두 돈다. header 모드의
# e2e-assistant 는 STEP 0~8 이고 e2e-assistant-oidc 는 같은 STEP 0~8 뒤에 9~12 를 돈다
# (docs/guide/e2e.md 5절). header 모드만의 확인(토큰 없는 GET /api/clusters 가 200)은
# e2e-agent STEP 1 이 후보마다 본다
COVERED_BY = {"e2e-assistant": "e2e-assistant-oidc"}
# 고른 게이트 앞에 드는 시간(분): 설치와 늘 도는 게이트. 1.1.0-rc6 에서 잰 값(core 786 s,
# agent 704 s, auth 315 s, release-images run 41236728641)에 여유를 더했다
# sandbox 는 아직 재지 않았다. auth 의 설치에 샌드박스 이미지 굽기를 더해 어림했다. provisioning 은
# mock-oidc 가 없는 auth 의 설치와 같게 어림했다. exchange 도 그렇다. core 는
# Agent 런타임 이미지 굽기(P85 B2 부터 모든 조각이 굽는다)를 어림해 더했다. core 에는 e2e-gateway 의 8c(P56: 서버와
# Gateway 의 배포, code mode 가 닫히는 재적용 두 번)의 약 6분도 어림해 더했다
BEFORE_PICKED = {"core": 26, "agent": 15, "auth": 8, "sandbox": 10, "provisioning": 8, "exchange": 8}
# 시간 상한이 지나 TERM 을 보낸 뒤 KILL 까지 기다리는 시간(분, timeout -k). auth 의 게이트는
# EXIT trap 이 인증 모드를 되돌리고 플랫폼을 굴린 뒤 재적용을 기다리므로 더 길다. sandbox 의
# EXIT trap 도 ConfigMap 과 벌 수를 되돌리고 플랫폼을 굴린다. provisioning 의 EXIT trap 은 Agent
# 다섯을 지운 뒤 모드와 설정을 되돌리고 제어면을 굴려 재적용을 기다린다. exchange 의 EXIT trap 은 Gateway 다섯과
# 게시를 걷은 뒤 모드를 되돌리고 제어면을 굴려 재적용을 기다린다
KILL_GRACE = {"core": 2, "agent": 2, "auth": 6, "sandbox": 6, "provisioning": 6, "exchange": 6}
# 고른 게이트 뒤의 진단·결과 올리기·요약 단계에 남기는 시간(분)
AFTER_PICKED = 5
# core 조각이 고른 게이트와 진단 뒤에 도는 차트의 기본 값 설치(P85 12절)의 시간(분). 설치,
# 그것에 대고 도는 e2e-install, 업그레이드, MongoDB root 사용자 가드의 단계 timeout-minutes 를
# 합친 것이고 아직 재지 않았다(platform/tests/test_e2e_select.py 가 워크플로의 값과 맞댄다). 그
# e2e-install STEP 8 이 Agent 를 띄우므로 Agent 런타임 이미지는 모든 조각이 굽는다
DEFAULT_INSTALL = {"core": 41}
# 고른 게이트 가운데 k3d 에서 아직 통과하지 못해 실패해도 조각을 막지 않는 것. 새로 더한
# 게이트는 처음 통과할 때까지 여기 두고, 통과를 기록하는 PR 에서 뺀다(docs/guide/e2e.md
# 20절). e2e-llm-gateway(P38)는 그 첫 통과를 기다리고, e2e-sandbox 와 e2e-provisioning(P44), e2e-oidc-platform
# (P53b), e2e-exchange(P57), e2e-evaluation(P45)은 아직 한 번도 돌지 않았다. e2e-pat 은 설치본에서만 돌았고 k3d 에서는
# 아직 돌지 않았다
NOT_BLOCKING_YET: tuple[str, ...] = (
    "e2e-llm-gateway",
    "e2e-sandbox",
    "e2e-provisioning",
    "e2e-oidc-platform",
    "e2e-exchange",
    "e2e-evaluation",
    "e2e-pat",
)
# 클러스터 안 mock-oidc 가 토큰을 내야 하는 게이트
NEEDS_MOCK_OIDC = ("e2e-mcp-auth", "e2e-agent-oidc", "e2e-assistant-oidc", "e2e-oidc-platform")
# 설치본의 배포와 e2e-install 뒤에 이 차례로 돈다. e2e-pat 과 e2e-sandbox 는 k3d 조각(OPTIONAL)에도 있어, 고르면 두
# 곳에서 돈다. k3d 에서는 로컬 갈래가, 설치본에서는 install 갈래가 돈다. 앞의 셋은 메아리 네임스페이스와
# Platform Settings 를 함께 쓰므로 겹치면 안 된다. e2e-runs 는 설치본에만 있는 게이트다.
# e2e-runs 와 e2e-sandbox 는 플랫폼 ConfigMap 을 게이트 동안 고치고 플랫폼을 굴린다
INSTALL = (
    "e2e-web-tools",
    "e2e-skills",
    "e2e-telemetry",
    "e2e-ui-serving",
    "e2e-pat",
    "e2e-oauth",
    "e2e-runs",
    "e2e-sandbox",
)

SHARDS = ("core", "agent", "auth", "sandbox", "provisioning", "exchange")
NETPOL = "e2e-netpol"


def known() -> list[str]:
    names = [g for gates in ALWAYS.values() for g in gates]
    names += [g for gates in OPTIONAL.values() for g, _ in gates]
    return names + list(INSTALL)


def chosen(message: str) -> set[str]:
    """태그 메시지 -> 도는 고른 게이트 이름. `all` 은 늘 도는 게이트 밖의 게이트 전부다. 덮는
    게이트(COVERED_BY)와 함께 고른 게이트는 빠진다.

    모르는 이름은 ValueError 다.
    """
    words: list[str] = []
    for line in message.splitlines():
        m = re.match(r"\s*e2e:(.*)$", line)
        if m:
            words += [w for w in re.split(r"[\s,]+", m.group(1)) if w]
    names = set(known())
    unknown = sorted(w for w in set(words) if w != "all" and w not in names)
    if unknown:
        raise ValueError(f"unknown e2e gate {', '.join(unknown)}; known: all {' '.join(known())}")
    if "all" in words:
        picked = {g for gates in OPTIONAL.values() for g, _ in gates} | set(INSTALL)
    else:
        picked = set(words)
    return {g for g in picked if COVERED_BY.get(g) not in picked}


def matrix(picked: set[str]) -> dict:
    """고른 게이트 -> e2e-cluster.yml 의 matrix.

    blocking_gates(막는다)와 soft_gates(아직 막지 않는다)는 `이름:분` 의 목록이고 kill_grace 는
    그 조각의 timeout -k 분이다.
    """
    include = []
    for shard in SHARDS:
        gates = [(g, m) for g, m in OPTIONAL[shard] if g in picked and g != NETPOL]
        if shard not in ALWAYS and not gates:
            continue
        netpol = [m for g, m in OPTIONAL[shard] if g == NETPOL and g in picked]
        include.append(
            {
                "shard": shard,
                "mock_oidc": any(g in NEEDS_MOCK_OIDC for g, _ in gates),
                "ui": any(g == "e2e-ui-full" for g, _ in gates),
                "sandbox_image": any(g == "e2e-sandbox" for g, _ in gates),
                "netpol_minutes": netpol[0] if netpol else 0,
                "blocking_gates": _listed(g for g in gates if g[0] not in NOT_BLOCKING_YET),
                "soft_gates": _listed(g for g in gates if g[0] in NOT_BLOCKING_YET),
                "kill_grace": KILL_GRACE[shard],
            }
        )
    return {"include": include}


def _listed(gates) -> str:
    return " ".join(f"{g}:{m}" for g, m in gates)


def install(picked: set[str]) -> list[str]:
    return [g for g in INSTALL if g in picked]


def describe(picked: set[str]) -> str:
    lines = []
    for shard in SHARDS:
        always = list(ALWAYS.get(shard, ()))
        extra = [
            f"{g}(not blocking)" if g in NOT_BLOCKING_YET else g
            for g, _ in OPTIONAL[shard]
            if g in picked
        ]
        if always or extra:
            lines.append(f"k3d {shard}: {' '.join(always + extra)}")
    lines.append(f"install e2e-install-extra: {' '.join(install(picked)) or '-'}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("check", "matrix", "install"):
        print(f"usage: {argv[0]} check|matrix|install < tag-message", file=sys.stderr)
        return 2
    try:
        picked = chosen(sys.stdin.read())
    except ValueError as e:
        print(f"e2e_select: {e}", file=sys.stderr)
        return 2
    if argv[1] == "check":
        print(describe(picked))
    elif argv[1] == "matrix":
        print(json.dumps(matrix(picked), separators=(",", ":")))
    else:
        print(" ".join(install(picked)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
