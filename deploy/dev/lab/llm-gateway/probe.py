"""P38 F0 측정 도구다. 측정 파드(llm-client, 런타임 이미지)에서 돈다.

    lab.sh 가 올린 /spike/probe.py 를 부르거나, 토큰을 숨기려고 표준 입력으로 넘긴다:
    (echo "TOKENS = {...}"; cat probe.py) | kubectl exec -i llm-client -- python - CMD [ARGS]

TOKENS 는 이름 -> JWT 다(mint 는 측정하는 쪽이 플랫폼 서명 키로 한다). 결과는 stdout 에 JSON
한 줄씩이다.
메아리가 받은 요청은 메아리 로그(llm_echo.py 의 JSON 줄)에서 표식으로 찾는다. 표식은 사용자 글의
`probe:<id>` 다.

CMD
  fidelity        런타임의 모델 조립(runtime.models)으로 만든 요청을 직접과 게이트웨이로 한 번씩
                  보낸다(4·17번)
  paths           헤더·경로·우회 경로·?beta=true 를 날 HTTP 로 보낸다(5번)
  jwt             토큰 종류별 상태(6번)
  watch NAME SEC  keep-alive 연결 하나와 매번 새 연결 하나로 SEC 초 동안 0.5초마다 부르고 상태가
                  바뀐 때를 찍는다
  latency N       20 조각을 1초에 보내는 스트림을 N 번, 직접과 게이트웨이로(9번)
  long            180초 침묵·5분 비스트림·10분 스트림을 게이트웨이로 함께 부른다(18번)
  large           11 MiB OpenAI·Anthropic 요청과 13 MiB 요청(19번)
  streams N SEC   SEC 초 걸리는 스트림 N 개를 함께 부르고 각각 끝난 모양을 찍는다(10번)
  burst N         N 번 부르고 상태 수를 센다(8·11번)
"""

import asyncio
import base64
import hashlib
import http.client
import json
import os
import sys
import time
import uuid
from typing import Any

import httpx

GW = os.environ.get("GW", "http://llm-gateway.daegam-llm.svc.cluster.local:8080")
ECHO = os.environ.get("ECHO", "http://llm-echo.daegam-e2e-llm.svc.cluster.local:8080")
DIRECT_KEY = "sk-lab-direct"
TOKENS: dict[str, str] = globals().get("TOKENS", {})


def out(**kw: Any) -> None:
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def marker() -> str:
    return f"probe:{uuid.uuid4().hex[:10]}"


def base(route: str, slug: str) -> str:
    return f"{GW}/p/{slug}" if route == "gateway" else ECHO


def canonical_sha256(body: Any) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


# -- 4·17번: 런타임의 요청 ------------------------------------------------------------------------


def openai_model(
    route: str, name: str, effort: str, temperature: float | None, extra: dict | None
) -> Any:
    from runtime.models import build_chat_model

    os.environ["PROBE_KEY"] = TOKENS["main"] if route == "gateway" else DIRECT_KEY
    provider = {
        "name": "probe-openai",
        "kind": "openai",
        "env_key": "PROBE_KEY",
        "base_url": base(route, "echo-openai") + "/v1",
        "stream_usage": True,
    }
    if extra:
        provider["extra_body"] = extra
    params = {"max_output_tokens": 2048, "effort": effort, "temperature": temperature}
    return build_chat_model({"name": name, "provider": provider, "params": params})


def anthropic_model(route: str, name: str, effort: str, *, direct_flags: bool) -> Any:
    """런타임의 _anthropic 과 같은 인자다. 게이트웨이 경로는 JWT 를 키 자리에 준다. SDK 가 그것을
    `Authorization: Bearer` 로 싣고 X-Api-Key 는 보내지 않는다(P38 5절). direct_flags 는
    block_binding 과 그 베타를 싣는다(17번)."""
    from runtime import llm_http
    from runtime.models import anthropic_reasoning

    kwargs: dict[str, Any] = {
        "model": name,
        "max_tokens": 2048,
        "api_key": TOKENS["main"] if route == "gateway" else DIRECT_KEY,
        "max_retries": 0,
        "base_url": base(route, "echo-anthropic"),
        **anthropic_reasoning(name, effort, direct=direct_flags),
    }
    return llm_http.HookedChatAnthropic(**kwargs)


