import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { describeError } from "@/components/ErrorMessage";
import { ASSISTANT_ABORTED, describeChatError } from "@/components/chat";
import { dictionary } from "@/i18n";
import { REF_KEYS, type RefKey, describeCode, describeErrorLine } from "@/lib/errorText";

import { POLITE_ENDING, sentences, serverError } from "./text";

const ko = dictionary("ko");
const en = dictionary("en");
const ERROR_TEXT = ko.errors.codes;
const SKILL_ISSUE_TEXT = ko.skills.issues;

/** 대시로 설명을 이은 자리. 긴 대시와 중간 대시는 어디에 있어도, 붙임표는 앞뒤가 띄어 있을 때 잡는다. */
const DASH = /[—–]|\s-\s/;

/**
 * 두 문장 규칙의 예외로 한 문장인 문구다(P78 화면 문구 표). turn_access_lost 는 사람이 할 일이 없는 멈춤이라 처방
 * 문장이 없다. invalid_expiry 는 범위가 곧 처방이다. rule_agent_oidc 의 영어는 까닭을 이은 한 문장이고(P52 화면 문구
 * 표), file_refused_executable 의 영어는 해당하는 파일을 늘어놓은 한 문장이다(P40 화면 문구 표).
 */
const ONE_SENTENCE_CODES = new Set([
  "turn_access_lost",
  "invalid_expiry",
  "rule_agent_oidc",
  "file_refused_executable",
  // P55 화면 문구 표에서 한 문장인 허브 이미지와 Secret env 의 오류다
  "image_outside_registry",
  "image_refused",
  "refused_image_not_shareable",
  "secret_not_shareable",
  "env_must_be_secret",
]);

/**
 * '코드 → 화면 문장' 표가 언어와 상관없이 지킬 두 규칙. 오류 표 두 언어와 Skill 검사 표(issueText 가 오류 표로 넘어간다)가
 * 같이 받는다.
 */
function sentenceRules(table: Readonly<Record<string, string>>): void {
  it("문장에 코드가 들어가지 않는다", () => {
    const leaked = Object.entries(table).filter(([code, text]) => text.includes(code));
    expect(leaked).toEqual([]);
  });

  it("무엇을 하면 되는지까지 적혀 두 문장 이상이다", () => {
    const tooShort = Object.entries(table).filter(([code, text]) => !ONE_SENTENCE_CODES.has(code) && sentences(text).length < 2);
    expect(tooShort).toEqual([]);
  });
}

/** 한국어 표는 모든 문장이 합니다체로 끝난다. */
function politeRule(table: Readonly<Record<string, string>>): void {
  it("모든 문장이 합니다체로 끝난다", () => {
    const bad: string[] = [];
    for (const [code, text] of Object.entries(table)) {
      for (const sentence of sentences(text)) {
        if (!POLITE_ENDING.test(sentence)) bad.push(`${code}: ${sentence}`);
      }
    }
    expect(bad).toEqual([]);
  });
}

/** 영어 표는 문장이 마침표로 끝나고 please 없이 명령한다 (P49 7절). 두 문장 이상이라는 규칙은 한국어 표와 같다. */
function englishRules(table: Readonly<Record<string, string>>): void {
  sentenceRules(table);

  it("모든 문장이 마침표로 끝난다", () => {
    expect(Object.entries(table).filter(([, text]) => !text.endsWith("."))).toEqual([]);
  });

  it("please 를 쓰지 않는다", () => {
    expect(Object.entries(table).filter(([, text]) => /\bplease\b/i.test(text))).toEqual([]);
  });
}

