"""P45 F0.6: 평가 사례의 개인정보 패턴 (8절, 17절 F0.6).

적어 둔 말뭉치에서 바꿔야 할 꼴은 모두 바뀌고, 예약 도메인 아래의 주소와 대표번호, 확인 항목의
`1588-1234` 는 남는다. 손으로 쓴 글의 13자리 주문번호는 등록번호처럼 보여 경고로 잡힌다. 사례의 어느
칸에서 찾았는지와 Agent 의 `pii_mask` 정규식도 본다. 저장소가 필요 없는 단위 시험이다.
"""

from __future__ import annotations

import time

import pytest
import regex

from daegam.services.evaluation import pii
from daegam.store.agents import Agent
from daegam.store.evaluation import CaseContent
from tests.helpers.docs import agent_doc

REPLACED: list[tuple[str, str]] = [
    # 전화번호: 휴대전화, 띄어쓰기와 점, 붙여 쓴 꼴, +82, 전각 숫자, 지역번호, 인터넷 전화
    ("010-1234-5678", pii.PHONE),
    ("010 1234 5678", pii.PHONE),
    ("010.1234.5678", pii.PHONE),
    ("01012345678", pii.PHONE),
    ("+82 10-1234-5678", pii.PHONE),
    ("+82-10-1234-5678", pii.PHONE),
    ("+821012345678", pii.PHONE),
    ("+82 010 1234 5678", pii.PHONE),
    ("０１０－１２３４－５６７８", pii.PHONE),
    ("02-123-4567", pii.PHONE),
    ("031-123-4567", pii.PHONE),
    ("064 1234 5678", pii.PHONE),
    ("070-1234-5678", pii.PHONE),
    ("010 - 1234 - 5678", pii.PHONE),
    ("010–1234–5678", pii.PHONE),
    ("010—1234—5678", pii.PHONE),
    ("(02) 123-4567", pii.PHONE),
    ("02)123-4567", pii.PHONE),
    ("(031)123-4567", pii.PHONE),
    # 주민등록번호와 외국인등록번호, 가린 꼴
    ("900101-1234567", pii.ID_NUMBER),
    ("9001011234567", pii.ID_NUMBER),
    ("900101 2234567", pii.ID_NUMBER),
    ("900101-5234567", pii.ID_NUMBER),
    ("900101-1******", pii.ID_NUMBER),
    ("900101-*******", pii.ID_NUMBER),
    ("900101-1xxxxxx", pii.ID_NUMBER),
    ("９００１０１－１２３４５６７", pii.ID_NUMBER),
    ("900101 - 1234567", pii.ID_NUMBER),
    ("900101﹣1234567", pii.ID_NUMBER),
    # 카드 번호
    ("4111-1111-1111-1111", pii.CARD_NUMBER),
    ("4111 1111 1111 1111", pii.CARD_NUMBER),
    ("4111111111111111", pii.CARD_NUMBER),
    ("3782-822463-10005", pii.CARD_NUMBER),
    # 이메일
    ("kim.minsu@company.co.kr", pii.EMAIL),
    ("a+b@mail.daegam.io", pii.EMAIL),
]

KEPT = [
    "test@example.com",
    "help@support.example.org",
    "<20261114.abc123@mail.example.net>",
    "user@host.test",
    "admin@router.localhost",
    "1588-1234",
    "1644 0000",
    "주문 수량 12개, 금액 34,000원",
    "버전 2.0.0-rc1",
]


@pytest.mark.parametrize(("value", "kind"), REPLACED)
def test_each_listed_form_is_replaced_in_a_sentence(value, kind):
    text = f"연락처는 {value} 입니다."
    replaced, counts = pii.replace(text)
    assert replaced == f"연락처는 [{kind}] 입니다.", replaced
    assert counts == {kind: 1}


@pytest.mark.parametrize("value", KEPT)
def test_reserved_domains_and_representative_numbers_stay(value):
    text = f"참고: {value}"
    assert pii.find(text) == []
    assert pii.replace(text) == (text, {})


# 한글 조사가 붙거나 한글 바로 뒤에 온 주소. 내장 패턴의 경계는 ASCII 글자로만 본다
ATTACHED: list[tuple[str, str]] = [
    ("hong@gmail.com으로 보내 줘", "[email]으로 보내 줘"),
    ("hong@naver.com로 회신", "[email]로 회신"),
    ("담당자hong@corp.com", "담당자[email]"),
    ("kim@corp.co.kr로 보내 주세요", "[email]로 보내 주세요"),
    ("lee@corp.kr에게 물어보세요", "[email]에게 물어보세요"),
    ("주소는 park@corp.kr이고", "주소는 [email]이고"),
    ("번호는 010-1234-5678이고", "번호는 [phone]이고"),
    ("등록번호900101-1234567로", "등록번호[id_number]로"),
]


@pytest.mark.parametrize(("text", "expected"), ATTACHED)
def test_hangul_next_to_a_match_does_not_hide_it(text, expected):
    assert pii.replace(text)[0] == expected


