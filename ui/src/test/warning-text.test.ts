import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { type Messages, dictionary } from "@/i18n";
import {
  PROSE_WARNINGS,
  describeWarning,
  failureAdvice,
  isFailed,
  isProgressing,
  missingResource,
  phaseText,
  progressBanner,
  warningCode,
  warningKey,
} from "@/lib/warningText";
import { elapsedSeconds } from "@/lib/utils";

import { POLITE_ENDING, sentences, serverError } from "./text";

const ko = dictionary("ko");
const en = dictionary("en");

/** 대시로 설명을 이은 자리. 긴 대시와 중간 대시는 어디에 있어도, 붙임표는 앞뒤가 띄어 있을 때 잡는다. */
const DASH = /[—–]|\s-\s/;

/** 도구 이름을 모를 때 서버가 백엔드 종류를 보고 골라 붙이는 고치는 법이다 (services/gateways.unknown_names_hint). */
const FIX_HINTS = {
  refresh: "(run refresh-tools)",
  redeploy: "(redeploy the source; internal and published items have no refresh-tools)",
  both: "(run refresh-tools, or redeploy the source for internal and published items)",
};
/** 앞머리에 허브 항목 이름을 단 Gateway 의 필터 미검증 경고다. */
const UNVALIDATED_FILTER = `weather: tool_names unknown, tools filter not validated ${FIX_HINTS.refresh}`;

interface ShownWarning {
  key: string;
  label: string;
  text: string;
}

/**
 * 한 언어의 경고 문장 전부. 고치는 법을 받는 문장은 서버가 고를 수 있는 셋을 저마다 이어 붙인 결과를 본다. 원인을 받는 문장은
 * 원인마다 이어 붙인 결과이고, 허브 항목 상세의 문장도 넣는다.
 */
/** 목록으로 끝나는 문장(`…: {namespaces}`)은 화면이 목록을 채운다. 문체는 목록을 뗀 문장으로 본다. */
function withoutTrailingList(text: string): string {
  return text.replace(/: \{namespaces\}$/, ".");
}

function shownWarnings(t: Messages): ShownWarning[] {
  const byCode = Object.entries(t.warnings.codes).flatMap(([code, entry]) => {
    if ("choices" in entry) return Object.entries(entry.choices).map(([choice, text]) => ({ key: `${code}/${choice}`, label: entry.label, text }));
    if ("withReason" in entry) return [{ key: code, label: entry.label, text: entry.withReason("Pending") }];
    return [{ key: code, label: entry.label, text: typeof entry.text === "string" ? withoutTrailingList(entry.text) : entry.text({ schedules: 2, triggers: 1, broken: 3 }) }];
  });
  const caused = Object.entries(t.warnings.codes.agent_tool_hidden.causes).map(([cause]) => ({
    key: `agent_tool_hidden/${cause}`,
    ...describeWarning(t, `agent_tool_hidden: hidden (cause: ${cause}; tools: a)`),
  }));
  const hub = Object.entries(t.warnings.hubCodes).map(([code, entry]) => ({ key: `hub/${code}`, label: entry.label, text: entry.text }));
  const prose = PROSE_WARNINGS.flatMap((p) =>
    Object.values(FIX_HINTS).map((hint) => {
      const { label, text } = describeWarning(t, `${p.match} ${hint}`);
      return { key: p.match, label, text };
    }),
  );
  return [...byCode, ...caused, ...hub, ...prose];
}

describe.each([
  ["ko", ko],
  ["en", en],
])("경고 표 (%s)", (_language, t) => {
  it("문장과 이름에 경고 코드가 들어가지 않는다", () => {
    const codes = Object.keys(t.warnings.codes);
    const leaked = shownWarnings(t).filter((w) => codes.some((code) => w.text.includes(code) || w.label.includes(code)));
    expect(leaked).toEqual([]);
  });

  it("대시로 설명을 잇지 않는다", () => {
    const dashed = shownWarnings(t).filter((w) => DASH.test(w.text));
    expect(dashed).toEqual([]);
  });
});

