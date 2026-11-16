import { BookOpen, ChartColumn, Coins, ConciergeBell, Database, MessagesSquare, PanelLeftClose, PanelLeftOpen, Puzzle, ScrollText, Server, Settings, Settings2, UserCog, Users, Waypoints, Workflow, Wrench } from "lucide-react";
import { useContext, useEffect, useMemo, useState } from "react";
import { Navigate, useLocation } from "react-router";
import { Link, NavLink, Outlet } from "react-router";

import { AccountMenu } from "@/components/AccountMenu";
import { AssistantPanel } from "@/components/assistant/AssistantPanel";
import { RouteErrorBoundary } from "@/components/ErrorBoundary";
import { KubernetesIcon, McpIcon, type NavIcon, DaegamMark } from "@/components/icons/brand";
import { TenantSelector } from "@/components/TenantSelector";
import { Trail, trailFor, TrailTitleContext, type TrailEntry } from "@/components/Trail";
import { ActionMenu, type MenuItem } from "@/components/ui/action-menu";
import { Chevron } from "@/components/ui/icons";
import { type Messages, useT } from "@/i18n";
import { proposalLink, setAssistantOpen, useAssistantConfig, useAssistantOpen } from "@/lib/assistant";
import { hasScope, initAuth, loginPath, passwordPath, useAuthState } from "@/lib/auth";
import { STORAGE_KEYS, readItem, readStringList, writeItem } from "@/lib/storage";
import { useNarrowViewport } from "@/lib/media";
import { useTenant } from "@/lib/tenant";
import { cn } from "@/lib/utils";

/*
 * IA (D22·D35·D42, P81 5절): 맨 위 단독 자리는 홈 챗(`/chat`, 첫 화면)의 "새 대화" 이고, 그 아래 Tool / Agent / 관리
 * 세 묶음이다. Tool 은 MCP Hub 와 Tool 을 만드는 세 리소스, Agent 는 Agent 와 그것이 쓰는 Skill·Knowledge Base·Database,
 * 관리는 테넌트 설정과 role 전용 항목과 Clusters 다. data-testid 는 이전 구조 그대로 유지한다 (스모크·full e2e 가 쓴다).
 */
export interface NavItem {
  to: string;
  /** 화면 제목과 같은 영어 이름은 글자 그대로 두고, 화면 언어를 따르는 말은 사전에서 고르는 함수로 둔다 */
  label: string | ((t: Messages) => string);
  testId: string;
  /** 접힌 사이드바(아이콘 레일)에서 라벨 대신 보이는 아이콘. Profile 은 설계, Instance 는 실행을 뜻하는 모양으로 짝을 맞춘다 */
  icon: NavIcon;
  /** 같은 경로로 가는 항목이 화면에 할 말. 받는 쪽이 읽고 바로 지운다(홈의 "새 대화") */
  state?: Record<string, unknown>;
}

/**
 * 사이드바 묶음의 id. 접힌 묶음을 저장할 때(STORAGE_KEYS.navCollapsed)와 묶음을 견줄 때 쓴다. 보이는 이름은
 * 사전(shell.sidebar.groups)이 준다.
 */
export type NavGroupId = "Tool" | "Agent" | "Admin";

