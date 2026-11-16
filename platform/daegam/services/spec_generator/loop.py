"""되먹임 루프. 입력·힌트로 LLM 이 초안(JSON)을 만들면 verify() 로 검사해 실패 항목을 되먹여 고친다.

세 모드가 있다.
- generate: LLM 이 OpenAPI 문서 전체를 만든다. 첫 반복에 입력 원문, 되먹임에는 실패 항목과 직전
  초안 전체.
- refine(D25): 서버가 원본에서 허용 필드만 뽑은 뷰를 보내고(projection.extract), 돌려받은 값을
  원본 deep copy 위에 심는다(projection.apply). 판정은 표면 보호(surface.guard) 와 verify 다.
  뷰가 크면 unit 을 묶음(batch)으로 나눠 순서대로 부르고 묶음마다 iteration 을 기록한다.
  되먹임에는 실패 항목과 그 항목이 가리키는 unit 의 현재 값만 넣는다.
- overrides(D37): 대상이 OpenAPI 문서가 아니라 MCP Server 리비전의 `backends` 조각이다. 그래서
  `check_doc` 을 쓰지 못한다. 판정을 `overrides.findings_for` 가 따로 하고 재료는 표면 보호와 이름
  규칙 둘이다. 묶음이 없다. 뷰가 툴마다 두 칸뿐이라 크기는 job 을 받을 때 막는다.

종료 조건
- 판정에 error 가 없고 report.complete 이면 done (warn 은 허용. F9 verdict 와 같은 규칙).
  refine 은 표면 위반 0 과 refine.unknown 0 도 필요하다. overrides 는 검사할 파이프라인이 없어
  표면 위반 0 과 이름 규칙 통과가 곧 done 이다
- 직전 반복과 error 코드 집합(failed_ids)이 같으면 failed(no_progress). 표면 위반·unknown 도 포함
- 최대 max_iterations(5) 회 → failed(max_iterations). refine 은 묶음마다 센다
- job 누적 토큰(입력+출력)이 token_budget(200k) 를 넘으면 failed(budget)

반복마다 초안을 GridFS 에 저장하고 iterations[] 에 기록하므로 파드가 죽어도 다음 워커가 마지막
초안을 다시 검사해서 그 다음 반복(refine 은 마지막 완료 묶음 다음)부터 이어 간다. 재개 경로도
본 루프와 같은 판정(check_draft, overrides 는 `_judge_overrides`)을 쓰므로 표면 위반 초안은
재개로도 done 이 되지 않는다. 저장된 초안은 뷰가 아니라 문서(overrides 는 조각)이므로 재개는
투영 없이 그 문서를 바로 판정한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from starlette.concurrency import run_in_threadpool

from daegam import urlfetch
from daegam.config import Settings
from daegam.pipeline import convert as pipeline
from daegam.pipeline.verdict import Verdict, verdict_of
from daegam.secrets import SecretStore
from daegam.services.llm_endpoint import LlmEndpoints
from daegam.services.llm_providers import (
    ProviderError,
    SpecProvider,
    build_provider,
    json_object,
)
from daegam.services.spec_generator import overrides, projection, refine, surface
from daegam.services.spec_generator.projection import Unit
from daegam.services.spec_generator.worker import JobContext, Outcome
from daegam.services.usage.ledger import CallSite, UsageLedger, metered
from daegam.store.settings import LLMProvider, MongoPlatformSettingsStore
from daegam.store.spec_jobs import Iteration, SpecJob
from daegam.store.usage import UsageSource

logger = logging.getLogger("daegam.spec_generator")

MAX_ITERATIONS = 5
TOKEN_BUDGET = 200_000
MAX_OUTPUT_TOKENS = 16_000
MAX_URL_CHARS = 200_000  # 되먹임 전 첫 프롬프트에 넣는 URL 본문 상한 (1 MiB 는 너무 크다)
PROVIDER_RETRIES = 2

SYSTEM_PROMPT = """You write OpenAPI 3.0.3 documents that will be exposed as MCP tools.

Output rules (strict):
- Reply with ONE JSON object: the complete OpenAPI document. No prose, no Markdown, no code fences.
- The top level must have "openapi": "3.0.3", "info" (title, version), and a non-empty "paths".
- Every operation MUST have an "operationId" matching [A-Za-z0-9_.-]{1,64}, unique across the
  document, ideally already in [A-Za-z0-9_] (avoid '-' and '.').
