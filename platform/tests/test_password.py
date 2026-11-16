"""비밀번호 해시와 규칙 (auth/password.py)."""

from __future__ import annotations

import time

import pytest

from daegam.auth.password import (
    DUMMY_HASH,
    MIN_LENGTH,
    PasswordPolicyError,
    check_policy,
    hash_password,
    verify_password,
)


def test_roundtrip_and_salt():
    h1 = hash_password("correct horse battery staple")
    h2 = hash_password("correct horse battery staple")
    assert h1 != h2, "salt 가 매번 달라야 한다"
    assert h1.startswith("scrypt$32768$8$1$")
    assert verify_password("correct horse battery staple", h1)
    assert verify_password("correct horse battery staple", h2)
    assert not verify_password("correct horse battery stapl", h1)


def test_dummy_hash_never_matches_but_costs_the_same():
    """없는 사용자에게도 같은 시간을 쓴다. 그래야 응답 시간으로 사용자 존재를 알 수 없다."""
    started = time.perf_counter()
    assert not verify_password("anything at all here", DUMMY_HASH)
    dummy = time.perf_counter() - started
    real = hash_password("anything at all here")
    started = time.perf_counter()
    assert verify_password("anything at all here", real)
    genuine = time.perf_counter() - started
    assert dummy > 0.005 and abs(dummy - genuine) < max(genuine, dummy)


def test_broken_hash_is_false_not_an_exception():
    assert not verify_password("x", "")
    assert not verify_password("x", "bcrypt$whatever")
    assert not verify_password("x", "scrypt$a$b$c$d$e")


def test_policy_is_length_only():
    check_policy("a" * MIN_LENGTH)
    with pytest.raises(PasswordPolicyError, match="at least"):
        check_policy("a" * (MIN_LENGTH - 1))
    with pytest.raises(PasswordPolicyError, match="at most"):
        check_policy("a" * 257)
    with pytest.raises(PasswordPolicyError, match="username"):
        check_policy("Administrator1", username="administrator1")
    # 복잡도 규칙은 없다. 긴 소문자 문장은 통과한다
    check_policy("purple monkey dishwasher")
