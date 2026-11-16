import { useId, useState, type KeyboardEvent } from "react";

import { HUB_ACTIONS, type AccessSubjects, type HubAccess, type HubAction } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Input, Label, Select } from "@/components/ui/input";
import { useT } from "@/i18n";
import { ACCESS_ROLES, DEFAULT_MIN_ROLE } from "@/lib/hubAccess";
import { cn } from "@/lib/utils";

/**
 * 허브 항목 접근 정책 편집기 (docs/guide/hub-gateway.md 7절, D23). view / consume / manage 세 줄이고 줄마다
 * 테넌트(칩 입력 + `*` 체크), role 최소값(셀렉트 하나. 서버는 목록을 받지만 UI 는 `roles: [값]` 으로 보낸다),
 * Okta 그룹(칩 입력)이다. 허브 상세의 정책 폼, 게시 다이얼로그, 외부 MCP 등록 폼이 같이 쓴다.
 *
 * - 경계를 넓히는 저장(테넌트 추가, `*`)은 hub:share 라 platform-admin 만 한다. canShare 가 거짓이면 추가만
 *   막고 이미 있는 칩은 뺄 수 있다(좁히기는 manage 만으로 된다).
 * - internal·published 항목은 다른 테넌트에 열 수 없다(422 cross_tenant_unsupported). crossTenant 가 거짓이면
 *   테넌트 줄 전체가 비활성이다.
 * - Skill(D64)도 같은 정책을 쓴다. 동작의 뜻(labels), 넓히는 scope(shareScope), 항목을 가진 테넌트를 부르는 말(ownerLabel)이
 *   다르고, manage 는 소유 테넌트 밖으로 열지 못해 tenantActions 에 없는 동작은 테넌트 칸 대신 그 사실을 적는다.
 * - data-testid: `<testId>-fields`, `<testId>-<action>-tenants|tenants-all|tenants-fixed|role|groups`, `<testId>-toggle`.
 */
export interface AccessPolicyEditorProps {
  value: HubAccess;
  onChange: (next: HubAccess) => void;
  /** platform-admin 이면 참. */
  canShare: boolean;
  /** 다른 테넌트에 열 수 있는 kind(remote·entry·image)면 참. 기본 참. */
  crossTenant?: boolean;
  /** 다른 테넌트를 넣을 수 있는 동작. 기본은 셋 다다. */
  tenantActions?: readonly HubAction[];
  /** 동작마다의 뜻. 기본은 허브 항목의 것이다. */
  labels?: Record<HubAction, { title: string; hint: string }>;
  /** 경계를 넓히는 저장에 드는 scope. 안내 문장에 쓴다. */
  shareScope?: string;
  /** 항목을 가진 테넌트를 부르는 말. 허브 항목은 "게시한 테넌트", Skill 은 "소유 테넌트" 다. */
  ownerLabel?: string;
  /** 접었다 펼 수 있는 섹션으로 그린다. 게시 다이얼로그와 등록 폼은 접힘으로 시작한다. */
  collapsible?: boolean;
  defaultOpen?: boolean;
  disabled?: boolean;
  testId?: string;
}

