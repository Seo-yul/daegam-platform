"""OpenAI·Anthropic 호환 메아리 서버 (P37 e2e `make e2e-telemetry`, P38 F0). 런타임 이미지에
ConfigMap 으로 얹어 돈다. 가짜 키로만 부른다.

    python /echo/llm_echo.py            # 8080 에서 듣는다. PORT 로 바꾼다

모델은 런타임의 `StubChatModel` 을, 임베딩은 `hash_embedding` 을 그대로 부른다. 그래서 Tool 선택과
벡터가 stub provider 와 같다(D26 의 규칙). stub provider 와 달리 진짜 HTTP 호출이라 상관
헤더(`traceparent`), `stream_usage`, 캐시 토큰 읽기를 지난다.

- `POST /v1/chat/completions`: 스트림이면 `stream_options.include_usage` 일 때 마지막 조각에
  사용량을 싣는다. 사용량은 글자 수에서 정하고(스텁의 규칙), `prompt_tokens_details.cached_tokens`
  는 입력의 4분의 1, `completion_tokens_details. reasoning_tokens` 는 출력의 10분의 1 이다
- `POST /v1/messages`: Anthropic Messages 다. 스트림은 message_start 부터 message_stop 까지의
  이벤트이고, 요청에 thinking 이 켜져 있으면(disabled 가 아니면) thinking 블록을 먼저 보낸다.
  사용량의 `cache_read_input_tokens` 는 입력의 4분의 1, `cache_creation_input_tokens` 는 8분의 1
  이다. Anthropic 은 추론 토큰을 따로 세지 않고 output_tokens 에 넣는다
- 마지막 사용자 메시지에 `{"sleep": n}` 조각이 있으면 스텁처럼 n 초 기다린 뒤 답한다. e2e 가
  진행 중인 모델 호출을 끊는다
- `POST /v1/embeddings`: `dimensions`(없으면 1536) 차원의 hash 벡터다. 사용량은
  `llm_http.estimate_tokens` 의 글자 규칙이다. `OpenAIEmbeddings` 는 응답의 사용량을 버리므로
  호출하는 쪽도 같은 규칙으로 어림한다
- 요청마다 경로·`traceparent`·모델·사용량에 더해 메서드·쿼리·보낸 쪽 주소·헤더 전부와 본문을
  JSON 한 줄로 stdout 에 쓴다. 본문은 BODY_LOG_LIMIT 바이트까지만 그대로 싣고, 언제나 정규화한
  JSON 의 sha256(`body_sha256`, 키 정렬·공백 없음)과 받은 바이트 수를 싣는다. 큰 본문은 그 해시로
  맞댄다.
  e2e 가 이 줄을 원장과 맞춰 보고, P38 F0 가 게이트웨이가 위로 보낸 헤더·경로·본문을 본다
- 위 세 경로 밖의 요청도 같은 줄을 쓰고 404 로 답한다. 게이트웨이가 막아야 할 경로가 여기까지
  왔는지 보는 자리다

제어 (P38 F0). 규칙은 요청 헤더 `x-echo-control` 의 JSON 이나, 메아리에 직접 보낸
`POST /_echo/control` 의 규칙 목록(먼저 넣은 것부터 `count` 번씩, 음수면 계속)으로 준다. 헤더가
있으면 그것이 먼저다. `GET` 은 남은 규칙을, `DELETE` 는 모두 지운다. 칸은 모두 고를 수 있다.
- `match`: 이 글자가 경로에 있는 요청에만 건다
- `status`, `body`, `retry_after`: 그 상태와 JSON 본문(없으면 OpenAI 모양의 오류)으로 답한다.
  `retry_after` 는 `Retry-After` 헤더다
- `first_byte_delay`: 응답의 첫 바이트(헤더 포함) 전에 그 초만큼 아무것도 보내지 않는다
- `chunks`, `chunk_interval`: 스트림의 글을 그 수의 조각으로 나눠 조각 사이를 그 초만큼 띄운다
- `cut_after`: 스트림의 글 조각을 그 수만큼 보낸 뒤 연결을 끊는다(사용량 조각 없이)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from runtime.llm_http import estimate_tokens
from runtime.stub import StubChatModel
from runtime.stub_embedding import hash_embedding

# 요청에 dimensions 가 없을 때의 차원이다. e2e_telemetry.sh 가 이 줄을 읽어 Knowledge Base 의
# 차원으로 적는다
DEFAULT_DIMENSIONS = 1536
CACHED_SHARE = 4
CACHE_WRITE_SHARE = 8
REASONING_SHARE = 10
BODY_LOG_LIMIT = 256 * 1024
"""stdout 줄에 본문을 그대로 싣는 상한이다. 넘으면 해시와 크기만 싣는다."""
CONTROL_HEADER = "x-echo-control"
THINKING_TEXT = "메아리가 생각한다."
SIGNATURE = "echo-signature"

app = FastAPI()
_rules: list[dict[str, Any]] = []


def canonical_sha256(body: Any) -> str:
    """키를 정렬하고 공백을 뺀 JSON 의 sha256 이다. 보내는 쪽도 같은 규칙으로 맞댄다."""
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def log(
    path: str,
    request: Request,
    model: str,
    usage: dict[str, Any],
    *,
    raw: bytes = b"",
    body: Any = None,
    status: int = 200,
) -> None:
    line = {
        "llm_echo": path,
        "traceparent": request.headers.get("traceparent"),
        "model": model,
        "usage": usage,
        "ts": time.time(),
        "method": request.method,
        "query": request.url.query,
        "peer": request.client.host if request.client else None,
        "status": status,
        "headers": {k: v for k, v in request.headers.items()},
        "repeated_headers": sorted(
            {k for k, _ in request.headers.items() if len(request.headers.getlist(k)) > 1}
        ),
        "body_bytes": len(raw),
        "body_sha256": canonical_sha256(body) if body is not None else None,
        "body": body if body is not None and len(raw) <= BODY_LOG_LIMIT else None,
    }
    print(json.dumps(line, ensure_ascii=False), flush=True)


async def read_body(request: Request) -> tuple[bytes, Any]:
    raw = await request.body()
    try:
        return raw, json.loads(raw) if raw else None
    except ValueError:
        return raw, None


def control_for(request: Request) -> dict[str, Any]:
    """이 요청에 걸 제어 규칙이다. 헤더가 먼저고, 없으면 대기열에서 경로가 맞는 첫 규칙을 쓴다."""
    header = request.headers.get(CONTROL_HEADER)
    if header:
        try:
            return dict(json.loads(header))
        except (ValueError, TypeError):
            return {}
    for rule in _rules:
        if rule.get("match") and rule["match"] not in request.url.path:
            continue
        count = int(rule.get("count", 1))
        if count > 0:
            rule["count"] = count - 1
            if rule["count"] == 0:
                _rules.remove(rule)
        return rule
    return {}


def error_response(ctl: dict[str, Any]) -> JSONResponse:
    status = int(ctl["status"])
    body = ctl.get("body") or {
        "error": {"message": f"echo status {status}", "type": "echo_error", "code": None}
    }
    headers = {"Retry-After": str(ctl["retry_after"])} if ctl.get("retry_after") else None
    return JSONResponse(body, status_code=status, headers=headers)


def pieces(text: str, ctl: dict[str, Any]) -> list[str]:
    """스트림의 글 조각이다. `chunks` 가 없으면 한 조각이다."""
    n = max(1, int(ctl.get("chunks") or 1))
    size = -(-len(text) // n) if text else 0
    return [text[i * size : (i + 1) * size] for i in range(n)] if size else [text] + [""] * (n - 1)


def text_of(content: Any) -> str:
    """메시지 본문의 글이다. 블록 목록(이미지를 첨부한 사람 메시지)은 text 블록만 잇는다.
    stub provider 가 보는 langchain 메시지의 `.text` 와 같아서, 첨부 옆의 스텁 조각도 그대로
    읽힌다."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(b.get("text") or "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return json.dumps(content or "", ensure_ascii=False)


