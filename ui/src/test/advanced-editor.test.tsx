import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { ManifestDocument } from "@/api/types";
import { AdvancedEditor } from "@/components/AdvancedEditor";
import { TestApp } from "@/App";
import { parsePath, linesForPaths } from "@/lib/yamlPaths";
import { lastRequest, manifestDoc } from "./msw";

const YAML = `apiVersion: toolhive.stacklok.dev/v1alpha1
kind: MCPServer
metadata:
  name: mcp-calc
  labels:
    daegam.devops.ai.kr/tenant: default
    app: calc
spec:
  image: daegam-mcp-runtime:0.1.0
  replicas: 2
  env:
    - name: SPEC2OPENAPI_ENDPOINT
      value: http://x
    - name: OTHER
      value: y
  podTemplateSpec:
    spec:
      containers:
        - name: mcp
          args: [a]
          resources:
            limits:
              cpu: 500m
        - name: sidecar
          image: s
`;

describe("yamlPaths", () => {
  it("parses platform path strings", () => {
    expect(parsePath("spec.podTemplateSpec.spec.containers[name=mcp].resources.limits.cpu")).toEqual([
      { kind: "key", pattern: "spec" },
      { kind: "key", pattern: "podTemplateSpec" },
      { kind: "key", pattern: "spec" },
      { kind: "key", pattern: "containers" },
      { kind: "selector", key: "name", pattern: "mcp" },
      { kind: "key", pattern: "resources" },
      { kind: "key", pattern: "limits" },
      { kind: "key", pattern: "cpu" },
    ]);
    expect(parsePath("metadata.labels[daegam.devops.ai.kr/*]")).toEqual([
      { kind: "key", pattern: "metadata" },
      { kind: "key", pattern: "labels" },
      { kind: "key", pattern: "daegam.devops.ai.kr/*" },
    ]);
    expect(parsePath("spec.env[name=SPEC2OPENAPI_*]")[2]).toEqual({ kind: "selector", key: "name", pattern: "SPEC2OPENAPI_*" });
    expect(parsePath("args[0]")[1]).toEqual({ kind: "index", index: 0 });
  });

  it("maps paths (globs, selectors) to yaml lines, best effort", () => {
    const got = Object.fromEntries(
      linesForPaths(YAML, [
        "spec.image",
        "metadata.labels[daegam.devops.ai.kr/*]",
        "spec.env[name=SPEC2OPENAPI_*]",
        "spec.podTemplateSpec.spec.containers[name=mcp].args",
        "spec.podTemplateSpec.spec.containers[name=mcp].image",
        "spec.nope",
      ]).map((p) => [p.path, p.lines]),
    );
    expect(got["spec.image"]).toEqual([9]);
    expect(got["metadata.labels[daegam.devops.ai.kr/*]"]).toEqual([6]);
    expect(got["spec.env[name=SPEC2OPENAPI_*]"]).toEqual([12]);
    expect(got["spec.podTemplateSpec.spec.containers[name=mcp].args"]).toEqual([20]);
    expect(got["spec.podTemplateSpec.spec.containers[name=mcp].image"]).toEqual([]);
    expect(got["spec.nope"]).toEqual([]);
    expect(linesForPaths("{{{ not yaml", ["spec.image"])[0].lines).toEqual([]);
  });
});

function docFixture(over: Partial<ManifestDocument> = {}): ManifestDocument {
  return { ...manifestDoc, ...over };
}

/** 스텁 편집기(textarea)의 현재 값. jest-dom 의 toHaveValue 는 비대칭 matcher 를 받지 않는다. */
function editorValue(): string {
  return (screen.getByTestId("monaco") as HTMLTextAreaElement).value;
}

