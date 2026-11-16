"""웹 가드(P39b 14절 2·3·6부, D64). 출처 규칙, 목록, 첨부와 데이터 Tool 의 허용 목록, 한도, 인용과
외부 웹 표시, 서브에이전트에 내려가고 돌아오는 출처를 든다.

가짜 웹 Tool 서버와 문서·모델은 web_fixtures.py 에 있다. 스텁은 사람 메시지의 JSON 조각을 따르므로
그 URL 은 늘 사람의 글에 있다. 모델이 지은 URL 과 결과의 링크는 ScriptModel 이 부른다.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from runtime.citations import KIND_WEB, CitationLedger, emitted_citations, untrusted_kind
from runtime.compile import compile_agent
from runtime.config import ConfigError, parse_config
from runtime.web_citations import CITE_LINE, cite_fetch, cite_search, escape_untrusted
from runtime.web_guard import REASON_ATTACHMENT, REASON_DATA_TOOLS
from runtime.web_urls import (
    MAX_WEB_URLS,
    add_recent_urls,
    domain_matches,
    extract_urls,
    normalize_url,
    result_urls,
)
from tests.app_fixtures import guardrail, mask_rule, regenerate, resume, sid_of
from tests.attachment_fixtures import document, image, send
from tests.conftest import chat, events_named, load_fixture, run_app
from tests.web_fixtures import (
    ALLOWED,
    BLOCKED,
    LANDING,
    OTHER,
    PAGE_A,
    PAGE_B,
    REDIRECTING,
    FakeWeb,
    ScriptModel,
    call,
    code_of,
    delegate,
    fetch,
    fetch_message,
    fetch_text,
    per_user,
    results_of,
    sample,
    sample_path,
    search_text,
    web_doc,
)

# --- URL 규칙 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "urls"),
    [
        ("see https://a.com/x.", ["https://a.com/x"]),
        ("(https://a.com/x) and https://b.com/y?q=1,", ["https://a.com/x", "https://b.com/y?q=1"]),
        ("https://en.wikipedia.org/wiki/Foo_(bar)", ["https://en.wikipedia.org/wiki/Foo_(bar)"]),
        ("`https://a.com/code` <https://b.com/>", ["https://a.com/code", "https://b.com/"]),
        ('"https://a.com/q"', ["https://a.com/q"]),
        ("https://a.com에서 봐", ["https://a.com에서", "https://a.com"]),
        (
            "https://ko.wikipedia.org/wiki/한국 문서",
            ["https://ko.wikipedia.org/wiki/한국", "https://ko.wikipedia.org/wiki/"],
        ),
        ("ftp://a.com/x and HTTP://A.com", ["HTTP://A.com"]),
        ("no links here", []),
    ],
)
def test_extract_urls(text: str, urls: list[str]) -> None:
    assert extract_urls(text) == urls


@pytest.mark.parametrize(
    ("raw", "normal"),
    [
        ("HTTPS://Example.COM", "https://example.com/"),
        ("https://example.com:443/a#frag", "https://example.com/a"),
        ("http://example.com:80/", "http://example.com/"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
        (
            "https://example.com/%7euser/%2f?q=%e2%82%ac",
            "https://example.com/~user/%2F?q=%E2%82%AC",
        ),
        ("https://example.com/%41b", "https://example.com/Ab"),
        ("https://ko.wikipedia.org/wiki/한국", "https://ko.wikipedia.org/wiki/%ED%95%9C%EA%B5%AD"),
        ("https://한국.kr/a", "https://xn--3e0b707e.kr/a"),
        ("https://user@example.com/", "https://user@example.com/"),
        ("https://[::1]:8443/", "https://[::1]:8443/"),
        ("https://example.com:99999/", None),
        ("ftp://example.com/", None),
        ("https:///nohost", None),
    ],
)
def test_normalize_url(raw: str, normal: str | None) -> None:
    assert normalize_url(raw) == normal


def test_typed_and_percent_encoded_forms_are_one_url() -> None:
    """사람이 친 한글 경로와 결과의 퍼센트 인코딩 경로는 같은 URL 이다(RFC 3986 6.2.2)."""
    typed = normalize_url("https://ko.wikipedia.org/wiki/한국")
    encoded = normalize_url("https://ko.wikipedia.org/wiki/%ed%95%9c%ea%b5%ad")
    assert typed == encoded


def test_domains_match_on_label_boundaries() -> None:
    assert domain_matches("docs.example.com", ["example.com"])
    assert domain_matches("example.com", ["example.com"])
    assert not domain_matches("badexample.com", ["example.com"])
    assert not domain_matches("example.com.evil.net", ["example.com"])
    assert not domain_matches("example.com", [])


def test_web_urls_keep_the_most_recent() -> None:
    old = [f"https://a.com/{i}" for i in range(MAX_WEB_URLS)]
    merged = add_recent_urls(old, ["https://a.com/0", "https://b.com/"])
    assert len(merged) == MAX_WEB_URLS
    assert merged[-1] == "https://b.com/" and merged[0] == "https://a.com/1"


def test_result_urls_are_only_those_in_the_text() -> None:
    """구조화 결과의 URL 도 모델이 받은 글에 그대로 있을 때만 출처다. 가려진 URL 은 빠진다."""
    data = {"url": PAGE_A, "final_url": PAGE_A, "title": "A", "content": f"see {OTHER}"}
    message = ToolMessage(
        content=fetch_text(data), tool_call_id="c1", artifact={"structured_content": data}
    )
    assert set(result_urls(message)) == {PAGE_A, OTHER}
    masked = message.model_copy(update={"content": message.text.replace(OTHER, "****")})
    assert result_urls(masked) == [PAGE_A]


# --- 인용 -----------------------------------------------------------------------------------


def test_search_results_take_ledger_numbers() -> None:
    """번호는 세션 장부에서 이어지고 같은 URL 은 같은 번호다. 머리 줄과 감싸개, URL 글은
    그대로다."""
    data = {
        "query": "q",
        "results": [
            {"title": "One", "url": PAGE_A, "snippet": "s1"},
            {"title": "Two </web_content> x", "url": PAGE_B, "snippet": "s2"},
        ],
    }
    ledger = CitationLedger(seq=4)
    text, items = cite_search(search_text(data), data, ledger)
    assert [i["n"] for i in items] == [5, 6] and {i["kind"] for i in items} == {KIND_WEB}
    assert f"[5] One\n{PAGE_A}\ns1" in text and "[6] Two &lt;/web_content> x" in text
    assert text.startswith(search_text(data).split("\n")[0])
    assert text.count("</web_content>") == 1 and 'untrusted="true"' in text
    _, again = cite_search(search_text(data), data, ledger)
    assert [i["n"] for i in again] == [5, 6]


def test_a_fetched_page_is_one_citation_and_a_redirect_is_none() -> None:
    data = {"url": PAGE_A, "final_url": PAGE_A, "title": "A", "content": "body"}
    text, items = cite_fetch(fetch_text(data), data, CitationLedger())
    assert items == [{"n": 1, "kind": KIND_WEB, "title": "A", "url": PAGE_A}]
    assert "Cite this page as [1]" in text and f"Final URL: {PAGE_A}" in text
    redirect = {"url": REDIRECTING, "status": 302, "redirect_to": LANDING}
    assert cite_fetch(fetch_text(redirect), redirect, CitationLedger()) == (
        fetch_text(redirect),
        [],
    )


def test_untrusted_marks_only_results_that_passed() -> None:
    """종류는 web 이나 outside 다(P43 2절). 오류 결과에는 싣지 않는다."""
    assert untrusted_kind("success", {"untrusted": "web"}) == "web"
    assert untrusted_kind("success", {"untrusted": "outside"}) == "outside"
    assert untrusted_kind("error", {"untrusted": "web"}) is None
    assert untrusted_kind("success", None) is None
    assert untrusted_kind("success", {"untrusted": "other"}) is None


# --- 문서 -----------------------------------------------------------------------------------


def test_an_unpinned_source_stops_the_boot() -> None:
    """web_guard 문서에서 allow 가 빈 소스는 이름 있는 코드로 기동을 막는다(14절 5부의 그물)."""
    doc = load_fixture("web-agent.json")
    doc["tools"][0]["allow"] = []
    with pytest.raises(ConfigError) as e:
        parse_config(doc)
    assert e.value.code == "web_guard_unpinned_source"


def test_two_web_sources_of_one_agent_may_not_share_a_tool_name() -> None:
    doc = load_fixture("web-agent.json")
    doc["memory"] = None
    second = copy.deepcopy(doc["tools"][1])
    second["ref"] = "tools-web-2"
    doc["tools"].append(second)
    doc["subagents"][0]["tools"] = ["tools-web", "tools-web-2"]
    with pytest.raises(ConfigError) as e:
        compile_agent(parse_config(doc), tools_by_ref={}, checkpointer=None)
    assert e.value.code == "tool_name_conflict"


# --- 한 턴 ----------------------------------------------------------------------------------


async def test_a_url_the_person_wrote_is_fetched_cited_and_marked(fake_web: FakeWeb) -> None:
    async with run_app(web_doc(fake_web)) as running:
        _, sse = await chat(running, fetch_message(PAGE_A))
    [result] = results_of(sse, "web_fetch")
    assert result["status"] == "success" and result["untrusted"] == "web"
    assert "Cite this page as [1]" in result["content"]
    [cited] = events_named(sse, "citations")
    assert cited["items"] == [{"n": 1, "kind": KIND_WEB, "title": "A", "url": PAGE_A}]
    assert fake_web.fetched == [PAGE_A]


async def test_a_url_the_model_made_up_is_refused(fake_web: FakeWeb) -> None:
    model = ScriptModel(calls=[fetch(OTHER + "?leak=secret")])
    async with run_app(web_doc(fake_web), model=model) as running:
        _, sse = await chat(running, "다른 곳을 찾아봐")
        observed = running.observed("web_guard")
    [result] = results_of(sse, "web_fetch")
    assert code_of(result) == "url_not_in_conversation" and "untrusted" not in result
    assert fake_web.fetched == []
    assert observed[0]["code"] == "url_not_in_conversation"
    assert observed[0]["host"] == "other.example"


async def test_links_in_a_result_become_origins(fake_web: FakeWeb) -> None:
    """가져온 페이지의 링크는 다음 fetch 의 출처다. 결과에 없던 URL 은 아니다."""
    model = ScriptModel(calls=[fetch(PAGE_A), fetch(PAGE_B), fetch("https://example.com/z")])
    async with run_app(web_doc(fake_web), model=model) as running:
        _, sse = await chat(running, f"{PAGE_A} 을 읽어 줘")
    codes = [code_of(r) for r in results_of(sse, "web_fetch")]
    assert codes == [None, None, "url_not_in_conversation"]
    assert fake_web.fetched == [PAGE_A, PAGE_B]


async def test_a_url_masked_out_of_a_result_is_no_origin(fake_web: FakeWeb) -> None:
    """ToolGuard 가 가린 URL 은 모델이 받은 글에 없으므로 출처가 되지 못한다."""
    doc = web_doc(fake_web)
    doc["guardrails"] = [guardrail("tool", [mask_rule(r"other\.example")], "flag")]
    model = ScriptModel(calls=[fetch(PAGE_A), fetch(OTHER)])
    async with run_app(doc, model=model) as running:
        _, sse = await chat(running, f"{PAGE_A} 을 읽어 줘")
    codes = [code_of(r) for r in results_of(sse, "web_fetch")]
    assert codes == [None, "url_not_in_conversation"]


async def test_a_redirect_target_is_an_origin_under_the_block_list(fake_web: FakeWeb) -> None:
    """다른 호스트로의 리다이렉트는 redirect_to 로 오고 모델이 따라갈 수 있다. 막는 목록은 그때도
    걸린다."""
    model = ScriptModel(calls=[fetch(REDIRECTING), fetch(LANDING)])
    async with run_app(web_doc(fake_web), model=model) as running:
        _, sse = await chat(running, f"{REDIRECTING} 을 읽어 줘")
    assert [code_of(r) for r in results_of(sse, "web_fetch")] == [None, None]
    doc = web_doc(fake_web)
    doc["tools"][0]["web"]["block_domains"] = ["elsewhere.example"]
    model = ScriptModel(calls=[fetch(REDIRECTING), fetch(LANDING)])
    async with run_app(doc, model=model) as running:
        _, sse = await chat(running, f"{REDIRECTING} 을 읽어 줘")
    assert [code_of(r) for r in results_of(sse, "web_fetch")] == [None, "url_blocked"]


async def test_a_blocked_host_is_refused_even_when_the_person_wrote_it(fake_web: FakeWeb) -> None:
    async with run_app(web_doc(fake_web)) as running:
        _, sse = await chat(running, fetch_message(BLOCKED))
    [result] = results_of(sse, "web_fetch")
    assert code_of(result) == "url_blocked" and fake_web.fetched == []


async def test_data_tools_require_the_allow_list(fake_web: FakeWeb) -> None:
    """데이터 Tool 이 있는 그래프는 모든 대화에서 허용 목록의 호스트로만 간다. 허용 목록은 대화에
    없어도 출처다."""
    model = ScriptModel(calls=[fetch(PAGE_A), fetch(ALLOWED)])
    async with run_app(web_doc(fake_web, data_tools=True), model=model) as running:
        _, sse = await chat(running, f"{PAGE_A} 을 읽어 줘")
        observed = running.observed("web_guard")
    [refused, allowed] = results_of(sse, "web_fetch")
    assert code_of(refused) == "url_not_allowed" and "company data" in refused["content"]
    assert code_of(allowed) is None
    assert observed[0]["reason"] == REASON_DATA_TOOLS


async def test_an_attachment_keeps_fetch_on_the_allow_list(fake_web: FakeWeb) -> None:
    """첨부를 받은 턴부터 세션이 끝날 때까지 허용 목록 밖은 거절된다. 첨부 없는 다음 턴, 다시 생성,
    고쳐 보내기도 같다. 첨부 없는 대화는 그대로 가져온다."""
    async with run_app(web_doc(fake_web)) as running:
        _, sse = await chat(running, fetch_message(PAGE_A))
        assert code_of(results_of(sse, "web_fetch")[0]) is None
        sid = sid_of(sse)
        _, sse = await send(running, [image()], fetch_message(PAGE_B), session_id=sid)
        [refused] = results_of(sse, "web_fetch")
        assert code_of(refused) == "url_not_allowed" and "attached" in refused["content"]
        _, sse = await chat(running, fetch_message(PAGE_A), session_id=sid)
        assert code_of(results_of(sse, "web_fetch")[0]) == "url_not_allowed"
        _, sse = await chat(running, fetch_message(ALLOWED), session_id=sid)
        assert code_of(results_of(sse, "web_fetch")[0]) is None
        _, sse = await regenerate(running, sid)
        assert code_of(results_of(sse, "web_fetch")[0]) is None
        _, sse = await regenerate(running, sid, message=fetch_message(PAGE_A))
        assert code_of(results_of(sse, "web_fetch")[0]) == "url_not_allowed"
        observed = running.observed("web_guard")
    assert {e["reason"] for e in observed} == {REASON_ATTACHMENT}
    assert fake_web.fetched == [PAGE_A, ALLOWED, ALLOWED]


async def test_metrics_say_when_attachments_narrow_fetch(fake_web: FakeWeb) -> None:
    async with run_app(web_doc(fake_web)) as running:
        assert (await running.client.get("/metrics")).json()["chat"]["attachments_limit_fetch"]
    async with run_app(web_doc(fake_web, data_tools=True)) as running:
        chat_field = (await running.client.get("/metrics")).json()["chat"]
    assert chat_field["attachments_limit_fetch"] is False


# --- 한도 -----------------------------------------------------------------------------------


async def test_a_turn_allows_five_searches(fake_web: FakeWeb) -> None:
    """턴 상한은 턴마다 새로 찬다."""
    model = ScriptModel(calls=[call("web_search", query=f"q{i}") for i in range(6)])
    async with run_app(web_doc(fake_web), model=model) as running:
        _, sse = await chat(running, "찾아 줘")
        codes = [code_of(r) for r in results_of(sse, "web_search")]
        assert codes == [None] * 5 + ["web_turn_limit"]
        model.calls.append(call("web_search", query="next"))
        _, sse = await chat(running, "또 찾아 줘", session_id=sid_of(sse))
    assert [code_of(r) for r in results_of(sse, "web_search")] == [None]
    assert len(fake_web.searched) == 6


async def test_search_results_are_cited_and_their_urls_are_origins(fake_web: FakeWeb) -> None:
    model = ScriptModel(calls=[call("web_search", query="a"), fetch(PAGE_A)])
    async with run_app(web_doc(fake_web), model=model) as running:
        _, sse = await chat(running, "찾아서 읽어 줘")
    [searched] = results_of(sse, "web_search")
    assert searched["untrusted"] == "web" and f"[1] First\n{PAGE_A}" in searched["content"]
    cited = events_named(sse, "citations")
    assert [i["n"] for i in cited[0]["items"]] == [1, 2]
    assert [i["n"] for i in cited[1]["items"]] == [1], "같은 URL 은 같은 번호다"
    assert code_of(results_of(sse, "web_fetch")[0]) is None


async def test_the_per_user_limit_counts_across_turns(fake_web: FakeWeb) -> None:
    doc = per_user(web_doc(fake_web), fetch_per_minute=2)
    async with run_app(doc) as running:
        codes = []
        for _ in range(3):
            _, sse = await chat(running, fetch_message(PAGE_A))
            codes.append(code_of(results_of(sse, "web_fetch")[0]))
        _, sse = await chat(running, fetch_message(PAGE_A), user_sub="bob")
    assert codes == [None, None, "rate_limited"]
    assert code_of(results_of(sse, "web_fetch")[0]) is None, "한도는 사용자마다다"


# --- 서브에이전트 ----------------------------------------------------------------------------


async def test_subagents_take_the_persons_urls_but_not_the_delegation_text(
    fake_web: FakeWeb,
) -> None:
    """사람의 URL 은 서브에이전트로 내려가 출처가 되고, 메인이 위임 글에 지은 URL 은 출처가
    아니다."""
    made_up = "https://example.com/made-up"
    model = ScriptModel(calls=[delegate("researcher", PAGE_A), delegate("researcher", made_up)])
    async with run_app(web_doc(fake_web, on_main=False), model=model) as running:
        _, sse = await chat(running, f"{PAGE_A} 을 조사해 줘")
    results = results_of(sse, "web_fetch")
    assert [code_of(r) for r in results] == [None, "url_not_in_conversation"]
    assert all(r.get("agent") for r in results)
    assert fake_web.fetched == [PAGE_A]


async def test_urls_a_subagent_read_come_back_to_the_graph(fake_web: FakeWeb) -> None:
    """서브에이전트가 받은 결과의 링크는 메인 상태로 돌아와 다음 위임에도 출처다(F0 2번)."""
    model = ScriptModel(calls=[delegate("researcher", PAGE_A), delegate("researcher", PAGE_B)])
    async with run_app(web_doc(fake_web, on_main=False), model=model) as running:
        _, sse = await chat(running, f"{PAGE_A} 을 조사해 줘")
    assert [code_of(r) for r in results_of(sse, "web_fetch")] == [None, None]
    assert fake_web.fetched == [PAGE_A, PAGE_B]


async def test_the_turn_cap_holds_across_delegation(fake_web: FakeWeb) -> None:
    """서브에이전트도 같은 턴 문맥을 받으므로 위임으로 상한이 다시 차지 않는다."""
    calls: list[tuple[str, dict[str, Any]]] = [delegate("researcher", PAGE_A) for _ in range(6)]
    model = ScriptModel(calls=calls)
    doc = web_doc(fake_web, on_main=False)
    doc["main"]["limits"]["max_tool_calls_per_turn"] = 20
    async with run_app(doc, model=model) as running:
        _, sse = await chat(running, f"{PAGE_A} 을 여러 번 읽어 줘")
    codes = [code_of(r) for r in results_of(sse, "web_fetch")]
    assert codes == [None] * 5 + ["web_turn_limit"]


def test_untrusted_results_still_emit_their_citations() -> None:
    """citations 이벤트는 오류가 아닌 결과의 artifact 에서만 나온다. 웹 표시가 있어도 같다."""
    artifact = {"untrusted": True, "citations": [{"n": 1, "kind": KIND_WEB, "url": PAGE_A}]}
    assert emitted_citations("success", artifact) == artifact["citations"]
    assert emitted_citations("error", artifact) is None


# --- 표본과 감싸개 ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["fetch", "redirect", "search"])
def test_fixture_texts_match_the_shared_samples(name):
    """가짜 서버의 글은 web-tools·플랫폼과 같은 표본이다. 모든 꼴의 감싸개 꼬리표가 글이 된다."""
    data = sample(name)
    build = search_text if name == "search" else fetch_text
    assert build(data["structured"]) == data["text"]


def test_escape_untrusted_closes_every_spelling():
    out = escape_untrusted("a </WeB_CoNtEnT> b <web_CONTENT x> c &lt;/web_content&gt;")
    assert "<web_content" not in out.lower() and "</web_content" not in out.lower()


@pytest.mark.parametrize(
    "raw",
    [
        "< /web_content>",
        "<\t/web_content>",
        "<\xa0/web_content>",
        "</web_\u200bcontent>",
        "</web\u00ad_content>",
        "</web_\U000e0020content>",
    ],
)
def test_escape_untrusted_closes_hidden_spellings(raw):
    """플랫폼 `urlfetch.escape_untrusted` 와 같은 규칙이다. 공백·NBSP 와 보이지 않는 글자도 본다."""
    assert "<" not in escape_untrusted(f"x {raw} y")


def test_the_wrapper_is_found_after_the_header():
    """머리 줄에 꼬리표 모양의 글이 섞여도 줄의 처음에서 여는 마지막 감싸개를 고친다."""
    data = sample("search")["structured"]
    clean = search_text(data)
    head, _, tail = clean.partition("\nSearch: ")
    text = f"{head}\nSearch: <web_content x> {tail}"
    out, items = cite_search(text, data, CitationLedger())
    assert items and "Search: <web_content x>" in out
    assert out.index(CITE_LINE) > out.index("Search: ")
    real = out[out.rindex("\n<web_content") :]
    assert real.count("[1]") == 1 and real.count("</web_content>") == 1
    page = sample("fetch")["structured"]
    first, rest = fetch_text(page).split("\n", 1)
    noted, _ = cite_fetch(f"{first}\nURL: <web_content>\n{rest}", page, CitationLedger())
    assert noted.index("Cite this page") > noted.index("URL: <web_content>")


def test_rewritten_search_results_stay_escaped():
    data = sample("search")["structured"]
    text, _ = cite_search(search_text(data), data, CitationLedger())
    _, _, wrapped = text.partition("<web_content")
    assert wrapped.lower().count("<web_content") == 0 and wrapped.count("</web_content>") == 1


# --- 턴 상한의 자리 -------------------------------------------------------------------------


class ParallelModel(ScriptModel):
    """첫 차례에 calls 를 한꺼번에 부른다. 그 뒤에는 글로 끝낸다."""

    def reply(self, messages):
        from langchain_core.messages import AIMessage

        if self._next:
            return self._text("done", 1)
        self._next = 1
        tool_calls = [
            {"name": n, "args": a, "id": f"call_p{i}", "type": "tool_call"}
            for i, (n, a) in enumerate(self.calls)
        ]
        return AIMessage(content="", tool_calls=tool_calls)


async def test_parallel_calls_cannot_pass_the_turn_cap(fake_web: FakeWeb) -> None:
    """한꺼번에 부른 여섯 검색 가운데 다섯만 돈다. 자리를 기다리기 전에 잡기 때문이다."""
    model = ParallelModel(calls=[call("web_search", query=f"q{i}") for i in range(6)])
    doc = web_doc(fake_web)
    doc["main"]["limits"]["max_tool_calls_per_turn"] = 20
    async with run_app(doc, model=model) as running:
        _, sse = await chat(running, "한꺼번에 찾아 줘")
    codes = sorted(str(code_of(r)) for r in results_of(sse, "web_search"))
    assert codes == ["None"] * 5 + ["web_turn_limit"]
    assert len(fake_web.searched) == 5


async def test_a_call_without_a_turn_context_is_refused() -> None:
    from types import SimpleNamespace

    from runtime.web_guard import WebFetchGuard, WebSource, WebTool

    source = WebSource(
        "s", None, "web_search", (), (), {"search_per_minute": 9, "search_per_day": 9}
    )
    guard = WebFetchGuard({"web_search": WebTool("search", source)}, data_tools=False, main=True)
    request = SimpleNamespace(state={}, runtime=SimpleNamespace(context=None))
    call_ = {"name": "web_search", "args": {"query": "q"}, "id": "c"}
    refusal = await guard._check(call_, guard.web_tools["web_search"], request)
    assert refusal is not None and refusal.code == "web_no_turn"
    assert "turn" in refusal.message and "used all" not in refusal.message


# --- 승인과 첨부 ----------------------------------------------------------------------------


def _approval_doc(fake_web: FakeWeb) -> dict[str, Any]:
    doc = web_doc(fake_web)
    doc["tools"][0]["approval"] = {"web_fetch": ["approve", "edit", "reject"]}
    return doc


async def test_a_url_changed_by_approval_must_be_in_the_conversation(fake_web: FakeWeb) -> None:
    """승인자가 고친 URL 도 대화에 나온 것이어야 한다. 사람은 그 URL 을 채팅에 보내면 된다."""
    async with run_app(_approval_doc(fake_web)) as running:
        _, sse = await chat(running, fetch_message(PAGE_A))
        sid = sid_of(sse, "interrupt")
        edit = {"type": "edit", "edited_action": {"name": "web_fetch", "args": {"url": OTHER}}}
        _, sse = await resume(running, sid, [edit])
        assert code_of(results_of(sse, "web_fetch")[0]) == "url_not_in_conversation"
        _, sse = await chat(running, fetch_message(PAGE_A), session_id=sid)
        sid = sid_of(sse, "interrupt")
        same = {"type": "edit", "edited_action": {"name": "web_fetch", "args": {"url": PAGE_A}}}
        _, sse = await resume(running, sid, [same])
        assert code_of(results_of(sse, "web_fetch")[0]) is None
    assert fake_web.fetched == [PAGE_A]


async def test_an_attachment_holds_after_an_approval_resume(fake_web: FakeWeb) -> None:
    async with run_app(_approval_doc(fake_web)) as running:
        _, sse = await send(running, [image()], fetch_message(PAGE_A))
        sid = sid_of(sse, "interrupt")
        _, sse = await resume(running, sid, [{"type": "approve"}])
    assert code_of(results_of(sse, "web_fetch")[0]) == "url_not_allowed"
    assert fake_web.fetched == []


async def test_an_attachment_holds_inside_a_subagent(fake_web: FakeWeb) -> None:
    """첨부를 받은 대화는 위임한 서브에이전트의 fetch 도 허용 목록으로 좁힌다(문맥이 같다)."""
    model = ScriptModel(calls=[delegate("researcher", PAGE_A)])
    async with run_app(web_doc(fake_web, on_main=False), model=model) as running:
        _, sse = await send(running, [image()], f"{PAGE_A} 을 조사해 줘")
    [result] = results_of(sse, "web_fetch")
    assert code_of(result) == "url_not_allowed" and result.get("agent")
    assert fake_web.fetched == []


async def test_a_document_attachment_counts_too(fake_web: FakeWeb) -> None:
    async with run_app(web_doc(fake_web)) as running:
        _, sse = await send(running, [document()], fetch_message(PAGE_A))
    assert code_of(results_of(sse, "web_fetch")[0]) == "url_not_allowed"


def test_domain_rules_match_the_platform_table():
    """런타임의 A-label 과 레이블 경계 규칙이 플랫폼과 같은 표를 지난다."""
    from runtime.web_urls import a_label

    table = json.loads((sample_path("domains")).read_text())
    assert [[value, a_label(value)] for value, _ in table["a_label"]] == table["a_label"]
    for host, domains, want in table["matches"]:
        assert domain_matches(host, domains) is want, host