def test_positions_match_the_original_text():
    """전각 글자를 반각으로 읽어도 자리는 원문의 것이다."""
    text = "전화 ０１０－１２３４－５６７８, 메일 kim@corp.kr"
    found = pii.find(text)
    assert [(f.kind, text[f.start : f.end]) for f in found] == [
        (pii.PHONE, "０１０－１２３４－５６７８"),
        (pii.EMAIL, "kim@corp.kr"),
    ]


def test_a_thirteen_digit_order_number_looks_like_an_id_number():
    """손으로 쓴 글의 13자리 주문번호는 등록번호처럼 보인다. 손으로 쓴 사례에서는 경고이고 확인을
    받고 저장한다."""
    found = pii.find("주문번호 2026111412345 의 배송 상태를 알려 주세요")
    assert [f.kind for f in found] == [pii.ID_NUMBER]


def test_a_longer_number_is_not_cut_into_a_shorter_match():
    """16자리 카드 번호 안의 13자리를 등록번호로 따로 잡지 않는다."""
    found = pii.find("4111111111111111")
    assert [f.kind for f in found] == [pii.CARD_NUMBER]
    assert pii.find("41111111111111112") == []


def _agent_with_rule(pattern: str) -> Agent:
    spec = {
        "graph": {
            "nodes": [
                {
                    "id": "g",
                    "type": "guardrail",
                    "data": {
                        "position": "input",
                        "rules": [
                            {"kind": "max_length", "value": 10},
                            {"kind": "pii_mask", "value": {"types": ["regex"], "regex": pattern}},
                        ],
                    },
                }
            ]
        }
    }
    return agent_doc("ag-1", spec=spec)


def test_a_slow_agent_pattern_masks_the_whole_field(monkeypatch):
    """되돌아가기가 긴 식(`regex_safety` 가 받는 꼴)은 시간 한도에서 멈추고 그 칸 전부를 찾은 것으로
    본다. 긴 칸은 식을 돌리지 않고 같은 답이다."""
    monkeypatch.setattr(pii, "PATTERN_TIMEOUT_SECONDS", 0.05)
    slow = [regex.compile(r"(\d|\d)+x")]
    # 숫자 뒤에 x 가 아닌 글자가 오고 그 뒤에 x 가 있어 엔진이 앞당겨 포기하지 못한다
    text = "1" * 40 + "yx"
    started = time.monotonic()
    assert pii.find(text, slow) == [pii.Found(pii.MASKED, 0, len(text))]
    assert time.monotonic() - started < 2
    assert pii.replace(f"주문 {text}", slow)[0] == "[masked]"
    # 한도를 넘지 않는 식은 맞은 자리만 가린다
    assert pii.replace("사번 E12 확인", [regex.compile(r"E\d+")])[0] == "사번 [masked] 확인"
    long = "가" * (pii.PATTERN_MAX_CHARS + 1)
    assert pii.find(long, [regex.compile("없는 식")]) == [pii.Found(pii.MASKED, 0, len(long))]
    # 식이 없으면 긴 칸도 내장 패턴만 본다
    assert pii.find(long) == []


def test_the_agents_own_regex_rules_are_found():
    agent = _agent_with_rule(r"EMP-\d{5}")
    patterns = pii.agent_patterns(agent)
    assert [p.pattern for p in patterns] == [r"EMP-\d{5}"]
    replaced, counts = pii.replace("사번 EMP-12345 의 휴가", patterns)
    assert replaced == "사번 [masked] 의 휴가" and counts == {pii.MASKED: 1}
    assert pii.agent_patterns(agent_doc("ag-2")) == []


def test_scan_names_every_field_a_match_can_move_into():
    content = CaseContent.model_validate(
        {
            "messages": ["안녕하세요", "제 번호는 010-1234-5678"],
            "reference": {"text": "kim@corp.kr 로 보냈습니다"},
            "criteria": ["900101-1234567 을 말하지 않는다"],
            "checks": [
                {"kind": "answer_contains", "text": "1588-1234"},
                {"kind": "answer_not_contains", "text": "010-9999-8888"},
                {"kind": "tool_called", "tool": "Send", "args": {"to": "lee@corp.kr"}},
                {"kind": "tool_not_called", "tool": "Call 010-5555-6666"},
            ],
            "tool_results": {"Send": "4111-1111-1111-1111 결제됨"},
        }
    )
    places = [(f.place, f.kind) for f in pii.scan_case(content, [regex.compile("없는 식")])]
    assert places == [
        ("messages[1]", pii.PHONE),
        ("reference", pii.EMAIL),
        ("criteria[0]", pii.ID_NUMBER),
        ("checks[1].text", pii.PHONE),
        ("checks[2].args", pii.EMAIL),
        ("checks[3].tool", pii.PHONE),
        ('tool_results["Send"]', pii.CARD_NUMBER),
    ]


def test_a_mail_agents_case_with_a_reserved_address_has_nothing():
    content = CaseContent.model_validate(
        {
            "messages": ["test@example.com 으로 회의 초대를 보내 주세요"],
            "checks": [
                {"kind": "tool_called", "tool": "SendMail", "args": {"to": "test@example.com"}}
            ],
            "tool_results": {"SendMail": "sent to test@example.com"},
        }
    )
    assert pii.scan_case(content) == []
