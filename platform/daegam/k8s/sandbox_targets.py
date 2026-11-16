"""샌드박스 자체 점검의 대상 (P40 8절).

열기마다, 그리고 클러스터 점검의 양성 대조마다 플랫폼이 이미 읽는 목록(노드, 파드, Service)에서
대상을 짓는다. 샌드박스는 이 대상 어느 것에도 닿지 않아야 한다.

- 들어갈 노드(7절)의 주소마다: 모든 NodePort, 모든 hostPort, hostNetwork 컨테이너의 모든
  포트, 그리고 노드 포트 22, 2379-2380, 6443, 10250, 10255, 10256, 10010. NetworkPolicy API 는
  파드와 그 노드 사이를 정의상 열어 두므로 컨트롤러가 막는지 재야 한다(F0a 2번)
- 메타데이터 주소 `169.254.169.254:80`
- 클러스터 DNS(UDP 질의와 TCP 53)와 `kubernetes` Service. 이 둘은 늘 남기고, 양성 대조가 닿지
  못하면 점검을 하지 못한 것이다(`ALWAYS_KEPT`)
- 플랫폼의 주소(home 만)
- 공개 주소 하나(TCP 와 ICMP)와 공개 resolver 하나

주소는 IP 글자뿐이다. 샌드박스 안에는 이름 풀이가 없다. 서버가 받는 칸은 kind·host·port·timeout
넷이라 `role` 은 env 에 싣지 않는다.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from daegam.k8s.templates.sandbox import TARGETS_MAX

NODE_PORTS = (22, 2379, 2380, 6443, 10250, 10255, 10256, 10010)
METADATA = ("169.254.169.254", 80)
PUBLIC_TCP = ("1.1.1.1", 443)
PUBLIC_RESOLVER = ("8.8.8.8", 53)
PUBLIC_ICMP = "1.1.1.1"
DNS_PORT = 53
# 클러스터 DNS 의 Service. kube-dns 이름은 CoreDNS 를 쓰는 배포판(k3s 포함)도 그대로 둔다
DNS_SERVICE = ("kube-system", "kube-dns")
API_SERVICE = ("default", "kubernetes")
NODE_ADDRESS_TYPES = frozenset({"InternalIP", "ExternalIP"})

# 역할. 양성 대조가 닿지 못해도 남기는 둘과 나머지다
ROLE_DNS = "cluster_dns"
ROLE_API = "apiserver"
ROLE_NODE = "node"
ROLE_METADATA = "metadata"
ROLE_PLATFORM = "platform"
ROLE_PUBLIC = "public"
ALWAYS_KEPT = frozenset({ROLE_DNS, ROLE_API})


@dataclass(frozen=True)
class Target:
    kind: str
    host: str
    port: int | None
    role: str
    # 자기 노드(열기가 잡은 노드)의 대상인가. 상한을 넘으면 이것을 먼저 남긴다
    own_node: bool = False

    @property
    def key(self) -> tuple[str, str, int | None]:
        return (self.kind, self.host, self.port)

    def env(self) -> dict[str, Any]:
        """파드 env `SANDBOX_TARGETS` 의 항목 하나."""
        out: dict[str, Any] = {"kind": self.kind, "host": self.host}
        if self.port is not None:
            out["port"] = self.port
        return out


def _ip(value: Any) -> str | None:
    try:
        return str(ipaddress.ip_address(str(value)))
    except ValueError:
        return None


def node_addresses(node: Mapping[str, Any]) -> list[str]:
    """노드의 InternalIP·ExternalIP. IP 가 아닌 값은 버린다."""
    out = []
    for addr in (node.get("status") or {}).get("addresses") or []:
        if addr.get("type") in NODE_ADDRESS_TYPES and (ip := _ip(addr.get("address"))):
            out.append(ip)
    return sorted(set(out))


def _port_target(host: str, port: Any, protocol: Any, *, own: bool) -> Target | None:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return None
    # UDP 는 답이 오면 닿은 것이다. DNS 질의 하나를 보낸다. DNS 가 아닌 UDP 서버는 답하지 않을 수
    # 있고, 그러면 양성 대조도 닿지 못해 그 대상은 빠진다
    kind = "dns" if str(protocol or "TCP").upper() == "UDP" else "tcp"
    return Target(kind, host, port, ROLE_NODE, own)


def _service_ip(service: Mapping[str, Any]) -> str | None:
    spec = service.get("spec") or {}
    ip = spec.get("clusterIP")
    return _ip(ip) if ip and ip != "None" else None


def _find_service(services: Iterable[Mapping[str, Any]], ns: str, name: str) -> Mapping[str, Any]:
    for s in services:
        meta = s.get("metadata") or {}
        if meta.get("namespace") == ns and meta.get("name") == name:
            return s
    return {}


def platform_address(
    platform_internal_url: str, services: Iterable[Mapping[str, Any]]
) -> tuple[str, int] | None:
    """플랫폼 주소의 (IP, 포트). `<svc>.<ns>.svc[.cluster.local]` 꼴이면 그 Service 의
    ClusterIP 이고 IP 글자면 그대로다. 풀 수 없으면 None 이다."""
    try:
        parts = urlsplit(platform_internal_url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    port = port or (443 if parts.scheme == "https" else 80)
    if ip := _ip(host):
        return ip, port
    labels = host.split(".")
    if len(labels) >= 3 and labels[2] == "svc":
        ip = _service_ip(_find_service(services, labels[1], labels[0]))
        if ip:
            return ip, port
    return None


def build_targets(
    *,
    eligible: Iterable[Mapping[str, Any]],
    pods: Iterable[Mapping[str, Any]],
    services: Iterable[Mapping[str, Any]],
    platform: tuple[str, int] | None = None,
    own_node: str | None = None,
) -> list[Target]:
    """자체 점검 대상. own_node 를 주면(열기가 잡은 노드) 상한(`TARGETS_MAX`)을 넘을 때 그 노드의
    대상을 먼저 남긴다. 같은 (kind, host, port) 는 하나다."""
    services = list(services)
    out: dict[tuple[str, str, int | None], Target] = {}

    def add(target: Target | None) -> None:
        if target is not None and target.key not in out:
            out[target.key] = target

    if dns := _service_ip(_find_service(services, *DNS_SERVICE)):
        add(Target("dns", dns, DNS_PORT, ROLE_DNS))
        add(Target("tcp", dns, DNS_PORT, ROLE_DNS))
    api = _find_service(services, *API_SERVICE)
    if api_ip := _service_ip(api):
        port = next(iter((api.get("spec") or {}).get("ports") or []), {}).get("port", 443)
        add(Target("tcp", api_ip, int(port), ROLE_API))
    add(Target("tcp", METADATA[0], METADATA[1], ROLE_METADATA))
    if platform is not None:
        add(Target("tcp", platform[0], platform[1], ROLE_PLATFORM))
    add(Target("tcp", PUBLIC_TCP[0], PUBLIC_TCP[1], ROLE_PUBLIC))
    add(Target("dns", PUBLIC_RESOLVER[0], PUBLIC_RESOLVER[1], ROLE_PUBLIC))
    add(Target("icmp", PUBLIC_ICMP, None, ROLE_PUBLIC))

    nodes = {str((n.get("metadata") or {}).get("name") or ""): n for n in eligible}
    addresses = {name: node_addresses(n) for name, n in nodes.items()}
    node_ports = [
        (p.get("nodePort"), p.get("protocol"))
        for s in services
        if (s.get("spec") or {}).get("type") in ("NodePort", "LoadBalancer")
        for p in (s.get("spec") or {}).get("ports") or []
    ]
    for name in sorted(nodes, key=lambda n: (n != own_node, n)):
        own = name == own_node
        for ip in addresses[name]:
            for port in NODE_PORTS:
                add(Target("tcp", ip, port, ROLE_NODE, own))
            for port, protocol in node_ports:
                add(_port_target(ip, port, protocol, own=own))
    for pod in pods:
        spec = pod.get("spec") or {}
        node = spec.get("nodeName")
        if node not in nodes:
            continue
        host_network = bool(spec.get("hostNetwork"))
        for container in [*(spec.get("initContainers") or []), *(spec.get("containers") or [])]:
            for p in container.get("ports") or []:
                port = p.get("hostPort") or (p.get("containerPort") if host_network else None)
                for ip in addresses[node]:
                    add(_port_target(ip, port, p.get("protocol"), own=node == own_node))
    return _capped(list(out.values()))


def _capped(targets: list[Target]) -> list[Target]:
    """상한을 넘으면 늘 남기는 것, 자기 노드의 것, 나머지 차례로 남긴다."""
    if len(targets) <= TARGETS_MAX:
        return targets
    rank = {
        t.key: (t.role not in ALWAYS_KEPT, not t.own_node, t.role == ROLE_NODE) for t in targets
    }
    return sorted(targets, key=lambda t: rank[t.key])[:TARGETS_MAX]


def target_env(targets: Iterable[Target]) -> list[dict[str, Any]]:
    return [t.env() for t in targets]
