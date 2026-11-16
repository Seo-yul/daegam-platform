import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect } from "react";
import { BrowserRouter, MemoryRouter, Navigate, Route, Routes } from "react-router";

import { Layout } from "@/components/Layout";
import { useT } from "@/i18n";
import { HomeChatPage } from "@/pages/home-chat/HomeChat";
import { HubDetailPage } from "@/pages/hub/Detail";
import { HubListPage } from "@/pages/hub/List";
import { HubNewPage } from "@/pages/hub/New";
import { GroupMappingsPage } from "@/pages/admin/GroupMappings";
import { PlatformAdminOnly } from "@/pages/admin/permission";
import { PlatformSettingsPage } from "@/pages/admin/PlatformSettings";
import { PlatformUsagePage } from "@/pages/admin/Usage";
import { AuditPage, McpAuditPage } from "@/pages/audit/Audit";
import { CallbackPage } from "@/pages/auth/Callback";
import { ConsentPage } from "@/pages/auth/Consent";
import { LoginPage } from "@/pages/auth/Login";
import { ApprovalChoicesPage } from "@/pages/account/ApprovalChoices";
import { RunsAsMePage } from "@/pages/account/RunsAsMe";
import { PasswordPage } from "@/pages/account/Password";
import { TokensPage } from "@/pages/account/Tokens";
import { UsersPage } from "@/pages/admin/Users";
import { TenantSettingsPage } from "@/pages/tenant/Settings";
import { UsagePage } from "@/pages/usage/Usage";
import { BackendApiDetailPage } from "@/pages/backend-apis/Detail";
import { BackendApiListPage } from "@/pages/backend-apis/List";
import { BackendApiNewPage } from "@/pages/backend-apis/New";
import { BackendApiRefinePage } from "@/pages/backend-apis/Refine";
import { ClusterDetailPage } from "@/pages/clusters/Detail";
import { ClusterListPage } from "@/pages/clusters/List";
import { ClusterNewPage } from "@/pages/clusters/New";
import { DataServiceDetailPage } from "@/pages/data-services/Detail";
import { DataServiceListPage } from "@/pages/data-services/List";
import { DataServiceNewPage } from "@/pages/data-services/New";
import { GatewayDetailPage } from "@/pages/gateways/Detail";
import { GatewayFormPage } from "@/pages/gateways/Form";
import { GatewayListPage } from "@/pages/gateways/List";
import { KnowledgeBaseDetailPage } from "@/pages/knowledge-bases/Detail";
import { KnowledgeBaseListPage } from "@/pages/knowledge-bases/List";
import { KnowledgeBaseNewPage } from "@/pages/knowledge-bases/New";
import { McpServerDetailPage } from "@/pages/mcp-servers/Detail";
import { McpServerFormPage } from "@/pages/mcp-servers/Form";
import { McpServerListPage } from "@/pages/mcp-servers/List";
import { McpServerSuggestPage } from "@/pages/mcp-servers/Suggest";
import { AgentCanvasPage } from "@/pages/agents/Canvas";
import { AgentDetailPage } from "@/pages/agents/Detail";
import { AgentListPage } from "@/pages/agents/List";
import { SkillDetailPage } from "@/pages/skills/Detail";
import { SkillFormPage } from "@/pages/skills/Form";
import { SkillListPage } from "@/pages/skills/List";
import { PlaceholderPage } from "@/pages/Placeholder";

// UI 는 루트에서 서빙된다 (API 는 /api 아래).
export const BASENAME = "/";

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false, staleTime: 5_000 },
      mutations: { retry: false },
    },
  });
}

