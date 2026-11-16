"""자기 점검의 대상 하나하나 (P40 3절, 8절).

닿았다는 것은 TCP 연결이 맺어졌거나, DNS 답이 왔거나, ICMP 응답이 온 것뿐이다. 거절(k3s 의 정책
컨트롤러는 막은 연결을 거절하므로 샌드박스에는 ECONNREFUSED 로 보인다), 닿을 수 없음, 시간 초과는
모두 막힌 것이다. 소켓을 만들지 못하면 점검을 하지 못한 것(error)이다. 다만 ICMP 소켓을 열 수 없는
노드에서는 그 대상을 건너뛰고(skipped) 적어 둔다.

대상은 IP 글자라 이름을 풀지 않는다. 이름 풀이는 asyncio 가 스레드 풀로 넘기므로, TCP 는
loop.sock_connect 의 IP 글자 지름길로 잇고 UDP·ICMP 는 막히지 않는 connect 를 바로 부른다.
"""

from __future__ import annotations

import asyncio
import errno
import os
import socket
import struct
import time
from dataclasses import dataclass

from daegam_sandbox.config import Target

# 대상을 한꺼번에 여는 소켓 수
CONCURRENCY = 64
# DNS 질의 이름. 답이 무엇이든(NXDOMAIN 이라도) 답이 오면 닿은 것이다
DNS_NAME = "kubernetes.default.svc.cluster.local"


@dataclass
class Outcome:
    target: Target
    reached: bool
    seconds: float
    note: str = ""
    # 점검 자체를 하지 못했다(소켓을 만들지 못함)
    error: bool = False
    skipped: bool = False

    def as_dict(self) -> dict:
        out = {
            "kind": self.target.kind,
            "host": self.target.host,
            "port": self.target.port,
            "reached": self.reached,
            "seconds": round(self.seconds, 3),
        }
        if self.note:
            out["note"] = self.note
        if self.skipped:
            out["skipped"] = True
        if self.error:
            out["error"] = True
        return out


def _family(host: str) -> int:
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def _blocked_note(e: BaseException) -> str:
    if isinstance(e, TimeoutError):
        return "timeout"
    if isinstance(e, ConnectionRefusedError):
        return "refused"
    if isinstance(e, OSError) and e.errno is not None:
        return errno.errorcode.get(e.errno, str(e.errno)).lower()
    return type(e).__name__


def dns_query(ident: int) -> bytes:
    """A 레코드 질의 하나(RD 켬)."""
    header = struct.pack("!HHHHHH", ident, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in DNS_NAME.split(".")) + b"\0"
    return header + qname + struct.pack("!HH", 1, 1)


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def icmp_echo(v6: bool, ident: int) -> bytes:
    """ICMP 에코 요청. IPv6 는 커널이 검사합을 채우고, ping 소켓은 ident 를 바꿔 쓴다."""
    kind = 128 if v6 else 8
    payload = b"daegam-sandbox"
    packet = struct.pack("!BBHHH", kind, 0, 0, ident, 1) + payload
    if v6:
        return packet
    return struct.pack("!BBHHH", kind, 0, _checksum(packet), ident, 1) + payload


async def _datagram(sock: socket.socket, target: Target, packet: bytes) -> None:
    """연결한 데이터그램 소켓으로 보내고 답 하나를 기다린다. 답이 오면 돌아온다."""
    loop = asyncio.get_running_loop()
    await loop.sock_sendall(sock, packet)
    async with asyncio.timeout(target.timeout):
        while True:
            data = await loop.sock_recv(sock, 4096)
            if data:
                return


async def probe(target: Target) -> Outcome:
    started = time.monotonic()

    def done(reached: bool, note: str = "", **flags: bool) -> Outcome:
        return Outcome(target, reached, time.monotonic() - started, note, **flags)

    family = _family(target.host)
    if target.kind == "tcp":
        kind, proto = socket.SOCK_STREAM, 0
    elif target.kind == "dns":
        kind, proto = socket.SOCK_DGRAM, 0
    else:
        kind = socket.SOCK_DGRAM
        proto = socket.IPPROTO_ICMPV6 if family == socket.AF_INET6 else socket.IPPROTO_ICMP
    try:
        sock = socket.socket(family, kind, proto)
    except OSError as e:
        if target.kind == "icmp":
            return done(False, f"no unprivileged ICMP socket ({_blocked_note(e)})", skipped=True)
        if e.errno == errno.EAFNOSUPPORT:
            # 이 주소 체계를 커널이 모르면 파드 안의 무엇도 그 대상에 닿지 못한다
            return done(False, "address family unsupported")
        return done(False, f"socket: {_blocked_note(e)}", error=True)
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    try:
        if target.kind == "tcp":
            async with asyncio.timeout(target.timeout):
                await loop.sock_connect(sock, (target.host, target.port))
            return done(True, "connected")
        if target.kind == "dns":
            sock.connect((target.host, target.port))
            await _datagram(sock, target, dns_query(int.from_bytes(os.urandom(2), "big")))
            return done(True, "answered")
        sock.connect((target.host, 0))
        ident = int.from_bytes(os.urandom(2), "big")
        await _datagram(sock, target, icmp_echo(family == socket.AF_INET6, ident))
        return done(True, "echo reply")
    except (TimeoutError, OSError) as e:
        return done(False, _blocked_note(e))
    finally:
        sock.close()


async def probe_all(targets: tuple[Target, ...]) -> list[Outcome]:
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(target: Target) -> Outcome:
        async with gate:
            return await probe(target)

    return list(await asyncio.gather(*(one(t) for t in targets)))