def to_messages(raw: list[dict[str, Any]]) -> list[BaseMessage]:
    """OpenAI 메시지를 langchain 메시지로. 스텁은 마지막 메시지의 종류와 글만 본다."""
    out: list[BaseMessage] = []
    for m in raw:
        text = text_of(m.get("content"))
        role = m.get("role")
        if role == "tool":
            out.append(ToolMessage(content=text, tool_call_id=m.get("tool_call_id") or "call"))
        elif role == "assistant":
            out.append(AIMessage(content=text))
        elif role == "system":
            out.append(SystemMessage(content=text))
        else:
            out.append(HumanMessage(content=text))
    return out


def anthropic_to_messages(body: dict[str, Any]) -> list[BaseMessage]:
    """Anthropic 메시지를 langchain 메시지로. tool_result 블록만 든 사용자 메시지는
    ToolMessage 다. 사람 메시지의 글은 OpenAI 경로처럼 `text_of` 로 읽는다."""
    out: list[BaseMessage] = []
    if body.get("system"):
        system = body["system"]
        out.append(SystemMessage(content=system if isinstance(system, str) else json.dumps(system)))
    for m in body.get("messages") or []:
        content = m.get("content")
        blocks = content if isinstance(content, list) else []
        results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        if m.get("role") == "user" and results:
            result = results[-1]
            out.append(
                ToolMessage(
                    content=json.dumps(result.get("content"), ensure_ascii=False),
                    tool_call_id=result.get("tool_use_id") or "call",
                )
            )
            continue
        if m.get("role") == "assistant":
            text = (
                content
                if isinstance(content, str)
                else json.dumps(content or "", ensure_ascii=False)
            )
            out.append(AIMessage(content=text))
        else:
            out.append(HumanMessage(content=text_of(content)))
    return out


