"""내부 MCPServer 를 직접 부를 때 쓰는 서비스 토큰 발급 CLI (D41). e2e 와 사람이 쓴다.

플랫폼은 서비스 토큰을 내는 API 를 두지 않는다. 플랫폼 네임스페이스의 서명 키 Secret
`daegam-local-signing` 을 읽을 수 있는 사람이 스스로 서명한다. 그 Secret 을 읽을 수 있다는 것이
곧 권한이다.

실행: uv run --project platform python deploy/dev/lab/svc_token.py [--context CTX] [--namespace NS]
        [--tenant TENANT] [--sub SUB | --human-sub SUB] [--ttl SECONDS]
        [--server ID | --audience AUD] [--groups G1,G2 | --no-groups] [--act SUB]
        [--bound-to TARGET] [--agent-id ID] [--roles R1,R2]
  기본은 컨텍스트 orbstack, 네임스페이스 daegam-system, 테넌트 default, sub svc:dev:<사용자>,
  10분, audience daegam-internal:<tenant>. 발급자(iss)는 플랫폼 ConfigMap 의 DAEGAM_PLATFORM_ISSUER,
  없으면 http://daegam-platform.<ns>.svc:9000 이다.
  --server 는 내부 서버(MCP Server, 허브 image 항목, 공유 복제본 기록)의 id 이고, 토큰은 플랫폼의
  소비자 토큰처럼 테넌트 audience 와 그 서버의 audience daegam-internal:<tenant>:<id> 를 함께
  싣는다 (P50 9절). 자기 audience 로 옮겨 간 서버는 테넌트 audience 하나인 토큰을 거절한다.
  LLM 게이트웨이의 사용량 수집 토큰은 --audience daegam-telemetry --sub svc:llm-gateway:<클러스터 id>
  다 (P37 8절). 테넌트는 읽지 않는다.
  --human-sub 는 svc: 로 시작하지 않는 sub 로 서명한다. audience 가 맞아도 서비스 토큰이 아닌
  토큰을 테넌트 울타리가 막는지 볼 때만 쓴다 (P50 11절).
  턴 위임 토큰의 모양(P53 1절)은 --act·--bound-to·--no-groups 로 만든다. --act SUB 는 act 클레임
  {"sub": SUB} 를, --bound-to TARGET 은 bound_to(gateway:<id>, mcp-server:<id>, agent:<id>)를 싣고,
  --no-groups 는 groups 를 싣지 않는다. Gateway 와 노출 Agent 가 묶이지 않은 위임 토큰을 거절하는지
  e2e 가 볼 때 쓰고, 그 경계가 믿는 사람 발급자(--issuer)와 audience daegam-mcp(--audience)를
  함께 준다.
  Tool 규칙(P52 2·3절)의 호출자 모양은 --agent-id·--roles 로 만든다. --agent-id ID 는 --act 가
  있으면 act 레코드에 agent_id 를 더하고({"sub": ACT, "agent_id": ID}), 없으면 agent_id 클레임을 싣는다.
  --roles R1,R2 는 daegam_roles 에 그 테넌트의 역할 그룹(daegam-role:<tenant>:<role>)을 싣는다.
stdout 에 JWT 한 줄을 찍는다.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import subprocess
import sys

from daegam.auth.rbac import ROLES_CLAIM, role_group
from daegam.auth.service_token import service_claims, service_sub
from daegam.auth.signer import PlatformSigner
from daegam.services.svc_tokens import token_audiences


def _kubectl(context: str, *args: str) -> str:
    return subprocess.check_output(["kubectl", "--context", context, *args], text=True)


def _signing_key(context: str, namespace: str) -> str:
    raw = _kubectl(
        context,
        "-n",
        namespace,
        "get",
        "secret",
        "daegam-local-signing",
        "-o",
        "jsonpath={.data.private\\.pem}",
    )
    return base64.b64decode(raw).decode()


def _issuer(context: str, namespace: str) -> str:
    try:
        data = json.loads(
            _kubectl(context, "-n", namespace, "get", "configmap", "daegam-platform-config", "-o", "json")
        )["data"]
    except (subprocess.CalledProcessError, KeyError, ValueError):
        data = {}
    issuer = (data.get("DAEGAM_PLATFORM_ISSUER") or "").rstrip("/")
    return issuer or f"http://daegam-platform.{namespace}.svc:9000"


def _audience(a: argparse.Namespace) -> str | list[str]:
    """--audience 가 있으면 그것 하나이고, 아니면 플랫폼의 소비자 토큰과 같다(`token_audiences`).
    --server 가 없으면 테넌트 audience 하나다."""
    if a.audience:
        return a.audience
    audiences = token_audiences(a.tenant, a.server)
    return audiences[0] if len(audiences) == 1 else audiences


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--context", default="orbstack")
    ap.add_argument("--namespace", default="daegam-system")
    ap.add_argument("--tenant", default="default")
    subs = ap.add_mutually_exclusive_group()
    subs.add_argument("--sub", default=None)
    subs.add_argument("--human-sub", default=None, help="svc: 로 시작하지 않는 sub (울타리 확인용)")
    ap.add_argument("--ttl", type=int, default=600)
    target = ap.add_mutually_exclusive_group()
    target.add_argument("--audience", default=None)
    target.add_argument("--server", default=None, help="내부 서버 id. 그 서버의 audience 를 함께 싣는다")
    ap.add_argument("--groups", default=None, help="쉼표로 나눈 groups 클레임 (기본 daegam-<tenant>)")
    ap.add_argument(
        "--no-groups", action="store_true", help="groups 클레임을 싣지 않는다 (턴 위임 토큰 모양)"
    )
    ap.add_argument("--act", default=None, help='act 클레임 {"sub": ACT} 의 행위자 sub')
    ap.add_argument(
        "--bound-to",
        default=None,
        help="bound_to 클레임 (gateway:<id>, mcp-server:<id>, agent:<id>)",
    )
    ap.add_argument(
        "--agent-id", default=None, help="Agent id. --act 가 있으면 act 레코드에 싣는다 (P52 3절)"
    )
    ap.add_argument(
        "--roles", default=None, help="쉼표로 나눈 역할. daegam_roles 에 그 테넌트의 역할 그룹을 싣는다"
    )
    ap.add_argument(
        "--issuer",
        default=None,
        help="iss 주소. 기본은 ConfigMap 의 DAEGAM_PLATFORM_ISSUER, 없으면 svc 주소. make run 은 http://host.internal:9000",
    )
    a = ap.parse_args(argv[1:])
    signer = PlatformSigner(_signing_key(a.context, a.namespace))
    groups = a.groups.split(",") if a.groups else [f"daegam-{a.tenant}"]
    claims = service_claims(
        issuer=(a.issuer or _issuer(a.context, a.namespace)).rstrip("/"),
        audience=_audience(a),
        sub=a.sub or service_sub("dev", getpass.getuser()),
        tenant=a.tenant,
        groups=groups,
        ttl=a.ttl,
    )
    if a.human_sub:
        # service_claims 는 svc: 접두를 요구하므로 다 만든 클레임의 sub 만 바꾼다
        claims["sub"] = a.human_sub
    if a.no_groups:
        del claims["groups"]
    if a.act:
        claims["act"] = {"sub": a.act, **({"agent_id": a.agent_id} if a.agent_id else {})}
    elif a.agent_id:
        claims["agent_id"] = a.agent_id
    if a.roles:
        claims[ROLES_CLAIM] = [role_group(a.tenant, r) for r in a.roles.split(",")]
    if a.bound_to:
        claims["bound_to"] = a.bound_to
    print(signer.sign(claims))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
