"""바인딩 env 로 저장소 주소를 만든다(runtime/dsn.py). 개별 env 가 있으면 `_URL` 보다 앞서고
비밀번호의 예약 문자는 그대로 값이 된다. Postgres 는 conninfo 를, Redis 는 클라이언트 인자를
만든다."""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.conninfo import conninfo_to_dict

from runtime.config import ConfigError
from runtime.dsn import resolve_dsn, resolve_redis

RESERVED_PASSWORD = "p@ss:w/rd#?&=%2F 'quote' \\back"


REDIS_PARTS = {
    "DAEGAM_DB_0_HOST": "redis.svc",
    "DAEGAM_DB_0_PORT": "6380",
    "DAEGAM_DB_0_DB": "2",
    "DAEGAM_DB_0_USER": "daegam",
    "DAEGAM_DB_0_PASSWORD": RESERVED_PASSWORD,
    "DAEGAM_DB_0_URL": f"redis://daegam:{RESERVED_PASSWORD}@redis.svc:6380/2",
}
REDIS_DEFAULTS = {"port": 6379, "db": 0}
REDIS_URL_VALUE = "redis://:p@redis.svc:6379/0"


@pytest.mark.parametrize(
    ("name", "env", "expected"),
    [
        # 개별 env 가 있으면 `_URL` 을 무시하고 클라이언트 인자를 만든다. PORT·DB 는 정수이고
        # 비밀번호의 예약 문자는 그대로 값이다
        pytest.param(
            "DAEGAM_DB_0_URL",
            REDIS_PARTS,
            {
                "host": "redis.svc",
                "port": 6380,
                "db": 2,
                "username": "daegam",
                "password": RESERVED_PASSWORD,
            },
            id="parts-over-url",
        ),
        # 무인증 Redis 는 사용자·비밀번호 키가 없고 PORT·DB 가 비어 있으면 기본값이다
        pytest.param(
            "DAEGAM_DB_0_URL",
            {"DAEGAM_DB_0_HOST": "r"},
            {"host": "r", **REDIS_DEFAULTS},
            id="no-auth",
        ),
        pytest.param(
            "CACHE_DSN",
            {"CACHE_DSN_HOST": "h"},
            {"host": "h", **REDIS_DEFAULTS},
            id="no-url-suffix",
        ),
        pytest.param(
            "DAEGAM_DB_0_URL",
            {"DAEGAM_DB_0_URL": REDIS_URL_VALUE},
            {"url": REDIS_URL_VALUE},
            id="url",
        ),
        pytest.param("DAEGAM_DB_0_URL", {}, None, id="unset"),
        pytest.param("DAEGAM_DB_0_URL", {"DAEGAM_DB_0_URL": ""}, None, id="empty-url"),
    ],
)
def test_resolve_redis(name: str, env: dict[str, str], expected: dict[str, Any] | None) -> None:
    assert resolve_redis(name, env) == expected


TLS_CA = "/var/run/daegam/redis-ca/DAEGAM_DB_0/ca.crt"
TLS_ARGS = {
    "ssl_cert_reqs": "required",
    "ssl_check_hostname": True,
    "ssl_ca_certs": TLS_CA,
}


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        # TLS 바인딩(P61)은 PORT 가 TLS 포트이고 CA 로 서버 인증서와 이름을 확인한다
        pytest.param(
            {
                "DAEGAM_DB_0_HOST": "r",
                "DAEGAM_DB_0_PORT": "6380",
                "DAEGAM_DB_0_TLS": "1",
                "DAEGAM_DB_0_CA_FILE": TLS_CA,
            },
            {"host": "r", "port": 6380, "db": 0, "ssl": True, **TLS_ARGS},
            id="parts",
        ),
        # URL 만 있으면 rediss:// 가 TLS 를 켜고 인증서 확인 인자를 더한다
        pytest.param(
            {
                "DAEGAM_DB_0_URL": "rediss://:p@r:6380/0",
                "DAEGAM_DB_0_TLS": "1",
                "DAEGAM_DB_0_CA_FILE": TLS_CA,
            },
            {"url": "rediss://:p@r:6380/0", **TLS_ARGS},
            id="url",
        ),
        # TLS 가 1 이 아니면 예전 그대로다
        pytest.param(
            {"DAEGAM_DB_0_HOST": "r", "DAEGAM_DB_0_TLS": "0"},
            {"host": "r", **REDIS_DEFAULTS},
            id="off",
        ),
    ],
)
def test_resolve_redis_over_tls(env: dict[str, str], expected: dict[str, Any]) -> None:
    assert resolve_redis("DAEGAM_DB_0_URL", env) == expected


