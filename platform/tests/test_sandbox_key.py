"""샌드박스 토큰의 키 (P40 Security). 키는 Secret 하나에 한 번 만들고, 토큰은 키와 샌드박스 id 로
정해진다. 키도 토큰도 repr 이나 오류에 실리지 않는다."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from daegam.auth.sandbox_key import (
    KEY_FIELD,
    SANDBOX_KEY_SECRET,
    SandboxKey,
    token_sha256,
)
from tests.helpers.fake_secrets import FakeSecretStore

NS = "daegam-system"


def test_the_key_is_created_once_and_read_back() -> None:
    store = FakeSecretStore()
    first = SandboxKey.load_or_create(store, namespace=NS)
    again = SandboxKey.load_or_create(store, namespace=NS)
    assert first.token("sbx-1") == again.token("sbx-1")
    stored = store.secrets[(NS, SANDBOX_KEY_SECRET)][KEY_FIELD]
    assert len(base64.b64decode(stored)) == 32
    assert store.labels[(NS, SANDBOX_KEY_SECRET)] == {"daegam.devops.ai.kr/resource": "sandbox-key"}


def test_a_replica_that_loses_the_race_reads_the_winner() -> None:
    """두 레플리카가 함께 뜨면 먼저 만든 쪽의 키가 남는다. 진 쪽은 그것을 다시 읽는다."""
    winner = SandboxKey.generate()

    class Racing(FakeSecretStore):
        def create(self, name, data, *, namespace, labels=None) -> bool:
            key = base64.b64encode(winner._key).decode()
            super().create(name, {KEY_FIELD: key}, namespace=namespace, labels=labels)
            return False

    loaded = SandboxKey.load_or_create(Racing(), namespace=NS)
    assert loaded.token("sbx-1") == winner.token("sbx-1")


def test_the_token_is_hmac_of_the_versioned_id() -> None:
    key = SandboxKey(b"k" * 32)
    mac = hmac.new(b"k" * 32, b"sandbox:v1:sbx-1", hashlib.sha256).digest()
    assert key.token("sbx-1") == base64.urlsafe_b64encode(mac).rstrip(b"=").decode()
    assert key.token("sbx-1") != key.token("sbx-2")
    assert SandboxKey(b"j" * 32).token("sbx-1") != key.token("sbx-1")
    digest = token_sha256(key.token("sbx-1"))
    assert digest == hashlib.sha256(key.token("sbx-1").encode()).hexdigest() and len(digest) == 64


def test_the_key_stays_out_of_repr_and_errors() -> None:
    key = SandboxKey(b"s" * 32)
    assert "s" * 8 not in repr(key) and repr(key) == "SandboxKey(<hidden>)"
    with pytest.raises(ValueError, match="32 bytes"):
        SandboxKey(b"short")
    store = FakeSecretStore()
    store.put(SANDBOX_KEY_SECRET, {KEY_FIELD: "not base64!"}, namespace=NS)
    with pytest.raises(RuntimeError) as e:
        SandboxKey.load_or_create(store, namespace=NS)
    assert "not base64" not in str(e.value)
