/**
 * 영어로 그린 관리·로그인 화면의 언어 전환과 서버 글자 (P49 F9). Platform Settings 의 안내와 로그인 오류는 상태에 문장
 * 대신 일어난 일을 두므로 언어를 바꾸면 따라 바뀐다. 서버가 보내는 한국어(Degraded 배지, verdict 항목, "값 없음" 자리의 두
 * 값)는 코드나 인자가 있으면 사전의 말로 바뀌고, 없으면 서버가 쓴 그대로인지 본다 (6절).
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import type { Verdict, VerdictItem } from "@/api/types";
import { VerdictPanel } from "@/components/VerdictPanel";
import { dictionary } from "@/i18n";
import { degradedBadgeText } from "@/lib/gateway";
import { setLanguage } from "@/lib/language";
import { assistantSessionTitle, clientName } from "@/lib/serverLiterals";
import { leftKorean } from "./leftKorean";
import { signInLocal } from "./localSignIn";
import { authConfigState } from "./msw";

/** 서버가 이름을 밝히지 않은 MCP 클라이언트에 붙이는 값 (platform/daegam/services/oauth/clients.py UNNAMED_CLIENT). */
const SERVER_UNNAMED_CLIENT = "이름 없는 클라이언트";

const en = dictionary("en");
const ko = dictionary("ko");

/** 서버가 지금 내는 대소문자 충돌 항목. message 는 한국어이고 args 에 Tool 이름과 인자 이름이 있다. */
function caseCollisionItem(): VerdictItem {
  return {
    level: "warn",
    code: "tool-args.case-collision",
    message: "updateUser: 인자 Id · id 는 대소문자만 다릅니다. ToolHive 는 이 인자들을 함께 보낸 호출을 400 Invalid Request 로 거절합니다",
    count: 2,
    args: { tool: "updateUser", names: ["Id", "id"] },
  };
}

describe("admin screens in English", () => {
  beforeEach(() => setLanguage("en"));

  it("draws Platform Settings and redraws its notice in the new language", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/settings" />);
    const page = await screen.findByTestId("platform-settings-page");
    await screen.findByTestId("platform-settings-form");
    expect(screen.getByTestId("settings-scope-hint")).toHaveTextContent("These defaults apply to all tenants. To override them for one tenant, use that tenant's Tenant Settings.");
    expect(screen.getByTestId("ps-assistant")).toHaveTextContent("Steward (platform assistant)");
    expect(screen.getByTestId("ps-assistant")).toHaveTextContent("Tool calls allowed in one turn, up to");
    expect(screen.getByTestId("ps-providers")).toHaveTextContent("NameKindModelEmbedding modelAPI key *Base URLConnection");
    expect(screen.getByTestId("ps-provider-key-state-0")).toHaveTextContent("Operator-created Secret openai-key");
    expect(screen.getByTestId("ps-provider-key-0")).toHaveAttribute("placeholder", "Enter only to replace");
    expect(screen.getByTestId("ps-provider-secret-hint")).toHaveTextContent("An API key is required.");
    await user.click(screen.getByTestId("ps-provider-check-0"));
    expect(await screen.findByTestId("ps-provider-check-result-0")).toHaveTextContent("Connected.");
    expect(leftKorean(page)).toEqual([]);

    // 상태에는 문장이 아니라 일어난 일이 있으므로 언어를 바꾸면 안내도 바뀐다
    await user.click(screen.getByTestId("save-button"));
    expect(screen.getByTestId("ps-notice")).toHaveTextContent(en.errors.noChange);
    act(() => setLanguage("ko"));
    expect(screen.getByTestId("ps-notice")).toHaveTextContent(ko.errors.noChange);
  });
});

describe("sign-in screens in English", () => {
  beforeEach(() => setLanguage("en"));

  it("draws the login form and redraws its error in the new language", async () => {
    authConfigState.mode = "local";
    const user = userEvent.setup();
    render(<TestApp initialPath="/clusters" />);
    const page = await screen.findByTestId("login-page");
    expect(page).toHaveTextContent("Sign in to Daegam");
    expect(page).toHaveTextContent("Username");
    expect(page).toHaveTextContent("Password");
    expect(screen.getByTestId("login-submit")).toHaveTextContent("Sign in");
    await signInLocal(user, "not the password 1");
    expect(await screen.findByTestId("login-error")).toHaveTextContent("Incorrect username or password.");
    expect(leftKorean(page)).toEqual([]);
    // 상태에는 오류가 있고 문장은 그릴 때 만든다
    act(() => setLanguage("ko"));
    expect(screen.getByTestId("login-error")).toHaveTextContent(ko.auth.login.errors.invalid_credentials);
  });
});

