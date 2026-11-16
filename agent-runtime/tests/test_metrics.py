"""`GET /metrics` 의 `chat` 칸(D61). 받는 첨부의 형식과 상한이고 플랫폼이 상태의 `status.chat`
으로 넘긴다. `warnings` 는 저장소와 컴파일의 경고다.

문서는 메인 에이전트의 파일 도구, 이미지는 메인 모델 프로필의 `image_inputs`, 세션 합 상한은 저장소
종류가 정한다.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from runtime.attachments import (
    DEFAULT_SESSION_BYTES,
    MAX_ATTACHMENTS_PER_TURN,
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_TURN,
    MAX_SESSION_IMAGES,
    MIB,
    chat_report,
)
from runtime.compile import BUILTIN_TOOL_NAME
from runtime.config import parse_config
from runtime.stub import StubChatModel
from tests.conftest import load_fixture, run_app, serve


async def test_metrics_carry_what_the_chat_accepts(support_doc: dict[str, Any]) -> None:
    """파일 도구가 켜진 메인, 프로필에 이미지 칸이 없는 모델, memory 노드 없음이다."""
    async with run_app(support_doc) as running:
        metrics = (await running.client.get("/metrics")).json()
    assert metrics["chat"] == {
        "documents": True,
        "images": True,
        "attachments_limit_fetch": False,
        "limits": {
            "max_attachments_per_turn": MAX_ATTACHMENTS_PER_TURN,
            "max_images_per_turn": MAX_IMAGES_PER_TURN,
            "max_session_images": MAX_SESSION_IMAGES,
            "max_image_bytes": MAX_IMAGE_BYTES,
            "max_session_bytes": DEFAULT_SESSION_BYTES,
        },
    }
    # 프로필이 아예 없는 모델도 이미지를 받는다. deepagents 처럼 프로필이 막을 때만 거절한다
    blank = StubChatModel(profile=None)
    assert chat_report(parse_config(support_doc), blank)["images"] is True


def test_session_bytes_follow_the_store_kind() -> None:
    """Postgres 는 8MiB 이고 파드 RAM 을 나눠 쓰는 Redis 와 memory 없음은 2MiB 다."""

    def limit(name: str, *, memory: bool = True) -> int:
        doc = load_fixture(name)
        if not memory:
            doc["memory"] = None
        return chat_report(parse_config(doc), StubChatModel())["limits"]["max_session_bytes"]

    assert limit("support-agent.json") == 8 * MIB
    assert limit("redis-memory.json") == 2 * MIB
    assert limit("support-agent.json", memory=False) == 2 * MIB


async def test_metrics_carry_the_compile_warnings(support_doc: dict[str, Any]) -> None:
    """소스가 내장 이름의 도구를 주면 컴파일 경고가 플랫폼이 읽는 `/metrics` 의 warnings 에
    실린다."""
    server = FastMCP("files")

    @server.tool(name="write_file")
    def write_file(file_path: str, content: str) -> str:
        """원격 서버에 파일을 쓴다."""
        return "written remotely"

    async with serve(server.http_app()) as base_url:
        support_doc["tools"][0].update(url=f"{base_url}/mcp", allow=["write_file"])
        async with run_app(support_doc) as running:
            metrics = (await running.client.get("/metrics")).json()
    assert metrics["tools"]["count"] == 0
    [warning] = [w for w in metrics["warnings"] if w.startswith(f"{BUILTIN_TOOL_NAME}: ")]
    assert "(main: write_file from calc)" in warning
    assert "no_shared_state" in metrics["warnings"]
