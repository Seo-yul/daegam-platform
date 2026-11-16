"""서버가 파드 env 에서 읽는 값과 파일 배치 (P40 3절, 8절).

플랫폼이 파드 env 로 주는 것은 셋이다. 토큰의 sha256(`SANDBOX_TOKEN_SHA256`), 자기 점검이 닿지
않아야 하는 대상 목록(`SANDBOX_TARGETS`), 개발용 점검 생략(`SANDBOX_UNCHECKED`). 서버는 시작할 때
읽고 자기 environ 에서 지운다. 명령은 `command_env` 의 고정 env 만 받는다.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

TOKEN_ENV = "SANDBOX_TOKEN_SHA256"
TARGETS_ENV = "SANDBOX_TARGETS"
UNCHECKED_ENV = "SANDBOX_UNCHECKED"
SERVER_ENVS = (TOKEN_ENV, TARGETS_ENV, UNCHECKED_ENV)

# 대상 하나를 기다리는 시간. 양성 대조(F3)가 같은 값을 쓴다
DEFAULT_TARGET_TIMEOUT = 2.0
MAX_TARGET_TIMEOUT = 10.0
MAX_TARGETS = 512
TARGET_KINDS = ("tcp", "dns", "icmp")

# 명령이 쓰는 venv(이미지의 /app/.venv)가 맨 앞이다. 서버는 이 venv 를 쓰지 않는다
COMMAND_PATH = "/app/.venv/bin:/usr/local/bin:/usr/bin:/bin"
# CPU limit 이 1 이라 수치 라이브러리의 스레드도 하나다
THREAD_COUNT = "1"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ConfigError(ValueError):
    """파드 env 가 모자라거나 꼴이 틀렸다. 서버는 점검을 하지 못한 것(86)으로 끝난다."""


@dataclass(frozen=True)
class Target:
    """자기 점검이 닿지 않아야 하는 대상 하나. host 는 IP 글자다(샌드박스에는 DNS 가 없다)."""

    kind: str
    host: str
    port: int | None
    timeout: float

    def label(self) -> str:
        if self.port is None:
            return f"{self.kind} {self.host}"
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.kind} {host}:{self.port}"


def parse_targets(raw: str | None) -> tuple[Target, ...]:
    """`SANDBOX_TARGETS` 의 JSON 배열 -> 대상들. 비었거나 꼴이 틀리면 ConfigError 다.

    대상이 하나도 없는 점검은 아무것도 증명하지 못하므로 빈 배열도 거절한다.
    """
    if not raw:
        raise ConfigError(f"{TARGETS_ENV} 가 없다")
    try:
        items = json.loads(raw)
    except ValueError as e:
        raise ConfigError(f"{TARGETS_ENV} 가 JSON 이 아니다: {e}") from e
    if not isinstance(items, list) or not items:
        raise ConfigError(f"{TARGETS_ENV} 는 비지 않은 배열이어야 한다")
    if len(items) > MAX_TARGETS:
        raise ConfigError(f"{TARGETS_ENV} 의 대상이 {MAX_TARGETS} 개를 넘는다")
    return tuple(_target(i, item) for i, item in enumerate(items))


def _target(i: int, item: object) -> Target:
    where = f"{TARGETS_ENV}[{i}]"
    if not isinstance(item, dict):
        raise ConfigError(f"{where} 가 객체가 아니다")
    unknown = set(item) - {"kind", "host", "port", "timeout"}
    if unknown:
        raise ConfigError(f"{where} 에 모르는 키가 있다: {sorted(unknown)}")
    kind = item.get("kind")
    if kind not in TARGET_KINDS:
        raise ConfigError(f"{where}.kind 는 {TARGET_KINDS} 가운데 하나다")
    host = item.get("host")
    try:
        host = str(ipaddress.ip_address(host)) if isinstance(host, str) else None
    except ValueError:
        host = None
    if host is None:
        raise ConfigError(f"{where}.host 는 IP 글자여야 한다")
    port = item.get("port")
    if kind == "icmp":
        if port is not None:
            raise ConfigError(f"{where} 는 icmp 라 port 가 없다")
    elif not (isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536):
        raise ConfigError(f"{where}.port 는 1~65535 의 정수다")
    timeout = item.get("timeout", DEFAULT_TARGET_TIMEOUT)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= MAX_TARGET_TIMEOUT
    ):
        raise ConfigError(f"{where}.timeout 은 0 초과 {MAX_TARGET_TIMEOUT} 이하의 초다")
    return Target(kind=kind, host=host, port=port, timeout=float(timeout))


def parse_token_hash(raw: str | None) -> bytes:
    """`SANDBOX_TOKEN_SHA256`(소문자 16진 64자) -> 32 바이트. 증명의 HMAC 키이기도 하다."""
    if not raw or not _HEX64.match(raw):
        raise ConfigError(f"{TOKEN_ENV} 는 소문자 16진 64자여야 한다")
    return bytes.fromhex(raw)


def take_server_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """서버 env 셋을 읽고 environ 에서 지운다. 지운 뒤에도 /proc/1/environ 에는 처음 env 가 남으므로
    같은 uid 가 읽지 못하게 하는 것은 dumpable 끄기다(hardening.py)."""
    env = os.environ if environ is None else environ
    return {name: env.pop(name) for name in SERVER_ENVS if name in env}


@dataclass(frozen=True)
class Layout:
    """파일 배치. 파드에서 root 는 `/` 이고, 시험은 임시 디렉터리를 root 로 준다."""

    root: Path

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    @property
    def home(self) -> Path:
        return self.tmp / "home"

    @property
    def mplconfig(self) -> Path:
        return self.home / ".config" / "matplotlib"


def command_env(layout: Layout) -> dict[str, str]:
    """명령이 받는 고정 env. 서버의 env 는 하나도 넘기지 않는다."""
    return {
        "PATH": COMMAND_PATH,
        "LANG": "C.UTF-8",
        "HOME": str(layout.home),
        "MPLCONFIGDIR": str(layout.mplconfig),
        "OPENBLAS_NUM_THREADS": THREAD_COUNT,
        "OMP_NUM_THREADS": THREAD_COUNT,
    }
