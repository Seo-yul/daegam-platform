"""키 기반 병합 (F7a). RFC 7386(JSON merge patch) 문법이되 정해진 경로의 리스트는 키 맵으로 다룬다.

CRD 는 strategic merge 가 없고 podTemplateSpec 은 스키마가 없어 SSA 조차 그 아래 리스트를
통째로 치환한다.
그래서 플랫폼이 `KEYED_LISTS` 경로의 리스트를 `name`(ports 는 `containerPort`) 키로 병합·diff 한다.

경로 표기: 세그먼트 튜플. 키 리스트의 원소는 `"name=mcp"` 처럼 `키=값` 세그먼트다.
문자열 표기는 `spec.podTemplateSpec.spec.containers[name=mcp].resources`,
점·슬래시가 든 맵 키는 `metadata.labels[daegam.devops.ai.kr/id]`.
"""

from __future__ import annotations

import copy
import fnmatch
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from daegam.k8s.client import FIELD_MANAGER
from daegam.k8s.labels import APP_MCPSERVER, LABEL_APP_NAME
from daegam.k8s.templates.common import FOREIGN_CONTAINER_SECURITY

Path = tuple[str, ...]

DELETE_MARK = "$delete"  # 키 리스트 원소 삭제: {"name": "x", "$delete": true}

# Deployment 의 파드 스펙 뿌리다
DEPLOYMENT_POD: Path = ("spec", "template", "spec")
# 파드 스펙의 뿌리. MCPServer·vMCP 는 spec.podTemplateSpec.spec, Agent 의 Deployment(D26)는
# spec.template.spec 이다. 키 리스트 규칙과 탈출구 잠금은 두 뿌리에 같게 붙는다
POD_ROOTS: tuple[Path, ...] = (("spec", "podTemplateSpec", "spec"), DEPLOYMENT_POD)
# 파드 스펙 안의 컨테이너 리스트 둘. 키 규칙·잠금·보안 값이 둘에 같게 붙는다
_CONTAINER_LISTS = ("containers", "initContainers")

# 파드 스펙 안의 키 리스트 (뿌리 아래 경로 → 키 필드)
_POD_KEYED: dict[Path, str] = {
    ("containers",): "name",
    ("initContainers",): "name",
    ("volumes",): "name",
    ("imagePullSecrets",): "name",
    **{
        (kind, "*", field): key
        for kind in _CONTAINER_LISTS
        for field, key in (("env", "name"), ("volumeMounts", "name"), ("ports", "containerPort"))
    },
}

# 패턴 세그먼트 → 키 필드. 패턴의 "*" 는 임의의 리스트 원소(키=값 세그먼트)와 맞는다.
KEYED_LISTS: dict[Path, str] = {
    ("spec", "env"): "name",
    ("spec", "volumes"): "name",
    **{(*root, *rest): key for root in POD_ROOTS for rest, key in _POD_KEYED.items()},
}

# Advanced 편집이 파드에 더하거나 고치는 볼륨이 쓸 수 있는 종류 (`check_volume_sources`). 셋 다
# 파드 자신의 것이거나 네임스페이스의 ConfigMap 이다. 노드·원격 저장소·Secret·SA 토큰·다른 PVC 로
# 가는 종류는 없다
ADDED_VOLUME_SOURCES: frozenset[str] = frozenset({"emptyDir", "configMap", "downwardAPI"})

# 잠긴 경로 (접두 일치, 세그먼트별 fnmatch). 건드리면 422 locked_field
# 파드에 노드 권한을 주는 자리. Advanced 편집으로는 열 수 없다. 이것은 거부 목록이라 새 필드가
# 생기면 그때마다 뚫린 채로 시작한다. 진짜 방어는 대상 네임스페이스의 PodSecurity admission 이고
# 이쪽은 그 앞에 둔 한 겹이다
_POD_ESCAPES: tuple[Path, ...] = (
    ("hostNetwork",),
    ("hostPID",),
    ("hostIPC",),
    ("hostUsers",),
    # 사이드카가 다른 컨테이너의 /proc/<pid>/environ 을 읽는 길이다. provider 키가 env 에 있다
    ("shareProcessNamespace",),
    # 파드 identity. 다른 SA 를 빌리거나 토큰을 마운트해 API 서버로 나가는 길이다
    ("serviceAccountName",),
    ("automountServiceAccountToken",),
    ("securityContext", "sysctls"),
    ("securityContext", "seccompProfile"),
    # AppArmor·SELinux 가둠. seccomp 를 걸어도 이 둘을 풀면(Unconfined, spc_t) 가둠 밖에서 돈다
    ("securityContext", "appArmorProfile"),
    ("securityContext", "seLinuxOptions"),
    ("securityContext", "runAsNonRoot"),
    # Windows 노드의 hostProcess 컨테이너. PodSecurity baseline 이 막는 나머지 한 자리다
    ("securityContext", "windowsOptions"),
    *(
        (kind, "*", "securityContext", field)
        for kind in _CONTAINER_LISTS
        for field in (
            "privileged",
            "allowPrivilegeEscalation",
            "capabilities",
            "procMount",
            "seccompProfile",
            "appArmorProfile",
            "seLinuxOptions",
            "runAsNonRoot",
            "windowsOptions",
        )
    ),
    # 노드 주소에 포트를 여는 자리다 (PodSecurity baseline)
    *((kind, "*", "ports", "*", "hostPort") for kind in _CONTAINER_LISTS),
    ("volumes", "*", "hostPath"),
    # 같은 네임스페이스의 아무 Secret 이나 SA 토큰을 파드에 끌어오는 볼륨
    ("volumes", "*", "secret"),
    ("volumes", "*", "projected"),
    # 볼륨과 /proc 을 막아도 env 참조가 남는다. 사이드카 하나에 다른 Secret 을 env 로 끌어오고
    # 그 컨테이너의 command 는 잠겨 있지 않으므로 그대로 밖으로 나간다. 테넌트 네임스페이스의
    # 비밀 중 값이 테넌트 경계를 넘는 것은 LLM provider 키다(D25). envFrom 은 configMapRef 까지
    # 통째로 막는다. 이름 키가 없는 리스트라 갈래를 가리기 어렵고, 잃는 것이 작다
    *(
        (kind, "*", *rest)
        for kind in _CONTAINER_LISTS
        for rest in (("env", "*", "valueFrom", "secretKeyRef"), ("envFrom",))
    ),
)


