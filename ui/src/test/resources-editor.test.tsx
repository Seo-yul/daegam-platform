import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import {
  EMPTY_FIELDS,
  ResourcesEditor,
  assembleResources,
  draftFromResources,
  fieldsFromResources,
  formatResources,
  limitWarnings,
  parseQuantity,
  resourcesFromDraft,
  validateQuantity,
  type ResourcesDraft,
} from "@/components/ResourcesEditor";
import { dictionary, useT } from "@/i18n";

const ko = dictionary("ko");

describe("quantity validation", () => {
  it.each(["0", "1", "2.5", ".5", "500m", "1500m", "0m"])("cpu accepts %s", (v) => {
    expect(validateQuantity(ko, "cpu", v)).toBeNull();
  });
  it.each(["500mi", "1Mi", "abc", "-1", "1 m", "0.5m", "1e3", "500M"])("cpu rejects %s", (v) => {
    expect(validateQuantity(ko, "cpu", v)).not.toBeNull();
  });
  it.each(["1024", "256Mi", "1.5Gi", "512Ki", "2Ti", "1k", "500M", "1G", "1T", "1Pi", ".5Gi"])("memory accepts %s", (v) => {
    expect(validateQuantity(ko, "memory", v)).toBeNull();
  });
  it.each(["256mi", "1GB", "1K", "1KiB", "abc", "-256Mi", "1 Gi", "1e9", "1m"])("memory rejects %s", (v) => {
    expect(validateQuantity(ko, "memory", v)).not.toBeNull();
  });
  it("points out the K8s spelling for K/KB-style suffixes", () => {
    expect(validateQuantity(ko, "memory", "1K")).toContain("대문자 K");
    expect(validateQuantity(ko, "memory", "1GB")).toContain("B 접미사");
  });
  it("treats blanks as omitted", () => {
    expect(validateQuantity(ko, "cpu", "")).toBeNull();
    expect(validateQuantity(ko, "memory", "   ")).toBeNull();
  });
  it("parses to millicores and bytes for comparison", () => {
    expect(parseQuantity("cpu", "500m")).toBe(500);
    expect(parseQuantity("cpu", "1")).toBe(1000);
    expect(parseQuantity("cpu", "0.5")).toBe(500);
    expect(parseQuantity("memory", "1Ki")).toBe(1024);
    expect(parseQuantity("memory", "1k")).toBe(1000);
    expect(parseQuantity("memory", "256Mi")).toBe(256 * 1024 * 1024);
    expect(parseQuantity("memory", "1G")).toBe(1e9);
    expect(parseQuantity("memory", "oops")).toBeNull();
    expect(parseQuantity("cpu", "")).toBeNull();
  });
});

