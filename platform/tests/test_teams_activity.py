"""P47 6.2절 "The activity": Microsoft 가 공개한 활동(F0.1 고정물)을 받는 경로가 읽는 꼴.

사람이 친 글, 인용의 표시, `text/html` 사본은 첨부가 아니라는 것, 봇이 추가된 대화, 명령의 꼴을
본다.
"""

from __future__ import annotations

import json

import pytest

from daegam.services.channels.bot import parse_command
from daegam.services.channels.inbound import bot_id, parse_activity, typed_text
from tests.helpers.teams import fixture


def _parse(doc: dict):
    return parse_activity(json.dumps(doc).encode())


def test_the_published_personal_message():
    a = _parse(fixture("message-personal.json"))
    assert (a.type, a.channel_id, a.conversation_type) == ("message", "msteams", "personal")
    assert a.entra_tenant_id == "72f988bf-86f1-41af-91ab-2d7cd011db47"
    assert a.from_id.startswith("29:") and a.aad_object_id == "7faf8ab2-3d56-4244-b585-20c8a42ed2b8"
    assert a.recipient_id == bot_id("c9e8c047-2a74-40a2-b28a-b162d5f5327c")
    assert a.service_url == "https://smba.trafficmanager.net/amer/"
    assert a.text == "Hello Teams TestAgent.Sending bold-italic rich text"
    # `text/html` 사본은 첨부가 아니다
    assert not a.attachments and not a.quoted


def test_a_quoted_reply_entity_marks_the_message_quoted():
    doc = fixture("message-personal.json")
    doc["entities"] = [*doc["entities"], fixture("message-quoted-reply.json")]
    assert _parse(doc).quoted
    placeholder = {**fixture("message-personal.json"), "text": '<quoted messageId="m1"/> 이거'}
    assert _parse(placeholder).quoted


def test_other_attachments_are_attachments():
    doc = fixture("message-personal.json")
    doc["attachments"].append({"contentType": "image/png", "contentUrl": "https://x"})
    assert _parse(doc).attachments


def test_the_bot_added_update_names_the_bot():
    """공개 예시의 `recipient.id` 는 자리표시자다. 봇(`28:`)과 사람(`29:`)이 함께 더해진다."""
    a = _parse(fixture("conversation-update-bot-added.json"))
    assert a.type == "conversationUpdate"
    assert [m[:3] for m in a.members_added] == ["28:", "29:"]


def test_an_installation_update_carries_its_action():
    a = _parse(fixture("installation-update-add.json"))
    assert (a.type, a.action, a.conversation_type) == ("installationUpdate", "add", "channel")


@pytest.mark.parametrize("body", [b"[1]", b"not json", b'"text"'])
def test_a_body_that_is_not_an_object_is_refused(body):
    with pytest.raises(ValueError):
        parse_activity(body)


def test_typed_text_keeps_plain_text_and_strips_markup():
    assert typed_text("연결  <ABCD2345>", "plain") == "연결 <ABCD2345>"
    assert typed_text("<p>연결&nbsp;ABCD2345</p>", "xml") == "연결 ABCD2345"


@pytest.mark.parametrize(
    ("text", "kind", "code"),
    [
        ("연결 ABCD2345", "link", "ABCD2345"),
        ("LINK abcd-2345", "link", "abcd-2345"),
        ("연결 끊기", "unlink", ""),
        ("연결끊기", "unlink", ""),
        ("Unlink", "unlink", ""),
        ("도움말", "help", ""),
        ("help me", "other", ""),
        ("연결", "other", ""),
        ("에이전트", "other", ""),
    ],
)
def test_commands_match_the_whole_message(text, kind, code):
    command = parse_command(text)
    assert (command.kind, command.code) == (kind, code)
