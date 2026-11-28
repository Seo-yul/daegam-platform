"""대감 차트가 멈추는 렌더와 lint·스키마 (P85 4절·5절, 12절, 13절 F0.2 의 클러스터 없는 부분).

값의 규칙은 JSON Schema 로 적을 수 있는 것을 values.schema.json 에 두어 helm template 과 helm
lint 가 함께 막고, 템플릿의 fail 은 스키마를 건너뛴 렌더의 받침이다. helm lint 는 템플릿의 fail 을
INFO 줄로 낮추므로 스키마로 적지 못하는 규칙(네임스페이스, 클러스터 없는 렌더, 산 객체)은 helm
template 만 막는다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from daegam.config import settings_from_env
from tests.helpers.chart import (
    CHART,
    OFFLINE,
    chart_copy,
    config_data,
    helm,
    pod_template,
    render,
    render_error,
    values_file,
)
from tests.helpers.consts import PLATFORM_NS

SCHEMA = json.loads((CHART / "values.schema.json").read_text())
RESERVED = set(SCHEMA["definitions"]["reservedKeys"]["enum"])
CONFIG_ONLY = set(SCHEMA["definitions"]["configOnlyKeys"]["enum"])
# 차트가 ConfigMap 밖에서 정하는 이름. Secret 의 키와 컨테이너 env 다
OUTSIDE_CONFIGMAP = {
    "DAEGAM_MONGO_URL",
    "DAEGAM_LOCAL_BOOTSTRAP_USERNAME",
    "DAEGAM_LOCAL_BOOTSTRAP_PASSWORD",
    "DAEGAM_NAMESPACE",
    "DAEGAM_DOCPARSE_TOKEN",
}
HEADER_ALLOWED = {"auth": {"mode": "header", "allowHeaderMode": True}}
# ConfigMap 으로만 받지만 차트가 기본 키를 쓰지 않는 이름. 개발용 스위치 둘과 0 으로 줄이기의 설정
# 다섯이다(P44 14절). 다섯의 기본은 플랫폼의 것이라 차트를 고치지 않고 A4 가 창의 기본을 바꾼다.
# 평가의 설정 넷과 Agent 호출의 스위치도 기본이 플랫폼의 것이다(P45 13절, P46 6.5절)
NO_CHART_DEFAULT = {
    "DAEGAM_ALLOW_STUB_PROVIDER",
    "DAEGAM_SANDBOX_ALLOW_UNCHECKED",
    "DAEGAM_AGENT_SCALE_TO_ZERO",
    "DAEGAM_AGENT_SLEEP_AFTER_SECONDS",
    "DAEGAM_AGENT_HOUSEKEEPING_HOURS",
    "DAEGAM_AGENT_HOUSEKEEPING_PARALLEL",
    "DAEGAM_AGENT_HOUSEKEEPING_HOLD_SECONDS",
    "DAEGAM_EVALUATIONS_ENABLED",
    "DAEGAM_EVALUATION_RUN_TOKENS",
    "DAEGAM_EVALUATION_RUNS_PER_DAY",
    "DAEGAM_EVALUATION_RESULT_DAYS",
    "DAEGAM_EVALUATION_GATE_CRITERIA",
    "DAEGAM_AGENT_CALLS_ENABLED",
}


def _lint(*values: Path) -> str:
    """helm lint --strict 의 끝 코드와 출력. 0 이 아니면 lint 가 실패한 것이다."""
    args = ["lint", "--strict", str(CHART), "-n", PLATFORM_NS, "-f", str(OFFLINE)]
    for path in values:
        args += ["-f", str(path)]
    out = helm(*args)
    return f"exit={out.returncode}\n{out.stdout}{out.stderr}"


def test_helm_lint_strict_passes_with_the_offline_values():
    """완료 조건의 lint 다 (P85 15절). 경고도 실패로 센다. lint 는 템플릿의 fail 을 INFO 줄로만
    남기므로 그 줄이 없는지도 본다."""
    out = _lint()
    assert out.startswith("exit=0\n"), out
    assert "[INFO] Fail:" not in out


@pytest.mark.parametrize(
    "dry_run", [[], ["--dry-run=client"]], ids=["template", "template-client-dry-run"]
)
@pytest.mark.parametrize("generated", ["platform", "docparse", "mongo"])
def test_a_generated_secret_refuses_a_render_without_a_cluster(dry_run, generated):
    """만드는 Secret 이 하나라도 있으면 클러스터 없는 렌더는 그 값의 existingSecret 을 짚고 멈춘다.
    산 값을 읽지 못한 채 새 값을 내면 적용하는 순간 살아 있는 비밀번호를 덮는다 (P85 5절)."""
    sets = [f"secrets.{generated}.existingSecret="]
    if generated == "platform":
        sets.append("secrets.mongo.existingSecret=")  # 만드는 platform 은 만드는 mongo 와 함께다
    out = helm(
        "template",
        "daegam",
        str(CHART),
        *dry_run,
        "-n",
        PLATFORM_NS,
        "-f",
        str(OFFLINE),
        *[a for s in sets for a in ("--set", s)],
    )
    assert out.returncode != 0
    assert "no cluster access" in out.stderr
    assert f"secrets.{generated}.existingSecret" in out.stderr


# 값의 규칙마다 (값, 스키마 오류가 짚는 곳, 스키마를 건너뛴 렌더에서 템플릿이 짚는 글)
BAD_VALUES = {
    "header-without-allow": (
        {"auth": {"mode": "header"}},
        "/auth/allowHeaderMode",
        "auth.allowHeaderMode: true",
    ),
    "oidc-without-issuer": ({"auth": {"mode": "oidc"}}, "/auth/oidc/issuer", "auth.oidc.issuer"),
    # UI 의 PKCE 클라이언트가 없으면 아무도 로그인하지 못한다 (P53b 6.4절)
    "oidc-without-client-id": (
        {"auth": {"mode": "oidc", "oidc": {"issuer": "https://idp.example.com"}}},
        "/auth/oidc/clientId",
        "auth.oidc.clientId",
    ),
    "http-with-tls": (
        {"scheme": "http", "ingress": {"tls": "edge"}},
        "/ingress/tls",
        "use scheme https",
    ),
    "stub-switch": (
        {"platform": {"extraConfig": {"DAEGAM_ALLOW_STUB_PROVIDER": " Yes "}}},
        "/platform/extraConfig/DAEGAM_ALLOW_STUB_PROVIDER",
        "turns on a development switch",
    ),
    "sandbox-switch": (
        {"platform": {"extraConfig": {"DAEGAM_SANDBOX_ALLOW_UNCHECKED": True}}},
        "/platform/extraConfig/DAEGAM_SANDBOX_ALLOW_UNCHECKED",
        "turns on a development switch",
    ),
    "header-through-extra-env": (
        {"platform": {"extraEnv": [{"name": "DAEGAM_AUTH_MODE", "value": "header"}]}},
        "/platform/extraEnv/0/name",
        "platform.extraEnv: DAEGAM_AUTH_MODE",
    ),
    "stub-through-extra-env": (
        {"platform": {"extraEnv": [{"name": "DAEGAM_ALLOW_STUB_PROVIDER", "value": "true"}]}},
        "/platform/extraEnv/0/name",
        "platform.extraEnv: DAEGAM_ALLOW_STUB_PROVIDER",
    ),
    "issuer-through-ui-env": (
        {"ui": {"extraEnv": [{"name": "DAEGAM_OIDC_ISSUER", "value": "https://idp.example.com"}]}},
        "/ui/extraEnv/0/name",
        "ui.extraEnv: DAEGAM_OIDC_ISSUER",
    ),
    "token-through-docparse-env": (
        {"docparse": {"extraEnv": [{"name": "DAEGAM_DOCPARSE_TOKEN", "value": "x"}]}},
        "/docparse/extraEnv/0/name",
        "docparse.extraEnv: DAEGAM_DOCPARSE_TOKEN",
    ),
    "auth-mode-through-extra-config": (
        {"platform": {"extraConfig": {"DAEGAM_AUTH_MODE": "header"}}},
        "invalid propertyName 'DAEGAM_AUTH_MODE'",
        "platform.extraConfig.DAEGAM_AUTH_MODE",
    ),
    "ui-client-id-through-extra-config": (
        {"platform": {"extraConfig": {"DAEGAM_OIDC_CLIENT_ID": "ui"}}},
        "invalid propertyName 'DAEGAM_OIDC_CLIENT_ID'",
        "platform.extraConfig.DAEGAM_OIDC_CLIENT_ID",
    ),
    "redis-key-through-extra-config": (
        {"platform": {"extraConfig": {"DAEGAM_HOME_REDIS_TLS": "{}"}}},
        "invalid propertyName 'DAEGAM_HOME_REDIS_TLS'",
        "platform.extraConfig.DAEGAM_HOME_REDIS_TLS",
    ),
    "external-mongodb-without-secret": (
        {"mongodb": {"bundled": False}, "secrets": {"platform": {"existingSecret": ""}}},
        "/secrets/platform/existingSecret",
        "mongodb.bundled false needs secrets.platform.existingSecret",
    ),
    "generated-platform-with-existing-mongo": (
        {"secrets": {"platform": {"existingSecret": ""}}},
        "/secrets/mongo/existingSecret",
        None,  # 템플릿은 그 Secret 을 만들 때 막고, 클러스터 없는 렌더는 그보다 먼저 멈춘다
    ),
    # LLM 게이트웨이 (P85 8절). 예약 네임스페이스와 YAML 의 따옴표 없는 off 는 고칠 말을 주려고
    # 템플릿만 막는다(test_chart_gateway.py)
    "gateway-namespace-not-a-dns-label": (
        {"llmGateway": {"namespace": "Bad_NS"}},
        "/llmGateway/namespace",
        "is not a DNS label",
    ),
    "gateway-on-a-remote-cluster": (
        {"target": "remote", "llmGateway": {"mode": "agentgateway"}},
        "/llmGateway/mode",
        "goes only with target home",
    ),
    "gateway-key-through-extra-config": (
        {"platform": {"extraConfig": {"DAEGAM_LLM_GATEWAY": "agentgateway"}}},
        "invalid propertyName 'DAEGAM_LLM_GATEWAY'",
        "platform.extraConfig.DAEGAM_LLM_GATEWAY",
    ),
    # 플랫폼이 기동을 거절하는 참조(store/llm_gateway.py LlmGatewayImages)는 스키마가 먼저 막는다
    "unpinned-gateway-image": (
        {"llmGateway": {"images": {"redis": {"image": "docker.io/library/redis:8.2"}}}},
        "/llmGateway/images/redis/image",
        None,
    ),
}


@pytest.mark.parametrize("case", BAD_VALUES)
def test_the_schema_refuses_bad_values(tmp_path, case):
    values, where, _ = BAD_VALUES[case]
    error = render_error(values_file(tmp_path, values))
    assert "don't meet the specifications of the schema" in error and where in error, error


def test_helm_lint_reads_the_schema(tmp_path):
    """lint 도 스키마를 읽는다. 규칙마다의 거절은 helm template 으로 위 시험이 본다."""
    out = _lint(values_file(tmp_path, BAD_VALUES["header-without-allow"][0]))
    assert not out.startswith("exit=0\n"), out


def test_helm_lint_refuses_an_oidc_install_without_the_ui_client(tmp_path):
    """UI 의 PKCE 클라이언트 없는 oidc 설치는 lint 에서 멈춘다. 그 설치에서는 아무도 로그인하지
    못하고 최근 로그인의 ID 토큰을 맞댈 클라이언트도 없다 (P53b 6.4절)."""
    out = _lint(values_file(tmp_path, BAD_VALUES["oidc-without-client-id"][0]))
    assert not out.startswith("exit=0\n") and "/auth/oidc/clientId" in out, out


@pytest.mark.parametrize("case", [c for c, (_, _, backstop) in BAD_VALUES.items() if backstop])
def test_the_templates_refuse_bad_values_without_the_schema(tmp_path, case):
    values, _, backstop = BAD_VALUES[case]
    error = render_error(values_file(tmp_path, values), flags=("--skip-schema-validation",))
    assert backstop in error, error


def test_the_default_values_need_a_host_and_a_registry():
    """공개 이미지가 없으므로 registry 는 기본값이 없다 (P85 16절)."""
    out = helm("template", "daegam", str(CHART), "-n", PLATFORM_NS)
    assert out.returncode != 0
    assert "/host" in out.stderr and "/images/registry" in out.stderr


def test_the_schema_rejects_unknown_keys_and_accepts_global():
    assert "additional properties 'bogus'" in render_error(sets={"bogus": "1"})
    assert "additional properties 'tg'" in render_error(sets={"images.platform.tg": "x"})
    render(sets={"global.anything": "1"})


@pytest.mark.parametrize(
    ("values", "where"),
    [
        ({"images": {"platform": {"digest": "sha256:short"}}}, "/images/platform/digest"),
        ({"images": {"registry": "registry example"}}, "/images/registry"),
        ({"auth": {"mode": "basic"}}, "/auth/mode"),
        ({"home": {"exposure": {"tls": "acme"}}}, "/home/exposure/tls"),
        ({"home": {"blockMetadata": "yes"}}, "/home/blockMetadata"),
        ({"home": {"shippedRedis": {"port": 6379}}}, "/home/shippedRedis"),
        (
            {"home": {"resources": {"requests": {"cpu": "50m", "memory": "64Mi"}}}},
            "/home/resources",
        ),
        (
            {
                "home": {
                    "resources": {
                        "requests": {"cpu": "50m"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    }
                }
            },
            "/home/resources/requests",
        ),
        (
            {
                "home": {
                    "resources": {
                        "requests": {"cpu": 1, "memory": "64Mi"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    }
                }
            },
            "/home/resources/requests/cpu",
        ),
        (
            {"home": {"limitRange": {"limits": [{"type": "Container", "default": {"cpu": 1}}]}}},
            "/home/limitRange",
        ),
        ({"platform": {"extraConfig": {"lower_case": "x"}}}, "'lower_case' does not match pattern"),
        (
            {"platform": {"extraConfig": {"DAEGAM_SANDBOX_MAX": 1e9}}},
            "/platform/extraConfig/DAEGAM_SANDBOX_MAX",
        ),
    ],
)
def test_the_schema_types_values(tmp_path, values, where):
    assert where in render_error(values_file(tmp_path, values))


def test_the_release_lives_in_daegam_system():
    """클러스터 범위의 이름과 코드의 daegam-system 이 하나뿐이라 다른 네임스페이스는 멈춘다
    (P85 3절). 값이 아니라 스키마로 적지 못한다."""
    assert "namespace daegam-system" in render_error(namespace="daegam")


def test_header_and_oidc_modes_render_with_their_values():
    cm = config_data(render(sets={"auth.mode": "header", "auth.allowHeaderMode": True}))
    assert cm["DAEGAM_AUTH_MODE"] == "header"
    oidc = {
        "auth.mode": "oidc",
        "auth.oidc.issuer": "https://idp.example.com",
        "auth.oidc.clientId": "daegam-ui",
    }
    cm = config_data(render(sets=oidc))
    assert cm["DAEGAM_OIDC_ISSUER"] == "https://idp.example.com"
    assert cm["DAEGAM_OIDC_AUDIENCE"] == "daegam-api"
    assert cm["DAEGAM_OIDC_INSECURE_HTTP"] == "false"
    # oidc 가 아닌 모드는 비어 있는 클라이언트를 적지 않는다
    assert "DAEGAM_OIDC_CLIENT_ID" not in config_data(render())


def test_the_ui_client_id_reaches_the_sign_in_and_the_recent_sign_in_check(bare_env):
    """UI 의 PKCE 클라이언트는 차트 값 하나다. UI 는 /auth/config 의 client_id 로 로그인하고,
    플랫폼은 최근 로그인의 ID 토큰이 그 클라이언트에게 났는지 본다(P53b 6.4절). 둘 다
    DAEGAM_OIDC_CLIENT_ID 를 읽는다."""
    sets = {
        "auth.mode": "oidc",
        "auth.oidc.issuer": "https://idp.example.com",
        "auth.oidc.clientId": "daegam-ui",
    }
    cm = config_data(render(sets=sets))
    assert cm["DAEGAM_OIDC_CLIENT_ID"] == "daegam-ui"
    for key, value in cm.items():
        bare_env.setenv(key, value)
    settings = settings_from_env()
    assert (settings.oidc_client_id, settings.ui_client_id) == ("daegam-ui", "daegam-ui")


def test_the_data_plane_issuer_and_the_long_bound_reach_the_configmap(tmp_path, bare_env):
    """두 값은 모든 모드에서 ConfigMap 키라 게이트가 ConfigMap 을 고쳐 바꾼다. 데이터 플레인
    발급자의 기본은 platform 이고 idp 는 되돌아갈 자리다. 이 키가 없던 옛 값으로 다시 렌더해도
    (`helm upgrade --reuse-values`) 템플릿의 기본값이 선다 (P53b 6.2절, 12절 7번)."""
    oidc = {
        "auth.mode": "oidc",
        "auth.oidc.issuer": "https://idp.example.com",
        "auth.oidc.clientId": "daegam-ui",
    }
    cm = config_data(render())
    assert (cm["DAEGAM_DATA_PLANE_ISSUER"], cm["DAEGAM_IDP_CONFIRM_SECONDS"]) == (
        "platform",
        "604800",
    )
    cm = config_data(render(sets=oidc))
    for key, value in cm.items():
        bare_env.setenv(key, value)
    assert settings_from_env().data_plane_trusts_platform
    sets = {**oidc, "auth.oidc.dataPlaneIssuer": "idp", "auth.oidc.confirmSeconds": 86400}
    cm = config_data(render(sets=sets))
    assert (cm["DAEGAM_DATA_PLANE_ISSUER"], cm["DAEGAM_IDP_CONFIRM_SECONDS"]) == ("idp", "86400")
    for key, value in cm.items():
        bare_env.setenv(key, value)
    settings = settings_from_env()
    assert settings.data_plane_trusts_idp and settings.idp_confirm_seconds == 86400
    old = {"auth": {"oidc": {"dataPlaneIssuer": None, "confirmSeconds": None}}}
    cm = config_data(render(values_file(tmp_path, old)))
    assert (cm["DAEGAM_DATA_PLANE_ISSUER"], cm["DAEGAM_IDP_CONFIRM_SECONDS"]) == (
        "platform",
        "604800",
    )
    assert "/auth/oidc/dataPlaneIssuer" in render_error(sets={"auth.oidc.dataPlaneIssuer": "both"})


@pytest.mark.parametrize("switch", ["DAEGAM_ALLOW_STUB_PROVIDER", "DAEGAM_SANDBOX_ALLOW_UNCHECKED"])
@pytest.mark.parametrize("value", ["true", "1", " Yes ", "on", "TRUE", True])
def test_development_switches_need_the_header_allow(tmp_path, switch, value):
    """개발용 스위치 둘은 릴리스 산출물에 켜지 않는다(D25, D95). header 를 허락한 개발 설치만
    ConfigMap 에 켠다. env 가 아니라 ConfigMap 키라 e2e 가 게이트 동안 고칠 수 있다."""
    on = {"platform": {"extraConfig": {switch: value}}}
    assert "/platform/extraConfig" in render_error(values_file(tmp_path, on))
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    cm = config_data(render(values_file(allowed, on | HEADER_ALLOWED)))
    want = str(value).lower() if isinstance(value, bool) else value
    assert cm[switch] == want


def test_a_switch_that_stays_off_needs_no_allow(tmp_path):
    off = {"platform": {"extraConfig": {"DAEGAM_ALLOW_STUB_PROVIDER": "false"}}}
    assert config_data(render(values_file(tmp_path, off)))["DAEGAM_ALLOW_STUB_PROVIDER"] == "false"


def test_extra_config_adds_keys_and_overrides_only_the_plain_defaults(tmp_path):
    extra = {"DAEGAM_TRANSCRIPT_RETENTION_DAYS": "7", "DAEGAM_TURN_TOKENS_ENABLED": True}
    cm = config_data(render(values_file(tmp_path, {"platform": {"extraConfig": extra}})))
    assert cm["DAEGAM_TRANSCRIPT_RETENTION_DAYS"] == "7"
    assert cm["DAEGAM_TURN_TOKENS_ENABLED"] == "true"


def test_scale_to_zero_settings_come_through_extra_config_only(tmp_path):
    """0 으로 줄이기의 설정 넷은 ConfigMap 으로만 받는다(P44 14절). 게이트가 ConfigMap 을 고쳐
    창을 바꾸므로 env 가 그것을 이기면 안 된다. 기본 키는 없어 플랫폼의 기본이 쓰인다."""
    extra = {"DAEGAM_AGENT_SLEEP_AFTER_SECONDS": "1800", "DAEGAM_AGENT_SCALE_TO_ZERO": False}
    cm = config_data(render(values_file(tmp_path, {"platform": {"extraConfig": extra}})))
    assert cm["DAEGAM_AGENT_SLEEP_AFTER_SECONDS"] == "1800"
    assert cm["DAEGAM_AGENT_SCALE_TO_ZERO"] == "false"
    assert "DAEGAM_AGENT_HOUSEKEEPING_HOURS" not in cm
    env = [{"name": "DAEGAM_AGENT_SLEEP_AFTER_SECONDS", "value": "0"}]
    refused = tmp_path / "env"
    refused.mkdir()
    assert "/platform/extraEnv/0/name" in render_error(
        values_file(refused, {"platform": {"extraEnv": env}})
    )


def test_extra_env_takes_names_the_chart_leaves_alone(tmp_path):
    env = [{"name": "HTTPS_PROXY", "value": "http://proxy.example.com:3128"}]
    docs = render(values_file(tmp_path, {"platform": {"extraEnv": env}}))
    [main] = pod_template(docs, "daegam-platform")["spec"]["containers"]
    assert main["env"][-1] == env[0]


# -- 차트가 정하는 이름의 목록 (P85 4절) -------------------------------------------------------


def _template_list(tmp_path: Path, name: str) -> set[str]:
    """시험 사본에서 이름 목록 정의 name 을 ConfigMap 하나로 꺼낸다."""
    chart = chart_copy(tmp_path, "")
    (chart / "templates" / "zz_list.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: list}\n"
        f'data: {{names: {{{{ include "{name}" . | quote }}}}}}\n'
    )
    [cm] = [d for d in render(chart=chart) if d["metadata"]["name"] == "list"]
    return set(json.loads(cm["data"]["names"]))


def test_the_templates_and_the_schema_share_one_list_of_names(tmp_path):
    assert _template_list(tmp_path / "reserved", "daegam.reservedKeys") == RESERVED
    assert _template_list(tmp_path / "config", "daegam.configOnlyKeys") == CONFIG_ONLY
    assert not RESERVED & CONFIG_ONLY


def test_every_configmap_key_the_values_can_write_is_reserved(tmp_path):
    """값을 모두 켠 렌더의 ConfigMap 키는 차트가 정하는 이름이거나 ConfigMap 으로만 받는 기본
    키이고, 목록의 ConfigMap 키는 모두 그 렌더에 나온다. 키를 더하고 목록을 빠뜨리면 extraEnv 가 그
    키를 덮을 수 있다."""
    everything = {
        "auth": {
            "bootstrapAdminGroup": "admins",
            "oidc": {
                "issuer": "https://idp.example.com",
                "audience": "daegam-api",
                "clientId": "daegam-ui",
            },
        },
        "ingress": {"className": "traefik", "tls": "edge"},
        "home": {
            "exposure": {"controllerNamespace": "traefik", "domain": "apps.example.com"},
            "networkPolicy": "enforced",
            "blockMetadata": True,
            "resources": {
                "requests": {"cpu": "50m", "memory": "64Mi"},
                "limits": {"cpu": "1", "memory": "1Gi"},
            },
            "limitRange": {"limits": [{"type": "Container", "default": {"cpu": "500m"}}]},
            "shippedRedis": {
                "hosts": ["redis"],
                "port": 6379,
                "tls_port": 6380,
                "ca_configmap": "ca",
                "plain": True,
            },
        },
        "images": {"pullSecret": "pull"},
    }
    keys = set(config_data(render(values_file(tmp_path, everything))))
    defaults = CONFIG_ONLY - NO_CHART_DEFAULT
    assert keys - RESERVED == defaults
    assert RESERVED - OUTSIDE_CONFIGMAP == keys - defaults