export function AccessPolicyEditor({
  value,
  onChange,
  canShare,
  crossTenant = true,
  tenantActions = HUB_ACTIONS,
  labels: labelsProp,
  shareScope = "hub:share",
  ownerLabel: ownerProp,
  collapsible = false,
  defaultOpen = !collapsible,
  disabled = false,
  testId = "access",
}: AccessPolicyEditorProps) {
  const t = useT();
  const text = t.shared.accessPolicyEditor;
  const labels = labelsProp ?? t.hub.access.actions;
  const ownerLabel = ownerProp ?? t.hub.access.owner;
  const [open, setOpen] = useState(defaultOpen);
  const bodyId = `${useId()}-body`;

  function update(action: HubAction, patch: Partial<AccessSubjects>) {
    onChange({ ...value, [action]: { ...value[action], ...patch } });
  }

  const tenantsLocked = disabled || !crossTenant;
  const tenantsAddLocked = tenantsLocked || !canShare;

  const body = (
    <div id={bodyId} className="flex flex-col gap-3" data-testid={`${testId}-fields`}>
      {HUB_ACTIONS.map((action) => {
        const s = value[action];
        const all = s.tenants.includes("*");
        const chips = s.tenants.filter((tenant) => tenant !== "*");
        return (
          <div key={action} className="rounded-card border border-line bg-surface px-3 py-2.5" data-testid={`${testId}-${action}`}>
            <div className="mb-2 flex items-center gap-2">
              <Badge variant={action === "manage" ? "accent" : action === "consume" ? "info" : "default"}>{labels[action].title}</Badge>
              <span className="text-xs text-muted">{labels[action].hint}</span>
            </div>
            <div className="grid gap-3 sm:grid-cols-[1fr_10rem_1fr]">
              {tenantActions.includes(action) ? (
                <div className="flex flex-col gap-1">
                  <Label htmlFor={`${testId}-${action}-tenants`}>{text.tenants}</Label>
                  <ChipInput
                    id={`${testId}-${action}-tenants`}
                    testId={`${testId}-${action}-tenants`}
                    values={chips}
                    onChange={(next) => update(action, { tenants: all ? ["*", ...next] : next })}
                    canAdd={!tenantsAddLocked}
                    canRemove={!tenantsLocked}
                    placeholder={all ? text.allTenants : text.ownerOnly(ownerLabel)}
                  />
                  <label className={cn("flex items-center gap-1.5 text-xs", tenantsAddLocked && !all ? "text-dim" : "text-muted")}>
                    <input
                      type="checkbox"
                      data-testid={`${testId}-${action}-tenants-all`}
                      checked={all}
                      // 넓히기(체크)는 shareScope(hub:share·skill:share), 좁히기(해제)는 manage 만으로 된다
                      disabled={all ? tenantsLocked : tenantsAddLocked}
                      onChange={(e) => update(action, { tenants: e.target.checked ? ["*", ...chips] : chips })}
                    />
                    {text.allTenantsCheck}
                  </label>
                </div>
              ) : (
                <p className="self-center text-xs text-muted" data-testid={`${testId}-${action}-tenants-fixed`}>
                  {text.ownerFixed(ownerLabel)}
                </p>
              )}
              <div className="flex flex-col gap-1">
                <Label htmlFor={`${testId}-${action}-role`}>{text.minRole}</Label>
                <Select
                  id={`${testId}-${action}-role`}
                  data-testid={`${testId}-${action}-role`}
                  value={s.roles[0] ?? ""}
                  disabled={disabled}
                  onChange={(e) => update(action, { roles: e.target.value ? [e.target.value] : [] })}
                >
                  <option value="">{text.defaultRole(DEFAULT_MIN_ROLE[action])}</option>
                  {ACCESS_ROLES.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </Select>
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor={`${testId}-${action}-groups`}>{text.oktaGroups}</Label>
                <ChipInput
                  id={`${testId}-${action}-groups`}
                  testId={`${testId}-${action}-groups`}
                  values={s.groups}
                  onChange={(next) => update(action, { groups: next })}
                  canAdd={!disabled}
                  canRemove={!disabled}
                  placeholder={text.groupPlaceholder}
                />
              </div>
            </div>
          </div>
        );
      })}
      <p className="text-xs text-muted" data-testid={`${testId}-hint`}>
        {text.hint}
      </p>
      {!crossTenant ? (
        <p className="text-xs text-muted" data-testid={`${testId}-cross-tenant-reason`}>
          {text.crossTenant(ownerLabel)}
        </p>
      ) : !canShare ? (
        <p className="text-xs text-warn-fg" data-testid={`${testId}-share-reason`}>
          {text.share(shareScope)}
        </p>
      ) : null}
    </div>
  );

  if (!collapsible) return body;
  return (
    <div className="flex flex-col gap-2">
      <button
        type="button"
        data-testid={`${testId}-toggle`}
        aria-expanded={open}
        aria-controls={bodyId}
        className="flex items-center gap-2 rounded-item py-1 text-left text-sm font-medium text-fg hover:text-heading focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring"
        onClick={() => setOpen((v) => !v)}
      >
        <svg aria-hidden="true" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" className={cn("size-4 text-label transition-transform", open && "rotate-90")}>
          <path d="M6 4l4 4-4 4" />
        </svg>
        <span>{t.shared.accessPolicyCard.title}</span>
        <span className="text-xs font-normal text-muted">{open ? "" : text.collapsed}</span>
      </button>
      {open ? body : null}
    </div>
  );
}

/**
 * 칩 입력. Enter·쉼표·blur 로 값을 더하고 칩의 × 로 뺀다. canAdd 가 거짓이면 입력만 막고 칩은 그대로 뺄 수 있다.
 * data-testid: 입력은 `<testId>`, 칩은 `<testId>-chip-<값>`, 제거 버튼은 `<testId>-remove-<값>`.
 */
export function ChipInput({
  id,
  testId,
  values,
  onChange,
  canAdd = true,
  canRemove = true,
  placeholder,
}: {
  id?: string;
  testId: string;
  values: string[];
  onChange: (next: string[]) => void;
  canAdd?: boolean;
  canRemove?: boolean;
  placeholder?: string;
}) {
  const [text, setText] = useState("");

  function commit() {
    const items = text
      .split(",")
      .map((item) => item.trim())
      .filter((item) => item && !values.includes(item));
    if (items.length) onChange([...values, ...items]);
    setText("");
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      commit();
    } else if (e.key === "Backspace" && !text && values.length && canRemove) {
      onChange(values.slice(0, -1));
    }
  }

  return (
    <div className="flex flex-col gap-1">
      {values.length ? (
        <ul className="flex flex-wrap gap-1" data-testid={`${testId}-chips`}>
          {values.map((v) => (
            <li key={v}>
              <Badge variant="muted" className="font-mono" data-testid={`${testId}-chip-${v}`}>
                {v}
                {canRemove ? (
                  <button
                    type="button"
                    aria-label={`Remove ${v}`}
                    data-testid={`${testId}-remove-${v}`}
                    className="ml-0.5 rounded-full text-muted hover:text-err-fg"
                    onClick={() => onChange(values.filter((x) => x !== v))}
                  >
                    ×
                  </button>
                ) : null}
              </Badge>
            </li>
          ))}
        </ul>
      ) : null}
      <Input
        id={id}
        data-testid={testId}
        className="h-8 text-xs"
        value={text}
        disabled={!canAdd}
        placeholder={canAdd ? placeholder : undefined}
        autoComplete="off"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        onBlur={commit}
      />
    </div>
  );
}
