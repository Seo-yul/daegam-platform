"""Teams 봇의 글 (P47 6.9절, 14절).

봇의 고정 줄, 카드, 앱 매니페스트의 글은 플랫폼이 지어 보내는 서버 글이라(D67 "server text is
content") 화면의 사전(`ui/src/i18n/`)이 아니라 여기 있다. 한국어가 기본이고 연결마다 영어를 고를 수
있다. 두 언어가 같은 열쇠와 같은 자리표시자를 갖는지는 `tests/test_channel_texts.py` 가 본다.

자리표시자는 `{이름}` 이고, 한국어에서 그 뒤에 조사가 붙으면 `{이름|을/를}` 이다. 조사는 값의
끝소리로 `particle.py` 가 고른다. 봇의 글은 Teams 가 Markdown 으로 그리므로 넣는 값(사용자 이름,
Agent 이름)의 Markdown 글자를 역슬래시로 막는다. 주소를 넣는 `{link}` 만 그대로 넣는다.
"""

from __future__ import annotations

import re

from daegam.services.channels.particle import particle
from daegam.store.channels import Language

LANGUAGES: tuple[Language, ...] = ("ko", "en")
DEFAULT_LANGUAGE: Language = "ko"

# {이름} 또는 {이름|조사짝}
PLACEHOLDER = re.compile(r"\{([a-zA-Z]+)(?:\|([^{}]+))?\}")
# Teams 의 Markdown 이 꾸밈으로 읽는 글자
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~])")
# 그대로 넣는 자리표시자. 플랫폼이 지은 주소다
RAW_VALUES = frozenset({"link"})

