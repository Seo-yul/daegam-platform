"""P56 F1: Gateway 의 code mode 판정 (6.2절). 광고하는 Tool 이 모두 읽기일 때만 켜고, 노출·소유자
Cedar·모르는 이름·예약 이름·읽기가 아닌 Tool 은 거절한다. 읽기는 P43 처럼 백엔드 종류를 아는 읽기다.
판정의 지문은 효과가 바뀌면 바뀐다."""

from __future__ import annotations

from typing import Any

import pytest

from daegam.services.gateway_code_mode import (
    CAUSE_IMAGE_UNMARKED,
    CAUSE_RESERVED,
    CAUSE_UNKNOWN_KIND,
    CAUSE_WEB,
    CAUSE_WRITE,
    CODE_MODE_EXPOSED,
    CODE_MODE_NOT_READ_ONLY,
    CODE_MODE_OWNER_CEDAR,
    CODE_MODE_RESERVED_NAME,
    CODE_MODE_UNKNOWN_NAMES,
    code_mode_verdict,
)
from daegam.services.tool_effects import EFFECT_READ, SourceTools, gateway_source
from daegam.store.hub import HubItem
from tests.helpers.gateways import backend_snapshot
from tests.test_tool_effects import _gateway
from tests.test_tool_marks import _item, _marked_server

READ = {"read_only": True}


def _tools(
    *backends,
    resolution: str = "priority",
    items: dict[str, HubItem] | None = None,
    published: dict[str, SourceTools] | None = None,
    **rules: Any,
) -> SourceTools:
    """listOrders(GET)·createOrder(POST)·orderExists(HEAD) 서버(`mi-1`) 위 Gateway 의 소스다."""
    gw = _gateway(*backends, resolution=resolution)
    servers = {"mi-1": _marked_server(**rules)}
    return gateway_source(gw, servers=servers, published=published or {}, items=items)


def _orders_backend(**extra: Any):
    return backend_snapshot(
        "orders",
        "internal",
        ["listOrders", "createOrder", "orderExists"],
        server_id="mi-1",
        **extra,
    )


def _judge(tools: SourceTools, **kw: Any):
    return code_mode_verdict(tools, exposed=False, owner_cedar=False, **kw)


def test_a_gateway_whose_every_tool_reads_may_turn_code_mode_on() -> None:
    found = _judge(_tools(_orders_backend(tools=["listOrders", "orderExists"])))
    assert found.ok and found.code is None
    assert found.effects == (("listOrders", "read"), ("orderExists", "read"))


def test_each_tool_that_does_not_read_is_named_with_its_cause() -> None:
    mail = _item({"list_mail": READ}, names=["list_mail", "send_mail"])
    tools = _tools(
        _orders_backend(),
        backend_snapshot("web-fetch", "platform", ["web_fetch"]),
        backend_snapshot("spike", "remote", ["Add"]),
        backend_snapshot("outside", "entry", ["Mul"]),
        backend_snapshot("mail", "image", ["list_mail", "send_mail"], hub_item_id="hi-mail"),
        items={"hi-mail": mail},
    )
    found = _judge(tools)
    assert found.code == CODE_MODE_NOT_READ_ONLY
    assert found.names == {
        "createOrder": CAUSE_WRITE,
        "web_fetch": CAUSE_WEB,
        "Add": "remote",
        "Mul": "entry",
        "send_mail": CAUSE_IMAGE_UNMARKED,
    }
    body = found.error()
    assert body["code"] == CODE_MODE_NOT_READ_ONLY and body["names"] == found.names


def test_a_read_whose_backend_kind_is_unknown_is_not_read() -> None:
    """게시된 백엔드 아래가 이름을 모르면 효과는 읽기여도 종류가 없다. P43 처럼 읽기로 세지
    않는다."""
    below = SourceTools(names=None, effects={"lookup": EFFECT_READ})
    backend = backend_snapshot(
        "pub", "published", ["lookup"], tools=["lookup"], hub_item_id="hi-pub"
    )
    found = _judge(_tools(backend, published={"hi-pub": below}))
    assert found.code == CODE_MODE_NOT_READ_ONLY
    assert found.names == {"lookup": CAUSE_UNKNOWN_KIND}


def test_a_backend_with_unknown_names_refuses_code_mode() -> None:
    tools = _tools(_orders_backend(tools=["listOrders"]), backend_snapshot("spike", "remote", []))
    found = _judge(tools, unknown_backends=["spike"])
    assert found.code == CODE_MODE_UNKNOWN_NAMES
    assert found.error()["backends"] == ["spike"]


@pytest.mark.parametrize(
    ("resolution", "code"), [("priority", CODE_MODE_RESERVED_NAME), ("prefix", None)]
)
def test_the_reserved_name_is_judged_after_prefixes(resolution: str, code: str | None) -> None:
    names = ["execute_tool_script", "list_mail"]
    mail = _item({n: READ for n in names}, names=names)
    backend = backend_snapshot("mail", "image", names, hub_item_id="hi-mail")
    found = _judge(_tools(backend, resolution=resolution, items={"hi-mail": mail}))
    assert found.code == code
    if code is not None:
        assert found.names == {"execute_tool_script": CAUSE_RESERVED}


def test_exposure_and_owner_cedar_come_before_the_tools() -> None:
    tools = _tools(_orders_backend())
    verdict = code_mode_verdict(tools, exposed=True, owner_cedar=True)
    assert verdict.code == CODE_MODE_EXPOSED
    assert code_mode_verdict(tools, exposed=False, owner_cedar=True).code == CODE_MODE_OWNER_CEDAR


def test_the_fingerprint_follows_the_effects() -> None:
    read = _judge(_tools(_orders_backend(tools=["listOrders"])))
    write = _judge(_tools(_orders_backend(tools=["listOrders"]), listOrders={"read_only": False}))
    assert write.code == CODE_MODE_NOT_READ_ONLY
    assert read.fingerprint() != write.fingerprint()
    assert read.fingerprint() == _judge(_tools(_orders_backend(tools=["listOrders"]))).fingerprint()


def test_an_allowing_verdict_has_no_error() -> None:
    with pytest.raises(ValueError):
        _judge(_tools(_orders_backend(tools=["listOrders"]))).error()
