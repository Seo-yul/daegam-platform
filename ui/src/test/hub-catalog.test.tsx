import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { setLanguage } from "@/lib/language";
import { leftKorean } from "./leftKorean";
import { HttpResponse, catalogServerFixture, http, hubItemFixture, hubState, lastRequest, meDeveloper, server } from "./msw";
import { openSelect, selectValue } from "./select";

/** 카탈로그 대화상자를 연다. */
async function openCatalog(user: ReturnType<typeof userEvent.setup>) {
  render(<TestApp initialPath="/hub" />);
  await user.click(await screen.findByTestId("hub-import-catalog"));
  return screen.findByTestId("hub-catalog-list");
}

describe("hub catalog import (P55 8절)", () => {
  it("lists the snapshot with marks, filters by kind and search, and says why a server cannot be picked", async () => {
    const user = userEvent.setup();
    const list = await openCatalog(user);
    expect(screen.getByTestId("hub-catalog-hint")).toHaveTextContent("ToolHive 가 공개한 MCP 서버 목록입니다.");
    expect(within(list).getAllByRole("listitem")).toHaveLength(6);
    expect(screen.getByTestId("hub-catalog-osv")).toHaveTextContent("공식");
    expect(screen.getByTestId("hub-catalog-sqlite")).toHaveTextContent("커뮤니티");
    expect(screen.getByTestId("hub-catalog-osv-provenance")).toHaveTextContent("출처 확인 가능");
    expect(screen.getByTestId("hub-catalog-sqlite-provenance")).toHaveTextContent("출처 정보 없음");
    expect(screen.getByTestId("hub-catalog-osv-platforms")).toHaveTextContent("아키텍처 linux/amd64, linux/arm64");
    // 로그인 필요와 가져올 수 없는 자격 증명은 흐리게 까닭을 단다. 비밀 헤더 하나는 header 로 가져오는 안내다
    expect(screen.getByTestId("hub-catalog-oauth-remote")).toHaveAttribute("data-blocked", "needsLogin");
    expect(screen.getByTestId("hub-catalog-oauth-remote-reason")).toHaveTextContent("사람마다 로그인해야 사용하는 서버라");
    expect(screen.queryByTestId("hub-catalog-oauth-remote-pick")).not.toBeInTheDocument();
    expect(screen.getByTestId("hub-catalog-multi-header-reason")).toHaveTextContent("주소나 헤더에 자격 증명을 넣어야 하는 서버라");
    expect(screen.getByTestId("hub-catalog-magic")).toHaveTextContent("자격 증명 필요");
    expect(screen.getByTestId("hub-catalog-magic-header")).toHaveTextContent("이 서버는 x-api-key 헤더에 API 키를 넣어야 합니다.");
    expect(screen.queryByTestId("hub-catalog-magic-reason")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "image" }));
    expect(within(screen.getByTestId("hub-catalog-list")).getAllByRole("listitem")).toHaveLength(2);
    await user.click(screen.getByRole("tab", { name: "전체" }));
    await user.type(screen.getByTestId("hub-catalog-search"), "vulnerab");
    expect(within(screen.getByTestId("hub-catalog-list")).getAllByRole("listitem")).toHaveLength(1);
    await user.clear(screen.getByTestId("hub-catalog-search"));
    await user.type(screen.getByTestId("hub-catalog-search"), "없는서버");
    expect(screen.getByTestId("hub-catalog-empty")).toHaveTextContent('"없는서버"와 맞는 서버가 없습니다.');
  });

  it("greys catalog images out with the reason while hub images are not set up", async () => {
    const user = userEvent.setup();
    hubState.catalog = { ...hubState.catalog, admission: false, servers: hubState.catalog.servers.map((s) => ({ ...s, mirrored_image: null })) };
    await openCatalog(user);
    expect(screen.getByTestId("hub-catalog-osv")).toHaveAttribute("data-blocked", "admissionOff");
    expect(screen.getByTestId("hub-catalog-osv-reason")).toHaveTextContent("이미지 서명 확인이 설정되지 않아 카탈로그 이미지는 등록할 수 없습니다.");
    expect(screen.getByTestId("hub-catalog-docs-pick")).toBeInTheDocument();
  });

  it("greys an image out while admission is off even if a hub copy is named", async () => {
    const user = userEvent.setup();
    hubState.catalog = { ...hubState.catalog, admission: false };
    await openCatalog(user);
    expect(screen.getByTestId("hub-catalog-osv")).toHaveAttribute("data-blocked", "admissionOff");
  });

  it("says the catalog is empty when there is no server", async () => {
    const user = userEvent.setup();
    hubState.catalog = { ...hubState.catalog, servers: [] };
    render(<TestApp initialPath="/hub" />);
    await user.click(await screen.findByTestId("hub-import-catalog"));
    expect(await screen.findByTestId("hub-catalog-empty")).toHaveTextContent("카탈로그가 비어 있습니다.");
  });

  it("opens the form filled from an image server and registers it with its catalog name, env and Secret env", async () => {
    const user = userEvent.setup();
    await openCatalog(user);
    await user.click(screen.getByTestId("hub-catalog-osv-pick"));
    expect(await screen.findByTestId("hub-from-catalog")).toHaveTextContent("카탈로그에서 가져온 값입니다: OSV");
    // 플랫폼 관리자는 미러 작업의 입력을 본다. 출처가 있으니 승인은 없다
    expect(screen.getByTestId("hub-mirror-admin")).toHaveTextContent("허브 미러 작업을 아래 값으로 실행해");
    expect(screen.getByTestId("hub-mirror-inputs")).toHaveTextContent("HUB_UPSTREAM=ghcr.io/stackloklabs/osv-mcp/server:0.1.4 HUB_SERVER=osv");
    expect(screen.getByTestId("hub-mirror-inputs")).not.toHaveTextContent("HUB_APPROVE");
    expect(screen.getByTestId("hub-name")).toHaveValue("osv");
    expect(screen.getByTestId("hub-kind")).toHaveValue("image");
    expect(screen.getByTestId("hub-image")).toHaveValue("registry.example.com/hub/ghcr.io/stackloklabs/osv-mcp/server:0.1.4");
    expect(screen.getByTestId("hub-port")).toHaveValue(8080);
    expect(screen.getByTestId("hub-tools-filter")).toHaveValue("query_vulnerability, query_vulnerabilities_batch");
    expect(screen.getByTestId("hub-env-name-0")).toHaveValue("OSV_LOG");
    expect(screen.getByTestId("hub-env-value-0")).toHaveValue("info");
    expect(screen.getByTestId("hub-env-description-0")).toHaveTextContent("Log level");
    expect(screen.getByTestId("hub-secret-env-name-0")).toHaveValue("OSV_TOKEN");
    expect(screen.getByTestId("hub-secret-env-value-0")).toHaveAttribute("type", "password");
    expect(screen.getByTestId("hub-secret-env-description-0")).toHaveTextContent("API token for the OSV mirror");
    await user.type(screen.getByTestId("hub-secret-env-value-0"), "s3cret");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("hub-detail-page")).toBeInTheDocument();
    expect(lastRequest("POST", "/hub")?.body).toEqual({
      name: "osv",
      display_name: "OSV",
      kind: "image",
      cluster_id: null,
      image: "registry.example.com/hub/ghcr.io/stackloklabs/osv-mcp/server:0.1.4",
      port: 8080,
      transport: "streamable-http",
      tools_filter: ["query_vulnerability", "query_vulnerabilities_batch"],
      env: [{ name: "OSV_LOG", value: "info" }],
      secret_env: [{ name: "OSV_TOKEN", value: "s3cret" }],
      catalog: "osv",
    });
  });

  it("fills stdio and args from a server without provenance and asks for approval in the mirror inputs", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/new?catalog=sqlite" />);
    expect(await screen.findByTestId("hub-mirror-inputs")).toHaveTextContent("HUB_APPROVE=yes");
    expect(screen.getByTestId("hub-stdio")).toBeChecked();
    expect(screen.getByTestId("hub-arg-0")).toHaveValue("--db-path");
    expect(screen.getByTestId("hub-arg-1")).toHaveValue("/tmp/db.sqlite");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("hub-detail-page");
    expect(lastRequest("POST", "/hub")?.body).toEqual({
      name: "sqlite",
      display_name: "SQLite",
      kind: "image",
      cluster_id: null,
      image: "registry.example.com/hub/docker.io/mcp/sqlite:latest",
      transport: "streamable-http",
      stdio: true,
      args: ["--db-path", "/tmp/db.sqlite"],
      catalog: "sqlite",
    });
  });

  it("tells members that a platform-admin mirrors images", async () => {
    server.use(http.get("/me", () => HttpResponse.json(meDeveloper)));
    render(<TestApp initialPath="/hub/new?catalog=osv" />);
    expect(await screen.findByTestId("hub-mirror-member")).toHaveTextContent("플랫폼 관리자가 사내 레지스트리로 옮긴 뒤에 등록할 수 있습니다.");
    expect(screen.queryByTestId("hub-mirror-inputs")).not.toBeInTheDocument();
  });

  it("imports a remote that needs one secret header as an entry item with header auth (P86 A3)", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/new?catalog=magic" />);
    await waitFor(() => expect(screen.getByTestId("hub-kind")).toHaveValue("entry"));
    expect(screen.queryByTestId("hub-mirror-admin")).not.toBeInTheDocument();
    expect(screen.getByTestId("hub-url")).toHaveValue("https://magic.example.com/mcp");
    expect(screen.getByTestId("hub-auth")).toHaveValue("header");
    expect(screen.getByTestId("hub-header-name")).toHaveValue("x-api-key");
    await user.type(screen.getByTestId("hub-header-value"), "k-1");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("hub-detail-page");
    expect(lastRequest("POST", "/hub")?.body).toEqual({
      name: "magic",
      display_name: "Magic",
      kind: "entry",
      cluster_id: null,
      url: "https://magic.example.com/mcp",
      transport: "streamable-http",
      auth: { kind: "header", header_name: "x-api-key", header_value: "k-1" },
      catalog: "magic",
    });
  });

  it("imports a remote as an entry item with its catalog name", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/new?catalog=docs" />);
    await waitFor(() => expect(screen.getByTestId("hub-url")).toHaveValue("https://docs.example.com/mcp"));
    expect(screen.getByTestId("hub-kind")).toHaveValue("entry");
    await user.type(screen.getByTestId("hub-name"), "-2");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("hub-detail-page");
    expect(lastRequest("POST", "/hub")?.body).toMatchObject({ kind: "entry", catalog: "docs" });
  });

  it("registers an edited address as a plain item without the catalog notice", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/new?catalog=docs" />);
    expect(await screen.findByTestId("hub-from-catalog")).toBeInTheDocument();
    await user.type(screen.getByTestId("hub-url"), "/v2");
    expect(screen.queryByTestId("hub-from-catalog")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("hub-detail-page");
    expect(lastRequest("POST", "/hub")?.body).not.toHaveProperty("catalog");
  });

  it("drops the catalog name for an image whose reference was edited, and never sends a port with stdio", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/new?catalog=osv" />);
    expect(await screen.findByTestId("hub-port")).toHaveValue(8080);
    await user.click(screen.getByTestId("hub-stdio"));
    expect(screen.getByTestId("hub-port")).toBeDisabled();
    expect(screen.getByTestId("hub-port")).toHaveValue(null);
    await user.type(screen.getByTestId("hub-image"), "-edited");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("hub-detail-page");
    const body = lastRequest("POST", "/hub")?.body as Record<string, unknown>;
    expect(body).toMatchObject({ stdio: true });
    expect(body).not.toHaveProperty("port");
    expect(body).not.toHaveProperty("catalog");
  });

  it("sends an empty Secret env row by name and leaves an Env row without a value out", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/new?catalog=osv" />);
    await user.clear(await screen.findByTestId("hub-env-value-0"));
    expect(screen.getByTestId("hub-secret-env-required-0")).toHaveTextContent("필수");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("hub-detail-page");
    const body = lastRequest("POST", "/hub")?.body as Record<string, unknown>;
    expect(body.secret_env).toEqual([{ name: "OSV_TOKEN" }]);
    expect(body).not.toHaveProperty("env");
  });

  it("greys out a remote that speaks only SSE with the reason", async () => {
    const user = userEvent.setup();
    hubState.catalog = {
      ...hubState.catalog,
      servers: [...hubState.catalog.servers, catalogServerFixture({ name: "sse-remote", title: "SSE remote", url: "https://sse.example.com/sse", transport: "sse" })],
    };
    await openCatalog(user);
    expect(screen.getByTestId("hub-catalog-sse-remote")).toHaveAttribute("data-blocked", "sseOnly");
    expect(screen.getByTestId("hub-catalog-sse-remote-reason")).toHaveTextContent("SSE 방식으로만 동작하는 서버라 허브에 등록할 수 없습니다.");
    expect(screen.queryByTestId("hub-catalog-sse-remote-pick")).not.toBeInTheDocument();
  });

  it("offers no SSE transport for outside servers on the registration form", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/new" />);
    const listbox = await openSelect(user, await screen.findByTestId("hub-transport"));
    expect(within(listbox).getAllByRole("option").map((o) => o.getAttribute("data-value"))).toEqual(["", "streamable-http"]);
  });

  it("offers SSE for image items, keeps a catalog image's SSE and clears it when the kind turns to an outside server", async () => {
    const user = userEvent.setup();
    hubState.catalog = {
      ...hubState.catalog,
      servers: [
        ...hubState.catalog.servers,
        catalogServerFixture({
          name: "sse-image",
          title: "SSE image",
          kind: "image",
          transport: "sse",
          image: { repository: "ghcr.io/example/sse", tag: "1.0.0" },
          port: 8080,
          mirrored_image: "registry.example.com/hub/ghcr.io/example/sse:1.0.0",
        }),
      ],
    };
    render(<TestApp initialPath="/hub/new?catalog=sse-image" />);
    await waitFor(() => expect(screen.getByTestId("hub-kind")).toHaveValue("image"));
    expect(screen.getByTestId("hub-transport")).toHaveValue("sse");
    const offered = async () => within(await openSelect(user, "hub-transport")).getAllByRole("option").map((o) => o.getAttribute("data-value"));
    expect(await offered()).toEqual(["", "streamable-http", "sse"]);
    await user.keyboard("{Escape}");
    await selectValue(user, "hub-kind", "entry");
    expect(screen.getByTestId("hub-transport")).toHaveValue("");
    expect(await offered()).toEqual(["", "streamable-http"]);
  });

  it("does not send a catalog name for a remote with a URL template", async () => {
    const user = userEvent.setup();
    hubState.catalog = { ...hubState.catalog, servers: [...hubState.catalog.servers, catalogServerFixture({ name: "templated", title: "Templated", url: "https://{tenant}.example.com/mcp", url_template: true })] };
    render(<TestApp initialPath="/hub/new?catalog=templated" />);
    await waitFor(() => expect(screen.getByTestId("hub-url")).toHaveValue("https://{tenant}.example.com/mcp"));
    await user.clear(screen.getByTestId("hub-url"));
    await user.type(screen.getByTestId("hub-url"), "https://acme.example.com/mcp");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("hub-detail-page");
    expect(lastRequest("POST", "/hub")?.body).not.toHaveProperty("catalog");
  });

  it("says why a catalog image cannot be registered while admission is off, without mirror lines", async () => {
    hubState.catalog = { ...hubState.catalog, admission: false, servers: hubState.catalog.servers.map((s) => ({ ...s, mirrored_image: null })) };
    render(<TestApp initialPath="/hub/new?catalog=osv" />);
    expect(await screen.findByTestId("hub-admission-off")).toHaveTextContent("이미지 서명 확인이 설정되지 않아");
    expect(screen.queryByTestId("hub-mirror-admin")).not.toBeInTheDocument();
    expect(screen.queryByTestId("hub-mirror-member")).not.toBeInTheDocument();
  });

  it("tells a member that a server whose arguments need values is for a platform-admin", async () => {
    server.use(http.get("/me", () => HttpResponse.json(meDeveloper)));
    hubState.catalog = { ...hubState.catalog, servers: hubState.catalog.servers.map((s) => (s.name === "sqlite" ? { ...s, args_variables: true } : s)) };
    render(<TestApp initialPath="/hub/new?catalog=sqlite" />);
    expect(await screen.findByTestId("hub-catalog-admin-only")).toHaveTextContent("플랫폼 관리자만 등록할 수 있습니다");
  });

  it("says when the named server is not in this catalog", async () => {
    render(<TestApp initialPath="/hub/new?catalog=gone" />);
    expect(await screen.findByTestId("hub-catalog-missing")).toHaveTextContent('"gone"와 맞는 서버가 없습니다.');
    expect(screen.getByTestId("hub-name")).toHaveValue("");
  });
});