describe("노출 앞단의 넘겨받기 경고 (P54 11절)", () => {
  const LINE = "front_handover_pending: the external address still goes through the previous front proxy, because the new vMCP front cannot take over yet";
  const RAW = `${LINE} (reason: Pending)`;

  it("서버 문장의 vMCP phase 를 사전 문장에 넣는다", () => {
    expect(describeWarning(ko, RAW)).toMatchObject({
      code: "front_handover_pending",
      label: "앞단 전환 대기",
      text: "이 MCP Server 의 외부 주소는 아직 이전 앞단 프록시로 연결됩니다. 새 앞단이 Pending 상태라서 바꾸지 않았습니다. 이 상태가 이어지면 Advanced 탭에서 새 앞단의 클러스터 리소스를 확인해 주세요.",
      known: true,
    });
    expect(describeWarning(en, RAW).text).toBe(
      "This MCP Server's external address still goes through the previous front proxy. The new front is Pending, so the platform has not switched yet. If this lasts, check the new front's cluster resources in the Advanced tab.",
    );
  });

  it("실패한 단계의 코드도 그대로 넣는다", () => {
    const text = describeWarning(ko, `${LINE} (reason: name_taken)`).text;
    expect(text).toContain("새 앞단이 name_taken 상태라서 바꾸지 않았습니다.");
    expect(describeWarning(en, `${LINE} (reason: authz_not_ready)`).text).toContain("The new front is authz_not_ready, so");
  });

  it("까닭이 없거나 영문자와 밑줄 밖의 글이면 서버 원문을 그대로 보인다", () => {
    for (const raw of [LINE, `${LINE} (Pending)`, `${LINE} (reason: not ready)`]) {
      expect(describeWarning(ko, raw)).toMatchObject({ code: "front_handover_pending", text: raw, known: false });
    }
  });

  it("서버 상수의 코드 그대로 표에 있다", () => {
    const src = readFileSync(resolve(process.cwd(), "../platform/daegam/services/exposure_front.py"), "utf8");
    expect(/^FRONT_HANDOVER_PENDING = "front_handover_pending"$/m.test(src)).toBe(true);
    expect(Object.hasOwn(ko.warnings.codes, "front_handover_pending")).toBe(true);
  });
});

describe("배포가 멈춘 자동 실행 경고 (P41 3·10절)", () => {
  const RAW = "runs_paused_by_deploy: this deploy paused 2 schedule(s) and 1 trigger(s) that run as someone else, and 3 whose settings no longer hold";

  it("서버 문장의 수를 사전 문장에 넣는다", () => {
    expect(describeWarning(ko, RAW)).toMatchObject({
      code: "runs_paused_by_deploy",
      label: "자동 실행 멈춤",
      text: "이 배포로 다른 사람의 이름으로 도는 일정 2개와 트리거 1개, 설정이 맞지 않는 3개가 멈춥니다.",
      known: true,
    });
    expect(describeWarning(en, RAW).text).toBe(
      "This deploy pauses 2 schedules and 1 trigger that run as someone else, and 3 whose settings no longer hold.",
    );
  });

  it("수를 읽지 못하면 서버 원문을 그대로 보인다", () => {
    const raw = "runs_paused_by_deploy: paused some sources";
    expect(describeWarning(ko, raw)).toMatchObject({ code: "runs_paused_by_deploy", text: raw, known: false });
  });
});

/**
 * 원인 문장 끝의 괄호. Tool 숨김의 원인은 정한 문구대로 "원인: … 없습니다(규칙에 더해 주세요)." 꼴이라 괄호 앞과 괄호 안을 따로
 * 본다(P52 화면 문구 표).
 */
const TRAILING_ASIDE = /\(([^)]+)\)\.$/;

describe("한국어 경고 표", () => {
  it("모든 문장이 합니다체로 끝난다", () => {
    const bad = shownWarnings(ko).flatMap((w) =>
      sentences(w.text)
        .flatMap((s) => {
          const aside = TRAILING_ASIDE.exec(s);
          return aside ? [`${s.slice(0, aside.index)}.`, `${aside[1]}.`] : [s];
        })
        .filter((s) => !POLITE_ENDING.test(s))
        .map((s) => `${w.key}: ${s}`),
    );
    expect(bad).toEqual([]);
  });

  it("표 셀에 들어갈 label 은 16자를 넘지 않는다", () => {
    expect(shownWarnings(ko).filter((w) => w.label.length > 16)).toEqual([]);
  });
});

