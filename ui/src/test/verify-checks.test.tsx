import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import type { CheckResult } from "@/api/types";
import { CheckResultsTable } from "@/components/VerdictPanel";
import { type Messages, dictionary } from "@/i18n";
import { CHECK_IDS, describeCheck, roundtripTools } from "@/lib/verifyChecks";

import { versionDetail } from "./msw";
import { POLITE_ENDING, sentences } from "./text";

const ko = dictionary("ko");
const en = dictionary("en");

/** 대시로 설명을 이은 자리. error-text.test.ts 와 같은 기준이다. */
const DASH = /[—–]|\s-\s/;

/** 검사와 검사 묶음의 풀이. 키는 검사 id 나 묶음 id 다. */
function descriptions(t: Messages): [string, string][] {
  const { check, group } = t.backendApis.checks;
  return [...Object.entries(check), ...Object.entries(group)].map(([id, text]) => [id, text.description]);
}

/** verdict.py 의 CHECK_LEVELS 블록에서 (id, level) 을 뽑는다. 정본과 사전이 어긋나면 여기서 잡힌다. */
function readCheckLevels(): Map<string, string> {
  // vitest 는 ui/ 에서 돈다 (make ui-test, npx vitest run)
  const src = readFileSync(resolve(process.cwd(), "../platform/daegam/pipeline/verdict.py"), "utf8");
  const block = /CHECK_LEVELS[^=]*=\s*\{([\s\S]*?)\n\}/.exec(src)?.[1] ?? "";
  const out = new Map<string, string>();
  for (const m of block.matchAll(/"([^"]+)":\s*\("(error|warn)"/g)) out.set(m[1], m[2]);
  return out;
}

describe("verify check dictionary", () => {
  it("has the same ids and severities as CHECK_LEVELS in verdict.py", () => {
    const levels = readCheckLevels();
    expect(levels.size).toBe(21);
    expect(new Set(CHECK_IDS)).toEqual(new Set(levels.keys()));
    for (const [id, level] of levels) expect(describeCheck(ko, id).severity, id).toBe(level);
  });

  it("covers every id in the fixture report and falls back to 'other' for unknown ids", () => {
    const fixtureIds = (versionDetail.report.results ?? []).map((r) => r.id);
    expect(new Set(fixtureIds)).toEqual(new Set(CHECK_IDS));
    expect(describeCheck(ko, "mystery.check")).toEqual({ group: "other", title: "mystery.check", description: "", severity: null });
  });

  it("flattens fastmcp.roundtrip data.tools with array or schema parameters", () => {
    const tools = roundtripTools({
      id: "fastmcp.roundtrip",
      status: "pass",
      data: {
        tools: [
          { name: "Add", parameters: [{ name: "a", type: "integer", required: true }, "b"] },
          { name: "Echo", inputSchema: { type: "object", properties: { text: { type: "string" } }, required: ["text"] } },
          "Ping",
        ],
      },
    });
    expect(tools).toEqual([
      { name: "Add", params: [{ name: "a", type: "integer", required: true }, { name: "b" }] },
      { name: "Echo", params: [{ name: "text", type: "string", required: true }] },
      { name: "Ping", params: [] },
    ]);
    expect(roundtripTools({ id: "fastmcp.roundtrip", status: "skip" })).toBeNull();
  });
});

describe.each([
  ["ko", ko],
  ["en", en],
])("검사 풀이 (%s)", (_language, t) => {
  it("풀이에 검사 id 가 들어가지 않는다", () => {
    expect(Object.entries(t.backendApis.checks.check).filter(([id, text]) => text.description.includes(id))).toEqual([]);
  });

  it("대시로 설명을 잇지 않는다", () => {
    expect(descriptions(t).filter(([, text]) => DASH.test(text))).toEqual([]);
  });
});

describe("한국어 검사 풀이", () => {
  it("모든 문장이 합니다체로 끝난다", () => {
    const bad = descriptions(ko).flatMap(([id, text]) => sentences(text).filter((sentence) => !POLITE_ENDING.test(sentence)).map((sentence) => `${id}: ${sentence}`));
    expect(bad).toEqual([]);
  });
});

/** 영어 풀이는 마침표로 끝나고 please 없이 쓴다 (P49 7절). */
describe("영어 검사 풀이", () => {
  it("모든 풀이가 마침표로 끝난다", () => {
    expect(descriptions(en).filter(([, text]) => !text.endsWith("."))).toEqual([]);
  });

  it("please 를 쓰지 않는다", () => {
    expect(descriptions(en).filter(([, text]) => /\bplease\b/i.test(text))).toEqual([]);
  });
});

const soapResults = versionDetail.report.results!;

function restResults(): CheckResult[] {
  return soapResults.map((r) => {
    if (r.id === "tool-description.present") return { id: r.id, status: "pass", message: null };
    if (r.id === "tool-name.safe") return { id: r.id, status: "fail", message: "tool 'get order' has a space", location: "paths./orders.get" };
    if (r.id === "fastmcp.roundtrip") {
      return {
        id: r.id,
        status: "pass",
        message: "2 tools",
        data: { tools: [{ name: "listOrders", parameters: [] }, { name: "getOrder", parameters: [{ name: "id", type: "string", required: true }] }] },
      };
    }
    if (r.id === "fastmcp.tool-materialized") return { id: r.id, status: "pass", message: null };
    return r;
  });
}

