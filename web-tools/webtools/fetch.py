"""`web_fetch` 도구 (P39b 10·11절).

주소 규칙은 바이트 사본 `webtools/urlfetch.py` 가 정하고, 여기는 도구의 계약을 지킨다. 같은 호스트
안의 리다이렉트만 따르고 다른 호스트로 가는 것은 `redirect_to` 로 돌려준다. 그래야 모델이 그것을
다시 부를 때 대화 출처와 도메인 규칙(런타임의 `WebFetchGuard`)을 지난다. robots.txt 를 지키고,
파드마다 분당 호출 수와 동시 호출 수를 센다(네임스페이스마다 복제본이 하나라 네임스페이스 상한이다).

오류 글은 우리가 쓴 문장뿐이다. 남의 서버가 준 글은 오류에 싣지 않는다(`urlfetch.FETCH_ERRORS`).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from mcp.types import TextContent

from webtools import urlfetch
from webtools.contract import model_header, tool_spec
from webtools.page import TEXT_TYPES, media_type, render
from webtools.settings import FetchSettings

log = logging.getLogger("daegam.web-tools")

ROBOTS_TTL_SECONDS = 3600.0
ROBOTS_MAX_BYTES = 512 * 1024
ROBOTS_AGENT = "daegam-web-fetch"
TOOL_ERRORS = {
    **urlfetch.FETCH_ERRORS,
    "robots_disallowed": "the site's robots.txt does not allow fetching this page",
    "rate_limited": "too many fetches; try again later",
    "invalid_arguments": "the arguments do not match the tool's schema",
}


class ToolFailure(Exception):
    def __init__(self, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.code = code
        self.extra = extra


@dataclass
class TokenBucket:
    """분당 per_minute 번. 파드 하나의 상한이다."""

    per_minute: int
    tokens: float = field(init=False)
    stamp: float = field(init=False)

    def __post_init__(self) -> None:
        self.tokens = float(self.per_minute)
        self.stamp = time.monotonic()

    def take(self) -> float | None:
        """하나를 쓰면 None, 모자라면 다시 해 볼 때까지의 초."""
        now = time.monotonic()
        rate = self.per_minute / 60.0
        self.tokens = min(float(self.per_minute), self.tokens + (now - self.stamp) * rate)
        self.stamp = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return None
        return (1.0 - self.tokens) / rate


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_arguments(args: dict[str, Any], settings: FetchSettings) -> tuple[str, int, int]:
    """계약(`schema/tools.json`)의 입력 스키마를 손으로 지킨다. 경계는 계약의 값이고 max_chars 는
    관리자 상한으로 자른다."""
    props = tool_spec("web_fetch")["inputSchema"]["properties"]
    url = args.get("url")
    max_chars = args.get("max_chars", props["max_chars"]["default"])
    start = args.get("start_index", props["start_index"]["default"])
    if (
        set(args) - set(props)
        or not isinstance(url, str)
        or not props["url"]["minLength"] <= len(url) <= props["url"]["maxLength"]
        or not _is_int(max_chars)
        or not props["max_chars"]["minimum"] <= max_chars <= props["max_chars"]["maximum"]
        or not _is_int(start)
        or start < props["start_index"]["minimum"]
    ):
        raise ToolFailure("invalid_arguments")
    return url, min(max_chars, settings.max_chars), start


class Fetcher:
    def __init__(self, settings: FetchSettings, *, resolver: Any = None, transport: Any = None):
        self.settings = settings
        self.bucket = TokenBucket(settings.per_minute)
        self.slots = asyncio.Semaphore(settings.concurrency)
        self.robots: dict[str, tuple[float, RobotFileParser | None]] = {}
        self.resolver = resolver or urlfetch.system_resolver
        self.transport = transport

    def _rules(self) -> dict[str, Any]:
        s = self.settings
        return {
            "allowed_ports": s.allowed_ports,
            "blocked_suffixes": s.blocked_suffixes,
            "blocked_cidrs": s.blocked_cidrs,
            "user_agent": s.user_agent,
            "ca_bundle": s.ca_bundle,
            "resolver": self.resolver,
            "transport": self.transport,
            "timeout": s.timeout_seconds,
            "deadline": s.timeout_seconds,
        }

    async def _robots_for(self, url: str) -> RobotFileParser | None:
        """그 호스트의 robots.txt. 4xx 는 모두 허용(None), 받지 못하면 막힘이다. 경로가
        고정이라 모델 글을 싣지 않으므로 다른 호스트로 가는 리다이렉트도 5번까지 따른다. 한 시간
        둔다."""
        parts = urlsplit(url)
        key = f"{parts.scheme}://{parts.netloc}".lower()
        cached = self.robots.get(key)
        if cached and time.monotonic() - cached[0] < ROBOTS_TTL_SECONDS:
            return cached[1]
        parser: RobotFileParser | None
        try:
            result = await urlfetch.fetch_page(
                f"{key}/robots.txt",
                max_bytes=ROBOTS_MAX_BYTES,
                max_redirects=5,
                **self._rules(),
            )
            parser = RobotFileParser()
            parser.parse(urlfetch_text(result.body).splitlines())
        except urlfetch.FetchError as e:
            if e.code == "fetch_status" and e.status is not None and 400 <= e.status < 500:
                parser = None
            else:
                raise ToolFailure("robots_disallowed") from e
        except urlfetch.BlockedURL as e:
            raise ToolFailure("robots_disallowed") from e
        self.robots[key] = (time.monotonic(), parser)
        return parser

    async def fetch(self, args: dict[str, Any]) -> dict[str, Any]:
        """구조화 결과. 실패는 ToolFailure."""
        return (await self.fetch_sized(args))[0]

    async def fetch_sized(self, args: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """(구조화 결과, 받은 본문 바이트 수). robots.txt 까지 호출 전체가 시한 하나 안에
        끝난다."""
        url, max_chars, start = check_arguments(args, self.settings)
        wait = self.bucket.take()
        if wait is not None:
            raise ToolFailure("rate_limited", retry_after=urlfetch.retry_after_seconds(wait))
        try:
            return await asyncio.wait_for(
                self._fetch(url, max_chars, start), self.settings.timeout_seconds
            )
        except TimeoutError as e:
            raise ToolFailure("fetch_timeout") from e

    async def _fetch(self, url: str, max_chars: int, start: int) -> tuple[dict[str, Any], int]:
        async with self.slots:
            try:
                if self.settings.respect_robots:
                    # 주소 규칙이 먼저다. 막힌 URL 이면 robots.txt 를 가지러 가지도 않는다
                    s = self.settings
                    await urlfetch.resolve_public_async(
                        url,
                        resolver=self.resolver,
                        timeout=s.timeout_seconds,
                        allowed_ports=s.allowed_ports,
                        blocked_suffixes=s.blocked_suffixes,
                        blocked_cidrs=s.blocked_cidrs,
                    )
                    robots = await self._robots_for(url)
                    if robots is not None and not robots.can_fetch(ROBOTS_AGENT, url):
                        raise ToolFailure("robots_disallowed")
                result = await urlfetch.fetch_page(
                    url,
                    max_bytes=self.settings.max_bytes,
                    max_redirects=self.settings.max_redirects,
                    same_host_only=True,
                    content_types=TEXT_TYPES,
                    **self._rules(),
                )
            except urlfetch.FetchError as e:
                extra = {"status": e.status} if e.status is not None else {}
                raise ToolFailure(e.code, **extra) from e
            except urlfetch.BlockedURL as e:
                raise ToolFailure(e.code) from e
        if result.redirect_to is not None:
            moved = {"url": result.url, "status": result.status, "redirect_to": result.redirect_to}
            return moved, 0
        page = render(result.content_type, result.body, result.final_url)
        total = len(page.text)
        start = min(start, total)
        end = min(total, start + max_chars)
        data = {
            "url": result.url,
            "final_url": result.final_url,
            "status": result.status,
            "content_type": media_type(result.content_type) or None,
            "title": page.title,
            "content": page.text[start:end],
            "start_index": start,
            "next_start_index": end if end < total else None,
            "total_chars": total,
        }
        return data, len(result.body)


def urlfetch_text(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


def model_text(data: dict[str, Any]) -> str:
    """모델이 받는 글. 고정 머리 줄과 `<web_content … untrusted="true">` 감싸개다. 런타임 가드가
    받은 글에 그대로 있는 URL 만 출처로 삼으므로 url·final_url·redirect_to 를 글에도 적는다.
    페이지가 정한 제목은 감싸개 안에 둔다. URL 은 검사를 지나 공백과 따옴표가 없지만 줄에 적을 때는
    감싸개 꼬리표를 가리고(`escape_tags`), 꼬리표의 값으로 쓸 때는 HTML 로 다시 가린다."""
    lines = [model_header(), f"URL: {urlfetch.escape_tags(data['url'])}"]
    if "redirect_to" in data:
        lines.append(f"Status: {data['status']}")
        redirect = urlfetch.escape_tags(data["redirect_to"])
        lines.append(f"Redirects to another host (not followed): {redirect}")
        return "\n".join(lines)
    lines.append(f"Final URL: {urlfetch.escape_tags(data['final_url'])}")
    if data.get("next_start_index") is not None:
        lines.append(
            f"Showing characters {data['start_index']} to {data['next_start_index']} of "
            f"{data['total_chars']}; call again with start_index={data['next_start_index']}."
        )
    body = urlfetch.escape_untrusted(data["content"])
    if data.get("title"):
        body = f"Title: {urlfetch.escape_untrusted(data['title'])}\n\n{body}"
    url = html.escape(data["final_url"], quote=True)
    lines.append(f'<web_content url="{url}" untrusted="true">\n{body}\n</web_content>')
    return "\n".join(lines)


def error_body(failure: ToolFailure) -> dict[str, Any]:
    message = TOOL_ERRORS.get(failure.code, "the fetch failed")
    status = failure.extra.get("status")
    if failure.code == "fetch_status" and status:
        message = f"{message} (HTTP {status})"
    return {"code": failure.code, "message": message, **failure.extra}


def text_block(data: dict[str, Any] | str) -> list[TextContent]:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return [TextContent(type="text", text=text)]


def log_call(url: Any, data: dict[str, Any] | None, size: int, code: str, started: float) -> None:
    """한 줄 기록. URL 의 경로와 쿼리는 남기지 않는다. bytes 는 받은 본문의 바이트 수다."""
    try:
        host = urlsplit(url).hostname if isinstance(url, str) else None
    except ValueError:
        host = None
    log.info(
        json.dumps(
            {
                "tool": "web_fetch",
                "host": host,
                "status": (data or {}).get("status"),
                "bytes": size,
                "ms": int((time.monotonic() - started) * 1000),
                "code": code,
            }
        )
    )