export function AppRoutes() {
  // 탭 제목은 제품 이름이다. index.html 은 한국어(대감)로 시작하고 화면 언어가 정해지면 그 말로 바꾼다 (P80 7절)
  const product = useT().shell.names.product;
  useEffect(() => {
    document.title = product;
  }, [product]);
  return (
    <Routes>
      {/* local 인증 모드의 로그인 (D40). 껍데기 밖이다. 껍데기는 토큰이 없으면 여기로 보낸다 */}
      <Route path="login" element={<LoginPage />} />
      {/* MCP 클라이언트가 연 탭의 연결 동의 (P33). 로그인처럼 껍데기 밖이고 토큰이 없으면 스스로 로그인으로 보낸다 */}
      <Route path="oauth/consent" element={<ConsentPage />} />
      <Route element={<Layout />}>
        {/* 첫 화면은 에이전트와 대화하는 곳이다 (D35) */}
        <Route index element={<Navigate to="/chat" replace />} />
        <Route path="chat" element={<HomeChatPage />} />
        <Route path="backend-apis" element={<BackendApiListPage />} />
        <Route path="backend-apis/new" element={<BackendApiNewPage />} />
        <Route path="backend-apis/:id" element={<BackendApiDetailPage />} />
        {/* 만들기와 같은 화면이다. 편집으로 들어오면 기존 값으로 채우고 이름을 잠근다 */}
        <Route path="backend-apis/:id/edit" element={<BackendApiNewPage />} />
        {/* 버전 하위 화면 (D25): 규칙·LLM 으로 설명을 다듬어 새 버전을 만든다 */}
        <Route path="backend-apis/:id/versions/:n/refine" element={<BackendApiRefinePage />} />
        <Route path="data-services" element={<DataServiceListPage />} />
        <Route path="data-services/new" element={<DataServiceNewPage />} />
        <Route path="data-services/:id" element={<DataServiceDetailPage />} />
        {/* Knowledge Base (D32): 문서를 올려 두면 인덱서가 pgvector 에 넣고, 캔버스의 knowledge 노드가 검색 도구로 붙인다 */}
        <Route path="knowledge-bases" element={<KnowledgeBaseListPage />} />
        <Route path="knowledge-bases/new" element={<KnowledgeBaseNewPage />} />
        <Route path="knowledge-bases/:id" element={<KnowledgeBaseDetailPage />} />
        <Route path="clusters" element={<ClusterListPage />} />
        <Route path="clusters/new" element={<ClusterNewPage />} />
        <Route path="clusters/:id" element={<ClusterDetailPage />} />
        {/* 리소스 넷은 정의와 배포를 한 문서로 갖는다 (D42). 상세 하나가 개요·정의·배포·Advanced 탭을 다 든다 */}
        <Route path="mcp-servers" element={<McpServerListPage />} />
        <Route path="mcp-servers/new" element={<McpServerFormPage />} />
        <Route path="mcp-servers/:id" element={<McpServerDetailPage />} />
        <Route path="mcp-servers/:id/edit" element={<McpServerFormPage />} />
        <Route path="mcp-servers/:id/suggest" element={<McpServerSuggestPage />} />
        <Route path="hub" element={<HubListPage />} />
        <Route path="hub/new" element={<HubNewPage />} />
        <Route path="hub/:id" element={<HubDetailPage />} />
        <Route path="gateways" element={<GatewayListPage />} />
        <Route path="gateways/new" element={<GatewayFormPage />} />
        <Route path="gateways/:id" element={<GatewayDetailPage />} />
        <Route path="gateways/:id/edit" element={<GatewayFormPage />} />
        {/* Agent 의 정의는 캔버스다 (D26). 만들 때는 캔버스 한 장이고, 저장된 뒤에는 상세의 정의 탭 안에 든다 */}
        <Route path="agents" element={<AgentListPage />} />
        <Route path="agents/new" element={<AgentCanvasPage />} />
        <Route path="agents/:id" element={<AgentDetailPage />} />
        {/* Skill (D64): 에이전트가 따를 절차서다. 고치면 새 리비전이 쌓이므로 편집 경로가 곧 새 리비전 화면이다 */}
        <Route path="skills" element={<SkillListPage />} />
        <Route path="skills/new" element={<SkillFormPage />} />
        <Route path="skills/:id" element={<SkillDetailPage />} />
        <Route path="skills/:id/edit" element={<SkillFormPage />} />
        <Route path="tenant/settings" element={<TenantSettingsPage />} />
        <Route path="admin" element={<Navigate to="/admin/settings" replace />} />
        {/* 플랫폼 설정은 관리자만 연다. 바꿀 수 없는 화면을 열어 두면 Save 를 눌러서야 403 을 만난다 */}
        <Route
          path="admin/settings"
          element={
            <PlatformAdminOnly>
              <PlatformSettingsPage />
            </PlatformAdminOnly>
          }
        />
        {/* 사용량과 비용 (P37). 테넌트 화면은 usage:read, 플랫폼 화면은 관리자만 연다 */}
        <Route path="usage" element={<UsagePage />} />
        <Route
          path="admin/usage"
          element={
            <PlatformAdminOnly>
              <PlatformUsagePage />
            </PlatformAdminOnly>
          }
        />
        <Route path="admin/group-mappings" element={<GroupMappingsPage />} />
        <Route path="admin/users" element={<UsersPage />} />
        <Route path="account/password" element={<PasswordPage />} />
        {/* 개인 MCP 토큰 (P32). 사람마다의 화면이라 메뉴 묶음이 아니라 계정 메뉴로 온다 */}
        <Route path="account/tokens" element={<TokensPage />} />
        {/* 기억한 승인 선택 (P43 5절). 모든 인증 모드에서 부른 사람 자신의 것이다 */}
        <Route path="account/approval-choices" element={<ApprovalChoicesPage />} />
        {/* 나를 대신하는 자동 실행 (P41 6절). 모든 인증 모드에서 부른 사람에게 묶인 소스다 */}
        <Route path="account/runs-as-me" element={<RunsAsMePage />} />
        <Route path="audit" element={<AuditPage />} />
        <Route path="audit/mcp" element={<McpAuditPage />} />
        <Route path="callback" element={<CallbackPage />} />
        <Route path="*" element={<PlaceholderPage />} />
      </Route>
    </Routes>
  );
}

/** 브라우저용 앱. 테스트는 AppRoutes 를 MemoryRouter 로 감싼다 (TestApp). */
export function App() {
  return (
    <QueryClientProvider client={makeQueryClient()}>
      <BrowserRouter basename={BASENAME}>
        <AppRoutes />
      </BrowserRouter>
    </QueryClientProvider>
  );
}

export function TestApp({ initialPath = "/", client }: { initialPath?: string; client?: QueryClient }) {
  return (
    <QueryClientProvider client={client ?? makeQueryClient()}>
      <MemoryRouter initialEntries={[initialPath]}>
        <AppRoutes />
      </MemoryRouter>
    </QueryClientProvider>
  );
}
