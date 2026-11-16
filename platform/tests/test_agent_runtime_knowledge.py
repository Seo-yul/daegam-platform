"""P15 F3: knowledge 노드가 붙은 Agent 의 배포 (D32, D42).

knowledge 노드의 Database 가 배포 클러스터와 다르면 422 cluster_mismatch 인 것, 바인딩이 memory 뒤에
붙고 임베딩 키 env 가 채팅 provider 의 복사본을 함께 쓰는 것, agent.json "2" 문서, Database 와
Knowledge Base 삭제 가드가 배포가 핀한 리비전을 보는 것을 든다. 그래프 검증·컴파일은
test_agent_graph_knowledge.py 가 본다.
"""

from __future__ import annotations

import json

from daegam.services.references import KNOWLEDGE_BASE_DELETE
from daegam.store.data_services import secret_name_for
from tests.helpers.agent_runtime import agent_deployment, env_map, token_of
from tests.helpers.agents import (
    agent_body,
    agent_env,
    agent_env_with_openai,
    create_agent,
    deploy_agent,
    deploy_spec,
    template_for,
    template_spec,
    with_knowledge,
)
from tests.helpers.auth import bearer
from tests.helpers.clusters import register_cluster
from tests.helpers.consts import AG, DEFAULT_NS
from tests.helpers.data_services import created_database
from tests.helpers.docs import knowledge_base_doc
from tests.helpers.http import detail_code


def test_deploy_rejects_knowledge_database_on_another_cluster(client, fake_clusters, mongo):
    """knowledge 노드의 Database 가 배포 클러스터와 다르면 422 cluster_mismatch 이고 표식은 그
    노드다. 저장은 이미 끝났으므로 문서는 draft 로 남는다."""
    cid, ds, gw = agent_env(client, mongo)
    far = register_cluster(client, "far")
    pg_far = created_database(client, "pg-far", host="pg.far", database="kb", cluster_id=far)
    kb = knowledge_base_doc("far-kb", data_service_id=pg_far["id"])
    mongo["knowledge_bases"].insert_one(kb.to_doc())
    spec = template_for(gw, ds)
    with_knowledge(spec, node_id="kb-far", kb_id=kb.id)
    p = create_agent(client, spec, "far").json()
    assert "id" in p, p
    r = deploy_agent(client, p["id"], cluster_id=cid)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "cluster_mismatch" and detail["node_id"] == "kb-far"
    assert client.get(f"{AG}/{p['id']}").json()["state"] == "draft"