describe("영어 경고 표", () => {
  it("모든 문장이 마침표로 끝난다", () => {
    expect(shownWarnings(en).filter((w) => !w.text.endsWith("."))).toEqual([]);
  });

  it("표 셀에 들어갈 label 은 32자를 넘지 않는다", () => {
    expect(shownWarnings(en).filter((w) => w.label.length > 32)).toEqual([]);
  });

  it("please 를 쓰지 않는다", () => {
    expect(shownWarnings(en).filter((w) => /\bplease\b/i.test(`${w.label} ${w.text}`))).toEqual([]);
  });

  it("고치는 법은 쉼표 뒤에 이어지는 소문자 문장이다", () => {
    const d = describeWarning(en, UNVALIDATED_FILTER);
    expect(d.text).toContain("To check it, refresh the Tools on the hub item.");
  });
});

describe("경고 코드", () => {
  it("FIX_HINTS 는 서버 unknown_names_hint 의 문장을 글자대로 옮긴 것이다", () => {
    // 정본은 platform/daegam/services/gateway_plan.py 의 unknown_names_hint 다. 서버 문구가 바뀌면 화면이 읽어 내는 문장도 함께 본다
    const src = readFileSync(resolve(process.cwd(), "../platform/daegam/services/gateway_plan.py"), "utf8");
    for (const hint of Object.values(FIX_HINTS)) expect(src, hint).toContain(`"${hint.slice(1, -1)}"`);
  });

  it.each([
    // 검토가 반드시 덮으라고 한 셋
    "network_policy_unenforced",
    "pending_sessions_revision_changed",
    "unauthenticated_exposure",
    // 런타임 기능 기록의 경고 셋(D65). 배포·재배포 응답과 status.warnings 가 같은 코드로 낸다(services/agent_features.py)
    "runtime_features_unknown",
    "runtime_feature_dropped",
    "runtime_features_invalid",
    // 컴파일이 뒤 판의 소스를 뺀 경고(P46 A0, services/agent_status.py TOOLS_SOURCE_NEWER)
    "tools_source_newer",
    // SSE 로만 말하는 외부 서버로 그 전에 만든 허브 항목(services/hub.py OUTSIDE_SSE_UNSUPPORTED)
    "outside_sse_unsupported",
  ])("%s 경고가 표에 있다", (code) => {
    expect(Object.hasOwn(ko.warnings.codes, code)).toBe(true);
  });

  it("턴 위임 토큰 경고 셋은 서버 상수의 코드 그대로 표에 있다 (P53 7절)", () => {
    // 정본은 Gateway 셋의 services/gateway_status.py 와 Agent 하나의 services/agent_status.py 다. 서버가 코드를 바꾸면 이 검사가 잡는다
    const codes = ["gateway_status.py", "agent_status.py"].flatMap((file) => {
      const src = readFileSync(resolve(process.cwd(), `../platform/daegam/services/${file}`), "utf8");
      return [...src.matchAll(/^TURN_TOKENS_[A-Z_]+ = "([a-z_]+)"$/gm)].map((m) => m[1]);
    });
    expect(codes.sort()).toEqual(["turn_tokens_authz_forbid", "turn_tokens_owner_cedar", "turn_tokens_unsupported"]);
    for (const code of codes) expect(Object.hasOwn(ko.warnings.codes, code), code).toBe(true);
  });

  it("Tool 규칙의 경고는 서버 상수의 코드 그대로 표에 있다 (P52 7절)", () => {
    // 정본은 서버, Gateway, 허브 항목의 services/tool_rules.py 와 Agent 의 services/agent_status.py·tool_pins.py 다
    const files = ["tool_rules.py", "agent_status.py", "tool_pins.py"];
    const codes = files.flatMap((file) => {
      const src = readFileSync(resolve(process.cwd(), `../platform/daegam/services/${file}`), "utf8");
      return [...src.matchAll(/^(?:RULES_[A-Z_]+|CONDITIONS_[A-Z_]+|AGENT_TOOL_HIDDEN|RUNTIME_TOOL_REFUSALS|TOOL_MARKS_CHANGED) = "([a-z_]+)"$/gm)].map((m) => m[1]);
    });
    expect(codes.length).toBe(13);
    for (const code of codes) expect(Object.hasOwn(ko.warnings.codes, code), code).toBe(true);
  });

  it("Tool 숨김은 서버가 적은 원인의 문장을 잇고 Tool 이름을 아래에 둔다", () => {
    const raw = "agent_tool_hidden: a Tool rule hides Tools this Agent selected (cause: oidc; tools: create_order, refund)";
    const d = describeWarning(ko, raw);
    expect(d.text).toBe(`${ko.warnings.codes.agent_tool_hidden.text} ${ko.warnings.codes.agent_tool_hidden.causes.oidc}`);
    expect(d.detail).toBe("create_order, refund");
    // 모르는 원인이면 앞 문장만이다
    expect(describeWarning(ko, "agent_tool_hidden: hidden (cause: later)").text).toBe(ko.warnings.codes.agent_tool_hidden.text);
    // 원인은 서버의 HIDDEN_CAUSES 와 같다
    const src = readFileSync(resolve(process.cwd(), "../platform/daegam/services/agent_status.py"), "utf8");
    const causes = [...src.matchAll(/^HIDDEN_[A-Z_]+ = "([a-z_]+)"$/gm)].map((m) => m[1]);
    expect(causes.sort()).toEqual(Object.keys(ko.warnings.codes.agent_tool_hidden.causes).sort());
  });

  it("뒤 판의 소스 경고는 사전 문장 아래에 뺀 노드와 그 종류를 보인다 (P46 A0)", () => {
    // 정본은 platform/daegam/services/agent_status.py newer_source_warning 이다. 코드는 그 파일의 상수와 같다
    const src = readFileSync(resolve(process.cwd(), "../platform/daegam/services/agent_status.py"), "utf8");
    expect(src).toMatch(/^TOOLS_SOURCE_NEWER = "tools_source_newer"$/m);
    const raw = "tools_source_newer: tools nodes whose source kind this platform version does not know are left out of agent.json, so the pod starts without their Tools (tools-billing: later_kind)";
    expect(describeWarning(ko, raw)).toMatchObject({ text: ko.warnings.codes.tools_source_newer.text, detail: "tools-billing: later_kind", known: true });
  });

  it("허브 항목 상세는 규칙의 Tool 없음을 항목의 고치는 법으로 말한다", () => {
    const raw = "rules_tool_missing: some Tools with rules are no longer offered (send_mail)";
    expect(describeWarning(ko, raw).text).toBe(ko.warnings.codes.rules_tool_missing.text);
    expect(describeWarning(ko, raw, "hub")).toMatchObject({ text: ko.warnings.hubCodes.rules_tool_missing.text, detail: "send_mail" });
  });

  it("게시된 백엔드의 경고는 사전 문장 아래에 걸린 백엔드 이름을 보인다 (P57 6.4절)", () => {
    // 서버의 코드와 화면의 표가 같다(platform/daegam/services/published_backends.py)
    const src = readFileSync(resolve(process.cwd(), "../platform/daegam/services/published_backends.py"), "utf8");
    const codes = [...src.matchAll(/^PUBLISHED_[A-Z_]+ = "([a-z_]+)"$/gm)].map((m) => m[1]);
    expect(codes.sort()).toEqual(["published_backend_closed", "published_consumer_pending", "published_names_unknown", "published_owner_forbid"]);
    for (const code of codes) expect(Object.keys(ko.warnings.codes)).toContain(code);
    const raw = "published_consumer_pending: a published Gateway used as a backend has not been rendered again (orders-pub, ops)";
    expect(describeWarning(ko, raw)).toMatchObject({ label: "백엔드 연결 대기", detail: "orders-pub, ops", known: true });
    expect(describeWarning(ko, "published_owner_forbid: this Gateway's Authz policy holds a forbid statement").detail).toBeNull();
  });

  it("Code mode 를 끈 경고는 서버가 적은 까닭의 문장을 고르고 걸린 이름을 아래에 둔다 (P56 6.3·6.7절)", () => {
    const closed = ko.warnings.codes.code_mode_closed;
    const head = "code_mode_closed: code mode is off: the definition turns it on, but this render leaves it out";
    expect(describeWarning(ko, `${head} (cause: tools; tools: create_order, refund)`)).toMatchObject({
      label: "Code mode 꺼짐",
      text: closed.choices.tools,
      detail: "create_order, refund",
      known: true,
    });
    expect(describeWarning(ko, `${head} (cause: toolhive)`)).toMatchObject({ text: closed.choices.toolhive, detail: null });
    expect(describeWarning(en, `${head} (cause: toolhive)`).text).toBe("This cluster's ToolHive does not support Code mode, so Code mode was turned off. Ask a platform admin to upgrade ToolHive.");
    // 이름을 모르는 백엔드가 까닭이면 허브 항목 이름이 아래에 선다
    expect(describeWarning(ko, `${head} (cause: unknown_names; backends: weather)`)).toMatchObject({ text: closed.choices.unknown_names, detail: "weather" });
    // 저장 검사와 같은 까닭도 껐다고 말한다. 켤 수 없다는 오류 문장과 다르다
    for (const cause of ["unknown_names", "reserved_name", "exposed", "owner_cedar"] as const) {
      expect(closed.choices[cause]).toContain("Code mode 를 껐습니다.");
      expect(en.warnings.codes.code_mode_closed.choices[cause]).toContain("so Code mode was turned off.");
    }
    // 모르는 까닭이면 서버 원문이다
    const later = `${head} (cause: later)`;
    expect(describeWarning(ko, later)).toMatchObject({ text: later, known: false });
  });

  it("Code mode 의 경고와 까닭은 서버 상수 그대로 표에 있다 (P56 6.3절)", () => {
    // 정본은 platform/daegam/services/gateway_code_mode.py 의 CODE_MODE_CLOSED·CODE_MODE_TOOLS_HIDDEN 과 CLOSED_CAUSES·CAUSE_TOOLHIVE 다
    const src = readFileSync(resolve(process.cwd(), "../platform/daegam/services/gateway_code_mode.py"), "utf8");
    const codes = [...src.matchAll(/^CODE_MODE_(?:CLOSED|TOOLS_HIDDEN) = "([a-z_]+)"$/gm)].map((m) => m[1]);
    expect(codes.sort()).toEqual(["code_mode_closed", "code_mode_tools_hidden"]);
    for (const code of codes) expect(Object.hasOwn(ko.warnings.codes, code), code).toBe(true);
    const constants = new Map([...src.matchAll(/^(CAUSE_[A-Z_]+) = "([a-z_]+)"$/gm)].map((m) => [m[1], m[2]]));
    const table = /^CLOSED_CAUSES = \{\n([\s\S]*?)^\}/m.exec(src)?.[1] ?? "";
    const closed = [...table.matchAll(/: (?:"([a-z_]+)"|([A-Z_]+)),$/gm)].map((m) => m[1] ?? constants.get(m[2]));
    const causes = [...closed, constants.get("CAUSE_TOOLHIVE")];
    expect(causes.sort()).toEqual(Object.keys(ko.warnings.codes.code_mode_closed.choices).sort());
  });

  it("Code mode 가 숨긴 Tool 경고는 사전 문장 아래에 숨은 Tool 이름을 보인다 (P56 6.3절)", () => {
    const raw = "code_mode_tools_hidden: with code mode on, this Gateway hides and refuses Tools whose server does not report them read-only (list_orders, search)";
    expect(describeWarning(ko, raw)).toMatchObject({ label: "Code mode 로 숨김", text: ko.warnings.codes.code_mode_tools_hidden.text, detail: "list_orders, search", known: true });
  });

  it("빠진 기능의 이름은 문장에 들어가지 않는다 (D65)", () => {
    const dropped = describeWarning(ko, "runtime_feature_dropped: dropped: provider_stream_usage");
    expect(dropped.known).toBe(true);
    expect(dropped.text).not.toContain("provider_stream_usage");
  });
});

