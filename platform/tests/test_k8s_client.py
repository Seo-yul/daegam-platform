"""ClusterClient 의 응답 모양과 생성 실패 정리. API 서버에 붙지 않고 kubernetes 클라이언트를 바꿔
끼워 본다."""

from __future__ import annotations

import os

import pytest
from kubernetes import client as k8s
from kubernetes.dynamic.exceptions import (
    ConflictError,
    ForbiddenError,
    InternalServerError,
    NotFoundError,
    UnprocessibleEntityError,
)

from daegam.k8s import client as kclient
from daegam.k8s.client import (
    RBAC_GRANT_RETRIES,
    AccessDenied,
    ClusterClient,
    ClusterUnreachable,
    retry_forbidden,
)
from daegam.store.clusters import Cluster
from tests.helpers.docs import cluster_doc


def test_version_uses_api_server_camel_case(monkeypatch):
    """version() 은 fake·API 서버와 같은 camelCase(gitVersion)를 준다. preflight 는 그 키 하나만
    읽는다."""
    monkeypatch.setattr(kclient, "DynamicClient", lambda api_client: None)

    class _Version:
        def __init__(self, api):
            pass

        def get_code(self):
            return k8s.VersionInfo(
                git_version="v1.2.3+k3s1",
                major="1",
                minor="2",
                build_date="2026-01-01T00:00:00Z",
                compiler="gc",
                git_commit="abc",
                git_tree_state="clean",
                go_version="go1.24",
                platform="linux/amd64",
            )

    monkeypatch.setattr(kclient.k8s, "VersionApi", _Version)
    info = ClusterClient(k8s.ApiClient(k8s.Configuration())).version()
    assert info["gitVersion"] == "v1.2.3+k3s1"
    assert info["major"] == "1" and "git_version" not in info


class _Secrets:
    def __init__(self, data):
        self.data = data

    def get(self, name, namespace=None):
        return dict(self.data)


def _token_cluster() -> Cluster:
    return cluster_doc(mode="token", secret_name="s", api_url="https://r:6443")


def test_from_cluster_failure_removes_temp_ca_and_wraps_error(monkeypatch):
    """클라이언트를 만들다 실패하면 임시 CA 파일을 지우고 ClusterUnreachable 로 감싼다."""
    made: list[str] = []
    real = kclient.tempfile.NamedTemporaryFile

    def recording(*a, **kw):
        f = real(*a, **kw)
        made.append(f.name)
        return f

    def broken(api_client):
        raise OSError("connection refused")

    monkeypatch.setattr(kclient.tempfile, "NamedTemporaryFile", recording)
    monkeypatch.setattr(kclient, "DynamicClient", broken)
    secrets = _Secrets({"token": "t", "ca_bundle": "-----BEGIN CERTIFICATE-----\nx\n"})
    with pytest.raises(ClusterUnreachable, match="OSError: connection refused"):
        ClusterClient.from_cluster(_token_cluster(), secrets)
    assert len(made) == 1 and not os.path.exists(made[0])


def test_cluster_client_cache_reuses_until_invalidated(monkeypatch):
    """운영 배선(client_factory 없음)이 쓰는 캐시 그대로다. 같은 클러스터는 한 번만 만들고,
    invalidate·clear 뒤에는 다시 만든다. 없는 id 의 invalidate 는 할 일이 없다."""
    made: list[str] = []

    def build(cls, cluster, secret_store, *, platform_namespace):
        made.append(cluster.id)
        return object()

    monkeypatch.setattr(ClusterClient, "from_cluster", classmethod(build))
    cache = kclient.ClusterClientCache(lambda: None)
    cluster = _token_cluster()
    first = cache.get(cluster)
    assert cache.get(cluster) is first and made == [cluster.id]
    cache.invalidate("missing")
    cache.invalidate(cluster.id)
    assert cache.get(cluster) is not first
    cache.clear()
    cache.get(cluster)
    assert made == [cluster.id] * 3