async def run_chat(model: Any, text: str, *, cache: bool = False) -> dict[str, Any]:
    from langchain_core.messages import HumanMessage, SystemMessage

    system: Any = "You are a probe."
    if cache:
        system = [
            {"type": "text", "text": "You are a probe.", "cache_control": {"type": "ephemeral"}}
        ]
    usage = None
    chunks = 0
    t0 = time.monotonic()
    first = None
    async for chunk in model.astream([SystemMessage(content=system), HumanMessage(content=text)]):
        chunks += 1
        first = first or time.monotonic() - t0
        if chunk.usage_metadata:
            usage = chunk.usage_metadata
    return {"usage": usage, "chunks": chunks, "first": first, "total": time.monotonic() - t0}


async def fidelity() -> None:
    from langchain_openai import OpenAIEmbeddings

    cases: list[tuple[str, Any]] = []
    for effort in ("low", "medium", "high", "xhigh", "max"):
        cases.append(
            (
                f"openai gpt-5-mini effort={effort}",
                lambda r, e=effort: openai_model(r, "gpt-5-mini", e, None, None),
            )
        )
    cases.append(
        ("openai gpt-4.1 temperature=0.7", lambda r: openai_model(r, "gpt-4.1", "off", 0.7, None))
    )
    cases.append(
        (
            "openrouter extra_body provider",
            lambda r: openai_model(
                r,
                "moonshotai/kimi-k3",
                "high",
                0.3,
                {"provider": {"order": ["moonshotai"], "allow_fallbacks": False}},
            ),
        )
    )
    for effort in ("low", "high", "max"):
        cases.append(
            (
                f"anthropic opus-5-5 adaptive effort={effort} cache",
                lambda r, e=effort: anthropic_model(r, "claude-opus-5-5", e, direct_flags=False),
            )
        )
    cases.append(
        (
            "anthropic opus-5-5 block_binding beta (17)",
            lambda r: anthropic_model(r, "claude-opus-5-5", "high", direct_flags=True),
        )
    )
    for label, build in cases:
        for route in ("direct", "gateway"):
            m = marker()
            try:
                res = await run_chat(build(route), f"{m} 안녕", cache="anthropic" in label)
                out(case=label, route=route, marker=m, ok=True, **res)
            except Exception as e:  # noqa: BLE001
                out(
                    case=label,
                    route=route,
                    marker=m,
                    ok=False,
                    error=f"{type(e).__name__}: {e}"[:400],
                )
    for route in ("direct", "gateway"):
        m = marker()
        emb = OpenAIEmbeddings(
            model="text-embedding-3-small",
            base_url=base(route, "echo-openai") + "/v1",
            api_key=TOKENS["main"] if route == "gateway" else DIRECT_KEY,
            dimensions=256,
            check_embedding_ctx_length=False,
            max_retries=0,
        )
        try:
            vecs = await emb.aembed_documents([f"{m} 하나", f"{m} 둘"])
            out(
                case="embeddings",
                route=route,
                marker=m,
                ok=True,
                n=len(vecs),
                dims=len(vecs[0]),
                head=vecs[0][:3],
            )
        except Exception as e:  # noqa: BLE001
            out(
                case="embeddings",
                route=route,
                marker=m,
                ok=False,
                error=f"{type(e).__name__}: {e}"[:400],
            )


# -- 5번: 경로와 헤더 ----------------------------------------------------------------------------


def raw(
    method: str, path: str, token: str | None, body: dict | None = None, headers: dict | None = None
) -> tuple[int, str]:
    host = GW.split("//", 1)[1]
    h, _, p = host.partition(":")
    conn = http.client.HTTPConnection(h, int(p or 80), timeout=30)
    hdrs = {"content-type": "application/json", **(headers or {})}
    if token:
        hdrs["authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    text = resp.read().decode(errors="replace")[:300]
    conn.close()
    return resp.status, text


def chat_body(m: str, stream: bool = False) -> dict:
    return {
        "model": "gpt-5-mini",
        "messages": [{"role": "user", "content": f"{m} hi"}],
        "stream": stream,
    }


def msg_body(m: str) -> dict:
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": f"{m} hi"}],
    }


