"""P22 F2: 합친 Agent 의 렌더 (D26, D41, D42, D46, D64).

fake 클러스터에 배포한 support-agent 템플릿의 Deployment·Service·NetworkPolicy·Secret 스냅샷, 템플릿
함수에 바로 넘긴 렌더(local 모드의 gateway_token, 기록 보존 기간 env, oidc 모드의 agent_oauth env,
Cluster 스케줄링·이미지 규칙, redis 와 비밀번호 없는 바인딩), Skill 이 렌더를 바꾸지 않는 것을 든다.

배포한 Agent 의 나머지는 주제마다 따로 본다. knowledge 바인딩은 test_agent_runtime_knowledge.py,
상태·재배포·삭제는 test_agent_runtime_lifecycle.py, Advanced 편집은
test_agent_runtime_advanced_edit.py, provider 키 env 이름은 test_agent_runtime_llm_env.py, 설정
지문은 test_agent_runtime_config_sha.py, 장기 기억은 test_agent_long_term.py, 관측 이벤트 수집은
test_audit_collector.py, reconcile 재처리는 test_reconcile.py 가 본다. 정의 쪽은 나눠서 본다. 그래프
검증은 test_agent_graph_rules.py, agent.json 컴파일은 test_agent_compile.py, 리비전과 정의 API 는
test_agents.py 가 보고, 런타임 프록시는 test_agent_runtime_proxy.py, 준비되지 않은 파드의
이유(T21)는 test_agent_runtime_unready.py, 잠금 표가 넓어지기 전에 저장된 overrides 는
test_stored_overrides.py 가 본다. 실제 클러스터는 쓰지 않는다 (e2e 는 F5).
"""

from __future__ import annotations

import json

import jwt
import pytest

from daegam.k8s.labels import keyed_hash
from daegam.k8s.merge import AGENT_LOCKED_PATHS, MergeError, check_overrides
from daegam.k8s.templates.agent import (
    AGENT_CONTAINER,
    FETCH_SCRIPT,
    INIT_CONTAINER,
    PRESTOP_SECONDS,
    agent_binding_env,
    llm_secret_name_for,
    render_agent_deployment,
    render_agent_token_secret,
)
from daegam.k8s.templates.common import init_resources
from daegam.store.data_services import DataService, secret_name_for
from daegam.store.settings import LLMProvider
from daegam.store.tenants import AgentOAuth, Binding
from tests.helpers.agent_runtime import (
    agent_deployment,
    deployed_agent,
    env_map,
    minimal_agent_doc,
)
from tests.helpers.agents import (
    anthropic_with_openai_fallback,
    deploy_spec,
    template_for,
)
from tests.helpers.auth import bearer
from tests.helpers.consts import AG, DEFAULT_NS, ISSUER
from tests.helpers.docs import effective_config

# -- 렌더 스냅샷 (support-agent 템플릿) ------------------------------------------------------