# 파드 템플릿 metadata 아래의 잠금. 옛 AppArmor 주석은 appArmorProfile 과 같은 일을 한다.
# `daegam.devops.ai.kr/*` 라벨·어노테이션은 플랫폼이 렌더한 메타데이터다. 라벨은
# Service·NetworkPolicy·파드 조회의 selector 이고, 어노테이션은 서비스 토큰의 판처럼 롤아웃을 모는
# 값이다
_TEMPLATE_ESCAPES: tuple[Path, ...] = (
    ("annotations", "container.apparmor.security.beta.kubernetes.io/*"),
    ("labels", "daegam.devops.ai.kr/*"),
    ("annotations", "daegam.devops.ai.kr/*"),
)


def pod_escapes(root: Path) -> tuple[Path, ...]:
    """파드 스펙의 자리가 CR 마다 다르다 (POD_ROOTS). 같은 목록을 그 뿌리에 붙여 쓰고, 템플릿
    metadata 의 잠금은 뿌리 옆의 metadata 에 붙인다."""
    template_metadata = (*root[:-1], "metadata")
    return tuple((*root, *rest) for rest in _POD_ESCAPES) + tuple(
        (*template_metadata, *rest) for rest in _TEMPLATE_ESCAPES
    )


# 플랫폼이 렌더하는 컨테이너 하나가 잠그는 자리. 그 컨테이너는 플랫폼이 정한 이미지·설정·비밀로
# 도는 플랫폼 소유 값이고, 여기 적힌 것이 그 컨테이너가 무엇을 실행하고 무엇을 읽는지 정한다.
# env 는 이름을 가리지 않고 필드 전체다(허용 목록 방식). 플랫폼이 쓰는 env 만 그 컨테이너에 있고,
# 사용자가 더한 컨테이너의 env 는 그대로 자유다. 프로브는 exec 만 잠근다(httpGet·tcpSocket·grpc 는
# 열려 있다)
_PLATFORM_CONTAINER_LOCKS: tuple[Path, ...] = (
    ("command",),
    ("env",),
    ("volumeMounts",),
    ("lifecycle",),
    *((probe, "exec") for probe in ("livenessProbe", "readinessProbe", "startupProbe")),
)


def platform_container(root: Path, name: str, *extra: str) -> tuple[Path, ...]:
    """파드 스펙 뿌리 `root` 의 컨테이너 `name` 에 붙는 잠금. extra 는 그 컨테이너에만 더 잠그는
    필드다(이미지·args 는 CR 마다 오퍼레이터가 덮는지가 달라 부르는 쪽이 정한다)."""
    at = (*root, "containers", f"name={name}")
    return tuple((*at, *rest) for rest in _PLATFORM_CONTAINER_LOCKS) + tuple(
        (*at, field) for field in extra
    )


# 모든 잠금 표의 머리. 객체의 정체(종류·이름·자리)와 플랫폼 라벨이다
_IDENTITY_LOCKS: tuple[Path, ...] = (
    ("apiVersion",),
    ("kind",),
    ("metadata", "name"),
    ("metadata", "namespace"),
    ("metadata", "labels", "daegam.devops.ai.kr/*"),
)