describe("AdvancedEditor (component contract)", () => {
  it("shows document tabs for multiple documents and switches between them", async () => {
    const user = userEvent.setup();
    const docs = [docFixture(), docFixture({ kind: "Deployment", name: "agent-calc", rendered: { kind: "Deployment" }, final: { kind: "Deployment" }, live: null, drift: null })];
    render(<AdvancedEditor documents={docs} onSave={vi.fn()} onReset={vi.fn()} onAbsorb={vi.fn()} />);
    expect(screen.getByTestId("doc-tab-MCPServer")).toHaveAttribute("aria-selected", "true");
    await screen.findByTestId("monaco");
    expect(editorValue()).toContain("kind: MCPServer");
    await user.click(screen.getByTestId("doc-tab-Deployment"));
    expect(screen.getByTestId("advanced-doc-Deployment")).toBeInTheDocument();
    expect(await screen.findByTestId("monaco")).toHaveValue("kind: Deployment\n");
  });

  it("lists locked and ignored paths with line numbers next to the editor", async () => {
    render(<AdvancedEditor documents={[docFixture()]} onSave={vi.fn()} onReset={vi.fn()} onAbsorb={vi.fn()} />);
    await screen.findByTestId("monaco");
    const locked = screen.getByTestId("locked-list");
    expect(within(locked).getAllByTestId("locked-list-item")).toHaveLength(2);
    expect(locked).toHaveTextContent("spec.image");
    // final 픽스처에 spec.image 가 있으므로 줄 번호가 붙는다
    expect(within(locked).getAllByTestId("locked-list-line")[0]).toHaveTextContent(/L\d+/);
    // 이 문서에는 없는 경로라 접힌 쪽에 있다. 펴 두면 지금 문서에 있는 것이 묻힌다
    expect(within(locked).queryByTestId("locked-list-absent-toggle")).not.toBeInTheDocument();
    expect(screen.getByTestId("ignored-list-absent")).toHaveTextContent("containers[name=mcp].args");
  });

  it("오버라이드 차이 탭이 편집 중인 내용과 플랫폼이 그린 것을 견준다", async () => {
    const user = userEvent.setup();
    render(<AdvancedEditor documents={[docFixture()]} onSave={vi.fn()} onReset={vi.fn()} onAbsorb={vi.fn()} />);
    const editor = await screen.findByTestId("monaco");
    await user.clear(editor);
    await user.type(editor, "apiVersion: v1\nkind: MCPServer\nspec:\n  replicas: 5\n  extra: yes\n");
    expect(screen.getByTestId("dirty-badge")).toBeInTheDocument();
    // 서버는 부르지 않는다. 탭 하나가 그 일을 다 한다
    await user.click(screen.getByTestId("view-overrides"));
    const diff = await screen.findByTestId("overrides-diff");
    const rows = within(diff).getAllByTestId("overrides-diff-row");
    expect(rows.map((r) => r.textContent)).toEqual(expect.arrayContaining([expect.stringContaining("spec.replicas"), expect.stringContaining("spec.extra")]));
    await user.click(screen.getByTestId("discard-button"));
    expect(screen.queryByTestId("dirty-badge")).not.toBeInTheDocument();
  });

  it("잠긴 줄을 고치면 저장하기 전에 알린다", async () => {
    const user = userEvent.setup();
    render(<AdvancedEditor documents={[docFixture()]} onSave={vi.fn()} onReset={vi.fn()} onAbsorb={vi.fn()} />);
    const editor = await screen.findByTestId("monaco");
    expect(screen.queryByTestId("touched-locked")).not.toBeInTheDocument();

    // spec.image 는 잠겨 있다. 저장을 눌러야 422 를 보는 것은 너무 늦다
    const before = editorValue();
    expect(before).toContain("image: ");
    await user.clear(editor);
    await user.type(editor, before.replace(/image: .*/, "image: 내가-바꾼-이미지"));
    const warn = await screen.findByTestId("touched-locked");
    expect(warn).toHaveTextContent("서버가 거부합니다");
    expect(warn).toHaveTextContent("spec.image");
  });

  it("이 문서에 없는 잠긴 경로는 접어 둔다", async () => {
    // 목록은 CR 종류마다 정해진 카탈로그라 절반 가까이가 이 문서에 없다. 다 펴면 있는 것이 묻힌다
    render(<AdvancedEditor documents={[docFixture({ locked: ["spec.image", "spec.nope", "spec.nope2"] })]} onSave={vi.fn()} onReset={vi.fn()} onAbsorb={vi.fn()} />);
    await screen.findByTestId("monaco");
    const locked = screen.getByTestId("locked-list");
    expect(within(locked).getAllByTestId("locked-list-item")).toHaveLength(1);
    expect(screen.getByTestId("locked-list-absent-toggle")).toHaveTextContent("2개");
  });

  it("rejects unparsable YAML before calling onSave", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(<AdvancedEditor documents={[docFixture()]} onSave={onSave} onReset={vi.fn()} onAbsorb={vi.fn()} />);
    const editor = await screen.findByTestId("monaco");
    await user.type(editor, "\n  bad: [[unclosed");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("parse-error")).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
  });
});

