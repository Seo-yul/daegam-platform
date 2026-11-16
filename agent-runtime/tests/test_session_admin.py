"""소유자의 세션 관리(P36 4절, runtime/sessions_api.py)다.

제목 검사, 목록 검색어 되돌리기, 소유자만 부르는 규칙(외부 경로의 다른 sub, 승인자 헤더, 프록시
경로의 user_sub 없음), 지운 뒤 색인·전사·체크포인트·첨부가 없는 것, 진행 중 턴의 409, 승인 대기·닫힌
세션 지우기, 다 지우지 못한 지우기의 503, `session_deleted`, 노출 경로 CORS 의 PATCH·DELETE 를
본다. Redis 의 되풀이 지우기 도우미는 test_redis_store.py 가 보고, 실제 Postgres·Redis 의 지우기는
두 저장소가 있을 때만 돈다.
"""

from __future__ import annotations

from typing import Any

import pytest

from runtime import stream
from runtime.hitl import APPROVER_HEADER
from runtime.sessions import CheckpointsRemain, new_turn_token
from runtime.turn_record import SESSION_TITLE_MAX_CHARS
from tests.app_fixtures import ORIGIN, bearer, sid_of
from tests.attachment_fixtures import document, image, send
from tests.conftest import RunningApp, chat, run_app
from tests.idp_fixtures import FakeIdp, Signer, external_app
from tests.store_fixtures import real_store

OWNER = {"user_sub": "alice"}


async def _rename(running: RunningApp, sid: str, title: str, **params: Any):
    query = {**OWNER, **params}
    return await running.client.patch(f"/sessions/{sid}", params=query, json={"title": title})


async def _delete(running: RunningApp, sid: str, **params: Any):
    return await running.client.delete(f"/sessions/{sid}", params={**OWNER, **params})


async def _gone(running: RunningApp, sid: str) -> bool:
    """색인 행, 전사, 체크포인트, 첨부가 모두 없는가."""
    runtime = running.runtime
    snapshot = await runtime.agent.graph.aget_state(stream.thread_config(sid))
    return (
        await runtime.store.index.get(sid) is None
        and await runtime.store.transcripts.read(sid) == []
        and await runtime.store.uploads.read(sid) == []
        and not snapshot.values
    )


async def test_rename_cleans_the_title_and_refuses_an_empty_or_long_one(support_doc) -> None:
    """공백이 아닌 제어·서식 문자를 빼고 공백을 줄여 NFC 로 맞춘 1~100자다. 닫힌 세션도 이름을
    바꾼다."""
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "환불 기한")
        sid = sid_of(sse)
        # 사이에 낀 폭 없는 공백(Cf)과 줄바꿈이 빠진다. 공백 사이에 끼어도 공백 두 칸이 남지 않는다
        for raw in ("  새\u200b 이름\n  ", "새 \u200b 이름"):
            response = await _rename(running, sid, raw)
            assert response.status_code == 200, response.text
            assert response.json()["session"]["title"] == "새 이름"
        # 자모로 풀어 쓴 "한" 은 NFC 로 한 글자가 된다
        response = await _rename(running, sid, "\u1112\u1161\u11ab")
        assert response.json()["session"]["title"] == "한"
        for bad in ("   ", "\u200b\u202e", "가" * (SESSION_TITLE_MAX_CHARS + 1)):
            response = await _rename(running, sid, bad)
            assert response.status_code == 422 and response.json()["code"] == "invalid_title"
        assert (await running.runtime.store.index.get(sid)).title == "한"

        assert await running.runtime.store.sweep(0) == [sid]
        response = await _rename(running, sid, "닫힌 대화")
        assert response.status_code == 200 and response.json()["session"]["status"] == "closed"


async def test_list_echoes_the_search_query(support_doc) -> None:
    """목록은 받은 검색어 q 를 응답의 q 로 되돌려준다. 플랫폼은 이 칸으로 q 를 모르는 옛 파드를
    가른다. q 가 없으면 칸도 없다."""
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "환불 기한")
        sid = sid_of(sse)
        listed = (await running.client.get("/sessions", params={**OWNER, "q": "환불"})).json()
        assert listed["q"] == "환불" and [s["session_id"] for s in listed["sessions"]] == [sid]
        assert "q" not in (await running.client.get("/sessions", params=OWNER)).json()


async def test_only_the_owner_renames_or_deletes(support_doc) -> None:
    """승인자 헤더도 통하지 않는다. 프록시 경로에 user_sub 가 없으면 422, 없는 세션은 404 다."""
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "내 대화")
        sid = sid_of(sse)
        approver = {APPROVER_HEADER: "1"}
        response = await running.client.patch(
            f"/sessions/{sid}", params={"user_sub": "bob"}, json={"title": "남"}, headers=approver
        )
        assert response.status_code == 403 and response.json()["code"] == "not_session_owner"
        response = await running.client.delete(
            f"/sessions/{sid}", params={"user_sub": "bob"}, headers=approver
        )
        assert response.status_code == 403
        response = await running.client.patch(f"/sessions/{sid}", json={"title": "주체 없음"})
        assert response.status_code == 422 and response.json()["code"] == "user_sub_required"
        assert (await running.client.delete(f"/sessions/{sid}")).status_code == 422
        response = await _delete(running, "missing")
        assert response.status_code == 404 and response.json()["code"] == "session_not_found"
        assert (await running.runtime.store.index.get(sid)).title == "내 대화"