export const NAV_GROUPS: { id: NavGroupId | null; items: NavItem[] }[] = [
  {
    id: null,
    items: [
      // 첫 화면은 챗이다(D35). 이 항목은 "홈으로" 가 아니라 "새 대화" 다. 같은 경로라 클릭이 라우팅을 일으키지
      // 않으므로 state 로 화면에 알린다. 홈으로 가는 길은 브랜드 마크가 맡는다
      { to: "/chat", label: (t) => t.shared.chatPanel.newChat, testId: "nav-home-chat", icon: MessagesSquare, state: { fresh: true } },
    ],
  },
  {
    // Tool 을 고르는 허브와 Tool 을 만드는 리소스 셋. Gateway 는 vMCP 이고 외부 클라이언트도 부르므로 여기 둔다 (D42)
    id: "Tool",
    items: [
      { to: "/hub", label: "MCP Hub", testId: "nav-mcp-hub", icon: McpIcon },
      { to: "/backend-apis", label: "Backend APIs", testId: "nav-backend-apis", icon: Server },
      { to: "/mcp-servers", label: "MCP Servers", testId: "nav-mcp-servers", icon: Wrench },
      { to: "/gateways", label: "Gateways", testId: "nav-gateways", icon: Waypoints },
    ],
  },
  {
    // Agent 와 그것이 쓰는 것. Skill 은 붙이는 절차서(D64), Knowledge Base(D32)와 Database 는 Agent 의 지식과 기억이다
    id: "Agent",
    items: [
      { to: "/agents", label: "Agents", testId: "nav-agents", icon: Workflow },
      { to: "/skills", label: "Skills", testId: "nav-skills", icon: Puzzle },
      { to: "/knowledge-bases", label: "Knowledge Base", testId: "nav-knowledge-bases", icon: BookOpen },
      { to: "/data-services", label: "Database", testId: "nav-data-services", icon: Database },
    ],
  },
  {
    // 관리. 테넌트 설정 다음에 테넌트 범위의 role 항목(ROLE_NAV_ITEMS 중 platformOnly 가 아닌 것)이 끼고, 그 뒤가
    // Clusters, 구분선 아래가 플랫폼 관리자 항목이다 (adminEntries)
    id: "Admin",
    items: [
      { to: "/tenant/settings", label: "Tenant Settings", testId: "nav-settings", icon: Settings2 },
      { to: "/clusters", label: "Clusters", testId: "nav-clusters", icon: KubernetesIcon },
    ],
  },
];

/** role 전용 항목이 붙는 묶음과 접을 수 있는 묶음 */
const ROLE_ITEMS_GROUP: NavGroupId = "Admin";
const COLLAPSIBLE_GROUPS = new Set<NavGroupId>(["Admin"]);

/** 항목의 보이는 이름. 사전에서 고르는 항목은 지금 언어의 말이다. */
export function navLabel(t: Messages, item: NavItem): string {
  return typeof item.label === "string" ? item.label : item.label(t);
}

/** 평평한 목록 (테스트가 항목 존재를 검사할 때 쓴다). */
export const NAV_ITEMS: NavItem[] = NAV_GROUPS.flatMap((g) => g.items);

type RoleNavItem = NavItem & { scope: string; platformOnly?: boolean; localOnly?: boolean };

/** role 에 따라 관리 묶음에 조건부로 붙는 항목. scope 는 GET /me 기준.
 * Settings(/tenant/settings) 진입점은 위 정규 항목 하나뿐이고, members:write 여부는 페이지 안에서 갈린다. */
export const ROLE_NAV_ITEMS: RoleNavItem[] = [
  { to: "/audit", label: "Audit", testId: "nav-audit", scope: "audit:read", icon: ScrollText },
  // 사용량과 비용 (P37). 테넌트 화면은 tenant-admin 이상, 플랫폼 화면은 관리자다
  { to: "/usage", label: "Usage", testId: "nav-usage", scope: "usage:read", icon: Coins },
  { to: "/admin/usage", label: "Platform Usage", testId: "nav-admin-usage", scope: "settings:write", platformOnly: true, icon: ChartColumn },
  { to: "/admin/settings", label: "Platform Settings", testId: "nav-admin-settings", scope: "settings:write", platformOnly: true, icon: Settings },
  { to: "/admin/group-mappings", label: "Group Mappings", testId: "nav-admin-group-mappings", scope: "settings:write", platformOnly: true, icon: UserCog },
  // local 인증 모드(D40)에만 있다. oidc 사용자는 IdP 가 관리한다
  { to: "/admin/users", label: "Users", testId: "nav-admin-users", scope: "users:write", platformOnly: true, localOnly: true, icon: Users },
];

/**
 * 경로의 메뉴 그림. 메뉴 밖에서 같은 갈래를 가리키는 곳(홈 첫 화면의 빠른 시작)이 이것으로 그림을 받아,
 * 메뉴의 그림이 바뀌어도 둘이 갈라지지 않는다. 메뉴에 없는 경로는 만든 사람의 실수라 바로 터뜨린다.
 */
export function navIcon(path: string): NavIcon {
  const item = [...NAV_ITEMS, ...ROLE_NAV_ITEMS].find((i) => i.to === path);
  if (!item) throw new Error(`no nav item for ${path}`);
  return item.icon;
}

