import { Bookmark, CalendarClock, ChevronsUpDown, KeyRound, LockKeyhole, LogIn, LogOut, Moon, Sun } from "lucide-react";
import { useEffect, useId, useRef, useState, type FocusEvent } from "react";
import { NavLink, useLocation } from "react-router";

import { LanguageSwitch } from "@/components/LanguageSwitch";
import { PlatformApiDocsButton } from "@/components/PlatformApiDocs";
import { Button } from "@/components/ui/button";
import { MENU_ROW } from "@/components/ui/menu-row";
import { hasModalAbove, POPOVER_PANEL_CLASS, usePopover } from "@/components/ui/usePopover";
import { VersionBadge } from "@/components/VersionBadge";
import { useT } from "@/i18n";
import { login, logout, tokenAuth, useAuthState } from "@/lib/auth";
import { useRunsEnabled } from "@/lib/runs";
import { toggleTheme, useTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";

/*
 * 사이드바 맨 아래의 계정 줄과 계정 메뉴 (P81 5절). 계정 화면으로 가는 링크, 테마, 언어, API 문서, 판 표시, 로그아웃이
 * 한 메뉴에 있다.
 *
 * 메뉴는 닫혀도 DOM 에 남고 `hidden` 으로만 감춘다. API 문서 단추가 자기 대화상자를 품고 있어서, 메뉴가 닫히며 단추째
 * 사라지면 대화상자도 같이 사라지기 때문이다. 자리 잡기와 바깥 누름은 다른 떠 있는 패널과 같이 `usePopover` 가 맡고,
 * 그 대화상자가 위에 떠 있는 동안의 누름과 포커스 이동은 바깥으로 치지 않는다(`hasModalAbove`).
 */

/**
 * 이름의 머리글자. 전자 우편 주소는 @ 앞만 본다. 구분 기호(공백 . _ -)로 나눈 앞 두 조각의 첫 글자이고, 나뉘지 않는
 * 이름(한글 이름)은 첫 글자 하나다. 이름이 비면 물음표다.
 */
export function initials(name: string): string {
  const parts = name.split("@")[0].split(/[\s._-]+/).filter(Boolean);
  if (!parts.length) return "?";
  const letters = parts.slice(0, 2).map((p) => Array.from(p)[0]);
  return letters.join("").toUpperCase();
}

/** 계정 메뉴의 테마 줄. 지금 테마를 아이콘과 이름으로 보이고, 누르면 반대 테마로 바꾼다. */
function ThemeToggle() {
  const text = useT().shell.theme;
  const theme = useTheme();
  const Icon = theme === "dark" ? Moon : Sun;
  return (
    <button type="button" data-testid="theme-toggle" aria-label={theme === "dark" ? text.toLight : text.toDark} className={cn(MENU_ROW, "justify-between")} onClick={toggleTheme}>
      <span>{text.label}</span>
      <span className="inline-flex items-center gap-1.5 rounded-item border border-line bg-sunken px-2 py-0.5 text-xs font-semibold text-heading">
        <Icon size={13} aria-hidden />
        <span data-testid="theme-current">{theme === "dark" ? text.dark : text.light}</span>
      </span>
    </button>
  );
}

/** 기억한 승인 선택 화면 (P43 5절). 어느 인증 모드에서도 기억할 수 있으므로 계정 메뉴마다 둔다. */
function ApprovalChoicesLink() {
  const t = useT();
  return (
    <NavLink to="/account/approval-choices" className={MENU_ROW} data-testid="approval-choices-link">
      <Bookmark size={16} aria-hidden />
      {t.account.menu.approvalChoices}
    </NavLink>
  );
}

/** 나를 대신하는 자동 실행 화면 (P41 6절). 자동 실행이 꺼진 플랫폼에서는 서지 않는다. */
function RunsAsMeLink() {
  const t = useT();
  if (!useRunsEnabled()) return null;
  return (
    <NavLink to="/account/runs-as-me" className={MENU_ROW} data-testid="runs-as-me-link">
      <CalendarClock size={16} aria-hidden />
      {t.account.runsAsMe.title}
    </NavLink>
  );
}

const DIVIDER = <div className="mx-0.5 my-1.5 h-px bg-line" aria-hidden />;

/** 계정 줄. 펼친 사이드바에서는 아바타·이름·인증 모드, 레일에서는 아바타 하나다. */
const ACCOUNT_ROW =
  "flex items-center rounded-chip text-left transition-colors hover:bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring";
const AVATAR = "flex size-7 shrink-0 items-center justify-center rounded-full bg-sunken text-[11.5px] font-bold text-heading ring-1 ring-line";

/** 인증 모드마다 계정 줄에 보일 사람. 로그인하지 않은 oidc 는 null 이다. */
function usePerson(): { name: string; sub: string | null; testId?: string } | null {
  const t = useT();
  const text = t.account.menu;
  const auth = useAuthState();
  const admin = auth.me?.is_platform_admin ? "platform-admin" : null;
  if (auth.mode === "local") {
    // 이름은 /me 의 name 이다. 서버가 토큰의 name 을, 없으면 preferred_username 을 채워 주므로 여기서 토큰을 다시 풀지
    // 않는다(브라우저에서 atob 로 풀면 한글 이름이 깨진다)
    const name = auth.me?.name ?? auth.me?.email ?? "";
    const email = auth.me?.email && auth.me.email !== name ? auth.me.email : null;
    return { name, sub: admin ?? email };
  }
  if (auth.mode === "oidc") {
    if (!auth.token) return null;
    return { name: auth.me?.email ?? auth.me?.sub ?? text.signedIn, sub: admin };
  }
  return { name: auth.me?.sub ?? "", sub: text.headerMode(null), testId: "login-placeholder" };
}

export function AccountMenu({ rail = false }: { rail?: boolean }) {
  const t = useT();
  const text = t.account.menu;
  const auth = useAuthState();
  const person = usePerson();
  const location = useLocation();
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const panelId = useId();
  const errorId = useId();
  const { style } = usePopover({ open, triggerRef, panelRef, onClose: () => setOpen(false), align: "start" });

  // 메뉴의 링크를 눌러 화면이 바뀌면 닫는다
  useEffect(() => setOpen(false), [location.pathname]);
  // Escape 는 문서에서 받는다. 단추를 눌러 연 뒤 포커스가 단추에 없을 수도 있어서다(사파리). 위에 뜬 대화상자의
  // Escape 는 그 대화상자만 닫는다
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || hasModalAbove(triggerRef.current)) return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);
  // 포커스가 줄과 메뉴 밖으로 나가면 닫는다. 창을 떠나는 것(relatedTarget 없음)과 위에 뜬 대화상자로 가는 것은 뺀다
  const onBlur = (e: FocusEvent<HTMLDivElement>) => {
    const next = e.relatedTarget;
    if (!(next instanceof Node) || e.currentTarget.contains(next) || hasModalAbove(triggerRef.current)) return;
    setOpen(false);
  };

  const signedIn = auth.mode === "local" || (auth.mode === "oidc" && Boolean(auth.token));
  const name = person?.name ?? text.signedOut;

  return (
    <div className={cn("flex flex-col gap-1.5", rail && "items-center")} data-testid={signedIn ? "auth-user" : undefined} onBlur={onBlur}>
      {person === null ? (
        // 로그인하지 않은 oidc. 로그인 단추는 메뉴 안에 숨기지 않는다. 레일에서는 실패 까닭을 단추의 설명과 title 로 둔다
        rail ? (
          <>
            <Button
              size="sm"
              className="size-10 px-0"
              data-testid="login-button"
              aria-label={text.login}
              aria-describedby={auth.error ? errorId : undefined}
              title={auth.error ?? text.login}
              onClick={() => void login()}
            >
              <LogIn size={16} aria-hidden />
            </Button>
            {auth.error ? (
              <span id={errorId} className="sr-only">
                {auth.error}
              </span>
            ) : null}
          </>
        ) : (
          <div className="flex flex-col gap-1.5 px-1">
            <p className={cn("text-xs", auth.error ? "text-err-fg" : "text-muted")}>{auth.error ?? text.signedOut}</p>
            <Button size="sm" data-testid="login-button" onClick={() => void login()}>
              <LogIn size={14} aria-hidden />
              {text.login}
            </Button>
          </div>
        )
      ) : null}
      <button
        ref={triggerRef}
        type="button"
        data-testid="account-menu"
        aria-expanded={open}
        aria-controls={panelId}
        title={rail ? name : undefined}
        className={cn(ACCOUNT_ROW, rail ? "size-10 justify-center" : "w-full gap-2.5 px-2 py-1.5", open && "bg-hover")}
        onClick={() => setOpen((v) => !v)}
      >
        <span className={AVATAR} aria-hidden>
          {initials(person?.name ?? "")}
        </span>
        {rail ? (
          // 레일에서는 이름과 인증 모드가 낭독용 글로만 남는다. header 모드의 test id 도 여기 있다
          <span className="sr-only" data-testid={person?.testId}>
            {person?.sub ? `${name} · ${person.sub}` : name}
          </span>
        ) : (
          <>
            <span className="flex min-w-0 flex-1 flex-col leading-tight" data-testid={person?.testId}>
              <span className="truncate text-[13px] font-semibold text-heading" data-testid={auth.mode === "local" ? "auth-user-name" : undefined}>
                {name}
              </span>
              {person?.sub ? <span className="truncate text-xs text-muted">{person.sub}</span> : null}
            </span>
            <ChevronsUpDown size={14} className="shrink-0 text-muted" aria-hidden />
          </>
        )}
      </button>
      <div ref={panelRef} id={panelId} hidden={!open} data-testid="account-menu-panel" style={style} className={cn(POPOVER_PANEL_CLASS, "flex w-[264px] flex-col p-1.5")}>
        {/* 개인 MCP 토큰은 플랫폼이 사람 토큰을 내는 모드(local, oidc)에 있고(P53b 6.5절), 비밀번호는 local 모드에만 있다 */}
        {tokenAuth(auth.mode) ? (
          <NavLink to="/account/tokens" className={MENU_ROW} data-testid="tokens-link">
            <KeyRound size={16} aria-hidden />
            {text.tokens}
          </NavLink>
        ) : null}
        {auth.mode === "local" ? (
          <NavLink to="/account/password" className={MENU_ROW} data-testid="password-link">
            <LockKeyhole size={16} aria-hidden />
            {text.password}
          </NavLink>
        ) : null}
        {person !== null ? (
          <>
            <ApprovalChoicesLink />
            <RunsAsMeLink />
          </>
        ) : null}
        <PlatformApiDocsButton />
        {DIVIDER}
        <ThemeToggle />
        <LanguageSwitch />
        {DIVIDER}
        <div className="px-2 py-1 font-mono [&>div]:text-[11.5px] [&>div]:text-faint">
          <VersionBadge />
        </div>
        {signedIn ? (
          <>
            {DIVIDER}
            <button type="button" className={MENU_ROW} data-testid="logout-button" onClick={() => void logout()}>
              <LogOut size={16} aria-hidden />
              {text.logout}
            </button>
          </>
        ) : null}
      </div>
    </div>
  );
}
