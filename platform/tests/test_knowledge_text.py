"""자르기·토큰·스텁 임베딩 (D32, F1). 형식 판별과 추출은 `test_documents.py` 다(D61)."""

from __future__ import annotations

import math

import pytest

from daegam.services.knowledge import text as T
from daegam.services.knowledge.stub_embedding import hash_embedding

# -- 자르기 ---------------------------------------------------------------------------


def test_chunk_empty_and_short():
    assert T.chunk("", 100, 10) == []
    assert T.chunk("  \n\n \t ", 100, 10) == []
    assert T.chunk("one paragraph", 100, 10) == ["one paragraph"]
    with pytest.raises(ValueError):
        T.chunk("x", 10, 10)
    with pytest.raises(ValueError):
        T.chunk("x", 0, 0)


def test_chunk_packs_paragraphs_and_prepends_overlap():
    text = "aaaa aaaa\n\nbbbb bbbb\n\n\ncccc cccc\r\n\r\ndddd"
    out = T.chunk(text, 12, 0)  # 9 + 2 + 4 = 15 > 12 라 문단마다 하나
    assert out == ["aaaa aaaa", "bbbb bbbb", "cccc cccc", "dddd"]
    out = T.chunk(text, 21, 0)  # 두 문단이 \n\n 로 붙는다 (9 + 2 + 9 = 20)
    assert out == ["aaaa aaaa\n\nbbbb bbbb", "cccc cccc\n\ndddd"]
    out = T.chunk(text, 21, 4)
    assert out[0] == "aaaa aaaa\n\nbbbb bbbb"
    assert out[1] == "bbbb" + "\n\n" + "cccc cccc\n\ndddd"
    assert all(c for c in out)
    # 결정적이다
    assert T.chunk(text, 21, 4) == out


def test_chunk_splits_long_paragraph_by_sentence_then_fixed_length():
    para = "First sentence here. Second one! Third? 한국어 문장입니다. 그래요. " + "x" * 50
    out = T.chunk(para, 30, 0)
    assert out == [
        "First sentence here.",  # 20 + 1 + 11 > 30 이라 여기서 끊긴다
        "Second one! Third? 한국어 문장입니다.",  # 문장 셋이 공백으로 붙어 29 자
        "그래요.",
        "x" * 30,  # 문장 하나가 size 를 넘으면 고정 길이
        "x" * 20,
    ]
    assert all(len(c) <= 30 for c in out)
    # 겹침은 앞 청크의 끝 overlap 자를 앞에 붙인다. 고정 길이 조각 사이는 공백으로 잇는다
    over = T.chunk(para, 30, 5)
    assert over[0] == out[0]
    assert over[1] == "here." + " " + out[1]
    assert over[2] == "장입니다." + " " + out[2]
    assert over[4] == "xxxxx" + " " + out[4]
    assert len(over) == len(out)


# -- 토큰 -----------------------------------------------------------------------------


def test_count_tokens_real_encoding(monkeypatch):
    """cl100k_base 로 센 값. 특수 토큰 문자열도 예외 없이 센다. 인코딩 객체는 처음 한 번만 만들어
    캐시한다."""
    loads: list[str] = []
    get_encoding = T.tiktoken.get_encoding

    def counted(name: str):
        loads.append(name)
        return get_encoding(name)

    monkeypatch.setattr(T, "_encoding", None)
    monkeypatch.setattr(T.tiktoken, "get_encoding", counted)
    n = T.count_tokens(["hello world", "안녕하세요"])
    assert 4 <= n <= 12
    assert T.count_tokens(["<|endoftext|>"]) >= 1
    assert T.count_tokens([]) == 0
    assert loads == ["cl100k_base"]


# -- 스텁 임베딩 ---------------------------------------------------------------------------


def test_hash_embedding_is_deterministic_and_normalized():
    a = hash_embedding("Hello, World! 안녕 hello", 64)
    assert a == hash_embedding("hello world 안녕 HELLO", 64)  # 소문자화, 구두점 무시
    assert len(a) == 64
    assert math.isclose(math.sqrt(sum(v * v for v in a)), 1.0, rel_tol=1e-9)
    assert hash_embedding("", 8) == [0.0] * 8
    assert hash_embedding("!!! ...", 8) == [0.0] * 8
    assert hash_embedding("cat", 64) != hash_embedding("dog", 64)
    # 같은 토큰이 두 번이면 같은 차원에 더해져도 정규화 뒤 방향은 같다
    assert all(
        math.isclose(x, y, abs_tol=1e-12)
        for x, y in zip(hash_embedding("cat cat", 16), hash_embedding("cat", 16), strict=True)
    )
    with pytest.raises(ValueError):
        hash_embedding("x", 0)

    # 코사인 유사도: 겹치는 토큰이 있으면 없는 쪽보다 크다
    def cos(u, v):
        return sum(x * y for x, y in zip(u, v, strict=True))

    q = hash_embedding("petstore inventory", 64)
    assert cos(q, hash_embedding("the inventory of the petstore", 64)) > cos(
        q, hash_embedding("unrelated words entirely", 64)
    )


def test_stub_embedding_module_has_no_daegam_imports():
    import daegam.services.knowledge.stub_embedding as m

    src = open(m.__file__, encoding="utf-8").read()
    assert "import daegam" not in src and "from daegam" not in src
