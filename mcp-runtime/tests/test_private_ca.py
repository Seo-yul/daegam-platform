"""관리자가 준 CA(env DAEGAM_CA_B64)로 사설 CA 가 서명한 https 백엔드를 부른다 (D88).

시험마다 CA 와 127.0.0.1 서버 인증서를 새로 만들고 그 인증서로 https 서버를 띄운다. 이미지의 기본
루트는 베이스가 가리키는 SSL_CERT_FILE 이므로 시험도 certifi 묶음을 SSL_CERT_FILE 로 둔다. 서버는
파드처럼 옵션 없이 만들어 env 를 스스로 읽게 한다.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import ssl
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, NamedTuple

import certifi
import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastmcp import Client
from fastmcp.exceptions import ToolError

from runtime.main import CA_ENV, _pod_options, build_composite_server, build_server
from runtime.profile import ProfileConfig, parse_profile
from tests.conftest import serve

CA_NAME = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "daegam test private CA")])
PETS = [{"id": 1, "name": "rex"}]


class PrivateCA(NamedTuple):
    b64: str  # DAEGAM_CA_B64 의 값
    server: ssl.SSLContext  # 그 CA 가 서명한 127.0.0.1 서버 문맥


def _usage(*, ca: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=not ca,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=ca,
        crl_sign=ca,
        encipher_only=False,
        decipher_only=False,
    )


def _cert(
    subject: x509.Name,
    key: ec.EllipticCurvePrivateKey,
    issuer_key: ec.EllipticCurvePrivateKey,
    *,
    ca: bool,
) -> x509.Certificate:
    """CPython 3.13 의 엄격한 검사(VERIFY_X509_STRICT)를 지나는 확장을 단다
    (platform/tests/helpers/tls.py 와 같다)."""
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(CA_NAME)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None), critical=True)
        .add_extension(_usage(ca=ca), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    if not ca:
        san = x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
        server_auth = x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH])
        aki = x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key())
        builder = (
            builder.add_extension(san, critical=False)
            .add_extension(server_auth, critical=False)
            .add_extension(aki, critical=False)
        )
    return builder.sign(issuer_key, hashes.SHA256())


def _b64(pem: bytes) -> str:
    return base64.b64encode(pem).decode()


@pytest.fixture
def private_ca(tmp_path: Path) -> PrivateCA:
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _cert(CA_NAME, ca_key, ca_key, ca=True)
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    leaf = _cert(leaf_name, leaf_key, ca_key, ca=False)
    cert_file, key_file = tmp_path / "server.pem", tmp_path / "server.key"
    cert_file.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_file, key_file)
    return PrivateCA(_b64(ca.public_bytes(serialization.Encoding.PEM)), server)


@pytest.fixture
def certifi_roots(monkeypatch) -> int:
    """이미지 베이스처럼 SSL_CERT_FILE 이 기본 루트를 가리킨다. 그 루트의 수를 준다."""
    monkeypatch.setenv("SSL_CERT_FILE", certifi.where())
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    return len(ssl.create_default_context(cafile=certifi.where()).get_ca_certs())


class _Pets(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 의 이름)
        body = json.dumps(PETS).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: Any) -> None:
        return


@pytest.fixture
def private_backend(private_ca, certifi_roots) -> Iterator[str]:
    with serve(_Pets, tls=private_ca.server) as base_url:
        yield base_url


async def _list_pets(spec: dict, endpoint: str) -> Any:
    mcp = build_server(spec, ProfileConfig(tools=["get_pets"], endpoint=endpoint))
    async with Client(mcp) as c:
        return await c.call_tool("get_pets", {})


@pytest.mark.parametrize("trust_env", ["true", "false"])
async def test_the_given_ca_lets_the_backend_be_called(
    trust_env, petstore_spec, private_ca, private_backend, monkeypatch
):
    monkeypatch.setenv(CA_ENV, private_ca.b64)
    monkeypatch.setenv("SPEC2OPENAPI_TRUST_ENV", trust_env)
    res = await _list_pets(petstore_spec, private_backend)
    assert res.structured_content == {"result": PETS}


async def test_every_backend_of_a_composite_server_trusts_the_ca(
    petstore_spec, private_ca, private_backend, monkeypatch
):
    monkeypatch.setenv(CA_ENV, private_ca.b64)
    profile = parse_profile(
        {
            "backends": [
                {
                    "spec": f"openapi-{i}.yaml",
                    "endpoint": private_backend,
                    "tools": ["get_pets"],
                    "tool_overrides": {"get_pets": {"name": name}},
                }
                for i, name in enumerate(("pets_a", "pets_b"))
            ]
        }
    )
    mcp = build_composite_server([petstore_spec, petstore_spec], profile)
    async with Client(mcp) as c:
        for name in ("pets_a", "pets_b"):
            assert (await c.call_tool(name, {})).structured_content == {"result": PETS}


async def test_without_the_ca_the_backend_certificate_is_refused(petstore_spec, private_backend):
    with pytest.raises(ToolError, match="CERTIFICATE_VERIFY_FAILED"):
        await _list_pets(petstore_spec, private_backend)


@pytest.mark.parametrize("trust_env", [True, False])
def test_the_ca_is_added_to_the_default_roots(
    trust_env, private_ca, certifi_roots, monkeypatch, caplog
):
    """환경을 믿으면 SSL_CERT_FILE 의 루트에 더하고, 믿지 않으면 httpx2 가 쓰는 시스템 저장소
    문맥(truststore)에 더한다. 그 문맥은 든 인증서를 보여 주지 않아 종류만 보고, 그 길로 부르는
    것은 `test_the_given_ca_lets_the_backend_be_called` 가 본다."""
    monkeypatch.setenv(CA_ENV, private_ca.b64)
    monkeypatch.setenv("SPEC2OPENAPI_TRUST_ENV", str(trust_env).lower())
    default = httpx2.create_ssl_context(verify=True, trust_env=trust_env)
    with caplog.at_level(logging.INFO, logger="daegam.mcp-runtime"):
        verify = _pod_options().verify
    assert type(verify) is type(default)
    if trust_env:
        assert len(verify.get_ca_certs()) == certifi_roots + 1
    assert f"trusting 1 extra CA certificate(s) from {CA_ENV}" in caplog.text


def test_turned_off_verification_ignores_the_ca(private_ca, monkeypatch):
    monkeypatch.setenv(CA_ENV, private_ca.b64)
    monkeypatch.setenv("SPEC2OPENAPI_VERIFY", "false")
    assert _pod_options().verify is False


@pytest.mark.parametrize("value", ["not base64!", _b64(b"not a certificate")])
def test_an_unreadable_ca_is_logged_and_ignored(value, monkeypatch, caplog):
    monkeypatch.setenv(CA_ENV, value)
    with caplog.at_level(logging.WARNING, logger="daegam.mcp-runtime"):
        assert _pod_options().verify is True
    assert f"{CA_ENV} is not a base64-encoded PEM certificate bundle" in caplog.text
