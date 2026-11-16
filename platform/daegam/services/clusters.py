"""Cluster 서비스: 클라이언트 팩토리, home 자동 등록, preflight 실행·저장, 접속 비밀 저장."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection
from typing import Any, ClassVar

from starlette.concurrency import run_in_threadpool

from daegam.config import Settings
from daegam.k8s.client import ClusterClientCache, ClusterUnreachable, KubeClient
from daegam.k8s.labels import LABEL_ID, LABEL_RESOURCE
from daegam.k8s.preflight import GateInputs, check_connected, connect
from daegam.secrets import K8sSecretStore, SecretStore
from daegam.services.shipped_redis_ca import read_shipped_ca
from daegam.store.clusters import (
    HOME_CLUSTER_NAME,
    Cluster,
    ClusterConnection,
    MongoClusterStore,
    Preflight,
    StrictApplyCheck,
    home_env_fields,
    secret_name_for,
)

logger = logging.getLogger("daegam.clusters")

ClientFactory = Callable[[Cluster], KubeClient]
# 플랫폼이 테넌트 객체를 두는 이 클러스터의 네임스페이스 (P63 preflight 의 바인딩 검사). 점검이 만든
# 클라이언트를 함께 받는다
TenantNamespaces = Callable[[Cluster, KubeClient], Awaitable[Collection[str]]]


class ClusterService:
    """앱 상태에 하나 올라간다. 테스트는 tests/helpers 의 대역 클러스터와 Secret 저장소를 넣는다."""

    # 만든 뒤에 붙이는 훅 (P72 2.4절). 세 인스턴스 서비스와 인증 재적용이 이 서비스를 받아
    # 만들어지므로 생성자로 받을 수 없다. wiring 의 `_check_wiring` 이 기동 때 모두 채워졌는지 본다
    late_hooks: ClassVar[frozenset[str]] = frozenset({"instance_checker", "tenant_namespaces"})

    def __init__(
        self,
        store: MongoClusterStore,
        settings: Settings,
        *,
        client_factory: ClientFactory | None = None,
        secret_store: SecretStore | None = None,
        invalidate: Callable[[str], None] | None = None,
    ):
        self.store = store
        self.settings = settings
        self._secret_store = secret_store
        self._home_client: KubeClient | None = None
        if client_factory is None:
            cache = ClusterClientCache(
                lambda: self.secret_store(), platform_namespace=settings.namespace
            )
            client_factory = cache.get
            invalidate = cache.invalidate
        self.client_for = client_factory
        self._invalidate = invalidate or (lambda _id: None)
        # wiring 이 인증 재적용의 `DataPlaneAuth.tenant_namespaces` 를 붙인다. 없으면 preflight 가
        # 바인딩을 보지 않는다
        self.tenant_namespaces: TenantNamespaces | None = None

    # -- 접속 비밀 ---------------------------------------------------------

    def home_client(self) -> KubeClient | None:
        """home 클러스터 클라이언트. 기동의 bootstrap_home 전이거나 붙지 못했으면 None 이다."""
        return self._home_client

    def secret_store(self) -> SecretStore | None:
        """플랫폼 네임스페이스 Secret 저장소. 기본은 home 클러스터의 K8sSecretStore."""
        if self._secret_store is not None:
            return self._secret_store
        if self._home_client is not None:
            self._secret_store = K8sSecretStore(self._home_client)
        return self._secret_store

    def store_credentials(self, cluster_id: str, data: dict[str, str]) -> str:
        ss = self.secret_store()
        if ss is None:
            raise NoHomeCluster()
        name = secret_name_for(cluster_id)
        ss.put(
            name,
            data,
            namespace=self.settings.namespace,
            labels={LABEL_RESOURCE: "cluster", LABEL_ID: cluster_id},
        )
        return name

    def delete_credentials(self, cluster: Cluster) -> None:
        ss = self.secret_store()
        if ss is None or not cluster.connection.secret_name:
            return
        ss.delete(cluster.connection.secret_name, namespace=self.settings.namespace)

    def invalidate(self, cluster_id: str) -> None:
        self._invalidate(cluster_id)

    # -- preflight ---------------------------------------------------------

    def _client_or_failure(self, cluster: Cluster) -> KubeClient | Preflight:
        """점검에 쓸 클라이언트. 만들지 못하면 그 실패를 적은 Preflight 다."""
        try:
            return self.client_for(cluster)
        except ClusterUnreachable as e:
            return Preflight(ok=False, missing=[f"connect: {e}"])
        except Exception as e:  # noqa: BLE001
            return Preflight(ok=False, missing=[f"connect: {type(e).__name__}: {e}"])

    async def _tenant_namespaces(
        self, cluster: Cluster, client: KubeClient
    ) -> Collection[str] | None:
        """바인딩을 볼 네임스페이스. 얻다가 오류가 나면 None 이라 이번 점검은 바인딩을 보지
        않는다."""
        if self.tenant_namespaces is None:
            return None
        try:
            return await self.tenant_namespaces(cluster, client)
        except Exception as e:  # noqa: BLE001 - 목록을 못 얻어도 preflight 는 돈다
            logger.warning("cluster %s: tenant namespaces unavailable: %s", cluster.name, e)
            return None

    async def _check(self, cluster: Cluster) -> tuple[Preflight, GateInputs]:
        """클라이언트 하나로 연결을 확인하고, 연결되면 네임스페이스 목록을 얻은 뒤 나머지를
        점검한다. 연결하지 못한 클러스터에는 목록을 묻지 않는다. K8s 호출은 스레드풀에서 한다."""
        client = await run_in_threadpool(self._client_or_failure, cluster)
        if isinstance(client, Preflight):
            return client, GateInputs()
        k8s_version, failed = await run_in_threadpool(connect, client)
        if failed is not None:
            return failed, GateInputs()
        namespaces = await self._tenant_namespaces(cluster, client)
        return await run_in_threadpool(check_connected, client, k8s_version, namespaces)

    async def preflight(self, cluster: Cluster) -> Preflight:
        """preflight 를 돌리고 결과와 게이트 재료(`Cluster.gates`)를 저장한다. 예외를 던지지 않는다.

        이번 점검이 판정하지 못한 재료(연결 오류, 권한 검사 오류, 오퍼레이터를 읽지 못함)는 저장된
        값을 그대로 둔다. 판(toolhive_version)도 오퍼레이터를 읽지 못했으면 앞의 값을 이어 쓴다.
        연결 오류 한 번으로 한 시간 동안 게이트가 닫히지 않게 하려는 것이다(P50 2절). 넘겨받은
        cluster 의 `gates` 도 저장한 값으로 맞춘다. 부르는 쪽이 그 문서로 응답을 만들기 때문이다.

        정책 컨트롤러의 워크로드 목록을 다 읽지 못했으면 저장된 컨트롤러와 NetworkPolicy 판정을
        이어 쓴다(`GateInputs.policy_scanned`). home 의 NetworkPolicy 판정은 환경
        (DAEGAM_HOME_NETWORK_POLICY)이 있으면 그 값이다. 배포 뒤 실제 차단을 본 결과다(D54).
        컨트롤러는 그때도 탐지한 것이다."""
        result, gates = await self._check(cluster)
        previous = cluster.status.preflight
        if not gates.operator_read and previous is not None and previous.toolhive_version:
            result = result.model_copy(update={"toolhive_version": previous.toolhive_version})
        if cluster.is_home and self.settings.home_redis_tls is not None:
            found = await read_shipped_ca(
                self._home_client, self.settings.namespace, self.settings.home_redis_tls
            )
            if found is not None:
                result = result.model_copy(
                    update={"shipped_redis_cert_expires_at": found.expires_at}
                )
        if not gates.policy_scanned and previous is not None and previous.policy_controller:
            result = result.model_copy(
                update={
                    "network_policy": previous.network_policy,
                    "policy_controller": previous.policy_controller,
                }
            )
        pinned = self.settings.home_network_policy
        if cluster.is_home and pinned:
            # 못 박는 것은 집행 판정뿐이다. 컨트롤러는 탐지한 것을 그대로 둔다. 메타데이터 차단이
            # 확인된 컨트롤러인지를 그것으로 가린다 (T75)
            result = result.model_copy(
                update={"network_policy": "enforced" if pinned == "enforced" else "unknown"}
            )
        # 점검 결과는 읽은 접속 정보에 대한 것이다. 그사이 사람의 저장이 지나갔거나 클레임이 살아
        # 있으면 남기지 않고 다음 점검을 기다린다 (P68 2절)
        stored = await self.store.set_preflight(cluster.id, result, expect_seq=cluster._seq)
        decided: dict[str, Any] = {}
        if gates.tenant_object_rights is not None:
            decided["tenant_object_rights"] = gates.tenant_object_rights
        if gates.sandbox_rights is not None:
            decided["sandbox_rights"] = gates.sandbox_rights
        if gates.operator_read:
            decided["usage_metrics"] = gates.usage_metrics
        if stored:
            await self.store.set_gates(cluster.id, decided, expect_seq=cluster._seq)
        else:
            logger.info("cluster %s changed during preflight; result not stored", cluster.name)
        cluster.gates = cluster.gates.model_copy(update=decided)
        cluster.status.preflight = result
        if not result.ok:
            logger.warning("cluster %s preflight failed: %s", cluster.name, result.missing)
        return result

    async def record_strict_apply(self, cluster: Cluster, check: StrictApplyCheck) -> None:
        """Strict dry run 의 판정을 저장한다. Strict 로 적용할 kind 가 바뀌면 이 프로세스의
        클라이언트 캐시를 비워 다음 적용부터 따르게 한다. 다른 레플리카는 캐시된 클라이언트가
        지나면(`CLIENT_CACHE_TTL`) 따른다."""
        before = cluster.gates.strict_apply
        await self.store.set_strict_apply(cluster.id, check, expect_seq=cluster._seq)
        cluster.gates = cluster.gates.model_copy(update={"strict_apply": check})
        if before is None or set(before.kinds) != set(check.kinds):
            self.invalidate(cluster.id)
            logger.info(
                "cluster %s strict apply of ToolHive CRs on %s (%d checked, %d refused)",
                cluster.name,
                ", ".join(sorted(check.kinds)) or "no kind",
                check.checked,
                check.refused,
            )

    # -- home --------------------------------------------------------------

    async def bootstrap_home(self) -> Cluster | None:
        """DAEGAM_IN_CLUSTER 또는 DAEGAM_HOME_KUBECONTEXT 가 있을 때 home 을 upsert 하고
        preflight 를 한 번 돌린다."""
        mode = self.settings.home_mode
        if mode is None:
            return None
        if mode == "in_cluster":
            conn = ClusterConnection(mode="in_cluster")
        else:
            conn = ClusterConnection(mode="kubecontext", kubecontext=self.settings.home_kubecontext)
        home = await self.store.upsert_home(conn, self._home_env_fields())
        self.invalidate(home.id)
        try:
            self._home_client = await run_in_threadpool(self.client_for, home)
        except Exception as e:  # noqa: BLE001
            logger.error("home cluster client unavailable: %s", e)
            self._home_client = None
        await self.preflight(home)
        return home

    async def home(self) -> Cluster | None:
        return await self.store.get_by_name(HOME_CLUSTER_NAME)

    async def plain_closed_at(self, cluster_id: str, host: str, port: int | None) -> bool:
        """그 클러스터의 그 주소가 평문 포트를 닫은 함께 올린 Redis 인가 (P61 5절 5단계). 그
        Redis 는 home 에만 있고(`DAEGAM_HOME_REDIS_TLS`), 주소는 `ShippedRedisTls.serves` 로
        견준다. 기동의 표시(`shipped_redis.mark_shipped_plain`)와 Database·클러스터 저장이 같은
        판정을 쓴다."""
        shipped = self.settings.home_redis_tls
        if shipped is None or shipped.plain or not shipped.serves(host, port):
            return False
        home = await self.home()
        return home is not None and home.id == cluster_id

    def _home_env_fields(self) -> dict[str, Any]:
        return home_env_fields(
            self.settings.home_platform_internal_url(),
            ingress_overrides=self.settings.home_ingress,
            images_overrides=self.settings.home_images,
            resources=self.settings.home_resources,
            limit_range=self.settings.home_limit_range,
            sandbox_image=self.settings.home_sandbox_image,
            block_metadata=self.settings.home_block_metadata,
        )

    def env_managed(self, cluster: Cluster) -> list[str]:
        """환경(DAEGAM_HOME_*)이 기동마다 덮어쓰는 home 필드의 경로. 화면은 이 필드를 읽기 전용으로
        보이고 PATCH 는 이 필드를 다른 값으로 바꾸지 못한다(D54). 다른 클러스터는 빈 목록이다."""
        if not cluster.is_home or self.settings.home_mode is None:
            return []
        return sorted(self._home_env_fields())

    # -- 삭제 훅 -----------------------------------------------------------

    # 배선(wiring.py)이 붙인다. 붙이지 않으면 삭제 검사가 사라지므로 조용히 통과시키지 않는다
    instance_checker: Callable[[str], Awaitable[bool]] | None = None

    async def has_instances(self, cluster_id: str) -> bool:
        if self.instance_checker is None:
            raise RuntimeError(
                "cluster_service.instance_checker 가 배선되지 않았다. 이대로면 살아 있는 "
                "인스턴스가 있는 클러스터도 지워진다 (wiring.build_services 를 본다)"
            )
        return await self.instance_checker(cluster_id)


class NoHomeCluster(Exception):
    """플랫폼 네임스페이스 Secret 을 쓸 home 클러스터 클라이언트가 없다."""