LOCKED_PATHS: tuple[Path, ...] = (
    *_IDENTITY_LOCKS,
    ("spec", "image"),
    ("spec", "mcpPort"),
    # vMCP 의 Entry·Agent 의 도구 소스·내부 OIDC 의 resourceUrl 이 이 포트로 프록시를 부른다
    ("spec", "proxyPort"),
    ("spec", "transport"),
    ("spec", "groupRef"),
    # 내부 홉 인증 (D41). oidcConfigRef 를 풀면 무인증이 되고, authzConfig·authzConfigRef 는
    # 플랫폼이 테넌트 울타리를 싣는 자리라(P50 11절) 고치면 울타리가 풀리며, trustProxyHeaders 는
    # X-Forwarded 를 믿는 자리를 만든다
    ("spec", "oidcConfigRef"),
    ("spec", "authzConfig"),
    ("spec", "trustProxyHeaders"),
    # 같은 인증 배선의 나머지 자리. 울타리 설정을 이름으로 가리키거나, 나가는 요청에 자격을
    # 붙이거나, 인증 서버를 세우거나, 요청을 webhook 으로 넘긴다. 가리키는 객체는 네임스페이스의
    # 것이다
    ("spec", "authzConfigRef"),
    ("spec", "externalAuthConfigRef"),
    ("spec", "authServerRef"),
    ("spec", "webhookConfigRef"),
    # 감사 이벤트는 프록시 stdout 으로 나가 플랫폼이 모은다 (P6 F5). 끄면 이 서버의 기록이 끊긴다
    ("spec", "audit"),
    # 네임스페이스의 다른 객체를 이름으로 끌어와 이 서버에 붙이는 자리. toolConfigRef 가 가리키는
    # MCPToolConfig 는 허브 이미지 항목의 도구 거르기로 플랫폼이 만든다(`render_tool_config`).
    # permissionProfile 은 쿠버네티스 런타임이 쓰지 않고 configmap 종류일 때 ConfigMap 을 프록시에
    # 붙이는 일만 한다
    ("spec", "telemetryConfigRef"),
    ("spec", "toolConfigRef"),
    ("spec", "permissionProfile"),
    # Secret 을 파드로 끌어오는 자리. secrets 와 sessionStorage 는 플랫폼이 백엔드 자격증명·바인딩
    # 비밀번호·Redis 를 렌더하는 자리라 통째로 잠가 항목을 더하지 못하게 한다. secrets 는 키
    # 리스트가 아니다. 0.52 의 list-map 키는 (name, key)라 한 Secret 의 두 항목을 name 으로 가르지
    # 못하므로 통째로 견준다
    ("spec", "secrets"),
    ("spec", "sessionStorage"),
    # ServiceAccount 와 호스트 경로를 참조하는 자리. serviceAccount 는 백엔드 파드의 SA 를 정하고,
    # spec.volumes 는 ToolHive 가 hostPath 볼륨으로 만들어 프록시 컨테이너에 붙인다
    ("spec", "serviceAccount"),
    ("spec", "volumes"),
    # 플랫폼이 렌더한 프록시 Deployment·Service·파드의 메타데이터와 프록시 컨테이너의 env 를 덮는
    # 자리. 스케줄링(nodeSelector·tolerations·affinity)과 pull secret 은 파드 스펙의 같은 자리처럼
    # 열어 둔다
    ("spec", "resourceOverrides", "proxyService"),
    *(
        ("spec", "resourceOverrides", "proxyDeployment", field)
        for field in ("annotations", "labels", "podTemplateMetadataOverrides", "env")
    ),
    # 백엔드 컨테이너의 env 는 플랫폼이 쓴다. CR 의 spec.env 와 파드 쪽 env 가 같은 자리이고,
    # 런타임은 그 env 를 profile.json 보다 먼저 읽는다(mcp-runtime `BridgeOptions.from_env`)
    ("spec", "env"),
    ("spec", "podTemplateSpec", "spec", "initContainers", "name=fetch-spec"),
    # mcp 컨테이너의 image·args 는 proxyrunner 가 덮어써 아래 IGNORED 로 알린다
    *platform_container(("spec", "podTemplateSpec", "spec"), "mcp"),
    # 스펙 볼륨은 어느 컨테이너도 마운트하지 못한다. 사이드카가 스펙을 바꿔 쓰면 도구가 바뀐다
    ("spec", "podTemplateSpec", "spec", "containers", "*", "volumeMounts", "name=spec"),
    ("spec", "podTemplateSpec", "spec", "initContainers", "*", "volumeMounts", "name=spec"),
    ("spec", "podTemplateSpec", "spec", "volumes", "name=spec"),
    *pod_escapes(("spec", "podTemplateSpec", "spec")),
)

# proxyrunner 가 무조건 덮어쓰므로 바꿔도 조용히 무시되는 경로. 422 ignored_by_operator
IGNORED_BY_OPERATOR: tuple[Path, ...] = (
    ("spec", "podTemplateSpec", "spec", "containers", "name=mcp", "image"),
    ("spec", "podTemplateSpec", "spec", "containers", "name=mcp", "args"),
    ("spec", "podTemplateSpec", "spec", "containers", "name=mcp", "stdin"),
    ("spec", "podTemplateSpec", "spec", "containers", "name=mcp", "tty"),
)

# 드리프트 비교에서 빼는 것 (서버가 채우거나 바꾸는 필드)
VOLATILE_METADATA = (
    "managedFields",
    "resourceVersion",
    "generation",
    "uid",
    "creationTimestamp",
    "selfLink",
    "deletionTimestamp",
    "deletionGracePeriodSeconds",
    "finalizers",
)


class MergeError(Exception):
    def __init__(self, code: str, path: Path, detail: str | None = None):
        self.code = code
        self.path = path
        self.detail = detail or f"{code}: {format_path(path)}"
        super().__init__(self.detail)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "path": format_path(self.path)}


# -- 경로 --------------------------------------------------------------------


def _is_selector(seg: str) -> bool:
    return "=" in seg


def _pattern_matches(pattern: Path, path: Path) -> bool:
    """pattern 이 path 의 접두인가.
    세그먼트는 fnmatch, pattern 의 '*' 는 셀렉터 세그먼트와 맞는다."""
    if len(pattern) > len(path):
        return False
    for p, s in zip(pattern, path, strict=False):
        if p == "*":
            if not _is_selector(s):
                return False
            continue
        if not fnmatch.fnmatchcase(s, p):
            return False
    return True


def _list_key(path: Path) -> str | None:
    """path 위치의 값이 키 리스트면 키 필드 이름."""
    for pattern, key in KEYED_LISTS.items():
        if len(pattern) == len(path) and _pattern_matches(pattern, path):
            return key
    return None


def format_path(path: Iterable[str]) -> str:
    out = ""
    for seg in path:
        if _is_selector(seg) or "." in seg or "/" in seg:
            out += f"[{seg}]"
        else:
            out += f".{seg}" if out else seg
    return out


def _selector(key: str, elem: Any) -> str | None:
    if isinstance(elem, dict) and key in elem:
        return f"{key}={elem[key]}"
    return None


# -- 병합 --------------------------------------------------------------------