def test_deploy_renders_deployment_service_policy_and_secrets(
    client, fake_clusters, fake_secrets, mongo
):
    deployed = deployed_agent(
        client,
        fake_clusters,
        mongo,
        spec=anthropic_with_openai_fallback,
        openai_secrets=fake_secrets,
        runtime=False,
    )
    inst = deployed.doc
    assert inst["state"] == "deployed" and inst["last_error"] is None
    assert inst["status"]["phase"] == "Pending" and inst["status"]["ready_replicas"] == 0
    # 화면이 안내문을 만들 때 읽는 값이다 (D46)
    assert inst["status"]["transcript_retention_days"] == 30
    assert inst["llm_secrets"] == [
        "agent-support-llm-local-anthropic",
        "agent-support-llm-local-openai",
    ]
    fake = deployed.fake
    # 네임스페이스 → 토큰 Secret → Deployment → Service → 인스턴스 NetworkPolicy 순서
    applied = [(o["kind"], o["metadata"]["name"]) for o in fake.applied]
    order = [
        applied.index(("Namespace", DEFAULT_NS)),
        applied.index(("Secret", inst["k8s"]["token_secret"])),
        applied.index(("Deployment", "agent-support")),
        applied.index(("Service", "agent-support")),
        applied.index(("NetworkPolicy", "daegam-agent-support")),
    ]
    assert order == sorted(order)

    # 토큰 Secret: 평문은 여기에만, Mongo 에는 sha256 만
    token_secret = fake.get("v1", "Secret", inst["k8s"]["token_secret"], DEFAULT_NS)
    token = token_secret["stringData"]["token"]
    assert token and token_secret["metadata"]["labels"]["daegam.devops.ai.kr/resource"] == (
        "agent-instance-token"
    )
    stored = mongo["agents"].find_one({"_id": inst["id"]})
    assert token not in json.dumps(stored, default=str)
    # 서비스 토큰 (D41): 같은 Secret 의 mcp_token 이 MCP Server 를 부를 토큰이고 파드 템플릿
    # 어노테이션이 그 판을 담는다. header 모드라 gateway_token 은 없다
    mcp_token = token_secret["stringData"]["mcp_token"]
    claims = jwt.decode(mcp_token, options={"verify_signature": False})
    assert claims["sub"] == "svc:agent:agent-support" and claims["aud"] == "daegam-internal:default"
    assert claims["tenant"] == "default" and claims["groups"] == ["daegam-default"]
    assert "gateway_token" not in token_secret["stringData"]
    assert mcp_token not in json.dumps(stored, default=str)
    dep0 = agent_deployment(fake)
    assert dep0["spec"]["template"]["metadata"]["annotations"] == {
        "daegam.devops.ai.kr/svc-token-rev": stored["svc_token_rev"],
        # 설정 지문 (P35 13절). 남긴 키 없는 digest 를 인스턴스 토큰으로 건 판이다
        "daegam.devops.ai.kr/config-sha": keyed_hash(token, stored["config_digest"], 16),
    }
    assert stored["svc_token_rev"] == claims["jti"][:8]
    r = client.get(f"{AG}/{inst['id']}/agent.json", headers=bearer(token))
    assert r.status_code == 200 and r.json()["main"]["fallback"]["provider"]["kind"] == "openai"
    # provider 키 복사본: 플랫폼 네임스페이스 Secret 의 api_key 가 대상 네임스페이스로 왔다
    anth = fake.get("v1", "Secret", "agent-support-llm-local-anthropic", DEFAULT_NS)
    assert anth["stringData"] == {"api_key": "sk-test-key"}
    assert anth["metadata"]["labels"]["daegam.devops.ai.kr/id"] == inst["id"]
    assert anth["metadata"]["labels"]["daegam.devops.ai.kr/resource"] == "agent-instance-llm"
    assert fake.get("v1", "Secret", "agent-support-llm-local-openai", DEFAULT_NS)["stringData"] == {
        "api_key": "sk-openai"
    }

    dep = agent_deployment(fake)
    # 라벨 키와 resource 값은 그대로 두되 프로필 라벨의 값은 이제 합친 문서 id 다 (D42)
    labels = {
        "daegam.devops.ai.kr/tenant": "default",
        "daegam.devops.ai.kr/resource": "agent-instance",
        "daegam.devops.ai.kr/id": inst["id"],
        "app.kubernetes.io/managed-by": "daegam",
        "daegam.devops.ai.kr/agent-profile": inst["id"],
        "daegam.devops.ai.kr/profile-revision": "1",
    }
    assert dep["metadata"]["labels"] == labels
    assert dep["spec"]["replicas"] == 1
    assert dep["spec"]["selector"] == {
        "matchLabels": {
            "daegam.devops.ai.kr/resource": "agent-instance",
            "daegam.devops.ai.kr/id": inst["id"],
        }
    }
    assert dep["spec"]["template"]["metadata"]["labels"] == labels
    pod = dep["spec"]["template"]["spec"]
    assert pod["securityContext"] == {
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "fsGroup": 1000,
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert [v["name"] for v in pod["volumes"]] == ["config", "tmp"]
    (init,) = pod["initContainers"]
    # init 은 이 Agent 의 런타임 이미지로 돌며 이미지가 아는 기능 이름을 스스로 알린다 (D65).
    # 메인과 같은 pull 정책이라 둘이 같은 이미지를 받는다
    assert init["name"] == INIT_CONTAINER and init["image"] == "daegam-agent-runtime:0.1.0"
    assert init["imagePullPolicy"] == pod["containers"][0]["imagePullPolicy"]
    # 모든 컨테이너에 requests/limits 를 요구하는 클러스터를 위해 init 에도 늘 적는다 (D54)
    assert init["resources"] == init_resources()
    assert init["securityContext"]["runAsUser"] == 1000
    # 파드는 `/api/agents/{id}` 를 부른다. agent.json 은 주소 하나로 받고 기능의 init 단계는 설정
    # 경로의 뿌리 아래를 연다
    config_base = f"{ISSUER}/api/agents/{inst['id']}"
    assert init["env"] == [
        {
            "name": "DAEGAM_INSTANCE_TOKEN",
            "valueFrom": {"secretKeyRef": {"name": inst["k8s"]["token_secret"], "key": "token"}},
        },
        {"name": "DAEGAM_AGENT_CONFIG", "value": "/config/agent.json"},
        {"name": "DAEGAM_AGENT_CONFIG_URL", "value": f"{config_base}/agent.json"},
        {"name": "DAEGAM_CONFIG_BASE_URL", "value": config_base},
        {"name": "DAEGAM_AGENT_IMAGE", "value": "daegam-agent-runtime:0.1.0"},
        # 이 템플릿의 MCP Server 토큰 env 표 지문 (P50 9절). 소스가 없어도 표는 있다
        {"name": "DAEGAM_MCP_TOKEN_TABLE", "value": stored["mcp_token_tables"][0]["digest"]},
    ]
    assert init["command"] == ["python", "-c"] and init["args"] == [FETCH_SCRIPT]
    # 루트가 읽기 전용이라 initContainer 에도 쓰기 자리를 준다
    assert init["volumeMounts"] == [
        {"name": "config", "mountPath": "/config"},
        {"name": "tmp", "mountPath": "/tmp"},
    ]
    (agent,) = pod["containers"]
    assert agent["name"] == AGENT_CONTAINER and agent["image"] == "daegam-agent-runtime:0.1.0"
    assert agent["ports"] == [{"name": "http", "containerPort": 8080}]
    # 준비 검사만 질의를 단다. 모르는 기능을 받은 파드는 이 검사에서 503 이라 Ready 가 되지 않는다
    assert agent["readinessProbe"]["httpGet"] == {"path": "/healthz?probe=ready", "port": 8080}
    assert agent["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": 8080}
    # 내려가는 파드는 SIGTERM 전에 기다려 엔드포인트 제거가 퍼지는 동안에도 답한다
    assert agent["lifecycle"] == {"preStop": {"sleep": {"seconds": PRESTOP_SECONDS}}}
    assert agent["securityContext"]["runAsUser"] == 1000
    assert agent["volumeMounts"][0] == {"name": "config", "mountPath": "/config", "readOnly": True}
    ds_secret = secret_name_for(deployed.ds["id"])
    assert [e["name"] for e in agent["env"]] == [
        "DAEGAM_AGENT_CONFIG",
        "DAEGAM_TENANT",
        "DAEGAM_TRANSCRIPT_RETENTION_DAYS",
        "DAEGAM_INSTANCE_TOKEN",
        "DAEGAM_MCP_TOKEN",
        "DAEGAM_LLM_LOCAL_ANTHROPIC_API_KEY",
        "DAEGAM_LLM_LOCAL_OPENAI_API_KEY",
        "DAEGAM_DB_0_HOST",
        "DAEGAM_DB_0_PORT",
        "DAEGAM_DB_0_DATABASE",
        "DAEGAM_DB_0_USER",
        "DAEGAM_DB_0_PASSWORD",
        "DAEGAM_DB_0_URL",
        "DAEGAM_EVENTS_URL",
        "DAEGAM_POD_NAME",
        "DAEGAM_POD_UID",
        "DAEGAM_TURN_DRAIN_SECONDS",
        "TIKTOKEN_CACHE_DIR",
    ]
    env = env_map(agent)
    assert env["DAEGAM_AGENT_CONFIG"] == "/config/agent.json" and env["DAEGAM_TENANT"] == "default"
    assert env["DAEGAM_TRANSCRIPT_RETENTION_DAYS"] == "30"
    assert env["DAEGAM_INSTANCE_TOKEN"] == {
        "secretKeyRef": {"name": inst["k8s"]["token_secret"], "key": "token"}
    }
    # provider 키 env 는 provider 이름에서 나온다 (P35 12절). base URL 은 agent.json 에만 있다
    assert env["DAEGAM_LLM_LOCAL_ANTHROPIC_API_KEY"] == {
        "secretKeyRef": {"name": "agent-support-llm-local-anthropic", "key": "api_key"}
    }
    assert env["DAEGAM_LLM_LOCAL_OPENAI_API_KEY"] == {
        "secretKeyRef": {"name": "agent-support-llm-local-openai", "key": "api_key"}
    }
    assert "OPENAI_BASE_URL" not in env and "ANTHROPIC_BASE_URL" not in env
    assert env["DAEGAM_DB_0_HOST"] == "pg.svc" and env["DAEGAM_DB_0_PORT"] == "5432"
    assert env["DAEGAM_DB_0_DATABASE"] == "agent" and env["DAEGAM_DB_0_USER"] == "u"
    assert env["DAEGAM_DB_0_PASSWORD"] == {"secretKeyRef": {"name": ds_secret, "key": "password"}}
    assert env["DAEGAM_DB_0_URL"] == "postgresql://u:$(DAEGAM_DB_0_PASSWORD)@pg.svc:5432/agent"
    # 런타임 이벤트 수집 주소는 agent.json 과 같은 뿌리이고 파드 이름·uid 는 downward API 다(P37)
    assert env["DAEGAM_EVENTS_URL"].endswith("/api/telemetry/events")
    assert env["DAEGAM_POD_UID"] == {"fieldRef": {"fieldPath": "metadata.uid"}}
    assert env["DAEGAM_POD_NAME"] == {"fieldRef": {"fieldPath": "metadata.name"}}
    # 비밀 값은 매니페스트 어디에도 없다
    assert "sk-test-key" not in json.dumps(dep) and "p" != env.get("DAEGAM_DB_0_PASSWORD")

    svc = fake.get("v1", "Service", "agent-support", DEFAULT_NS)
    assert svc["metadata"]["labels"] == labels
    assert svc["spec"] == {
        "type": "ClusterIP",
        "selector": {
            "daegam.devops.ai.kr/resource": "agent-instance",
            "daegam.devops.ai.kr/id": inst["id"],
        },
        "ports": [{"name": "http", "port": 8080, "targetPort": 8080}],
    }
    pol = fake.get("networking.k8s.io/v1", "NetworkPolicy", "daegam-agent-support", DEFAULT_NS)
    assert pol["spec"]["podSelector"] == {
        "matchLabels": {
            "daegam.devops.ai.kr/resource": "agent-instance",
            "daegam.devops.ai.kr/id": inst["id"],
        }
    }
    assert pol["spec"]["policyTypes"] == ["Ingress"]
    assert pol["spec"]["ingress"][0]["from"] == [
        {"podSelector": {}},
        {"namespaceSelector": {"matchLabels": {"daegam.devops.ai.kr/tenant": "default"}}},
        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "daegam-system"}}},
    ]
    # stub provider 는 키가 없어 복사본도 env 도 표의 줄도 없다
    p2, inst2 = deploy_spec(client, template_for(deployed.gw, deployed.ds), "stub")
    assert inst2["llm_secrets"] == []
    dep2 = agent_deployment(fake, "agent-stub")
    env2 = env_map(dep2["spec"]["template"]["spec"]["containers"][0])
    assert not [name for name in env2 if name.startswith("DAEGAM_LLM_")]
    assert mongo["agents"].find_one({"_id": inst2["id"]})["llm_env"] == {}