describe("resources assembly", () => {
  it("drops empty keys and returns null when everything is blank", () => {
    expect(assembleResources(EMPTY_FIELDS)).toBeNull();
    expect(assembleResources({ ...EMPTY_FIELDS, limCpu: " 500m " })).toEqual({ limits: { cpu: "500m" } });
    expect(assembleResources({ reqCpu: "250m", reqMem: "128Mi", limCpu: "1", limMem: "512Mi" })).toEqual({
      requests: { cpu: "250m", memory: "128Mi" },
      limits: { cpu: "1", memory: "512Mi" },
    });
  });

  it("round-trips a dict through the four fields", () => {
    const dict = { requests: { cpu: "250m" }, limits: { cpu: "1", memory: "512Mi" } };
    const { fields, extras } = fieldsFromResources(dict);
    expect(extras).toEqual([]);
    expect(fields).toEqual({ reqCpu: "250m", reqMem: "", limCpu: "1", limMem: "512Mi" });
    expect(assembleResources(fields)).toEqual(dict);
    // JSON 숫자는 문자열로 받는다
    expect(fieldsFromResources({ limits: { cpu: 1 } }).fields.limCpu).toBe("1");
  });

  it("reports keys the four fields cannot hold", () => {
    expect(fieldsFromResources({ limits: { cpu: "1", "ephemeral-storage": "1Gi" } }).extras).toEqual(["limits.ephemeral-storage"]);
    expect(fieldsFromResources({ requests: { "nvidia.com/gpu": 1 }, claims: [] }).extras).toEqual(["requests.nvidia.com/gpu", "claims"]);
    expect(fieldsFromResources({ limits: { cpu: { raw: "1" } } }).extras).toEqual(["limits.cpu"]);
  });

  it("draft starts in form mode unless extras force JSON", () => {
    expect(draftFromResources(null)).toEqual({ mode: "form", fields: EMPTY_FIELDS, json: "" });
    expect(draftFromResources({ limits: { cpu: "500m" } })).toEqual({ mode: "form", fields: { ...EMPTY_FIELDS, limCpu: "500m" }, json: "" });
    const d = draftFromResources({ limits: { cpu: "500m", "nvidia.com/gpu": "1" } });
    expect(d.mode).toBe("json");
    expect(JSON.parse(d.json)).toEqual({ limits: { cpu: "500m", "nvidia.com/gpu": "1" } });
  });

  it("resourcesFromDraft validates the form and parses JSON", () => {
    expect(resourcesFromDraft(ko, { mode: "form", fields: EMPTY_FIELDS, json: "" })).toEqual({ value: null, error: null });
    expect(resourcesFromDraft(ko, { mode: "form", fields: { ...EMPTY_FIELDS, reqMem: "500mi" }, json: "" }).error).toMatch(/^requests\.memory: /);
    expect(resourcesFromDraft(ko, { mode: "json", fields: EMPTY_FIELDS, json: "" })).toEqual({ value: null, error: null });
    expect(resourcesFromDraft(ko, { mode: "json", fields: EMPTY_FIELDS, json: "{oops" }).error).toMatch(/^invalid JSON/);
    expect(resourcesFromDraft(ko, { mode: "json", fields: EMPTY_FIELDS, json: '{"limits":{"cpu":"500mi"}}' })).toEqual({
      value: { limits: { cpu: "500mi" } },
      error: null,
    });
  });

  it("warns when limits fall below requests, per resource", () => {
    expect(limitWarnings(ko, { reqCpu: "1", reqMem: "128Mi", limCpu: "500m", limMem: "256Mi" })).toEqual([
      "cpu limits(500m) 가 requests(1) 보다 작습니다. 클러스터가 파드 생성을 거부할 수 있습니다.",
    ]);
    expect(limitWarnings(ko, { reqCpu: "500m", reqMem: "1Gi", limCpu: "0.5", limMem: "512M" })).toHaveLength(1);
    expect(limitWarnings(ko, { reqCpu: "1", reqMem: "", limCpu: "", limMem: "" })).toEqual([]);
    expect(limitWarnings(ko, { reqCpu: "oops", reqMem: "", limCpu: "1", limMem: "" })).toEqual([]);
  });

  it("formats a readable summary", () => {
    expect(formatResources(ko, null)).toBe("(클러스터 기본값)");
    expect(formatResources(ko, {})).toBe("(클러스터 기본값)");
    expect(formatResources(ko, { limits: { cpu: "500m" } })).toBe("limits cpu 500m");
    expect(formatResources(ko, { requests: { cpu: "500m", memory: "256Mi" }, limits: { cpu: "1", memory: "512Mi" } })).toBe(
      "cpu 500m / memory 256Mi (limits cpu 1 / memory 512Mi)",
    );
    expect(formatResources(ko, { requests: { "nvidia.com/gpu": 1 }, claims: [{ name: "x" }] })).toBe('nvidia.com/gpu 1 claims [{"name":"x"}]');
  });
});

function Harness({ initial }: { initial: unknown }) {
  const t = useT();
  const [draft, setDraft] = useState<ResourcesDraft>(() => draftFromResources(initial));
  return (
    <div>
      <ResourcesEditor testId="t" value={draft} onChange={setDraft} />
      <output data-testid="out">{JSON.stringify(resourcesFromDraft(t, draft))}</output>
    </div>
  );
}