def merge(base: Any, patch: Any, path: Path = (), *, keep_null: bool = False) -> Any:
    """base 에 patch 를 얹는다 (RFC 7386 + 키 리스트). base 를 바꾸지 않는다.
    keep_null=True 면 null 을 지우지 않고 그대로 둔다 (patch 끼리 합칠 때)."""
    if isinstance(patch, dict):
        if not isinstance(base, dict):
            base = {}
        out = copy.deepcopy(base)
        for k, v in patch.items():
            if v is None and not keep_null:
                out.pop(k, None)
            else:
                out[k] = merge(base.get(k), v, (*path, k), keep_null=keep_null)
        return out
    if isinstance(patch, list):
        key = _list_key(path)
        if key is None:
            return copy.deepcopy(patch)
        return _merge_keyed(base if isinstance(base, list) else [], patch, key, path, keep_null)
    return copy.deepcopy(patch)


def _merge_keyed(base: list, patch: list, key: str, path: Path, keep_null: bool) -> list:
    out: list = [copy.deepcopy(e) for e in base]
    index = {_selector(key, e): i for i, e in enumerate(out) if _selector(key, e)}
    for elem in patch:
        sel = _selector(key, elem)
        if sel is None:
            out.append(copy.deepcopy(elem))
            continue
        if isinstance(elem, dict) and elem.get(DELETE_MARK):
            if sel in index:
                out.pop(index[sel])
                index = {_selector(key, e): i for i, e in enumerate(out) if _selector(key, e)}
            if keep_null:
                # patch 합성: base 에 그 원소가 있을 수 있으니 삭제 마커는 남긴다
                out.append(copy.deepcopy(elem))
                index[sel] = len(out) - 1
            continue
        if sel in index:
            out[index[sel]] = merge(out[index[sel]], elem, (*path, sel), keep_null=keep_null)
        else:
            out.append(merge({}, elem, (*path, sel), keep_null=keep_null))
            index[sel] = len(out) - 1
    return out


def compose(p1: dict[str, Any], p2: dict[str, Any]) -> dict[str, Any]:
    """patch 두 개를 하나로 (p2 가 나중). null 과 $delete 를 보존한다."""
    return merge(p1, p2, keep_null=True)


# -- diff --------------------------------------------------------------------


def diff(base: Any, target: Any, path: Path = ()) -> Any:
    """base 에서 target 으로 가는 patch.
    같아도 None 이나 특수값을 돌려주지 않는다. 최상위는 언제나 dict 이고
    변화가 없으면 {} 다."""
    if isinstance(base, dict) and isinstance(target, dict):
        out: dict[str, Any] = {}
        for k in base:
            if k not in target:
                out[k] = None
        for k, v in target.items():
            if k not in base:
                out[k] = copy.deepcopy(v)
                continue
            sub = _diff_value(base[k], v, (*path, k))
            if sub is not _NO_CHANGE:
                out[k] = sub
        return out
    return _diff_value(base, target, path)


class _NoChange:
    pass


_NO_CHANGE = _NoChange()


def _diff_value(base: Any, target: Any, path: Path) -> Any:
    if isinstance(base, dict) and isinstance(target, dict):
        d = diff(base, target, path)
        return d if d else _NO_CHANGE
    if isinstance(base, list) and isinstance(target, list):
        key = _list_key(path)
        if key is None:
            return copy.deepcopy(target) if base != target else _NO_CHANGE
        return _diff_keyed(base, target, key, path)
    if base == target:
        return _NO_CHANGE
    return copy.deepcopy(target)


def _diff_keyed(base: list, target: list, key: str, path: Path) -> Any:
    b_index = {_selector(key, e): e for e in base if _selector(key, e)}
    t_index = {_selector(key, e): e for e in target if _selector(key, e)}
    out: list = []
    for sel, elem in b_index.items():
        if sel not in t_index:
            out.append({key: elem[key], DELETE_MARK: True})
    for sel, elem in t_index.items():
        if sel not in b_index:
            out.append(copy.deepcopy(elem))
            continue
        sub = diff(b_index[sel], elem, (*path, sel))
        if sub:
            out.append({key: elem[key], **sub})
    # 키 없는 원소는 통째로 비교
    b_rest = [e for e in base if not _selector(key, e)]
    t_rest = [e for e in target if not _selector(key, e)]
    if b_rest != t_rest:
        out.extend(copy.deepcopy(t_rest))
    return out if out else _NO_CHANGE


# -- 경로 열거·검사 --------------------------------------------------------------


def key_paths(patch: Any, path: Path = ()) -> list[Path]:
    """patch 가 건드리는 잎 경로 목록 (키 리스트 원소는 셀렉터 세그먼트로)."""
    out: list[Path] = []
    if isinstance(patch, dict):
        if not patch:
            return [path] if path else []
        for k, v in patch.items():
            if k == DELETE_MARK:
                continue
            out.extend(key_paths(v, (*path, k)))
        return out
    if isinstance(patch, list):
        key = _list_key(path)
        if key is None:
            return [path]
        for elem in patch:
            sel = _selector(key, elem)
            if sel is None:
                out.append(path)
                continue
            if isinstance(elem, dict) and elem.get(DELETE_MARK):
                out.append((*path, sel))
                continue
            rest = {k: v for k, v in elem.items() if k != key}
            out.extend(key_paths(rest, (*path, sel)) if rest else [(*path, sel)])
        return out
    return [path]


def matches_any(path: Path, patterns: Iterable[Path]) -> Path | None:
    for pattern in patterns:
        if _pattern_matches(pattern, path):
            return pattern
    return None