/*
 * nav 항목: 32px, radius 7, 13.5px. 사이드바는 한지 바탕에 바로 서므로 활성 항목은 흰 면에 머리카락 선과 그림자를 두른
 * 먹 글자다 (P81 5절). 선은 ring 으로 그려 활성이 바뀌어도 글자가 밀리지 않는다.
 */
export function navClass({ isActive }: { isActive: boolean }, rail = false) {
  return cn(
    "flex shrink-0 items-center gap-2.5 rounded-item text-[13.5px] font-medium text-fg transition-colors hover:bg-hover hover:text-heading [&>svg]:text-muted",
    rail ? "size-10 justify-center px-0" : "h-8 px-2.5",
    isActive && "bg-surface font-semibold text-heading shadow-hairline ring-1 ring-line hover:bg-surface [&>svg]:text-heading",
  );
}

/** nav 항목. 펼친 사이드바는 아이콘 + 라벨, 접힌 레일은 아이콘만 두고 라벨을 title 로 보인다. */
function NavItemLink({ item, rail, end }: { item: NavItem; rail: boolean; end?: boolean }) {
  const t = useT();
  const Icon = item.icon;
  const label = navLabel(t, item);
  return (
    <NavLink to={item.to} state={item.state} end={end} data-testid={item.testId} title={rail ? label : undefined} aria-label={rail ? label : undefined} className={(s) => navClass(s, rail)}>
      <Icon size={16} className="shrink-0" aria-hidden />
      {rail ? null : <span className="truncate">{label}</span>}
    </NavLink>
  );
}

/* 셸의 꼴 (P81 5절). 본문 패널은 창에서 8px 들어온 흰 면이고, 머리 줄은 52px 다 */
const SKIP_LINK =
  "sr-only rounded-item bg-surface px-3 py-2 text-sm font-medium text-heading shadow-popover outline-none focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:ring-2 focus:ring-focus-ring";
const SIDEBAR = "flex shrink-0 flex-col py-3 transition-[width] duration-200";
const BRAND_LINK = "flex min-w-0 items-center rounded-item focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none";
const WORDMARK = "min-w-0 truncate font-display text-[17px] font-bold tracking-[-0.02em] text-heading";
const SIDEBAR_TOGGLE =
  "flex size-7 shrink-0 items-center justify-center rounded-item text-muted hover:bg-hover hover:text-heading focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring";
const MAIN_PANEL = "my-2 mr-2 flex min-w-0 flex-1 flex-col overflow-hidden rounded-panel border border-line bg-surface shadow-hairline";
const TOP_BAR = "flex h-[52px] shrink-0 items-center gap-3 border-b border-line pl-[22px] pr-3.5";
/* 본문의 여백. 홈 챗은 대화 목록 열이 패널 가장자리에 붙어야 해서 여백 없이 둔다 (P81 6절) */
const MAIN = "flex min-h-0 flex-1 flex-col overflow-y-auto outline-none";
const MAIN_PAD = "page-pad py-7";
const ASSISTANT_TOGGLE =
  "flex h-8 items-center gap-1.5 rounded-chip px-2.5 text-[13px] font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring";

/** 묶음 제목. 대문자로 펴지 않은 12px 글자다. 한글 제목(관리)과 영어 제목(Tool)이 같은 무게로 읽혀야 해서다 */
const GROUP_LABEL = "flex w-full items-center justify-between px-2.5 pb-1.5 pt-[18px] text-left text-xs font-semibold text-muted";

function GroupLabel({ children }: { children: React.ReactNode }) {
  return <div className={GROUP_LABEL}>{children}</div>;
}

/**
 * 캔버스가 화면을 다 쓰는 자리인가. `/agents/new` 와 상세의 정의 탭이다.
 *
 * 저장된 Agent 의 캔버스는 상세의 정의 탭 안에 있다 (D42). 상세를 여는 것만으로 접으면 캔버스를 보려던
 * 것이 아닌 사람까지 접힌 화면을 받으므로 경로만으로는 판단하지 않는다. 정의 탭을 고르는 것은 캔버스를
 * 보겠다는 뜻이라 그때만 든다.
 */