describe("warningKey", () => {
  it("코드가 같으면 서버 설명이 달라도 같은 경고다", () => {
    // 배포 응답은 코드만, 상태 폴링은 `코드: 영어 설명` 을 준다. 상세 화면이 둘을 따로 그리므로
    // 이 둘을 같은 것으로 보지 않으면 방금 배포한 직후 같은 문장이 두 번 선다
    const fromDeploy = "network_policy_unenforced";
    const fromStatus = "network_policy_unenforced: no NetworkPolicy controller was detected on this cluster (D16)";
    expect(warningKey(fromDeploy)).toBe(warningKey(fromStatus));
  });

  it("코드가 없는 경고는 원문으로 가른다", () => {
    const raw = "some backends have no tool_names; falling back to prefix mode";
    expect(warningKey(raw)).toBe(raw);
  });
});

describe("warningCode", () => {
  it.each([
    // `코드: 설명` 에서 코드만 뗀다
    ["no_shared_state: the graph has no memory node", "no_shared_state"],
    // 콜론 없이 코드만 와도 코드로 읽는다 (배포 응답이 그렇게 준다)
    ["network_policy_unenforced", "network_policy_unenforced"],
    // 코드 꼴이 아닌 앞머리는 코드가 아니다. Gateway 의 도구 충돌 경고는 코드 없이 영어 문장만 온다
    ["some backends have no tool_names; falling back to prefix mode", null],
    // 앞머리가 허브 항목 이름이면 코드로 보인다. describeWarning 이 걸러 낸다
    [UNVALIDATED_FILTER, "weather"],
  ])("warningCode(%j) -> %j", (raw, code) => {
    expect(warningCode(raw)).toBe(code);
  });
});

