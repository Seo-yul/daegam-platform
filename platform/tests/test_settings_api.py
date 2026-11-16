"""Platform Settings API 와 LLM provider 연결 확인(D25 F1)."""

from __future__ import annotations

import httpx2
import pytest

from daegam.store.base import utcnow
from daegam.store.settings import MongoPlatformSettingsStore
from tests.helpers.aio import run_store
from tests.helpers.consts import LLM_KEY_SECRET, PLATFORM_NS
from tests.helpers.http import api_client, detail_code, make_app
from tests.helpers.llm_gateway import IMAGES

CHECK = "/settings/llm-providers/{}/check"


def test_patch_validation(client):
    assert client.patch("/settings", json={}).json()["detail"]["code"] == "empty_patch"
    assert client.patch("/settings", json={"bogus": 1}).status_code == 422
    r = client.patch("/settings", json={"namespace_template": "static"})
    assert detail_code(r) == (422, "invalid_value")
    r = client.patch("/settings", json={"default_runtime_image": ""})
    assert r.status_code == 422
    r = client.patch("/settings", json={"default_cluster_id": "nope"})
    assert detail_code(r) == (422, "unknown_cluster")


# -- LLM provider 연결 확인 -----------------------------------------------------------


def _mock_llm_http(status: dict[str, int], seen: list[httpx2.Request]) -> httpx2.AsyncClient:
    """provider 의 models.list 를 흉내 낸다. status 0 은 연결 실패, 4xx·5xx 는 마스킹된 키가 든
    OpenAI 식 오류 본문이다(응답에 새어 나오지 않아야 한다)."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if status["code"] == 0:
            raise httpx2.ConnectError("refused")
        if status["code"] == 200:
            return httpx2.Response(200, json={"object": "list", "data": []})
        return httpx2.Response(
            status["code"],
            json={"error": {"message": "Incorrect API key provided: sk-test***", "type": "x"}},
        )

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def test_check_llm_provider_codes(client, fake_secrets, mongo):
    status = {"code": 200}
    seen: list[httpx2.Request] = []
    client.app.state.llm_http_client = _mock_llm_http(status, seen)
    assert client.post(CHECK.format("nope")).status_code == 404
    providers = [
        {"name": "nokey", "kind": "openai"},
        {"name": "ant", "kind": "anthropic", "api_key_secret": "ant-keys"},
        {
            "name": "gw",
            "kind": "openai",
            "api_key_secret": LLM_KEY_SECRET,
            "base_url": "http://gw/v1",
        },
    ]
    assert client.patch("/settings", json={"llm_providers": providers}).status_code == 200

    def check(name: str) -> str:
        r = client.post(CHECK.format(name))
        assert r.status_code == 200, r.text
        assert set(r.json()) == {"ok", "code"}
        assert r.json()["ok"] is (r.json()["code"] == "ok")
        return r.json()["code"]

    # 키 경로 문제는 HTTP 를 부르지 않는다
    assert check("nokey") == "no_api_key"
    assert check("ant") == "no_secret"
    fake_secrets.put("ant-keys", {"x": "1", "y": "2"}, namespace=PLATFORM_NS)
    assert check("ant") == "no_api_key"
    assert seen == []
    # 두 kind 모두 models.list 한 번. Secret 의 키가 요청에 실린다
    fake_secrets.put("ant-keys", {"api_key": "sk-ant"}, namespace=PLATFORM_NS)
    assert check("gw") == "ok"
    assert str(seen[-1].url) == "http://gw/v1/models"
    assert seen[-1].headers["authorization"] == "Bearer sk-test-key"
    assert check("ant") == "ok"
    assert seen[-1].url.path == "/v1/models"
    assert seen[-1].headers["authorization"] == "Bearer sk-ant"
    # provider 오류는 코드로만 나간다. 401 본문의 마스킹된 키는 응답에 없다
    status["code"] = 401
    r = client.post(CHECK.format("gw"))
    assert r.json() == {"ok": False, "code": "auth"}
    assert "sk-test" not in r.text and "Incorrect" not in r.text
    status["code"] = 500
    assert check("ant") == "provider_error"
    status["code"] = 0
    assert check("gw") == "connection"
    assert len(seen) == 5
    # 감사: 핸들러 이름이 action 이고 경로에는 provider 이름만 남는다
    rows = list(mongo["audit_events"].find({"action": "check_llm_provider"}).sort("ts", 1))
    assert len(rows) == 8 and rows[0]["status"] == 200
    assert rows[-1]["path_params"] == {"name": "gw"} and rows[-1]["method"] == "POST"
    assert not any("sk-" in str(row) for row in rows)


KEY = "/settings/llm-providers/{}/api-key"


def test_put_api_key_creates_a_managed_secret_and_points_the_provider_at_it(
    client, fake_secrets, mongo
):
    """화면에서 받은 키 (D56). 플랫폼 네임스페이스의 관리 Secret 에 넣고 api_key_secret 을 맞춘다.
    키는 응답·Mongo·감사 어디에도 남지 않는다."""
    providers = [
        {"name": "openai", "kind": "openai"},
        {"name": "Ops_GW", "kind": "openai", "api_key_secret": LLM_KEY_SECRET},
    ]
    assert client.patch("/settings", json={"llm_providers": providers}).status_code == 200
    assert client.put(KEY.format("nope"), json={"api_key": "sk-x"}).status_code == 404

    r = client.put(KEY.format("openai"), json={"api_key": "  sk-first  "})
    assert r.status_code == 200, r.text
    assert r.json() == {"name": "openai", "api_key_secret": "daegam-llm-key-openai"}
    assert "sk-first" not in r.text
    assert fake_secrets.get("daegam-llm-key-openai", namespace=PLATFORM_NS) == {
        "api_key": "sk-first"
    }
    got = {p["name"]: p["api_key_secret"] for p in client.get("/settings").json()["llm_providers"]}
    assert got == {"openai": "daegam-llm-key-openai", "Ops_GW": LLM_KEY_SECRET}
    # 다시 넣으면 키만 바뀐다
    assert client.put(KEY.format("openai"), json={"api_key": "sk-second"}).status_code == 200
    assert fake_secrets.get("daegam-llm-key-openai", namespace=PLATFORM_NS) == {
        "api_key": "sk-second"
    }
    # 운영자가 만든 Secret 을 쓰던 provider 는 관리 Secret 으로 옮긴다. 원래 Secret 은 그대로다
    r = client.put(KEY.format("Ops_GW"), json={"api_key": "sk-gw"})
    assert r.json()["api_key_secret"].startswith("daegam-llm-key-ops-gw-")
    assert fake_secrets.get(LLM_KEY_SECRET, namespace=PLATFORM_NS) == {"api_key": "sk-test-key"}
    # stub 은 키가 없는 실증용이다
    stub = [*providers, {"name": "s", "kind": "stub"}]
    stub[0] = {**stub[0], "api_key_secret": "daegam-llm-key-openai"}
    stub[1] = {**stub[1], "api_key_secret": r.json()["api_key_secret"]}
    assert client.patch("/settings", json={"llm_providers": stub}).status_code == 200
    r = client.put(KEY.format("s"), json={"api_key": "sk-s"})
    assert detail_code(r) == (422, "stub_has_no_key")
    # 한 줄이 아닌 키는 받지 않는다
    r = client.put(KEY.format("openai"), json={"api_key": "sk a"})
    assert detail_code(r) == (422, "invalid_api_key")
    assert client.put(KEY.format("openai"), json={"api_key": ""}).status_code == 422
    # 키 값은 Mongo(설정 문서·감사) 어디에도 없다
    assert "sk-first" not in str(mongo["settings"].find_one({"_id": "platform"}))
    rows = list(mongo["audit_events"].find({"action": "put_llm_provider_key"}))
    assert len(rows) >= 4 and not any("sk-" in str(row) for row in rows)


def test_put_api_key_needs_a_secret_store(settings, mongo, fake_clusters):
    app = make_app(settings, fake_clusters, None)
    with api_client(app) as c:
        body = {"llm_providers": [{"name": "o", "kind": "openai"}]}
        assert c.patch("/settings", json=body).status_code == 200
        # 키를 둘 곳(home 클러스터)이 없으면 422
        r = c.put(KEY.format("o"), json={"api_key": "sk-x"})
        assert detail_code(r) == (422, "no_secret_store")


def test_check_llm_provider_without_secret_store(settings, mongo, fake_clusters):
    """home 클러스터가 없어 Secret 저장소가 없으면 job 생성과 같은 422 no_secret_store."""
    app = make_app(settings, fake_clusters, None)
    with api_client(app) as c:
        body = {"llm_providers": [{"name": "o", "kind": "openai", "api_key_secret": "k"}]}
        assert c.patch("/settings", json=body).status_code == 200
        r = c.post(CHECK.format("o"))
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "no_secret_store"


def test_the_connection_check_goes_direct_with_the_gateway_on(
    settings, mongo, fake_clusters, fake_secrets
):
    """게이트웨이를 켜고 그 provider 가 반영돼 있어도 연결 확인은 provider 에 바로 플랫폼 키로
    간다 (P38 6절). 키를 저장한 바로 뒤에 부르므로 게이트웨이가 키를 반영하기 전이라도 맞는 키를
    맞다고 답해야 한다."""
    mongo["settings"].insert_one(
        {"_id": "llm_gateway", "ready_at": utcnow(), "providers_ready": ["gw"]}
    )
    app = make_app(
        settings,
        fake_clusters,
        fake_secrets,
        llm_gateway="agentgateway",
        llm_gateway_images=IMAGES,
    )
    seen: list[httpx2.Request] = []
    with api_client(app) as c:
        c.app.state.llm_http_client = _mock_llm_http({"code": 200}, seen)
        body = {
            "llm_providers": [
                {
                    "name": "gw",
                    "kind": "openai",
                    "api_key_secret": LLM_KEY_SECRET,
                    "base_url": "https://gw.example/v1",
                }
            ]
        }
        assert c.patch("/settings", json=body).status_code == 200
        assert c.post(CHECK.format("gw")).json() == {"ok": True, "code": "ok"}
    assert str(seen[0].url) == "https://gw.example/v1/models"
    assert seen[0].headers["authorization"] == "Bearer sk-test-key"


# -- 비서 (D31) -------------------------------------------------------------------------


def test_llm_provider_embedding_model(client):
    """임베딩 provider 는 LLM provider 행이다 (D32). kind 별 기본 모델·차원 표, anthropic 은
    거부."""
    from daegam.store.settings import (
        DEFAULT_EMBEDDING_MODELS,
        EMBEDDING_DIMENSIONS,
        LLMProvider,
        known_dimensions,
    )

    oai = LLMProvider(name="o", kind="openai")
    assert oai.embedding_model is None and oai.supports_embedding
    assert oai.effective_embedding_model == "text-embedding-3-small"
    big = LLMProvider(name="o", kind="openai", embedding_model="text-embedding-3-large")
    assert big.effective_embedding_model == "text-embedding-3-large"
    stub = LLMProvider(name="s", kind="stub")
    assert stub.supports_embedding and stub.effective_embedding_model == "stub"
    ant = LLMProvider(name="a", kind="anthropic")
    assert not ant.supports_embedding and ant.effective_embedding_model is None
    with pytest.raises(ValueError, match="embedding_model is not supported"):
        LLMProvider(name="a", kind="anthropic", embedding_model="x")
    assert DEFAULT_EMBEDDING_MODELS == {"openai": "text-embedding-3-small", "stub": "stub"}
    assert EMBEDDING_DIMENSIONS == {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
        "stub": 64,
    }
    assert known_dimensions("text-embedding-3-large") == 3072
    assert known_dimensions("gateway-custom") is None

    # API: 저장·조회되고 anthropic 에 적으면 422
    providers = [
        {"name": "oai", "kind": "openai", "api_key_secret": LLM_KEY_SECRET},
        {"name": "big", "kind": "openai", "embedding_model": "text-embedding-3-large"},
        {"name": "st", "kind": "stub"},
    ]
    r = client.patch("/settings", json={"llm_providers": providers})
    assert r.status_code == 200, r.text
    got = {p["name"]: p["embedding_model"] for p in r.json()["llm_providers"]}
    assert got == {"oai": None, "big": "text-embedding-3-large", "st": None}
    r = client.patch(
        "/settings",
        json={"llm_providers": [{"name": "a", "kind": "anthropic", "embedding_model": "x"}]},
    )
    assert r.status_code == 422, r.text
    assert "embedding_model" in r.text
    assert client.get("/settings").json()["llm_providers"][1]["embedding_model"] == (
        "text-embedding-3-large"
    )


def test_llm_providers_from_get_patch_back_unchanged(client):
    """GET /settings 의 llm_providers 를 손대지 않고 PATCH 로 되돌려 보내면 200 이고 값이 그대로다.
    scripts/lib/e2e_json.sh 의 providers_without·providers_with 와 e2e 게이트가 이 계약에 기댄다.
    GET 에만 있는 필드가 생겨 PATCH 가 그것을 거절하면 게이트가 provider 를 넣고 지우다 깨진다."""
    providers = [
        {
            "name": "big",
            "kind": "openai",
            "api_key_secret": LLM_KEY_SECRET,
            "embedding_model": "text-embedding-3-large",
        },
        {"name": "st", "kind": "stub"},
    ]
    assert client.patch("/settings", json={"llm_providers": providers}).status_code == 200
    before = client.get("/settings").json()["llm_providers"]
    assert [(p["name"], p["embedding_model"]) for p in before] == [
        ("big", "text-embedding-3-large"),
        ("st", None),
    ]
    r = client.patch("/settings", json={"llm_providers": before})
    assert r.status_code == 200, r.text
    assert r.json()["llm_providers"] == before
    assert client.get("/settings").json()["llm_providers"] == before


def test_put_api_key_keeps_a_provider_added_while_the_secret_is_written(
    client, fake_secrets, mongo, monkeypatch
):
    """Secret 을 쓰는 사이 다른 요청이 provider 를 더해도 키 저장이 그것을 지우지 않는다. 그사이
    provider 가 지워졌으면 방금 쓴 Secret 을 지우고 404 다 (P68 2절)."""
    assert (
        client.patch(
            "/settings", json={"llm_providers": [{"name": "a", "kind": "openai"}]}
        ).status_code
        == 200
    )
    put = fake_secrets.put

    def put_while_added(name, data, **kw):
        put(name, data, **kw)
        mongo["settings"].update_one(
            {"_id": "platform"},
            {"$push": {"llm_providers": {"name": "c", "kind": "openai"}}},
        )

    monkeypatch.setattr(fake_secrets, "put", put_while_added)
    assert client.put(KEY.format("a"), json={"api_key": "sk-a"}).status_code == 200
    providers = client.get("/settings").json()["llm_providers"]
    got = {p["name"]: p.get("api_key_secret") for p in providers}
    assert got == {"a": "daegam-llm-key-a", "c": None}

    def put_while_removed(name, data, **kw):
        put(name, data, **kw)
        mongo["settings"].update_one(
            {"_id": "platform"}, {"$pull": {"llm_providers": {"name": "c"}}}
        )

    monkeypatch.setattr(fake_secrets, "put", put_while_removed)
    r = client.put(KEY.format("c"), json={"api_key": "sk-c"})
    assert detail_code(r) == (404, "not_found")
    assert fake_secrets.get("daegam-llm-key-c", namespace=PLATFORM_NS) is None


def test_a_provider_key_put_raises_seq_so_an_older_read_does_not_undo_it(settings):
    """키 저장은 `seq` 를 올린다. 그 전에 읽은 provider 목록 저장은 키를 지우지 못하고 진다
    (P68 B2)."""
    providers = [{"name": "a", "kind": "openai"}]

    async def go(ps):
        assert await ps.update({"llm_providers": providers}) is not None
        before = await ps.get()
        assert await ps.set_provider_key_secret("a", "daegam-llm-key-a") is not None
        assert await ps.update({"llm_providers": providers}, base=before) is None
        assert (await ps.get()).providers_by_name["a"].api_key_secret == "daegam-llm-key-a"

    run_store(settings, MongoPlatformSettingsStore, go)


def _read_then_other_save(read, other_save):
    """스토어의 get 자리에 넣는다. 읽은 뒤 다른 사람의 저장(other_save)이 지나가게 하고, 라우트에는
    그 전의 읽기를 돌려준다. other_save 는 get 과 같은 인자를 받는다."""

    async def get(*args):
        stale = await read(*args)
        assert await other_save(*args) is not None
        return stale

    return get


def test_settings_patches_answer_revision_conflict_when_another_save_passed(client, monkeypatch):
    """PATCH 가 읽은 뒤 다른 사람의 저장이 지나가면 409 `revision_conflict` 이고, 먼저 지나간 저장은
    그대로 남는다 (P68 B2). Platform·Tenant Settings 가 같다."""
    platform = client.app.state.platform_settings
    read_platform = platform.get

    async def other_platform_save():
        return await platform.update({"default_model": "other"}, base=await read_platform())

    monkeypatch.setattr(platform, "get", _read_then_other_save(read_platform, other_platform_save))
    r = client.patch(
        "/settings", json={"default_model": "mine", "namespace_template": "t-{tenant}"}
    )
    assert detail_code(r) == (409, "revision_conflict")
    monkeypatch.undo()
    body = client.get("/settings").json()
    assert (body["default_model"], body["namespace_template"]) == ("other", "daegam-{tenant}")

    tenants = client.app.state.tenant_settings
    read_tenant = tenants.get

    async def other_tenant_save(tenant):
        return await tenants.update(tenant, {"mcp_group": "g"}, base=await read_tenant(tenant))

    monkeypatch.setattr(tenants, "get", _read_then_other_save(read_tenant, other_tenant_save))
    r = client.patch("/tenants/acme/settings", json={"agent_client_ids": ["x"]})
    assert detail_code(r) == (409, "revision_conflict")
    monkeypatch.undo()
    body = client.get("/tenants/acme/settings").json()
    assert (body["mcp_group"], body["agent_client_ids"]) == ("g", [])


def test_a_key_secret_pointed_at_again_after_the_save_is_not_deleted(
    client, fake_secrets, monkeypatch
):
    """provider 를 지운 저장 뒤 다른 저장이 그 키 Secret 을 다시 가리키면, 지우기 직전의 다시 읽기가
    그것을 보고 Secret 을 남긴다 (P68 B2 검토)."""
    with_key = [{"name": "a", "kind": "openai"}]
    assert client.patch("/settings", json={"llm_providers": with_key}).status_code == 200
    assert client.put(KEY.format("a"), json={"api_key": "sk-a"}).status_code == 200
    with_key = client.get("/settings").json()["llm_providers"]
    platform = client.app.state.platform_settings
    read = platform.get
    reads = 0

    async def get_with_a_save_after_the_patch():
        # 첫 읽기는 PATCH 의 기준이고, 둘째는 지우기 직전의 다시 읽기다
        nonlocal reads
        reads += 1
        if reads == 2:
            assert await platform.update({"llm_providers": with_key}, base=await read())
        return await read()

    monkeypatch.setattr(platform, "get", get_with_a_save_after_the_patch)
    assert client.patch("/settings", json={"llm_providers": []}).status_code == 200
    monkeypatch.undo()
    assert fake_secrets.get("daegam-llm-key-a", namespace=PLATFORM_NS) == {"api_key": "sk-a"}
    assert client.patch("/settings", json={"llm_providers": []}).status_code == 200
    assert fake_secrets.get("daegam-llm-key-a", namespace=PLATFORM_NS) is None
