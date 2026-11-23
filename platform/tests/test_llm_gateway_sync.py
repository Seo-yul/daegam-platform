"""LLM 게이트웨이의 동기화 (P38 4절·10절).

fake 클러스터의 게이트웨이 네임스페이스에 렌더하고, 파드마다의 리비전은 가짜 읽기(`read_rev`)가
답한다. 시계는 테스트가 옮긴다. 상태 문서와 Platform Settings 는 시험용 Mongo 에 둔다.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import timedelta

import pytest

from daegam.k8s.client import decode_secret_data
from daegam.k8s.labels import LABEL_PLATFORM_ID
from daegam.k8s.templates import llm_gateway as tpl
from daegam.services.llm_gateway import (
    APPLY_FAILED,
    CONFIG_REJECTED,
    NAMESPACE_MISSING,
    NAMESPACE_TAKEN,
    PAUSE_ANNOTATION,
    PAUSED,
    PROVIDER_REJECTED,
)
from daegam.store.base import utcnow
from tests.helpers.aio import run_db
from tests.helpers.llm_gateway import (
    NS,
    PLATFORM_NS,
    PROVIDERS,
    T0,
    Rig,
    gateway_namespace,
    gateway_pod,
    make_rig,
)


def test_off_and_never_rendered_asks_the_cluster_nothing(settings):
    fake = gateway_namespace()

    async def go(db):
        rig = await make_rig(db, fake, on=False)
        assert await rig.gw.sync() == {}

    run_db(settings, go)
    assert fake.applied == [] and fake.deleted == []


@pytest.mark.parametrize(
    ("labels", "present", "reason"),
    [
        ({}, False, NAMESPACE_MISSING),
        ({"daegam.devops.ai.kr/tenant": "llm"}, True, NAMESPACE_TAKEN),
        ({LABEL_PLATFORM_ID: "another-install"}, True, "llm_gateway_foreign_install"),
    ],
)
def test_the_sync_renders_nothing_into_a_namespace_it_cannot_own(settings, labels, present, reason):
    fake = gateway_namespace(labels=labels)
    if not present:
        del fake.objects[("v1", "Namespace", None, NS)]

    async def go(db):
        rig = await make_rig(db, fake)
        assert await rig.gw.sync() == {"blocked": [reason]}
        assert (await rig.store.get()).reason == reason

    run_db(settings, go)
    assert [d for d in fake.applied if d["kind"] != "Namespace"] == []


def test_the_first_render_marks_the_namespace_and_writes_keys_as_files(settings):
    fake = gateway_namespace()

    async def go(db):
        rig = await make_rig(db, fake)
        out = await rig.gw.sync()
        state = await rig.store.get()
        install = await rig.ps.install_id()
        ns = rig.obj("v1", "Namespace", NS, None)
        assert ns["metadata"]["labels"][LABEL_PLATFORM_ID] == install
        assert ns["metadata"]["labels"]["daegam.devops.ai.kr/component"] == "llm-gateway"
        assert state.activated_at == T0 and state.rendered
        assert out == {"synced": [state.desired_rev]} and state.desired_rev == rig.rev()
        files = rig.config()
        assert files["key.openai"] == "sk-openai" and files["key.echo"] == "sk-echo"
        # 플랫폼 네임스페이스를 부르는 provider 는 키도 경로도 없다
        assert "key.inside" not in files and "/p/inside/" not in files["config.yaml"]
        assert state.providers["inside"].rejected == "namespace daegam-system is reserved"
        assert PROVIDER_REJECTED in state.warnings
        # 아직 파드가 없어 아무것도 반영되지 않았다
        assert state.providers_ready == [] and state.ready_at is None
        kinds = {(d["kind"], d["metadata"]["name"]) for d in fake.applied}
        assert ("Deployment", "llm-gateway") in kinds and (
            "NetworkPolicy",
            "llm-internal-upstreams",
        ) in kinds
        assert rig.obj("v1", "Secret", tpl.REDIS) is not None

    run_db(settings, go)


def test_a_revision_applies_when_every_pod_reports_it(settings):
    fake = gateway_namespace()
    gateway_pod(fake, "gw-a")
    gateway_pod(fake, "gw-b")

    async def go(db):
        rig = await make_rig(db, fake)
        await rig.gw.sync()
        rig.revs.update({"gw-a": rig.rev(), "gw-b": None})
        rig.tick()
        await rig.gw.sync()
        assert (await rig.store.get()).providers_ready == []
        rig.revs["gw-b"] = rig.rev()
        rig.tick()
        await rig.gw.sync()
        state = await rig.store.get()
        assert state.applied_rev == rig.rev() and state.ready_at == rig.now[0]
        assert state.providers_ready == ["echo", "openai"]
        applied = rig.obj("v1", "Secret", tpl.APPLIED_SECRET)
        assert decode_secret_data(applied["data"]) == rig.config()

    run_db(settings, go)


def test_the_purpose_attribute_counts_only_once_every_pod_started_with_it(settings):
    """채점 호출의 목적 속성은 시작할 때만 읽는 칸이다. 반영된 리비전이라도 파드가 지금의 설정
    판으로 뜨기 전에는 목적 리비전을 적지 않는다 (P45 11절)."""
    fake = gateway_namespace()
    gateway_pod(fake, "gw-a")

    async def go(db):
        rig = await make_rig(db, fake)
        await rig.gw.sync()
        rig.revs["gw-a"] = rig.rev()
        rig.tick()
        await rig.gw.sync()
        state = await rig.store.get()
        assert state.applied_rev == rig.rev() and state.purpose_rev is None
        template = rig.obj("apps/v1", "Deployment", tpl.GATEWAY)["spec"]["template"]
        started = template["metadata"]["annotations"][tpl.CONFIG_REV_ANNOTATION]
        pod = rig.obj("v1", "Pod", "gw-a")
        pod["metadata"]["annotations"] = {tpl.CONFIG_REV_ANNOTATION: started}
        rig.tick()
        await rig.gw.sync()
        assert (await rig.store.get()).purpose_rev == rig.rev()
        # 지금 판으로 뜨지 않은 파드가 다시 보이면 목적 리비전을 지운다
        pod["metadata"]["annotations"] = {tpl.CONFIG_REV_ANNOTATION: "older"}
        rig.tick()
        await rig.gw.sync()
        assert (await rig.store.get()).purpose_rev is None

    run_db(settings, go)


def test_a_changed_key_changes_the_revision_without_rolling_pods(settings):
    fake = gateway_namespace()

    async def go(db):
        rig = await make_rig(db, fake)
        await rig.gw.sync()
        before, template = rig.rev(), rig.obj("apps/v1", "Deployment", tpl.GATEWAY)
        rig.secrets.secrets[(PLATFORM_NS, "k-openai")] = {"api_key": "sk-rotated"}
        await rig.gw.sync_provider("openai")
        assert rig.config()["key.openai"] == "sk-rotated" and rig.rev() != before
        state = await rig.store.get()
        assert state.key_synced["openai"] == T0
        after = rig.obj("apps/v1", "Deployment", tpl.GATEWAY)
        # 키와 설정은 다시 읽히므로 파드 템플릿은 그대로다 (F0 1번)
        assert after["spec"]["template"] == template["spec"]["template"]

    run_db(settings, go)


def test_a_terminating_pod_counts_only_while_no_ready_pod_is_left(settings):
    fake = gateway_namespace()
    gateway_pod(fake, "old", ready=False, deleted=T0 - timedelta(seconds=5))

    async def go(db):
        rig = await make_rig(db, fake)
        await rig.gw.sync()
        rig.revs["old"] = "stale"
        await asyncio.to_thread(gateway_pod, fake, "new")
        rig.revs["new"] = rig.rev()
        # 지워진 지 30초가 안 됐다. 그 파드의 연결이 아직 요청을 받을 수 있다
        rig.tick(10)
        await rig.gw.sync()
        assert (await rig.store.get()).applied_rev is None
        rig.tick(30)
        await rig.gw.sync()
        assert (await rig.store.get()).applied_rev == rig.rev()

    run_db(settings, go)


def test_a_pod_stuck_on_an_old_revision_is_deleted_with_a_short_grace(settings):
    fake = gateway_namespace()
    gateway_pod(fake, "good")
    gateway_pod(fake, "stuck")

    async def go(db):
        rig = await make_rig(db, fake)
        await rig.gw.sync()
        rig.revs.update({"good": rig.rev(), "stuck": "old"})
        # 그 파드가 옛 리비전을 처음 알린 뒤 2분까지는 둔다
        for _ in range(3):
            rig.tick()
            await rig.gw.sync()
        assert ("v1", "Pod", NS, "stuck") not in fake.deleted
        rig.tick()
        await rig.gw.sync()

    run_db(settings, go)
    assert fake.deleted == [("v1", "Pod", NS, "stuck")]
    assert fake.delete_graces[("v1", "Pod", NS, "stuck")] == 10


def test_a_dropped_config_is_restored_and_not_rewritten(settings):
    """모든 파드가 옛 리비전에 2분 넘게 있으면 파드는 두고 마지막 좋은 설정으로 되돌린다. 원하는
    리비전이 바뀔 때까지 다시 쓰지 않는다 (4절)."""
    fake = gateway_namespace()
    gateway_pod(fake, "a")
    gateway_pod(fake, "b")

    async def go(db):
        rig = await make_rig(db, fake)
        await rig.gw.sync()
        good = rig.rev()
        rig.revs.update({"a": good, "b": good})
        rig.tick()
        await rig.gw.sync()
        good_files = rig.config()
        await rig.ps.update(
            {
                "llm_providers": [
                    *PROVIDERS,
                    {"name": "extra", "kind": "openai", "api_key_secret": "k-openai"},
                ]
            }
        )
        rig.tick()
        await rig.gw.sync()
        bad = rig.rev()
        assert bad != good
        for _ in range(3):
            rig.tick()
            await rig.gw.sync()
        state = await rig.store.get()
        assert state.reason == CONFIG_REJECTED and state.rejected_rev == bad
        assert rig.config() == good_files
        writes = len([d for d in fake.applied if d["metadata"]["name"] == tpl.CONFIG_SECRET])
        rig.tick()
        await rig.gw.sync()
        assert (
            len([d for d in fake.applied if d["metadata"]["name"] == tpl.CONFIG_SECRET]) == writes
        )
        assert (await rig.store.get()).reason == CONFIG_REJECTED

    run_db(settings, go)
    assert not [k for k in fake.deleted if k[1] == "Pod"]


def test_the_pause_annotation_leaves_the_gateway_deployment_alone(settings):
    fake = gateway_namespace()

    async def go(db):
        rig = await make_rig(db, fake)
        await rig.gw.sync()
        fake.objects[("v1", "Namespace", None, NS)]["metadata"]["annotations"] = {
            PAUSE_ANNOTATION: "e2e"
        }
        del fake.objects[("apps/v1", "Deployment", NS, tpl.GATEWAY)]
        rig.tick()
        await rig.gw.sync()
        assert PAUSED in (await rig.store.get()).warnings

    run_db(settings, go)
    assert ("apps/v1", "Deployment", NS, tpl.GATEWAY) not in fake.objects


def test_turning_off_clears_what_the_sync_created(settings):
    fake = gateway_namespace()

    async def go(db):
        rig = await make_rig(db, fake)
        await rig.gw.sync()
        install = await rig.ps.install_id()
        # 다른 것이 만든 같은 이름의 객체는 지우지 않는다
        fake.objects[("v1", "ConfigMap", NS, "unrelated")] = {"metadata": {"name": "unrelated"}}
        off = dataclasses.replace(rig.gw.settings, llm_gateway="off")
        rig.gw.settings = off
        assert await rig.gw.sync() == {"cleared": ["llm-gateway"]}
        state = await rig.store.get()
        assert not state.rendered and state.activated_at is None and state.providers_ready == []
        left = [k for k in fake.objects if k[2] == NS and k[1] != "Pod"]
        assert left == [("v1", "ConfigMap", NS, "unrelated")]
        assert (
            rig.obj("v1", "Namespace", NS, None)["metadata"]["labels"][LABEL_PLATFORM_ID] == install
        )
        # 다시 켜면 activated_at 이 새로 정해진다
        rig.gw.settings = dataclasses.replace(off, llm_gateway="agentgateway")
        rig.tick()
        await rig.gw.sync()
        assert (await rig.store.get()).activated_at == rig.now[0]

    run_db(settings, go)


def test_a_gateway_route_pod_keeps_the_gateway_after_turning_off(settings):
    """끈 뒤에도 게이트웨이를 부르는 파드가 남으면 렌더를 이어 간다 (6절)."""
    fake = gateway_namespace()
    fake.add_pod("daegam-acme", "agent-x", {tpl.LLM_ROUTE_LABEL: "gateway"})

    async def go(db):
        rig = await make_rig(db, fake)
        await rig.gw.sync()
        rig.gw.settings = dataclasses.replace(rig.gw.settings, llm_gateway="off")
        assert "synced" in await rig.gw.sync()

    run_db(settings, go)


def test_recorded_revocations_reach_the_listener_and_show_as_pending(settings):
    """회수는 켠 뒤에 낸 토큰의 jti 만 싣는다. 반영될 때까지 pending 이다 (5절)."""
    fake = gateway_namespace()
    gateway_pod(fake, "a")

    def rule(rig: Rig) -> str:
        config = json.loads(rig.config()["config.yaml"])
        return config["binds"][0]["listeners"][0]["policies"]["authorization"]["rules"][0]["allow"]

    async def go(db):
        rig = await make_rig(db, fake)
        await rig.gw.sync()
        revocations = rig.gw.revocations
        exp = T0 + timedelta(days=90)
        await revocations.add("before", iat=T0 - timedelta(days=1), exp=exp)
        await revocations.add("after", iat=T0 + timedelta(seconds=1), exp=exp)
        await rig.gw.sync_config()
        assert '"after": true' in rule(rig) and '"before"' not in rule(rig)
        rig.revs["a"] = rig.rev()
        rig.tick()
        await rig.gw.sync()
        state = await rig.store.get()
        assert (state.revocations, state.revocations_pending) == (1, 0)
        await revocations.add("next", iat=T0 + timedelta(seconds=2), exp=exp)
        rig.tick()
        await rig.gw.sync()
        assert (await rig.store.get()).revocations_pending == 1

    run_db(settings, go)


# -- 되돌리기가 회수를 되살리지 않는다 ---------------------------------------------------------


def _rule(rig: Rig) -> str:
    config = json.loads(rig.config()["config.yaml"])
    return config["binds"][0]["listeners"][0]["policies"]["authorization"]["rules"][0]["allow"]


@pytest.mark.parametrize("scenario", ["unreadable", "no_pods"])
def test_absent_or_unreadable_pods_do_not_restore_a_config_without_the_revocation(
    settings, scenario
):
    """노드가 재부팅되면 파드가 없거나 `/_daegam/rev` 를 읽지 못한다. 그것을 설정을 버린 것으로 보고
    되돌리면 회수한 jti 가 없는 옛 설정이 돌아와 회수한 토큰이 다시 통한다."""
    fake = gateway_namespace()
    gateway_pod(fake, "a")
    gateway_pod(fake, "b")

    async def go(db):
        rig = await make_rig(db, fake)
        await rig.gw.sync()
        first = rig.rev()
        rig.revs.update({"a": first, "b": first})
        rig.tick()
        await rig.gw.sync()
        await rig.gw.revocations.add(
            "leaked-jti", iat=T0 + timedelta(seconds=1), exp=T0 + timedelta(days=90)
        )
        await rig.gw.sync_config()
        revoked = rig.rev()
        if scenario == "unreadable":
            rig.revs.update({"a": None, "b": None})
        else:
            await asyncio.to_thread(_drop_pods, fake)
        for _ in range(4):
            rig.tick()
            await rig.gw.sync()
        state = await rig.store.get()
        assert state.rejected_rev is None and state.reason != CONFIG_REJECTED
        assert rig.rev() == revoked and '"leaked-jti": true' in _rule(rig)
        if scenario == "no_pods":
            await asyncio.to_thread(gateway_pod, fake, "a")
        rig.revs.update({"a": revoked, "b": revoked})
        rig.tick()
        await rig.gw.sync()
        state = await rig.store.get()
        assert state.applied_rev == revoked and state.revocations_pending == 0

    run_db(settings, go)


def _drop_pods(fake) -> None:
    for key in [k for k in fake.objects if k[1] == "Pod"]:
        del fake.objects[key]


def test_a_restored_config_is_retried_so_revocations_still_arrive(settings):
    """모든 파드가 옛 리비전을 알리면 되돌리고, 10분 뒤 원하는 리비전(회수 포함)을 다시 쓴다.
    느린 kubelet 을 버린 것으로 잘못 보았어도 회수가 결국 닿는다."""
    fake = gateway_namespace()
    gateway_pod(fake, "a")

    async def go(db):
        rig = await make_rig(db, fake)
        await rig.gw.sync()
        good = rig.rev()
        rig.revs["a"] = good
        rig.tick()
        await rig.gw.sync()
        await rig.gw.revocations.add(
            "leaked-jti", iat=T0 + timedelta(seconds=1), exp=T0 + timedelta(days=90)
        )
        await rig.gw.sync_config()
        for _ in range(4):
            rig.tick()
            await rig.gw.sync()
        state = await rig.store.get()
        assert state.reason == CONFIG_REJECTED and rig.rev() == good
        rig.tick(601)
        await rig.gw.sync()
        rig.tick()
        await rig.gw.sync()
        assert '"leaked-jti": true' in _rule(rig)
        rig.revs["a"] = rig.rev()
        rig.tick()
        await rig.gw.sync()
        state = await rig.store.get()
        assert state.reason is None and state.revocations_pending == 0

    run_db(settings, go)


def test_pending_revocations_compare_the_jti_sets(settings):
    """만료로 빠진 jti 와 새 jti 의 수가 같아도 새 jti 는 아직 먹지 않은 회수다."""
    fake = gateway_namespace()
    gateway_pod(fake, "a")

    async def go(db):
        rig = await make_rig(db, fake)
        await rig.gw.sync()
        exp = T0 + timedelta(days=90)
        await rig.gw.revocations.add("old", iat=T0 + timedelta(seconds=1), exp=exp)
        await rig.gw.sync_config()
        rig.revs["a"] = rig.rev()
        rig.tick()
        await rig.gw.sync()
        assert (await rig.store.get()).revocations_pending == 0
        past = utcnow() - timedelta(seconds=1)
        await rig.gw.revocations.coll.update_one({"_id": "old"}, {"$set": {"expires_at": past}})
        await rig.gw.revocations.add("new", iat=T0 + timedelta(seconds=2), exp=exp)
        rig.tick()
        await rig.gw.sync()
        state = await rig.store.get()
        assert state.revocations == 1 and state.revocations_pending == 1

    run_db(settings, go)


def test_a_key_sync_time_survives_the_tick_that_read_the_state_before_it(settings):
    async def go(db):
        rig = await make_rig(db, gateway_namespace())
        await rig.gw.sync()
        state = await rig.store.get()
        await rig.store.key_synced("openai", T0)
        await rig.store.put(state)
        assert (await rig.store.get()).key_synced == {"openai": T0}

    run_db(settings, go)


class _Tokens:
    """정리가 남은 Agent 하나를 흉내 낸다."""

    def __init__(self):
        self.settled = 0

    async def holding(self) -> bool:
        return True

    async def settle(self) -> list[str]:
        self.settled += 1
        return ["ag-1"]


@pytest.mark.parametrize("why", ["namespace_gone", "images_empty"])
def test_token_cleanup_runs_even_when_the_gateway_cannot_render(settings, why):
    """거둔 뒤(네임스페이스 없음)나 끈 채 이미지를 비운 배포에서도 롤아웃 뒤의 정리는 돈다."""
    fake = gateway_namespace()
    if why == "namespace_gone":
        del fake.objects[("v1", "Namespace", None, NS)]

    async def go(db):
        rig = await make_rig(db, fake)
        tokens = _Tokens()
        rig.gw.tokens = tokens  # type: ignore[assignment]
        if why == "images_empty":
            rig.gw.settings = dataclasses.replace(
                rig.gw.settings, llm_gateway="off", llm_gateway_images={}
            )
            await rig.store.put((await rig.store.get()).model_copy(update={"rendered": True}))
        out = await rig.gw.sync()
        assert out["llm_cleanup"] == ["ag-1"] and tokens.settled == 1

    run_db(settings, go)


def test_a_failed_apply_leaves_a_reason_and_the_next_tick_clears_it(settings):
    """적용이 실패한 틱도 까닭과 시각을 남긴다. 남기지 않으면 상태가 한 번도 맞추지 않은 것처럼
    읽힌다. 오류는 그대로 올라가 리컨실 로그에 남고, 다음 틱이 적용하면 까닭이 사라진다."""
    fake = gateway_namespace()
    real_apply = fake.apply

    def refuse(obj, **kwargs):
        if obj["kind"] == "NetworkPolicy":
            raise RuntimeError("the cluster refused the policy")
        return real_apply(obj, **kwargs)

    async def go(db):
        rig = await make_rig(db, fake)
        fake.apply = refuse
        with pytest.raises(RuntimeError, match="refused the policy"):
            await rig.gw.sync()
        state = await rig.store.get()
        assert (state.reason, state.synced_at) == (APPLY_FAILED, rig.now[0])
        # 네임스페이스는 이미 표시했으므로 끌 때 치울 수 있게 렌더한 것으로 남긴다
        assert state.rendered and state.activated_at is not None
        # 끝나지 않은 적용의 리비전은 남기지 않는다
        assert state.desired_rev is None
        fake.apply = real_apply
        rig.tick()
        assert "synced" in await rig.gw.sync()
        state = await rig.store.get()
        assert (state.reason, state.synced_at) == (None, rig.now[0])

    run_db(settings, go)