describe("Skill 검사 문장 표", () => {
  sentenceRules(SKILL_ISSUE_TEXT);
  politeRule(SKILL_ISSUE_TEXT);

  /*
   * 서버 형식 검사(`services/skill_format.py`)와 zip 풀기(`zipsafe.py`)가 내는 code 를 표가 다 덮는지 본다. 빠지면 그 issue 가
   * 서버의 영어 문장으로 화면에 나온다. 서버는 code 를 글자 그대로 넘기고(`_err(경로, "code", …)`, `_short_text(키, 상한,
   * "code")`, `ArchiveError("code", …)`) 여기는 그 글자를 읽는다. 선례는 agent-graph.test.ts 의 그래프 이슈 검사다.
   */
  it("서버 형식 검사와 zip 풀기가 내는 code 를 하나도 빠뜨리지 않는다", () => {
    const format = readFileSync(resolve(process.cwd(), "../platform/daegam/services/skill_format.py"), "utf8");
    const zipsafe = readFileSync(resolve(process.cwd(), "../platform/daegam/zipsafe.py"), "utf8");
    // code 를 조립하면(f"{key}_too_long") 이 검사가 읽지 못한다
    expect(format).not.toMatch(/\b_(?:err|warn)\(\s*[^,()]+,\s*f"/);
    const issued = [...format.matchAll(/\b_(?:err|warn)\(\s*[^,()]+,\s*"([a-z_]+)"/g)].map((m) => m[1]);
    const shortText = [...format.matchAll(/\b_short_text\(\s*"[^"]+",\s*[A-Z_]+,\s*"([a-z_]+)"\)/g)].map((m) => m[1]);
    const archive = [...zipsafe.matchAll(/\bArchiveError\(\s*"([a-z_]+)"/g)].map((m) => m[1]);
    expect(issued).toContain("skill_scripts_inert");
    expect(shortText).toEqual(expect.arrayContaining(["license_too_long", "compatibility_too_long"]));
    expect(archive).toContain("archive_symlink");
    const serverCodes = [...new Set([...issued, ...shortText, ...archive])];
    expect(serverCodes.filter((c) => !(c in SKILL_ISSUE_TEXT)).sort()).toEqual([]);
    // 거꾸로 표에만 있는 code 도 드러낸다. 서버가 내지 않는 문장은 다음 사람을 헷갈리게 한다
    expect(Object.keys(SKILL_ISSUE_TEXT).filter((c) => !serverCodes.includes(c)).sort()).toEqual([]);
  });
});

describe("코드 표", () => {
  sentenceRules(ERROR_TEXT);
  politeRule(ERROR_TEXT);

  it.each<[keyof typeof ERROR_TEXT, string, string]>([
    // 런타임 이미지가 낡았다는 코드는 세 자리(배포 422·init 409·기동 503)가 같은 이름으로 낸다
    ["runtime_image_too_old", "missing: x", "이미지"],
    // 런타임은 연결이 끊기면 턴을 멈추고 turn_cancelled 로 마감한다(D65)
    ["turn_cancelled", "연결이 끊겨 턴을 멈췄다", "Tool 쪽에서 끝났을 수 있습니다"],
    // 런타임은 edit 의 이름이 요청과 다르면 422 edit_name_mismatch 로 거절하고 아무것도 재개하지 않는다(D65)
    ["edit_name_mismatch", "edit 은 Add 의 인자만 고칠 수 있는데 write_file 이다", "Tool 이름"],
    // 중지 API 의 두 코드(진행 중 턴 없음, 기능 없는 이미지)다. 화면은 202 가 아니면 연결을 끊어 멈추므로(D65) 사람이 볼
    // 일은 드물지만 외부 호출자와 같은 이름이다
    ["no_turn_in_progress", "no turn", "진행 중인 답변이 없습니다"],
    ["runtime_feature_missing", "cancel", "다시 배포"],
    // 기록에는 이름이 있는데 그 기능을 모르는 옛 파드가 답하면 프록시가 409 runtime_rolling 으로 바꾼다(D61)
    ["runtime_rolling", "a runtime pod without the 'cancel' capability answered", "다시 시도해 주세요"],
  ])("%s 코드는 서버 detail 을 버리고 표의 문장을 쓴다", (code, detail, says) => {
    const d = describeCode(ko, code, detail);
    expect(d.text).toBe(ERROR_TEXT[code]);
    expect(d.text).toContain(says);
  });

  it("에이전트 채팅의 중단은 스트림의 turn_cancelled 와 같은 문장이고 비서의 중단은 다르다", () => {
    // 화면이 끊은 쪽이든 서버가 실어 보낸 쪽이든 같은 글을 본다. 비서의 턴은 요청과 떨어진 태스크라 끊겨도 이어지므로
    // 그 사실을 말하는 다른 문장이다
    expect(describeChatError(ko, new DOMException("aborted", "AbortError"))).toBe(ERROR_TEXT.turn_cancelled);
    expect(describeChatError(ko, ASSISTANT_ABORTED)).toBe("요청을 중단했습니다. 서버 쪽 처리는 끝날 때까지 이어집니다.");
  });
});

describe.each([
  ["ko", ko],
  ["en", en],
])("코드 표와 Skill 검사 표의 대시 (%s)", (_language, t) => {
  it.each([
    ["errors.codes", t.errors.codes],
    ["skills.issues", t.skills.issues],
  ])("%s 는 대시로 설명을 잇지 않는다", (_table, table) => {
    expect(Object.entries(table).filter(([, text]) => DASH.test(text))).toEqual([]);
  });
});

describe("영어 코드 표", () => {
  englishRules(en.errors.codes);
});

/** 평가 실행이 오류나 취소로 끝난 까닭의 표 (P45 13절). 실행 목록과 결과 창이 오류 사전보다 먼저 이 표를 읽는다. */
describe("평가 실행의 까닭 표", () => {
  sentenceRules(ko.agents.evaluation.run.errors);
  politeRule(ko.agents.evaluation.run.errors);
});

describe("영어 평가 실행의 까닭 표", () => {
  englishRules(en.agents.evaluation.run.errors);
});

describe("영어 Skill 검사 표", () => {
  englishRules(en.skills.issues);
});

describe("describeError", () => {
  it("표에 있는 코드는 한국어 문장만 쓰고 영어 detail 을 붙이지 않는다", () => {
    const d = describeError(ko, serverError(404, "not_found", "mcp instance 4c1f-... not found"));
    expect(d.text).toBe(ERROR_TEXT.not_found);
    expect(d.text).not.toContain("not_found");
    expect(d.text).not.toContain("not found");
    expect(d.detail).toBeUndefined();
    // 코드는 사람이 읽는 문장 밖에 따로 둔다 (디버깅용)
    expect(d.code).toBe("not_found");
  });

  it("표의 모든 코드가 코드 문자열 없는 문장을 낸다", () => {
    for (const code of Object.keys(ERROR_TEXT)) {
      const d = describeError(ko, serverError(422, code, "some english detail"));
      expect(d.text).not.toContain(code);
      expect(d.text).not.toContain("some english detail");
    }
  });

  it("클러스터 권한이 모자란 422 는 클러스터 관리자에게 맡길 일을 말한다 (P50 G10, P63)", () => {
    const body = { feature: "tenant_secrets", needed: "x", found: "y" };
    const d = describeError(ko, serverError(422, "cluster_rights_missing", "writing Secrets ...", body));
    expect(d.text).toBe(ERROR_TEXT.cluster_rights_missing);
    expect(d.text).toContain("rbac.yaml");
    expect(describeError(en, serverError(422, "cluster_rights_missing", "x", body)).text).toContain("run preflight again");
  });

  it("표에 없는 코드도 영어 detail 만 덩그러니 나오지 않는다", () => {
    const d = describeError(ko, serverError(422, "brand_new_code", "something went sideways"));
    expect(d.text).toBe(ko.errors.fallback);
    expect(d.text).not.toContain("brand_new_code");
    expect(d.detail).toBe("something went sideways");
    expect(d.code).toBe("brand_new_code");
  });

  it("409 참조 목록은 받아 둔 references 의 이름을 쓴다", () => {
    const names = new Map([
      ["ms-1", "주문 MCP"],
      ["ag-2", "상담 에이전트"],
      ["kb-3", "제품 FAQ"],
    ]);
    const d = describeError(
      ko,
      serverError(409, "in_use", "data service is bound by MCP profiles", {
        mcp_server_ids: ["ms-1"],
        agent_ids: ["ag-2"],
        knowledge_base_ids: ["kb-3"],
        token_ids: ["tok-4"],
      }),
      names,
    );
    // 토큰은 이름이 없어 종류와 개수만 말한다. 어디에도 날 id 가 없다 (D43 넷째)
    expect(d.refs).toEqual(["MCP Server 주문 MCP", "Agent 상담 에이전트", "Knowledge Base 제품 FAQ", "읽기 토큰 1개"]);
    for (const ref of d.refs) {
      expect(ref).not.toContain("_ids");
      expect(ref).not.toMatch(/\b(ms-1|ag-2|kb-3|tok-4)\b/);
    }
  });

  it("이름을 모르면 종류와 개수만 말한다", () => {
    const d = describeError(ko, serverError(409, "in_use", "x", { mcp_server_ids: ["ms-1", "ms-2"] }));
    expect(d.refs).toEqual(["MCP Server 2개"]);
  });

  it("영어 사전으로는 문장과 참조 줄이 영어다", () => {
    const d = describeError(en, serverError(409, "in_use", "x", { mcp_server_ids: ["ms-1", "ms-2"], other_tenant_agents: 3 }));
    expect(d.text).toBe(en.errors.codes.in_use);
    expect(d.refs).toEqual(["MCP Server (2)", "Agents in other tenants (3)"]);
    expect(describeError(en, serverError(422, "brand_new_code", "x")).text).toBe(en.errors.fallback);
  });

  /**
   * 서버가 409 `in_use` 의 extra 로 내는 참조 키. 표가 이것을 다 덮지 못하면 화면이
   * "연결을 먼저 해제해 주세요" 와 빈 목록을 함께 준다. 표 자신을 순회하면 빠진 키를 원리상 못 잡으므로
   * 서버 쪽 목록을 여기 적어 두고 그것을 기준으로 본다. 서버가 키를 늘리면 여기와 표를 함께 고친다.
   * 나오는 자리는 `services/references.py` 의 가드 표(Database·Backend API·MCP Server·Gateway·허브 항목·
   * Knowledge Base·Skill 의 삭제와 배포 해제)다.
   */
  const SERVER_REF_KEYS: readonly RefKey[] = [
    "mcp_server_ids",
    "deployed_mcp_server_ids",
    "gateway_ids",
    "agent_ids",
    "deployed_agent_ids",
    "knowledge_base_ids",
    "token_ids",
  ];

  describe.each([
    ["ko", ko],
    ["en", en],
  ])("참조 키 (%s)", (_language, t) => {
    it.each(SERVER_REF_KEYS)("서버가 내는 참조 키 %s 를 종류 이름으로 읽는다", (key) => {
      const d = describeError(t, serverError(409, "in_use", "x", { [key]: ["id-1"] }));
      expect(d.refs).toEqual([t.errors.refCount(t.errors.refNames[key], 1)]);
    });

    it("표에만 있고 서버가 안 내는 참조 키가 없다", () => {
      // 죽은 라벨은 다음 사람을 헷갈리게 한다
      expect(Object.keys(t.errors.refNames).sort()).toEqual([...SERVER_REF_KEYS].sort());
    });
  });

  it("푸는 키 목록이 서버가 내는 참조 키와 같다", () => {
    expect([...REF_KEYS].sort()).toEqual([...SERVER_REF_KEYS].sort());
  });

  it("참조가 없으면 목록도 비어 있다", () => {
    expect(describeError(ko, serverError(422, "invalid_name", "bad")).refs).toEqual([]);
  });

  it("ApiError 가 아닌 것은 그대로 지난다", () => {
    expect(describeError(ko, new Error("boom"))).toEqual({ text: "boom", refs: [] });
    expect(describeError(ko, "문자열")).toEqual({ text: "문자열", refs: [] });
  });
});

describe("describeCode", () => {
  it.each([
    // 표에 있는 코드는 한국어 처방이고 서버 detail 은 버린다
    ["cluster_unreachable", "HTTPSConnectionPool(host='10.0.0.1', port=6443)", { text: ERROR_TEXT.cluster_unreachable, known: true, code: "cluster_unreachable" }],
    // 표에 없는 코드는 감추지 않고 원문을 보인다
    ["weird_thing", "upstream said no", { text: "weird_thing: upstream said no", known: false, code: "weird_thing" }],
    // 코드도 detail 도 없으면 폴백 문장이다
    [null, undefined, { text: ko.errors.fallback, known: false, code: null }],
    [undefined, "", { text: ko.errors.fallback, known: false, code: null }],
    // 표 객체가 물려받은 이름은 표의 코드가 아니다
    ["constructor", "inherited from Object", { text: "constructor: inherited from Object", known: false, code: "constructor" }],
  ])("describeCode(%j, %j)", (code, detail, described) => {
    expect(describeCode(ko, code, detail)).toEqual(described);
  });
});

describe("describeErrorLine", () => {
  it.each([
    // `코드: 설명` 한 줄을 갈라 표에 태운다. 서버가 last_error 를 저장하는 꼴 그대로다 (services/instance_base.py 의 record_failure)
    ["secret_missing: Secret ds-1 not found in daegam-default", ERROR_TEXT.secret_missing, "secret_missing"],
    // 상태 조회가 클러스터에서 거절당한 줄. 코드는 앞머리이고 뒤는 빠진 권한이다 (P71 2.4절)
    ["cluster_forbidden: deployments:get", ERROR_TEXT.cluster_forbidden, "cluster_forbidden"],
    // 코드만 있고 설명이 없어도 표를 지난다
    ["in_use", ERROR_TEXT.in_use, "in_use"],
    // 코드 꼴이 아니면 줄 전체가 설명이다
    ["Traceback (most recent call last): boom", "Traceback (most recent call last): boom", null],
  ])("describeErrorLine(%j)", (line, text, code) => {
    const d = describeErrorLine(ko, line);
    expect(d.text).toBe(text);
    expect(d.code).toBe(code);
  });
});