describe("ResourcesEditor", () => {
  it("shows per-field errors and the limit warning as the user types", async () => {
    const user = userEvent.setup();
    render(<Harness initial={null} />);
    await user.type(screen.getByTestId("t-req-cpu"), "500mi");
    const err = screen.getByTestId("t-req-cpu-error");
    expect(err).toHaveTextContent("requests.cpu: CPU 는 코어 수");
    expect(screen.getByTestId("t-req-cpu")).toHaveAttribute("aria-invalid", "true");
    expect(JSON.parse(screen.getByTestId("out").textContent!).error).toMatch(/^requests\.cpu/);

    await user.clear(screen.getByTestId("t-req-cpu"));
    await user.type(screen.getByTestId("t-req-cpu"), "1");
    expect(screen.queryByTestId("t-req-cpu-error")).not.toBeInTheDocument();
    await user.type(screen.getByTestId("t-lim-cpu"), "500m");
    expect(screen.getByTestId("t-resources-warning")).toHaveTextContent("cpu limits(500m) 가 requests(1) 보다 작습니다");
    // 경고는 저장을 막지 않는다
    expect(JSON.parse(screen.getByTestId("out").textContent!)).toEqual({ value: { requests: { cpu: "1" }, limits: { cpu: "500m" } }, error: null });
  });

  it("converts between form and JSON in both directions", async () => {
    const user = userEvent.setup();
    render(<Harness initial={{ limits: { cpu: "500m" } }} />);
    expect(screen.getByTestId("t-lim-cpu")).toHaveValue("500m");
    expect(screen.queryByTestId("t-resources")).not.toBeInTheDocument();
    await user.type(screen.getByTestId("t-req-mem"), "128Mi");

    await user.click(screen.getByTestId("t-resources-json"));
    const ta = screen.getByTestId("t-resources");
    expect(JSON.parse((ta as HTMLTextAreaElement).value)).toEqual({ requests: { memory: "128Mi" }, limits: { cpu: "500m" } });
    expect(screen.queryByTestId("t-lim-cpu")).not.toBeInTheDocument();

    // JSON 에서 고친 값이 폼으로 돌아온다
    await user.clear(ta);
    await user.type(ta, '{{"limits": {{"memory": "1Gi"}}');
    await user.click(screen.getByTestId("t-resources-json"));
    expect(screen.getByTestId("t-lim-mem")).toHaveValue("1Gi");
    expect(screen.getByTestId("t-lim-cpu")).toHaveValue("");
    expect(screen.getByTestId("t-req-mem")).toHaveValue("");
  });

  it("JSON mode shows parse errors immediately and refuses to leave while broken", async () => {
    const user = userEvent.setup();
    render(<Harness initial={null} />);
    await user.click(screen.getByTestId("t-resources-json"));
    await user.type(screen.getByTestId("t-resources"), "{{oops");
    expect(screen.getByTestId("t-resources-error")).toHaveTextContent("invalid JSON");
    expect(screen.getByTestId("t-resources-json")).toBeDisabled();
    expect(screen.getByTestId("t-resources-json-notice")).toHaveTextContent("JSON 오류를 고친 뒤");
    await user.clear(screen.getByTestId("t-resources"));
    expect(screen.getByTestId("t-resources-json")).toBeEnabled();
  });

  it("starts in JSON mode with a notice when stored keys do not fit the form", async () => {
    const user = userEvent.setup();
    render(<Harness initial={{ limits: { cpu: "1", "nvidia.com/gpu": "1" } }} />);
    const toggle = screen.getByTestId("t-resources-json");
    expect(toggle).toBeChecked();
    expect(toggle).toBeDisabled();
    expect(screen.getByTestId("t-resources-json-notice")).toHaveTextContent("limits.nvidia.com/gpu");
    expect(JSON.parse(screen.getByTestId("out").textContent!).value).toEqual({ limits: { cpu: "1", "nvidia.com/gpu": "1" } });

    // 확장 키를 지우면 폼으로 돌아갈 수 있다
    const ta = screen.getByTestId("t-resources");
    await user.clear(ta);
    await user.type(ta, '{{"limits": {{"cpu": "1"}}');
    expect(toggle).toBeEnabled();
    await user.click(toggle);
    expect(screen.getByTestId("t-lim-cpu")).toHaveValue("1");
  });
});
