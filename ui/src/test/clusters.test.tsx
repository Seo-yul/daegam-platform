import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { dictionary } from "@/i18n";
import { annotationsText, groupMissing, ingressOfForm, parseAnnotations } from "@/pages/clusters/Detail";
import { clusterEdge, clusterHome, http, HttpResponse, lastRequest, meDeveloper, server } from "./msw";
import { selectValue } from "./select";

describe("clusters", () => {
  it("lists clusters with preflight, kubernetes and toolhive versions", async () => {
    render(<TestApp initialPath="/clusters" />);
    const home = await screen.findByTestId("cluster-row-home");
    expect(home).toHaveTextContent("home");
    expect(within(home).getByTestId("cluster-preflight-home")).toHaveTextContent("ok");
    expect(home).toHaveTextContent("v1.33.1");
    expect(home).toHaveTextContent("0.51.2");
    const edge = screen.getByTestId("cluster-row-edge");
    expect(within(edge).getByTestId("cluster-preflight-edge")).toHaveTextContent("failed");
    // 클러스터는 플랫폼 수준이지만 헤더는 그대로 나간다 (서버가 무시)
    expect(lastRequest("GET", "/clusters")?.headers["x-daegam-tenant"]).toBe("default");
  });

  it("registers a cluster from an uploaded kubeconfig", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/clusters/new" />);
    // 권한을 확인한 뒤에 폼을 그린다 (kubeconfig 를 다 채우고 403 을 받지 않게)
    await user.type(await screen.findByTestId("cl-name"), "edge2");
    await user.type(screen.getByTestId("cl-platform-url"), "http://daegam-platform.daegam-system.svc:9000");
    expect(screen.getByTestId("save-button")).toBeDisabled();
    const kc = new File(["apiVersion: v1\nkind: Config\n"], "kubeconfig.yaml", { type: "text/yaml" });
    await user.upload(screen.getByTestId("cl-kubeconfig-file"), kc);
    expect(await screen.findByText("kubeconfig.yaml 불러옴")).toBeInTheDocument();
    // 파일 읽기는 비동기라 값이 붙을 때까지 기다린다(동기로 읽으면 병렬 부하에서 가끔 빈 값을 본다)
    await waitFor(() => expect(screen.getByTestId("cl-kubeconfig")).toHaveValue("apiVersion: v1\nkind: Config\n"));
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("cluster-detail-page")).toBeInTheDocument();
    const body = lastRequest("POST", "/clusters")?.body as Record<string, unknown>;
    expect(body.name).toBe("edge2");
    expect(body.connection).toEqual({ kubeconfig: "apiVersion: v1\nkind: Config\n" });
    expect(body.platform_internal_url).toBe("http://daegam-platform.daegam-system.svc:9000");
    // 메타데이터 차단은 켤 때만 보낸다. 이 칸을 모르는 옛 레플리카가 롤아웃 중에 받아도 거절하지 않는다 (T75)
    expect(body).not.toHaveProperty("block_metadata");
  });

  it("detail shows preflight and the unenforced NetworkPolicy, skips an unchanged defaults save, re-runs preflight, and edits deployment defaults as JSON", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/clusters/cl-edge" />);
    await screen.findByTestId("cluster-detail-page");
    expect(screen.getByTestId("preflight-status")).toHaveTextContent("failed");
    expect(screen.getByTestId("preflight-missing")).toHaveTextContent("toolhive-operator");
    // 집행하는 컨트롤러가 없으면 NetworkPolicy 가 아무것도 막지 않는다는 사실을 적는다. 다시 점검하면 결과가 바뀌므로 먼저 본다
    expect(screen.getByTestId("preflight-network-policy-warning")).toHaveTextContent("아무것도 막지 않습니다");
    // 컨트롤러 줄에도 집행하는 컨트롤러가 없다고 적는다
    expect(screen.getByTestId("preflight-network-policy")).toHaveTextContent("집행하는 컨트롤러 없음");

    // 배포 기본값이 그대로면 보내지 않고 오류가 아닌 안내로 알린다. 아래의 PATCH 보다 먼저 본다
    // 예전에는 영어 "nothing changed" 를 빨간 파싱 오류 자리에 띄워 실패로 읽혔다
    await user.click(screen.getByTestId("defaults-save"));
    const notice = await screen.findByTestId("defaults-notice");
    expect(notice).toHaveTextContent("바뀐 것이 없어 저장하지 않았습니다.");
    expect(notice).toHaveClass("bg-info-soft");
    expect(screen.queryByTestId("defaults-parse-error")).not.toBeInTheDocument();
    expect(lastRequest("PATCH", "/clusters/cl-edge")).toBeUndefined();

    await user.click(screen.getByTestId("preflight-button"));
    expect(await screen.findByTestId("preflight-k8s")).toHaveTextContent("v1.34.0");
    expect(screen.getByTestId("preflight-status")).toHaveTextContent("ok");
    expect(lastRequest("POST", "/clusters/cl-edge/preflight")).toBeDefined();

    // 잘못된 JSON
    const editor = screen.getByTestId("defaults-json") as HTMLTextAreaElement;
    await user.clear(editor);
    await user.type(editor, "{{not json");
    await user.click(screen.getByTestId("defaults-save"));
    expect(await screen.findByTestId("defaults-parse-error")).toHaveTextContent("invalid JSON");

    // 허용되지 않는 키
    await user.clear(editor);
    await user.type(editor, '{{"name": "x"}');
    await user.click(screen.getByTestId("defaults-save"));
    expect(await screen.findByTestId("defaults-parse-error")).toHaveTextContent("unknown keys: name");

    // 바뀐 키만 PATCH
    await user.clear(editor);
    await user.type(
      editor,
      '{{"images": {{"pull_policy": "Always", "registry_prefix": "reg.local/"}, "scheduling": {{}}',
    );
    await user.click(screen.getByTestId("defaults-save"));
    await screen.findByTestId("defaults-saved");
    expect(lastRequest("PATCH", "/clusters/cl-edge")?.body).toEqual({
      images: { pull_policy: "Always", registry_prefix: "reg.local/" },
    });
  });

  it("hides delete for the home cluster", async () => {
    render(<TestApp initialPath="/clusters/cl-home" />);
    await screen.findByTestId("cluster-detail-page");
    expect(screen.queryByTestId("delete-button")).not.toBeInTheDocument();
    expect(screen.getByTestId("preflight-toolhive")).toHaveTextContent("0.51.2");
    expect(screen.getByTestId("preflight-usage-metrics")).toHaveTextContent("꺼짐");
  });

  it.each([
    [true, "켜짐", true],
    [null, "-", false],
  ])("usage metrics 줄: %s 이면 %s 이다 (P50 3절)", async (value, shown, warn) => {
    clusterHome.gates = { ...clusterHome.gates!, usage_metrics: value };
    try {
      render(<TestApp initialPath="/clusters/cl-home" />);
      const row = await screen.findByTestId("preflight-usage-metrics");
      expect(row).toHaveTextContent(shown);
      expect(Boolean(row.querySelector(".text-warn-fg"))).toBe(warn);
    } finally {
      clusterHome.gates = { ...clusterHome.gates!, usage_metrics: false };
    }
  });

  it("tenant Secrets 줄은 RoleBinding 검사를 보이고, 클러스터 전체 Secret 권한이 남으면 안내한다 (P63)", async () => {
    // home 픽스처: 이 릴리스의 RBAC 이고 관리 네임스페이스 둘에 모두 RoleBinding 이 있다. 안내는 없다
    let app = render(<TestApp initialPath="/clusters/cl-home" />);
    expect(await screen.findByTestId("preflight-secret-rights")).toHaveTextContent("네임스페이스 2개 모두 RoleBinding 있음");
    expect(screen.queryByTestId("preflight-cluster-secret-rights")).not.toBeInTheDocument();
    expect(screen.queryByTestId("preflight-unbound-secret-rights")).not.toBeInTheDocument();
    app.unmount();

    // 클러스터 전체 Secret 권한이 남은 클러스터는 이 릴리스의 RBAC 파일을 적용하라고 한다
    const withRights = (secret_rights: object) =>
      server.use(
        http.get("/clusters/:id", () =>
          HttpResponse.json({ ...clusterHome, status: { preflight: { ...clusterHome.status.preflight!, secret_rights } } }),
        ),
      );
    withRights({ cluster_wide: true, role: true, writable: true, namespaces: 2, unbound: 0, unbound_namespaces: [] });
    app = render(<TestApp initialPath="/clusters/cl-home" />);
    expect(await screen.findByTestId("preflight-cluster-secret-rights")).toHaveTextContent(
      "이 릴리스의 RBAC 파일을 적용하면 이 권한이 없어집니다",
    );
    app.unmount();

    const unbound = { cluster_wide: false, role: true, writable: true, namespaces: 3, unbound: 2, unbound_namespaces: ["daegam-a", "daegam-b"] };
    server.use(
      http.get("/clusters/:id", () =>
        HttpResponse.json({ ...clusterHome, status: { preflight: { ...clusterHome.status.preflight!, secret_rights: unbound } } }),
      ),
    );
    render(<TestApp initialPath="/clusters/cl-home" />);
    const row = await screen.findByTestId("preflight-secret-rights");
    expect(row).toHaveTextContent("네임스페이스 3개 중 2개에 RoleBinding 없음: daegam-a, daegam-b");
    expect(row.querySelector(".text-warn-fg")).not.toBeNull();
    expect(screen.queryByTestId("preflight-cluster-secret-rights")).not.toBeInTheDocument();
    expect(screen.getByTestId("preflight-unbound-secret-rights")).toHaveTextContent(
      "RoleBinding 이 없는 네임스페이스에서는 플랫폼이 Secret 을 읽거나 쓸 수 없습니다",
    );
  });

  it.each([
    ["10일 뒤에 끝나면 안내한다", 10, true],
    ["90일 뒤에 끝나면 조용하다", 90, false],
  ])("함께 올린 Redis 의 TLS 인증서가 %s (P61)", async (_label, days, warn) => {
    const end = new Date(Date.now() + days * 24 * 60 * 60 * 1000).toISOString();
    const preflight = { ...clusterHome.status.preflight!, shipped_redis_cert_expires_at: end };
    server.use(http.get("/clusters/:id", () => HttpResponse.json({ ...clusterHome, status: { preflight } })));
    render(<TestApp initialPath="/clusters/cl-home" />);
    await screen.findByTestId("preflight-secret-rights");
    const note = screen.queryByTestId("preflight-shipped-redis-cert");
    if (warn) {
      expect(note).toHaveTextContent("이 설치에 사용한 배포 절차로 인증서를 갱신해 주세요");
      // 공개 설치에는 없는 배포 명령과 그 스위치를 말하지 않는다 (P85 18절)
      expect(note?.textContent).not.toMatch(/make |=1/);
    } else expect(note).not.toBeInTheDocument();
  });

  it.each([
    ["역할에 Secret 동사가 없으면", { role: false, namespaces: 2, unbound: 0 }, "daegam-tenant-objects 에 Secret 권한 없음"],
    ["관리 네임스페이스가 아직 없으면", { role: true, namespaces: 0, unbound: 0 }, "아직 관리하는 네임스페이스 없음"],
    ["네임스페이스 목록을 얻지 못했으면", { role: true, namespaces: null, unbound: 0 }, "-"],
  ])("tenant Secrets 줄: %s 그렇게 적는다", async (_case, rights, shown) => {
    const secret_rights = { cluster_wide: false, writable: null, unbound_namespaces: [], ...rights };
    server.use(
      http.get("/clusters/:id", () =>
        HttpResponse.json({ ...clusterHome, status: { preflight: { ...clusterHome.status.preflight!, secret_rights } } }),
      ),
    );
    render(<TestApp initialPath="/clusters/cl-home" />);
    expect((await screen.findByTestId("preflight-secret-rights")).textContent).toBe(shown);
  });

  it("설명한다: preflight 가 무엇이 없는지 대신 무엇을 하면 되는지", () => {
    const groups = groupMissing([
      "crd: toolhive.stacklok.dev/v1beta1/mcpservers",
      "rbac: toolhive.stacklok.dev/mcpservers:create",
      "rbac: toolhive.stacklok.dev/mcpservers:delete",
      "toolhive-operator",
    ]);
    expect(groups.map((g) => g.prefix)).toEqual(["crd", "rbac", "other"]);
    expect(groups[1].items).toEqual(["toolhive.stacklok.dev/mcpservers:create", "toolhive.stacklok.dev/mcpservers:delete"]);
    expect(dictionary("ko").clusters.detail.missing[groups[1].prefix]).toContain("target: remote");
    expect(groups[2].items).toEqual(["toolhive-operator"]);
  });

  it("상세는 모자란 RBAC 권한을 한 줄의 조치와 개수로 적고, 접두사 없는 줄은 따로 적는다", async () => {
    server.use(
      http.get("/clusters/:id", () =>
        HttpResponse.json({
          ...clusterEdge,
          status: {
            preflight: {
              ...clusterEdge.status.preflight!,
              missing: ["rbac: toolhive.stacklok.dev/mcpservers:create", "rbac: toolhive.stacklok.dev/mcpservers:delete", "toolhive-operator"],
            },
          },
        }),
      ),
    );
    const text = dictionary("ko").clusters.detail;
    render(<TestApp initialPath="/clusters/cl-edge" />);
    const advice = await screen.findByTestId("preflight-advice-rbac");
    expect(advice).toHaveTextContent(text.missing.rbac);
    expect(advice).toHaveTextContent(text.missingCount(2));
    expect(screen.getByTestId("preflight-advice-other")).toHaveTextContent(text.missing.other);
    expect(screen.getByTestId("preflight-advice-other")).not.toHaveTextContent(text.missingCount(1));
  });

  it("k3s 처럼 컨트롤러 파드 없이 집행하는 클러스터는 경고 대신 추정이라고 적는다 (D54)", async () => {
    server.use(
      http.get("/clusters/:id", () =>
        HttpResponse.json({
          ...clusterEdge,
          status: {
            preflight: { ...clusterEdge.status.preflight!, network_policy: "presumed", policy_controller: "k3s (embedded kube-router)" },
          },
          warnings: [],
        }),
      ),
    );
    render(<TestApp initialPath="/clusters/cl-edge" />);
    await screen.findByTestId("cluster-detail-page");
    expect(screen.getByTestId("preflight-network-policy")).toHaveTextContent("집행 추정 (k3s (embedded kube-router))");
    expect(screen.getByTestId("preflight-network-policy-presumed")).toHaveTextContent("DAEGAM_HOME_NETWORK_POLICY");
    expect(screen.queryByTestId("preflight-network-policy-warning")).not.toBeInTheDocument();
  });

  it("locks writes for a user without cluster:write instead of failing on submit", async () => {
    server.use(http.get("/me", () => HttpResponse.json(meDeveloper)));
    render(<TestApp initialPath="/clusters" />);
    await screen.findByTestId("cluster-row-home");
    expect(await screen.findByTestId("readonly-notice")).toBeInTheDocument();
    expect(screen.queryByTestId("cluster-new")).not.toBeInTheDocument();

    render(<TestApp initialPath="/clusters/cl-edge" />);
    await waitFor(() => expect(screen.getAllByTestId("preflight-button")[0]).toBeDisabled());
    expect(screen.queryByTestId("delete-button")).not.toBeInTheDocument();

    render(<TestApp initialPath="/clusters/new" />);
    await screen.findAllByTestId("readonly-notice");
    expect(screen.queryByTestId("cl-kubeconfig")).not.toBeInTheDocument();
  });
});

