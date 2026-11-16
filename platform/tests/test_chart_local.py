"""로컬 개발 클러스터의 차트 값과 make deploy-local 의 스크립트 (P85 4절).

값(deploy/dev/values-local.yaml)은 helm template 으로 렌더해 본다. scripts/deploy-local.sh 는
helm·kubectl·docker 를 흉내 내는 시험용 명령을 PATH 앞에 두고 돌려, 클러스터 없이 부르는 명령과
얹는 값을 본다. 흉내는 받은 인자를 기록하고, Deployment 의 metadata.generation 은 STUB_GEN 의
`이름=앞:뒤` 대로 업그레이드 앞과 뒤에 다른 값을 돌려준다.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from daegam.config import settings_from_env
from daegam.k8s.resources import validate_complete_resources
from tests.helpers.chart import (
    CHART,
    OFFLINE,
    chart_copy,
    config_data,
    containers,
    helm,
    live_cluster,
    named,
    pod_specs,
    render,
)
from tests.helpers.consts import LOCAL_VALUES, PLATFORM_NS, REPO

LOCAL = REPO / LOCAL_VALUES
VALUES = yaml.safe_load(LOCAL.read_text())
SCRIPT = REPO / "scripts" / "deploy-local.sh"
WORKLOADS = ("daegam-docparse", "daegam-platform", "daegam-ui")
DEV_TAG = re.compile(r"dev-[0-9a-f]{12}")

KUBECTL = r"""#!/usr/bin/env bash
echo "kubectl $*" >>"$STUB_DIR/log"
prev="" verb="" name="" kind=""
for a in "$@"; do
  case "$prev" in
    get) verb=get; kind="$a" ;;
    deploy | secret) [ "$verb" = get ] && [ -z "$name" ] && name="$a" ;;
  esac
  prev="$a"
done
case "$verb:$kind" in
  get:deploy)
    entry="$(tr ' ' '\n' <<<"${STUB_GEN:-}" | sed -n "s/^$name=//p")"
    [ -n "$entry" ] || entry=":1"
    before="${entry%%:*}" after="${entry#*:}"
    if [ -e "$STUB_DIR/gen-$name" ]; then printf '%s' "$after"; else printf '%s' "$before"; fi
    touch "$STUB_DIR/gen-$name"
    ;;
  get:secret) case " ${STUB_SECRETS:-} " in *" $name "*) ;; *) exit 1 ;; esac ;;
esac
"""
HELM = r"""#!/usr/bin/env bash
echo "helm $*" >>"$STUB_DIR/log"
prev=""
for a in "$@"; do
  case "$prev:${a##*/}" in
    -f:overlay.json | -f:gateway-mirror.json) cp "$a" "$STUB_DIR/${a##*/}" ;;
  esac
  [ "$a" != status ] || [ -n "${STUB_RELEASE:-}" ] || exit 1
  prev="$a"
done
"""
DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >>"$STUB_DIR/log"
if [ "$1 $2" = "image inspect" ]; then
  image="${*: -1}"
  case " ${STUB_IMAGES:-} " in *" $image "*) ;; *) exit 1 ;; esac
  python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "$image"
fi
"""


class Run:
    """deploy-local.sh 한 번의 끝 코드와 흉내 명령이 받은 인자, helm 이 받은 얹는 값과 게이트웨이
    사본의 값 파일(LLM_GATEWAY_IMAGES_FROM=mirror 일 때만)."""

    def __init__(self, out: subprocess.CompletedProcess[str], stub: Path):
        self.code, self.stderr = out.returncode, out.stderr
        log = stub / "log"
        self.calls = log.read_text().splitlines() if log.exists() else []
        overlay = stub / "overlay.json"
        self.overlay = json.loads(overlay.read_text()) if overlay.exists() else None
        self.overlay_path = overlay
        mirror = stub / "gateway-mirror.json"
        self.mirror_path = mirror if mirror.exists() else None

    def of(self, tool: str) -> list[str]:
        return [c.removeprefix(f"{tool} ") for c in self.calls if c.startswith(f"{tool} ")]


