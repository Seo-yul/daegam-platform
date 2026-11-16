"""공용 픽스처와 세션 준비. 픽스처가 쓰는 도우미와 상수는 tests/helpers 에 있고 테스트 모듈도
거기서 가져온다."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import uuid
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo import MongoClient

from daegam import error_codes
from daegam.auth import password
from daegam.config import Settings
from daegam.main import create_app
from tests.helpers.auth import LOCAL_BOOT_PASSWORD, admin_headers, bearer, map_groups, user_token
from tests.helpers.consts import (
    B_VIEWER_MAPPINGS,
    GROUP_MAPPINGS,
    ISSUER,
    LLM_KEY_SECRET,
    OIDC_AUDIENCE,
    OIDC_ISSUER,
    PLATFORM_NS,
    PUBLIC_URL,
    REPO,
    TENANT_HEADER,
)
from tests.helpers.docs import TENANT, seed_gateway
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.fake_secrets import FakeSecretStore
from tests.helpers.http import api_client, make_app
from tests.helpers.oauth import GW_HOST, connect, person, register
from tests.helpers.oidc import Signer, create_mock_oidc, mock_oidc_config

# psycopg 목 `fake_pg` 는 플러그인(tests/helpers/fake_pg.py)으로 싣는다
pytest_plugins = ("tests.helpers.fake_pg", "tests.helpers.evaluation")

MONGO_URL = os.environ.get("DAEGAM_TEST_MONGO_URL", "mongodb://localhost:27017")
# offline_settings 의 주소. 닫힌 포트라 이 설정으로 DB 를 쓰는 시험은 로컬 게이트와 CI 에서 똑같이
# 서버를 찾는 시간(serverSelectionTimeoutMS)을 기다린 뒤 실패한다. 그 시간은 앱의 Mongo 클라이언트가
# 5초, tests/helpers/aio.py 의 클라이언트가 30초다
OFFLINE_MONGO_URL = "mongodb://127.0.0.1:1"


def pytest_configure(config):
    """DAEGAM_TEST_SKIP_MONGO 는 시험용 Mongo 없이 도는 로컬 게이트만 준다
    (Makefile PLATFORM_STORES). Mongo 주소가 있거나 CI 에서 켜지면 Mongo 시험이 통과처럼
    사라지므로 시작부터 멈춘다."""
    if os.environ.get("DAEGAM_TEST_SKIP_MONGO") == "1" and (
        os.environ.get("DAEGAM_TEST_MONGO_URL") or os.environ.get("CI")
    ):
        raise pytest.UsageError("DAEGAM_TEST_SKIP_MONGO is only for a local gate without MongoDB")


@pytest.fixture(autouse=True)
def _cheap_password_hash(request):
    """비밀번호 해시의 scrypt 비용(운영 n=2^15, 32MiB)을 시험에서 n=2^10 으로 낮춘다.
    저장 형식이 n 을 들고 있어 검증은 그대로다. 비용 자체를 보는 test_password.py 는
    운영 값으로 돈다. monkeypatch 를 쓰지 않는다: autouse 가 monkeypatch 를 먼저 세우면 시험의
    monkeypatch 가 client 의 정리(앱 종료)가 끝날 때까지 남는다."""
    if request.module.__name__.rsplit(".", 1)[-1] == "test_password":
        yield
        return
    saved = password._N
    password._N = 2**10
    try:
        yield
    finally:
        password._N = saved


# 모든 응답을 오류 코드표와 맞춰 본다 (P74 2.2절). 운영은 이 검사를 하지 않는다
error_codes.STRICT = True

# 셸 스크립트의 파이썬 조각(scripts/lib 의 image_report·image_pin·registry_auth·cpython_builds·
# public_fetch·llm_gateway_images)을 테스트 모듈이 바로 import 한다
SCRIPTS_LIB = REPO / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))


# 앱을 만들 때마다 lifespan 이 app.openapi() 로 스키마 전체를 다시 만든다(비서 도구 표의
# 재료, D31). 한 번에 0.45초쯤이고 라우트가 같으면 결과도 같으므로, 테스트에서는 라우트 표를
# 열쇠로 한 번만 만든다.
_OPENAPI_BY_ROUTES: dict[tuple, dict] = {}
_generate_openapi = FastAPI.openapi


def _route_key(route) -> tuple:
    # create_app 안에서 만든 엔드포인트는 앱마다 새 함수라 객체가 아니라 이름으로 맞댄다
    endpoint = getattr(route, "endpoint", None)
    return (
        getattr(route, "path", None),
        tuple(sorted(getattr(route, "methods", None) or ())),
        getattr(endpoint, "__module__", None),
        getattr(endpoint, "__qualname__", None),
    )


def _cached_openapi(app: FastAPI) -> dict:
    if app.openapi_schema is None:
        key = tuple(_route_key(r) for r in app.routes)
        if key not in _OPENAPI_BY_ROUTES:
            _OPENAPI_BY_ROUTES[key] = _generate_openapi(app)
        app.openapi_schema = _OPENAPI_BY_ROUTES[key]
    return app.openapi_schema


FastAPI.openapi = _cached_openapi


# 워커(xdist)마다 DB 하나를 쓴다. 테스트마다 새 DB 를 만들면 앱 기동이 컬렉션 25개와
# 인덱스 81개를 새로 만드느라 1초쯤 걸리고, MongoDB 는 파일을 만드는 DDL 을 한 줄로 세우므로
# 병렬로 돌려도 빨라지지 않는다. 그래서 워커 DB 에 기동을 한 번 돌려 인덱스를 만들어 두고,
# 테스트가 끝날 때마다 문서를 비우고 컬렉션과 인덱스를 기동 직후의 모양으로 되돌린다.
# RUN_ID 는 같은 Mongo 를 쓰는 다른 실행(다른 worktree)과 DB 이름이 겹치지 않게 한다.
RUN_ID = uuid.uuid4().hex[:8]
WORKER_DB = f"daegam_test_{RUN_ID}_{os.environ.get('PYTEST_XDIST_WORKER', 'main')}"


def _test_settings(mongo_db: str, mongo_url: str = MONGO_URL) -> Settings:
    # 인증은 header 모드다(테넌트 헤더를 그대로 믿는다). oidc·local 모드는 아래 oidc_settings·
    # local_settings 가 이 설정을 바꿔 만든다.
    return Settings(
        mongo_url=mongo_url,
        mongo_db=mongo_db,
        reconcile_enabled=False,
        pod_id="test-pod",
        auth_mode="header",
        # Spec Generator 워커는 job 을 만든 뒤 상태를 보는 테스트와 경합하므로 기본으로 끈다.
        # 워커가 필요한 테스트는 spec_worker_enabled=True 로 자기 앱을 만든다.
        spec_worker_enabled=False,
        # Knowledge 인덱서도 같은 이유로 끈다. 필요한 테스트는 run·tick 을 직접 부른다 (D32)
        knowledge_indexer_enabled=False,
        # MCP 감사 수집 워커도 마찬가지로 끈다. 필요한 테스트는 run_once 를 직접 부른다
        audit_collector_enabled=False,
        # 백그라운드 실행의 워커도 같은 이유로 끈다. 필요한 테스트는 fire 를 직접 부른다 (P41)
        runs_workers_enabled=False,
        # Agent 테스트가 kind: stub provider 를 등록한다 (D25 예외). 막는 쪽은 자기 앱을 만든다
        allow_stub_provider=True,
    )


Baseline = dict[str, set[str]]


def _startup_baseline(client: MongoClient) -> Baseline:
    """워커 DB 를 비우고 앱을 한 번 띄운 뒤, 기동이 만든 컬렉션마다 인덱스 이름을 적는다."""
    client.drop_database(WORKER_DB)
    app = create_app(
        _test_settings(WORKER_DB),
        client_factory=lambda _cluster: FakeClusterClient(),
        secret_store=FakeSecretStore(),
    )
    with TestClient(app):
        pass
    db = client[WORKER_DB]
    return {name: _index_names(db[name]) for name in db.list_collection_names()}


def _index_names(collection) -> set[str]:
    return {ix["name"] for ix in collection.list_indexes()}


def _reset_to_baseline(db, baseline: Baseline) -> bool:
    """문서를 비우고 테스트가 더한 컬렉션·인덱스를 지운다. 기동이 만든 것이 빠졌으면 False 다."""
    present = set()
    for name in db.list_collection_names():
        if name.startswith("system."):
            continue
        if name not in baseline:
            db.drop_collection(name)
            continue
        present.add(name)
        db[name].delete_many({})
        indexes = _index_names(db[name])
        for extra in indexes - baseline[name]:
            db[name].drop_index(extra)
        if not baseline[name] <= indexes:
            return False
    return present == set(baseline)


@pytest.fixture(scope="session")
def _worker_db():
    """(클라이언트, 기동 직후의 모양). 세션이 끝나면 워커 DB 를 지운다.

    DAEGAM_TEST_SKIP_MONGO=1 이면 Mongo 를 쓰는 시험을 건너뛴다. 시험용 Mongo 없이
    도는 로컬 게이트의 링크 갈래가 준다(Makefile PLATFORM_STORES). 그 시험은 PR 의
    pr-check 가 돌린다."""
    if os.environ.get("DAEGAM_TEST_SKIP_MONGO") == "1":
        pytest.skip("needs MongoDB: no DAEGAM_TEST_MONGO_URL here; the PR's pr-check runs it")
    client: MongoClient = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    state = {"baseline": _startup_baseline(client)}
    try:
        yield client, state
    finally:
        client.drop_database(WORKER_DB)
        client.close()


@pytest.fixture
def settings(_worker_db) -> Iterator[Settings]:
    """워커 DB 를 쓰는 설정. 테스트가 끝나면 DB 를 기동 직후의 모양으로 되돌린다. DB 를 쓰지 않는
    시험은 offline_settings 를 받는다."""
    client, state = _worker_db
    yield _test_settings(WORKER_DB)
    if not _reset_to_baseline(client[WORKER_DB], state["baseline"]):
        # 테스트가 기동이 만든 컬렉션이나 인덱스를 지웠다. 다음 테스트가 같은 데서 출발하게
        # 다시 만든다
        state["baseline"] = _startup_baseline(client)


@pytest.fixture
def offline_settings() -> Settings:
    """워커 DB 를 끌어오지 않는 설정. 설정 값만 보거나 앱을 만들기만 하고 띄우지 않는 시험이 받는다.
    그래서 시험용 Mongo 가 없는 로컬 게이트도 그 시험을 돌린다. DB 를 쓰는 시험은 settings 를
    받는다."""
    return _test_settings("daegam_test_offline", OFFLINE_MONGO_URL)


@pytest.fixture
def bare_env(monkeypatch):
    """DAEGAM_* 가 하나도 없는 환경. settings_from_env 가 바깥 셸의 값을 읽지 않게 한다."""
    for key in list(os.environ):
        if key.startswith("DAEGAM_"):
            monkeypatch.delenv(key)
    return monkeypatch


@pytest.fixture
def mongo(settings: Settings):
    """동기 pymongo 클라이언트. 저장된 원문 문서를 검사한다. 비우는 일은 settings 가 한다."""
    client: MongoClient = MongoClient(settings.mongo_url, serverSelectionTimeoutMS=5000)
    try:
        yield client[settings.mongo_db]
    finally:
        client.close()


class FakeClusters:
    """이름 → FakeClusterClient. 없는 이름은 기본 fake 를 만든다. factory 호출 횟수를 센다."""

    def __init__(self) -> None:
        self.by_name: dict[str, FakeClusterClient] = {}
        self.calls: list[str] = []

    def factory(self, cluster):
        self.calls.append(cluster.id)
        return self.by_name.setdefault(cluster.name, FakeClusterClient())


@pytest.fixture
def fake_clusters() -> FakeClusters:
    return FakeClusters()


# LLM provider 키 Secret(LLM_KEY_SECRET)을 미리 넣어 두므로 tests/helpers/spec_jobs.py 의 PROVIDERS
# 로 바로 job 을 만들 수 있다.
@pytest.fixture
def fake_secrets() -> FakeSecretStore:
    ss = FakeSecretStore()
    ss.put(LLM_KEY_SECRET, {"api_key": "sk-test-key"}, namespace=PLATFORM_NS)
    return ss


@pytest.fixture
def client(settings: Settings, mongo, fake_clusters: FakeClusters, fake_secrets: FakeSecretStore):
    with api_client(make_app(settings, fake_clusters, fake_secrets)) as c:
        yield c


# local 인증 모드(D40)의 플랫폼이다. 서비스 발급자는 ISSUER 이고 공개 주소가 없으면 사람 토큰의
# 발급자도 ISSUER 다. 부트스트랩 관리자 비밀번호는 LOCAL_BOOT_PASSWORD 다
def _local_mode(settings: Settings) -> Settings:
    return dataclasses.replace(
        settings,
        auth_mode="local",
        platform_issuer=ISSUER,
        local_bootstrap_password=LOCAL_BOOT_PASSWORD,
    )


@pytest.fixture
def local_settings(settings: Settings) -> Settings:
    return _local_mode(settings)


@pytest.fixture
def local_app(local_settings, mongo, fake_clusters, fake_secrets):
    return make_app(local_settings, fake_clusters, fake_secrets)


@pytest.fixture
def local_client(local_app):
    with api_client(local_app) as c:
        yield c


@pytest.fixture
def local_admin(local_client, mongo):
    """local 모드 플랫폼에 부트스트랩 관리자로 들어간 클라이언트. 테넌트는 default 다."""
    local_client.headers.update({**admin_headers(local_client, mongo), TENANT_HEADER: "default"})
    return local_client


# OAuth 인가 서버가 켜진 local 플랫폼 (P33 1절). 공개 주소는 PUBLIC_URL 이고 https 라 평문 허용
# 플래그가 필요 없다
def _oauth_mode(local: Settings) -> Settings:
    return dataclasses.replace(local, public_url=PUBLIC_URL)


@pytest.fixture
def oauth_settings(local_settings) -> Settings:
    return _oauth_mode(local_settings)


@pytest.fixture
def offline_oauth_settings(offline_settings) -> Settings:
    """oauth_settings 와 같되 워커 DB 를 끌어오지 않는다 (offline_settings)."""
    return _oauth_mode(_local_mode(offline_settings))


@pytest.fixture
def oauth_client(oauth_settings, mongo, fake_clusters, fake_secrets):
    with api_client(make_app(oauth_settings, fake_clusters, fake_secrets)) as c:
        yield c


# OAuth 플랫폼의 관리자, acme 의 멤버, 노출한 Gateway 와 DCR 로 등록한 스크립트 클라이언트, 그
# 클라이언트로 맺은 연결이다. 테넌트는 문서 빌더의 것(acme)이고 주소는 tests/helpers/oauth.py 의
# 것이다


@pytest.fixture
def oauth_admin(oauth_client, mongo) -> dict[str, str]:
    return admin_headers(oauth_client, mongo)


@pytest.fixture
def member(oauth_client, oauth_admin, mongo):
    """acme 의 viewer alice(`Person`)."""
    return person(oauth_client, oauth_admin, mongo, "alice", TENANT)


@pytest.fixture
def client_id(oauth_client, mongo) -> str:
    """노출한 Gateway gw-1 과 DCR 로 등록한 스크립트 클라이언트의 client_id."""
    seed_gateway(mongo, "gw-1", GW_HOST)
    return register(oauth_client)


@pytest.fixture
def connected(oauth_client, client_id, member) -> dict:
    """member 가 허용하고 코드를 바꿔 받은 토큰 응답."""
    return connect(oauth_client, client_id, member)


@pytest.fixture
def root_client(
    settings: Settings, mongo, fake_clusters: FakeClusters, fake_secrets: FakeSecretStore
):
    """루트에 뿌리내린 클라이언트. /healthz 처럼 /api 밖에 남은 경로를 부를 때 쓴다."""
    with TestClient(make_app(settings, fake_clusters, fake_secrets)) as c:
        yield c


@pytest.fixture
def local_root(local_app):
    """local 모드 플랫폼(local_app)의 루트 클라이언트. /.well-known/* 의 JWKS 를 부를 때 쓴다."""
    with TestClient(local_app) as c:
        yield c


@pytest.fixture(scope="session")
def openapi_spec() -> dict:
    """기본 설정 앱의 OpenAPI 문서. 라우트만 보므로 세션에 한 번 만든다."""
    return create_app().openapi()


# -- OIDC (P6). mock OIDC(tests/helpers/oidc.py) 를 httpx ASGITransport 로 인프로세스에 붙인다 -----


@pytest.fixture
def signer() -> Signer:
    return Signer(None)


@pytest.fixture
def mock_oidc(signer):
    """(ASGI 앱, 그 앱을 호출하는 sync TestClient). 토큰 발급은 TestClient 로 한다."""
    app = create_mock_oidc(mock_oidc_config(), signer)
    return app, TestClient(app)


@pytest.fixture
def oidc_settings(settings: Settings) -> Settings:
    """IdP 를 믿는 oidc 모드. 데이터 플레인 발급자의 기본은 platform 이지만(D104) 이 픽스처를 쓰는
    시험은 IdP 를 믿는 길을 본다. 그 길은 되돌아갈 자리로 남는다(P53b 12절 5번). 플랫폼을 믿는
    oidc 는 시험이 `data_plane_issuer="platform"` 으로 따로 청한다."""
    return dataclasses.replace(
        settings,
        auth_mode="oidc",
        oidc_issuer=OIDC_ISSUER,
        oidc_audience=OIDC_AUDIENCE,
        oidc_insecure_http=True,  # mock 발급자는 평문 http 다
        data_plane_issuer="idp",
    )


@pytest.fixture
def oidc_transport(mock_oidc) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=mock_oidc[0])


@pytest.fixture
def oidc_client(oidc_settings, mongo, fake_clusters, fake_secrets, oidc_transport):
    """oidc 모드 플랫폼. 토큰은 `token` 픽스처로 만든다."""
    http = httpx.AsyncClient(transport=oidc_transport, base_url=OIDC_ISSUER)
    with api_client(
        make_app(oidc_settings, fake_clusters, fake_secrets, oidc_http_client=http)
    ) as c:
        yield c
    asyncio.run(http.aclose())


@pytest.fixture
def token(mock_oidc):
    """mock OIDC 토큰을 만드는 함수 `token(sub, *groups, **본문)`."""

    def make(sub: str, *groups: str, **kw) -> str:
        return user_token(mock_oidc[1], sub=sub, groups=list(groups), **kw)

    return make


# 그룹 → 역할 매핑(tests/helpers/consts.py)을 넣은 oidc 플랫폼. `oidc` 는 테넌트 a 의 viewer 와
# 테넌트 b 의 developer 를, `oidc_b_viewer` 는 테넌트 b 의 viewer 를 더한 매핑이다


@pytest.fixture
def oidc(oidc_client, mongo):
    map_groups(mongo, GROUP_MAPPINGS)
    return oidc_client


@pytest.fixture
def oidc_b_viewer(oidc_client, mongo):
    map_groups(mongo, B_VIEWER_MAPPINGS)
    return oidc_client


@pytest.fixture
def oidc_headers(token, mongo) -> dict[str, str]:
    """oidc 플랫폼(oidc_client)의 플랫폼 관리자가 테넌트 acme 로 보내는 헤더. 그룹 daegam-admins 를
    platform-admin 으로 매핑하고 그 그룹을 가진 root 의 토큰을 단다."""
    map_groups(mongo, [{"group": "daegam-admins", "tenant": "*", "role": "platform-admin"}])
    return bearer(token("root", "daegam-admins"), tenant=TENANT)


@pytest.fixture
def admin(local_client, mongo) -> dict[str, str]:
    return admin_headers(local_client, mongo)