async def test_external_caller_manages_only_its_own_sessions(support_doc) -> None:
    """외부 경로의 주체는 토큰 sub 다. 쿼리 user_sub 로 남의 세션을 고르지 못한다."""
    signer = Signer()
    async with external_app(support_doc, FakeIdp(signer)) as running:
        alice = bearer(signer.token(sub="alice"))
        bob = bearer(signer.token(sub="bob"))
        _, sse = await chat(running, "앨리스의 대화", headers=alice)
        sid = sid_of(sse)
        response = await running.client.patch(
            f"/sessions/{sid}", params=OWNER, json={"title": "가로채기"}, headers=bob
        )
        assert response.status_code == 403 and response.json()["code"] == "not_session_owner"
        assert (await running.client.delete(f"/sessions/{sid}", headers=bob)).status_code == 403
        response = await running.client.patch(
            f"/sessions/{sid}", json={"title": "앨리스가 바꿈"}, headers=alice
        )
        assert response.status_code == 200
        assert (await running.client.delete(f"/sessions/{sid}", headers=alice)).status_code == 200
        assert await _gone(running, sid)


async def test_delete_removes_the_session_and_leaves_an_event(support_doc) -> None:
    """체크포인트, 전사, 색인 행이 사라지고 목록·상세에서도 없다. 관측 이벤트는 사람과 세션만
    싣는다."""
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "지울 대화")
        sid = sid_of(sse)
        response = await _delete(running, sid)
        assert response.status_code == 200
        assert response.json() == {"session_id": sid, "deleted": True}
        assert await _gone(running, sid)
        assert (await running.client.get(f"/sessions/{sid}")).status_code == 404
        listed = (await running.client.get("/sessions", params=OWNER)).json()["sessions"]
        assert sid not in [row["session_id"] for row in listed]
        deleted = running.observed("session_deleted")
        assert len(deleted) == 1
        assert {k: deleted[0][k] for k in ("session_id", "user_sub")} == {
            "session_id": sid,
            "user_sub": "alice",
        }
        assert (await _delete(running, sid)).status_code == 404


async def test_delete_waits_for_a_running_turn(support_doc) -> None:
    """도는 턴이 리스를 쥐고 있으면 409 이고 아무것도 지우지 않는다. 리스가 풀리면 지운다."""
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "도는 턴")
        sid = sid_of(sse)
        index = running.runtime.store.index
        token = new_turn_token()
        assert await index.claim_turn(sid, lease_seconds=60, token=token)
        response = await _delete(running, sid)
        assert response.status_code == 409 and response.json()["code"] == "turn_in_progress"
        assert await index.get(sid) is not None
        await index.release_turn(sid, token)
        assert (await _delete(running, sid)).status_code == 200


async def test_pending_and_closed_sessions_are_deleted(support_doc) -> None:
    """승인 대기 세션은 리스가 없으므로 지워지고, 닫힌 세션도 남은 기록까지 지운다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    async with run_app(support_doc) as running:
        _, pending = await chat(running, '{"tool": "Add", "args": {"a": 1, "b": 2}}')
        assert pending[-1][0] == "interrupt"
        sid = sid_of(pending)
        assert (await running.runtime.store.index.get(sid)).status == "pending"
        assert (await _delete(running, sid)).status_code == 200
        assert await _gone(running, sid)

        _, sse = await chat(running, "닫힐 대화")
        closed = sid_of(sse)
        assert await running.runtime.store.sweep(0) == [closed]
        assert (await _delete(running, closed)).status_code == 200
        assert await _gone(running, closed)


async def test_cors_lets_the_browser_rename_and_delete(support_doc) -> None:
    """노출 경로의 preflight 가 PATCH·DELETE 를 허락한다."""
    async with external_app(support_doc, FakeIdp(Signer()), cors_origins=(ORIGIN,)) as running:
        for method in ("PATCH", "DELETE"):
            preflight = await running.client.options(
                "/sessions/s1",
                headers={
                    "Origin": ORIGIN,
                    "Access-Control-Request-Method": method,
                    "Access-Control-Request-Headers": "authorization, content-type",
                },
            )
            assert preflight.status_code in (200, 204)
            allowed = preflight.headers["Access-Control-Allow-Methods"].split(",")
            assert {"PATCH", "DELETE"} <= {m.strip() for m in allowed}


async def test_an_unfinished_delete_is_503_and_can_be_repeated(support_doc) -> None:
    """체크포인트가 남아 다 지우지 못하면 503 session_delete_incomplete 다. 색인 행과 리스가 남지
    않아 다시 지우면 끝까지 지운다."""
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "긴 대화")
        sid = sid_of(sse)
        store = running.runtime.store

        async def stuck(session_id: str) -> None:
            raise CheckpointsRemain(session_id)

        store.delete_thread = stuck
        response = await _delete(running, sid)
        assert response.status_code == 503
        assert response.json()["code"] == "session_delete_incomplete"
        assert await store.index.get(sid) is not None
        store.delete_thread = None
        assert (await _delete(running, sid)).status_code == 200
        assert await _gone(running, sid)


@pytest.mark.parametrize("kind", ["postgres", "redis"])
async def test_real_store_delete_leaves_nothing_of_the_session(support_doc, kind) -> None:
    """실제 저장소에서도 지운 세션의 체크포인트·첨부·전사·색인 행이 남지 않는다. 다른 세션은
    그대로다."""
    async with (
        real_store(kind, support_doc) as store,
        run_app(support_doc, store=store) as running,
    ):
        _, kept = await chat(running, "남길 대화")
        _, sse = await send(running, [document(), image()], "지울 대화")
        sid = sid_of(sse)
        assert len(await store.uploads.read(sid)) == 2
        _, sse = await chat(running, "둘째 턴", session_id=sid)
        assert sse[-1][0] == "done"
        assert (await _delete(running, sid)).status_code == 200
        assert await _gone(running, sid)
        assert await store.index.get(sid_of(kept)) is not None
        await _delete(running, sid_of(kept))