def _deploy_local(tmp_path: Path, command: str, **env: str) -> Run:
    stub = tmp_path / "stub"
    (stub / "bin").mkdir(parents=True)
    for name, body in (("kubectl", KUBECTL), ("helm", HELM), ("docker", DOCKER)):
        path = stub / "bin" / name
        path.write_text(body)
        path.chmod(0o755)
    base = {k: v for k, v in os.environ.items() if k in ("HOME", "PATH", "TMPDIR")}
    base["PATH"] = f"{stub / 'bin'}{os.pathsep}{base['PATH']}"
    out = subprocess.run(
        ["bash", str(SCRIPT), command],
        cwd=REPO,
        env=base | {"STUB_DIR": str(stub)} | env,
        capture_output=True,
        text=True,
        check=False,
    )
    return Run(out, stub)


BUILT = {"STUB_IMAGES": "daegam-platform:0.1.0 daegam-ui:0.1.0"}


# -- 값 ------------------------------------------------------------------------------------------


def test_the_local_values_render_a_header_mode_dev_install():
    docs = render(LOCAL)
    cm = config_data(docs)
    host = VALUES["host"]
    assert cm["DAEGAM_PUBLIC_URL"] == f"http://{host}"
    assert cm["DAEGAM_HOME_INGRESS_RESERVED_HOSTS"] == host
    assert [r["host"] for r in named(docs, "Ingress", "daegam-platform")["spec"]["rules"]] == [host]
    assert cm["DAEGAM_AUTH_MODE"] == "header"
    assert cm["DAEGAM_OIDC_ISSUER"] == "http://mock-oidc.daegam-system.svc:8080"
    # 개발용 스위치는 e2e 가 게이트 동안 고치는 ConfigMap 키다. env 는 envFrom 을 이긴다 (P85 4절)
    assert cm["DAEGAM_ALLOW_STUB_PROVIDER"] == cm["DAEGAM_SANDBOX_ALLOW_UNCHECKED"] == "true"
    for where, spec in pod_specs(docs):
        for c in containers(spec):
            names = {e["name"] for e in c.get("env", [])}
            assert not names & {"DAEGAM_ALLOW_STUB_PROVIDER", "DAEGAM_SANDBOX_ALLOW_UNCHECKED"}, (
                where
            )
            validate_complete_resources(c.get("resources"), where=f"{where}:{c['name']}")
    # 노드가 가진 로컬 이미지를 그대로 쓴다
    images = json.loads(cm["DAEGAM_HOME_IMAGES"])
    assert (images["registry_prefix"], images["pull_policy"]) == (
        "docker.io/library",
        "IfNotPresent",
    )
    [platform] = named(docs, "Deployment", "daegam-platform")["spec"]["template"]["spec"][
        "containers"
    ]
    assert platform["image"] == "docker.io/library/daegam-platform:0.1.0"
    assert platform["imagePullPolicy"] == "IfNotPresent"
    # http 라 UI 의 sticky 쿠키에 Secure 가 없다(D52)
    svc = named(docs, "Service", "daegam-ui")["metadata"]["annotations"]
    assert svc["traefik.ingress.kubernetes.io/service.sticky.cookie.secure"] == "false"


def test_the_mongodb_claim_keeps_the_size_of_earlier_local_installs():
    """클레임 템플릿은 바뀌지 않는다. 차트 이전 로컬 MongoDB 의 1Gi 와 기본 StorageClass
    그대로다."""
    [claim] = named(render(LOCAL), "StatefulSet", "mongodb")["spec"]["volumeClaimTemplates"]
    assert claim["spec"]["resources"]["requests"]["storage"] == "1Gi"
    assert "storageClassName" not in claim["spec"]


