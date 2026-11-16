"""`pods/portforward` 로 원격 클러스터의 Service 나 파드에 닿는 HTTP 클라이언트 (D41, D21 의 둘째
다리).

API 서버 `services/proxy` 는 인증을 마치면 `Authorization` 을 지우고 뒤 홉에 넘긴다(실증). 내부
MCPServer 와 에이전트 런타임이 Bearer 를 요구하므로(D41) 직행이 아닌 경로(kubecontext 로컬 개발,
원격 클러스터)는 이것을 쓴다. Service 의 ready 파드 하나에 웹소켓 터널을 열고, 127.0.0.1 의 임시
포트에서 그 터널로 바이트를 양방향으로 잇는 asyncio 서버를 세운 뒤 그 주소로 가는 httpx 클라이언트를
준다. 헤더는 손대지 않고 지나간다.

경로 판정은 Service 로 가는 `bearer_service_client` 와 파드 하나로 가는 `bearer_pod_client` 두
곳이다. 허브의 tools/list 와 에이전트 런타임 프록시가 앞의 것을, 준비되지 않은 런타임의 이유
읽기(T21)가 뒤의 것을 쓴다. 직행이면 클러스터 클라이언트의 경로에 헤더를 얹고, 아니면 여기 터널이다.

터널 하나에 소켓 하나다. 연결마다 터널을 새로 연다(닫힌 소켓을 다시 쓰면 EBADF, 스파이크 2차).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import logging
import random
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

import httpx2
from starlette.concurrency import run_in_threadpool

from daegam.k8s.client import API_READ_TIMEOUT, ClusterTimeout, KubeClient, exception_chain

logger = logging.getLogger("daegam.portforward")

TunnelFactory = Callable[[], Awaitable[tuple[socket.socket, Callable[[], None]]]]


class PortForwardError(Exception):
    pass


def _target_port(spec: dict[str, Any], port: int) -> int | str:
    """Service 포트의 targetPort. 숫자이거나 컨테이너 포트 이름이다."""
    for p in spec.get("ports") or []:
        if int(p.get("port", 0)) == port:
            return p.get("targetPort", port)
    return port


def _is_ready(pod: dict[str, Any]) -> bool:
    # 지워지는 파드는 Service 의 끝점에서 빠지듯 터널의 후보에서도 뺀다. 내려가는 런타임은 새 연결을
    # 받지 않는다(P78 5절)
    if (pod.get("metadata") or {}).get("deletionTimestamp"):
        return False
    status = pod.get("status") or {}
    if status.get("phase") != "Running":
        return False
    return any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in status.get("conditions") or []
    )


def _container_port(pod: dict[str, Any], name: str) -> int:
    """이름 붙은 targetPort 를 그 파드의 컨테이너 포트에서 푼다."""
    for c in (pod.get("spec") or {}).get("containers") or []:
        for cp in c.get("ports") or []:
            if cp.get("name") == name:
                return int(cp["containerPort"])
    raise PortForwardError(f"pod {pod['metadata']['name']} has no container port named {name!r}")


def ready_pod_for_service(
    client: KubeClient, namespace: str, service: str, port: int
) -> tuple[str, int]:
    """Service 의 selector 로 Ready 파드 하나와 그 파드의 대상 포트를 고른다. 파드는 Ready 인 것
    가운데 무작위다. 첫 파드로 고정하면 원격 채팅이 레플리카 하나에 몰린다."""
    svc = client.get("v1", "Service", service, namespace)
    if svc is None:
        raise PortForwardError(f"service {namespace}/{service} not found")
    spec = svc.get("spec") or {}
    selector = spec.get("selector") or {}
    if not selector:
        raise PortForwardError(f"service {namespace}/{service} has no selector")
    target = _target_port(spec, port)
    ready = [p for p in client.list("v1", "Pod", namespace, labels=dict(selector)) if _is_ready(p)]
    if not ready:
        raise PortForwardError(f"service {namespace}/{service} has no ready pod")
    pod = random.choice(ready)  # 부하 분산이지 보안 난수가 아니다
    name = pod["metadata"]["name"]
    return name, target if isinstance(target, int) else _container_port(pod, target)


# 터널 웹소켓의 핸드셰이크 시한(초). API 서버로 가는 다른 요청의 읽기 시한과 같다 (P65 2절)
TUNNEL_HANDSHAKE_TIMEOUT = API_READ_TIMEOUT


def _bounded_websocket_class() -> type:
    """핸드셰이크에만 시한을 두는 웹소켓. kubernetes stream 모듈(36.0.3)은 시한 없이 연결하고 시한을
    받는 자리도 없어서, 그 모듈이 만드는 WebSocket 을 이것으로 바꾼다. 열린 터널은 지금처럼 시한이
    없다. 터널 위 httpx 클라이언트가 시한을 정한다."""
    from websocket import WebSocket, WebSocketTimeoutException

    class BoundedWebSocket(WebSocket):
        def connect(self, url: str, **options: Any) -> None:
            options.setdefault("timeout", TUNNEL_HANDSHAKE_TIMEOUT)
            try:
                super().connect(url, **options)
            except (TimeoutError, WebSocketTimeoutException) as e:
                limit = options["timeout"]
                raise ClusterTimeout(f"port-forward handshake: no answer within {limit} s") from e
            self.settimeout(None)

    return BoundedWebSocket


def _install_handshake_bound() -> None:
    """kubernetes stream 모듈이 만드는 웹소켓을 시한 있는 것으로 바꾼다. 한 번만, 처음 터널을 열
    때다. 모든 터널이 같은 시한을 원하므로 스레드가 겹쳐도 바뀌는 값이 같다."""
    from kubernetes.stream import ws_client

    if not getattr(ws_client.WebSocket, "_daegam_bounded", False):
        bounded = _bounded_websocket_class()
        bounded._daegam_bounded = True  # type: ignore[attr-defined]
        ws_client.WebSocket = bounded


def _tunnel_api_client(api_client: Any) -> Any:
    """터널 하나만 쓰는 ApiClient 사본. kubernetes stream 모듈(36.0.3)은 터널을 여는 동안
    ApiClient.request 를 웹소켓 함수로 바꿔 끼우고, 끝나면 연 때 본 값으로 되돌린다. 클러스터가
    함께 쓰는 ApiClient 로 열면 그동안 다른 스레드의 REST 요청이 웹소켓으로 가고, 겹친 두 열기
    가운데 늦게 끝난 쪽이 웹소켓 함수를 되돌려 놓아 그 뒤 REST 요청이 모두 실패한다.

    얕은 사본이라 Configuration, REST 클라이언트, 쿠키를 함께 쓰고, 바꿔 끼우는 `request` 는 사본의
    속성으로만 붙는다. 새 ApiClient 를 만들지 않는 까닭은 그 REST 클라이언트 생성이 프록시를 쓰는
    설정에서 프로세스의 `no_proxy` 환경 변수를 썼다 되돌리기 때문이다. 열기가 겹치면 그 쓰기도
    겹친다. 기본 헤더는 사본에 따로 두어 사본에 쓰는 일이 클러스터 클라이언트에 닿지 않게
    한다. 사본은 닫지 않는다. 닫으면 함께 쓰는 스레드 풀을 닫는다."""
    tunnel = copy.copy(api_client)
    tunnel.default_headers = dict(api_client.default_headers)
    return tunnel


def open_tunnel(
    client: Any, namespace: str, pod: str, port: int
) -> tuple[socket.socket, Callable[[], None]]:
    """kubernetes 파이썬 클라이언트의 portforward 로 터널을 연다. (소켓, 닫는 함수). 핸드셰이크는
    `TUNNEL_HANDSHAKE_TIMEOUT` 안에 끝나야 하고, 넘기면 ClusterTimeout 이다 (P65)."""
    from kubernetes import client as k8s
    from kubernetes.stream import portforward

    api_client = getattr(client, "api_client", None)
    if api_client is None:
        raise PortForwardError("this cluster client cannot open a port-forward tunnel")
    _install_handshake_bound()
    try:
        pf = portforward(
            k8s.CoreV1Api(_tunnel_api_client(api_client)).connect_get_namespaced_pod_portforward,
            pod,
            namespace,
            ports=str(port),
        )
    except Exception as e:
        # stream 모듈은 연결 오류를 ApiException(status=0) 으로 감싸고, 본문 없는 그 예외를
        # ApiClient 가 다시 풀다 AttributeError 를 낸다(36.0.3). 시한이었으면 그 이름으로 올린다
        timeout = next((x for x in exception_chain(e) if isinstance(x, ClusterTimeout)), None)
        if timeout is not None:
            raise timeout from e
        raise
    wrapped = pf.socket(port)
    # kubernetes 의 _Socket 은 socketpair 한쪽을 감싼 프록시다. asyncio 에는 진짜 소켓을 준다
    raw: socket.socket = getattr(wrapped, "_socket", wrapped)
    return raw, pf.close


class TunnelBridge:
    """127.0.0.1 의 임시 포트로 들어온 TCP 연결마다 터널을 열어 양방향으로 잇는다."""

    def __init__(self, tunnel_factory: TunnelFactory):
        self.tunnel_factory = tunnel_factory
        self._server: asyncio.AbstractServer | None = None
        self._closers: list[Callable[[], None]] = []
        self._tasks: set[asyncio.Task[None]] = set()
        # 아직 터널이 열리기를 기다리는 연결. 닫을 때 이것을 먼저 닫아야 wait_closed() 가 터널 여는
        # 스레드를 기다리지 않는다
        self._waiting: set[asyncio.StreamWriter] = set()
        self._closing = False
        self.port = 0

    async def __aenter__(self) -> str:
        self._server = await asyncio.start_server(self._handle, host="127.0.0.1", port=0)
        self.port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}"

    async def __aexit__(self, *exc: Any) -> None:
        # 펌프를 먼저 끊는다. 3.12 의 wait_closed() 는 연결이 모두 닫힐 때까지 기다리므로 터널 쪽이
        # EOF 를 안 주면 순서가 반대일 때 요청이 매달린다. 터널을 기다리는 연결도 같은 까닭으로
        # 닫는다. 그 터널은 늦게 열려도 `_handle` 이 `_closing` 을 보고 바로 닫는다
        self._closing = True
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            with contextlib.suppress(BaseException):
                await t
        for w in list(self._waiting):
            w.close()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=5)
        for close in self._closers:
            with contextlib.suppress(Exception):
                close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._waiting.add(writer)
        try:
            raw, close = await self.tunnel_factory()
        except Exception as e:  # noqa: BLE001 - 터널을 못 열면 이 연결만 닫는다
            logger.warning("port-forward tunnel failed: %s", e)
            writer.close()
            return
        finally:
            self._waiting.discard(writer)
        if self._closing:
            # 브리지가 먼저 닫혔다(호출자의 시한). 늦게 열린 터널을 남기지 않는다
            for done in (close, raw.close, writer.close):
                with contextlib.suppress(Exception):
                    done()
            return
        self._closers.append(close)
        t_reader, t_writer = await asyncio.open_connection(sock=raw)
        a = asyncio.create_task(_pump(reader, t_writer))
        b = asyncio.create_task(_pump(t_reader, writer))
        self._tasks.update({a, b})
        try:
            await asyncio.gather(a, b, return_exceptions=True)
        finally:
            self._tasks.difference_update({a, b})
            for w in (writer, t_writer):
                with contextlib.suppress(Exception):
                    w.close()


def _service_host(namespace: str, service: str, port: int) -> str:
    """터널로 가는 요청의 Host. 런타임이 Service 주소로 부른 것처럼 받는다."""
    return f"{service}.{namespace}.svc:{port}"


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            if writer.can_write_eof():
                writer.write_eof()


@contextlib.asynccontextmanager
async def portforward_client(
    client: KubeClient,
    namespace: str,
    service: str,
    port: int,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    read_timeout: float | None = None,
) -> AsyncIterator[httpx2.AsyncClient]:
    """Service 의 ready 파드 하나로 가는 httpx 클라이언트. `base_url` 이 브리지 주소다.
    read_timeout=None 은 timeout 과 같다. SSE 처럼 오래 읽는 호출은 큰 값을 준다."""
    pod_name, target = await run_in_threadpool(
        ready_pod_for_service, client, namespace, service, port
    )
    async with _pod_tunnel_client(
        client,
        namespace,
        pod_name,
        target,
        host=_service_host(namespace, service, port),
        headers=headers,
        timeout=timeout,
        read_timeout=read_timeout,
    ) as hc:
        yield hc


@contextlib.asynccontextmanager
async def _pod_tunnel_client(
    client: KubeClient,
    namespace: str,
    pod_name: str,
    target: int,
    *,
    host: str,
    headers: dict[str, str] | None,
    timeout: float,
    read_timeout: float | None = None,
) -> AsyncIterator[httpx2.AsyncClient]:
    """파드 하나의 포트로 여는 pods/portforward 터널 위의 httpx 클라이언트다."""

    async def factory() -> tuple[socket.socket, Callable[[], None]]:
        return await run_in_threadpool(open_tunnel, client, namespace, pod_name, target)

    async with TunnelBridge(factory) as base_url:
        async with httpx2.AsyncClient(
            base_url=base_url,
            headers={"Host": host, **(headers or {})},
            timeout=httpx2.Timeout(timeout, read=read_timeout or timeout),
            follow_redirects=True,
        ) as hc:
            yield hc


@contextlib.asynccontextmanager
async def bearer_pod_client(
    client: KubeClient,
    namespace: str,
    service: str,
    pod: dict[str, Any],
    port: int,
    *,
    bearer: str | None,
    timeout: float,
) -> AsyncIterator[tuple[httpx2.AsyncClient, str]]:
    """Bearer 를 실은 채 파드 하나에 바로 닿는 (httpx2 클라이언트, 기준 URL). Service 는 Ready
    파드로만 가므로 준비되지 않은 파드를 들여다볼 때 쓴다(T21). 그 Service 로 직행하는 플랫폼이면
    파드 IP 로, 아니면 그 파드로 여는 pods/portforward 터널이다(`bearer_service_client` 와 같은
    갈래). `port` 는 Service 포트이자 컨테이너 포트다. 직행 판정·Host 와 파드 주소·터널에 같은 값을
    쓰므로 두 포트가 같은 Service 에만 쓴다(Agent 는 `AGENT_PORT` 하나다). 파드마다의 리비전을
    읽는 LLM 게이트웨이(P38 4절)처럼 토큰이 없는 포트면 bearer 를 비운다."""
    name = pod["metadata"]["name"]
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    route = client.service_route(namespace, service, port)
    if route.via == "direct":
        ip = (pod.get("status") or {}).get("podIP")
        if not ip:
            raise PortForwardError(f"pod {namespace}/{name} has no IP yet")
        host = f"[{ip}]" if ":" in ip else ip  # IPv6 는 괄호로 싼다
        pod_route = dataclasses.replace(route, base_url=f"http://{host}:{port}")
        async with pod_route.http_client(timeout=timeout) as hc:
            hc.headers.update(headers)
            yield hc, pod_route.base_url
        return
    async with _pod_tunnel_client(
        client,
        namespace,
        name,
        port,
        host=_service_host(namespace, service, port),
        headers=headers,
        timeout=timeout,
    ) as hc:
        yield hc, str(hc.base_url).rstrip("/")


def bearer_header(bearer: str | None) -> dict[str, str]:
    """서비스 토큰의 `Authorization` 헤더. 토큰이 없으면 빈 사전이다."""
    return {"Authorization": f"Bearer {bearer}"} if bearer else {}


@contextlib.asynccontextmanager
async def bearer_service_client(
    client: KubeClient,
    namespace: str,
    service: str,
    port: int,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float,
    read_timeout: float | None = None,
    scheme: str = "http",
) -> AsyncIterator[tuple[httpx2.AsyncClient, str]]:
    """헤더를 실은 채 Service 로 가는 (httpx2 클라이언트, 기준 URL). 직행이면 클러스터 클라이언트의
    경로에 헤더를 얹고, 실은 헤더에 `Authorization` 이 있으면 pods/portforward 다. API 서버
    services/proxy 가 Authorization 을 지우기 때문이다(D41). 그 헤더가 없으면 지울 것도 없어 그 경로
    그대로다. 서비스 토큰은 `Authorization: Bearer` 로, 외부 서버 항목의 고정 헤더는 그 이름으로
    싣는다 (P86 6.3절). scheme 은 그 Service 를 부르는 URL 의 것이다.

    기준 URL 은 끝에 `/` 가 없다. 호출부는 `/` 로 시작하는 경로를 뒤에 붙인다. 문맥을 나가면
    클라이언트와 터널이 닫힌다."""
    route = client.service_route(namespace, service, port, scheme=scheme)
    headers = dict(headers or {})
    needs_tunnel = any(k.lower() == "authorization" for k in headers)
    if route.via == "direct" or not needs_tunnel:
        async with route.http_client(timeout=timeout, read_timeout=read_timeout) as hc:
            hc.headers.update(headers)
            yield hc, route.base_url
        return
    async with portforward_client(
        client,
        namespace,
        service,
        port,
        headers=headers,
        timeout=timeout,
        read_timeout=read_timeout,
    ) as hc:
        yield hc, str(hc.base_url).rstrip("/")
