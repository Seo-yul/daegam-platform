"""P36 F11b: 채팅 첨부의 업로드와 턴에 싣기 (D61, `services/chat_uploads.py`,
`store/chat_uploads.py`).

가짜 런타임(tests/helpers/runtime.py 의 `fake_runtime`)에 배포한 Agent 에 대고 업로드 두 라우트와
첨부 턴을 본다. 형식·크기·빈 글, 텍스트 형식은 문서 파싱 서비스를 부르지 않는 것, 남용 상한
셋(분당 20, 쓰지 않은 20, 이름 리스로 동시 추출 하나)과 거절한 업로드도 분당 칸에 세는 것, 문서
상한을 크게 넘는 본문은 읽기 전에 끊는 것, 색인, 남의 id·다른 Agent·만료가 모두 unknown_upload 인
것, 능력 이름이 없는 런타임과 Ready 가 아닌 phase 의 409 와 업로드 유지, 런타임이 스트림을 연 뒤의
삭제(지우기에 실패해도 턴은 흐른다), 입구 거절 뒤의 유지, 런타임에 싣는 모양을 든다. 문서 파싱
서비스는 진짜 서버 앱을 같은 프로세스에서 붙인다(tests/helpers/documents.py 의 `docparse_client`).
"""

from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from typing import Any

import pytest
from pymongo.errors import AutoReconnect

from daegam.body_limit import MULTIPART_SLACK
from daegam.documents.formats import MAX_DOCUMENT_BYTES
from daegam.services import chat_uploads as service_module
from daegam.services.chat_uploads import (
    MAX_UNUSED_UPLOADS,
    MAX_UPLOADS_PER_MINUTE,
    upload_lease_name,
)
from daegam.store.base import utcnow
from daegam.store.chat_uploads import CHAT_UPLOAD_RATE, CHAT_UPLOADS, ChatUpload
from daegam.store.leases import LEASES
from tests.helpers.agent_runtime import chat_upload, deployed_agent, ready_agent, set_ready
from tests.helpers.consts import AG
from tests.helpers.documents import docparse_client, make_pdf
from tests.helpers.http import detail_code
from tests.helpers.runtime import ATTACHMENT_FEATURES

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
OWNER = "header"


def _chat(client, agent_id: str, ids: list[str], message: str = "이 파일을 정리해 줘"):
    return client.post(f"{AG}/{agent_id}/chat", json={"message": message, "attachments": ids})


def _chat_calls(runtime) -> list[dict[str, Any]]:
    return [c for c in runtime.state.calls if c["path"] == "/chat"]


def _stored(mongo, **extra: Any) -> dict[str, Any]:
    """Mongo 에 바로 넣는 업로드 한 건이다. 남의 것·다른 Agent·만료를 꾸밀 때 쓴다."""
    now = utcnow()
    fields: dict[str, Any] = {
        "tenant": "default",
        "agent_id": "ag-x",
        "user_sub": OWNER,
        "name": "메모.txt",
        "kind": "document",
        "format": "text",
        "media_type": "text/plain",
        "bytes": 5,
        "sha256": "0" * 64,
        "content": b"hello",
        "created_at": now - timedelta(minutes=5),
        "expires_at": now + timedelta(hours=1),
        **extra,
    }
    doc = ChatUpload(**fields).to_doc()
    mongo[CHAT_UPLOADS].insert_one(doc)
    return doc


@pytest.fixture
def frozen_minute(monkeypatch):
    """업로드 서비스의 시계를 이번 분의 1초에 세운다. 분당 칸이 테스트 도중 넘어가지 않는다."""
    now = utcnow().replace(second=1, microsecond=0)
    monkeypatch.setattr(service_module, "utcnow", lambda: now)
    return now


