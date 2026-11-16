"""PEM 판정 (코어). Redis TLS 의 CA(P61)를 받는 Database 와 클러스터 기본 Redis 가 같은 규칙으로
본다. 오류 코드와 칸 이름은 부르는 쪽마다 달라서 여기는 판정만 한다(`urls.is_http_url` 과 같다).
"""

from __future__ import annotations

import hashlib
import re
import ssl

# CA PEM 의 상한. 인증서 몇 장이면 충분하다
MAX_CA_PEM_BYTES = 64 * 1024

# 인증서 블록 하나. 머리와 꼬리의 대시는 다섯이고 라벨은 대문자 CERTIFICATE 이며 몸은 base64 줄이다
_CERTIFICATE_BLOCK = re.compile(
    r"-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----"
)


def is_ca_pem(pem: str) -> str | None:
    """PEM 인증서 블록만 든 값이면 다듬은 PEM(끝에 줄바꿈 하나)을, 아니면 None 을 돌려준다. 블록
    사이에는 공백만 올 수 있다. 개인 키(라벨에 PRIVATE KEY 가 든 블록, 대소문자 무관)나 다른 블록,
    소문자 라벨, 대시 수가 틀린 머리, 머리 없는 base64 가 하나라도 있으면 아니다. CA 는 문서와 응답,
    테넌트 Secret 으로 퍼지기 때문이다. 파이썬 TLS 가 신뢰 저장소로 읽지 못하는 값도 아니다. 돌려줄
    값(끝의 줄바꿈 포함)이 `MAX_CA_PEM_BYTES` 를 넘어도 아니다. 그 값을 저장하는 모델의 상한이다."""
    pem = pem.strip()
    if not pem or len(pem.encode()) >= MAX_CA_PEM_BYTES or "PRIVATE KEY" in pem.upper():
        return None
    blocks = _CERTIFICATE_BLOCK.findall(pem)
    if not blocks or _CERTIFICATE_BLOCK.sub("", pem).strip():
        return None
    try:
        ssl.create_default_context(cadata=pem)
    except (ssl.SSLError, ValueError):
        return None
    return pem + "\n"


def ca_sha256(pem: str | None) -> str | None:
    """CA PEM 의 지문. 다듬은 PEM(`is_ca_pem`)의 SHA-256 이고, CA 가 아니면 None 이다. 같은 CA 를
    줄 끝이나 앞뒤 공백만 달리 보내도 같은 값이다."""
    ca = is_ca_pem(pem or "")
    return hashlib.sha256(ca.encode()).hexdigest() if ca is not None else None