def test_the_local_values_generate_the_secrets_with_the_admin_the_e2e_signs_in_as(tmp_path):
    """values-local 은 Secret 셋을 차트가 만든다(P85 16절). local 로 바꾸는 e2e 는 admin 으로
    들어간다(scripts/lib/e2e_local_auth.sh)."""
    chart = chart_copy(tmp_path, live_cluster({}))
    generated = {f"secrets.{k}.existingSecret": "" for k in ("platform", "docparse", "mongo")}
    docs = render(LOCAL, chart=chart, sets=generated)
    secrets = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Secret"}
    assert set(secrets) == {"daegam-mongo-auth", "daegam-platform-secrets", "daegam-docparse-token"}
    platform = secrets["daegam-platform-secrets"]["data"]
    assert base64.b64decode(platform["DAEGAM_LOCAL_BOOTSTRAP_USERNAME"]).decode() == "admin"
    assert VALUES["auth"]["adminUsername"] == "admin"


def test_helm_lint_strict_passes_with_the_local_values():
    out = helm(
        "lint", "--strict", str(CHART), "-n", PLATFORM_NS, "-f", str(OFFLINE), "-f", str(LOCAL)
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "[INFO] Fail:" not in out.stdout + out.stderr


def test_the_local_host_is_read_from_the_values_file():
    """scripts/lib/hosts.sh 의 local_host 하나가 그 줄을 읽고 Makefile 의 UI_FULL_HOST 가 그것을
    부른다."""
    read = subprocess.run(
        ["bash", "-c", f'. "{REPO / "scripts/lib/hosts.sh"}"; local_host'],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    )
    assert read.stdout.strip() == VALUES["host"]
    makefile = (REPO / "Makefile").read_text().splitlines()
    [line] = [text for text in makefile if text.startswith("UI_FULL_HOST")]
    assert "local_host" in line


# -- make deploy-local ---------------------------------------------------------------------------


def test_deploy_local_installs_the_chart_with_this_checkouts_image_tags(tmp_path):
    run = _deploy_local(tmp_path, "deploy", **BUILT)
    assert run.code == 0, run.stderr
    [upgrade] = [c for c in run.of("helm") if " upgrade " in f" {c} "]
    assert upgrade.startswith(
        f"--kube-context orbstack upgrade --install daegam {CHART} -n {PLATFORM_NS} "
        f"--create-namespace -f {LOCAL} -f "
    ), upgrade
    tags = run.overlay["images"]
    assert DEV_TAG.fullmatch(tags["platform"]["tag"]) and DEV_TAG.fullmatch(tags["ui"]["tag"])
    assert f"tag daegam-ui:0.1.0 daegam-ui:{tags['ui']['tag']}" in run.of("docker")
    # 게이트웨이는 기본으로 꺼져 있고 PDB 가 없으며 차트 값의 원본 이미지다 (P38 12절, P85 8절)
    assert run.overlay["llmGateway"] == {
        "mode": "off",
        "namespace": "daegam-llm",
        "pdb": {"enabled": False},
    }
    assert "platform" not in run.overlay and run.mirror_path is None
    kubectl = run.of("kubectl")
    # 이번에 처음 만든 Deployment 는 다시 굴리지 않고 롤아웃을 기다린다
    assert not [c for c in kubectl if "rollout restart" in c]
    waited = [c.split()[-2] for c in kubectl if "rollout status" in c]
    assert waited == ["statefulset/mongodb", *(f"deploy/{d}" for d in WORKLOADS)]


def test_deploy_local_restarts_the_deployments_the_upgrade_left_alone(tmp_path):
    gens = "daegam-docparse=2:2 daegam-platform=3:3 daegam-ui=5:6"
    run = _deploy_local(tmp_path, "deploy", STUB_GEN=gens, **BUILT)
    assert run.code == 0, run.stderr
    restarted = [c.split()[-1] for c in run.of("kubectl") if "rollout restart" in c]
    assert restarted == ["deploy/daegam-docparse", "deploy/daegam-platform"]


def test_given_tags_need_no_local_image(tmp_path):
    """태그의 클러스터 e2e 잡은 노드에 넣은 판 태그를 준다. 로컬 도커를 묻지 않는다."""
    run = _deploy_local(
        tmp_path, "deploy", PLATFORM_TAG="0.1.0", UI_TAG="0.1.0", KUBE_CONTEXT="k3d-x"
    )
    assert run.code == 0, run.stderr
    assert run.of("docker") == []
    assert run.overlay["images"] == {"platform": {"tag": "0.1.0"}, "ui": {"tag": "0.1.0"}}
    assert all(c.startswith("--kube-context k3d-x ") for c in run.of("helm"))


def test_a_missing_ui_image_stops_before_the_upgrade(tmp_path):
    run = _deploy_local(tmp_path, "deploy", STUB_IMAGES="daegam-platform:0.1.0")
    assert run.code != 0 and "daegam-ui:0.1.0" in run.stderr
    assert not [c for c in run.of("helm") if " upgrade " in f" {c} "]


def test_make_variables_reach_the_values(tmp_path):
    run = _deploy_local(
        tmp_path,
        "deploy",
        LOCAL_HOST="other.test",
        INGRESS_CLASS="nginx",
        LLM_GATEWAY="agentgateway",
        LLM_GATEWAY_NS="gw-x",
        LLM_GATEWAY_PDB="true",
        **BUILT,
    )
    assert run.code == 0, run.stderr
    assert run.overlay["host"] == "other.test"
    assert run.overlay["ingress"] == {"className": "nginx"}
    assert run.overlay["llmGateway"] == {
        "mode": "agentgateway",
        "namespace": "gw-x",
        "pdb": {"enabled": True},
    }
    for n, env in enumerate(({"LLM_GATEWAY": "on"}, {"LLM_GATEWAY_PDB": "yes"})):
        bad = _deploy_local(tmp_path / f"bad-{n}", "deploy", **env, **BUILT)
        assert bad.code != 0 and not [c for c in bad.of("helm") if " upgrade " in f" {c} "]


def test_an_external_mongodb_uses_the_operators_secrets(tmp_path):
    secrets = "daegam-platform-secrets daegam-docparse-token"
    run = _deploy_local(tmp_path, "deploy", MONGO="external", STUB_SECRETS=secrets, **BUILT)
    assert run.code == 0, run.stderr
    assert run.overlay["mongodb"] == {"bundled": False}
    assert run.overlay["secrets"] == {
        "platform": {"existingSecret": "daegam-platform-secrets"},
        "docparse": {"existingSecret": "daegam-docparse-token"},
    }
    assert not [c for c in run.of("kubectl") if "statefulset/mongodb" in c]
    missing = _deploy_local(
        tmp_path / "missing",
        "deploy",
        MONGO="external",
        STUB_SECRETS="daegam-platform-secrets",
        **BUILT,
    )
    assert missing.code != 0 and "daegam-docparse-token" in missing.stderr
    assert not [c for c in missing.of("helm") if " upgrade " in f" {c} "]


@pytest.mark.parametrize("mode", ["off", "agentgateway"])
def test_the_rendered_local_configmap_starts_the_platform(tmp_path, bare_env, mode):
    """values-local 과 deploy-local 이 얹는 값으로 렌더한 ConfigMap 을 플랫폼 설정이 받는다. 켠
    게이트웨이는 이미지 넷이 digest 로 고정돼 있어야 기동한다 (P38 3절)."""
    run = _deploy_local(tmp_path, "deploy", LLM_GATEWAY=mode, **BUILT)
    assert run.code == 0, run.stderr
    overlay = tmp_path / "overlay.json"
    overlay.write_text(json.dumps(run.overlay))
    for key, value in config_data(render(LOCAL, overlay)).items():
        bare_env.setenv(key, value)
    settings = settings_from_env()
    assert settings.auth_mode == "header"
    assert settings.public_url == f"http://{VALUES['host']}"
    assert settings.allow_stub_provider and settings.sandbox_allow_unchecked
    # 로컬 배포와 k3d 의 게이트는 Agent 를 스스로 재우지 않는다 (P44 17절)
    assert settings.agent_sleep_after_seconds == 0
    assert settings.home_ingress["controller_namespace"] == "traefik"
    assert settings.home_ingress["allow_unauthenticated"] is True
    assert settings.llm_gateway == mode
    assert settings.llm_gateway_namespace == "daegam-llm"
    assert settings.llm_gateway_url == "http://llm-gateway.daegam-llm.svc.cluster.local:8080"
    assert settings.llm_gateway_images["gateway_image"].startswith("ghcr.io/agentgateway/")
    assert settings.llm_gateway_pdb is False


def test_mirrored_gateway_images_come_as_a_values_file(tmp_path, bare_env):
    """LLM_GATEWAY_IMAGES_FROM=mirror 는 설치 값 파일의 REGISTRY 아래 사본과 pull secret 을 쓰는
    값 파일을 하나 더 얹는다. 그 렌더도 플랫폼이 받는다 (P85 4절)."""
    install = tmp_path / "install.env"
    install.write_text("REGISTRY=registry.example.com/daegam\n")
    run = _deploy_local(
        tmp_path,
        "deploy",
        LLM_GATEWAY="agentgateway",
        LLM_GATEWAY_IMAGES_FROM="mirror",
        INSTALL_ENV_FILE=str(install),
        **BUILT,
    )
    assert run.code == 0, run.stderr
    [upgrade] = [c for c in run.of("helm") if " upgrade " in f" {c} "]
    assert upgrade.split(" -f ")[-1].split()[0].endswith("/gateway-mirror.json")
    assert run.mirror_path is not None
    overlay = tmp_path / "overlay.json"
    overlay.write_text(json.dumps(run.overlay))
    for key, value in config_data(render(LOCAL, overlay, run.mirror_path)).items():
        bare_env.setenv(key, value)
    images = settings_from_env().llm_gateway_images
    assert images["registry_prefix"] == "registry.example.com/daegam/third-party"
    assert images["pull_secret"] == "registry-pull"
    assert images["gateway_image"].startswith("agentgateway:v1.5.0@sha256:")
    bad = _deploy_local(tmp_path / "bad", "deploy", LLM_GATEWAY_IMAGES_FROM="copies", **BUILT)
    assert bad.code != 0 and not [c for c in bad.of("helm") if " upgrade " in f" {c} "]


# -- make deploy-ui-local, undeploy-local --------------------------------------------------------


def test_deploy_ui_local_moves_only_the_ui_tag_and_rolls_only_the_ui(tmp_path):
    gens = "daegam-platform=3:3 daegam-ui=5:5"
    run = _deploy_local(tmp_path, "ui", STUB_RELEASE="1", STUB_GEN=gens, **BUILT)
    assert run.code == 0, run.stderr
    [upgrade] = [c for c in run.of("helm") if " upgrade " in f" {c} "]
    tag = upgrade.rsplit("images.ui.tag=", 1)[1]
    assert DEV_TAG.fullmatch(tag)
    assert upgrade == (
        f"--kube-context orbstack upgrade daegam {CHART} -n {PLATFORM_NS} "
        f"--reset-then-reuse-values --set images.ui.tag={tag}"
    )
    kubectl = run.of("kubectl")
    assert [c.split()[-1] for c in kubectl if "rollout restart" in c] == ["deploy/daegam-ui"]
    assert not [c for c in kubectl if "daegam-platform" in c or "docparse" in c]


def test_deploy_ui_local_needs_the_release(tmp_path):
    run = _deploy_local(tmp_path, "ui", **BUILT)
    assert run.code != 0 and "make deploy-local" in run.stderr
    assert not [c for c in run.of("helm") if " upgrade " in f" {c} "]


def test_undeploy_local_removes_the_release_and_the_namespace(tmp_path):
    run = _deploy_local(tmp_path, "undeploy")
    assert run.code == 0, run.stderr
    assert run.of("helm") == [
        f"--kube-context orbstack uninstall daegam -n {PLATFORM_NS} "
        "--ignore-not-found --wait --timeout 3m"
    ]
    kubectl = run.of("kubectl")
    assert (
        f"--context orbstack delete namespace {PLATFORM_NS} --ignore-not-found --timeout=180s"
        in kubectl
    )
    # 차트 이전 배포의 클러스터 범위 객체도 지워 다음 설치가 넘겨받을 것이 없다
    assert any(
        "clusterrole/daegam-platform" in c and "priorityclass/daegam-sandbox" in c for c in kubectl
    )