def wiping_paths(patch: Any, path: Path = ()) -> list[Path]:
    """그 자리의 하위 전체를 없애거나 갈아 치우는 경로.

    `merge` 는 값이 None 이면 키를 통째로 지우고, 값이 dict·list 가 아니면 그 자리를 스칼라로
    덮는다. 키 없는 리스트는 그 자리를 통째로 갈아 치우고, 키 리스트 자리에 온 dict 도 리스트를
    통째로 갈아 치운다. 넷 다 아래에 있던 것이 사라진다. 렌더에서 dict 이던 자리에 리스트를 두면
    그 아래의 잠긴 값이 함께 사라지므로 타입을 바꾸는 값도 여기에 든다. `key_paths` 는 잎 경로만
    주고 그것이 삭제인지 아닌지는 잃어버리므로 따로 모은다."""
    out: list[Path] = []
    if isinstance(patch, dict):
        if _list_key(path) is not None:
            return [path]
        for k, v in patch.items():
            if k == DELETE_MARK:
                continue
            out.extend(wiping_paths(v, (*path, k)))
        return out
    if isinstance(patch, list):
        key = _list_key(path)
        if key is None:
            return [path] if path else []
        for elem in patch:
            sel = _selector(key, elem)
            if sel is None or not isinstance(elem, dict):
                continue
            if elem.get(DELETE_MARK):
                # 원소 삭제는 그 원소 아래 전체가 사라지는 것이다. containers[name=agent] 를 지우면
                # 잠긴 image·env 가 함께 사라진다
                out.append((*path, sel))
                continue
            rest = {k: v for k, v in elem.items() if k != key}
            out.extend(wiping_paths(rest, (*path, sel)))
        return out
    return [path] if path else []


def covers_any(path: Path, patterns: Iterable[Path]) -> Path | None:
    """path 아래에 잠긴 경로가 있는가. `matches_any` 와 방향이 반대다.

    패턴의 접두를 path 에 대어 본다. 인자 순서가 중요하다. `_pattern_matches` 의 첫 인자만 `*` 와
    fnmatch 를 해석하므로 뒤집어 넣으면 `containers[*]` 같은 와일드카드 잠금이 하나도 걸리지
    않는다."""
    for pattern in patterns:
        if len(path) < len(pattern) and _pattern_matches(pattern[: len(path)], path):
            return pattern
    return None


def value_paths(obj: Any, path: Path = ()) -> Iterator[Path]:
    """obj 에 값이 있는 모든 자리(뿌리는 빼고). 키 리스트의 원소는 셀렉터 세그먼트다. 키 없는
    리스트 안으로는 들어가지 않는다. 잠긴 경로는 dict 와 키 리스트만 지나기 때문이다."""
    if isinstance(obj, dict):
        children = [((*path, k), v) for k, v in obj.items()]
    elif isinstance(obj, list) and (key := _list_key(path)) is not None:
        children = [((*path, s), e) for e in obj if (s := _selector(key, e)) is not None]
    else:
        return
    for at, value in children:
        yield at
        yield from value_paths(value, at)


def rendered_locks(rendered: Any, patterns: Iterable[Path]) -> dict[Path, Path]:
    """부모 경로마다 그 부모를 지우면 함께 사라지는 렌더의 잠긴 값 하나를 둔 표. `covers_any` 의
    렌더판이다.

    잠금에는 렌더한 값을 지키는 것과 새로 적지 못하게 막는 것(exec 프로브, hostPort 같은 자리)이
    섞여 있다. 부모를 지우면 사라지는 것은 렌더에 있던 값뿐이므로, 그 아래 렌더에 잠긴 값이 없으면
    지워도 플랫폼 소유 값을 잃지 않는다. 부모 자신이 잠긴 경우는 잎 검사(`matches_any`)의 몫이라
    `covers_any` 처럼 부모보다 긴 잠금만 센다. 렌더를 한 번 훑어 표로 만들므로 지우는 경로가
    몇 개든 경로마다 조회 한 번이다."""
    longest_first = sorted(set(patterns), key=len, reverse=True)
    out: dict[Path, Path] = {}
    for at in value_paths(rendered):
        longest = next((len(p) for p in longest_first if _pattern_matches(p, at)), 0)
        for n in range(1, longest):
            out.setdefault(at[:n], at)
    return out