- Every operation MUST have a "description" (or "summary") that says what the tool does.
- Use "components.schemas" for request/response bodies and make every $ref resolvable.
- Put the base URL in "servers" when one is known.
- Do not invent SOAP extensions (x-soap) unless the input is a SOAP/WSDL description.
- When you are given verification findings, fix every finding and return the whole document again.
"""

REFINE_SYSTEM_PROMPT = refine.REFINE_SYSTEM_PROMPT

ProviderFactory = Callable[..., SpecProvider]
Fetcher = Callable[..., Any]
Guard = Callable[[dict[str, Any]], list[surface.Violation]]


class DraftParseError(ValueError):
    pass


def parse_draft(text: str) -> dict[str, Any]:
    """LLM 텍스트에서 JSON 객체 하나를 꺼낸다(`llm_providers.json_object`)."""
    try:
        return json_object(text)
    except ValueError as e:
        raise DraftParseError(str(e)) from e


# -- 프롬프트 (generate) ------------------------------------------------------------


def _hints_block(hints: dict[str, Any]) -> str:
    lines = []
    if hints.get("title"):
        lines.append(f"- Title: {hints['title']}")
    if hints.get("base_url"):
        lines.append(f"- Base URL (put it in servers[0].url): {hints['base_url']}")
    if hints.get("style"):
        lines.append(f"- Style: {hints['style']}")
    return ("Hints:\n" + "\n".join(lines) + "\n\n") if lines else ""


def build_first_prompt(
    inp: dict[str, Any], hints: dict[str, Any], *, url_content: str | None = None
) -> str:
    parts = [
        "Produce an OpenAPI 3.0.3 document for the API described below.\n\n",
        _hints_block(hints),
    ]
    if inp.get("text"):
        parts.append("API description:\n<<<\n" + str(inp["text"]).strip() + "\n>>>\n\n")
    if inp.get("url"):
        parts.append(f"Source URL: {inp['url']}\n")
        if url_content is not None:
            parts.append("Fetched content:\n<<<\n" + url_content + "\n>>>\n\n")
    if inp.get("examples"):
        parts.append("Example request/response pairs (JSON):\n")
        for i, ex in enumerate(inp["examples"], 1):
            parts.append(
                f"Example {i}:\nrequest: {json.dumps(ex.get('request'), ensure_ascii=False)}\n"
                f"response: {json.dumps(ex.get('response'), ensure_ascii=False)}\n"
            )
        parts.append("\n")
    if inp.get("partial_spec"):
        ps = inp["partial_spec"]
        text = ps if isinstance(ps, str) else json.dumps(ps, ensure_ascii=False, indent=1)
        parts.append("Partial spec to complete (keep what is there):\n<<<\n" + text + "\n>>>\n\n")
    parts.append("Reply with the JSON document only.")
    return "".join(parts)


def build_feedback_prompt(
    draft_text: str, findings: list[refine.Finding], hints: dict[str, Any]
) -> str:
    return (
        "Your previous OpenAPI document did not pass verification.\n\n"
        + _hints_block(hints)
        + "Findings to fix (every one of them):\n"
        + refine.findings_block(findings)
        + "\n\nPrevious document:\n<<<\n"
        + draft_text
        + "\n>>>\n\nReturn the complete corrected JSON document only."
    )


# -- 검증 ---------------------------------------------------------------------------


@dataclass
class Checked:
    """초안 하나의 검증 결과."""

    draft_text: str
    ok: bool
    findings: list[refine.Finding]
    # no_progress 비교용 (verdict error 코드 + surface.changed + refine.unknown)
    error_ids: list[str]
    report: dict[str, Any] | None
    verdict: Verdict
    report_summary: dict[str, Any] | None

    @property
    def doc(self) -> dict[str, Any]:
        return json.loads(self.draft_text)


def _findings_from(insp: pipeline.Inspection, verdict: Verdict) -> list[refine.Finding]:
    out: list[refine.Finding] = []
    if insp.error is not None:
        out.append({"id": insp.error.code, "message": insp.error.detail, "location": ""})
    for r in (insp.report or {}).get("results") or []:
        if r.get("status") in ("fail", "warn"):
            out.append(
                {"id": r["id"], "message": r.get("message", ""), "location": r.get("location", "")}
            )
    seen = {f["id"] for f in out}
    for item in verdict.items:
        if item.level == "error" and item.code not in seen:
            out.append({"id": item.code, "message": item.message, "location": ""})
    return out


def unparsable(text: str, reason: str) -> Checked:
    """JSON 객체가 아닌 응답. draft_text 는 저장할 본문(응답 원문 또는 refine 의 현재 초안)이다."""
    verdict = Verdict(level="error", items=())
    finding = refine.output_json_finding(reason)
    return Checked(text, False, [finding], [refine.OUTPUT_JSON], None, verdict, None)


async def check_doc(
    doc: dict[str, Any],
    *,
    guard: Guard | None = None,
    extra: Sequence[refine.Finding] = (),
) -> Checked:
    """문서 하나를 판정한다. guard 는 원본 비교 콜백(refine)이고 그 위반은
    `surface.changed:<pointer>` 로, extra 는 응답·투영 단계의 항목(refine.unknown, output.json)
    으로 findings 와 error_ids 에 든다."""
    draft_text = refine.dumps(doc)
    # 응답·투영·표면 단계의 항목이 먼저, verify 항목이 뒤다
    pre: list[refine.Finding] = list(extra)
    if guard is not None:
        pre.extend(refine.surface_finding(v) for v in guard(doc))
    pre_ids = [f["id"] for f in pre]
    insp = await run_in_threadpool(
        pipeline.inspect, draft_text.encode("utf-8"), filename="draft.json"
    )
    verdict = verdict_of(insp)
    report = insp.report
    findings = pre + _findings_from(insp, verdict)
    ok = not pre_ids and verdict.level != "error" and bool(report and report.get("complete"))
    summary = (
        {
            "ok": report.get("ok"),
            "complete": report.get("complete"),
            "summary": report.get("summary"),
        }
        if report
        else None
    )
    error_ids = sorted({*pre_ids, *(i.code for i in verdict.items if i.level == "error")})
    return Checked(draft_text, ok, findings, error_ids, report, verdict, summary)


async def check_draft(
    text: str, *, guard: Guard | None = None, extra: Sequence[refine.Finding] = ()
) -> Checked:
    """LLM 텍스트(generate) 또는 저장된 초안 본문(재개)을 파싱해 check_doc 으로 판정한다."""
    try:
        doc = parse_draft(text)
    except DraftParseError as e:
        return unparsable(text, str(e))
    return await check_doc(doc, guard=guard, extra=extra)


# -- refine 의 재료 ---------------------------------------------------------------------


@dataclass
class _RefinePrompts:
    """refine 이 LLM 에 보내는 두 프롬프트. 묶음 하나의 첫 프롬프트와 되먹임 프롬프트다.

    둘 다 `projection.extract` 로 뷰를 만들어 보낸다. 첫 프롬프트는 묶음의 unit 전부를, 되먹임은
    실패 항목이 가리키는 unit 만 담는다."""

    job: SpecJob
    batches: list[list[Unit]]
    rename: bool

    @property
    def total(self) -> int:
        """묶음 수. 프롬프트에 `part k of m` 으로 적힌다."""
        return len(self.batches)

    def first(self, base: dict[str, Any], k: int) -> list[dict[str, str]]:
        view = projection.extract(base, rename=self.rename, units=self.batches[k - 1])
        return [
            {
                "role": "user",
                "content": refine.build_first_prompt(
                    view,
                    presets=self.job.presets,
                    instruction=self.job.instruction,
                    batch=(k, self.total),
                ),
            }
        ]

    def feedback(self, base: dict[str, Any], k: int, checked: Checked) -> list[dict[str, str]]:
        units: list[Unit] = refine.units_for_findings(base, checked.findings, self.batches[k - 1])
        view = projection.extract(base, rename=self.rename, units=units)
        return [{"role": "user", "content": refine.build_feedback_prompt(checked.findings, view)}]


@dataclass
class _RefineState:
    """묶음 루프가 한 바퀴마다 갱신하는 것. 재개는 이 상태를 복원하는 일이다."""

    base: dict[str, Any]
    """지금 기준 문서. 원본이거나 마지막으로 판정한 초안이다."""
    batch: int
    """지금 도는 묶음 번호 (1..m)."""
    prev_error_ids: list[str]
    """직전 반복의 error 코드. 같으면 no_progress 다. 묶음이 바뀌면 비운다."""
    messages: list[dict[str, str]] | None
    """다음에 보낼 프롬프트. None 은 "이 묶음의 첫 프롬프트를 만들라" 는 뜻이다."""
    last_verdict: dict[str, Any] | None


# -- 반복 기록과 실패 결과 -----------------------------------------------------------------
# 세 모드의 루프가 같은 모양으로 쓰는 것만 모았다. 루프 본문과 재개는 모드마다 따로 둔다


def _provider_failed(e: ProviderError, verdict: dict[str, Any] | None) -> Outcome:
    return Outcome(state="failed", verdict=verdict, error={"code": e.code, "detail": e.detail})


def _max_iterations(job: SpecJob, verdict: dict[str, Any] | None, detail: str) -> Outcome:
    """반복 상한에 걸린 실패. 초안 자리는 마지막 반복의 것이다."""
    last = job.iterations[-1] if job.iterations else None
    return Outcome(
        state="failed",
        draft_ref=last.draft_ref if last else None,
        verdict=verdict,
        error={"code": "max_iterations", "detail": detail},
    )


async def _record(
    ctx: JobContext,
    n: int,
    checked: Checked,
    usage: dict[str, int],
    *,
    batch: int | None = None,
    batch_total: int | None = None,
) -> str:
    """반복 n 의 초안을 저장하고 iterations[] 에 적는다. 저장한 초안의 ref 를 돌려준다."""
    ref = await ctx.save_draft(n, checked.draft_text.encode("utf-8"))
    await ctx.push_iteration(
        Iteration(
            n=n,
            draft_ref=ref,
            report_summary=checked.report_summary,
            failed_ids=checked.error_ids,
            usage=usage,
            note=None if checked.ok else "; ".join(f["id"] for f in checked.findings)[:500],
            batch=batch,
            batch_total=batch_total,
        )
    )
    return ref


# -- runner ---------------------------------------------------------------------------


class MeteredProvider:
    """SpecProvider 를 감싸 부를 때마다 원장에 남긴다 (P37 7절). 재시도 한 번도 호출 하나다.
    사용량 없이 끝난 호출은 글자 수로 어림해 원장과 job 의 사용량에 함께 센다."""

    def __init__(self, inner: SpecProvider, ledger: UsageLedger | None, site: CallSite):
        self.inner = inner
        self.ledger = ledger
        self.site = site
        self.name = inner.name
        self.model = inner.model

    async def complete(
        self, system: str, messages: list[dict[str, str]], *, max_tokens: int
    ) -> tuple[str, dict[str, int]]:
        async with metered(self.ledger, self.site) as meter:
            text, usage = await self.inner.complete(system, messages, max_tokens=max_tokens)
            if usage:
                meter.report(usage)
            else:
                usage = meter.estimate([system, *(m["content"] for m in messages)], [text])
        return text, usage


class RoutedProvider:
    """job 의 provider. 부를 때마다(재시도 한 번도) 길을 다시 고르고 그 길의 provider 를 만든다
    (P38 6절). 게이트웨이를 끄거나 켠 것이 재시도하는 job 에도 곧바로 닿는다. 원장 행은 부른 길의
    주소로 남는다."""

    def __init__(
        self, runner: SpecGeneratorRunner, job: SpecJob, cfg: LLMProvider, first: MeteredProvider
    ):
        self.runner = runner
        self.job = job
        self.cfg = cfg
        self.name = first.name
        self.model = first.model
        # job 을 시작할 때 고른 길이다. 첫 호출만 쓴다
        self._first: MeteredProvider | None = first

    async def complete(
        self, system: str, messages: list[dict[str, str]], *, max_tokens: int
    ) -> tuple[str, dict[str, int]]:
        provider, self._first = self._first, None
        if provider is None:
            provider = await self.runner._metered(self.job, self.cfg)
        return await provider.complete(system, messages, max_tokens=max_tokens)


class SpecGeneratorRunner:
    """SpecJobRunner 구현. 워커가 job 마다 run(ctx) 를 부른다."""

    def __init__(
        self,
        platform_settings: MongoPlatformSettingsStore,
        secret_store: Callable[[], SecretStore | None] | SecretStore | None,
        settings: Settings,
        *,
        provider_factory: ProviderFactory = build_provider,
        fetch: Fetcher = urlfetch.fetch_url,
        max_iterations: int = MAX_ITERATIONS,
        token_budget: int = TOKEN_BUDGET,
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
        retry_delay: float = 1.0,
        view_max_bytes: int = refine.REFINE_VIEW_MAX_BYTES,
        batch_ops: int = refine.REFINE_BATCH_OPS,
        usage_ledger: UsageLedger | None = None,
        endpoints: LlmEndpoints | None = None,
    ):
        self.platform_settings = platform_settings
        self._secret_store = secret_store
        self.settings = settings
        # job 마다 provider 의 길을 고른다 (P38 6절). 없으면(테스트) 늘 직접 경로다
        self.endpoints = endpoints or LlmEndpoints(settings, self._secret_store_now)
        self.provider_factory = provider_factory
        self.fetch = fetch
        self.max_iterations = max_iterations
        self.token_budget = token_budget
        self.max_output_tokens = max_output_tokens
        self.retry_delay = retry_delay
        self.view_max_bytes = view_max_bytes
        self.batch_ops = batch_ops
        # 시도마다 원장에 남긴다 (P37 7절). 없으면(테스트) 남기지 않는다
        self.usage_ledger = usage_ledger

    def _secret_store_now(self) -> SecretStore | None:
        ss = self._secret_store
        return ss() if callable(ss) else ss

    async def _provider(self, job: SpecJob) -> SpecProvider:
        """job 의 provider. 길은 부를 때마다 고르고(`RoutedProvider`), 여기서 한 번 미리 골라 키가
        없는 직접 경로 같은 실패로 job 을 곧바로 끝낸다."""
        ps = await self.platform_settings.get()
        cfg: LLMProvider | None = ps.provider(job.provider)
        if cfg is None:
            raise ProviderError("no_provider", f"llm provider {job.provider!r} is not configured")
        return RoutedProvider(self, job, cfg, await self._metered(job, cfg))

    async def _metered(self, job: SpecJob, cfg: LLMProvider) -> MeteredProvider:
        """지금 길의 provider 를 원장에 남기도록 감싼다."""
        endpoint = await self.endpoints.resolve_endpoint(
            cfg, tenant=job.tenant, purpose="spec_generator"
        )
        provider = self.provider_factory(cfg, endpoint)
        # job 하나가 trace 하나이고 시도마다 원장 행이 하나다 (P37 7절)
        site = CallSite(
            tenant=job.tenant,
            source=UsageSource(kind="spec_generator", id=job.mode, name=job.mode),
            provider=cfg,
            model=provider.model,
            ref=job.id,
            user_sub=job.created_by,
            base_url=endpoint.base_url,
        )
        return MeteredProvider(provider, self.usage_ledger, site)

    async def _complete(self, provider: SpecProvider, system: str, messages: list[dict[str, str]]):
        attempt = 0
        while True:
            try:
                return await provider.complete(system, messages, max_tokens=self.max_output_tokens)
            except ProviderError as e:
                if not e.retryable or attempt >= PROVIDER_RETRIES:
                    raise
                attempt += 1
                await asyncio.sleep(self.retry_delay * attempt)

    @staticmethod
    def _tokens(usage: dict[str, int]) -> int:
        return int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))

    def _budget_exhausted(self, ctx: JobContext, n: int, last_verdict) -> Outcome | None:
        """반복 n 을 시작하기 전의 예산 검사. 부등호가 `>=` 인 것은 "이 반복을 시작할 값이 남아
        있는가" 를 묻기 때문이다. 이미 예산과 같은 만큼 썼으면 남은 것이 없으니 부르지 않는다.
        반복을 마친 뒤의 검사(`_stop_after`)는 이유가 반대라 `>` 다. 두 코드가 똑같이
        `budget` 이므로 detail 의 exhausted(시작 전)와 exceeded(마친 뒤)가 어느 검사였는지를
        말한다."""
        if self._tokens(ctx.usage_total) >= self.token_budget:
            return Outcome(
                state="failed",
                verdict=last_verdict,
                error={
                    "code": "budget",
                    "detail": f"token budget {self.token_budget} exhausted before iteration {n}",
                },
            )
        return None

    async def run(self, ctx: JobContext) -> Outcome:
        job = ctx.job
        try:
            provider = await self._provider(job)
        except ProviderError as e:
            return Outcome.failed(e.code, e.detail)

        raw = await ctx.read_input()
        if raw is None:
            return Outcome.failed("input_missing", f"input {job.input_ref} not found")
        try:
            inp = json.loads(raw.decode("utf-8"))
        except ValueError as e:
            return Outcome.failed("input_invalid", f"input is not JSON: {e}")
        if job.mode == "refine":
            if not isinstance(inp, dict) or not inp.get("openapi"):
                return Outcome.failed("input_invalid", "refine input is not an OpenAPI document")
            return await self._run_refine(ctx, provider, inp)
        if job.mode == "overrides":
            if not isinstance(inp, dict) or not isinstance(inp.get("spec"), dict):
                return Outcome.failed("input_invalid", "overrides input has no spec")
            return await self._run_overrides(ctx, provider, inp)
        return await self._run_generate(ctx, provider, inp)

    # -- generate ---------------------------------------------------------------------

    async def _run_generate(
        self, ctx: JobContext, provider: SpecProvider, inp: dict[str, Any]
    ) -> Outcome:
        job = ctx.job
        hints = job.hints or {}

        # 재개: 마지막 초안을 다시 검증해서 되먹임 상태를 복원한다
        prev: Checked | None = None
        last_verdict: dict[str, Any] | None = None
        if job.iterations:
            last = job.iterations[-1]
            if last.draft_ref:
                prev_bytes = await ctx.read_draft(last.draft_ref)
                if prev_bytes is not None:
                    prev = await check_draft(prev_bytes.decode("utf-8", errors="replace"))
                    last_verdict = prev.verdict.to_dict()
                    if prev.ok:
                        return Outcome(
                            state="done",
                            draft_ref=last.draft_ref,
                            report=prev.report,
                            verdict=last_verdict,
                        )
            prev_error_ids = list(last.failed_ids)
        else:
            prev_error_ids = []

        if prev is None:
            url_content: str | None = None
            if inp.get("url"):
                try:
                    body = await self.fetch(inp["url"])
                except (urlfetch.BlockedURL, urlfetch.FetchError) as e:
                    return Outcome.failed(e.code, e.detail)
                url_content = body.decode("utf-8", errors="replace")[:MAX_URL_CHARS]
            messages = [
                {"role": "user", "content": build_first_prompt(inp, hints, url_content=url_content)}
            ]
        else:
            messages = [
                {
                    "role": "user",
                    "content": build_feedback_prompt(prev.draft_text, prev.findings, hints),
                }
            ]

        while len(job.iterations) < self.max_iterations:
            n = len(job.iterations) + 1
            if exhausted := self._budget_exhausted(ctx, n, last_verdict):
                return exhausted
            try:
                text, usage = await self._complete(provider, SYSTEM_PROMPT, messages)
            except ProviderError as e:
                return _provider_failed(e, last_verdict)

            checked = await check_draft(text)
            last_verdict = checked.verdict.to_dict()
            ref = await _record(ctx, n, checked, usage)
            if checked.ok:
                return Outcome(
                    state="done", draft_ref=ref, report=checked.report, verdict=last_verdict
                )
            if stop := self._stop_after(ctx, checked, ref, n, prev_error_ids, last_verdict):
                return stop
            prev_error_ids = checked.error_ids
            messages = [
                {
                    "role": "user",
                    "content": build_feedback_prompt(checked.draft_text, checked.findings, hints),
                }
            ]

        return _max_iterations(
            job, last_verdict, f"no passing draft after {self.max_iterations} iterations"
        )

    def _stop_after(
        self,
        ctx: JobContext,
        checked: Checked,
        ref: str,
        n: int,
        prev_error_ids: list[str],
        last_verdict: dict[str, Any] | None,
    ) -> Outcome | None:
        """실패한 반복 뒤의 중단 판정: budget 초과 또는 no_progress.

        부등호가 `>` 인 것은 이미 돌린 반복을 살리기 때문이다. 정확히 예산만큼 쓰고 끝난 반복은
        예산 안이므로 실패로 접지 않고, 다음 반복은 시작 전 검사(`_budget_exhausted`, `>=`)가
        막는다. 그래서 예산과 정확히 같은 값에서 두 검사가 반대로 판정하는 것이 의도다."""
        if self._tokens(ctx.usage_total) > self.token_budget:
            return Outcome(
                state="failed",
                draft_ref=ref,
                report=checked.report,
                verdict=last_verdict,
                error={
                    "code": "budget",
                    "detail": f"token budget {self.token_budget} exceeded at iteration {n}",
                },
            )
        if checked.error_ids and checked.error_ids == prev_error_ids:
            return Outcome(
                state="failed",
                draft_ref=ref,
                report=checked.report,
                verdict=last_verdict,
                error={
                    "code": "no_progress",
                    "detail": "same errors as the previous iteration: "
                    + ", ".join(checked.error_ids),
                },
            )
        return None

    # -- refine -----------------------------------------------------------------------

    async def _check_refine(
        self, base: dict[str, Any], text: str, *, guard: Guard, rename: bool
    ) -> Checked:
        """LLM 응답(뷰)을 base 위에 투영해 판정한다. JSON 이 아니면 base 를 그대로 둔 채
        output.json 을 낸다."""
        try:
            view = parse_draft(text)
        except DraftParseError as e:
            return unparsable(refine.dumps(base), str(e))
        applied = projection.apply(base, view, rename=rename)
        extra = [refine.unknown_finding(p) for p in applied.unknown]
        return await check_doc(applied.draft, guard=guard, extra=extra)

    async def _resume_refine(
        self, ctx: JobContext, original: dict[str, Any], guard: Guard, prompts: _RefinePrompts
    ) -> _RefineState | Outcome:
        """재개 상태를 복원한다. 마지막 초안을 본 루프와 같은 검사로 다시 판정하므로 표면 위반
        초안은 재개로도 done 이 되지 않는다.

        마지막 묶음까지 통과한 상태였으면 더 돌 것이 없어 `Outcome` 을 돌려준다. 그 밖에는 다음에
        돌 묶음과 기준 문서를 담은 `_RefineState` 다. iterations 가 없으면 처음부터 도는 상태다."""
        job = ctx.job
        state = _RefineState(
            base=original, batch=1, prev_error_ids=[], messages=None, last_verdict=None
        )
        if not job.iterations:
            return state
        last = job.iterations[-1]
        state.batch = min(max(last.batch or 1, 1), prompts.total)
        prev_bytes = await ctx.read_draft(last.draft_ref) if last.draft_ref else None
        if prev_bytes is None:
            return state
        extra = [f for f in (refine.finding_from_id(i) for i in last.failed_ids) if f is not None]
        prev = await check_draft(
            prev_bytes.decode("utf-8", errors="replace"), guard=guard, extra=extra
        )
        state.last_verdict = prev.verdict.to_dict()
        try:
            state.base = prev.doc
        except ValueError:
            state.base = original  # 저장된 초안이 JSON 이 아니면 원본부터 다시
        if not prev.ok:
            state.prev_error_ids = list(last.failed_ids)
            state.messages = prompts.feedback(state.base, state.batch, prev)
            return state
        if state.batch >= prompts.total:
            return Outcome(
                state="done",
                draft_ref=last.draft_ref,
                report=prev.report,
                verdict=state.last_verdict,
            )
        state.batch += 1
        return state

    async def _run_refine(
        self, ctx: JobContext, provider: SpecProvider, original: dict[str, Any]
    ) -> Outcome:
        """묶음을 하나씩 통과시킨다. 한 바퀴는 프롬프트, 응답, 투영·판정, 기록 순이고, 묶음이
        통과하면 다음 묶음으로 넘어간다. 재개 복원은 `_resume_refine` 이 먼저 한다."""
        job = ctx.job
        rename = refine.RENAME_PRESET in job.presets
        rules = surface.refine_rules(rename=rename)

        def guard(doc: dict[str, Any]) -> list[surface.Violation]:
            return surface.guard(original, doc, rules=rules)

        prompts = _RefinePrompts(
            job=job,
            batches=refine.split_batches(
                original, rename=rename, max_bytes=self.view_max_bytes, batch_ops=self.batch_ops
            ),
            rename=rename,
        )
        m = prompts.total
        resumed = await self._resume_refine(ctx, original, guard, prompts)
        if isinstance(resumed, Outcome):
            return resumed
        state = resumed

        while True:
            n = len(job.iterations) + 1
            attempts = sum(1 for it in job.iterations if it.batch == state.batch)
            if attempts >= self.max_iterations:
                return _max_iterations(
                    job,
                    state.last_verdict,
                    f"batch {state.batch}/{m}: no passing draft after"
                    f" {self.max_iterations} iterations",
                )
            if exhausted := self._budget_exhausted(ctx, n, state.last_verdict):
                return exhausted
            if state.messages is None:
                state.messages = prompts.first(state.base, state.batch)
            try:
                text, usage = await self._complete(provider, REFINE_SYSTEM_PROMPT, state.messages)
            except ProviderError as e:
                return _provider_failed(e, state.last_verdict)

            checked = await self._check_refine(state.base, text, guard=guard, rename=rename)
            state.last_verdict = checked.verdict.to_dict()
            ref = await _record(ctx, n, checked, usage, batch=state.batch, batch_total=m)
            state.base = checked.doc
            if checked.ok:
                if state.batch >= m:
                    return Outcome(
                        state="done",
                        draft_ref=ref,
                        report=checked.report,
                        verdict=state.last_verdict,
                    )
                state.batch += 1
                state.prev_error_ids = []
                state.messages = None
                continue
            if stop := self._stop_after(
                ctx, checked, ref, n, state.prev_error_ids, state.last_verdict
            ):
                return stop
            state.prev_error_ids = checked.error_ids
            state.messages = prompts.feedback(state.base, state.batch, checked)

    # -- overrides --------------------------------------------------------------------

    @staticmethod
    def _judge_overrides(
        original: dict[str, Any],
        draft: dict[str, Any],
        versions: list[Any],
        extra: list[refine.Finding],
    ) -> Checked:
        """조각 초안 하나를 판정한다. 본 루프는 투영한 초안을, 재개는 저장된 초안을 그대로 넣는다.

        표면 보호는 언제나 진짜 원본과 견준다. 검사할 파이프라인이 없으므로 report 는 없고
        verdict 는 findings 유무만 담은 빈 판정이다."""
        findings = overrides.findings_for(original, draft, versions, extra=extra)
        return Checked(
            draft_text=refine.dumps(draft),
            ok=not findings,
            findings=findings,
            error_ids=sorted({f["id"] for f in findings}),
            report=None,
            verdict=Verdict(level="error" if findings else "ok", items=()),
            report_summary=None,
        )

    def _check_overrides(
        self,
        original: dict[str, Any],
        base: dict[str, Any],
        text: str,
        versions: list[Any],
        names: list[list[str]],
    ) -> Checked:
        """LLM 응답(뷰)을 base 위에 투영해 판정한다. 투영은 직전 초안 위에 쌓고 표면 보호는
        원본과 견준다. refine 이 guard 를 원본에 묶어 두는 것과 같은 이유다."""
        try:
            view = parse_draft(text)
        except DraftParseError as e:
            return unparsable(refine.dumps(base), str(e))
        applied = overrides.apply(base, view, names)
        extra = [overrides.unknown_finding(p) for p in applied.unknown]
        return self._judge_overrides(original, applied.draft, versions, extra)

    def _overrides_prompt(
        self,
        job: SpecJob,
        base: dict[str, Any],
        names: list[list[str]],
        context: dict[str, Any],
        checked: Checked | None,
    ) -> list[dict[str, str]]:
        view = overrides.extract(base, names)
        if checked is None:
            content = overrides.build_first_prompt(
                view, context, presets=job.presets, instruction=job.instruction
            )
        else:
            content = overrides.build_feedback_prompt(checked.findings, view)
        return [{"role": "user", "content": content}]

    async def _run_overrides(
        self, ctx: JobContext, provider: SpecProvider, inp: dict[str, Any]
    ) -> Outcome:
        """한 바퀴는 프롬프트, 응답, 투영·판정, 기록 순이다. 묶음이 없어 refine 보다 단순하다."""
        job = ctx.job
        original = overrides.fragment(inp["spec"])
        try:
            versions = overrides.versions_from(inp)
        except (ValueError, TypeError) as e:
            return Outcome.failed("input_invalid", f"overrides input versions: {e}")
        names = overrides.tool_names_of(versions)
        context = overrides.context_of(original, names, overrides.details_by_op(versions))

        base = original
        prev_error_ids: list[str] = []
        last_verdict: dict[str, Any] | None = None
        messages: list[dict[str, str]] | None = None

        # 재개: 저장된 초안은 뷰가 아니라 조각이다(투영한 결과이거나, 응답이 JSON 이 아니었으면
        # 그때의 base). 투영 없이 그대로 판정하고, 응답·투영 단계 항목은 failed_ids 에서 되살린다
        if job.iterations:
            last = job.iterations[-1]
            prev_bytes = await ctx.read_draft(last.draft_ref) if last.draft_ref else None
            saved: dict[str, Any] | None = None
            if prev_bytes is not None:
                try:
                    saved = parse_draft(prev_bytes.decode("utf-8", errors="replace"))
                except DraftParseError:
                    pass  # 저장본이 깨졌으면 원본부터 다시
            if saved is not None:
                ids = last.failed_ids
                extra = [f for f in (overrides.finding_from_id(i) for i in ids) if f is not None]
                prev = self._judge_overrides(original, saved, versions, extra)
                last_verdict = prev.verdict.to_dict()
                if prev.ok:
                    return Outcome(state="done", draft_ref=last.draft_ref, verdict=last_verdict)
                base = saved
                prev_error_ids = list(last.failed_ids)
                messages = self._overrides_prompt(job, base, names, context, prev)

        while len(job.iterations) < self.max_iterations:
            n = len(job.iterations) + 1
            if exhausted := self._budget_exhausted(ctx, n, last_verdict):
                return exhausted
            if messages is None:
                messages = self._overrides_prompt(job, base, names, context, None)
            try:
                text, usage = await self._complete(
                    provider, overrides.OVERRIDES_SYSTEM_PROMPT, messages
                )
            except ProviderError as e:
                return _provider_failed(e, last_verdict)

            checked = self._check_overrides(original, base, text, versions, names)
            last_verdict = checked.verdict.to_dict()
            # overrides 의 Checked 는 report_summary 가 늘 None 이라 기록도 None 이다
            ref = await _record(ctx, n, checked, usage)
            base = checked.doc
            if checked.ok:
                return Outcome(state="done", draft_ref=ref, verdict=last_verdict)
            if stop := self._stop_after(ctx, checked, ref, n, prev_error_ids, last_verdict):
                return stop
            prev_error_ids = checked.error_ids
            messages = self._overrides_prompt(job, base, names, context, checked)

        return _max_iterations(
            job, last_verdict, f"no passing draft after {self.max_iterations} iterations"
        )