def test_a_tls_binding_never_falls_back_to_a_plain_url() -> None:
    """TLS 바인딩의 URL 이 redis:// 면 사람이 손댄 env 다. 평문으로 따르지 않고 기동을 멈춘다."""
    env = {"DAEGAM_DB_0_URL": "redis://r:6379/0", "DAEGAM_DB_0_TLS": "1"}
    with pytest.raises(ConfigError) as info:
        resolve_redis("DAEGAM_DB_0_URL", env)
    assert info.value.code == "memory_dsn_invalid"


def test_resolve_redis_refuses_a_bad_db_without_echoing_it() -> None:
    with pytest.raises(ConfigError) as info:
        resolve_redis("DAEGAM_DB_0_URL", {"DAEGAM_DB_0_HOST": "r", "DAEGAM_DB_0_DB": "zero"})
    assert info.value.code == "memory_dsn_invalid" and "zero" not in info.value.message


PG_PARTS = {
    "DAEGAM_DB_0_HOST": "pg.svc",
    "DAEGAM_DB_0_PORT": "5432",
    "DAEGAM_DB_0_DATABASE": "agent",
    "DAEGAM_DB_0_USER": "daegam",
    "DAEGAM_DB_0_PASSWORD": RESERVED_PASSWORD,
    # 플랫폼이 K8s env 확장으로 만든 URL. 예약 문자가 들어가 깨진 값이다
    "DAEGAM_DB_0_URL": f"postgresql://daegam:{RESERVED_PASSWORD}@pg.svc:5432/agent",
}
PG_URL = "postgresql://u:p@pg.svc:5432/agent"


@pytest.mark.parametrize(
    ("name", "env", "expected"),
    [
        # 개별 env 가 있으면 `_URL` 을 무시하고 conninfo 로 조립한다. 비밀번호의 URL 예약 문자·
        # 공백·따옴표·역슬래시가 그대로 값이 된다
        pytest.param(
            "DAEGAM_DB_0_URL",
            PG_PARTS,
            {
                "host": "pg.svc",
                "port": "5432",
                "dbname": "agent",
                "user": "daegam",
                "password": RESERVED_PASSWORD,
            },
            id="parts-over-url",
        ),
        # 비밀번호·사용자가 없는 바인딩(무인증 Postgres)은 그 키를 넣지 않는다
        pytest.param(
            "DAEGAM_DB_0_URL",
            {"DAEGAM_DB_0_HOST": "pg.svc", "DAEGAM_DB_0_PORT": "5432", "DAEGAM_DB_0_DATABASE": "d"},
            {"host": "pg.svc", "port": "5432", "dbname": "d"},
            id="no-auth",
        ),
        # 접미어가 _URL 이 아닌 이름도 접두어를 그대로 쓴다
        pytest.param("PG_DSN", {"PG_DSN_HOST": "h"}, {"host": "h"}, id="no-url-suffix"),
        pytest.param("DAEGAM_DB_0_URL", {"DAEGAM_DB_0_URL": PG_URL}, PG_URL, id="url"),
        pytest.param("DAEGAM_DB_0_URL", {}, None, id="unset"),
        pytest.param("DAEGAM_DB_0_URL", {"DAEGAM_DB_0_URL": ""}, None, id="empty-url"),
    ],
)
def test_resolve_dsn(name: str, env: dict[str, str], expected: dict[str, str] | str | None) -> None:
    """개별 env 로 조립한 것은 conninfo 라 키별로 견준다. 아니면 `_URL` 그대로이거나 None 이다."""
    dsn = resolve_dsn(name, env)
    if isinstance(expected, dict):
        assert dsn is not None and not dsn.startswith("postgresql://")
        assert conninfo_to_dict(dsn) == expected
    else:
        assert dsn == expected