KO: dict[str, str] = {
    "manifest.name": "대감",
    "manifest.short": "대감의 Agent 와 Teams 에서 대화합니다.",
    "manifest.full": (
        "대감 계정과 연결한 뒤, 채팅할 수 있는 Agent 를 선택해 질문하고 Agent 의 승인 요청에 "
        "답합니다."
    ),
    "manifest.commands.link": "연결",
    "manifest.commands.link.description": "대감 계정과 연결합니다",
    "manifest.commands.unlink": "연결 끊기",
    "manifest.commands.unlink.description": "연결을 끊습니다",
    "manifest.commands.agents": "에이전트",
    "manifest.commands.agents.description": "Agent 를 선택합니다",
    "manifest.commands.new": "새 대화",
    "manifest.commands.new.description": "새 대화를 시작합니다",
    "manifest.commands.stop": "중지",
    "manifest.commands.stop.description": "답하는 중인 턴을 멈춥니다",
    "manifest.commands.help": "도움말",
    "manifest.commands.help.description": "명령을 봅니다",
    "bot.welcome": (
        "대감의 Agent 를 Teams 에서 사용하려면 먼저 대감 계정과 연결해야 합니다. "
        "대감의 계정 메뉴 > Teams 연결에서 코드를 만든 뒤 여기에 `연결 <코드>` 를 보내 주세요."
    ),
    "bot.help": (
        "사용할 수 있는 명령입니다. "
        "연결 <코드>, 연결 끊기, 에이전트, 새 대화, 중지, 도움말. "
        "그 밖의 메시지는 선택한 Agent 에게 보냅니다."
    ),
    "bot.pendingConfirm": (
        "대감의 계정 메뉴 > Teams 연결에서 이 Teams 계정이 본인의 것인지 확인해 주세요. "
        "10분 안에 확인해야 연결됩니다."
    ),
    "bot.linked": (
        "대감 계정({username})에 연결했습니다. `에이전트` 를 보내 사용할 Agent 를 선택해 주세요."
    ),
    "bot.codeInvalid": (
        "코드가 맞지 않거나 기한이 지났습니다. 대감에서 새 코드를 만들어 다시 보내 주세요."
    ),
    "bot.codeBlocked": "틀린 코드를 여러 번 보내 1시간 동안 연결할 수 없습니다.",
    "bot.replaced": (
        "대감 계정의 연결이 다른 Teams 계정으로 바뀌어 이 채팅에서는 더 사용할 수 없습니다."
    ),
    "bot.relinked": "이 Teams 계정은 이제 다른 대감 계정에 연결되어 있습니다.",
    "bot.unlinked": "대감 계정과의 연결을 끊었습니다.",
    "bot.linkEnded": "연결 기한이 지났습니다. 대감에서 새 코드를 만들어 다시 연결해 주세요.",
    "bot.pickAgent": "사용할 Agent 를 선택해 주세요.",
    "bot.noAgents": (
        "Teams 에서 사용할 수 있는 Agent 가 없습니다. "
        "Agent 를 관리하는 사람에게 Teams 사용을 켜 달라고 요청해 주세요."
    ),
    "bot.agentChosen": "{agent|을/를} 선택했습니다. 메시지를 보내 주세요.",
    "bot.agentGone": (
        "선택한 Agent 를 더는 Teams 에서 사용할 수 없습니다. 다른 Agent 를 선택해 주세요."
    ),
    "bot.newSession": "새 대화를 시작합니다.",
    "bot.sessionClosed": "앞 대화가 끝나 새 대화를 시작합니다.",
    "bot.busy": "앞 메시지에 답하는 중입니다. 답변이 끝난 뒤 다시 보내거나 `중지` 를 보내 주세요.",
    "bot.waitingApproval": "승인 요청에 먼저 답해 주세요.",
    "bot.tooLong": "메시지가 이 Agent 가 받을 수 있는 길이보다 깁니다. 줄여서 다시 보내 주세요.",
    "bot.quoted": "인용하거나 전달한 메시지는 보낼 수 없습니다. 필요한 내용을 직접 입력해 주세요.",
    "bot.attachments": "첨부 파일은 받을 수 없습니다. 내용을 메시지로 보내 주세요.",
    "bot.personalOnly": "대감은 1:1 채팅에서만 사용할 수 있습니다.",
    "bot.rateLimited": "메시지를 너무 많이 보냈습니다. 1분 뒤에 다시 보내 주세요.",
    "bot.waking": "Agent 를 깨우는 중입니다. 잠시만 기다려 주세요.",
    "bot.stopped": "답하는 중인 턴을 멈췄습니다.",
    "bot.nothingToStop": "멈출 턴이 없습니다.",
    "bot.truncated": "답변이 길어 일부만 보냈습니다. 전체 답변은 대감에서 확인해 주세요: {link}",
    "bot.failed": "답하지 못했습니다({reason}). 자세한 내용은 대감에서 확인해 주세요: {link}",
    "bot.reasons.budget": "예산을 모두 사용함",
    "bot.reasons.limit": "요청 한도에 이름",
    "bot.reasons.access": "접근 권한이 없어짐",
    "bot.reasons.unavailable": "Agent 를 사용할 수 없음",
    "bot.reasons.aborted": "턴이 중단됨",
    "bot.unconfirmed": (
        "대감에 오랫동안 로그인하지 않아 Agent 를 부를 수 없습니다. "
        "대감에 로그인한 뒤 다시 보내 주세요."
    ),
    "card.title": "승인 요청 · {agent}",
    "card.approve": "승인",
    "card.reject": "거부",
    "card.reason": "거부 사유 (선택)",
    "card.send": "보내기",
    "card.open": "대감에서 열기",
    "card.sent": "결정을 보냈습니다. 답변이 오면 이 채팅에 보냅니다.",
    "card.approved": "승인했습니다.",
    "card.rejected": "거부했습니다.",
    "card.closed": "이미 처리했거나 끝난 요청입니다.",
}

