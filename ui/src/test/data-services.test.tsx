import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { submitConfirm } from "./confirm";
import { HttpResponse, NOW, apiError, dsPostgres, dsRedis, http, lastRequest, reference, referencesState, server } from "./msw";
import { selectValue } from "./select";

describe("data services", () => {

  it("lists services with kind, host and verified state", async () => {
    render(<TestApp initialPath="/data-services" />);
    const pg = await screen.findByTestId("data-service-row-orders-db");
    expect(pg).toHaveTextContent("postgres");
    expect(pg).toHaveTextContent("pg.internal:5432");
    expect(pg).toHaveTextContent("unverified");
    const redis = screen.getByTestId("data-service-row-cache");
    expect(redis).toHaveTextContent("redis");
    expect(redis).toHaveTextContent("verified");
  });

  it("registers a postgres service with the create body shape", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/data-services/new" />);
    await user.type(screen.getByTestId("ds-name"), "new-db");
    await user.type(screen.getByTestId("ds-host"), "db.internal");
    expect(screen.getByTestId("ds-port")).toHaveValue(5432);
    // 리소스 이름 Database 와 헷갈리지 않게 데이터베이스 자신의 이름 칸은 한국어 라벨이다
    expect(screen.getByLabelText("DB 이름")).toBe(screen.getByTestId("ds-database"));
    await user.type(screen.getByTestId("ds-database"), "app");
    await user.type(screen.getByTestId("ds-username"), "app");
    await user.type(screen.getByTestId("ds-password"), "s3cret");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("data-service-detail-page")).toBeInTheDocument();
    expect(lastRequest("POST", "/data-services")?.body).toEqual({
      name: "new-db",
      kind: "postgres",
      host: "db.internal",
      port: 5432,
      cluster_id: null,
      database: "app",
      username: "app",
      password: "s3cret",
    });
  });

  it("switching to redis swaps DB 이름 for DB 번호 and the default port", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/data-services/new" />);
    await selectValue(user, "ds-kind", "redis");
    expect(screen.getByTestId("ds-port")).toHaveValue(6379);
    expect(screen.queryByTestId("ds-database")).not.toBeInTheDocument();
    expect(screen.getByLabelText("DB 번호")).toBe(screen.getByTestId("ds-db"));
    await user.type(screen.getByTestId("ds-name"), "cache2");
    await user.type(screen.getByTestId("ds-host"), "redis.internal");
    await user.clear(screen.getByTestId("ds-db"));
    await user.type(screen.getByTestId("ds-db"), "3");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("data-service-detail-page");
    const body = lastRequest("POST", "/data-services")?.body as Record<string, unknown>;
    expect(body.kind).toBe("redis");
    expect(body.db).toBe(3);
    expect(body).not.toHaveProperty("database");
    expect(body.password).toBeNull();
  });

  it("a redis TLS endpoint sends its port and CA, and postgres has no TLS fields", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/data-services/new" />);
    expect(screen.queryByTestId("ds-tls")).not.toBeInTheDocument();
    await selectValue(user, "ds-kind", "redis");
    await user.type(screen.getByTestId("ds-name"), "secure");
    await user.type(screen.getByTestId("ds-host"), "redis.internal");
    expect(screen.queryByTestId("ds-tls-port")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("ds-tls"));
    expect(screen.getByTestId("ds-tls-port")).toHaveValue(6380);
    await user.type(screen.getByTestId("ds-tls-ca"), "PEM-CA");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("data-service-detail-page");
    const body = lastRequest("POST", "/data-services")?.body as Record<string, unknown>;
    expect(body.tls).toEqual({ port: 6380, ca: "PEM-CA" });
  });

  it("detail names the consumers that still connect to the plain port", async () => {
    const secure = { ...dsRedis, tls: { port: 6380, ca_secret: "ds-ds-redis-ca" } };
    const consumers = [
      { kind: "agent", id: "ag-1", name: "helper", data_service_id: "ds-redis", reason: "runtime_image" },
      { kind: "mcp_server", id: "ms-1", name: "calc", data_service_id: "ds-redis", reason: "binding" },
      { kind: "mcp_server", id: "ms-1", name: "calc", data_service_id: "ds-redis", reason: "session_storage" },
    ];
    server.use(
      http.get("/data-services/ds-redis", () => HttpResponse.json(secure)),
      http.get("/data-services/ds-redis/plain-consumers", () => HttpResponse.json({ consumers })),
    );
    render(<TestApp initialPath="/data-services/ds-redis" />);
    const row = await screen.findByTestId("ds-plain-consumers");
    expect(row).toHaveTextContent("Agent 1개와 MCP Server 1개가 아직 평문 포트로 접속합니다.");
    expect(row).toHaveTextContent("Agent 는 클러스터의 에이전트 이미지가 새 판이어야 TLS 포트로 옮겨 갑니다.");
    expect(row).toHaveTextContent("helper, calc");
  });

  it("detail counts an MCP Server once by id, adds the image note only for runtime_image, and shows a failed read", async () => {
    const secure = { ...dsRedis, tls: { port: 6380, ca_secret: "ds-ds-redis-ca" } };
    const consumers = [
      { kind: "mcp_server", id: "ms-1", name: "calc", data_service_id: "ds-redis", reason: "binding" },
      { kind: "mcp_server", id: "ms-2", name: "calc", data_service_id: "ds-redis", reason: "unknown" },
    ];
    server.use(
      http.get("/data-services/ds-redis", () => HttpResponse.json(secure)),
      http.get("/data-services/ds-redis/plain-consumers", () => HttpResponse.json({ consumers })),
    );
    const view = render(<TestApp initialPath="/data-services/ds-redis" />);
    const row = await screen.findByTestId("ds-plain-consumers");
    expect(row).toHaveTextContent("MCP Server 2개가 아직 평문 포트로 접속합니다.");
    expect(row).not.toHaveTextContent("에이전트 이미지");
    view.unmount();
    server.use(
      http.get("/data-services/ds-redis/plain-consumers", () =>
        HttpResponse.json({ detail: "boom", code: "internal_error" }, { status: 500 }),
      ),
    );
    render(<TestApp initialPath="/data-services/ds-redis" />);
    expect(await screen.findByTestId("ds-plain-consumers-error")).toBeInTheDocument();
  });

  it("detail says nothing connects to the plain port, and asks nothing without TLS", async () => {
    const secure = { ...dsRedis, tls: { port: 6380, ca_secret: "ds-ds-redis-ca" } };
    server.use(http.get("/data-services/ds-redis", () => HttpResponse.json(secure)));
    const view = render(<TestApp initialPath="/data-services/ds-redis" />);
    expect(await screen.findByTestId("ds-plain-consumers")).toHaveTextContent(
      "평문 포트로 접속하는 Agent 와 MCP Server 가 없습니다.",
    );
    view.unmount();
    server.use(http.get("/data-services/ds-redis", () => HttpResponse.json(dsRedis)));
    render(<TestApp initialPath="/data-services/ds-redis" />);
    await screen.findByTestId("data-service-detail-page");
    expect(screen.queryByTestId("ds-plain-consumers")).not.toBeInTheDocument();
  });

  it("detail marks a closed plain port next to the TLS endpoint", async () => {
    const closed = { ...dsRedis, tls: { port: 6380, ca_secret: "ds-ds-redis-ca" }, plain_closed: true };
    server.use(http.get("/data-services/ds-redis", () => HttpResponse.json(closed)));
    const view = render(<TestApp initialPath="/data-services/ds-redis" />);
    await screen.findByTestId("data-service-detail-page");
    expect(screen.getByTestId("ds-tls-summary")).toHaveTextContent("port 6380 · CA ds-ds-redis-ca · plain port closed");
    view.unmount();
    const open = { ...closed, plain_closed: false };
    server.use(http.get("/data-services/ds-redis", () => HttpResponse.json(open)));
    render(<TestApp initialPath="/data-services/ds-redis" />);
    await screen.findByTestId("data-service-detail-page");
    expect(screen.getByTestId("ds-tls-summary")).not.toHaveTextContent("plain port closed");
  });

  it("detail keeps the CA unless a new one is entered and can turn TLS off", async () => {
    const secure = { ...dsRedis, tls: { port: 6380, ca_secret: "ds-ds-redis-ca" } };
    server.use(http.get("/data-services/ds-redis", () => HttpResponse.json(secure)));
    const user = userEvent.setup();
    render(<TestApp initialPath="/data-services/ds-redis" />);
    await screen.findByTestId("data-service-detail-page");
    expect(screen.getByTestId("ds-tls-summary")).toHaveTextContent("port 6380 · CA ds-ds-redis-ca");
    expect(screen.getByTestId("ds-tls")).toBeChecked();
    expect(screen.getByTestId("ds-tls-ca")).toHaveValue("");

    const port = screen.getByTestId("ds-tls-port");
    await user.clear(port);
    await user.type(port, "6390");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("saved");
    expect(lastRequest("PATCH", "/data-services/ds-redis")?.body).toEqual({ tls: { port: 6390, ca: null } });

    await user.click(screen.getByTestId("ds-tls"));
    await user.click(screen.getByTestId("save-button"));
    await waitFor(() => expect(lastRequest("PATCH", "/data-services/ds-redis")?.body).toEqual({ tls: null }));
  });

  it("shows a name conflict from the API", async () => {
    server.use(http.post("/data-services", () => apiError(409, "name_conflict", "data service 'x' already exists")));
    const user = userEvent.setup();
    render(<TestApp initialPath="/data-services/new" />);
    await user.type(screen.getByTestId("ds-name"), "x");
    await user.type(screen.getByTestId("ds-host"), "h");
    await user.type(screen.getByTestId("ds-password"), "p");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("save-error")).toHaveTextContent("name_conflict");
  });

  it("detail patches only changed fields, verifies and deletes", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/data-services/ds-pg" />);
    await screen.findByTestId("data-service-detail-page");
    expect(screen.getByTestId("ds-host")).toHaveValue("pg.internal");

    const host = screen.getByTestId("ds-host");
    await user.clear(host);
    await user.type(host, "pg2.internal");
    await user.type(screen.getByTestId("ds-password"), "newpass");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("saved");
    expect(lastRequest("PATCH", "/data-services/ds-pg")?.body).toEqual({ host: "pg2.internal", password: "newpass" });

    // postgres 는 pgvector 칩만 있고 verify 전이라 unverified 다. redis 줄은 없다
    expect(screen.getByTestId("ds-capability-pgvector")).toHaveTextContent("pgvector unverified");
    expect(screen.queryByTestId("ds-capability-checkpointer")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("verify-button"));
    expect(await screen.findByTestId("verify-result")).toHaveTextContent("Connection ok");
    expect(lastRequest("POST", "/data-services/ds-pg/verify")).toBeDefined();
    expect(screen.queryByTestId("verify-redis")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("delete-button"));
    await submitConfirm(user);
    expect(await screen.findByTestId("data-services-page")).toBeInTheDocument();
    expect(lastRequest("DELETE", "/data-services/ds-pg")).toBeDefined();
  });

  it("redis 상세는 checkpointer 칩을 보이고 verify 가 버전·모듈·checkpointer 한 줄을 붙인다 (D33)", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/data-services/ds-redis" />);
    await screen.findByTestId("data-service-detail-page");
    expect(screen.getByTestId("ds-capability-checkpointer")).toHaveTextContent("checkpointer ready");
    expect(screen.queryByTestId("ds-capability-pgvector")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("verify-button"));
    expect(await screen.findByTestId("verify-result")).toHaveTextContent("Connection ok");
    expect(screen.getByTestId("verify-redis")).toHaveTextContent("Redis 8.2.1 · 모듈 search, ReJSON · checkpointer ready");
  });

  it("db 가 0 이 아닌 redis 는 모듈이 다 있어도 체크포인터로 못 쓴다고 말한다 (P17 F5)", async () => {
    server.use(
      http.get("/data-services/:id", () =>
        HttpResponse.json({ ...dsRedis, db: 3, capabilities: { checkpointer: "ready" } }),
      ),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/data-services/ds-redis" />);
    await screen.findByTestId("data-service-detail-page");
    // 값은 그대로 ready 지만 칩이 초록이 아니고, 옆 줄이 왜 못 쓰는지와 무엇을 바꾸면 되는지를 적는다
    const chip = screen.getByTestId("ds-capability-checkpointer");
    expect(chip).toHaveTextContent("checkpointer ready");
    expect(chip).toHaveTextContent("db 3에서는 사용할 수 없습니다");
    expect(screen.getByTestId("ds-capability-note-checkpointer")).toHaveTextContent("RediSearch 는 db 0 에서만 인덱스를 만들므로");
    expect(screen.getByTestId("ds-capability-note-checkpointer")).toHaveTextContent("위 DB 번호를 0 으로 바꾸면 됩니다");
    // 접속 자체는 되므로 verify 는 ok 다. 그래도 같은 설명을 함께 내려 초록으로 끝내지 않는다
    await user.click(screen.getByTestId("verify-button"));
    const result = await screen.findByTestId("verify-result");
    expect(result).toHaveTextContent("Connection ok");
    expect(screen.getByTestId("verify-checkpointer-note")).toHaveTextContent("Agent 의 memory 로 사용할 수 없습니다");
  });

  it("verify 계정에 INFO·MODULE LIST 권한이 없으면 버전은 그렇게 말하고 모듈 부분은 뺀다", async () => {
    server.use(
      http.get("/data-services/:id", () => HttpResponse.json({ id: "ds-redis", tenant: "default", name: "cache", display_name: "cache", cluster_id: "cl-home", kind: "redis", host: "redis.internal", port: 6379, database: null, db: 1, username: "limited", secret_name: "ds-ds-redis", has_password: true, capabilities: { checkpointer: "unknown" }, verified_at: NOW, created_at: NOW, updated_at: NOW, updated_by: null })),
      http.post("/data-services/:id/verify", () => HttpResponse.json({ id: "ds-redis", ok: true, verified_at: NOW, detail: "redis.internal:6379/0: connected", redis: { version: null, modules: null, checkpointer: "unknown" } })),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/data-services/ds-redis" />);
    await screen.findByTestId("data-service-detail-page");
    expect(screen.getByTestId("ds-capability-checkpointer")).toHaveTextContent("checkpointer unknown");
    await user.click(screen.getByTestId("verify-button"));
    const line = await screen.findByTestId("verify-redis");
    expect(line).toHaveTextContent("Redis 버전 확인 권한 없음 · checkpointer unknown");
    expect(line).not.toHaveTextContent("모듈");
  });

  it("delete shows the bound MCP Servers on 409", async () => {
    // 대화상자는 references 를 미리 받아 두므로 409 가 실어 오는 id 를 이름으로 바꿔 보인다 (D43)
    referencesState["data-services/ds-redis"] = [reference("mcp-server", "ms-1", { display_name: "주문 MCP", state: "deployed" })];
    server.use(
      http.delete("/data-services/:id", () =>
        apiError(409, "in_use", "data service is bound by mcp servers", { mcp_server_ids: ["ms-1"] }),
      ),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/data-services/ds-redis" />);
    await screen.findByTestId("data-service-detail-page");
    await user.click(screen.getByTestId("delete-button"));
    await submitConfirm(user);
    const err = await screen.findByTestId("confirm-error");
    expect(err).toHaveTextContent("다른 리소스가 사용하고 있어 삭제할 수 없습니다");
    expect(err).toHaveTextContent("MCP Server 주문 MCP");
    expect(err).not.toHaveTextContent("ms-1");
    // 409 를 기다리지 않고 열 때부터 무엇이 붙들고 있는지 보인다
    expect(screen.getByTestId("delete-references")).toHaveTextContent("주문 MCP");
    expect(screen.getByTestId("data-service-detail-page")).toBeInTheDocument();
  });

  it("delete 409 는 Gateway·Agent 가 묶고 있을 때도 무엇을 떼야 하는지 보여 준다", async () => {
    // 백엔드는 in_use 를 여러 갈래로 낸다(mcp_server_ids, gateway_ids, agent_ids,
    // deployed_agent_ids). 한 갈래라도 못 풀면 "묶여 있다" 는 문장만 남아 화면에서 해결할 수 없다.
    // 화면에는 키 이름이 아니라 리소스 이름이 붙는다 (lib/errorText.ts)
    for (const [key, id, label] of [
      ["gateway_ids", "gw-1", "Gateway"],
      ["agent_ids", "ag-1", "Agent"],
      ["deployed_agent_ids", "ag-2", "배포된 Agent"],
    ] as const) {
      server.use(
        http.delete("/data-services/:id", () =>
          apiError(409, "in_use", "data service is bound", { [key]: [id] }),
        ),
      );
      const user = userEvent.setup();
      const view = render(<TestApp initialPath="/data-services/ds-redis" />);
      await screen.findByTestId("data-service-detail-page");
      await user.click(screen.getByTestId("delete-button"));
      await submitConfirm(user);
      const err = await screen.findByTestId("confirm-error");
      // 이름을 못 찾으면 날 id 대신 종류와 개수다
      expect(err, key).toHaveTextContent(`${label} 1개`);
      expect(err, key).not.toHaveTextContent(id);
      view.unmount();
    }
  });

  it("상세 편집은 새로 만들기와 같은 Host·Port 제약을 걸고, 바뀐 것이 없으면 저장이 서버로 가지 않으며, 접속 정보를 고쳐 저장하면 확인 상태를 지웠다는 것을 말한다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/data-services/ds-pg" />);
    await screen.findByTestId("data-service-detail-page");
    // 편집 칸에만 제약이 없으면 서버가 받지 않을 포트를 보낸 뒤에야 422 를 본다
    expect(screen.getByTestId("ds-host")).toBeRequired();
    const port = screen.getByTestId("ds-port");
    expect(port).toBeRequired();
    expect(port).toHaveAttribute("min", "1");
    expect(port).toHaveAttribute("max", "65535");

    // 빈 PATCH 는 백엔드가 422 empty_patch 로 막는다. 왕복해서 오류를 받는 대신 화면에서 끝낸다
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("ds-notice")).toHaveTextContent("바뀐 것이 없어 저장하지 않았습니다.");
    expect(lastRequest("PATCH", "/data-services/ds-pg")).toBeUndefined();
    expect(screen.queryByTestId("saved")).not.toBeInTheDocument();

    // 주소(host)만 바꿔도 확인은 바뀐 주소에 해당하지 않으므로 서버가 verified_at 을 지운다 (P68)
    const host = screen.getByTestId("ds-host");
    await user.clear(host);
    await user.type(host, "pg2.internal");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("saved")).toHaveTextContent("확인 상태를 지웠습니다");

    // 비밀번호를 보내면 서버가 verified_at 을 지운다. 화면이 그 이유와 다음 행동을 말한다
    await user.type(screen.getByTestId("ds-password"), "newpass");
    await user.click(screen.getByTestId("save-button"));
    await waitFor(() => expect(screen.getByTestId("saved")).toHaveTextContent("확인 상태를 지웠습니다"));
    expect(screen.getByTestId("saved")).toHaveTextContent("연결 확인을 다시 눌러 주세요");
  });

  it("verify 실패는 코드만 보이지 않고 무엇을 고치라는지 적는다", async () => {
    // verify 는 실패도 200 결과 객체로 온다. postgres 는 connect_failed·auth_failed·unknown_database 다
    for (const [code, sentence] of [
      ["connect_failed", "Host 와 Port"],
      ["auth_failed", "Username 과 New password"],
      ["unknown_database", "그 이름의 database 가 서버에 없습니다"],
    ] as const) {
      server.use(
        http.post("/data-services/:id/verify", () =>
          HttpResponse.json({ id: "ds-pg", ok: false, error: code, detail: `10.0.0.5:5432: ${code}` }),
        ),
      );
      const user = userEvent.setup();
      const view = render(<TestApp initialPath="/data-services/ds-pg" />);
      await screen.findByTestId("data-service-detail-page");
      await user.click(screen.getByTestId("verify-button"));
      expect(await screen.findByTestId("verify-hint"), code).toHaveTextContent(sentence);
      // 코드는 사라지지 않고 문장 아래 고정폭 줄로 남는다
      expect(screen.getByTestId("verify-detail")).toHaveTextContent(code);
      view.unmount();
    }
  });

  it("redis 의 modules_missing 은 Redis 8 이 필요하다는 말까지 한다 (D33)", async () => {
    server.use(
      http.get("/data-services/:id", () =>
        HttpResponse.json({ ...dsRedis, capabilities: { checkpointer: "modules_missing" }, verified_at: NOW }),
      ),
      http.post("/data-services/:id/verify", () =>
        HttpResponse.json({ id: "ds-redis", ok: true, verified_at: NOW, detail: "connected", redis: { version: "7.4.1", modules: [], checkpointer: "modules_missing" } }),
      ),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/data-services/ds-redis" />);
    await screen.findByTestId("data-service-detail-page");
    // 칩은 사람 문구를 보이고 서버 값은 옆에 그대로 남는다
    const chip = screen.getByTestId("ds-capability-checkpointer");
    expect(chip).toHaveTextContent("RediSearch·RedisJSON 이 없습니다");
    expect(chip).toHaveTextContent("checkpointer modules_missing");
    expect(screen.getByTestId("ds-capability-note-checkpointer")).toHaveTextContent("Redis 8");
    // 접속이 성공해도 초록 "문제 없음" 으로 끝내지 않는다
    await user.click(screen.getByTestId("verify-button"));
    expect(await screen.findByTestId("verify-checkpointer-note")).toHaveTextContent("Redis 8");
  });

  it("지난 확인의 capability 는 흐려지고 다시 확인하라고 말한다", async () => {
    // 서버는 접속 정보가 바뀌면 verified_at 만 지우고 capabilities 는 남긴다
    server.use(http.get("/data-services/:id", () => HttpResponse.json({ ...dsPostgres, capabilities: { pgvector: "installed" }, verified_at: null })));
    render(<TestApp initialPath="/data-services/ds-pg" />);
    await screen.findByTestId("data-service-detail-page");
    expect(screen.getByTestId("ds-capabilities-stale")).toHaveTextContent("지난 확인의 결과입니다");
  });
});