# vMCP 의 호출 타임아웃 자리. Advanced 편집이 잠근다
VMCP_TIMEOUTS_PATH: Path = ("spec", "config", "operational", "timeouts")
# VirtualMCPServer 용 잠금 (F7a 구조 재사용, P4)
VMCP_LOCKED_PATHS: tuple[Path, ...] = (
    *_IDENTITY_LOCKS,
    ("spec", "groupRef"),
    # 들어오는 인증 (P50 8절). Cedar 는 Gateway 문서(authz_policy, 표면)가 정하고 MCPAuthzConfig 로
    # 참조한다. 노출하지 않은 Gateway 는 oidcConfigRef 의 resourceUrl·scopes 만 고칠 수 있다
    ("spec", "incomingAuth", "type"),
    ("spec", "incomingAuth", "authzConfig"),
    ("spec", "incomingAuth", "authzConfigRef"),
    ("spec", "incomingAuth", "oidcConfigRef", "name"),
    ("spec", "incomingAuth", "oidcConfigRef", "audience"),
    # 토큰이 어디로 나가는지 정하는 자리다 (D41). default 에 토큰을 두면 사용자 주소로 나간다
    ("spec", "outgoingAuth"),
    # 들어온 요청의 헤더를 모든 백엔드로 넘기는 허용 목록. 나가는 인증과 같은 인증 배선이다
    ("spec", "passthroughHeaders"),
    ("spec", "config", "passthroughHeaders"),
    # 내장 인증 서버. 서명 키와 상류 IdP 의 client secret 을 Secret 으로 참조하는 인증 배선이다
    ("spec", "authServerConfig"),
    # 노출은 플랫폼이 Ingress 와 인증을 함께 거는 일이다(D22). 서비스 종류는 플랫폼 값이다
    ("spec", "serviceType"),
    ("spec", "serviceAccount"),
    ("spec", "sessionStorage"),
    ("spec", "telemetryConfigRef"),
    # spec 의 인증·세션 저장소·텔레메트리와 같은 일을 하는 config 쪽 자리. 오퍼레이터가 spec 쪽
    # 값으로 덮는다고 적혀 있어도 그 변환에 기대지 않는다
    ("spec", "config", "incomingAuth"),
    ("spec", "config", "outgoingAuth"),
    ("spec", "config", "sessionStorage"),
    ("spec", "config", "telemetry"),
    # 감사 이벤트는 vMCP 파드 stdout 으로 나가 플랫폼이 모은다(`services/audit_collector.py` 의
    # PROXY_KINDS). 종류를 걸러 내거나 끄면 그 Gateway 의 기록이 끊긴다
    ("spec", "config", "audit"),
    # 백엔드마다의 호출 타임아웃은 Gateway 정의의 백엔드가 정한다 (P50 7절)
    VMCP_TIMEOUTS_PATH,
    # 백엔드 집합과 도구 표면은 Gateway 정의가 정하는 플랫폼 값이다. composite 도구·optimizer·
    # code mode 는 그 표면을 바꾸고, embeddingServerRef 는 네임스페이스의 다른 객체를 참조한다
    ("spec", "config", "groupRef"),
    ("spec", "config", "backends"),
    ("spec", "config", "aggregation"),
    ("spec", "config", "compositeTools"),
    ("spec", "config", "compositeToolRefs"),
    ("spec", "config", "optimizer"),
    ("spec", "config", "codeMode"),
    ("spec", "embeddingServerRef"),
    # vmcp 는 플랫폼 Secret 을 env(secretKeyRef)로 받는 플랫폼 컨테이너다. podTemplateSpec 이
    # strategic merge 로 병합되므로 이미지·args 까지 잠근다(오퍼레이터가 덮지 않는다)
    *platform_container(("spec", "podTemplateSpec", "spec"), "vmcp", "image", "args"),
    *pod_escapes(("spec", "podTemplateSpec", "spec")),
)


# 노출된 vMCP: incomingAuth 의 oidcConfigRef(resourceUrl)·authzConfig·authzConfigRef 도 잠긴다. 풀면
# 외부에 무인증으로 열리기 때문이다 (docs/guide/exposure.md 2절)
VMCP_EXPOSED_LOCKED_PATHS: tuple[Path, ...] = VMCP_LOCKED_PATHS + (
    ("spec", "incomingAuth", "oidcConfigRef"),
)
# 노출 앞단 MCPRemoteProxy expose-<k8s.name>. overrides 대상은 아니지만 문서 묶음에 잠김을 표시한다
EXPOSE_PROXY_LOCKED_PATHS: tuple[Path, ...] = (
    *_IDENTITY_LOCKS,
    ("spec", "remoteUrl"),
    ("spec", "oidcConfigRef"),
    ("spec", "trustProxyHeaders"),
    ("spec", "authzConfig"),
    ("spec", "authzConfigRef"),
    # 안쪽에 내는 서비스 토큰과 헤더 주입 자리 (D41)
    ("spec", "externalAuthConfigRef"),
    ("spec", "headerForward"),
    # 역할 없는 ServiceAccount (P50 10절)
    ("spec", "serviceAccount"),
)


def vmcp_locked_paths(exposed: bool) -> tuple[Path, ...]:
    """vMCP 의 잠긴 경로. 노출했으면 oidcConfigRef 전체까지다."""
    return VMCP_EXPOSED_LOCKED_PATHS if exposed else VMCP_LOCKED_PATHS


# Agent Instance 의 Deployment 용 잠금 (D26). 플랫폼이 정하는 것(이름·셀렉터·라벨·설정
# initContainer·설정 볼륨·롤아웃 멈춤, agent 컨테이너는 `platform_container` 규칙과 image·args)은
# Advanced 에서 못 바꾼다. 자원·스케줄링·httpGet 프로브·사이드카 같은 나머지는 자유다. 오퍼레이터가
# 덮어쓰는 경로는 없다
AGENT_LOCKED_PATHS: tuple[Path, ...] = (
    *_IDENTITY_LOCKS,
    ("spec", "selector"),
    ("spec", "template", "spec", "initContainers", "name=fetch-agent-config"),
    # agent 컨테이너는 플랫폼이 정한 런타임 이미지와 설정·비밀 env 로 도는 플랫폼 소유 값이다
    *platform_container(("spec", "template", "spec"), "agent", "image", "args"),
    # 롤아웃을 멈추면 서비스 토큰 회전·설정 변경이 파드에 닿지 않는다
    ("spec", "paused"),
    # 설정 볼륨은 어느 컨테이너도 마운트하지 못한다. 사이드카가 마운트하면 설정을 읽거나 바꾼다
    ("spec", "template", "spec", "containers", "*", "volumeMounts", "name=config"),
    ("spec", "template", "spec", "initContainers", "*", "volumeMounts", "name=config"),
    ("spec", "template", "spec", "volumes", "name=config"),
    *pod_escapes(("spec", "template", "spec")),
)