def test_gateway_token_env_and_secret_keys_in_local_mode():
    """local 모드에서 Gateway 소스를 쓰면 토큰 Secret 에 gateway_token 이 더 있고 파드 env 로
    내려간다 (D41). 없으면 둘 다 없다."""
    inst = minimal_agent_doc()
    secret = render_agent_token_secret(inst, "inst-tok", mcp_token="m.c.p", gateway_token="g.w")
    assert secret["stringData"] == {
        "token": "inst-tok",
        "mcp_token": "m.c.p",
        "gateway_token": "g.w",
    }
    assert "gateway_token" not in render_agent_token_secret(inst, "t", mcp_token="m")["stringData"]
    dep = render_agent_deployment(
        inst,
        effective_config(),
        providers=[],
        data_services={},
        agent_oauth=None,
        gateway_token=True,
        client_id_claim="cid",
        groups_claim=None,
    )
    env = env_map(dep["spec"]["template"]["spec"]["containers"][0])
    assert env["DAEGAM_MCP_TOKEN"] == {
        "secretKeyRef": {"name": inst.k8s.token_secret, "key": "mcp_token"}
    }
    assert env["DAEGAM_GATEWAY_TOKEN"] == {
        "secretKeyRef": {"name": inst.k8s.token_secret, "key": "gateway_token"}
    }
    plain = render_agent_deployment(
        inst,
        effective_config(),
        providers=[],
        data_services={},
        agent_oauth=None,
        client_id_claim="cid",
        groups_claim=None,
    )
    assert "DAEGAM_GATEWAY_TOKEN" not in env_map(plain["spec"]["template"]["spec"]["containers"][0])