def test_refused_uploads_count_toward_the_minute_and_big_bodies_are_cut_unread(
    client, fake_clusters, mongo, frozen_minute
):
    """형식·크기로 거절한 업로드도 분당 칸에 센다. 큰 파일을 거듭 보내 상한을 비켜 가지 못한다.
    본문이 문서 상한에 multipart 여유를 더한 것보다 크면 본문 상한 미들웨어가 라우트 전에 413
    payload_too_large 로 끊고 분당 칸에도 세지 않는다."""
    agent = ready_agent(client, fake_clusters, mongo)
    assert (
        detail_code(chat_upload(client, agent.id, "run.exe", b"MZ", "application/x-msdownload"))[0]
        == 422
    )
    big = b"a" * (MAX_DOCUMENT_BYTES + 1)
    assert detail_code(chat_upload(client, agent.id, "큰.txt", big)) == (413, "too_large")
    assert mongo[CHAT_UPLOAD_RATE].find_one()["count"] == 2

    huge = b"a" * (MAX_DOCUMENT_BYTES + MULTIPART_SLACK)
    assert detail_code(chat_upload(client, agent.id, "더큰.txt", huge)) == (
        413,
        "payload_too_large",
    )
    assert mongo[CHAT_UPLOAD_RATE].find_one()["count"] == 2


def test_uploads_need_a_deployed_agent(client, fake_clusters, mongo):
    """배포되지 않았으면 409 not_deployed 다. 기록에 `attachments` 가 없을 때의 409 는
    test_agent_chat_api.py 의 능력 표가 든다."""
    agent = deployed_agent(client, fake_clusters, mongo, features=ATTACHMENT_FEATURES)
    mongo["agents"].update_one({"_id": agent.id}, {"$set": {"state": "draft"}})
    assert detail_code(chat_upload(client, agent.id, "메모.txt", b"hello")) == (409, "not_deployed")


def test_per_minute_limit_counts_removed_and_failed_uploads(
    client, fake_clusters, mongo, frozen_minute
):
    """분당 20 은 지운 업로드와 추출에 실패한 업로드도 센다. 지우고 다시 업로드해 상한을 비켜 가지
    못한다. 넘으면 429 upload_rate_limited 와 Retry-After 다."""
    agent = ready_agent(client, fake_clusters, mongo)
    assert detail_code(chat_upload(client, agent.id, "빈.txt", b"   ")) == (422, "empty_document")
    for i in range(MAX_UPLOADS_PER_MINUTE - 1):
        r = chat_upload(client, agent.id, f"메모{i}.txt", b"hello")
        assert r.status_code == 201, r.text
        assert client.delete(f"{AG}/{agent.id}/uploads/{r.json()['id']}").status_code == 204
    r = chat_upload(client, agent.id, "하나더.txt", b"hello")
    assert detail_code(r) == (429, "upload_rate_limited")
    assert r.headers["retry-after"] == str(60 - frozen_minute.second)
    assert mongo[CHAT_UPLOADS].count_documents({}) == 0
    assert mongo[CHAT_UPLOAD_RATE].find_one()["count"] == MAX_UPLOADS_PER_MINUTE + 1


def test_unused_uploads_are_capped(client, fake_clusters, mongo):
    """쓰지 않은 업로드가 20 이면 429 too_many_uploads 다. 만료된 것은 TTL 이 아직 지우지 않았어도
    세지 않는다."""
    agent = ready_agent(client, fake_clusters, mongo)
    for _ in range(MAX_UNUSED_UPLOADS - 1):
        _stored(mongo, agent_id=agent.id)
    _stored(mongo, agent_id=agent.id, expires_at=utcnow() - timedelta(minutes=1))
    assert chat_upload(client, agent.id, "스물.txt", b"hello").status_code == 201
    assert detail_code(chat_upload(client, agent.id, "하나더.txt", b"hello")) == (
        429,
        "too_many_uploads",
    )


