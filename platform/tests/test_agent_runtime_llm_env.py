"""P35 12절: Agent 파드의 provider 키 env 이름.

env 이름이 provider 이름에서 나오는 규칙, 같은 kind 둘이 제 Secret 을 가리키는 제 env 를 갖는 렌더와
배포, provision 이 키를 복사한 뒤 렌더하고 표(`llm_env`)를 적용 직전에 남기는 순서를 든다.
"""

from __future__ import annotations

from typing import Any

from daegam.k8s.templates.agent import llm_env_for, provider_key_env, render_agent_deployment
from daegam.names import provider_slug
from daegam.services.llm_providers import managed_secret_name
from daegam.store.settings import LLMProvider
from tests.helpers.agent_runtime import (
    env_map,
    minimal_agent_doc,
)
from tests.helpers.docs import effective_config

MAIN_ENV = "DAEGAM_LLM_LOCAL_ANTHROPIC_API_KEY"


def _llm_env_names(container: dict[str, Any]) -> list[str]:
    return [e["name"] for e in container["env"] if e["name"].startswith("DAEGAM_LLM_")]


def _stored_llm_env(mongo, agent_id: str) -> dict[str, str]:
    return mongo["agents"].find_one({"_id": agent_id})["llm_env"]


def test_provider_key_env_comes_from_the_provider_name():
    """env 이름은 provider 이름에서 나온다. slug 는 관리 Secret 이름과 같은 규칙이라 낮추며 바뀐
    이름에는 원래 이름의 해시가 붙고, 서로 다른 provider 가 같은 env 를 갖지 않는다. 표에는 키가
    있는 provider 만 든다."""
    assert provider_key_env("local-anthropic") == MAIN_ENV
    assert provider_key_env("openai") == "DAEGAM_LLM_OPENAI_API_KEY"
    slug = provider_slug("Ops_GW.v2")
    assert slug.startswith("ops-gw-v2-") and len(slug) == len("ops-gw-v2-") + 6
    assert managed_secret_name("Ops_GW.v2") == f"daegam-llm-key-{slug}"
    assert provider_key_env("Ops_GW.v2") == f"DAEGAM_LLM_{slug.upper().replace('-', '_')}_API_KEY"
    assert len({provider_key_env(n) for n in ("ops-gw", "Ops-GW", "ops_gw", "ops.gw")}) == 4
    providers = [
        LLMProvider(name="a", kind="anthropic", api_key_secret="k"),
        LLMProvider(name="s", kind="stub"),
    ]
    assert llm_env_for(providers) == {"a": "DAEGAM_LLM_A_API_KEY"}


def test_render_with_the_table_names_each_provider_and_has_no_base_url_env():
    """표가 있는 문서(P35 뒤의 provision)는 같은 kind 둘이 제 Secret 을 가리키는 제 env 를 갖고
    `<KIND>_BASE_URL` 은 없다. 표에 없는 provider(핀을 옮긴 뒤 provision 전의 렌더)도 같은 규칙의
    이름이고 stub 은 없다."""
    providers = [
        LLMProvider(name="a", kind="anthropic", api_key_secret="k", base_url="http://a"),
        LLMProvider(name="a2", kind="anthropic", api_key_secret="k2"),
        LLMProvider(name="s", kind="stub"),
    ]
    inst = minimal_agent_doc(llm_env=llm_env_for(providers[:1]))
    dep = render_agent_deployment(
        inst,
        effective_config(),
        providers=providers,
        data_services={},
        agent_oauth=None,
        client_id_claim="cid",
        groups_claim=None,
    )
    agent = dep["spec"]["template"]["spec"]["containers"][0]
    env = env_map(agent)
    assert _llm_env_names(agent) == ["DAEGAM_LLM_A_API_KEY", "DAEGAM_LLM_A2_API_KEY"]
    assert env["DAEGAM_LLM_A_API_KEY"] == {
        "secretKeyRef": {"name": "agent-bot-llm-a", "key": "api_key"}
    }
    assert env["DAEGAM_LLM_A2_API_KEY"] == {
        "secretKeyRef": {"name": "agent-bot-llm-a2", "key": "api_key"}
    }
    assert not [name for name in env if name.endswith("_BASE_URL")]
    assert "ANTHROPIC_API_KEY" not in env