def paths() -> None:
    t = TOKENS["main"]
    checks = [
        ("POST", "/p/echo-openai/v1/chat/completions", chat_body),
        ("POST", "/p/echo-anthropic/v1/messages", msg_body),
        ("POST", "/p/echo-anthropic/v1/messages?beta=true", msg_body),
        ("POST", "/p/echo-prefix/v1/chat/completions", chat_body),
        ("POST", "/p/echo-anth-root/v1/messages", msg_body),
        ("POST", "/p/echo-openai/v1/embeddings", lambda m: {"model": "e", "input": [m]}),
        ("GET", "/p/echo-openai/v1/models", None),
        ("DELETE", "/p/echo-openai/v1/models/ft-x", None),
        ("POST", "/p/echo-openai/v1/files", chat_body),
        ("GET", "/p/echo-openai/v1/chat/completions", None),
        ("PUT", "/p/echo-openai/v1/chat/completions", chat_body),
        ("POST", "/p/echo-openai/v1/responses", chat_body),
        ("POST", "/p/echo-anthropic/v1/messages/count_tokens", msg_body),
        ("POST", "/p/echo-anthropic/v1/messages/batches", msg_body),
        ("POST", "/p/echo-openai/v1/chat/completions/", chat_body),
        ("POST", "/p/echo-openai//v1/chat/completions", chat_body),
        ("POST", "//p/echo-openai/v1/chat/completions", chat_body),
        ("POST", "/p/echo-openai/v1/chat/../chat/completions", chat_body),
        ("POST", "/p/echo-openai/v1/x/../../v1/chat/completions", chat_body),
        ("POST", "/p/echo-openai/v1/chat/completions/../../models", chat_body),
        ("POST", "/p/echo-openai/v1%2Fchat%2Fcompletions", chat_body),
        ("POST", "/p/echo-openai%2Fv1%2Fchat%2Fcompletions", chat_body),
        ("POST", "/p/echo-openai/v1/chat/completions%2F..%2F..%2Fmodels", chat_body),
        ("POST", "/p/echo-openai/v1/chat/%63ompletions", chat_body),
        ("POST", "/P/echo-openai/v1/chat/completions", chat_body),
        ("POST", "/p/ECHO-OPENAI/v1/chat/completions", chat_body),
        ("POST", "/p/echo-openai/V1/chat/completions", chat_body),
        ("POST", "/p/echo-openai/v1/chat/completions;x", chat_body),
        ("POST", "/p/echo-openai/v1/chat/completions#x", chat_body),
        ("POST", "/p/echo-anthropic/v1/chat/completions", chat_body),
        ("POST", "/p/echo-openai/v1/messages", msg_body),
        ("POST", "/v1/chat/completions", chat_body),
        ("GET", "/_daegam/rev", None),
        ("POST", "/_daegam/rev", None),
    ]
    for method, path, body in checks:
        m = marker()
        try:
            status, text = raw(method, path, t, body(m) if body else None)
        except Exception as e:  # noqa: BLE001
            status, text = -1, f"{type(e).__name__}: {e}"
        out(method=method, path=path, marker=m, status=status, body=text[:160])


# -- 6번: JWT ------------------------------------------------------------------------------------


def jwt_cases() -> None:
    path = "/p/echo-openai/v1/chat/completions"
    for name in ["none", *TOKENS]:
        m = marker()
        status, text = raw("POST", path, None if name == "none" else TOKENS[name], chat_body(m))
        out(token=name, marker=m, status=status, body=text[:200])
    # Bearer 가 아닌 곳에 둔 토큰
    m = marker()
    status, text = raw("POST", path, None, chat_body(m), {"x-api-key": TOKENS["main"]})
    out(token="main-in-x-api-key", marker=m, status=status, body=text[:200])


def watch(name: str, seconds: float) -> None:
    """keep-alive 연결 하나(httpx 클라이언트)와 매번 새 연결로 부르며 상태가 바뀐 시각을 찍는다."""
    path = "/p/echo-openai/v1/chat/completions"
    token = TOKENS[name]
    keep = httpx.Client(
        timeout=30, limits=httpx.Limits(max_keepalive_connections=1, max_connections=1)
    )
    last: dict[str, int] = {}
    t0 = time.time()
    while time.time() - t0 < seconds:
        for kind in ("keepalive", "fresh"):
            m = marker()
            try:
                if kind == "keepalive":
                    r = keep.post(
                        GW + path, json=chat_body(m), headers={"authorization": f"Bearer {token}"}
                    )
                    status = r.status_code
                else:
                    status, _ = raw("POST", path, token, chat_body(m))
            except Exception as e:  # noqa: BLE001
                status = -1
                out(kind=kind, error=str(e)[:200], t=round(time.time() - t0, 2))
            if last.get(kind) != status:
                out(
                    kind=kind,
                    status=status,
                    t=round(time.time() - t0, 2),
                    wall=time.time(),
                    marker=m,
                )
                last[kind] = status
        time.sleep(0.5)
    out(done=True, last=last)


# -- 9번: 지연 ------------------------------------------------------------------------------------