describe("CheckResultsTable", () => {
  it("renders group headers, titles, severity badges and the summary line", () => {
    render(<CheckResultsTable results={soapResults} kind="soap" />);
    expect(screen.getByTestId("report-summary")).toHaveTextContent("21개 검사 중 pass 18 · warn 1 · fail 0 · skip 2");
    // roundtrip 이 skip 이라 도구 개수 문장은 없다
    expect(screen.getByTestId("report-summary")).not.toHaveTextContent("Tool");
    for (const g of ["document", "tool-name", "tool-description", "x-soap", "deep"]) {
      expect(screen.getByTestId(`check-group-${g}`)).toBeInTheDocument();
    }
    expect(screen.queryByTestId("check-group-other")).not.toBeInTheDocument();
    const row = screen.getByTestId("check-tool-name.safe");
    expect(row).toHaveTextContent("이름 규칙");
    expect(row).toHaveTextContent("tool-name.safe");
    expect(within(row).getByTestId("check-status-tool-name.safe")).toHaveTextContent("pass");
    expect(within(row).getByTestId("check-severity-tool-name.safe")).toHaveTextContent("등록 거부");
    expect(within(screen.getByTestId("check-tool-name.normalized")).getByTestId("check-severity-tool-name.normalized")).toHaveTextContent("경고");
    // SOAP 스펙에서는 x-soap 그룹이 그대로 펼쳐져 있다
    expect(screen.getByTestId("check-x-soap.refs")).toBeInTheDocument();
    expect(screen.queryByTestId("check-group-toggle-x-soap")).not.toBeInTheDocument();
  });

  it("expands warn rows by default with location and refs, keeps pass rows collapsed until clicked", async () => {
    const user = userEvent.setup();
    render(<CheckResultsTable results={soapResults} kind="soap" />);
    const detail = screen.getByTestId("check-detail-tool-description.present");
    expect(detail).toHaveTextContent("summary 또는 description");
    expect(detail).toHaveTextContent("paths./GetOrder.post");
    expect(within(detail).getByRole("link", { name: "mcp-tools" })).toHaveAttribute(
      "href",
      "https://modelcontextprotocol.io/specification/latest/server/tools",
    );
    expect(screen.queryByTestId("check-detail-document.openapi3")).not.toBeInTheDocument();
    expect(screen.getByTestId("check-toggle-document.openapi3")).toHaveAttribute("aria-expanded", "false");

    await user.click(screen.getByTestId("check-toggle-document.openapi3"));
    expect(screen.getByTestId("check-detail-document.openapi3")).toHaveTextContent("openapi: 3.x");
    expect(screen.getByTestId("check-toggle-document.openapi3")).toHaveAttribute("aria-expanded", "true");
    // 행 자체를 눌러도 접힌다
    await user.click(screen.getByTestId("check-document.openapi3"));
    expect(screen.queryByTestId("check-detail-document.openapi3")).not.toBeInTheDocument();
    // 펼쳐져 있던 warn 행도 눌러 접을 수 있다
    await user.click(screen.getByTestId("check-toggle-tool-description.present"));
    expect(screen.queryByTestId("check-detail-tool-description.present")).not.toBeInTheDocument();
  });

  it("collapses x-soap for REST specs, opens fail rows and renders roundtrip tools as a table", async () => {
    const user = userEvent.setup();
    render(<CheckResultsTable results={restResults()} kind="rest" />);
    expect(screen.getByTestId("report-summary")).toHaveTextContent("21개 검사 중 pass 20 · warn 0 · fail 1");
    expect(screen.getByTestId("report-summary")).toHaveTextContent("Tool 2개가 생깁니다");

    const soapGroup = screen.getByTestId("check-group-x-soap");
    expect(soapGroup).toHaveTextContent("해당 없음(REST 스펙)");
    expect(screen.queryByTestId("check-x-soap.refs")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("check-group-toggle-x-soap"));
    expect(screen.getByTestId("check-x-soap.refs")).toBeInTheDocument();

    const fail = screen.getByTestId("check-detail-tool-name.safe");
    expect(fail).toHaveTextContent("paths./orders.get");
    expect(within(screen.getByTestId("check-tool-name.safe")).getByTestId("check-severity-tool-name.safe")).toHaveTextContent("등록 거부");

    // roundtrip 은 pass 라 접혀 있다. 펼치면 도구별 파라미터 표가 나온다
    await user.click(screen.getByTestId("check-toggle-fastmcp.roundtrip"));
    const tools = within(screen.getByTestId("check-detail-fastmcp.roundtrip")).getByTestId("roundtrip-tools");
    expect(within(tools).getByTestId("roundtrip-tool-listOrders")).toHaveTextContent("-");
    expect(within(tools).getByTestId("roundtrip-tool-getOrder")).toHaveTextContent("id: string*");
  });

  it("puts unknown ids under 'other' with the id as title", () => {
    render(<CheckResultsTable results={[{ id: "mystery.check", status: "pass", message: "?" }]} />);
    expect(screen.getByTestId("check-group-other")).toBeInTheDocument();
    expect(screen.getByTestId("check-mystery.check")).toHaveTextContent("mystery.check");
    expect(screen.queryByTestId("check-severity-mystery.check")).not.toBeInTheDocument();
  });
});
