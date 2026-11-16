"""차트가 만드는 Secret 과 산 객체를 읽는 규칙을 클러스터 없이 본다 (P85 4절·5절).

차트는 클러스터 없는 렌더에서 만드는 Secret 을 거절하므로, 시험은 차트 사본에 시험만의 정의를
더한다(tests/helpers/chart.py `live_cluster`). daegam.online 을 참으로, daegam.live 를 주어진 산
객체로 바꿔 처음 설치와 업그레이드의 렌더를 helm template 으로 재현한다.
"""

from __future__ import annotations

import base64
import hashlib
import re
from urllib.parse import urlsplit

import pytest

from tests.helpers.chart import chart_copy, config_data, live_cluster, named, pod_template, render
from tests.helpers.chart import render_error as chart_error
from tests.helpers.consts import PLATFORM_NS

HEX48 = re.compile(r"[0-9a-f]{48}")
# offline-values.yaml 의 existingSecret 셋을 비워 차트가 만들게 한다
GENERATED = {f"secrets.{k}.existingSecret": "" for k in ("platform", "docparse", "mongo")}
KEEP = {"helm.sh/resource-policy": "keep"}


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def _data(secret: dict) -> dict[str, str]:
    return {k: base64.b64decode(v).decode() for k, v in secret["data"].items()}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _render(tmp_path, live=None, **sets):
    chart = chart_copy(tmp_path, live_cluster(live or {}))
    return render(chart=chart, sets=GENERATED | sets)


def _error(tmp_path, live=None, **sets):
    chart = chart_copy(tmp_path, live_cluster(live or {}))
    return chart_error(chart=chart, sets=GENERATED | sets)


def test_a_first_install_generates_one_password_one_token_and_keeps_them(tmp_path):
    docs = _render(tmp_path)
    secrets = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Secret"}
    assert set(secrets) == {"daegam-mongo-auth", "daegam-platform-secrets", "daegam-docparse-token"}
    for secret in secrets.values():
        assert secret["metadata"].get("annotations") == KEEP, secret["metadata"]["name"]
    mongo = _data(secrets["daegam-mongo-auth"])
    platform = _data(secrets["daegam-platform-secrets"])
    token = _data(secrets["daegam-docparse-token"])["token"]
    # MongoDB 비밀번호와 그 URL 은 한 값이다 (P85 5절)
    url = urlsplit(platform["DAEGAM_MONGO_URL"])
    assert (url.username, url.password) == (mongo["username"], mongo["password"])
    assert (url.hostname, url.port) == (f"mongodb.{PLATFORM_NS}.svc", 27017)
    assert mongo["username"] == "daegam"
    for value in (mongo["password"], platform["DAEGAM_LOCAL_BOOTSTRAP_PASSWORD"], token):
        assert HEX48.fullmatch(value), value
    assert re.fullmatch(r"admin-[0-9a-f]{6}", platform["DAEGAM_LOCAL_BOOTSTRAP_USERNAME"])
    # 두 파드가 같은 토큰의 체크섬을 싣고, 플랫폼은 MongoDB 비밀번호의 체크섬도 싣는다
    main = pod_template(docs, "daegam-platform")["metadata"]["annotations"]
    parser = pod_template(docs, "daegam-docparse")["metadata"]["annotations"]
    assert main["daegam.devops.ai.kr/docparse-token-sha"] == _sha(token)
    assert parser["daegam.devops.ai.kr/docparse-token-sha"] == _sha(token)
    assert main["daegam.devops.ai.kr/mongo-auth-sha"] == _sha(mongo["password"])
    assert "daegam.devops.ai.kr/mongo-auth-sha" not in parser


def test_the_first_admin_takes_the_configured_name(tmp_path):
    docs = _render(tmp_path, **{"auth.adminUsername": "ops-admin"})
    platform = _data(named(docs, "Secret", "daegam-platform-secrets"))
    assert platform["DAEGAM_LOCAL_BOOTSTRAP_USERNAME"] == "ops-admin"