/** Secret env 와 허용 기록을 든 image 항목 (P55 5·6절). */
function imageItem(over: Partial<ReturnType<typeof hubItemFixture>> = {}) {
  const base = hubItemFixture();
  return {
    ...base,
    id: "hub-osv",
    name: "osv",
    display_name: "OSV",
    kind: "image" as const,
    source: {
      ...base.source,
      instance_id: null,
      mcp_profile_id: null,
      image: "registry.example.com/hub/ghcr.io/stackloklabs/osv-mcp/server@sha256:" + "a".repeat(64),
      transport: "streamable-http" as const,
      stdio: true,
      args: ["--verbose"],
      env: [{ name: "OSV_LOG", value: "info" }],
      secret_env: [
        { name: "OSV_TOKEN", set: true },
        { name: "OSV_EXTRA", set: false },
      ],
      catalog: { server: "osv", release: "v0.20261026.0" },
      admission: {
        digest: "sha256:" + "a".repeat(64),
        provenance: "verified",
        upstream: "ghcr.io/stackloklabs/osv-mcp/server:0.1.4@sha256:" + "a".repeat(64),
        key: "sha256:" + "b".repeat(64),
        run: { by: "admin", via: "make", platform: "abc123" },
        signatures: 1,
        at: "2026-11-16T00:00:00Z",
        by: "header-admin",
      },
    },
    k8s: { kind: "MCPServer", name: "hub-osv", namespace: "daegam-default", group: null, tool_config: null },
    permissions: { view: true, consume: true, manage: true, replace_header: false, replace_secret_env: true },
    ...over,
  };
}