def stub_reply(
    model_name: str, tools: list[str], messages: list[BaseMessage]
) -> tuple[StubChatModel, AIMessage]:
    stub = StubChatModel(model_name=model_name)
    stub.bind_tools([{"name": t} for t in tools if t])
    return stub, stub.reply(messages)


def usage_of(reply: AIMessage) -> dict[str, Any]:
    meta = reply.usage_metadata or {}
    prompt, completion = int(meta.get("input_tokens", 1)), int(meta.get("output_tokens", 1))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": max(1, prompt // CACHED_SHARE)},
        "completion_tokens_details": {"reasoning_tokens": max(1, completion // REASONING_SHARE)},
    }


def anthropic_usage_of(reply: AIMessage) -> dict[str, Any]:
    meta = reply.usage_metadata or {}
    prompt, completion = int(meta.get("input_tokens", 1)), int(meta.get("output_tokens", 1))
    return {
        "input_tokens": prompt,
        "cache_creation_input_tokens": max(1, prompt // CACHE_WRITE_SHARE),
        "cache_read_input_tokens": max(1, prompt // CACHED_SHARE),
        "output_tokens": completion,
    }


def tool_calls_of(reply: AIMessage) -> list[dict[str, Any]]:
    return [
        {
            "index": i,
            "id": call["id"],
            "type": "function",
            "function": {"name": call["name"], "arguments": json.dumps(call["args"])},
        }
        for i, call in enumerate(reply.tool_calls)
    ]


async def delayed(ctl: dict[str, Any]) -> None:
    if ctl.get("first_byte_delay"):
        await asyncio.sleep(float(ctl["first_byte_delay"]))


@app.post("/v1/chat/completions")
async def chat(request: Request) -> Any:
    raw, body = await read_body(request)
    body = body or {}
    ctl = control_for(request)
    model_name = str(body.get("model") or "echo")
    if ctl.get("status"):
        log(
            "/v1/chat/completions",
            request,
            model_name,
            {},
            raw=raw,
            body=body,
            status=int(ctl["status"]),
        )
        await delayed(ctl)
        return error_response(ctl)
    tools = [t.get("function", {}).get("name") for t in body.get("tools") or []]
    messages = to_messages(body.get("messages") or [])
    stub, reply = stub_reply(model_name, tools, messages)
    # 스텁처럼 {"sleep": n} 이면 그만큼 기다린 뒤 답한다. e2e 가 진행 중인 호출을 끊는 자리다
    if delay := stub.sleep_seconds(messages):
        await asyncio.sleep(delay)
    usage = usage_of(reply)
    log("/v1/chat/completions", request, model_name, usage, raw=raw, body=body)
    call_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    finish = "tool_calls" if reply.tool_calls else "stop"
    message: dict[str, Any] = {"role": "assistant", "content": reply.text or None}
    if reply.tool_calls:
        message["tool_calls"] = [
            {k: v for k, v in c.items() if k != "index"} for c in tool_calls_of(reply)
        ]
    await delayed(ctl)
    if not body.get("stream"):
        choice = {"index": 0, "message": message, "finish_reason": finish}
        return JSONResponse(
            {
                "id": call_id,
                "object": "chat.completion",
                "model": model_name,
                "choices": [choice],
                "usage": usage,
            }
        )
    with_usage = bool((body.get("stream_options") or {}).get("include_usage"))

    def chunk(delta: dict[str, Any], finish_reason: str | None = None, **extra: Any) -> str:
        choices = (
            [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
            if delta is not None
            else []
        )
        data = {
            "id": call_id,
            "object": "chat.completion.chunk",
            "model": model_name,
            "choices": choices,
            **extra,
        }
        return f"data: {json.dumps(data)}\n\n"

    async def stream() -> AsyncIterator[str]:
        for i, text in enumerate(pieces(reply.text or "", ctl)):
            if i and ctl.get("chunk_interval"):
                await asyncio.sleep(float(ctl["chunk_interval"]))
            if ctl.get("cut_after") is not None and i >= int(ctl["cut_after"]):
                raise ConnectionAbortedError("echo cut_after")
            delta: dict[str, Any] = {"content": text}
            if i == 0:
                delta["role"] = "assistant"
                if reply.tool_calls:
                    delta["tool_calls"] = tool_calls_of(reply)
            yield chunk(delta)
        yield chunk({}, finish)
        if with_usage:
            yield chunk(None, usage=usage)
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


def anthropic_content(reply: AIMessage, thinking: bool) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if thinking:
        blocks.append({"type": "thinking", "thinking": THINKING_TEXT, "signature": SIGNATURE})
    if reply.text or not reply.tool_calls:
        blocks.append({"type": "text", "text": reply.text or ""})
    blocks += [
        {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
        for c in reply.tool_calls
    ]
    return blocks


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps({'type': event, **data}, ensure_ascii=False)}\n\n"


async def anthropic_events(
    msg: dict[str, Any], usage: dict[str, Any], ctl: dict[str, Any]
) -> AsyncIterator[str]:
    start_usage = {**usage, "output_tokens": 1}
    yield sse(
        "message_start",
        {"message": {**msg, "content": [], "stop_reason": None, "usage": start_usage}},
    )
    for index, block in enumerate(msg["content"]):
        kind = block["type"]
        empty = {"thinking": {"thinking": "", "signature": ""}, "text": {"text": ""}}.get(kind)
        yield sse(
            "content_block_start",
            {
                "index": index,
                "content_block": {
                    "type": kind,
                    **(empty or {"id": block.get("id"), "name": block.get("name"), "input": {}}),
                },
            },
        )
        if kind == "thinking":
            yield sse(
                "content_block_delta",
                {
                    "index": index,
                    "delta": {"type": "thinking_delta", "thinking": block["thinking"]},
                },
            )
            yield sse(
                "content_block_delta",
                {
                    "index": index,
                    "delta": {"type": "signature_delta", "signature": block["signature"]},
                },
            )
        elif kind == "text":
            for i, text in enumerate(pieces(block["text"], ctl)):
                if i and ctl.get("chunk_interval"):
                    await asyncio.sleep(float(ctl["chunk_interval"]))
                if ctl.get("cut_after") is not None and i >= int(ctl["cut_after"]):
                    raise ConnectionAbortedError("echo cut_after")
                yield sse(
                    "content_block_delta",
                    {"index": index, "delta": {"type": "text_delta", "text": text}},
                )
        else:
            yield sse(
                "content_block_delta",
                {
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(block["input"]),
                    },
                },
            )
        yield sse("content_block_stop", {"index": index})
    yield sse(
        "message_delta",
        {"delta": {"stop_reason": msg["stop_reason"], "stop_sequence": None}, "usage": usage},
    )
    yield sse("message_stop", {})


@app.post("/v1/messages")
async def messages(request: Request) -> Any:
    raw, body = await read_body(request)
    body = body or {}
    ctl = control_for(request)
    model_name = str(body.get("model") or "echo")
    if ctl.get("status"):
        log("/v1/messages", request, model_name, {}, raw=raw, body=body, status=int(ctl["status"]))
        await delayed(ctl)
        return error_response(ctl)
    tools = [t.get("name") for t in body.get("tools") or []]
    history = anthropic_to_messages(body)
    stub, reply = stub_reply(model_name, tools, history)
    if delay := stub.sleep_seconds(history):
        await asyncio.sleep(delay)
    usage = anthropic_usage_of(reply)
    log("/v1/messages", request, model_name, usage, raw=raw, body=body)
    thinking = (body.get("thinking") or {}).get("type") not in (None, "disabled")
    msg = {
        "id": f"msg_{uuid.uuid4().hex[:12]}",
        "type": "message",
        "role": "assistant",
        "model": model_name,
        "content": anthropic_content(reply, thinking),
        "stop_reason": "tool_use" if reply.tool_calls else "end_turn",
        "stop_sequence": None,
    }
    await delayed(ctl)
    if not body.get("stream"):
        return JSONResponse({**msg, "usage": usage})
    return StreamingResponse(anthropic_events(msg, usage, ctl), media_type="text/event-stream")


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> Any:
    raw, body = await read_body(request)
    body = body or {}
    ctl = control_for(request)
    texts = body.get("input")
    texts = [texts] if isinstance(texts, str) else list(texts or [])
    model_name = str(body.get("model") or "echo-embed")
    if ctl.get("status"):
        log(
            "/v1/embeddings", request, model_name, {}, raw=raw, body=body, status=int(ctl["status"])
        )
        await delayed(ctl)
        return error_response(ctl)
    dimensions = int(body.get("dimensions") or DEFAULT_DIMENSIONS)
    tokens = estimate_tokens([str(t) for t in texts])
    usage = {"prompt_tokens": tokens, "total_tokens": tokens}
    log("/v1/embeddings", request, model_name, usage, raw=raw, body=body)
    data = [
        {"object": "embedding", "index": i, "embedding": hash_embedding(str(t), dimensions)}
        for i, t in enumerate(texts)
    ]
    await delayed(ctl)
    return JSONResponse({"object": "list", "data": data, "model": model_name, "usage": usage})


@app.get("/v1/models")
async def models(request: Request) -> dict[str, Any]:
    """연결 확인(`models.list`)의 답이다 (P38 e2e 2단계). 두 SDK 가 읽는 목록 꼴을 함께 싣는다.
    받은 머리는 다른 경로처럼 stdout 에 남는다."""
    log("/v1/models", request, "", {})
    name = "echo-chat"
    entry = {"id": name, "object": "model", "type": "model", "created": 0, "owned_by": "echo"}
    page = {"has_more": False, "first_id": name, "last_id": name}
    return {"object": "list", "data": [entry], **page}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.api_route("/_echo/control", methods=["GET", "POST", "DELETE"])
async def control(request: Request) -> Any:
    if request.method == "POST":
        body = await request.json()
        _rules.extend(body if isinstance(body, list) else [body])
    elif request.method == "DELETE":
        _rules.clear()
    return {"rules": _rules}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def other(path: str, request: Request) -> Response:
    raw, body = await read_body(request)
    log("/" + path, request, "", {}, raw=raw, body=body, status=404)
    return JSONResponse(
        {"error": {"message": "echo: no such path", "type": "not_found"}}, status_code=404
    )


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="warning")  # noqa: S104
