/**
 * 허브 상세의 "MCP 주소" 줄 (P55a 1절). remote·entry 의 등록한 주소는 서버가 가르고(`status.address_class`), 화면은 그
 * 갈래를 읽을 뿐 URL 을 직접 가르지 않는다. 밖에서 닿는다고 확인하지 못한 주소는 노출하지 않았다는 말 대신 그 사정을 말한다.
 */
import { render, screen } from "@testing-library/react";

import { TestApp } from "@/App";
import type { HubItem } from "@/api/types";
import { ko } from "@/i18n/ko";

import { hubItemFixture, hubState } from "./msw";

const text = ko.hub.detail;

function entry(id: string, url: string, status: Partial<NonNullable<HubItem["status"]>>): HubItem {
  const base = hubItemFixture();
  return {
    ...base,
    id,
    name: id,
    display_name: id,
    kind: "entry",
    source: { ...base.source, url, transport: "streamable-http", instance_id: null, mcp_profile_id: null },
    k8s: { kind: null, name: `hub-${id}`, namespace: "daegam-default", group: null, tool_config: null },
    status: { ...base.status!, url, ...status },
  };
}

describe("허브 상세의 MCP 주소", () => {
  it("서버가 밖에서 닿는다고 확인하지 못한 주소는 넣지 않는 까닭을 말하고 복사 단추를 두지 않는다", async () => {
    hubState.items = [entry("loop", "http://127.0.0.1:8080/mcp", { external_url: null, address_class: "local" })];
    render(<TestApp initialPath="/hub/loop" />);
    expect(await screen.findByTestId("hub-mcp-url-not-outside")).toHaveTextContent(text.notOutside);
    expect(screen.queryByTestId("copy-mcp-url")).toBeNull();
  });

  it("사설망 주소는 등록한 그대로 보이고 복사할 수 있다", async () => {
    hubState.items = [entry("vpn", "http://10.0.0.5:8080/mcp", { external_url: "http://10.0.0.5:8080/mcp", address_class: "private" })];
    render(<TestApp initialPath="/hub/vpn" />);
    expect(await screen.findByTestId("hub-mcp-url")).toHaveTextContent("http://10.0.0.5:8080/mcp");
    expect(screen.getByTestId("copy-mcp-url")).toBeInTheDocument();
  });

  it("노출하지 않은 internal 항목은 지금처럼 노출하지 않았다고 말한다", async () => {
    hubState.items = [hubItemFixture({ status: { ...hubItemFixture().status!, external_url: null } })];
    render(<TestApp initialPath="/hub/hub-calc" />);
    expect(await screen.findByTestId("hub-mcp-url")).toHaveTextContent(text.notExposed);
    expect(screen.queryByTestId("hub-mcp-url-not-outside")).toBeNull();
  });
});
