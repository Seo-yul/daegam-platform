"""P47 6.2절·6.3절: Teams 메시지 엔드포인트와 연결.

local 모드 플랫폼에 Microsoft 쪽 셋(키 묶음, Entra, Connector)을 `FakeMicrosoft` 로 붙여 본다.
토큰과 활동의 거절, 막힌 호출의 버킷, 연결 전의 고정 줄과 멤버 확인, 코드로 연결하기(이메일이 같을
때와 사람이 확인할 때), 틀린 코드의 잠금, 다시 온 활동, 연결된 사람의 명령, 바뀐 연결의 알림을 본다.
코드와 메시지 글은 감사에 남지 않는다.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest
from fastapi.testclient import TestClient

from daegam.services.channels.bot import MESSAGES_PER_MINUTE, REFUSED_PER_MINUTE
from daegam.services.channels.texts import KO, text
from tests.helpers.auth import LOCAL_BOOT_PASSWORD, bearer, login, settle
from tests.helpers.http import api_client, detail_code, make_app
from tests.helpers.oidc import Signer
from tests.helpers.teams import (
    APP_ID,
    SERVICE_URL,
    TENANT_ID,
    FakeMicrosoft,
    TeamsMember,
    attach,
    configure_teams,
    connector_token,
    fixture,
    personal_message,
)

MESSAGES = "/channels/teams/messages"
CONV = "a:conv-1"
FROM = "29:user-1"
AAD = "aad-1"
ADMIN_EMAIL = "admin@example.com"


@pytest.fixture
def fake() -> FakeMicrosoft:
    f = FakeMicrosoft(Signer(None))
    f.members[(CONV, FROM)] = TeamsMember(FROM, AAD, email=ADMIN_EMAIL, upn=ADMIN_EMAIL)
    return f


@pytest.fixture
def teams(local_admin: TestClient, mongo, fake_secrets, fake) -> TestClient:
    configure_teams(mongo, fake_secrets)
    mongo["users"].update_one({"username": "admin"}, {"$set": {"email": ADMIN_EMAIL}})
    attach(local_admin.app, fake)
    return local_admin


def send(c: TestClient, fake: FakeMicrosoft, activity: dict, *, token: str | None = None):
    token = token or connector_token(fake.signer, service_url=activity.get("serviceUrl"))
    return c.post(MESSAGES, json=activity, headers={"Authorization": f"Bearer {token}"})


def say(c: TestClient, fake: FakeMicrosoft, words: str, **kw) -> None:
    r = send(c, fake, personal_message(words, **kw))
    assert r.status_code == 200, r.text


def make_code(c: TestClient) -> str:
    r = c.post("/me/channels/teams/link-code", json={"password": LOCAL_BOOT_PASSWORD})
    assert r.status_code == 201, r.text
    return r.json()["code"]


def link(c: TestClient, fake: FakeMicrosoft, **kw) -> None:
    say(c, fake, f"연결 {make_code(c)}", **kw)


# -- 거절 ------------------------------------------------------------------------------------


def test_no_bot_means_404(local_admin, fake):
    attach(local_admin.app, fake)
    r = send(local_admin, fake, personal_message("hi"))
    assert detail_code(r) == (404, "not_found")


def test_channels_off_and_header_mode_are_404(local_settings, mongo, fake_clusters, fake_secrets):
    configure_teams(mongo, fake_secrets)
    off = dataclasses.replace(local_settings, channels_enabled=False)
    header = dataclasses.replace(local_settings, auth_mode="header")
    for settings in (off, header):
        with api_client(make_app(settings, fake_clusters, fake_secrets)) as c:
            assert c.post(MESSAGES, json={}).status_code == 404
            assert c.get("/me/channels").status_code == 404
            assert c.get("/settings/channels/teams/links").status_code == 404


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"token": "not-a-jwt"}, 401),
        ({"aud": "another-app"}, 401),
        ({"claim_service_url": "https://smba.trafficmanager.net/emea/"}, 401),
        ({"tenant": "00000000-0000-0000-0000-000000000000"}, 403),
        ({"recipient": {"id": "28:another-bot"}}, 403),
        ({"channelId": "webchat"}, 403),
    ],
)
def test_refused_activities(teams, fake, change, status):
    activity = personal_message("hi", tenant=change.get("tenant", TENANT_ID))
    for key in ("recipient", "channelId"):
        if key in change:
            activity[key] = change[key]
    token = change.get("token") or connector_token(
        fake.signer,
        service_url=change.get("claim_service_url", SERVICE_URL),
        aud=change.get("aud", APP_ID),
    )
    r = send(teams, fake, activity, token=token)
    assert r.status_code == status
    assert fake.posted == []


def test_a_key_without_the_teams_endorsement_is_403(teams, fake):
    fake.endorsements = ["webchat"]
    assert send(teams, fake, personal_message("hi")).status_code == 403


def test_an_unreachable_key_set_is_503_not_401(teams, fake):
    fake.keys_down = True
    assert detail_code(send(teams, fake, personal_message("hi"))) == (503, "idp_unavailable")


def test_body_limits_and_shape(teams, fake):
    token = connector_token(fake.signer)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    big = b'{"text": "' + b"x" * (256 * 1024) + b'"}'
    assert teams.post(MESSAGES, content=big, headers=headers).status_code == 413
    assert detail_code(teams.post(MESSAGES, content=b"[1]", headers=headers)) == (
        422,
        "invalid_value",
    )


def test_refused_calls_are_counted_then_429(teams, fake, mongo):
    activity = personal_message("hi")
    codes = [
        send(teams, fake, activity, token="bad").status_code
        for _ in range(int(REFUSED_PER_MINUTE) + 1)
    ]
    assert codes[:-1] == [401] * int(REFUSED_PER_MINUTE) and codes[-1] == 429
    row = mongo["audit_events"].find_one({"action": "teams_refused"})
    assert row["count"] == REFUSED_PER_MINUTE and row["error_code"] == "invalid_token"
    # Microsoft 의 토큰은 버킷을 쓰지 않는다
    assert send(teams, fake, activity).status_code == 200


# -- 연결 전 ---------------------------------------------------------------------------------


def test_an_unlinked_member_gets_the_link_steps_once_per_ten_minutes(teams, fake):
    say(teams, fake, "hi")
    say(teams, fake, "hello")
    assert fake.texts(CONV) == [KO["bot.welcome"]]
    member_reads = [r for r in fake.requests if r.method == "GET" and "/members/" in str(r.url)]
    assert len(member_reads) == 1


def test_a_non_member_gets_nothing(teams, fake):
    say(teams, fake, "hi", from_id="29:stranger", aad="aad-x")
    say(teams, fake, "도움말", from_id="29:stranger", aad="aad-x")
    assert fake.posted == []


def test_an_unlinked_member_may_ask_for_help(teams, fake):
    say(teams, fake, "Help")
    assert fake.texts(CONV) == [KO["bot.help"]]


def test_group_chats_get_one_fixed_line(teams, fake):
    group = {"conversationType": "groupChat", "id": "19:group@thread.v2"}
    fake.members[("19:group@thread.v2", FROM)] = TeamsMember(FROM, AAD)
    activity = personal_message("hi", activity_id="g1")
    activity["conversation"] = group
    send(teams, fake, activity)
    activity = personal_message("hi again", activity_id="g2")
    activity["conversation"] = group
    send(teams, fake, activity)
    assert fake.texts("19:group@thread.v2") == [KO["bot.personalOnly"]]


def test_the_bot_added_to_a_personal_chat_sends_the_link_steps(teams, fake):
    added = fixture("conversation-update-bot-added.json")
    added.update(
        {
            "serviceUrl": SERVICE_URL,
            "from": {"id": FROM, "aadObjectId": AAD},
            "conversation": {"conversationType": "personal", "id": CONV},
            "recipient": {"id": f"28:{APP_ID}"},
            "channelData": {"tenant": {"id": TENANT_ID}},
            "membersAdded": [{"id": f"28:{APP_ID}"}, {"id": FROM, "aadObjectId": AAD}],
        }
    )
    assert send(teams, fake, added).status_code == 200
    assert fake.texts(CONV) == [KO["bot.welcome"]]


def test_removing_the_bot_forgets_the_conversation(teams, fake, mongo):
    say(teams, fake, "hi")
    assert mongo["channel_conversations"].find_one({"_id": CONV})
    removed = personal_message("", type="installationUpdate", action="remove")
    assert send(teams, fake, removed).status_code == 200
    assert mongo["channel_conversations"].find_one({"_id": CONV}) is None


# -- 연결 -------------------------------------------------------------------------------------


def test_a_code_links_at_once_when_the_address_is_the_persons_email(teams, fake, mongo):
    link(teams, fake)
    assert fake.texts(CONV) == [text("bot.linked", "ko", username="admin")]
    mine = teams.get("/me/channels").json()["teams"]
    assert mine["pending"] is None and mine["code_expires_at"] is None
    got = mine["link"]
    assert (got["teams_name"], got["address"], got["language"]) == (
        "Teams User",
        ADMIN_EMAIL,
        "ko",
    )
    stored = mongo["channel_links"].find_one({})
    assert stored["teams"]["from_id"] == FROM and stored["teams"]["conversation_id"] == CONV
    assert (stored["ends_at"] - stored["linked_at"]).days == teams.app.state.settings.pat_max_days
    assert mongo["channel_link_codes"].count_documents({}) == 0
    assert mongo["audit_events"].find_one({"action": "teams_linked"})["via"] == "teams"


def test_another_address_waits_for_the_persons_confirmation(teams, fake):
    fake.members[(CONV, FROM)] = TeamsMember(FROM, AAD, upn="someone@example.com")
    link(teams, fake)
    assert fake.texts(CONV) == [KO["bot.pendingConfirm"]]
    mine = teams.get("/me/channels").json()["teams"]
    assert mine["link"] is None and mine["pending"]["address"] == "someone@example.com"
    r = teams.post("/me/channels/teams/confirm", json={"accept": True})
    assert r.status_code == 200 and r.json()["link"]["address"] == "someone@example.com"
    assert fake.texts(CONV)[-1] == text("bot.linked", "ko", username="admin")
    assert detail_code(teams.post("/me/channels/teams/confirm", json={"accept": True})) == (
        404,
        "not_found",
    )


def test_a_refused_confirmation_drops_the_pending_account(teams, fake):
    fake.members[(CONV, FROM)] = TeamsMember(FROM, AAD, upn="someone@example.com")
    link(teams, fake)
    r = teams.post("/me/channels/teams/confirm", json={"accept": False})
    assert r.status_code == 200 and r.json() == {
        "link": None,
        "pending": None,
        "code_expires_at": None,
        "taken": None,
    }


def test_a_code_works_once_and_a_new_code_replaces_the_old(teams, fake):
    first = make_code(teams)
    second = make_code(teams)
    say(teams, fake, f"link {first}")
    assert fake.texts(CONV)[-1] == KO["bot.codeInvalid"]
    say(teams, fake, f"연결 {second.lower()}")
    assert fake.texts(CONV)[-1] == text("bot.linked", "ko", username="admin")


def test_five_wrong_codes_block_codes_for_an_hour(teams, fake):
    for _ in range(5):
        say(teams, fake, "연결 AAAAAAAA")
    texts = fake.texts(CONV)
    assert texts == [KO["bot.codeInvalid"]] * 4 + [KO["bot.codeBlocked"]]
    say(teams, fake, f"연결 {make_code(teams)}")
    assert fake.texts(CONV)[-1] == KO["bot.codeBlocked"]


def test_a_redelivered_activity_is_handled_once(teams, fake):
    activity = personal_message("도움말", activity_id="same")
    assert send(teams, fake, activity).status_code == 200
    assert send(teams, fake, activity).status_code == 200
    assert fake.texts(CONV) == [KO["bot.help"]]


def test_a_linked_person_s_commands(teams, fake, mongo):
    link(teams, fake)
    quoted = personal_message("이거 봐", entities=[fixture("message-quoted-reply.json")])
    image = personal_message("사진", attachments=[{"contentType": "image/png", "contentUrl": "x"}])
    for activity in (quoted, image, personal_message("안녕"), personal_message("도움말")):
        assert send(teams, fake, activity).status_code == 200
    assert fake.texts(CONV)[1:] == [
        KO["bot.quoted"],
        KO["bot.attachments"],
        KO["bot.noAgents"],
        KO["bot.help"],
    ]
    say(teams, fake, "연결 끊기")
    assert fake.texts(CONV)[-1] == KO["bot.unlinked"]
    assert mongo["channel_links"].count_documents({}) == 0
    assert mongo["audit_events"].find_one({"action": "teams_unlinked"})


def test_the_language_choice_changes_the_bots_lines(teams, fake):
    link(teams, fake)
    r = teams.patch("/me/channels/teams", json={"language": "en"})
    assert r.status_code == 200 and r.json()["link"]["language"] == "en"
    say(teams, fake, "help")
    assert fake.texts(CONV)[-1] == text("bot.help", "en")


def test_a_message_from_another_conversation_is_not_the_link(teams, fake):
    """연결의 네 값이 모두 같아야 한다. 대화가 다르면 연결 전과 같고, 멤버가 아니면 아무것도
    보내지 않는다."""
    link(teams, fake)
    say(teams, fake, "도움말", conversation="a:other")
    assert fake.texts("a:other") == []
    assert len(fake.texts(CONV)) == 1


def test_linking_another_teams_account_tells_the_old_chat(teams, fake):
    link(teams, fake)
    fake.members[("a:conv-2", "29:user-2")] = TeamsMember(
        "29:user-2", "aad-2", email=ADMIN_EMAIL, name="Second"
    )
    link(teams, fake, from_id="29:user-2", aad="aad-2", conversation="a:conv-2")
    assert fake.texts(CONV)[-1] == KO["bot.replaced"]
    assert fake.texts("a:conv-2") == [text("bot.linked", "ko", username="admin")]
    got = teams.get("/me/channels").json()["teams"]["link"]
    assert got["replaced_name"] == "Teams User" and got["replaced_at"] is not None


def test_too_many_messages_are_dropped_with_one_line(teams, fake):
    link(teams, fake)
    for i in range(int(MESSAGES_PER_MINUTE) + 2):
        say(teams, fake, f"m{i}")
    texts = fake.texts(CONV)
    assert texts.count(KO["bot.rateLimited"]) == 1
    assert texts.count(KO["bot.noAgents"]) == int(MESSAGES_PER_MINUTE) - 1


def test_codes_and_message_text_never_reach_the_audit(teams, fake, mongo, caplog):
    caplog.set_level(logging.DEBUG)
    code = make_code(teams)
    say(teams, fake, "비밀 이야기")
    say(teams, fake, f"연결 {code}")
    rows = list(mongo["audit_events"].find({}))
    blob = repr(rows) + caplog.text
    assert code not in blob and "비밀 이야기" not in blob


# -- 경계에서 틀리기 쉬운 자리 -----------------------------------------------------------------


def _kim(c: TestClient, mongo) -> dict[str, str]:
    """두 번째 사람 kim 의 제어면 헤더."""
    body = {"username": "kim", "password": "kim-password-1234", "email": "kim@example.com"}
    assert c.post("/users", json=body).status_code == 201
    settle(mongo, "kim")
    r = login(c, "kim", "kim-password-1234")
    return bearer(r.json()["access_token"])


def test_a_teams_user_moving_to_another_person_tells_the_one_who_lost_it(teams, fake, mongo):
    """Teams 사용자 Y 가 admin 에서 kim 에게 옮겨 가면 admin 은 계정 메뉴에서 그것을 보고, 감사 행이
    남으며, 앞 대화가 새 대화와 다르면 그 대화에 한 줄이 간다."""
    link(teams, fake)
    kim = _kim(teams, mongo)
    r = teams.post(
        "/me/channels/teams/link-code", json={"password": "kim-password-1234"}, headers=kim
    )
    code = r.json()["code"]
    # Y 가 다른 1:1 대화(앱을 다시 설치한 대화)에서 kim 의 코드를 보낸다. 주소가 kim 의 것이 아니라
    # kim 이 계정 메뉴에서 받는다
    fake.members[("a:conv-9", FROM)] = TeamsMember(FROM, AAD, email=ADMIN_EMAIL)
    say(teams, fake, f"연결 {code}", conversation="a:conv-9")
    assert fake.texts("a:conv-9") == [KO["bot.pendingConfirm"]]
    r = teams.post("/me/channels/teams/confirm", json={"accept": True}, headers=kim)
    assert r.status_code == 200 and r.json()["link"]["username"] == "kim"
    assert fake.texts("a:conv-9")[-1] == text("bot.linked", "ko", username="kim")
    assert fake.texts(CONV)[-1] == KO["bot.relinked"]
    mine = teams.get("/me/channels").json()["teams"]
    assert mine["link"] is None and mine["taken"]["teams_name"] == "Teams User"
    row = mongo["audit_events"].find_one({"action": "teams_links_cut", "error_code": "replaced"})
    assert row is not None and row["resource_id"]
    # 다음 연결 끊기가 표시를 지운다
    teams.delete("/me/channels/teams")
    assert teams.get("/me/channels").json()["teams"]["taken"] is None


def test_the_same_conversation_moving_to_another_person_gets_no_extra_line(teams, fake, mongo):
    link(teams, fake)
    kim = _kim(teams, mongo)
    r = teams.post(
        "/me/channels/teams/link-code", json={"password": "kim-password-1234"}, headers=kim
    )
    say(teams, fake, f"연결 {r.json()['code']}")
    teams.post("/me/channels/teams/confirm", json={"accept": True}, headers=kim)
    assert KO["bot.relinked"] not in fake.texts(CONV)
    assert teams.get("/me/channels").json()["teams"]["taken"] is not None


def test_a_remembered_member_read_does_not_vouch_for_another_object_id(teams, fake, mongo):
    """멤버 읽기를 기억한 뒤 같은 `from.id` 와 대화로 다른 `aadObjectId` 를 실은 코드는 받지
    않는다."""
    say(teams, fake, "hi")
    code = make_code(teams)
    say(teams, fake, f"연결 {code}", aad="aad-other")
    assert mongo["channel_links"].count_documents({}) == 0
    assert fake.texts(CONV) == [KO["bot.welcome"]]
    # 코드는 쓰이지 않았고 맞는 계정이 보내면 연결된다
    say(teams, fake, f"연결 {code}")
    assert mongo["channel_links"].find_one({})["teams"]["aad_object_id"] == AAD


def test_a_member_read_without_an_object_id_is_not_a_member(teams, fake, mongo):
    fake.members[(CONV, FROM)] = TeamsMember(FROM, "", email=ADMIN_EMAIL)
    say(teams, fake, f"연결 {make_code(teams)}")
    assert fake.posted == [] and mongo["channel_links"].count_documents({}) == 0


def test_a_non_member_is_not_read_again_for_a_minute(teams, fake):
    for i in range(3):
        say(teams, fake, f"hi {i}", from_id="29:stranger", aad="aad-x")
    reads = [r for r in fake.requests if r.method == "GET" and "/members/" in str(r.url)]
    assert len(reads) == 1


def test_bot_added_updates_share_the_message_rate(teams, fake):
    added = personal_message("", type="conversationUpdate")
    added["membersAdded"] = [{"id": f"28:{APP_ID}"}]
    for _ in range(int(MESSAGES_PER_MINUTE) + 3):
        assert send(teams, fake, added).status_code == 200
    reads = [r for r in fake.requests if r.method == "GET" and "/members/" in str(r.url)]
    assert len(reads) == 1


def test_a_trailing_slash_of_the_service_url_is_not_a_difference(teams, fake):
    activity = personal_message("도움말", service_url="https://smba.trafficmanager.net/amer")
    token = connector_token(fake.signer, service_url="https://smba.trafficmanager.net/amer/")
    assert send(teams, fake, activity, token=token).status_code == 200
    assert fake.texts(CONV) == [KO["bot.help"]]


def test_a_code_typed_inside_the_brackets_of_the_steps_works(teams, fake):
    say(teams, fake, f"연결 <{make_code(teams)}>")
    assert fake.texts(CONV) == [text("bot.linked", "ko", username="admin")]


def test_the_endpoint_answers_before_its_reply_is_posted(teams, fake, monkeypatch):
    """받은 기록을 넣은 뒤 200 으로 답하고 고정 줄은 답한 뒤에 올린다."""
    order: list[str] = []
    bot = teams.app.state.teams_bot
    real = bot._message

    async def message(a, settings):
        order.append("reply")
        await real(a, settings)

    monkeypatch.setattr(bot, "_message", message)
    original = bot.receive

    async def receive(*args, **kwargs):
        follow = await original(*args, **kwargs)
        order.append("answer")
        return follow

    monkeypatch.setattr(bot, "receive", receive)
    say(teams, fake, "도움말")
    assert order == ["answer", "reply"]
