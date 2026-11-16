"""임베딩 클라이언트 인자 표 (D32).

`agent-runtime/runtime/knowledge.py` 의 `build_embeddings` 는 플랫폼 인덱서의
`services/knowledge/embeddings.build_embeddings` 와 같은 `OpenAIEmbeddings` 인자를 만든다. 인덱싱과
검색이 다른 인자로 임베딩하면 벡터가 어긋나 검색이 조용히 빗나간다. 런타임 패키지는 여기서 import 할
수 없으므로 파일에서 상수 넷과 그 함수만 뽑아 실행한다(test_assistant_model.py 와 같은 방식).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

from daegam.services.knowledge import embeddings
from tests.helpers.source import AGENT_RUNTIME, exec_excerpt

RUNTIME_KNOWLEDGE = AGENT_RUNTIME / "knowledge.py"
CONSTANTS = ("BATCH_SIZE", "TIMEOUT", "MAX_RETRIES", "DIMENSIONS_MODEL_PREFIX")
ENV_KEY = "DAEGAM_KNOWLEDGE_0_API_KEY"


class Recorder:
    """OpenAIEmbeddings 자리에 서서 생성자 kwargs 만 기억한다."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class ConfigError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")


@pytest.fixture(scope="module")
def runtime() -> dict[str, Any]:
    """상수 넷과 build_embeddings 다."""
    scope = {
        "Any": Any,
        "Embeddings": object,
        "os": os,
        "ConfigError": ConfigError,
        "OpenAIEmbeddings": Recorder,
        "StubEmbeddings": object,
        # 상관 헤더 클라이언트(P37 2절)는 두 사본 모두 인자 표 밖이라 비교에서 뺀다
        "llm_http": SimpleNamespace(hooked_async_client=lambda **_: None),
    }
    ns = exec_excerpt(
        RUNTIME_KNOWLEDGE,
        scope,
        functions=("build_embeddings",),
        constants=CONSTANTS,
    )
    constants = {name: ns[name] for name in CONSTANTS}
    return {"constants": constants, "build_embeddings": ns["build_embeddings"]}


def test_constants_match_runtime(runtime):
    ours = {name: getattr(embeddings, name) for name in CONSTANTS}
    assert ours == runtime["constants"]


@pytest.mark.parametrize("model", ["text-embedding-3-small", "nomic-embed-text"])
@pytest.mark.parametrize("base_url", [None, "https://gw.example.com/v1"])
def test_openai_kwargs_match_runtime(runtime, monkeypatch, model, base_url):
    monkeypatch.setattr(embeddings, "OpenAIEmbeddings", Recorder)
    monkeypatch.setenv(ENV_KEY, "sk-e")
    item = {
        "ref": "kb-faq",
        "embedding": {
            "provider": {"name": "p", "kind": "openai", "env_key": ENV_KEY, "base_url": base_url},
            "model": model,
        },
        "store": {"dimensions": 256},
    }
    theirs = runtime["build_embeddings"](item).kwargs
    ours = embeddings.build_embeddings(
        "openai", api_key="sk-e", model=model, dimensions=256, base_url=base_url
    ).kwargs
    for kwargs in (ours, theirs):
        kwargs.pop("http_async_client")
    assert ours == theirs
    # dimensions 는 text-embedding-3* 에만 간다. 그 밖의 모델은 인자를 모른다
    assert ("dimensions" in ours) == model.startswith("text-embedding-3")
