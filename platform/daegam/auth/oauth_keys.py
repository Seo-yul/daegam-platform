"""OAuth 인가 서버의 두 HMAC 키와 봉인·풀기 (P33 3절·4절).

인가가 성공하기 전에는 서버에 아무것도 두지 않는다. DCR `client_id` 는 등록 값을, 요청 핸들은 인가
요청을 서명해 그 글자 안에 싣는다. 둘은 같은 꼴 `<접두>.<base64url(JSON)>.<base64url(HMAC-SHA256)>`
이라 봉인과 풀기 한 쌍(`Seal`)이 둘을 만든다. JSON 은 키를 정렬해 적으므로 같은 값은 같은 글자가
된다. HMAC 은 접두와 본문 글자 위에 걸고, 풀 때는 다시 만든 HMAC 글자와 `hmac.compare_digest` 로
맞댄다. 받은 글자를 고쳐 적어 맞대지 않으므로 같은 값을 다른 글자로 적은 것은 받지 않는다.

두 키는 플랫폼 서명 키에서 HKDF-SHA256(salt `daegam-oauth-v1`, info `dcr`·`request`)으로 만든다
(`PlatformSigner.derive`). 서명 키를 바꾸면 DCR `client_id` 와 살아 있는 핸들이 모두 무효가 된다.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from jwt.utils import base64url_decode, base64url_encode

from daegam.auth.signer import PlatformSigner

OAUTH_SALT = b"daegam-oauth-v1"
KEY_BYTES = 32
DCR_PREFIX = "dcr"
REQUEST_PREFIX = "v1"
# DCR client_id 는 클라이언트 설정과 URL 에 오가는 글자라 HMAC 의 앞 16바이트만 싣는다. 요청 핸들은
# 32바이트 전부다
DCR_MAC_BYTES = 16
REQUEST_MAC_BYTES = 32


@dataclass(frozen=True)
class Seal:
    """접두 하나와 키 하나의 봉인. `seal` 이 만든 글자만 `unseal` 이 푼다."""

    key: bytes = field(repr=False)
    prefix: str
    mac_bytes: int

    def seal(self, payload: Mapping[str, Any]) -> str:
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        body = base64url_encode(text.encode("ascii")).decode("ascii")
        return f"{self.prefix}.{body}.{self._mac(body)}"

    def unseal(self, token: str) -> dict[str, Any] | None:
        """서명이 맞으면 JSON 객체를, 아니면 None 을 준다. 꼴이 틀린 것과 서명이 틀린 것을 가르지
        않는다."""
        prefix, _, rest = token.partition(".")
        body, _, mac = rest.partition(".")
        if prefix != self.prefix or not (body.isascii() and mac.isascii()):
            return None
        if not hmac.compare_digest(mac.encode("ascii"), self._mac(body).encode("ascii")):
            return None
        try:
            payload = json.loads(base64url_decode(body))
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def _mac(self, body: str) -> str:
        digest = hmac.new(self.key, f"{self.prefix}.{body}".encode("ascii"), hashlib.sha256)
        return base64url_encode(digest.digest()[: self.mac_bytes]).decode("ascii")


@dataclass(frozen=True)
class OAuthKeys:
    dcr: Seal
    request: Seal


@lru_cache(maxsize=2)
def oauth_keys(signer: PlatformSigner) -> OAuthKeys:
    """서명 키의 두 봉인. 레플리카가 서명 키마다 한 번 만든다."""

    def seal(info: bytes, prefix: str, mac_bytes: int) -> Seal:
        return Seal(signer.derive(OAUTH_SALT, info, KEY_BYTES), prefix, mac_bytes)

    return OAuthKeys(
        dcr=seal(b"dcr", DCR_PREFIX, DCR_MAC_BYTES),
        request=seal(b"request", REQUEST_PREFIX, REQUEST_MAC_BYTES),
    )