def test_one_extraction_at_a_time_per_person(client, fake_clusters, mongo):
    """그 사람의 추출이 돌고 있으면(이름 리스 `chat-upload:{sub}`) 문서는 429 upload_busy 다.
    서비스를 부르지 않는 텍스트 형식은 리스와 상관없고, 추출이 끝나면 리스가 풀린다."""
    agent = ready_agent(client, fake_clusters, mongo)
    client.app.state.chat_uploads.docparse = docparse_client()
    held = {"_id": upload_lease_name(OWNER), "holder": "other-pod/turn"}
    mongo[LEASES].insert_one({**held, "expires_at": utcnow() + timedelta(minutes=1)})
    pdf = make_pdf("refund in 7 days")
    assert detail_code(chat_upload(client, agent.id, "보고서.pdf", pdf, "application/pdf")) == (
        429,
        "upload_busy",
    )
    assert chat_upload(client, agent.id, "메모.txt", b"hello").status_code == 201

    mongo[LEASES].delete_one({"_id": upload_lease_name(OWNER)})
    assert chat_upload(client, agent.id, "보고서.pdf", pdf, "application/pdf").status_code == 201
    assert mongo[LEASES].count_documents({"_id": upload_lease_name(OWNER)}) == 0


def test_a_turn_carries_the_runtime_shape_and_consumes_the_uploads(client, fake_clusters, mongo):
    """첨부 턴은 업로드를 보낸 차례대로 런타임 모양으로 싣는다. 글은 뽑은 글과 원본의 해시,
    이미지는 base64 다. 런타임이 스트림을 열면 업로드를 지운다."""
    agent = ready_agent(client, fake_clusters, mongo)
    text = chat_upload(client, agent.id, "규정.md", "환불은 7일".encode(), "text/markdown").json()
    image = chat_upload(client, agent.id, "화면.png", PNG, "image/png").json()

    r = _chat(client, agent.id, [image["id"], text["id"]])
    assert r.status_code == 200, r.text
    sent = _chat_calls(agent.runtime)[-1]["body"]["attachments"]
    assert sent == [
        {
            "kind": "image",
            "name": "화면.png",
            "media_type": "image/png",
            "data": base64.b64encode(PNG).decode(),
        },
        {
            "kind": "text",
            "name": "규정.md",
            "format": "markdown",
            "text": "환불은 7일",
            "sha256": hashlib.sha256("환불은 7일".encode()).hexdigest(),
            "truncated": False,
        },
    ]
    assert mongo[CHAT_UPLOADS].count_documents({}) == 0
    # 첨부가 없는 턴은 그 칸을 싣지 않는다
    assert client.post(f"{AG}/{agent.id}/chat", json={"message": "안녕"}).status_code == 200
    assert "attachments" not in _chat_calls(agent.runtime)[-1]["body"]


def test_a_code_execution_agent_gets_the_originals_within_the_turn_cap(
    client, fake_clusters, mongo, monkeypatch
):
    """코드 실행 Agent 에 올린 문서는 원본도 남고 턴에 `original` 로 실린다(P40 6절). 한 턴의 원본은
    합쳐 상한까지이고, 넘는 문서는 글만 간다. 이미지는 그 자체가 원본이라 따로 싣지 않는다."""
    monkeypatch.setattr(service_module, "code_execution_on", lambda _doc: True)
    monkeypatch.setattr(service_module, "MAX_ORIGINAL_BYTES_PER_TURN", 12)
    agent = ready_agent(client, fake_clusters, mongo)
    first = chat_upload(client, agent.id, "a.csv", b"x;y\n1;2\n", "text/csv").json()
    second = chat_upload(client, agent.id, "b.csv", b"x;y\n3;4\n", "text/csv").json()
    image = chat_upload(client, agent.id, "화면.png", PNG, "image/png").json()
    stored = mongo[CHAT_UPLOADS].find_one({"_id": first["id"]})
    assert stored["original"] == b"x;y\n1;2\n"
    assert mongo[CHAT_UPLOADS].find_one({"_id": image["id"]}).get("original") is None

    r = _chat(client, agent.id, [first["id"], second["id"], image["id"]])
    assert r.status_code == 200, r.text
    sent = _chat_calls(agent.runtime)[-1]["body"]["attachments"]
    assert base64.b64decode(sent[0]["original"]) == b"x;y\n1;2\n"
    assert "original" not in sent[1] and "original" not in sent[2]
    # 상한으로 싣지 않은 원본은 알린다. 런타임의 안내 줄이 글만 남았다고 말한다
    assert sent[1]["original_dropped"] is True and "original_dropped" not in sent[0]
    assert "original_dropped" not in sent[2]