describe("cluster exposure defaults", () => {
  it("parses annotation lines and builds the ingress body", () => {
    expect(parseAnnotations('a/b: "1"\n# note\nc: d: e\n')).toEqual({ value: { "a/b": "1", c: "d: e" } });
    expect(parseAnnotations("no-colon").badLine).toBe("no-colon");
    expect(annotationsText({ x: "1", y: "2" })).toBe("x: 1\ny: 2");
    const r = ingressOfForm({
      enabled: true,
      allow_unauthenticated: false,
      class_name: " nginx ",
      allowed_classes: "nginx, traefik",
      domain: "",
      tls: "secret",
      default_issuer: "",
      annotations: "",
      controller_namespace: "ingress-nginx",
      controller_ip_blocks: "10.0.0.0/8\n192.168.0.0/16",
      reserved_hosts: "",
    });
    expect(r.value).toEqual({
      enabled: true,
      allow_unauthenticated: false,
      class_name: "nginx",
      allowed_classes: ["nginx", "traefik"],
      domain: null,
      tls: "secret",
      default_issuer: null,
      annotations: null,
      controller_namespace: "ingress-nginx",
      controller_ip_blocks: ["10.0.0.0/8", "192.168.0.0/16"],
      reserved_hosts: [],
    });
  });

  it("edits the ingress defaults as a form and PATCHes {ingress}; home shows the platform-settings notice", async () => {
    const user = userEvent.setup();
    const app = render(<TestApp initialPath="/clusters/cl-edge" />);
    await screen.findByTestId("ingress-card");
    expect(screen.queryByTestId("ingress-home-notice")).not.toBeInTheDocument();
    expect(screen.getByTestId("ci-enabled")).toBeChecked();
    expect(screen.getByTestId("ci-class")).toHaveValue("traefik");
    expect(screen.getByTestId("ci-reserved-hosts")).toHaveValue("daegam.com");
    expect(screen.getByTestId("ingress-annotation-example")).toHaveTextContent("proxy-read-timeout");

    await user.click(screen.getByTestId("ci-allow-unauth"));
    await user.clear(screen.getByTestId("ci-class"));
    await user.type(screen.getByTestId("ci-class"), "nginx");
    await user.type(screen.getByTestId("ci-allowed-classes"), "nginx");
    await user.clear(screen.getByTestId("ci-domain"));
    await user.type(screen.getByTestId("ci-domain"), "mcp.example.com");
    await selectValue(user, "ci-tls", "cert-manager");
    await user.type(screen.getByTestId("ci-issuer"), "letsencrypt");
    await user.clear(screen.getByTestId("ci-controller-ns"));
    await user.type(screen.getByTestId("ci-controller-ns"), "ingress-nginx");
    await user.type(screen.getByTestId("ci-ip-blocks"), "10.0.0.0/8");
    await user.click(screen.getByTestId("ingress-example-apply"));
    // 잘못된 annotation 줄은 저장 전에 막는다
    await user.type(screen.getByTestId("ci-annotations"), "\nbroken");
    await user.click(screen.getByTestId("ingress-save"));
    expect(await screen.findByTestId("ingress-form-error")).toHaveTextContent("broken");
    expect(lastRequest("PATCH", "/clusters/cl-edge")).toBeUndefined();

    await user.click(screen.getByTestId("ingress-example-apply"));
    const ta = screen.getByTestId("ci-annotations") as HTMLTextAreaElement;
    await user.clear(ta);
    await user.type(ta, "nginx.ingress.kubernetes.io/proxy-read-timeout: 3600");
    await user.click(screen.getByTestId("ingress-save"));
    await screen.findByTestId("ingress-saved");
    expect(lastRequest("PATCH", "/clusters/cl-edge")?.body).toEqual({
      ingress: {
        enabled: true,
        allow_unauthenticated: false,
        class_name: "nginx",
        allowed_classes: ["nginx"],
        domain: "mcp.example.com",
        tls: "cert-manager",
        default_issuer: "letsencrypt",
        annotations: { "nginx.ingress.kubernetes.io/proxy-read-timeout": "3600" },
        controller_namespace: "ingress-nginx",
        controller_ip_blocks: ["10.0.0.0/8"],
        reserved_hosts: ["daegam.com"],
      },
    });

    app.unmount();
    render(<TestApp initialPath="/clusters/cl-home" />);
    // home 에서 플랫폼 설정(DAEGAM_HOME_*)이 정한 칸은 기동마다 다시 쓰이므로 이름을 보이고 막는다 (D54)
    const notice = await screen.findByTestId("ingress-home-notice");
    expect(notice).toHaveTextContent("DAEGAM_HOME_");
    expect(notice).toHaveTextContent("ingress.class_name");
    expect(screen.getByTestId("ci-class")).toBeDisabled();
    expect(screen.getByTestId("ci-enabled")).toBeDisabled();
    // 환경이 정하지 않은 칸은 고칠 수 있다
    expect(screen.getByTestId("ci-domain")).toBeEnabled();
    expect(screen.getByTestId("ci-tls")).toBeEnabled();
  });
  it("turns the instance metadata block on with a PATCH {block_metadata}, warns when the controller is unknown, and locks it on a home set by the environment (T75)", async () => {
    const user = userEvent.setup();
    const app = render(<TestApp initialPath="/clusters/cl-edge" />);
    const card = await screen.findByTestId("metadata-card");
    expect(within(card).getByTestId("metadata-hint")).toHaveTextContent("GKE Workload Identity");
    expect(screen.getByTestId("cm-block-metadata")).not.toBeChecked();
    expect(screen.queryByTestId("metadata-unsupported")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("cm-block-metadata"));
    await user.click(screen.getByTestId("metadata-save"));
    await screen.findByTestId("metadata-saved");
    expect(lastRequest("PATCH", "/clusters/cl-edge")?.body).toEqual({ block_metadata: true });
    app.unmount();

    const unsupported = "metadata_block_unsupported: block_metadata is on, but the NetworkPolicy controller (none found) is not one known to work";
    server.use(
      http.get("/clusters/:id", () => HttpResponse.json({ ...clusterEdge, block_metadata: true, warnings: [unsupported] })),
    );
    const again = render(<TestApp initialPath="/clusters/cl-edge" />);
    expect(await screen.findByTestId("metadata-unsupported")).toHaveTextContent("egress 정책을 두지 않았습니다");
    again.unmount();

    // 관리자의 egress 정책이 있어 차단을 두지 않은 네임스페이스는 그 이름을 보인다
    const skipped = "metadata_block_skipped: block_metadata is on, but these namespaces hold egress policies the platform did not make, so they get no metadata block: team-a, team-b";
    server.use(
      http.get("/clusters/:id", () =>
        HttpResponse.json({
          ...clusterEdge,
          block_metadata: true,
          status: { ...clusterEdge.status, metadata_skipped: ["team-a", "team-b"] },
          warnings: [skipped],
        }),
      ),
    );
    const third = render(<TestApp initialPath="/clusters/cl-edge" />);
    const note = await screen.findByTestId("metadata-skipped");
    expect(note).toHaveTextContent("그 네임스페이스의 정책에서 직접 주소를 막아 주세요: team-a, team-b");
    expect(note).not.toHaveTextContent("{namespaces}");
    third.unmount();

    server.use(
      http.get("/clusters/:id", () =>
        HttpResponse.json({ ...clusterHome, block_metadata: true, env_managed: [...clusterHome.env_managed, "block_metadata"] }),
      ),
    );
    render(<TestApp initialPath="/clusters/cl-home" />);
    expect(await screen.findByTestId("metadata-home-notice")).toHaveTextContent("block_metadata");
    expect(screen.getByTestId("cm-block-metadata")).toBeDisabled();
    expect(screen.getByTestId("cm-block-metadata")).toBeChecked();
    expect(screen.getByTestId("metadata-save")).toBeDisabled();
    // 배포 기본값 카드는 이 칸을 자기 안내에 싣지 않는다. 남는 칸이 없어 그 안내가 없다
    expect(screen.queryByTestId("defaults-env-notice")).not.toBeInTheDocument();
  });
});