export function isCanvasPath(pathname: string, search = ""): boolean {
  if (pathname === "/agents/new") return true;
  if (!/^\/agents\/[^/]+$/.test(pathname)) return false;
  return new URLSearchParams(search).get("tab") === "definition";
}

/** 홈 챗인가. 여기서는 청지기가 화면 안에 있어 오른쪽 패널과 토글을 감춘다(D35). */
export function isHomeChatPath(pathname: string): boolean {
  return pathname === "/chat";
}

/* 사이드바 전체를 왼쪽으로 접는 상태(접히면 아이콘 레일 w-16)와 접힌 묶음. 둘 다 lib/storage 에 남는다. */
function readSidebarCollapsed(): boolean {
  return readItem(STORAGE_KEYS.sidebar) === "collapsed";
}
function writeSidebarCollapsed(collapsed: boolean): void {
  writeItem(STORAGE_KEYS.sidebar, collapsed ? "collapsed" : "expanded");
}
function readCollapsed(): Set<string> {
  return new Set(readStringList(STORAGE_KEYS.navCollapsed));
}
/** 접을 수 있는 묶음만 적는다. */
function writeCollapsed(set: Set<string>): void {
  writeItem(STORAGE_KEYS.navCollapsed, JSON.stringify([...set].filter((id) => COLLAPSIBLE_GROUPS.has(id as NavGroupId))));
}
function CollapsibleGroupLabel({ id, label, open, onToggle }: { id: NavGroupId; label: string; open: boolean; onToggle: () => void }) {
  return (
    <button type="button" aria-expanded={open} data-testid={`nav-group-${id.toLowerCase()}`} className={cn(GROUP_LABEL, "rounded-item hover:text-fg")} onClick={onToggle}>
      <span>{label}</span>
      <Chevron open={open} />
    </button>
  );
}

/**
 * 관리 묶음의 차례 (P81 5절). 테넌트 설정, 테넌트 범위의 role 항목(Audit, Usage), 나머지 정규 항목(Clusters)이 한 덩어리이고,
 * 플랫폼 관리자 항목은 구분선 아래 따로다. role 항목은 보이는 것만 받는다.
 */
function adminEntries(items: NavItem[], visibleRoleItems: RoleNavItem[]) {
  return {
    head: items.slice(0, 1),
    tenantRole: visibleRoleItems.filter((i) => !i.platformOnly),
    tail: items.slice(1),
    platform: visibleRoleItems.filter((i) => i.platformOnly),
  };
}

/**
 * 어떤 화면에 들어가면 열린 것을 닫고, 나가면 다시 연다. 사이드바 레일과 오른쪽 청지기 패널이 같이 쓴다.
 *
 * 우리가 닫은 것만 되돌린다. 원래 닫혀 있었으면 나갈 때도 그대로 두고, 닫힌 동안 사용자가 직접 열었으면
 * 그 선택을 존중해 다시 닫지도 되돌리지도 않는다. `close`·`reopen` 은 저장하지 않는 변경이어야 한다.
 */