def check_overrides(
    patch: dict[str, Any],
    *,
    locked: Iterable[Path] = LOCKED_PATHS,
    ignored: Iterable[Path] = IGNORED_BY_OPERATOR,
    rendered: dict[str, Any] | None = None,
    refuse_proxy_label: bool = True,
) -> None:
    """잠긴 경로·오퍼레이터가 무시하는 경로를 건드리거나 허용하지 않는 종류의 볼륨을 적으면
    MergeError. refuse_proxy_label 이면 파드 템플릿의 프록시 라벨 값도 본다(`check_proxy_label`).
    저장된 overrides 를 다시 볼 때는 끈다. 그 값은 렌더에서 뺀다.

    rendered 는 patch 를 얹을 렌더다. 주면 부모를 지우는 patch 는 그 렌더에서 부모 아래에 잠긴 값이
    실제로 있을 때만 거부한다(`rendered_locks`). 없으면 잠긴 경로가 놓일 수 있는 부모를 모두
    거부한다(`covers_any`). 키 리스트 원소의 키 값이 문자열·정수가 아니면 invalid_manifest 다."""
    locked = tuple(locked)
    ignored = tuple(ignored)
    check_list_keys(patch)
    # 지우는 쪽을 먼저 본다. 잠긴 경로의 부모를 null 로 두면 아래가 통째로 사라지는데, 잎 경로만
    # 보는 검사로는 잠긴 것보다 얕은 경로가 걸리지 않는다 (`_pattern_matches` 가 길이에서 끊는다)
    wipes = wiping_paths(patch)
    locks = rendered_locks(rendered, locked) if wipes and rendered is not None else {}
    for p in wipes:
        under = covers_any(p, locked) if rendered is None else locks.get(p)
        if under is not None:
            raise MergeError(
                "locked_field",
                p,
                f"{format_path(p)} would remove {format_path(under)}, "
                "which is managed by the platform",
            )
    for p in key_paths(patch):
        if matches_any(p, locked):
            raise MergeError("locked_field", p, f"{format_path(p)} is managed by the platform")
        if matches_any(p, ignored):
            raise MergeError(
                "ignored_by_operator",
                p,
                f"{format_path(p)} is overwritten by the ToolHive proxyrunner and would be ignored",
            )
    check_volume_sources(patch)
    if refuse_proxy_label:
        check_proxy_label(patch)


def _template_labels(doc: Any, root: Path) -> dict[str, Any] | None:
    """파드 스펙 뿌리 root 옆의 템플릿 metadata.labels. 없으면 None 이다."""
    node: Any = doc
    for seg in (*root[:-1], "metadata", "labels"):
        node = node.get(seg) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else None


def check_proxy_label(patch: dict[str, Any]) -> None:
    """patch 가 파드 템플릿의 `app.kubernetes.io/name` 을 `mcpserver` 로 두면 MergeError
    (locked_field, T67). MCP 백엔드 정책이 그 값의 파드를 MCPServer 프록시로 받으므로, 사이드카를 단
    Agent 파드가 그 값으로 백엔드에 닿을 수 있다. 다른 값은 받는다. 저장된 overrides 에 이미 그 값이
    있으면 거부하지 않고 렌더에서 뺀다(`strip_proxy_label`)."""
    for root in POD_ROOTS:
        labels = _template_labels(patch, root)
        if labels is not None and labels.get(LABEL_APP_NAME) == APP_MCPSERVER:
            at: Path = (*root[:-1], "metadata", "labels", LABEL_APP_NAME)
            raise MergeError(
                "locked_field",
                at,
                f"{format_path(at)}: {APP_MCPSERVER} marks the ToolHive MCPServer proxy, "
                "which MCP backends admit",
            )


def strip_proxy_label(rendered: dict[str, Any], final: dict[str, Any]) -> None:
    """overrides 가 파드 템플릿에 단 `app.kubernetes.io/name: mcpserver` 를 최종본에서 뺀다(제자리
    수정). 렌더가 단 값이면 둔다. 이 값을 거부하기 전에 저장된 overrides 가 다음 재적용에서 낫는다
    (`check_proxy_label`)."""
    for root in POD_ROOTS:
        labels = _template_labels(final, root)
        if labels is None or labels.get(LABEL_APP_NAME) != APP_MCPSERVER:
            continue
        if (_template_labels(rendered, root) or {}).get(LABEL_APP_NAME) != APP_MCPSERVER:
            del labels[LABEL_APP_NAME]


def check_list_keys(patch: Any, path: Path = ()) -> None:
    """patch 의 키 리스트 원소가 가진 키 값이 문자열·정수가 아니면 MergeError(invalid_manifest).

    키 값은 원소를 가리키는 셀렉터(`name=mcp`)가 되고 병합이 그 값으로 원소를 찾는다. 리스트나
    dict 는 셀렉터로 쓸 수 없고 쿠버네티스도 받지 않는다."""
    if isinstance(patch, dict):
        for k, v in patch.items():
            check_list_keys(v, (*path, k))
        return
    key = _list_key(path) if isinstance(patch, list) else None
    if key is None:
        return
    for elem in patch:
        if not isinstance(elem, dict) or key not in elem:
            continue
        value = elem[key]
        if isinstance(value, bool) or not isinstance(value, str | int):
            raise MergeError(
                "invalid_manifest",
                path,
                f"{format_path(path)}: {key} must be a string or an integer",
            )
        check_list_keys({k: v for k, v in elem.items() if k != key}, (*path, f"{key}={value}"))


def check_volume_sources(patch: dict[str, Any]) -> None:
    """patch 가 파드 스펙의 볼륨에 적은 종류가 `ADDED_VOLUME_SOURCES` 밖이면 MergeError.

    볼륨 종류는 거부 목록으로 다 셀 수 없다(원격 저장소·드라이버·CSI·ephemeral 이 계속 는다).
    그래서 볼륨만은 허용 목록이다. 렌더가 만든 볼륨은 patch 가 건드리지 않으면 보지 않는다."""
    for root in POD_ROOTS:
        node: Any = patch
        for seg in (*root, "volumes"):
            node = node.get(seg) if isinstance(node, dict) else None
        for volume in _dicts(node):
            if volume.get(DELETE_MARK):
                continue
            sel = _selector("name", volume)
            at: Path = (*root, "volumes", sel) if sel else (*root, "volumes")
            for source in volume:
                if source != "name" and source not in ADDED_VOLUME_SOURCES:
                    raise MergeError(
                        "locked_field",
                        (*at, source),
                        f"{format_path((*at, source))}: Advanced edit takes only "
                        f"{', '.join(sorted(ADDED_VOLUME_SOURCES))} volumes",
                    )


