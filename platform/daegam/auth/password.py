"""비밀번호 해시 (D40 local 모드).

stdlib `hashlib.scrypt` 다. 새 의존성 없이 메모리 비용이 있는 해시를 얻는다. 저장 형식은
`scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>` 라 매개변수를 나중에 올려도 옛 해시를 그대로 검증한다.

규칙은 길이 하나다. 12자 이상 256자 이하. 복잡도 규칙(대문자·숫자·기호)은 두지 않는다. 그런 규칙은
사람을 짧고 예측 가능한 변형으로 몰고, 길이가 그 무엇보다 강하다.

해시는 CPU 를 수십 ms 쓰므로 호출자는 `run_in_threadpool` 로 돈다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

MIN_LENGTH = 12
MAX_LENGTH = 256

_N = 2**15
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16
_ALG = "scrypt"
# scrypt 는 128·r·n 바이트를 쓴다. n=2^15, r=8 이면 32MiB 를 조금 넘어 OpenSSL 기본 상한(32MiB)에
# 걸리므로 상한을 명시한다. 저장된 해시의 n 이 이보다 크면 검증하지 않는다(조작된 해시로 메모리를
# 태우지 못하게)
_MAXMEM = 2**27
_MAX_N = 2**17

# 없는 사용자에게도 같은 시간을 쓰려고 두는 더미. 절대 맞지 않는 값이다
DUMMY_HASH = (
    "scrypt$32768$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)


class PasswordPolicyError(ValueError):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def check_policy(password: str, *, username: str | None = None) -> None:
    """길이와 "아이디와 같음" 만 본다. 아니면 PasswordPolicyError."""
    if len(password) < MIN_LENGTH:
        raise PasswordPolicyError(f"password must be at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        raise PasswordPolicyError(f"password must be at most {MAX_LENGTH} characters")
    if username and password.lower() == username.lower():
        raise PasswordPolicyError("password must not be the username")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM
    )
    return "$".join(
        [_ALG, str(_N), str(_R), str(_P), _b64(salt), _b64(digest)],
    )


def verify_password(password: str, stored: str) -> bool:
    """저장된 해시와 비교한다. 형식이 깨진 해시는 거짓이다(예외를 내지 않는다)."""
    try:
        alg, n, r, p, salt, digest = stored.split("$")
        if alg != _ALG:
            return False
        expected = base64.b64decode(digest)
        if int(n) > _MAX_N:
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()