describe("describeWarning", () => {
  it("표에 있는 코드는 코드도 영어도 없는 한 문장이 된다", () => {
    for (const [code, entry] of Object.entries(ko.warnings.codes)) {
      // 수를 받는 문장은 서버 원문의 수가, 까닭을 받는 문장은 까닭이 있어야 한다. 아래 시험이 따로 본다
      if (!("text" in entry) || typeof entry.text !== "string") continue;
      const d = describeWarning(ko, `${code}: some english explanation (D16)`);
      expect(d.known).toBe(true);
      expect(d.code).toBe(code);
      expect(d.text).toBe(entry.text);
      expect(d.text).not.toContain(code);
      expect(d.text).not.toContain("some english explanation");
      expect(d.text).not.toContain("D16");
    }
  });

  it("코드 없는 Gateway 문장도 조각으로 알아본다", () => {
    const d = describeWarning(ko, "some backends have no tool_names; falling back to prefix mode (tool names become <workload>_<tool>)");
    expect(d.known).toBe(true);
    expect(d.code).toBeNull();
    expect(d.text).toContain("prefix 모드로 전환됩니다");
    expect(d.text).not.toContain("falling back");
  });

  it("앞머리의 허브 항목 이름은 코드로 읽지 않고 문장 조각으로 알아본다", () => {
    const d = describeWarning(ko, UNVALIDATED_FILTER);
    expect(d.code).toBeNull();
    expect(d.text).toContain("Tool 필터가 맞는지 확인하지 못했습니다");
  });

  // 서버가 백엔드 종류를 보고 갈라 적으므로 화면이 그 판단을 다시 하지 않는다. 파생 항목에는 새로고침 버튼이 아예 없어 422 다 (D49)
  it.each([
    [UNVALIDATED_FILTER, ["Tool 새로고침을 실행해 주세요"], ["internal·published"]],
    [`calc: tool_names unknown, tools filter not validated ${FIX_HINTS.redeploy}`, ["원본을 다시 배포해 주세요"], ["Tool 새로고침을 실행해 주세요"]],
    [`priority mode with unknown tool_names for a, b: ... ${FIX_HINTS.both} or use prefix mode`, ["Tool 새로고침을 실행하거나", "원본을 다시 배포해 주세요"], []],
  ])("고치는 법은 서버가 고른 것을 따른다: %s", (raw, says, omits) => {
    const { text } = describeWarning(ko, raw);
    for (const s of says) expect(text).toContain(s);
    for (const o of omits) expect(text).not.toContain(o);
  });

  it.each([
    // 표에 없는 코드는 서버 문장을 그대로 보이고, 표 셀에는 그래도 코드만 넣어 한 줄을 지킨다
    ["brand_new_warning: something the UI has never heard of", "brand_new_warning", "brand_new_warning"],
    // 코드도 조각도 못 알아보면 문장을 통째로 쓴다
    ["a wholly unexpected english sentence", null, "a wholly unexpected english sentence"],
    // 표 객체가 물려받은 이름은 표의 코드가 아니다
    ["constructor: inherited from Object", "constructor", "constructor"],
  ])("모르는 경고(%j)는 서버 문장을 그대로 보인다", (raw, code, label) => {
    expect(describeWarning(ko, raw)).toEqual({ code, label, text: raw, known: false, detail: null });
  });
});