function useAutoClose(active: boolean, isOpen: boolean, close: () => void, reopen: () => void): void {
  const [closedByUs, setClosedByUs] = useState(false);
  useEffect(() => {
    if (active) {
      if (!closedByUs && isOpen) {
        setClosedByUs(true);
        close();
      }
    } else if (closedByUs) {
      setClosedByUs(false);
      if (!isOpen) reopen();
    }
    // isOpen 은 판정 재료일 뿐 그 변화가 이 효과를 다시 돌게 해서는 안 된다(사용자가 연 것을 되닫지 않으려는 것이다)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);
}

export function Layout() {
  const t = useT();
  const text = t.shell.sidebar;
  const auth = useAuthState();
  const tenant = useTenant();
  useEffect(() => {
    void initAuth();
  }, []);

  const visibleRoleItems = ROLE_NAV_ITEMS.filter((item) => {
    if (item.localOnly && auth.mode !== "local") return false;
    if (item.platformOnly) return auth.me?.is_platform_admin ?? false;
    return hasScope(auth.me, tenant, item.scope);
  });

  const location = useLocation();
  const [pageTitle, setPageTitle] = useState<string | null>(null);
  // 위치 표시가 견줄 메뉴 항목. role 항목은 보이든 말든 다 넣는다. 화면이 그 경로에 있으면 그 묶음의 것이다
  const trailEntries = useMemo<TrailEntry[]>(
    () => [
      ...NAV_GROUPS.flatMap((g) => g.items.map((item) => ({ item, group: g.id }))),
      ...ROLE_NAV_ITEMS.map((item) => ({ item, group: ROLE_ITEMS_GROUP as NavGroupId | null })),
    ].map(({ item, group }) => ({ to: item.to, label: navLabel(t, item), group: group ? text.groups[group] : null })),
    [t, text],
  );
  const [collapsed, setCollapsed] = useState<Set<string>>(() => readCollapsed());
  const [rail, setRail] = useState<boolean>(() => readSidebarCollapsed());
  // 좁은 창에서는 저장된 취향과 상관없이 레일로 접는다. 저장하지 않으므로 넓히면 되돌아온다.
  const narrow = useNarrowViewport();
  const railed = rail || narrow;
  const toggleRail = () => {
    setRail((prev) => {
      writeSidebarCollapsed(!prev);
      return !prev;
    });
  };
  // 캔버스(/agents/new)와 홈 챗에 들어가면 자동으로 접고, 나가면 들어오기 전 상태로 돌린다. 둘 다 본문이
  // 화면을 넓게 써야 하는 자리다. 자동으로 접은 동안 사용자가 직접 펼치면 그 선택을 존중해 되돌리지 않는다.
  // 자동 접힘은 저장하지 않는다.
  const onCanvas = isCanvasPath(location.pathname, location.search);
  // 홈 챗에서는 청지기가 화면 안에 있다. 같은 상대가 한 화면에 둘이면 헷갈리므로 패널도 토글도 감춘다(D35)
  const onHomeChat = isHomeChatPath(location.pathname);
  const autoRail = onCanvas || onHomeChat;
  useAutoClose(autoRail, !rail, () => setRail(true), () => setRail(false));
  // 오른쪽 청지기 패널(D31). 설정이 꺼져 있거나 읽지 못하면 패널도 토글도 없다.
  // 캔버스에서는 사이드바와 같은 규칙으로 자동으로 접고 나가면 되돌리며, 자동 접힘은 저장하지 않는다.
  // 청지기의 제안을 연 캔버스는 접지 않는다. 사람이 캔버스와 그 대화를 함께 보며 다듬는 자리다 (P48 6.5절)
  const assistant = useAssistantConfig();
  const assistantOpen = useAssistantOpen();
  const hidesAssistant = (onCanvas && !proposalLink(location.search)) || onHomeChat;
  useAutoClose(hidesAssistant, assistantOpen, () => setAssistantOpen(false, false), () => setAssistantOpen(true, false));
  const toggleGroup = (id: NavGroupId) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      writeCollapsed(next);
      return next;
    });
  };
  // 접힌 묶음 안의 화면으로 이동하면 그 묶음을 펼쳐 현재 위치가 보이게 한다
  const isInGroup = (id: NavGroupId) => {
    const group = NAV_GROUPS.find((g) => g.id === id);
    const paths = [...(group?.items ?? []), ...(id === ROLE_ITEMS_GROUP ? visibleRoleItems : [])].map((i) => i.to);
    return paths.some((p) => location.pathname === p || location.pathname.startsWith(p + "/"));
  };
  // 묶음의 항목이 보이는가. 접을 수 없는 묶음은 늘 보이고, 접힌 묶음도 지금 위치가 그 안이면 보인다
  const groupOpen = (id: NavGroupId) => !COLLAPSIBLE_GROUPS.has(id) || !collapsed.has(id) || isInGroup(id);

  // local 인증 모드(D40): 토큰이 없으면 로그인으로, 비밀번호를 바꿔야 하면 그 화면으로 먼저 보낸다.
  // 규칙은 서버가 지킨다(401·403 password_change_required). 여기는 그 결과를 화면으로 옮기는 자리다
  if (auth.mode === "local" && !auth.loading) {
    const back = location.pathname + location.search;
    if (!auth.token) return <Navigate to={loginPath(back)} replace />;
    if (auth.mustChangePassword && location.pathname !== "/account/password") {
      return <Navigate to={passwordPath(back)} replace />;
    }
  }

  const navItems = (items: NavItem[]) =>
    items.map((item) => <NavItemLink key={item.testId} item={item} rail={railed} end={item.to === "/tenant/settings"} />);

  return (
    // 껍데기가 뷰포트 높이를 그대로 쓴다. 스크롤은 사이드바 nav 와 main 각자가 한다.
    // 첫 화면(홈 챗)이 세로로 꽉 차야 하는데 min-h-screen 이면 카드가 화면 밖으로 밀려 입력칸이 잘린다(실측).
    <div className="flex h-screen">
      {/*
        본문 건너뛰기. 키보드로 오는 사람은 화면마다 사이드바 20여 개를 지나야 본문에 닿는다.
        평소에는 화면 밖에 있다가 포커스를 받으면 왼쪽 위에 나타난다(`sr-only focus:not-sr-only`).
        DOM 의 첫 자리여야 Tab 한 번에 닿으므로 사이드바보다 앞에 둔다.
      */}
      <a href="#main" data-testid="skip-to-main" className={SKIP_LINK}>
        {text.skipToMain}
      </a>
      {/* 사이드바는 한지 바탕에 바로 선다. 면을 두르지 않아 흰 본문 패널이 화면의 중심으로 읽힌다 (P81 5절) */}
      <aside
        className={cn(SIDEBAR, railed ? "w-16 items-center px-3" : "w-[248px] px-2.5")}
        data-testid="sidebar"
        data-collapsed={railed ? "true" : "false"}
      >
        <div className={cn("flex items-center pb-3", railed ? "flex-col gap-2" : "gap-2.5 px-1.5 pt-1")}>
          {/* 마크와 워드마크가 홈으로 가는 길이다. nav 의 같은 자리는 "새 대화" 라 홈으로 가는 항목이 따로 없다 */}
          <Link
            to="/chat"
            data-testid="brand-home"
            aria-label={text.home}
            title={text.homeTitle}
            className={cn(BRAND_LINK, !railed && "flex-1 gap-2.5")}
          >
            <DaegamMark size={28} className="shrink-0" data-testid="brand-mark" aria-hidden />
            {railed ? null : (
              <span className={WORDMARK} data-testid="brand-wordmark">
                {t.shell.names.product}
              </span>
            )}
          </Link>
          {/* 좁은 창에서는 펼칠 자리가 없어 토글이 아무 일도 못 한다. 그때는 버튼을 두지 않는다. */}
          {narrow ? null : (
            <button
              type="button"
              data-testid="sidebar-toggle"
              aria-label={railed ? text.expand : text.collapse}
              aria-expanded={!railed}
              title={railed ? text.expand : text.collapse}
              className={SIDEBAR_TOGGLE}
              onClick={toggleRail}
            >
              {railed ? <PanelLeftOpen size={16} aria-hidden /> : <PanelLeftClose size={16} aria-hidden />}
            </button>
          )}
        </div>
        <TenantSelector rail={railed} />
        <nav className={cn("mt-3.5 flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto", railed && "items-center")}>
          {NAV_GROUPS.map((group) => {
            const open = group.id ? groupOpen(group.id) : true;
            const admin = group.id === ROLE_ITEMS_GROUP ? adminEntries(group.items, visibleRoleItems) : null;
            return (
              <div key={group.id ?? "home"} className={cn("flex flex-col gap-0.5", railed && "items-center")}>
                {railed ? (
                  group.id ? <div className="my-1.5 h-px w-8 bg-line" aria-hidden /> : null
                ) : group.id && COLLAPSIBLE_GROUPS.has(group.id) ? (
                  <CollapsibleGroupLabel id={group.id} label={text.groups[group.id]} open={open} onToggle={() => toggleGroup(group.id!)} />
                ) : group.id ? (
                  <GroupLabel>{text.groups[group.id]}</GroupLabel>
                ) : null}
                {!railed && !open ? null : admin ? (
                  <>
                    {navItems(admin.head)}
                    {admin.tenantRole.length ? (
                      <div className="contents" data-testid="role-nav">
                        {navItems(admin.tenantRole)}
                      </div>
                    ) : null}
                    {navItems(admin.tail)}
                    {admin.platform.length ? (
                      <div className={cn("flex flex-col gap-0.5", railed && "items-center")} data-testid="role-nav-platform">
                        <div className={cn("my-1.5 h-px bg-line", railed ? "w-8" : "mx-2.5")} aria-hidden />
                        {navItems(admin.platform)}
                      </div>
                    ) : null}
                  </>
                ) : (
                  navItems(group.items)
                )}
              </div>
            );
          })}
        </nav>
        <div className="mt-2 w-full border-t border-line pt-2">
          <AccountMenu rail={railed} />
        </div>
      </aside>
      {/* 본문 패널. 창에서 8px 들어와 흰 면 위에 머리 줄과 화면이 선다. 스크롤은 패널 안의 main 이 한다 (P81 5절) */}
      <div className={MAIN_PANEL}>
        <header className={TOP_BAR} data-testid="topbar">
          <Trail crumbs={trailFor(trailEntries, location.pathname, pageTitle)} />
          <div className="ml-auto flex shrink-0 items-center gap-2">
            {assistant.enabled && !onHomeChat ? <AssistantToggle open={assistantOpen} onToggle={() => setAssistantOpen(!assistantOpen)} /> : null}
          </div>
        </header>
        {/* 세로로 꽉 차는 화면(홈 챗)이 flex-1 로 남은 높이를 가져갈 수 있게 열 flex 다.
            보통 화면은 자식이 하나뿐이라 지금과 같이 내용만큼만 차지한다 */}
        {/* 건너뛰기 링크의 목적지다. tabIndex=-1 이라야 링크로 왔을 때 포커스가 실제로 옮겨 간다 */}
        {/* 여백은 여기가 주고 폭 상한은 페이지가 `PageWidth` 로 쥔다(D44 첫째). 안쪽에 래퍼를 끼우면 홈 챗에
            남은 높이를 넘기는 사슬이 끊겨 입력칸이 화면 밖으로 밀린다(D35) */}
        {/* 본문만 감싼다. 화면 하나가 그리다 터져도 사이드바와 머리 줄은 남아 다른 곳으로 갈 수 있다 */}
        <main id="main" tabIndex={-1} className={cn(MAIN, !onHomeChat && MAIN_PAD)}>
          <TrailTitleContext.Provider value={setPageTitle}>
            <RouteErrorBoundary>
              <Outlet />
            </RouteErrorBoundary>
          </TrailTitleContext.Provider>
        </main>
      </div>
      {assistant.enabled && assistantOpen && !onHomeChat ? <AssistantPanel onClose={() => setAssistantOpen(false)} /> : null}
    </div>
  );
}

