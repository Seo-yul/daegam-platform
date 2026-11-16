import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronsUpDown, Plus } from "lucide-react";
import { useRef, useState } from "react";

import { listMyTenants } from "@/api/resources";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/input";
import { POPOVER_PANEL_CLASS, usePopover } from "@/components/ui/usePopover";
import { useT } from "@/i18n";
import { tokenAuth, useAuthState } from "@/lib/auth";
import { isValidTenant, memberTenantNames, setTenant, useRecentTenants, useTenant } from "@/lib/tenant";
import { cn } from "@/lib/utils";

/** 펼친 사이드바의 테넌트 카드와 레일의 머리글자 칸. 셀렉트 트리거의 필드 모양을 덮는다. */
const SWITCHER_CARD = "h-auto min-h-11 gap-2.5 rounded-tile border-line px-2 py-[7px] shadow-hairline hover:border-line-strong";
const SWITCHER_TILE = "size-10 justify-center rounded-chip border-line p-0 shadow-hairline hover:border-line-strong";

/** 테넌트 머리글자 칸. 쪽빛을 옅게 깔아 먹색 브랜드 마크와 겹쳐 보이지 않게 한다 (P81 5절). */
function TenantInitial({ name }: { name: string }) {
  return (
    <span className="flex size-[26px] shrink-0 items-center justify-center rounded-item bg-accent-soft text-xs font-bold text-accent" aria-hidden>
      {(name[0] ?? "?").toUpperCase()}
    </span>
  );
}

/**
 * 사이드바의 테넌트 전환 (P81 5절). GET /tenants 의 소속 목록(platform-admin 은 "*" + 설정 문서가 있는 테넌트 전부)과
 * 최근 사용 목록을 합친다. 직접 입력은 header 모드나 platform-admin 만 할 수 있다.
 * 바꾸면 모든 쿼리를 무효화해 새 테넌트 데이터로 다시 그린다.
 *
 * 펼친 사이드바에서는 머리글자 칸, 이름과 역할 두 줄, 위아래 갈매기를 담은 카드이고 옆에 직접 입력 단추가 선다.
 * 접힌 레일에서는 머리글자 칸 하나다. 둘 다 같은 셀렉트라 목록과 키보드 동작이 같다.
 */
export function TenantSelector({ rail = false }: { rail?: boolean } = {}) {
  const t = useT();
  const text = t.detail.tenantSelector;
  const tenant = useTenant();
  const recent = useRecentTenants();
  const auth = useAuthState();
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  // 문장 대신 잘못 적었는지만 둔다. 문장은 그릴 때 화면 언어로 만든다
  const [invalid, setInvalid] = useState(false);

  const memberships = useQuery({
    queryKey: ["my-tenants", auth.me?.sub ?? null],
    queryFn: listMyTenants,
    enabled: auth.me !== null,
  });
  const fromServer = memberTenantNames(memberships.data ?? []);
  const canPickAny = !tokenAuth(auth.mode) || (auth.me?.is_platform_admin ?? false);
  const options = canPickAny ? [...new Set([...fromServer, ...recent])] : fromServer.length ? fromServer : [tenant];
  if (!options.includes(tenant)) options.unshift(tenant);

  function choose(name: string) {
    if (name === tenant) return;
    setTenant(name);
    void queryClient.invalidateQueries();
  }

  function submitDraft() {
    const name = draft.trim();
    if (!isValidTenant(name)) {
      setInvalid(true);
      return;
    }
    choose(name);
    setDraft("");
    setInvalid(false);
    setAdding(false);
  }

  const roleOf = (name: string) => memberships.data?.find((m) => m.tenant === name)?.role;
  const role = roleOf(tenant);

  // 레일에서는 직접 입력 칸이 들어갈 자리가 없어 "+" 옆에 떠 있는 패널로 연다. 좁은 화면은 늘 레일이라 거기서도
  // 테넌트를 새로 쓸 수 있어야 한다. 바깥 누름은 다른 떠 있는 패널처럼 닫는다
  const addRef = useRef<HTMLButtonElement>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const floating = rail && adding;
  const cancel = () => {
    setAdding(false);
    setDraft("");
    setInvalid(false);
  };
  const { style } = usePopover({ open: floating, triggerRef: addRef, panelRef: formRef, onClose: cancel, align: "start" });

  const form = (
    <form
      ref={formRef}
      className={floating ? cn(POPOVER_PANEL_CLASS, "flex w-60 flex-col gap-1.5 p-2") : "flex flex-col gap-1.5"}
      style={floating ? style : undefined}
      onSubmit={(e) => {
        e.preventDefault();
        submitDraft();
      }}
      onKeyDown={(e) => {
        if (e.key !== "Escape") return;
        e.stopPropagation();
        cancel();
        addRef.current?.focus();
      }}
    >
      <Input autoFocus aria-label={text.newTenant} data-testid="tenant-input" value={draft} placeholder={text.namePlaceholder} onChange={(e) => setDraft(e.target.value)} />
      <div className="flex items-center gap-1.5">
        <Button size="sm" type="submit" data-testid="tenant-add-submit">
          {text.use}
        </Button>
        <Button size="sm" variant="ghost" onClick={cancel}>
          {t.common.cancel}
        </Button>
      </div>
      {invalid ? <p className="text-xs text-err-fg">{text.invalid}</p> : null}
    </form>
  );

  return (
    <div className={cn("flex flex-col gap-1", rail && "items-center")} data-testid="tenant-selector">
      {adding && !rail ? (
        form
      ) : (
        <div className={cn("flex w-full items-center gap-1", rail && "flex-col")}>
          <Select
            aria-label={text.labels.select}
            data-testid="tenant-select"
            title={rail ? tenant : undefined}
            className={rail ? SWITCHER_TILE : SWITCHER_CARD}
            value={tenant}
            onChange={(e) => choose(e.target.value)}
            renderTrigger={() =>
              rail ? (
                <TenantInitial name={tenant} />
              ) : (
                <>
                  <TenantInitial name={tenant} />
                  <span className="flex min-w-0 flex-1 flex-col leading-tight">
                    <span className="truncate text-[13.5px] font-semibold text-heading" data-testid="tenant-current">
                      {tenant}
                    </span>
                    {role ? <span className="truncate text-xs text-muted">{role}</span> : null}
                  </span>
                  <ChevronsUpDown size={14} className="shrink-0 text-muted" aria-hidden />
                </>
              )
            }
          >
            {options.map((name) => (
              <option key={name} value={name}>
                {name}
                {roleOf(name) ? ` (${roleOf(name)})` : ""}
              </option>
            ))}
          </Select>
          {canPickAny ? (
            <Button
              ref={addRef}
              size="sm"
              variant="ghost"
              className={cn("shrink-0 px-0", rail ? "size-7" : "size-8")}
              data-testid="tenant-add"
              aria-label={text.add}
              aria-expanded={rail ? adding : undefined}
              title={text.addHint}
              onClick={() => (floating ? cancel() : setAdding(true))}
            >
              <Plus size={rail ? 14 : 16} aria-hidden />
            </Button>
          ) : null}
          {floating ? form : null}
        </div>
      )}
    </div>
  );
}