EN: dict[str, str] = {
    "manifest.name": "Daegam",
    "manifest.short": "Talk to Daegam's Agents in Teams.",
    "manifest.full": (
        "After linking your Daegam account, select an Agent you may chat with, ask it questions "
        "and answer its approval requests."
    ),
    "manifest.commands.link": "link",
    "manifest.commands.link.description": "link your Daegam account",
    "manifest.commands.unlink": "unlink",
    "manifest.commands.unlink.description": "disconnect",
    "manifest.commands.agents": "agents",
    "manifest.commands.agents.description": "select an Agent",
    "manifest.commands.new": "new",
    "manifest.commands.new.description": "start a new conversation",
    "manifest.commands.stop": "stop",
    "manifest.commands.stop.description": "stop the current turn",
    "manifest.commands.help": "help",
    "manifest.commands.help.description": "show commands",
    "bot.welcome": (
        "To use Daegam's Agents in Teams, link your Daegam account first. "
        "Create a code under the account menu > Teams connection in Daegam, then send `link "
        "<code>` here."
    ),
    "bot.help": (
        "Commands: link <code>, unlink, agents, new, stop, help. "
        "Any other message goes to the selected Agent."
    ),
    "bot.pendingConfirm": (
        "Confirm in Daegam, under the account menu > Teams connection, that this Teams account is "
        "yours. "
        "It links only if you confirm within 10 minutes."
    ),
    "bot.linked": "Linked to the Daegam account {username}. Send `agents` to select an Agent.",
    "bot.codeInvalid": (
        "The code is wrong or has expired. Create a new one in Daegam and send it again."
    ),
    "bot.codeBlocked": "Too many wrong codes. Linking is blocked for an hour.",
    "bot.replaced": (
        "Your Daegam account is now linked to another Teams account, so this chat can no longer be "
        "used."
    ),
    "bot.relinked": "This Teams account is now linked to another Daegam account.",
    "bot.unlinked": "Your Daegam account is disconnected.",
    "bot.linkEnded": "The link has expired. Create a new code in Daegam and link again.",
    "bot.pickAgent": "Select an Agent.",
    "bot.noAgents": (
        "No Agent is available in Teams. "
        "Ask whoever manages an Agent to make it available in Teams."
    ),
    "bot.agentChosen": "Selected {agent}. Send a message.",
    "bot.agentGone": "The selected Agent is no longer available in Teams. Select another one.",
    "bot.newSession": "Starting a new conversation.",
    "bot.sessionClosed": "The previous conversation ended, so a new one starts.",
    "bot.busy": (
        "Still answering your previous message. Send again when it is done, or send `stop`."
    ),
    "bot.waitingApproval": "Answer the approval request first.",
    "bot.tooLong": "The message is longer than this Agent accepts. Shorten it and send it again.",
    "bot.quoted": "Quoted or forwarded messages cannot be sent. Type what you need instead.",
    "bot.attachments": "Attachments are not accepted. Send the content as a message.",
    "bot.personalOnly": "Daegam works only in 1:1 chats.",
    "bot.rateLimited": "Too many messages. Send again in a minute.",
    "bot.waking": "The Agent is waking up. Please wait a moment.",
    "bot.stopped": "Stopped the current turn.",
    "bot.nothingToStop": "There is no turn to stop.",
    "bot.truncated": (
        "The answer was long, so only part of it was sent. See the full answer in Daegam: {link}"
    ),
    "bot.failed": "Couldn't answer ({reason}). See Daegam for details: {link}",
    "bot.reasons.budget": "budget used up",
    "bot.reasons.limit": "request limit reached",
    "bot.reasons.access": "access lost",
    "bot.reasons.unavailable": "Agent unavailable",
    "bot.reasons.aborted": "turn aborted",
    "bot.unconfirmed": (
        "You have not signed in to Daegam for a while, so Agents cannot be called. "
        "Sign in to Daegam, then send again."
    ),
    "card.title": "Approval request · {agent}",
    "card.approve": "Approve",
    "card.reject": "Reject",
    "card.reason": "Reason for rejecting (optional)",
    "card.send": "Send",
    "card.open": "Open in Daegam",
    "card.sent": "Decision sent. The answer will come to this chat.",
    "card.approved": "Approved.",
    "card.rejected": "Rejected.",
    "card.closed": "This request was already handled or has ended.",
}

TABLES: dict[Language, dict[str, str]] = {"ko": KO, "en": EN}


def language_of(value: str | None) -> Language:
    """저장된 언어. 모르는 값이면 기본(한국어)이다."""
    return "en" if value == "en" else DEFAULT_LANGUAGE


def markdown_escaped(value: str) -> str:
    """값의 Markdown 글자를 역슬래시로 막는다."""
    return _MARKDOWN.sub(r"\\\1", value)


def text(key: str, language: str | None = None, **values: str) -> str:
    """열쇠의 글. 자리표시자에 values 를 넣고 `{이름|짝}` 이면 값 뒤에 조사를 붙인다. 조사는 값의
    원래 글자로 고르고, 값은 `{link}` 말고는 Markdown 글자를 막아 넣는다. 없는 열쇠나 빠진 값은
    KeyError 다."""
    template = TABLES[language_of(language)][key]

    def fill(m: re.Match[str]) -> str:
        name, pair = m.group(1), m.group(2)
        value = values[name]
        shown = value if name in RAW_VALUES else markdown_escaped(value)
        return shown + particle(value, pair) if pair else shown

    return PLACEHOLDER.sub(fill, template)


def placeholders(template: str) -> set[str]:
    """글이 받는 자리표시자의 이름."""
    return {m.group(1) for m in PLACEHOLDER.finditer(template)}


__all__ = [
    "DEFAULT_LANGUAGE",
    "EN",
    "KO",
    "LANGUAGES",
    "Language",
    "TABLES",
    "language_of",
    "markdown_escaped",
    "placeholders",
    "text",
]