/** 머리 줄의 청지기 패널 토글. 비서의 표시인 컨시어지 종과 이름이다. 설정이 꺼져 있으면 아예 그리지 않는다. */
function AssistantToggle({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  const t = useT();
  const text = t.shell.assistantToggle;
  return (
    <button
      type="button"
      data-testid="assistant-toggle"
      aria-expanded={open}
      aria-controls="assistant-panel"
      aria-label={open ? text.close : text.open}
      title={open ? text.closeTitle : text.openTitle}
      className={cn(ASSISTANT_TOGGLE, open ? "bg-hover text-heading" : "text-muted hover:bg-hover hover:text-heading")}
      onClick={onToggle}
    >
      <ConciergeBell size={16} aria-hidden />
      <span>{t.shell.names.assistant}</span>
    </button>
  );
}

/**
 * 화면의 머리 (D24·D44 둘째).
 *
 * `title` 은 표시 이름이고 `name` 을 주면 K8s 이름을 고정폭 보조로 그 아래에 보인다. 상태 배지는 이름 옆
 * (`badges`)이고 오른쪽에는 동작이 선다. 오른쪽 규칙이 D44 다. 주 동작(`primary`)은 먹 버튼 하나,
 * 그 옆이 두 번째 동작(`secondary`, 상세 넷에서는 편집), 나머지는 `···` 가 여는 메뉴(`menu`)다.
 *
 * 버튼 변형도 자리마다 정해져 있다. 주 동작은 `default`(먹 바탕), 리소스에 무언가를 하는 보조 동작은
 * `secondary`(흰 면 + 테두리 + 머리카락 그림자), 멈추거나 지우는 것은 `danger`(붉은 글자)이고 붉은 면의
 * `destructive` 는 지우기 확인 창의 확정 버튼만 쓴다. `outline` 은 그림자가 없어 표 칸·카드 안에 쓴다
 * (P81 4절).
 *
 * `actions` 는 아직 옮기지 않은 화면(허브·클러스터·Database·Knowledge Base 상세, 폼 몇)의 자리다. 새 화면은
 * 쓰지 않는다.
 */
export function PageHeader({
  title,
  name,
  subtitle,
  subtitleTestId,
  badges,
  actions,
  primary,
  secondary,
  menu,
  testId,
}: {
  /** 보통은 글자다. 제자리에서 이름을 고치는 화면은 입력칸을 그대로 넘긴다 (input 은 phrasing content 라 h1 안에 들어간다). */
  title: React.ReactNode;
  name?: string | null;
  /** 제목 아래 한 줄. 링크가 든 안내도 받는다(설정 화면의 범위 안내). */
  subtitle?: React.ReactNode;
  subtitleTestId?: string;
  badges?: React.ReactNode;
  actions?: React.ReactNode;
  primary?: React.ReactNode;
  secondary?: React.ReactNode;
  menu?: MenuItem[];
  testId?: string;
}) {
  const t = useT();
  const right = actions || primary || secondary || menu?.length;
  // 머리 줄의 위치 표시에 이 화면의 이름을 알린다. 제자리에서 이름을 고치는 화면은 제목이 입력칸이라 name 을 쓴다
  const setTrailTitle = useContext(TrailTitleContext);
  const trailTitle = typeof title === "string" ? title : (name ?? null);
  useEffect(() => {
    setTrailTitle(trailTitle);
    return () => setTrailTitle(null);
  }, [setTrailTitle, trailTitle]);
  return (
    <div className="mb-5 flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
      <div className="flex min-w-0 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2.5">
          {/* 대화상자가 닫힐 때 여는 버튼이 사라졌으면 포커스가 여기로 온다 (components/ui/focus.ts) */}
          {/* 페이지 제목은 24px/700 이고 자간을 조금만 좁힌다. 한글은 라틴처럼 조이면 획이 붙는다 (P81 2절) */}
          <h1 data-page-heading className="text-2xl leading-tight font-bold tracking-[-0.015em] text-heading" data-testid={testId}>
            {title}
          </h1>
          {badges}
        </div>
        {name ? (
          <code className="font-mono text-xs text-muted" data-testid="page-name">
            {name}
          </code>
        ) : null}
        {subtitle ? (
          <p className="text-[13.5px] text-muted" data-testid={subtitleTestId}>
            {subtitle}
          </p>
        ) : null}
      </div>
      {/* ml-auto 는 줄바꿈이 일어났을 때를 위한 것이다. 제목이 길거나 배지가 많으면 동작 줄이
          아래로 내려가는데, justify-between 은 그 줄에 항목이 하나뿐이라 왼쪽으로 붙여 버린다.
          그러면 같은 버튼이 화면마다 오른쪽 끝에 있다가 왼쪽 끝에 있다가 한다 */}
      {/* 동작 줄은 오른쪽 끝에 서고 주 동작이 맨 끝이다. 눈이 줄의 끝에서 멈추므로 하나뿐인 먹 단추가
          거기 있어야 다른 단추와 다투지 않는다 (P81 6절) */}
      {right ? (
        <div className="ml-auto flex items-center gap-2">
          {actions}
          {secondary}
          {/* title 은 ReactNode 라 문자열로 끼우면 "[object Object] 동작" 이 된다. 제자리에서 이름을
              고치는 화면이 입력칸을 제목으로 넘기기 때문이다. 글자가 아니면 name 으로 떨어진다 */}
          {menu?.length ? <ActionMenu items={menu} label={t.ui.cardGrid.menu(typeof title === "string" ? title : (name ?? t.shell.pageHeader.thisResource))} /> : null}
          {primary}
        </div>
      ) : null}
    </div>
  );
}