describe("phase 문구", () => {
  it.each([
    ["Pending", "준비 중"],
    ["Rolling", "교체 중"],
    ["Degraded", "일부 사용 불가"],
    ["Sleeping", "휴면"],
    ["Waking", "깨우는 중"],
    [null, null],
    ["무엇인가", null],
  ])("phase 마다 사람 말이 붙는다: phaseText(%j) -> %j", (phase, text) => {
    expect(phaseText(ko, phase)).toBe(text);
  });

  it("Pending·Rolling·Waking 만 진행 중이고 폴링 주기와 예상 시간을 문구에 넣는다", () => {
    expect(isProgressing("Pending")).toBe(true);
    expect(isProgressing("Rolling")).toBe(true);
    expect(isProgressing("Waking")).toBe(true);
    expect(isProgressing("Ready")).toBe(false);
    // 잠든 Agent 는 기다릴 것이 없다. 다음 대화가 깨운다
    expect(isProgressing("Sleeping")).toBe(false);
    expect(progressBanner(ko, "Waking", 5)!.text).toContain("다시 띄우는 중입니다");
    const pending = progressBanner(ko, "Pending", 5)!;
    expect(pending.variant).toBe("info");
    // 처음부터 예상을 말한다. 얼마나 기다리면 되는지 모르면 언제 이상한지도 모른다
    expect(pending.text).toContain("보통 30초 안에");
    expect(pending.text).toContain("5초마다");
    expect(progressBanner(ko, "Rolling", 5)!.text).toContain("보통 1분 안에");
    expect(progressBanner(ko, "Ready", 5)).toBeNull();
  });

  it.each([
    // Pending 은 120초, Rolling 은 180초를 넘기면 warn 이다
    ["Pending", 119, "info"],
    ["Pending", 121, "warn"],
    ["Rolling", 179, "info"],
    ["Rolling", 181, "warn"],
    // 깨우기는 파드 하나가 뜨는 것이라 Pending 과 같은 120초다
    ["Waking", 119, "info"],
    ["Waking", 121, "warn"],
    // 흐른 시간을 모르면(옛 문서라 since 가 없다) 늘 하던 말만 한다
    ["Pending", null, "info"],
  ])("예상 시간을 넘기면 배너가 warn 으로 바뀐다: progressBanner(%j, 5, %j) -> %s", (phase, elapsed, variant) => {
    expect(progressBanner(ko, phase, 5, elapsed)?.variant).toBe(variant);
  });

  it("예상 시간을 넘긴 배너는 어디를 보라고 말한다", () => {
    const slow = progressBanner(ko, "Pending", 5, 121)!;
    expect(slow.text).toContain("예상보다 오래 걸립니다");
    expect(slow.text).toContain("파드 이벤트");
  });

  const now = Date.parse("2026-09-16T00:05:00Z");

  it.each([
    ["2026-09-16T00:00:00Z", 300],
    // 시계가 어긋나 미래로 보여도 음수는 내지 않는다
    ["2026-09-16T00:10:00Z", 0],
    // 없거나 이상하면 null 이다
    [null, null],
    ["어제", null],
  ])("since 로 흐른 시간을 잰다: elapsedSeconds(%j) -> %j", (since, seconds) => {
    expect(elapsedSeconds(since, now)).toBe(seconds);
  });

  it("Failed·Missing 만 다음 행동을 안내한다", () => {
    expect(isFailed("Failed")).toBe(true);
    expect(isFailed("Missing")).toBe(true);
    expect(isFailed("Degraded")).toBe(false);
    expect(failureAdvice(ko, "Failed")).toContain("다시 배포해 주세요");
    expect(failureAdvice(ko, "Missing")).toContain("클러스터 연결");
    expect(failureAdvice(ko, "Ready")).toBeNull();
  });
});