def test_unknown_foreign_and_expired_uploads_all_look_the_same(client, fake_clusters, mongo):
    """남의 업로드, 다른 Agent 의 업로드, 만료된 업로드, 없는 id 는 모두 422 unknown_upload 다.
    하나라도 그러면 런타임을 부르지 않고 같이 보낸 내 업로드도 남는다. 남의 업로드를 지우는 요청은
    내 것과 같은 204 이지만 그 업로드는 남는다."""
    agent = ready_agent(client, fake_clusters, mongo)
    mine = chat_upload(client, agent.id, "메모.txt", b"hello").json()["id"]
    foreign = [
        _stored(mongo, agent_id=agent.id, user_sub="someone-else")["_id"],
        _stored(mongo, agent_id="another-agent")["_id"],
        _stored(mongo, agent_id=agent.id, expires_at=utcnow() - timedelta(seconds=1))["_id"],
        "0" * 32,
    ]
    for other in foreign:
        r = _chat(client, agent.id, [mine, other])
        assert detail_code(r) == (422, "unknown_upload"), other
    assert _chat_calls(agent.runtime) == []
    assert mongo[CHAT_UPLOADS].count_documents({"_id": mine}) == 1
    assert client.delete(f"{AG}/{agent.id}/uploads/{foreign[0]}").status_code == 204
    assert mongo[CHAT_UPLOADS].count_documents({"_id": foreign[0]}) == 1


def test_the_turn_body_takes_at_most_five_distinct_uploads(client, fake_clusters, mongo):
    """한 턴은 다섯까지이고 같은 id 를 두 번 실을 수 없다. 둘 다 본문 검사의 422 다."""
    agent = ready_agent(client, fake_clusters, mongo)
    assert _chat(client, agent.id, [f"{i:032x}" for i in range(6)]).status_code == 422
    assert _chat(client, agent.id, ["a" * 32, "a" * 32]).status_code == 422
    assert _chat_calls(agent.runtime) == []


def test_old_or_rolling_runtimes_keep_the_uploads(client, fake_clusters, mongo):
    """기록에 `attachments` 가 없으면 409 runtime_feature_missing, phase 가 Ready 가 아니면 409
    runtime_rolling 이다. 둘 다 런타임을 부르지 않고 업로드를 남겨 다시 보낼 수 있다."""
    agent = deployed_agent(client, fake_clusters, mongo, features=ATTACHMENT_FEATURES)
    upload = chat_upload(client, agent.id, "메모.txt", b"hello").json()["id"]
    assert detail_code(_chat(client, agent.id, [upload])) == (409, "runtime_rolling")

    set_ready(agent.fake)
    mongo["agents"].update_one({"_id": agent.id}, {"$set": {"runtime.features": ["cancel"]}})
    assert detail_code(_chat(client, agent.id, [upload])) == (409, "runtime_feature_missing")
    assert _chat_calls(agent.runtime) == []
    assert mongo[CHAT_UPLOADS].count_documents({"_id": upload}) == 1


def test_an_open_stream_survives_a_failed_cleanup(client, fake_clusters, mongo, monkeypatch):
    """런타임이 스트림을 연 뒤 업로드를 지우지 못해도 턴은 그대로 흐른다. 남은 업로드는 TTL 이
    지운다."""
    agent = ready_agent(client, fake_clusters, mongo)
    upload = chat_upload(client, agent.id, "메모.txt", b"hello").json()["id"]

    async def broken(*_args, **_kwargs):
        raise AutoReconnect("mongo went away")

    monkeypatch.setattr(client.app.state.chat_uploads.store, "delete", broken)
    r = _chat(client, agent.id, [upload])
    assert r.status_code == 200 and "event: done" in r.text
    assert mongo[CHAT_UPLOADS].count_documents({"_id": upload}) == 1