def test_transcript_retention_env_comes_from_platform_config():
    """닫힌 대화의 기록 보존 기간은 플랫폼 설정이 정해 파드 env 로 내려간다 (D46). 세션 TTL 은
    Chat 입구 노드가 정하는 다른 값이라 그래프 스키마와 agent.json 은 그대로다."""

    def days(**kw) -> str:
        dep = render_agent_deployment(
            minimal_agent_doc(),
            effective_config(),
            providers=[],
            data_services={},
            agent_oauth=None,
            **kw,
            client_id_claim="cid",
            groups_claim=None,
        )
        env = env_map(dep["spec"]["template"]["spec"]["containers"][0])
        return env["DAEGAM_TRANSCRIPT_RETENTION_DAYS"]

    assert days() == "30"
    assert days(transcript_retention_days=7) == "7"
    # 0 은 뜻이 있는 값이라 그대로 내려간다(세션이 닫힐 때 기록도 지운다)
    assert days(transcript_retention_days=0) == "0"
    # 음수는 런타임이 읽을 수 없는 값이라 0 으로 내린다
    assert days(transcript_retention_days=-3) == "0"


def test_render_oauth_env_scheduling_and_bindings():
    """oidc 모드의 agent_oauth env, Cluster 스케줄링·이미지 규칙, redis 바인딩과 비밀번호 없는
    바인딩의 URL."""
    oauth = AgentOAuth(
        client_id="daegam-agent", secret_name="agent-oauth", token_url="http://idp/t", scope="mcp"
    )
    redis = DataService(
        id="ds-r",
        tenant="acme",
        name="cache",
        cluster_id="c1",
        kind="redis",
        host="redis.svc",
        port=6379,
        db=2,
        secret_name="ds-ds-r",
        has_password=True,
    )
    pg = DataService(
        id="ds-p",
        tenant="acme",
        name="pg",
        cluster_id="c1",
        kind="postgres",
        host="pg.svc",
        port=5432,
        database="d",
        username="app",
        secret_name="ds-ds-p",
    )
    effective = effective_config(
        bindings=[
            Binding(data_service_id="ds-p", env_prefix="DAEGAM_DB_0"),
            Binding(data_service_id="ds-r", env_prefix="DAEGAM_DB_1"),
        ]
    )
    dep = render_agent_deployment(
        minimal_agent_doc(),
        effective,
        providers=[
            LLMProvider(name="a", kind="anthropic", api_key_secret="k", base_url="http://a"),
            LLMProvider(name="a2", kind="anthropic", api_key_secret="k2"),
            LLMProvider(name="s", kind="stub"),
        ],
        data_services={"ds-p": pg, "ds-r": redis},
        agent_oauth=oauth,
        client_id_claim="cid",
        groups_claim=None,
    )
    pod = dep["spec"]["template"]["spec"]
    assert dep["spec"]["replicas"] == 2
    assert pod["imagePullSecrets"] == [{"name": "regcred"}]
    assert pod["nodeSelector"] == {"pool": "agents"} and pod["priorityClassName"] == "low"
    assert pod["tolerations"] == [{"key": "agents", "operator": "Exists"}]
    agent = pod["containers"][0]
    assert agent["image"] == "registry.local/daegam-agent-runtime:0.2.0"
    assert agent["resources"] == {"limits": {"cpu": "1"}}
    env = env_map(agent)
    # 표(`llm_env`)에 없는 provider 도 이름이 정하는 env 를 갖고, base URL env 는 없다
    assert env["DAEGAM_LLM_A_API_KEY"] == {
        "secretKeyRef": {"name": "agent-bot-llm-a", "key": "api_key"}
    }
    assert env["DAEGAM_LLM_A2_API_KEY"] == {
        "secretKeyRef": {"name": "agent-bot-llm-a2", "key": "api_key"}
    }
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["DAEGAM_AGENT_CLIENT_ID"] == "daegam-agent"
    assert env["DAEGAM_AGENT_TOKEN_URL"] == "http://idp/t" and env["DAEGAM_AGENT_SCOPE"] == "mcp"
    assert env["DAEGAM_AGENT_CLIENT_SECRET"] == {
        "secretKeyRef": {"name": "agent-oauth", "key": "client_secret"}
    }
    assert env["DAEGAM_DB_0_URL"] == "postgresql://app@pg.svc:5432/d"
    assert "DAEGAM_DB_0_PASSWORD" not in env
    assert env["DAEGAM_DB_1_URL"] == "redis://:$(DAEGAM_DB_1_PASSWORD)@redis.svc:6379/2"
    assert env["DAEGAM_DB_1_PASSWORD"] == {"secretKeyRef": {"name": "ds-ds-r", "key": "password"}}
    names = [e["name"] for e in agent["env"]]
    assert names.index("DAEGAM_DB_1_PASSWORD") < names.index("DAEGAM_DB_1_URL")
    assert llm_secret_name_for("agent-bot", "My.Provider_1") == "agent-bot-llm-my-provider-1"
    assert [e["name"] for e in agent_binding_env("X", pg)] == [
        "X_HOST",
        "X_PORT",
        "X_DATABASE",
        "X_USER",
        "X_URL",
    ]
    # Advanced 잠금: 이미지·DAEGAM_*·provider 키 env·설정 볼륨은 못 바꾸고 리소스는 자유다
    with pytest.raises(MergeError) as e:
        check_overrides(
            {"spec": {"template": {"spec": {"containers": [{"name": "agent", "image": "x"}]}}}},
            locked=AGENT_LOCKED_PATHS,
            ignored=(),
        )
    assert e.value.code == "locked_field"
    with pytest.raises(MergeError):
        check_overrides(
            {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "agent",
                                    "env": [{"name": "ANTHROPIC_API_KEY", "value": "x"}],
                                }
                            ]
                        }
                    }
                }
            },
            locked=AGENT_LOCKED_PATHS,
            ignored=(),
        )
    check_overrides(
        {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "agent", "resources": {"limits": {"memory": "1Gi"}}}
                        ]
                    }
                }
            }
        },
        locked=AGENT_LOCKED_PATHS,
        ignored=(),
    )