def test_deploy_with_knowledge_binds_databases_and_renders_embedding_env(
    client, fake_clusters, fake_secrets, mongo
):
    """knowledge 노드 둘(다른 Database, openai 와 stub 임베딩): 바인딩은 memory 뒤에 DAEGAM_DB_1,
    임베딩 키 env 는 DAEGAM_KNOWLEDGE_<n>_API_KEY(stub 은 없음), 채팅과 같은 provider 의 Secret 은
    한 번 복사, agent.json 은 "2" 문서다. knowledge 바인딩도 Database 삭제를 막고, Knowledge Base
    삭제 가드 훅은 현재 리비전과 배포가 핀한 리비전을 따로 본다. memory 와 같은 Database 하나면
    바인딩도 하나다."""
    cid, ds, gw = agent_env_with_openai(client, mongo, fake_secrets)
    ds2 = created_database(
        client, "pg2", host="pg2.svc", database="kb", username="u2", password="second-secret"
    )
    faq = knowledge_base_doc(
        data_service_id=ds["id"],
        provider="local-openai",
        model="text-embedding-3-small",
        dimensions=1536,
    )
    docs = knowledge_base_doc("docs", data_service_id=ds2["id"])
    mongo["knowledge_bases"].insert_many([faq.to_doc(), docs.to_doc()])

    spec = template_spec(provider="local-openai", source_id=gw["id"], ds_id=ds["id"])
    with_knowledge(spec, node_id="kb-faq", kb_id=faq.id, tool_name="search_faq")
    with_knowledge(spec, node_id="kb-docs", kb_id=docs.id)
    p, inst = deploy_spec(client, spec)
    assert inst["bindings"] == [
        {"data_service_id": ds["id"], "env_prefix": "DAEGAM_DB_0"},
        {"data_service_id": ds2["id"], "env_prefix": "DAEGAM_DB_1"},
    ]
    # 문서가 없는 Knowledge Base 는 경고다 (fake 클러스터의 preflight 경고와 함께)
    assert sorted(w.split(":")[0] for w in inst["warnings"]) == [
        "knowledge_base_empty",
        "knowledge_base_empty",
        "network_policy_unenforced",
    ]
    # 채팅과 임베딩이 같은 provider 라 복사본은 하나다
    assert inst["llm_secrets"] == ["agent-support-llm-local-openai"]
    fake = fake_clusters.by_name["edge"]
    assert fake.get("v1", "Secret", "agent-support-llm-local-openai", DEFAULT_NS)["stringData"] == {
        "api_key": "sk-openai"
    }
    dep = agent_deployment(fake)
    agent = dep["spec"]["template"]["spec"]["containers"][0]
    assert [e["name"] for e in agent["env"]] == [
        "DAEGAM_AGENT_CONFIG",
        "DAEGAM_TENANT",
        "DAEGAM_TRANSCRIPT_RETENTION_DAYS",
        "DAEGAM_INSTANCE_TOKEN",
        "DAEGAM_MCP_TOKEN",
        "DAEGAM_LLM_LOCAL_OPENAI_API_KEY",
        "DAEGAM_KNOWLEDGE_0_API_KEY",
        "DAEGAM_KNOWLEDGE_0_BASE_URL",
        "DAEGAM_DB_0_HOST",
        "DAEGAM_DB_0_PORT",
        "DAEGAM_DB_0_DATABASE",
        "DAEGAM_DB_0_USER",
        "DAEGAM_DB_0_PASSWORD",
        "DAEGAM_DB_0_URL",
        "DAEGAM_DB_1_HOST",
        "DAEGAM_DB_1_PORT",
        "DAEGAM_DB_1_DATABASE",
        "DAEGAM_DB_1_USER",
        "DAEGAM_DB_1_PASSWORD",
        "DAEGAM_DB_1_URL",
        "DAEGAM_EVENTS_URL",
        "DAEGAM_POD_NAME",
        "DAEGAM_POD_UID",
        "DAEGAM_TURN_DRAIN_SECONDS",
        "TIKTOKEN_CACHE_DIR",
    ]
    env = env_map(agent)
    assert env["DAEGAM_KNOWLEDGE_0_API_KEY"] == {
        "secretKeyRef": {"name": "agent-support-llm-local-openai", "key": "api_key"}
    }
    assert env["DAEGAM_KNOWLEDGE_0_API_KEY"] == env["DAEGAM_LLM_LOCAL_OPENAI_API_KEY"]
    assert env["DAEGAM_KNOWLEDGE_0_BASE_URL"] == "https://gw.example.com/v1"
    assert env["DAEGAM_DB_1_HOST"] == "pg2.svc" and env["DAEGAM_DB_1_DATABASE"] == "kb"
    assert env["DAEGAM_DB_1_PASSWORD"] == {
        "secretKeyRef": {"name": secret_name_for(ds2["id"]), "key": "password"}
    }
    assert env["DAEGAM_DB_1_URL"] == "postgresql://u2:$(DAEGAM_DB_1_PASSWORD)@pg2.svc:5432/kb"
    assert "sk-openai" not in json.dumps(dep) and "second-secret" not in json.dumps(dep)

    token = token_of(fake, inst)
    r = client.get(f"{AG}/{inst['id']}/agent.json", headers=bearer(token))
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["schema_version"] == "2" and doc["main"]["knowledge"] == ["kb-faq", "kb-docs"]
    assert [
        (k["ref"], k["store"]["dsn_env"], k["embedding"]["provider"]["env_key"])
        for k in doc["knowledge"]
    ] == [
        ("kb-faq", "DAEGAM_DB_0_URL", "DAEGAM_KNOWLEDGE_0_API_KEY"),
        ("kb-docs", "DAEGAM_DB_1_URL", None),
    ]
    assert doc["knowledge"][0]["store"]["table"] == faq.table
    assert doc["knowledge"][0]["tool"]["name"] == "search_faq"
    assert doc["memory"]["checkpointer"]["dsn_env"] == "DAEGAM_DB_0_URL"

    # Database 삭제 가드: knowledge 바인딩도 배포된 것의 바인딩이다
    r = client.delete(f"/data-services/{ds2['id']}")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "in_use"
    assert r.json()["detail"]["deployed_agent_ids"] == [inst["id"]]

    # Knowledge Base 삭제 가드의 두 칸(정의 쪽·배포 쪽). 한 번 센 참조에서 둘 다 고른다
    refs = client.app.state.references

    async def count(kb_id: str, side: int) -> list[str]:
        found = await refs.used_by("knowledge-base", kb_id, tenant=None)
        return KNOWLEDGE_BASE_DELETE[side].ids(found)

    async def defined(kb_id: str) -> list[str]:
        return await count(kb_id, 0)

    async def deployed(kb_id: str) -> list[str]:
        return await count(kb_id, 1)

    assert client.portal.call(defined, faq.id) == [inst["id"]]
    assert client.portal.call(deployed, faq.id) == [inst["id"]]
    assert client.portal.call(deployed, docs.id) == [inst["id"]]
    assert client.portal.call(defined, "nope") == []
    assert client.portal.call(deployed, "nope") == []
    # 정의에서 knowledge 를 빼면 현재 리비전은 참조가 없지만 배포는 옛 리비전을 핀하고 있다
    plain = template_spec(provider="local-openai", source_id=gw["id"], ds_id=ds["id"])
    r = client.put(f"{AG}/{inst['id']}", json=agent_body(plain))
    assert r.status_code == 200 and r.json()["revision"] == 2, r.text
    assert client.portal.call(defined, faq.id) == []
    assert client.portal.call(deployed, faq.id) == [inst["id"]]
    # redeploy 로 핀이 옮겨 가면 바인딩·env·문서가 knowledge 없는 것으로 돌아간다
    r = client.post(f"{AG}/{inst['id']}/redeploy")
    assert r.status_code == 200, r.text
    assert r.json()["deployed_revision"] == 2
    assert r.json()["bindings"] == [{"data_service_id": ds["id"], "env_prefix": "DAEGAM_DB_0"}]
    assert client.portal.call(deployed, faq.id) == []
    dep = agent_deployment(fake)
    names = [e["name"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]]
    assert not any(n.startswith(("DAEGAM_KNOWLEDGE_", "DAEGAM_DB_1")) for n in names)
    r = client.get(f"{AG}/{inst['id']}/agent.json", headers=bearer(token))
    assert r.status_code == 200 and r.json()["schema_version"] == "1"
    assert "knowledge" not in r.json()
    # Database 삭제는 그것을 쓰는 Knowledge Base 가 남아 있는 한 409 in_use 다. 지우면 200
    r = client.delete(f"/data-services/{ds2['id']}")
    assert detail_code(r) == (409, "in_use"), r.text
    assert r.json()["detail"]["knowledge_base_ids"] == [docs.id]
    mongo["knowledge_bases"].delete_one({"_id": docs.id})
    assert client.delete(f"/data-services/{ds2['id']}").status_code == 200

    # memory 와 같은 Database 의 stub Knowledge Base 하나: 바인딩 하나, 임베딩 env·Secret 없음
    same = knowledge_base_doc("same", data_service_id=ds["id"])
    mongo["knowledge_bases"].insert_one(same.to_doc())
    spec2 = template_for(gw, ds)
    with_knowledge(spec2, node_id="kb-same", kb_id=same.id)
    p2, inst2 = deploy_spec(client, spec2, "same")
    assert inst2["bindings"] == [{"data_service_id": ds["id"], "env_prefix": "DAEGAM_DB_0"}]
    assert inst2["llm_secrets"] == []
    dep2 = agent_deployment(fake, "agent-same")
    names2 = [e["name"] for e in dep2["spec"]["template"]["spec"]["containers"][0]["env"]]
    assert not any(n.startswith(("DAEGAM_KNOWLEDGE_", "DAEGAM_DB_1")) for n in names2)
    assert "DAEGAM_DB_0_HOST" in names2
    token2 = token_of(fake, inst2)
    r = client.get(f"{AG}/{inst2['id']}/agent.json", headers=bearer(token2))
    assert r.status_code == 200, r.text
    doc2 = r.json()
    assert doc2["schema_version"] == "2" and doc2["stub"] is True
    assert doc2["knowledge"][0]["store"]["dsn_env"] == "DAEGAM_DB_0_URL"
    assert doc2["knowledge"][0]["embedding"]["provider"] == {
        "name": "local-stub",
        "kind": "stub",
        "env_key": None,
        "base_url": None,
    }
    assert doc2["knowledge"][0]["tool"]["description"] == (
        "Search the knowledge base 'same' for passages relevant to a query."
    )
    assert client.portal.call(deployed, same.id) == [inst2["id"]]