class _Resource:
    """동적 클라이언트의 리소스 자리. server_side_apply 가 받은 인자를 남긴다."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def server_side_apply(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs["body"]


def _client_with(monkeypatch, resource=None, **kwargs) -> tuple[ClusterClient, _Resource]:
    resource = resource or _Resource()

    class _Dyn:
        def __init__(self, api_client):
            self.resources = self

        def get(self, *, api_version, kind):
            return resource

    monkeypatch.setattr(kclient, "DynamicClient", _Dyn)
    return ClusterClient(k8s.ApiClient(k8s.Configuration()), **kwargs), resource


CR = {"apiVersion": "toolhive.stacklok.dev/v1beta1", "kind": "MCPServer", "metadata": {"name": "m"}}
SECRET = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "s", "namespace": "n"}}


def test_apply_is_strict_only_for_the_kinds_the_dry_run_accepted(monkeypatch):
    """P50 2절: Strict dry run 을 통과한 kind 의 ToolHive CR 만 fieldValidation=Strict 로 보낸다.
    다른 ToolHive kind 와 다른 리소스는 서버 기본값 그대로다."""
    cc, res = _client_with(monkeypatch, strict_kinds=frozenset({"MCPServer"}))
    cc.apply(CR)
    assert res.calls[-1]["query_params"] == [("fieldValidation", "Strict")]
    assert res.calls[-1]["field_manager"] == "daegam" and res.calls[-1]["force_conflicts"]
    cc.apply({**CR, "kind": "MCPOIDCConfig"})
    assert "query_params" not in res.calls[-1]
    cc.apply(SECRET)
    assert "query_params" not in res.calls[-1]
    # dry run 미리보기도 같은 규칙이다 (Advanced 편집이 적용과 같은 거부를 먼저 본다)
    cc.dry_run_apply(CR)
    assert res.calls[-1]["dry_run"] == "All"
    assert res.calls[-1]["query_params"] == [("fieldValidation", "Strict")]


def test_apply_is_not_strict_until_the_cluster_is_ready(monkeypatch):
    cc, res = _client_with(monkeypatch)
    cc.apply(CR)
    assert "query_params" not in res.calls[-1]
    # 점검 단계는 값을 직접 준다
    cc.apply(CR, dry_run=True, field_validation="Strict")
    assert res.calls[-1]["dry_run"] == "All"
    assert res.calls[-1]["query_params"] == [("fieldValidation", "Strict")]
    # 값을 주면 클러스터 설정보다 그것이다
    strict, res = _client_with(monkeypatch, strict_kinds=frozenset({"MCPServer"}))
    strict.apply(CR, field_validation="Warn")
    assert res.calls[-1]["query_params"] == [("fieldValidation", "Warn")]


def test_from_cluster_takes_the_kinds_of_the_last_dry_run(monkeypatch):
    from daegam.store.clusters import ClusterGates, StrictApplyCheck

    monkeypatch.setattr(kclient, "DynamicClient", lambda api_client: None)
    secrets = _Secrets({"token": "t", "ca_bundle": "-----BEGIN CERTIFICATE-----\nx\n"})
    cluster = _token_cluster()

    def kinds() -> frozenset[str]:
        return ClusterClient.from_cluster(cluster, secrets).strict_kinds

    assert kinds() == frozenset()
    # 거부가 있었던 kind 는 빠지고 통과한 kind 만 든다
    check = StrictApplyCheck(ok=False, checked=3, refused=1, kinds=["MCPServer"])
    cluster.gates = ClusterGates(strict_apply=check)
    assert kinds() == frozenset({"MCPServer"})
    # 아무 문서도 보내지 않은 점검은 어느 kind 도 켜지 않는다
    cluster.gates = ClusterGates(strict_apply=StrictApplyCheck(ok=False, kinds=["MCPServer"]))
    assert kinds() == frozenset()


def test_unreachable_errors_are_connection_errors_and_5xx():
    """Strict dry run 은 이런 오류에서 그 클러스터를 멈추고 지난 판정을 둔다. 4xx 는 요청 하나의
    판정이다."""
    import urllib3
    from kubernetes.client.exceptions import ApiException

    from daegam.k8s.client import ClusterUnreachable, is_unreachable_error

    assert is_unreachable_error(ClusterUnreachable("down"))
    assert is_unreachable_error(ConnectionRefusedError())
    assert is_unreachable_error(urllib3.exceptions.MaxRetryError(None, "/x"))
    assert is_unreachable_error(ApiException(status=503))
    assert is_unreachable_error(ApiException(status=0))
    assert not is_unreachable_error(ApiException(status=422))
    assert not is_unreachable_error(ValueError("strict decoding error"))


def _forbidden() -> ForbiddenError:
    return ForbiddenError(k8s.ApiException(status=403, reason="Forbidden"))


class _Forbidding:
    """앞의 denials 번은 403 으로 거절하는 리소스 자리. 동사마다 부른 순서를 남긴다."""

    name = "secrets"

    def __init__(self, denials: int) -> None:
        self.denials = denials
        self.calls: list[str] = []

    def _call(self, verb: str, result):
        self.calls.append(verb)
        if len(self.calls) <= self.denials:
            raise _forbidden()
        return result

    def get(self, **kw):
        return self._call("get", {"kind": "Secret", "metadata": {"name": kw["name"]}})

    def server_side_apply(self, **kw):
        return self._call("apply", kw["body"])

    def create(self, **kw):
        return self._call("create", kw["body"])

    def delete(self, **kw):
        return self._call("delete", {})

    def patch(self, **kw):
        return self._call("patch", {})


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(kclient.time, "sleep", lambda _seconds: None)


@pytest.mark.parametrize(
    ("verb", "call"),
    [
        ("get", lambda cc: cc.get("v1", "Secret", "s", "n")),
        ("apply", lambda cc: cc.apply(SECRET)),
        ("create", lambda cc: cc.create(SECRET)),
        ("delete", lambda cc: cc.delete("v1", "Secret", "s", "n")),
        ("patch", lambda cc: cc.drop_secret_keys("s", "n", ["k"])),
    ],
)
def test_a_secret_call_waits_out_a_fresh_binding(monkeypatch, no_sleep, verb, call):
    """P63 2절: 테넌트 Secret 권한은 막 만든 RoleBinding 으로 오므로, 읽기를 포함한 Secret 의 모든
    호출이 403 을 잠깐 다시 해 본다."""
    resource = _Forbidding(denials=2)
    cc, _ = _client_with(monkeypatch, resource)
    call(cc)
    assert resource.calls == [verb] * 3


class _Patching:
    """patch 의 인자를 남긴다. refuse 면 JSON Patch 의 test 가 어긋난 것처럼 422 다. error 를 주면
    그것을 낸다."""

    name = "secrets"

    def __init__(self, refuse: bool = False, error: Exception | None = None) -> None:
        self.refuse = refuse
        self.error = error
        self.patches: list[dict] = []

    def patch(self, **kw):
        self.patches.append(kw)
        if self.error is not None:
            raise self.error
        if self.refuse:
            raise UnprocessibleEntityError(k8s.ApiException(status=422, reason="Invalid"))
        return {}


def test_a_swap_is_one_json_patch_that_tests_before_it_writes(monkeypatch):
    resource = _Patching()
    cc, _ = _client_with(monkeypatch, resource)
    assert cc.swap_secret_keys("s", "n", {"t1": "new"}, {"t1": "old", "t1.previous": None})
    (patch,) = resource.patches
    assert patch["content_type"] == "application/json-patch+json"
    assert patch["body"] == [
        {"op": "test", "path": "/data/t1", "value": "bmV3"},
        {"op": "add", "path": "/data/t1", "value": "b2xk"},
        {"op": "remove", "path": "/data/t1.previous"},
    ]
    refused = _Patching(refuse=True)
    cc, _ = _client_with(monkeypatch, refused)
    assert cc.swap_secret_keys("s", "n", {"a/b~": "x"}, {"a/b~": None}) is False
    assert refused.patches[0]["body"][0]["path"] == "/data/a~1b~0"


def test_a_swap_on_a_missing_secret_is_false_and_other_errors_raise(monkeypatch):
    gone = _Patching(error=NotFoundError(k8s.ApiException(status=404, reason="Not Found")))
    cc, _ = _client_with(monkeypatch, gone)
    assert cc.swap_secret_keys("s", "n", {"t1": "new"}, {"t1": None}) is False
    for error in (
        ConflictError(k8s.ApiException(status=409, reason="Conflict")),
        InternalServerError(k8s.ApiException(status=500, reason="Internal Server Error")),
    ):
        cc, _ = _client_with(monkeypatch, _Patching(error=error))
        with pytest.raises(type(error)):
            cc.swap_secret_keys("s", "n", {"t1": "new"}, {"t1": None})


def test_a_secret_403_that_stays_is_reported(monkeypatch, no_sleep):
    resource = _Forbidding(denials=RBAC_GRANT_RETRIES)
    cc = _client_by_kind(monkeypatch, {"Secret": resource, "Namespace": _Namespaces({"n"})})
    with pytest.raises(AccessDenied):
        cc.get("v1", "Secret", "s", "n")
    assert len(resource.calls) == RBAC_GRANT_RETRIES


def test_other_kinds_are_not_retried(monkeypatch, no_sleep):
    resource = _Forbidding(denials=1)
    cc = _client_by_kind(monkeypatch, {"ConfigMap": resource, "Namespace": _Namespaces({"n"})})
    with pytest.raises(AccessDenied):
        cc.get("v1", "ConfigMap", "c", "n")
    assert resource.calls == ["get"]


def test_retry_forbidden_raises_other_errors_at_once(no_sleep):
    calls: list[int] = []

    def boom():
        calls.append(1)
        raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        retry_forbidden(boom)
    assert calls == [1]

    def denied_then_ok():
        calls.append(2)
        if calls.count(2) == 1:
            raise AccessDenied("get", "secrets", "n")
        return "ok"

    assert retry_forbidden(denied_then_ok) == "ok" and calls.count(2) == 2


class _Namespaces:
    """Namespace 자리. present 에 든 이름만 있다."""

    name = "namespaces"

    def __init__(self, present: set[str], *, forbidden: bool = False) -> None:
        self.present = present
        self.forbidden = forbidden

    def get(self, **kw):
        if self.forbidden:
            raise _forbidden()
        if kw["name"] not in self.present:
            raise NotFoundError(k8s.ApiException(status=404, reason="Not Found"))
        return {"kind": "Namespace", "metadata": {"name": kw["name"]}}


def _client_by_kind(monkeypatch, resources: dict[str, object]) -> ClusterClient:
    class _Dyn:
        def __init__(self, api_client):
            self.resources = self

        def get(self, *, api_version, kind):
            return resources[kind]

    monkeypatch.setattr(kclient, "DynamicClient", _Dyn)
    return ClusterClient(k8s.ApiClient(k8s.Configuration()))


@pytest.mark.parametrize("kind", ["Secret", "MCPAuthzConfig"])
def test_a_403_in_a_namespace_that_is_gone_reads_as_gone(monkeypatch, no_sleep, kind):
    """P63: 테넌트 권한은 네임스페이스의 RoleBinding 으로 오므로, 밖에서 지운 네임스페이스의 객체는
    404 가 아니라 403 이다. 네임스페이스가 없으면 지우기는 False, 읽기는 None 이라 삭제가 멈추지
    않는다."""
    gone = _Forbidding(denials=10**6)
    cc = _client_by_kind(monkeypatch, {kind: gone, "Namespace": _Namespaces(set())})
    assert cc.delete("v1", kind, "s", "daegam-gone") is False
    assert cc.get("v1", kind, "s", "daegam-gone") is None


def test_a_403_in_a_namespace_that_exists_is_still_reported(monkeypatch, no_sleep):
    """네임스페이스가 있거나 그 읽기도 거절되면 403 은 그대로다."""
    for namespaces in (_Namespaces({"daegam-a"}), _Namespaces(set(), forbidden=True)):
        cc = _client_by_kind(
            monkeypatch, {"Secret": _Forbidding(denials=10**6), "Namespace": namespaces}
        )
        with pytest.raises(ForbiddenError):
            cc.delete("v1", "Secret", "s", "daegam-a")
        with pytest.raises(AccessDenied):
            cc.get("v1", "Secret", "s", "daegam-a")


class _Deleting:
    """delete 가 차례로 answers 를 내는 리소스 자리. 예외면 올리고 아니면 돌려준다."""

    name = "mcpserverentries"

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls = 0

    def delete(self, **kw):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _status(cls, status: int):
    return cls(k8s.ApiException(status=status, reason="x"))


@pytest.mark.parametrize(
    ("answers", "deleted", "calls"),
    [
        # 지우고도 500 으로 답하고 바로 다시 지우면 404: 앞의 지우기가 지운 것이다
        ((_status(InternalServerError, 500), _status(NotFoundError, 404)), True, 2),
        ((_status(InternalServerError, 500), {}), True, 2),
        ((_status(NotFoundError, 404),), False, 1),
        (({},), True, 1),
    ],
)
def test_a_delete_answered_with_a_server_error_is_tried_once_more(
    answers, deleted, calls, monkeypatch
):
    """OrbStack 의 API 서버는 오퍼레이터가 status 를 쓰는 순간의 MCPServerEntry 지우기에
    `unexpected end of JSON input` 500 으로 답하지만 객체는 지웠다. Gateway 삭제가 그 500 으로 502
    k8s_error 를 내지 않도록 한 번 다시 지워 본다."""
    resource = _Deleting(*answers)
    cc = _client_by_kind(monkeypatch, {"MCPServerEntry": resource})
    assert cc.delete("toolhive.stacklok.dev/v1beta1", "MCPServerEntry", "e", "daegam-a") is deleted
    assert resource.calls == calls


def test_a_server_error_that_stays_is_raised(monkeypatch):
    resource = _Deleting(_status(InternalServerError, 500), _status(InternalServerError, 500))
    cc = _client_by_kind(monkeypatch, {"MCPServerEntry": resource})
    with pytest.raises(InternalServerError):
        cc.delete("toolhive.stacklok.dev/v1beta1", "MCPServerEntry", "e", "daegam-a")
    assert resource.calls == 2