describe("server sentences in English", () => {
  beforeEach(() => setLanguage("en"));

  const caseCollision = caseCollisionItem();

  it("writes a verdict item from its code and arguments, and keeps the message of a stored item without them", () => {
    const fresh: Verdict = { level: "warn", items: [caseCollision] };
    const app = render(<VerdictPanel verdict={fresh} />);
    expect(screen.getByTestId("verdict-item-tool-args.case-collision")).toHaveTextContent(
      "updateUser: the arguments Id · id differ only in case. If a call sends them together, ToolHive rejects it with 400 Invalid Request.",
    );
    expect(leftKorean(screen.getByTestId("verdict-panel"))).toEqual([]);
    app.unmount();

    // args 가 생기기 전에 저장된 판정은 서버가 쓴 message 그대로다
    const stored = { level: caseCollision.level, code: caseCollision.code, message: caseCollision.message, count: 1 } as VerdictItem;
    render(<VerdictPanel verdict={{ level: "warn", items: [stored] }} />);
    expect(screen.getByTestId("verdict-item-tool-args.case-collision")).toHaveTextContent(caseCollision.message);
  });

  it("keeps the Korean verdict sentence and the Degraded badge the server writes today", () => {
    setLanguage("ko");
    render(<VerdictPanel verdict={{ level: "warn", items: [caseCollision] }} />);
    expect(screen.getByTestId("verdict-item-tool-args.case-collision")).toHaveTextContent(caseCollision.message);
    expect(degradedBadgeText(ko, { badge: "일부 백엔드 불가", badge_code: "some_backends_down" })).toBe("일부 백엔드 불가");
    expect(degradedBadgeText(ko, { badge: "모든 백엔드 불가", badge_code: "all_backends_down" })).toBe("모든 백엔드 불가");
  });

  it("draws the Degraded badge from its code and falls back to the server's badge without one", () => {
    expect(degradedBadgeText(en, { badge: "모든 백엔드 불가", badge_code: "all_backends_down" })).toBe("All backends down");
    expect(degradedBadgeText(en, { badge: "일부 백엔드 불가", badge_code: null })).toBe("일부 백엔드 불가");
    // UI 보다 새 서버가 사전에 없는 코드를 보내면 서버의 badge 를 그린다
    const unknownCode = "backends_flapping" as "all_backends_down";
    expect(degradedBadgeText(en, { badge: "백엔드 불안정", badge_code: unknownCode })).toBe("백엔드 불안정");
    expect(degradedBadgeText(en, { badge: null, badge_code: null })).toBeNull();
  });

  it("reads the two server values as not set and draws the dictionary's words", () => {
    expect(clientName(en, SERVER_UNNAMED_CLIENT)).toBe("Unnamed client");
    expect(clientName(ko, SERVER_UNNAMED_CLIENT)).toBe(SERVER_UNNAMED_CLIENT);
    expect(clientName(en, "Cursor")).toBe("Cursor");
    expect(assistantSessionTitle(en, "새 대화")).toBe(en.shared.chatPanel.newChat);
    expect(assistantSessionTitle(ko, "새 대화")).toBe("새 대화");
    expect(assistantSessionTitle(en, "acme backend")).toBe("acme backend");
    expect(assistantSessionTitle(en, null)).toBe("");
  });
});

/** 화면이 읽는 서버 값이 정본(platform/daegam)과 같은지 본다. 서버 문구가 바뀌면 사전과 lib/serverLiterals.ts 도 함께 바꾼다. */
describe("server values the screen reads", () => {
  const read = (path: string) => readFileSync(resolve(process.cwd(), "../platform/daegam", path), "utf8");

  it("has the Korean Degraded badge of gateways.py for each badge_code", () => {
    const src = read("services/gateway_status.py");
    for (const [code, text] of Object.entries(ko.gateways.degradedBadge)) expect(src).toContain(`"${code}": "${text}",`);
  });

  it("reads the same two values the server writes where nothing is set", () => {
    expect(read("services/oauth/clients.py")).toContain(`UNNAMED_CLIENT = "${SERVER_UNNAMED_CLIENT}"`);
    expect(read("store/assistant_sessions.py")).toContain('DEFAULT_TITLE = "새 대화"');
  });

  it("writes the same Korean case-collision sentence as verdict.py", () => {
    const src = read("pipeline/verdict.py");
    expect(src).toContain(`CASE_COLLISION_CODE = "${caseCollisionItem().code}"`);
    // 서버의 f-string 은 두 줄로 나뉘어 있다
    expect(src).toContain(`f"{tool}: 인자 {' · '.join(names)} 는 대소문자만 다릅니다. ToolHive 는 이 인자들을 "`);
    expect(src).toContain('"함께 보낸 호출을 400 Invalid Request 로 거절합니다",');
    expect(ko.backendApis.verdict.caseCollision("updateUser", "Id · id")).toBe(caseCollisionItem().message);
  });
});