async def one_stream(
    client: httpx.AsyncClient, url: str, token: str, ctl: dict, body: dict
) -> dict:
    t0 = time.monotonic()
    first = None
    n = 0
    status = None
    try:
        async with client.stream(
            "POST",
            url,
            json=body,
            headers={"authorization": f"Bearer {token}", "x-echo-control": json.dumps(ctl)},
        ) as r:
            status = r.status_code
            async for line in r.aiter_lines():
                if line.startswith("data:"):
                    n += 1
                    if first is None:
                        first = time.monotonic() - t0
        return {"status": status, "first": first, "total": time.monotonic() - t0, "events": n}
    except Exception as e:  # noqa: BLE001
        return {
            "status": status,
            "first": first,
            "total": time.monotonic() - t0,
            "events": n,
            "error": f"{type(e).__name__}: {e}"[:200],
        }


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]


async def latency(n: int) -> None:
    ctl = {"chunks": 20, "chunk_interval": 0.05}
    res: dict[str, list[dict]] = {"direct": [], "gateway": []}
    async with httpx.AsyncClient(timeout=60) as client:
        for i in range(n):
            for route in ("direct", "gateway") if i % 2 == 0 else ("gateway", "direct"):
                url = (
                    GW + "/p/echo-openai" if route == "gateway" else ECHO
                ) + "/v1/chat/completions"
                token = TOKENS["main"] if route == "gateway" else DIRECT_KEY
                body = chat_body(marker(), stream=True)
                body["messages"][0]["content"] += " " + "x" * 200
                res[route].append(await one_stream(client, url, token, ctl, body))
    summary = {}
    for route, rows in res.items():
        ok = [r for r in rows if r["status"] == 200 and r["first"] is not None]
        summary[route] = {
            "n": len(ok),
            "first_p50": pct([r["first"] for r in ok], 0.5),
            "first_p95": pct([r["first"] for r in ok], 0.95),
            "total_p50": pct([r["total"] for r in ok], 0.5),
            "total_p95": pct([r["total"] for r in ok], 0.95),
        }
    d, g = summary["direct"], summary["gateway"]
    out(
        summary=summary,
        added_first_p95_ms=round((g["first_p95"] - d["first_p95"]) * 1000, 1),
        added_total_p95_ms=round((g["total_p95"] - d["total_p95"]) * 1000, 1),
        added_first_p50_ms=round((g["first_p50"] - d["first_p50"]) * 1000, 1),
        added_total_p50_ms=round((g["total_p50"] - d["total_p50"]) * 1000, 1),
    )


# -- 18번: 긴 호출 ---------------------------------------------------------------------------------


async def long_calls() -> None:
    token = TOKENS["main"]
    url = GW + "/p/echo-openai/v1/chat/completions"

    async def silent_stream() -> dict:
        async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=10)) as c:
            return await one_stream(
                c, url, token, {"first_byte_delay": 180}, chat_body(marker(), stream=True)
            )

    async def long_stream() -> dict:
        async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=10)) as c:
            return await one_stream(
                c,
                url,
                token,
                {"chunks": 120, "chunk_interval": 5},
                chat_body(marker(), stream=True),
            )

    async def slow_plain() -> dict:
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=10)) as c:
            try:
                r = await c.post(
                    url,
                    json=chat_body(marker()),
                    headers={
                        "authorization": f"Bearer {token}",
                        "x-echo-control": json.dumps({"first_byte_delay": 300}),
                    },
                )
                return {
                    "status": r.status_code,
                    "total": time.monotonic() - t0,
                    "body": r.text[:120],
                }
            except Exception as e:  # noqa: BLE001
                return {
                    "status": None,
                    "total": time.monotonic() - t0,
                    "error": f"{type(e).__name__}: {e}"[:200],
                }

    names = ["silence_180s_stream", "nonstream_300s", "stream_600s"]
    results = await asyncio.gather(silent_stream(), slow_plain(), long_stream())
    for name, r in zip(names, results, strict=True):
        out(case=name, **r)


# -- 19번: 큰 본문 ---------------------------------------------------------------------------------


