"""스텁 임베딩 (D32). 플랫폼 `services/knowledge/stub_embedding.py` 와 agent-runtime
`runtime/stub_embedding.py` 는 바이트 동일해야 한다. 인덱싱한 쪽과 검색하는 쪽이 다른 값을 내면
e2e 가 성립하지 않기 때문이다. 그래서 이 파일은 어느 쪽 패키지도 import 하지 않는다.

알고리즘: 소문자화한 텍스트에서 `[a-z0-9가-힣]+` 토큰을 뽑아 토큰마다 sha256 의 앞 4 바이트로
차원을, 다음 바이트의 마지막 비트로 부호를 정해 더하고 L2 정규화한다. 빈 텍스트는 영벡터다.
"""

from __future__ import annotations

import hashlib
import math
import re

_TOKEN_RE = re.compile(r"[a-z0-9가-힣]+")


def hash_embedding(text: str, dim: int) -> list[float]:
    if dim < 1:
        raise ValueError("dim must be positive")
    vec = [0.0] * dim
    for token in _TOKEN_RE.findall(text.lower()):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        vec[index] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]
