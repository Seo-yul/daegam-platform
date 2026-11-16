"""샌드박스 토큰의 키 (P40 Security, D95).

샌드박스마다의 토큰은 `HMAC-SHA256(key, "sandbox:v1:" + sandbox_id)` 의 base64url 이다. key 는 이
쓰임에만 쓰는 32 바이트이고 플랫폼 네임스페이스의 Secret `daegam-sandbox-key` 에 있다. 파드에는
토큰이 아니라 그 sha256 만 env 로 간다(`SANDBOX_TOKEN_SHA256`). 샌드박스 서버는 Bearer 의 sha256 을
그것과 맞대고, 새 연결의 증명(`/proof`)도 그 sha256 을 키로 한 HMAC 이다. 키를 바꾸면 열린
샌드박스의 토큰이 모두 맞지 않으므로 그것들을 잃는다. 토큰과 키는 로그·이벤트·오류에 싣지 않는다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from daegam.secrets import SecretStore

SANDBOX_KEY_SECRET = "daegam-sandbox-key"
KEY_FIELD = "key"
KEY_BYTES = 32
TOKEN_CONTEXT = b"sandbox:v1:"


class SandboxKey:
    """샌드박스 토큰을 만드는 키. repr 에 키를 싣지 않는다."""

    def __init__(self, key: bytes):
        if len(key) != KEY_BYTES:
            raise ValueError(f"the sandbox key must be {KEY_BYTES} bytes")
        self._key = key

    def __repr__(self) -> str:
        return "SandboxKey(<hidden>)"

    @classmethod
    def generate(cls) -> SandboxKey:
        return cls(secrets.token_bytes(KEY_BYTES))

    @classmethod
    def load_or_create(cls, store: SecretStore, *, namespace: str) -> SandboxKey:
        """Secret 의 키를 읽고, 없으면 만든다. 만들기는 create-if-absent 라 레플리카 둘이 함께 떠도
        먼저 만든 쪽의 키 하나가 남고 진 쪽은 그것을 다시 읽는다(서명 키와 같다)."""
        found = cls._read(store, namespace)
        if found is not None:
            return found
        fresh = cls.generate()
        created = store.create(
            SANDBOX_KEY_SECRET,
            {KEY_FIELD: base64.b64encode(fresh._key).decode("ascii")},
            namespace=namespace,
            labels={"daegam.devops.ai.kr/resource": "sandbox-key"},
        )
        if created:
            return fresh
        stored = cls._read(store, namespace)
        if stored is None:
            raise RuntimeError(
                f"secret {SANDBOX_KEY_SECRET} exists but holds no usable {KEY_FIELD}"
            )
        return stored

    @classmethod
    def _read(cls, store: SecretStore, namespace: str) -> SandboxKey | None:
        value = (store.get(SANDBOX_KEY_SECRET, namespace=namespace) or {}).get(KEY_FIELD)
        if not value:
            return None
        try:
            return cls(base64.b64decode(value, validate=True))
        except ValueError as e:
            # 값은 싣지 않는다
            raise RuntimeError(f"secret {SANDBOX_KEY_SECRET} holds a malformed {KEY_FIELD}") from e

    def token(self, sandbox_id: str) -> str:
        """샌드박스 하나의 토큰. 같은 키와 id 면 어느 레플리카에서나 같다."""
        mac = hmac.new(self._key, TOKEN_CONTEXT + sandbox_id.encode("utf-8"), hashlib.sha256)
        return base64.urlsafe_b64encode(mac.digest()).rstrip(b"=").decode("ascii")


def token_sha256(token: str) -> str:
    """파드 env `SANDBOX_TOKEN_SHA256` 의 값. 서버가 Bearer 를 맞대고 증명의 키로 쓴다."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()