def image_b64(size: int) -> str:
    return base64.b64encode(os.urandom(size * 3 // 4)).decode()


def large() -> None:
    token = TOKENS["main"]
    for label, target in (("11MiB", 11 * 1024 * 1024), ("13MiB", 13 * 1024 * 1024)):
        img = image_b64(target - 2048)
        m = marker()
        oa = {
            "model": "gpt-5-mini",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"{m} 그림"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}},
                    ],
                }
            ],
        }
        an = {
            "model": "claude-opus-5-5",
            "max_tokens": 64,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"{m} 그림"},
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png", "data": img},
                        },
                    ],
                }
            ],
        }
        for slug, path, body, hdr in (
            ("echo-openai", "/v1/chat/completions", oa, {"authorization": f"Bearer {token}"}),
            ("echo-anthropic", "/v1/messages", an, {"authorization": f"Bearer {token}"}),
        ):
            data = json.dumps(body).encode()
            t0 = time.monotonic()
            try:
                r = httpx.post(
                    f"{GW}/p/{slug}{path}",
                    content=data,
                    headers={**hdr, "content-type": "application/json"},
                    timeout=120,
                )
                status, text = r.status_code, r.text[:200]
            except Exception as e:  # noqa: BLE001
                status, text = -1, f"{type(e).__name__}: {e}"[:200]
            out(
                case=f"{label} {slug}",
                marker=m,
                bytes=len(data),
                sent_sha256=canonical_sha256(body),
                status=status,
                seconds=round(time.monotonic() - t0, 2),
                body=text,
            )
    # 13 MiB 을 런타임 SDK 로 보내면 런타임은 무엇을 보이나
    try:
        from openai import OpenAI

        from runtime.stream import describe_error

        client = OpenAI(base_url=GW + "/p/echo-openai/v1", api_key=token, max_retries=0)
        img = image_b64(13 * 1024 * 1024)
        client.chat.completions.create(
            model="gpt-5-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}}
                    ],
                }
            ],
        )
        out(case="13MiB sdk", ok=True)
    except Exception as e:  # noqa: BLE001
        try:
            described = describe_error(e)
        except Exception as e2:  # noqa: BLE001
            described = f"describe_error failed: {e2}"
        out(
            case="13MiB sdk",
            error=f"{type(e).__name__}: {str(e)[:200]}",
            described=str(described)[:300],
        )


# -- 10번: 스트림과 파드 ---------------------------------------------------------------------


async def streams(n: int, seconds: float) -> None:
    token = TOKENS["main"]
    url = GW + "/p/echo-openai/v1/chat/completions"
    chunks = max(1, int(seconds))
    ctl = {"chunks": chunks, "chunk_interval": seconds / chunks}
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(900, connect=10), limits=httpx.Limits(max_connections=n)
    ) as c:
        rows = await asyncio.gather(
            *[one_stream(c, url, token, ctl, chat_body(marker(), stream=True)) for _ in range(n)]
        )
    for r in rows:
        r["complete"] = r["events"] >= chunks + 2
    out(streams=rows, complete=sum(r["complete"] for r in rows), n=n)


# -- 8·11번: 여러 번 ------------------------------------------------------------------------


def burst(
    n: int,
    slug: str = "echo-openai",
    name: str = "main",
    ctl: str = "",
    pause: float = 0.0,
    pad: int = 0,
) -> None:
    token = TOKENS[name]
    counts: dict[str, int] = {}
    rows = []
    with httpx.Client(timeout=60) as c:
        for i in range(n):
            m = marker()
            body = chat_body(m)
            if pad:
                body["messages"][0]["content"] += " " + "y" * pad
            hdr = {"authorization": f"Bearer {token}"}
            if ctl:
                hdr["x-echo-control"] = ctl
            try:
                r = c.post(f"{GW}/p/{slug}/v1/chat/completions", json=body, headers=hdr)
                key = str(r.status_code)
                rows.append(
                    {
                        "i": i,
                        "marker": m,
                        "status": r.status_code,
                        "rl": {
                            k: v
                            for k, v in r.headers.items()
                            if "ratelimit" in k or k == "retry-after"
                        },
                        "body": r.text[:120],
                    }
                )
            except Exception as e:  # noqa: BLE001
                key = type(e).__name__
                rows.append({"i": i, "marker": m, "error": str(e)[:120]})
            counts[key] = counts.get(key, 0) + 1
            if pause:
                time.sleep(pause)
    out(counts=counts, rows=rows)


def main() -> None:
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "fidelity":
        asyncio.run(fidelity())
    elif cmd == "paths":
        paths()
    elif cmd == "jwt":
        jwt_cases()
    elif cmd == "watch":
        watch(args[0], float(args[1]))
    elif cmd == "latency":
        asyncio.run(latency(int(args[0])))
    elif cmd == "long":
        asyncio.run(long_calls())
    elif cmd == "large":
        large()
    elif cmd == "streams":
        asyncio.run(streams(int(args[0]), float(args[1])))
    elif cmd == "burst":
        kw = json.loads(args[1]) if len(args) > 1 else {}
        burst(int(args[0]), **kw)
    else:
        raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    main()
