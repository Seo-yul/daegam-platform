"""플랫폼이 렌더하는 Redis TLS 바인딩과 agent-runtime 이 읽는 규칙이 어긋나지 않게 막는다 (P61).

플랫폼은 `<PREFIX>_TLS`·`<PREFIX>_CA_FILE` 과 `rediss://` URL 을 렌더하고, 런타임은 같은 이름과
scheme 을 읽어 TLS 로만 붙는다. 플랫폼 자신의 접속(`redis_client_kwargs`)과 런타임의 접속
(`redis_tls_args`)은 서버 인증서와 호스트 이름을 같은 방식으로 확인해야 한다. 두 레포가 lock 과
이미지 컨텍스트를 따로 가져 같은 규칙을 두 벌 둔다. 한쪽만 바뀌면 Agent 가 평문으로 붙거나(env
이름이 다르다) 확인 없이 붙는다(인자가 다르다). `test_long_term_copy.py` 와 같은 이유의 드리프트
테스트다.
"""

from __future__ import annotations

from typing import Any

from daegam.k8s.templates.common import BINDING_CA_FILE, BINDING_TLS, binding_env_and_secrets
from daegam.services.data_services import redis_client_kwargs
from daegam.store.data_services import RedisTls
from tests.helpers.docs import data_service_doc
from tests.helpers.source import AGENT_RUNTIME, exec_excerpt, py_constant, read_source

RUNTIME_DSN = AGENT_RUNTIME / "dsn.py"
# 인증서 확인을 정하는 인자. TLS 를 켜는 것(`ssl`·`rediss://`)은 따로 본다
VERIFY_ARGS = ("ssl_cert_reqs", "ssl_check_hostname")


def _runtime_tls_args() -> Any:
    return exec_excerpt(RUNTIME_DSN, {"Any": Any}, functions=("redis_tls_args",))["redis_tls_args"]


def test_binding_env_suffixes_match() -> None:
    assert py_constant(RUNTIME_DSN, "REDIS_TLS_SUFFIX") == BINDING_TLS
    assert py_constant(RUNTIME_DSN, "REDIS_CA_FILE_SUFFIX") == BINDING_CA_FILE


def test_both_sides_verify_the_certificate_and_the_host_name_the_same_way() -> None:
    platform = redis_client_kwargs(
        data_service_doc(kind="redis", tls=RedisTls(port=6380, ca_secret="ds-x-ca")),
        None,
        ca="PEM",
    )
    runtime = _runtime_tls_args()("/ca.crt")
    assert platform["ssl"] is True
    assert {k: platform[k] for k in VERIFY_ARGS} == {k: runtime[k] for k in VERIFY_ARGS}
    assert runtime["ssl_ca_certs"] == "/ca.crt"


def test_both_sides_use_rediss_for_the_tls_url() -> None:
    """플랫폼은 TLS 바인딩의 URL 을 `rediss://` 로 렌더하고 런타임은 그 scheme 만 받는다."""
    ds = data_service_doc(kind="redis", tls=RedisTls(port=6380, ca_secret="ds-x-ca"))
    env, _secrets = binding_env_and_secrets("CACHE", ds, allow_tls=True)
    url = next(e["value"] for e in env if e["name"] == "CACHE_URL")
    assert url.startswith("rediss://")
    assert 'url.startswith("rediss://")' in read_source(RUNTIME_DSN), (
        "런타임이 TLS 바인딩의 URL scheme 을 다르게 본다. 플랫폼의 binding_url 도 함께 고친다"
    )