def apply_overrides(rendered: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    """렌더한 CR 에 overrides 를 얹는다. overrides 가 더한 컨테이너(사이드카·init)에는 외부 이미지와
    같은 보안 값(`FOREIGN_CONTAINER_SECURITY`)을 건다. 그 세 값은 잠금 표(`_POD_ESCAPES`) 때문에
    사용자가 쓰지 못하므로 덮어도 사용자가 쓴 값을 잃지 않는다. 런타임이 기본으로 주던
    capabilities 는 사이드카가 잃는다(resource-model.md 13절)."""
    if not overrides:
        return copy.deepcopy(rendered)
    final = merge(rendered, overrides)
    strip_proxy_label(rendered, final)
    for container in _added_containers(rendered, final):
        security = container.get("securityContext")
        container["securityContext"] = {
            **(security if isinstance(security, dict) else {}),
            **copy.deepcopy(FOREIGN_CONTAINER_SECURITY),
        }
    return final


def drop_enforced_security(rendered: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """편집한 CR 전체에서 만든 patch 에서 `apply_overrides` 가 건 보안 값을 뺀다.

    편집기는 최종본을 보이므로 사이드카가 있으면 그 보안 값이 diff 에 그대로 실려 온다. 그 자리는
    잠겨 있어 빼지 않으면 한 번 저장한 사이드카를 다시 저장할 때 locked_field 가 된다. 값이 다르면
    남겨 두어 검사가 잡게 한다."""
    out = copy.deepcopy(patch)
    for container in _added_containers(rendered, out):
        security = container.get("securityContext")
        if not isinstance(security, dict):
            continue
        for field, value in FOREIGN_CONTAINER_SECURITY.items():
            if security.get(field) == value:
                del security[field]
        if not security:
            del container["securityContext"]
    return out


def _added_containers(rendered: dict[str, Any], doc: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """doc 의 파드 스펙마다 렌더에 없던 이름의 컨테이너·init 컨테이너 (Advanced 편집이 더한 것).
    렌더가 만든 컨테이너는 고르지 않는다. 그래야 기존 객체에 드리프트가 생기지 않는다."""
    for root in POD_ROOTS:
        pod = pod_spec(doc, root)
        if pod is None:
            continue
        base = pod_spec(rendered, root) or {}
        for kind in _CONTAINER_LISTS:
            known = {c.get("name") for c in _dicts(base.get(kind))}
            for container in _dicts(pod.get(kind)):
                if container.get("name") not in known and not container.get(DELETE_MARK):
                    yield container


def pod_spec(doc: Mapping[str, Any], root: Path) -> dict[str, Any] | None:
    """doc 의 root 아래 파드 스펙이다. 없거나 dict 가 아니면 None 이다."""
    node: Any = doc
    for seg in root:
        node = node.get(seg) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else None


def _dicts(items: Any) -> list[dict[str, Any]]:
    return [e for e in items if isinstance(e, dict)] if isinstance(items, list) else []


def strip_volatile(cr: dict[str, Any]) -> dict[str, Any]:
    """드리프트 비교용: status 와 서버 메타데이터를 뺀 사본."""
    out = copy.deepcopy(cr)
    out.pop("status", None)
    meta = out.get("metadata") or {}
    for k in VOLATILE_METADATA:
        meta.pop(k, None)
    return out


def foreign_managers(live: dict[str, Any], own: str = FIELD_MANAGER) -> list[str]:
    managers = []
    for m in (live.get("metadata") or {}).get("managedFields") or []:
        name = m.get("manager")
        if name and name != own and name not in managers:
            managers.append(name)
    return managers


@dataclass(frozen=True)
class SpecOwners:
    """managedFields 로 읽은 spec 자리의 소유. `applied` 는 own 이 Apply 로 적용한 기록이 있는가,
    `others` 는 본 자리를 하나라도 쥔 다른 manager 들(이름 순)이다."""

    applied: bool
    others: tuple[str, ...]


def spec_owners(live: dict[str, Any], keys: Iterable[str], own: str = FIELD_MANAGER) -> SpecOwners:
    """살아 있는 객체의 `spec` 바로 아래 자리 keys(`f:env` 같은 fieldsV1 키)를 누가 쥐었는가.
    status 하위 리소스의 기록은 보지 않는다. 다른 manager 의 기록에 fieldsV1 이 없으면 무엇을
    쥐었는지 모르므로 쥔 것으로 본다."""
    wanted = tuple(keys)
    applied = False
    others: set[str] = set()
    for m in (live.get("metadata") or {}).get("managedFields") or []:
        if m.get("subresource") == "status":
            continue
        manager = m.get("manager") or "unknown"
        if manager == own:
            applied = applied or m.get("operation") == "Apply"
            continue
        fields = m.get("fieldsV1")
        spec = (fields or {}).get("f:spec") or {}
        if fields is None or any(key in spec for key in wanted):
            others.add(manager)
    return SpecOwners(applied=applied, others=tuple(sorted(others)))


def locked_path_strings(paths: Iterable[Path] = LOCKED_PATHS) -> list[str]:
    return [format_path(p) for p in paths]