def test_an_upgrade_keeps_the_live_values(tmp_path):
    live = {
        ("Secret", "daegam-mongo-auth"): {
            "data": {"username": _b64("root"), "password": _b64("old-mongo")}
        },
        ("Secret", "daegam-platform-secrets"): {
            "data": {
                "DAEGAM_LOCAL_BOOTSTRAP_USERNAME": _b64("admin-abc123"),
                "DAEGAM_LOCAL_BOOTSTRAP_PASSWORD": _b64("old-admin"),
            }
        },
        ("Secret", "daegam-docparse-token"): {"data": {"token": _b64("old-token")}},
        ("PersistentVolumeClaim", "data-mongodb-0"): {"metadata": {"name": "data-mongodb-0"}},
    }
    docs = _render(tmp_path, live, **{"auth.adminUsername": "ignored"})
    assert _data(named(docs, "Secret", "daegam-mongo-auth")) == {
        "username": "root",
        "password": "old-mongo",
    }
    platform = _data(named(docs, "Secret", "daegam-platform-secrets"))
    assert platform == {
        # URL 은 산 MongoDB 비밀번호에서 다시 만든다
        "DAEGAM_MONGO_URL": f"mongodb://root:old-mongo@mongodb.{PLATFORM_NS}.svc:27017/?authSource=admin",
        "DAEGAM_LOCAL_BOOTSTRAP_USERNAME": "admin-abc123",
        "DAEGAM_LOCAL_BOOTSTRAP_PASSWORD": "old-admin",
    }
    assert _data(named(docs, "Secret", "daegam-docparse-token")) == {"token": "old-token"}


def test_a_lost_mongo_secret_with_its_volume_refuses_the_render(tmp_path):
    """root 사용자는 빈 볼륨으로 처음 뜰 때만 만들어진다. PVC 가 있는데 Secret 이 없으면 새
    비밀번호는 맞지 않는다 (P85 5절)."""
    live = {("PersistentVolumeClaim", "data-mongodb-0"): {"metadata": {"name": "data-mongodb-0"}}}
    assert "the PVC data-mongodb-0 exists" in _error(tmp_path, live)


SHIPPED = {
    "hosts": ["redis.daegam-system.svc"],
    "port": 6379,
    "tls_port": 6380,
    "ca_configmap": "daegam-redis-ca",
}


def _recorded(plain: bool) -> dict:
    raw = (
        '{"hosts": ["redis.daegam-system.svc"], "port": 6379, "tls_port": 6380, '
        f'"ca_configmap": "daegam-redis-ca", "plain": {str(plain).lower()}}}'
    )
    return {("ConfigMap", "daegam-platform-config"): {"data": {"DAEGAM_HOME_REDIS_TLS": raw}}}


def _shipped_sets(plain: bool | None, reopen: bool = False) -> dict:
    sets: dict = {"home.allowRedisPlainReopen": reopen}
    if plain is not None:
        sets |= {f"home.shippedRedis.{k}": v for k, v in SHIPPED.items() if k != "hosts"}
        sets |= {
            "home.shippedRedis.hosts[0]": SHIPPED["hosts"][0],
            "home.shippedRedis.plain": plain,
        }
    return sets


def test_an_upgrade_without_the_shipped_redis_value_is_refused(tmp_path):
    """산 ConfigMap 이 함께 올린 Redis 를 적었으면 그 값을 빼고 올리지 못한다 (P85 4절)."""
    assert "set home.shippedRedis" in _error(tmp_path, _recorded(True), **_shipped_sets(None))


@pytest.mark.parametrize(
    ("recorded", "plain", "reopen", "refused"),
    [
        (False, True, False, True),  # 닫힌 평문 포트를 말없이 다시 열지 않는다
        (False, True, True, False),  # 여는 것은 따로 허락한다
        (False, False, False, False),
        (True, False, False, False),  # 닫는 것은 그 Redis 를 두는 배포의 단계다
        (True, True, False, False),
    ],
)
def test_the_plain_port_reopens_only_when_allowed(tmp_path, recorded, plain, reopen, refused):
    sets = _shipped_sets(plain, reopen)
    if refused:
        assert "home.allowRedisPlainReopen" in _error(tmp_path, _recorded(recorded), **sets)
    else:
        cm = config_data(_render(tmp_path, _recorded(recorded), **sets))
        assert f'"plain":{str(plain).lower()}' in cm["DAEGAM_HOME_REDIS_TLS"]
