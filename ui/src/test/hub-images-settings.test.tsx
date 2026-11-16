import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp, makeQueryClient } from "@/App";
import { HttpResponse, apiError, http, hubCatalogFixture, hubState, lastRequest, platformSettings, received, record, server } from "./msw";

const PEM = "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE\n-----END PUBLIC KEY-----";
const DIGEST = "sha256:" + "c".repeat(64);

describe("Platform Settings hub images card (P55 6·8절)", () => {
  it("saves the prefix, keys, revoked keys and refused digests in one PATCH, then the read credential", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/settings" />);
    const card = await screen.findByTestId("ps-hub-images");
    expect(card).toHaveTextContent("허브의 image 항목은 이 레지스트리에 있고 아래 공개 키로 서명된 이미지만 사용할 수 있습니다.");
    expect(screen.getByTestId("ps-hub-credential-state")).toHaveTextContent("비어 있음");
    await user.type(screen.getByTestId("ps-hub-prefix"), "registry.example.com/hub");
    await user.click(screen.getByTestId("ps-hub-key-add"));
    // userEvent.type 은 줄바꿈을 Enter 로 친다. textarea 에는 그대로 들어간다
    await user.type(screen.getByTestId("ps-hub-key-0"), PEM);
    await user.click(screen.getByTestId("ps-hub-refused-add"));
    await user.type(screen.getByTestId("ps-hub-refused-0"), DIGEST);
    await user.click(screen.getByTestId("ps-hub-revoked-add"));
    await user.click(screen.getByTestId("ps-hub-revoked-add"));
    await user.type(screen.getByTestId("ps-hub-revoked-1"), "sha256:" + "d".repeat(64));
    await user.type(screen.getByTestId("ps-hub-username"), "reader");
    await user.type(screen.getByTestId("ps-hub-password"), "pw-1");
    await user.click(screen.getByTestId("ps-hub-images-save"));
    await waitFor(() => expect(lastRequest("PUT", "/settings/hub-images/credential")).toBeDefined());
    // 빈 줄은 빠진다
    expect(lastRequest("PATCH", "/settings")?.body).toEqual({
      hub_images: { prefix: "registry.example.com/hub", public_keys: [PEM], revoked_keys: ["sha256:" + "d".repeat(64)], refused_digests: [DIGEST] },
    });
    expect(lastRequest("PUT", "/settings/hub-images/credential")?.body).toEqual({ username: "reader", password: "pw-1" });
  });

  it("will not save a user name without a password", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/settings" />);
    await user.type(await screen.findByTestId("ps-hub-username"), "reader");
    expect(screen.getByTestId("ps-hub-images-save")).toBeDisabled();
    expect(screen.getByTestId("ps-hub-images")).toHaveTextContent("사용자 이름과 비밀번호를 함께 입력해 주세요.");
    await user.type(screen.getByTestId("ps-hub-password"), "pw");
    expect(screen.getByTestId("ps-hub-images-save")).toBeEnabled();
  });

  it("sends the credential alone, after no PATCH, and clears the typed values once stored", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/settings" />);
    await user.type(await screen.findByTestId("ps-hub-username"), "reader");
    await user.type(screen.getByTestId("ps-hub-password"), "pw-1");
    await user.click(screen.getByTestId("ps-hub-images-save"));
    await waitFor(() => expect(lastRequest("PUT", "/settings/hub-images/credential")).toBeDefined());
    expect(lastRequest("PATCH", "/settings")).toBeUndefined();
    await waitFor(() => expect(screen.getByTestId("ps-hub-password")).toHaveValue(""));
    expect(screen.getByTestId("ps-hub-username")).toHaveValue("");
  });

  it("writes the credential after the PATCH, and keeps the error and the typed credential when it fails", async () => {
    const user = userEvent.setup();
    server.use(
      http.put("/settings/hub-images/credential", async ({ request }) => {
        await record(request);
        return apiError(503, "hub_images_secret_unavailable", "the secret store is not available");
      }),
    );
    render(<TestApp initialPath="/admin/settings" />);
    await user.type(await screen.findByTestId("ps-hub-prefix"), "registry.example.com/hub");
    await user.click(screen.getByTestId("ps-hub-key-add"));
    await user.type(screen.getByTestId("ps-hub-key-0"), "key");
    await user.type(screen.getByTestId("ps-hub-username"), "reader");
    await user.type(screen.getByTestId("ps-hub-password"), "pw-1");
    await user.click(screen.getByTestId("ps-hub-images-save"));
    expect(await screen.findByTestId("ps-hub-images-error")).toHaveTextContent("사내 레지스트리의 읽기 자격 증명을 지금 읽을 수 없어");
    const order = received.map((r) => `${r.method} ${r.path.replace(/^\/api/, "")}`);
    expect(order.indexOf("PATCH /settings")).toBeGreaterThan(-1);
    expect(order.indexOf("PATCH /settings")).toBeLessThan(order.lastIndexOf("PUT /settings/hub-images/credential"));
    expect(screen.getByTestId("ps-hub-password")).toHaveValue("pw-1");
    expect(screen.getByTestId("ps-hub-username")).toHaveValue("reader");
  });

  it("refreshes the catalog after a save, so its images can be picked", async () => {
    const user = userEvent.setup();
    const client = makeQueryClient();
    hubState.catalog = { ...hubState.catalog, admission: false, servers: hubState.catalog.servers.map((s) => ({ ...s, mirrored_image: null })) };
    // 1) 대화상자가 허용이 꺼진 스냅숏을 캐시에 담는다
    const hub = render(<TestApp initialPath="/hub" client={client} />);
    await user.click(await screen.findByTestId("hub-import-catalog"));
    expect(await screen.findByTestId("hub-catalog-osv-reason")).toBeInTheDocument();
    hub.unmount();
    // 2) 허브 이미지 설정을 저장하면 그 캐시를 무효화한다
    hubState.catalog = hubCatalogFixture();
    const admin = render(<TestApp initialPath="/admin/settings" client={client} />);
    await user.type(await screen.findByTestId("ps-hub-prefix"), "registry.example.com/hub");
    await user.click(screen.getByTestId("ps-hub-key-add"));
    await user.type(screen.getByTestId("ps-hub-key-0"), "key");
    await user.click(screen.getByTestId("ps-hub-images-save"));
    expect(await screen.findByText("저장했습니다.")).toBeInTheDocument();
    admin.unmount();
    // 3) 다시 연 대화상자는 새 스냅숏을 읽어 이미지를 고를 수 있다
    render(<TestApp initialPath="/hub" client={client} />);
    await user.click(await screen.findByTestId("hub-import-catalog"));
    expect(await screen.findByTestId("hub-catalog-osv-pick")).toBeInTheDocument();
  });

  it("shows the key fingerprints, where each refused digest runs and the product image overlap", async () => {
    server.use(
      http.get("/settings", () => HttpResponse.json({ ...platformSettings, hub_images: { prefix: "registry.example.com/hub", public_keys: [PEM], revoked_keys: [], refused_digests: [DIGEST], credential_set: true } })),
      http.get("/settings/hub-images/status", () =>
        HttpResponse.json({
          configured: true,
          keys: [{ fingerprint: "sha256:" + "e".repeat(64), revoked: true }],
          overlaps: [{ cluster_id: "cl-home", cluster_name: "home", registry_prefix: "registry.example.com" }],
          refused: [{ digest: DIGEST, items: [{ id: "hub-osv", tenant: "default", name: "osv", gateways: 2 }] }],
        }),
      ),
    );
    render(<TestApp initialPath="/admin/settings" />);
    expect(await screen.findByTestId("ps-hub-images-overlap")).toHaveTextContent("허브 이미지 경로가 클러스터 home 의 제품 이미지 경로와 겹칩니다.");
    expect(screen.getByTestId("ps-hub-fingerprints")).toHaveTextContent("회수한 키");
    expect(screen.getByTestId(`ps-hub-refused-usage-${DIGEST}`)).toHaveTextContent("1개 항목에서 사용 중 default/osv");
    expect(screen.getByTestId("ps-hub-credential-state")).toHaveTextContent("설정됨");
  });
});
