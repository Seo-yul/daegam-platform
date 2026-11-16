"""Advanced 편집으로 더한 컨테이너의 보안 값 (P34 H4).

`apply_overrides` 는 렌더에 없던 이름의 컨테이너·init 컨테이너에 `FOREIGN_CONTAINER_SECURITY` 를
걸고 렌더가 만든 것은 그대로 둔다. 그 세 값을 덮어도 잃는 것이 없다는 전제는 잠금 표의
`containers[*]` 와일드카드가 어떤 이름에든 걸린다는 것이라 그것도 함께 본다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from daegam.k8s.merge import (
    _CONTAINER_LISTS,
    AGENT_LOCKED_PATHS,
    LOCKED_PATHS,
    POD_ROOTS,
    VMCP_LOCKED_PATHS,
    MergeError,
    Path,
    apply_overrides,
    check_overrides,
    diff,
    drop_enforced_security,
)
from daegam.k8s.templates.agent import AGENT_CONTAINER, INIT_CONTAINER
from daegam.k8s.templates.common import CONTAINER_SECURITY, FOREIGN_CONTAINER_SECURITY
from daegam.k8s.templates.mcpserver import FETCH_INIT

MCP_ROOT, AGENT_ROOT = POD_ROOTS
# 잠금 표가 막는 세 값의 약한 쪽. 사용자가 이것을 쓰지 못해야 덮어도 잃는 것이 없다
WEAKENED: dict[str, Any] = {
    "allowPrivilegeEscalation": True,
    "capabilities": {"add": ["NET_ADMIN"]},
    "seccompProfile": {"type": "Unconfined"},
}


def _nest(root: Path, pod: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = pod
    for seg in reversed(root):
        out = {seg: out}
    return out


def _pod(doc: dict[str, Any], root: Path) -> dict[str, Any]:
    for seg in root:
        doc = doc[seg]
    return doc


def _named(pod: dict[str, Any], kind: str, name: str) -> dict[str, Any]:
    return next(c for c in pod[kind] if c["name"] == name)


def _rendered_pod(main: str, init: str) -> dict[str, Any]:
    return {
        "containers": [{"name": main, "image": "rt:1", "securityContext": CONTAINER_SECURITY}],
        "initContainers": [{"name": init, "image": "curl", "securityContext": CONTAINER_SECURITY}],
    }


def _mcpserver() -> dict[str, Any]:
    doc = _nest(MCP_ROOT, _rendered_pod("mcp", FETCH_INIT))
    return {"apiVersion": "toolhive.stacklok.dev/v1beta1", "kind": "MCPServer", **doc}


def _deployment() -> dict[str, Any]:
    doc = _nest(AGENT_ROOT, _rendered_pod(AGENT_CONTAINER, INIT_CONTAINER))
    return {"apiVersion": "apps/v1", "kind": "Deployment", **doc}


@dataclass(frozen=True)
class Root:
    """파드 뿌리 하나. 그 CR 의 렌더, 뿌리 경로, 렌더가 만든 메인 컨테이너 이름, 잠금 표다."""

    render: Callable[[], dict[str, Any]]
    path: Path
    main: str
    locked: tuple[Path, ...]


ROOTS = [
    pytest.param(Root(_mcpserver, MCP_ROOT, "mcp", LOCKED_PATHS), id="mcpserver"),
    pytest.param(
        Root(_deployment, AGENT_ROOT, AGENT_CONTAINER, AGENT_LOCKED_PATHS), id="agent-deployment"
    ),
]


def _sidecars(root: Root) -> dict[str, Any]:
    """메인의 자원을 바꾸고 사이드카 하나와 init 하나를 더하는 overrides."""
    return _nest(
        root.path,
        {
            "containers": [
                {"name": root.main, "resources": {"limits": {"cpu": "1"}}},
                {
                    "name": "side",
                    "image": "busybox",
                    "securityContext": {"runAsUser": 1234, "readOnlyRootFilesystem": True},
                },
            ],
            "initContainers": [{"name": "warm", "image": "busybox"}],
        },
    )


@pytest.mark.parametrize("root", ROOTS)
def test_added_containers_get_the_foreign_security_values(root: Root) -> None:
    rendered = root.render()
    pod = _pod(apply_overrides(rendered, _sidecars(root)), root.path)
    side = _named(pod, "containers", "side")
    assert side["securityContext"] == {
        "runAsUser": 1234,
        "readOnlyRootFilesystem": True,
        **FOREIGN_CONTAINER_SECURITY,
    }
    assert _named(pod, "initContainers", "warm")["securityContext"] == FOREIGN_CONTAINER_SECURITY
    # 렌더가 만든 메인·init 은 렌더의 값 그대로다
    assert _named(pod, "containers", root.main)["securityContext"] == CONTAINER_SECURITY
    for init in _pod(rendered, root.path)["initContainers"]:
        assert _named(pod, "initContainers", init["name"]) == init
    # 건 값은 사본이다. 한 CR 을 고쳐도 상수와 다른 CR 에 번지지 않는다
    side["securityContext"]["capabilities"]["drop"].append("x")
    assert FOREIGN_CONTAINER_SECURITY["capabilities"] == {"drop": ["ALL"]}


@pytest.mark.parametrize("root", ROOTS)
def test_rendered_containers_do_not_drift(root: Root) -> None:
    """렌더에 있던 이름만 고치는 overrides 는 그 차이만 남긴다. 보안 값이 없는 렌더 컨테이너(vMCP 의
    vmcp 처럼)에도 아무것도 더하지 않는다."""
    rendered = root.render()
    del _named(_pod(rendered, root.path), "containers", root.main)["securityContext"]
    main = {"name": root.main, "resources": {"limits": {"cpu": "1"}}}
    overrides = _nest(root.path, {"containers": [main]})
    final = apply_overrides(rendered, overrides)
    assert diff(rendered, final) == overrides
    assert "securityContext" not in _named(_pod(final, root.path), "containers", root.main)


@pytest.mark.parametrize("field", sorted(WEAKENED))
@pytest.mark.parametrize("kind", _CONTAINER_LISTS)
@pytest.mark.parametrize(
    "root, locked",
    [
        pytest.param(MCP_ROOT, LOCKED_PATHS, id="mcpserver"),
        pytest.param(MCP_ROOT, VMCP_LOCKED_PATHS, id="vmcp"),
        pytest.param(AGENT_ROOT, AGENT_LOCKED_PATHS, id="agent-deployment"),
    ],
)
def test_any_container_name_is_locked_out_of_the_enforced_fields(
    root: Path, locked: tuple[Path, ...], kind: str, field: str
) -> None:
    """`containers[*]` 잠금은 렌더에 없는 아무 이름에도 걸린다. 그래서 세 값을 덮어도 사용자가 쓴
    것을 잃지 않는다. 약하게 쓰든 securityContext 째 지우든 같다."""
    for security in ({field: WEAKENED[field]}, None):
        container = {"name": "any-sidecar-name", "securityContext": security}
        with pytest.raises(MergeError) as e:
            check_overrides(_nest(root, {kind: [container]}), locked=locked)
        assert e.value.code == "locked_field"


@pytest.mark.parametrize("root", ROOTS)
def test_the_edited_final_saves_again_without_the_enforced_values(root: Root) -> None:
    """편집기는 최종본을 보인다. 그대로 다시 저장하면 diff 에 플랫폼이 건 값이 실려 오는데, 그것을
    빼야 잠금에 걸리지 않고 overrides 에는 사용자가 쓴 것만 남는다."""
    rendered = root.render()
    overrides = _sidecars(root)
    final = apply_overrides(rendered, overrides)
    edited = diff(rendered, final)
    patch = drop_enforced_security(rendered, edited)
    assert patch == overrides
    # 사본에서 뺀다. 받은 diff 는 그대로다
    assert _named(_pod(edited, root.path), "initContainers", "warm")["securityContext"]
    check_overrides(patch, locked=root.locked)
    assert apply_overrides(rendered, patch) == final


@pytest.mark.parametrize("root", ROOTS)
def test_a_weakened_sidecar_in_the_edited_final_is_still_locked(root: Root) -> None:
    """값이 플랫폼의 것과 다르면 빼지 않는다. 잠금 검사가 그 자리를 잡는다."""
    rendered = root.render()
    edited = apply_overrides(rendered, _sidecars(root))
    side = _named(_pod(edited, root.path), "containers", "side")
    side["securityContext"]["allowPrivilegeEscalation"] = True
    patch = drop_enforced_security(rendered, diff(rendered, edited))
    with pytest.raises(MergeError) as e:
        check_overrides(patch, locked=root.locked)
    assert e.value.code == "locked_field"
    assert e.value.path == (
        *root.path,
        "containers",
        "name=side",
        "securityContext",
        "allowPrivilegeEscalation",
    )
