"""proxyrunner 헬스체크(GET /)와 GET /healthz 가 200 을 돌려준다."""

from __future__ import annotations

import httpx2
from spec2openapi.bridge import BridgeOptions

from runtime.main import build_server
from runtime.profile import parse_profile


async def test_health_routes_and_mcp_path(calculator_spec):
    profile = parse_profile({"tools": ["Add"], "soap": True})
    mcp = build_server(calculator_spec, profile, BridgeOptions())
    app = mcp.http_app(path="/mcp")
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as c:
            for path in ("/", "/healthz"):
                r = await c.get(path)
                assert r.status_code == 200, path
                assert r.json() == {"status": "ok"}
            # MCP 엔드포인트는 그대로 살아 있다 (GET without session -> not 404)
            r = await c.get("/mcp")
            assert r.status_code != 404