describe("hub image item detail (P55 8절)", () => {
  it("shows the container, the catalog origin and the admission record", async () => {
    hubState.items = [imageItem()];
    render(<TestApp initialPath="/hub/hub-osv" />);
    const card = await screen.findByTestId("hub-image-card");
    expect(screen.getByTestId("hub-image-stdio")).toHaveTextContent("yes");
    expect(screen.getByTestId("hub-image-args")).toHaveTextContent("--verbose");
    expect(screen.getByTestId("hub-image-env")).toHaveTextContent("OSV_LOG=info");
    expect(screen.getByTestId("hub-secret-OSV_TOKEN")).toHaveTextContent("설정됨");
    expect(screen.getByTestId("hub-secret-OSV_EXTRA")).toHaveTextContent("비어 있음");
    expect(screen.getByTestId("hub-catalog-origin")).toHaveTextContent("osv@v0.20261026.0");
    expect(card).not.toHaveTextContent("osv@");
    expect(screen.getByTestId("hub-admission-card")).toHaveTextContent("허용 기록");
    expect(screen.getByTestId("hub-admission-provenance")).toHaveTextContent("서명 확인됨");
    expect(screen.getByTestId("hub-admission-upstream")).toHaveTextContent("ghcr.io/stackloklabs/osv-mcp/server:0.1.4@sha256:");
    expect(screen.getByTestId("hub-admission-run")).toHaveTextContent("by=admin via=make platform=abc123");
  });

  it("replaces only the filled Secret values and shows who replaced them", async () => {
    const user = userEvent.setup();
    hubState.items = [imageItem()];
    render(<TestApp initialPath="/hub/hub-osv" />);
    await user.click(await screen.findByTestId("replace-secrets-button"));
    const dialog = screen.getByTestId("replace-secrets-dialog");
    expect(dialog).toHaveTextContent("비워 둔 이름은 지금 값을 그대로 사용합니다.");
    expect(screen.getByTestId("replace-secrets-save")).toBeDisabled();
    // 넣었다 지운 칸은 보내지 않는다
    await user.type(screen.getByTestId("replace-secret-OSV_TOKEN"), "x");
    await user.clear(screen.getByTestId("replace-secret-OSV_TOKEN"));
    await user.type(screen.getByTestId("replace-secret-OSV_EXTRA"), "v2");
    await user.click(screen.getByTestId("replace-secrets-save"));
    expect(await screen.findByTestId("hub-secrets-replaced")).toHaveTextContent("에 Secret 값을 바꿨습니다.");
    expect(lastRequest("PUT", "/hub/hub-osv/secret-env")?.body).toEqual({ values: { OSV_EXTRA: "v2" } });
    expect(screen.getByTestId("hub-secret-OSV_EXTRA")).toHaveTextContent("설정됨");
  });

  it("asks for every value while no Secret value is stored yet, without the keep-empty hint", async () => {
    const user = userEvent.setup();
    const base = imageItem();
    hubState.items = [{ ...base, source: { ...base.source, secret_env: [{ name: "OSV_TOKEN", set: false }, { name: "OSV_EXTRA", set: false }] } }];
    render(<TestApp initialPath="/hub/hub-osv" />);
    await user.click(await screen.findByTestId("replace-secrets-button"));
    expect(screen.getByTestId("replace-secrets-dialog")).not.toHaveTextContent("비워 둔 이름은");
    expect(screen.getByTestId("replace-secrets-dialog")).toHaveTextContent("아직 저장된 값이 없어 모든 이름의 값을 입력해야 합니다.");
    await user.type(screen.getByTestId("replace-secret-OSV_EXTRA"), "v2");
    expect(screen.getByTestId("replace-secrets-save")).toBeDisabled();
    await user.type(screen.getByTestId("replace-secret-OSV_TOKEN"), "v1");
    await user.click(screen.getByTestId("replace-secrets-save"));
    await waitFor(() => expect(lastRequest("PUT", "/hub/hub-osv/secret-env")?.body).toEqual({ values: { OSV_EXTRA: "v2", OSV_TOKEN: "v1" } }));
  });

  it("has no replace button for an image item without Secret env names", async () => {
    const base = imageItem();
    hubState.items = [{ ...base, source: { ...base.source, secret_env: undefined } }];
    render(<TestApp initialPath="/hub/hub-osv" />);
    expect(await screen.findByTestId("hub-image-card")).toBeInTheDocument();
    expect(screen.queryByTestId("replace-secrets-button")).not.toBeInTheDocument();
  });

  it("shows the catalog origin of an entry item imported with a header (P86 A3)", async () => {
    const base = hubItemFixture();
    hubState.items = [
      {
        ...base,
        id: "hub-magic",
        name: "magic",
        display_name: "Magic",
        kind: "entry",
        source: { ...base.source, url: "https://magic.example.com/mcp", transport: "streamable-http", instance_id: null, mcp_profile_id: null, catalog: { server: "magic", release: "v0.20261026.0" } },
        k8s: { kind: null, name: "hub-magic", namespace: "daegam-default", group: null, tool_config: null },
      },
    ];
    render(<TestApp initialPath="/hub/hub-magic" />);
    expect(await screen.findByTestId("hub-catalog-origin")).toHaveTextContent("magic@v0.20261026.0");
  });

  it("keeps a Secret env item in its tenant on the access tab", async () => {
    hubState.items = [imageItem()];
    render(<TestApp initialPath="/hub/hub-osv?tab=access" />);
    expect(await screen.findByTestId("access-image-notice")).toHaveTextContent("Secret env 가 있는 항목은 다른 테넌트에 열 수 없습니다.");
    expect(screen.getByTestId("access-cross-tenant-reason")).toBeInTheDocument();
  });

  it("hides the replace button without permissions.replace_secret_env and shows no admission card for an item without one", async () => {
    const item = imageItem({ permissions: { view: true, consume: true, manage: true, replace_header: false, replace_secret_env: false } });
    hubState.items = [{ ...item, source: { ...item.source, admission: undefined } }];
    render(<TestApp initialPath="/hub/hub-osv" />);
    expect(await screen.findByTestId("hub-image-card")).toBeInTheDocument();
    expect(screen.queryByTestId("replace-secrets-button")).not.toBeInTheDocument();
    expect(screen.queryByTestId("hub-admission-card")).not.toBeInTheDocument();
  });

  it("shows the image warnings and the not_answering status in words", async () => {
    const base = imageItem();
    hubState.items = [{ ...base, status: { ...base.status!, phase: "not_answering", ready: false, warnings: ["hub_image_refused: refused digest"] } }];
    render(<TestApp initialPath="/hub/hub-osv" />);
    expect(await screen.findByTestId("hub-status-warning")).toHaveTextContent("플랫폼 관리자가 이 항목의 이미지를 거부했습니다.");
    expect(screen.getByTestId("hub-status-phase")).toHaveTextContent("이 항목이 Tool 목록 요청에 응답하지 않습니다.");
    expect(screen.getByTestId("hub-status")).toHaveTextContent("응답 없음");
  });
});

describe("hub catalog screens in English", () => {
  beforeEach(() => setLanguage("en"));
  afterEach(() => setLanguage("ko"));

  it("draws the catalog dialog, the filled form and the image detail without Korean", async () => {
    const user = userEvent.setup();
    const list = await openCatalog(user);
    expect(leftKorean(screen.getByTestId("hub-catalog-dialog"))).toEqual([]);
    expect(list).toHaveTextContent("No provenance");
    await user.click(screen.getByTestId("hub-catalog-osv-pick"));
    expect(await screen.findByTestId("hub-from-catalog")).toHaveTextContent("Filled from the catalog: OSV");
    expect(leftKorean(screen.getByTestId("hub-new-page"))).toEqual([]);
  });

  it("draws the image detail without Korean", async () => {
    hubState.items = [imageItem()];
    render(<TestApp initialPath="/hub/hub-osv" />);
    await screen.findByTestId("hub-admission-card");
    expect(screen.getByTestId("hub-admission-provenance")).toHaveTextContent("Signature verified");
    expect(leftKorean(screen.getByTestId("hub-image-card"))).toEqual([]);
    expect(leftKorean(screen.getByTestId("hub-admission-card"))).toEqual([]);
  });
});
