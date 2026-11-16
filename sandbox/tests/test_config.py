"""파드 env 의 대상 목록과 토큰 해시, 명령의 고정 env (P40 3절, 8절)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from daegam_sandbox.config import (
    ConfigError,
    Layout,
    command_env,
    parse_targets,
    parse_token_hash,
    take_server_env,
)


def test_targets_parse_with_defaults() -> None:
    raw = json.dumps(
        [
            {"kind": "tcp", "host": "10.0.0.1", "port": 6443},
            {"kind": "dns", "host": "10.43.0.10", "port": 53, "timeout": 1.5},
            {"kind": "icmp", "host": "1.1.1.1"},
            {"kind": "tcp", "host": "fd00::1", "port": 22},
        ]
    )
    tcp, dns, icmp, v6 = parse_targets(raw)
    assert (tcp.kind, tcp.host, tcp.port, tcp.timeout) == ("tcp", "10.0.0.1", 6443, 2.0)
    assert dns.timeout == 1.5
    assert icmp.port is None and icmp.label() == "icmp 1.1.1.1"
    assert v6.label() == "tcp [fd00::1]:22"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not json",
        "[]",
        "{}",
        '[{"kind": "udp", "host": "10.0.0.1", "port": 1}]',
        '[{"kind": "tcp", "host": "kubernetes.default", "port": 443}]',
        '[{"kind": "tcp", "host": "10.0.0.1"}]',
        '[{"kind": "tcp", "host": "10.0.0.1", "port": 70000}]',
        '[{"kind": "tcp", "host": "10.0.0.1", "port": true}]',
        '[{"kind": "icmp", "host": "10.0.0.1", "port": 1}]',
        '[{"kind": "tcp", "host": "10.0.0.1", "port": 1, "timeout": 11}]',
        '[{"kind": "tcp", "host": "10.0.0.1", "port": 1, "timeout": 0}]',
        '[{"kind": "tcp", "host": "10.0.0.1", "port": 1, "extra": 1}]',
    ],
)
def test_bad_targets_are_refused(raw: str | None) -> None:
    with pytest.raises(ConfigError):
        parse_targets(raw)


def test_token_hash_is_64_lowercase_hex() -> None:
    assert parse_token_hash("ab" * 32) == bytes.fromhex("ab" * 32)
    for bad in (None, "", "AB" * 32, "ab" * 31, "zz" * 32):
        with pytest.raises(ConfigError):
            parse_token_hash(bad)


def test_server_env_leaves_the_environment() -> None:
    environ = {"SANDBOX_TOKEN_SHA256": "x", "SANDBOX_TARGETS": "[]", "PATH": "/bin"}
    taken = take_server_env(environ)
    assert taken == {"SANDBOX_TOKEN_SHA256": "x", "SANDBOX_TARGETS": "[]"}
    assert environ == {"PATH": "/bin"}


def test_the_command_env_is_fixed() -> None:
    env = command_env(Layout(Path("/")))
    assert env == {
        "PATH": "/app/.venv/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": "/tmp/home",
        "MPLCONFIGDIR": "/tmp/home/.config/matplotlib",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
    }