describe("MCP Server Advanced tab with AdvancedEditor (MSW)", () => {
  async function openManifest(user: ReturnType<typeof userEvent.setup>) {
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    await screen.findByTestId("mcp-server-detail-page");
    await user.click(screen.getByTestId("tab-advanced"));
    return screen.findByTestId("monaco");
  }

  it("saves the edited YAML with PUT overrides {yaml} and shows diff + validation badge", async () => {
    const user = userEvent.setup();
    const editor = await openManifest(user);
    expect(editorValue()).toContain("replicas: 2");
    await user.clear(editor);
    await user.type(editor, "apiVersion: toolhive.stacklok.dev/v1alpha1\nkind: MCPServer\nspec:\n  replicas: 4\n");
    await user.click(screen.getByTestId("save-button"));

    const saved = await screen.findByTestId("saved-panel");
    expect(within(saved).getByTestId("validation-badge")).toHaveTextContent("schema_only");
    expect(saved).toHaveTextContent("operator validation shows up in status.conditions");
    expect(within(saved).getByTestId("saved-diff")).toHaveTextContent("spec.replicas");
    const req = lastRequest("PUT", "/mcp-servers/ms-calc/overrides");
    expect(req?.body).toEqual({ yaml: expect.stringContaining("replicas: 4") });
    // 응답의 final 로 편집기가 갱신되고 unsaved 가 사라진다
    await waitFor(() => expect(editorValue()).toContain("replicas: 4"));
    expect(screen.queryByTestId("dirty-badge")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("view-overrides"));
    expect(screen.getByTestId("overrides-diff-server-paths")).toHaveTextContent("spec.replicas");
  });

  it("shows 422 locked_field / ignored_by_operator / invalid_manifest with the path", async () => {
    const user = userEvent.setup();
    const editor = await openManifest(user);
    await user.clear(editor);
    await user.type(editor, "kind: MCPServer\nspec:\n  image: hacked\n");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("error-locked_field")).toHaveTextContent("managed by the platform");
    expect(screen.getByTestId("error-path")).toHaveTextContent("spec.image");

    await user.clear(editor);
    await user.type(editor, "kind: MCPServer\nspec:\n  podTemplateSpec:\n    spec:\n      containers:\n        - name: mcp\n          args: [[x]\n");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("error-ignored_by_operator")).toHaveTextContent("containers[name=mcp].args");

    await user.clear(editor);
    await user.type(editor, "kind: MCPServer\nspec:\n  replicas: 99\n");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("error-invalid_manifest")).toHaveTextContent("must be <= 10");
    expect(screen.queryByTestId("error-path")).not.toBeInTheDocument();
  });

  it("drift view lists final → live differences, managers, and reset/absorb call the API", async () => {
    const user = userEvent.setup();
    await openManifest(user);
    await user.click(screen.getByTestId("view-drift"));
    const drift = await screen.findByTestId("drift-view");
    expect(within(drift).getByTestId("drift-managers")).toHaveTextContent("kubectl-edit");
    expect(within(drift).getAllByTestId("drift-diff-row")[0]).toHaveTextContent("spec.replicas");
    expect(within(drift).getByTestId("drift-diff-server-paths")).toHaveTextContent("spec.replicas");

    await user.click(within(drift).getByTestId("drift-reset"));
    await waitFor(() => expect(lastRequest("POST", "/mcp-servers/ms-calc/drift/reset")).toBeDefined());
    // reset 뒤 다시 받은 문서에는 드리프트가 없다
    await waitFor(() => expect(screen.getByTestId("drift-view")).toHaveTextContent("어긋난 곳이 없습니다."));
    expect(screen.queryByTestId("drift-reset")).not.toBeInTheDocument();
  });

  it("absorb takes ownership of the live change", async () => {
    const user = userEvent.setup();
    await openManifest(user);
    await user.click(screen.getByTestId("view-drift"));
    await user.click(await screen.findByTestId("drift-absorb"));
    await waitFor(() => expect(lastRequest("POST", "/mcp-servers/ms-calc/drift/absorb")).toBeDefined());
    await waitFor(() => expect(screen.getByTestId("current-overrides")).toHaveTextContent("replicas: 3"));
    await user.click(screen.getByTestId("view-edit"));
    await waitFor(() => expect(editorValue()).toContain("replicas: 3"));
  });
});
