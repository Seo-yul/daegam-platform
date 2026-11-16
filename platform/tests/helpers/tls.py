"""TLS 를 보는 시험과 e2e 가 함께 쓰는 자체 서명 CA 와 서버 인증서 (P61, P85 9절).

`new_ca` 와 `new_server` 는 파이썬의 엄격한 인증서 검사(3.13 의 VERIFY_X509_STRICT)도 받는
P-256 인증서를 cryptography 로 만든다. CA 는 critical basicConstraints CA:TRUE, keyUsage
keyCertSign·cRLSign, 주체 키 식별자를, 서버 인증서는 기관 키 식별자, 주체 키 식별자,
extendedKeyUsage serverAuth 와 이름들의 SAN 을 갖는다. `ca_pem` 은 CA 인증서 PEM 하나이고 키는 시험
프로세스마다 이름마다 한 번 만든다. `key_pem` 은 CA 칸에 섞여 오면 안 되는 개인 키 PEM 이다.
scripts/e2e_instance.sh STEP 13 은 사설 CA 뒤의 https 백엔드를 이것으로 만든다.
"""

from __future__ import annotations

import datetime
from functools import cache

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from daegam.k8s.templates.common import redis_ca_mark
from daegam.pem import ca_sha256


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


def _window(days: int) -> tuple[datetime.datetime, datetime.datetime]:
    now = datetime.datetime.now(datetime.UTC)
    return now - datetime.timedelta(minutes=5), now + datetime.timedelta(days=days)


def new_ca(
    common_name: str, *, days: int = 1
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """CA 의 키와 인증서. days 일 동안 쓸 수 있다."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    start, end = _window(days)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_usage(ca=True), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def new_server(
    ca_key: ec.EllipticCurvePrivateKey, ca: x509.Certificate, names: list[str], *, days: int = 1
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """ca 가 서명한 서버 인증서와 그 키. names 가 SAN 이고 days 일 동안 쓸 수 있다."""
    key = ec.generate_private_key(ec.SECP256R1())
    start, end = _window(days)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_usage(ca=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def cert_bytes(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def key_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


@cache
def ca_pem(common_name: str = "daegam-test-ca") -> str:
    """CA 인증서 PEM. 하루 동안 쓸 수 있다."""
    _key, cert = new_ca(common_name)
    return cert_bytes(cert).decode()


@cache
def key_pem() -> str:
    """개인 키 PEM(PKCS#8) 하나."""
    key, _cert = new_ca("daegam-test-key")
    return key_bytes(key).decode()


def ca_mark(source: str, pem: str) -> str:
    """그 출처(Database id 나 `default_redis`)의 CA 를 믿는 파드의
    `daegam.devops.ai.kr/redis-ca` 값."""
    return redis_ca_mark({source: ca_sha256(pem)}) or ""