describe("missingResource", () => {
  const deletedAt = "2026-09-02T10:00:00+00:00";

  it.each([
    ["404 는 원래 없는 것이다", serverError(404, "not_found", "mcp instance x not found"), undefined, { gone: false, deletedAt: null }],
    ["410 은 지워진 것이고 언제 지워졌는지도 온다", serverError(410, "gone", "mcp instance x was deleted", { deleted_at: deletedAt }), undefined, { gone: true, deletedAt }],
    ["500 은 없는 것이 아니다", serverError(500, "internal", "boom"), undefined, null],
    ["ApiError 가 아닌 오류는 없는 것이 아니다", new Error("network"), undefined, null],
    ["오류가 없으면 없는 것이 아니다", null, undefined, null],
    ["앞서 받아 둔 데이터가 있으면 화면을 갈아 끼우지 않는다", serverError(404, "not_found", "gone now"), { id: "mi-1" }, null],
  ])("%s", (_case, error, data, missing) => {
    expect(missingResource(error, data)).toEqual(missing);
  });
});

describe("사전 문장 아래의 이름", () => {
  const raw =
    "builtin_tool_name: tool sources offer tools named like built-in tools (main: write_file from calc; subagents[r]: read_file from fs). The Agent keeps its built-in tools and does not offer these. Rename them at their source, or connect that source through a Gateway in prefix mode.";

  it("builtin_tool_name 은 사전 문장과 함께 겹친 Tool 과 소스의 이름을 낸다", () => {
    const d = describeWarning(ko, raw);
    expect(d.text).toBe(ko.warnings.codes.builtin_tool_name.text);
    expect(d.detail).toBe("main: write_file from calc; subagents[r]: read_file from fs");
  });

  it("이름을 따로 싣지 않는 코드와 모르는 경고에는 detail 이 없다", () => {
    expect(describeWarning(ko, "no_shared_state: graph has no memory node").detail).toBeNull();
    expect(describeWarning(ko, "something odd happened").detail).toBeNull();
    expect(describeWarning(ko, "builtin_tool_name: no names here").detail).toBeNull();
  });
});


describe("코드 실행 게이트 경고 (P40 8절)", () => {
  it("서버 문장의 까닭으로 까닭 문장을 고른다", () => {
    for (const lang of [ko, en]) {
      for (const [reason, text] of Object.entries(lang.clusters.sandbox.reason)) {
        const d = describeWarning(lang, `sandbox_gate_closed: code execution is unavailable on this cluster (reason: ${reason})`);
        expect(d).toMatchObject({ code: "sandbox_gate_closed", label: lang.warnings.codes.sandbox_gate_closed.label, text, known: true });
      }
    }
  });

  it("모르는 까닭은 서버 원문을 보인다", () => {
    const raw = "sandbox_gate_closed: code execution is unavailable on this cluster (reason: weather)";
    expect(describeWarning(ko, raw)).toMatchObject({ known: false, text: raw });
  });

  it("개발용 우회 경고는 한 문장이다", () => {
    const d = describeWarning(ko, "sandbox_override: code execution runs without the isolation check by a development setting");
    expect(d).toMatchObject({ known: true, label: "점검 없이 허용" });
  });
});
