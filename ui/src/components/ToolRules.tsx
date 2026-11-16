/**
 * Tool 규칙 카드 (P52 7절). MCP Server 와 Gateway 의 정의 탭, 허브 image 항목의 Tools 탭이 같이 쓴다.
 *
 * - 묶음(section)마다 Tool 이 한 줄이고 줄을 펴면 부를 수 있는 대상, 읽기 전용 표시, 인자 조건을 고친다. 출처마다 받는 칸이
 *   다르다(lib/toolRules.ts). 항목은 역할만, Gateway 는 대상만, MCP Server 는 셋 다다. 그룹은 oidc 모드에서만, Agent 는 항목과
 *   아직 IdP 토큰만 받는 oidc 모드의 Gateway 에서는 받지 않는다(`gatewayAgents`).
 * - 저장은 한 번에 전부다. 부르는 쪽이 리소스의 저장 API 로 보내고 답의 알림(rules_notice)을 돌려준다. 알림은 모두에게 열린
 *   Tool 과 토큰을 다시 받으려고 다시 뜰 수 있는 Agent 다(1절).
 * - 저장된 판(version)이 바뀌면 고치던 것을 버리고 저장된 것으로 다시 연다. 저장하지 않은 입력을 두고 나가면 묻는다.
 * - data-testid: `tool-rules-card`, `tool-rule-<key>`, `tool-rule-<key>-role-<role>`, `-groups`, `-agent-<id>`,
 *   `-read-only`, `-condition-<i>-path|op|value|optional|remove`, `-add-condition`, `-opens`, `tool-rules-save`,
 *   `tool-rules-opened`, `tool-rules-rolling`.
 */
import { useMutation } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { ErrorMessage } from "@/components/ErrorMessage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { useT } from "@/i18n";
import {
  RULE_OPERATORS,
  RULE_ROLES,
  conditionArguments,
  draftOf,
  normalizedRule,
  opening,
  ruleOf,
  ruledCount,
  type ConditionDraft,
  type RuleDraft,
  type RuleKind,
  type RuleOperator,
  type RuleRole,
  type RulesNotice,
  type StoredRule,
} from "@/lib/toolRules";
import { useUnsavedGuard } from "@/lib/unsavedGuard";

/** 규칙을 둘 Tool 하나. key 는 저장 키(operationId, 백엔드의 원래 이름, Tool 이름)이고 name 은 지금 화면 이름이다. */
export interface RuleTool {
  key: string;
  name: string;
  /** 규칙은 저장돼 있는데 지금 제공하는 Tool 목록에 없다. 서버가 그 키의 저장을 거절하므로 대상을 비워 걷게 한다. */
  stale?: boolean;
  /** MCP Server 의 Tool 이 받는 인자 {점 경로: 형식}. 버전에 인자 목록이 없으면 null 이다. */
  arguments?: Readonly<Record<string, string>> | null;
  /** 읽기 전용 표시의 기본값. MCP Server 는 GET·HEAD, 항목은 쓰기(false)다. */
  readOnlyDefault?: boolean;
}

/** 규칙의 한 묶음. MCP Server 는 백엔드마다, Gateway 는 백엔드(허브 항목)마다, 허브 항목은 하나다. */
export interface RuleSection {
  id: string;
  title: ReactNode | null;
  tools: RuleTool[];
  rules: Readonly<Record<string, StoredRule>>;
}

/** 묶음 id → 저장 키 → 규칙. 저장이 보내는 꼴이다. */
export type RulesBySection = Record<string, Record<string, StoredRule>>;

/** 고친 Tool 의 고친 꼴. 고치지 않은 Tool 은 저장된 규칙에서 바로 읽는다(`currentDraft`). */
type Edits = Record<string, Record<string, RuleDraft>>;

function currentDraft(edits: Edits, s: RuleSection, key: string): RuleDraft {
  return edits[s.id]?.[key] ?? draftOf(s.rules[key]);
}

/**
 * 묶음의 줄. 제공하는 Tool 뒤에, 규칙은 저장돼 있는데 목록에 없는 키를 stale 로 붙인다. 백엔드가 Tool 을 빼거나 이름을 바꾼
 * 자리다. 그 규칙을 말없이 버리면 이름이 바뀐 Tool 이 규칙 없이 열리므로 사람이 보고 걷게 한다(1절).
 */
function rowsOf(s: RuleSection): RuleTool[] {
  const offered = new Set(s.tools.map((tool) => tool.key));
  const stale = Object.keys(s.rules).filter((key) => !offered.has(key));
  return [...s.tools, ...stale.map((key) => ({ key, name: key, stale: true }))];
}

/** 고친 것을 저장 꼴로. 아무것도 정하지 않은 규칙은 보내지 않는다. */
function rulesOf(kind: RuleKind, sections: readonly RuleSection[], edits: Edits): RulesBySection {
  const out: RulesBySection = {};
  for (const s of sections) {
    const rules: Record<string, StoredRule> = {};
    for (const tool of rowsOf(s)) {
      const rule = ruleOf(kind, currentDraft(edits, s, tool.key), tool.arguments, tool.readOnlyDefault ?? false);
      if (rule) rules[tool.key] = rule;
    }
    out[s.id] = rules;
  }
  return out;
}

/** 저장된 규칙을 같은 변환에 태운 것. 고친 것과 견줘 바뀐 것이 있는지 본다. */
function storedOf(kind: RuleKind, sections: readonly RuleSection[]): RulesBySection {
  const out: RulesBySection = {};
  for (const s of sections) {
    const rules: Record<string, StoredRule> = {};
    for (const tool of rowsOf(s)) {
      const normalized = normalizedRule(kind, s.rules[tool.key], tool.arguments, tool.readOnlyDefault ?? false);
      if (normalized) rules[tool.key] = normalized;
    }
    out[s.id] = rules;
  }
  return out;
}

/** 키를 정렬해 견준다. 저장 꼴의 키 순서는 뜻이 없다. */
function canonical(rules: RulesBySection): string {
  return JSON.stringify(rules, (_key, value: unknown) =>
    value && typeof value === "object" && !Array.isArray(value) ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))) : value,
  );
}

export function ToolRulesCard({
  kind,
  sections,
  canEdit,
  oidc,
  gatewayAgents = false,
  agents,
  version,
  loading = false,
  loadError = null,
  notes,
  footer,
  save: saveRules,
  onSaved,
}: {
  kind: RuleKind;
  sections: readonly RuleSection[];
  canEdit: boolean;
  oidc: boolean;
  /** Gateway 규칙이 Agent 를 받는가. 데이터 플레인이 플랫폼 토큰을 받을 때다(lib/auth.ts idpDataPlane). Gateway 의 카드만 넘긴다. */
  gatewayAgents?: boolean;
  /** 이 테넌트의 Agent. 저장된 id 가 여기 없으면 삭제된 Agent 다. */
  agents: readonly { id: string; name: string }[];
  /** 저장된 것의 판. 바뀌면 고치던 것을 버린다. */
  version: string | number;
  /** Tool 목록을 아직 읽는 중이다. */
  loading?: boolean;
  /** Tool 목록을 읽지 못한 오류. 있으면 저장을 막는다. 읽기 전용 표시의 기본값과 인자를 모른 채 저장하면 표시가 바뀐다. */
  loadError?: unknown;
  /** 카드 머리 아래의 안내. 출처마다 다르다. */
  notes: readonly string[];
  /** 저장 단추 옆의 한 줄(새 리비전 안내 등). */
  footer?: ReactNode;
  save: (rules: RulesBySection) => Promise<RulesNotice | null | undefined>;
  onSaved?: () => Promise<void> | void;
}) {
  const t = useT();
  const text = t.toolRules;
  const [edits, setEdits] = useState<Edits>({});
  const [notice, setNotice] = useState<RulesNotice | null>(null);
  useEffect(() => {
    setEdits({});
  }, [version]);

  const next = useMemo(() => rulesOf(kind, sections, edits), [kind, sections, edits]);
  const dirty = canonical(next) !== canonical(storedOf(kind, sections));
  const save = useMutation({
    mutationFn: () => saveRules(next),
    onSuccess: async (answer) => {
      setNotice(answer ?? null);
      await onSaved?.();
    },
  });
  useUnsavedGuard(dirty && !save.isPending);

  const count = sections.reduce((n, s) => n + ruledCount(s.rules), 0);
  const names = new Map<string, string>(sections.flatMap((s) => rowsOf(s).map((tool) => [`${s.id}/${tool.key}`, tool.name] as const)));
  // 서버 MCP Server·Gateway 의 키는 `<묶음>/<키>` 이고 항목은 Tool 이름 그대로다(services/tool_rules.py server_rule_keys)
  const openedNames = (notice?.opened ?? []).map((key) => names.get(key) ?? names.get(`${sections[0]?.id}/${key}`) ?? key.split("/").pop() ?? key);
  const rolling = (notice?.rolling_agents ?? []).map((a) => a.name ?? a.id);
  const agentNames = new Map(agents.map((a) => [a.id, a.name]));

  function setDraft(s: RuleSection, key: string, patch: Partial<RuleDraft>) {
    setNotice(null);
    setEdits((e) => ({ ...e, [s.id]: { ...e[s.id], [key]: { ...currentDraft(e, s, key), ...patch } } }));
  }

  return (
    <Card data-testid="tool-rules-card">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        <span className="text-xs text-muted" data-testid="tool-rules-count">
          {text.ruledTools(count)}
        </span>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {notes.map((note) => (
          <p key={note} className="text-xs text-muted">
            {note}
          </p>
        ))}
        {canEdit ? null : (
          <p className="text-xs text-warn-fg" data-testid="tool-rules-read-only">
            {text.editRequired}
          </p>
        )}
        {sections.map((s) => (
          <div key={s.id} className="flex flex-col gap-1.5" data-testid={`tool-rules-section-${s.id}`}>
            {s.title ? <div className="text-sm font-medium text-heading">{s.title}</div> : null}
            {loading ? (
              <p className="text-xs text-muted">{t.common.loading}</p>
            ) : rowsOf(s).length ? (
              rowsOf(s).map((tool) => (
                <ToolRuleRow
                  key={tool.key}
                  kind={kind}
                  tool={tool}
                  stored={s.rules[tool.key]}
                  draft={currentDraft(edits, s, tool.key)}
                  onChange={(patch) => setDraft(s, tool.key, patch)}
                  disabled={!canEdit || save.isPending}
                  oidc={oidc}
                  gatewayAgents={gatewayAgents}
                  agents={agents}
                  agentNames={agentNames}
                />
              ))
            ) : (
              <p className="text-xs text-muted" data-testid="tool-rules-no-tools">
                {text.noTools}
              </p>
            )}
          </div>
        ))}
        <ErrorMessage error={loadError} testId="tool-rules-load-error" />
        <ErrorMessage error={save.error} testId="tool-rules-error" />
        {save.isSuccess && !dirty ? (
          <Alert variant="ok" data-testid="tool-rules-saved">
            {text.saved}
          </Alert>
        ) : null}
        {openedNames.length ? (
          <Alert variant="warn" data-testid="tool-rules-opened">
            {text.openedSaved(openedNames.join(", "))}
          </Alert>
        ) : null}
        {rolling.length ? (
          <Alert variant="info" data-testid="tool-rules-rolling">
            {text.rollingSaved(rolling.join(", "))}
          </Alert>
        ) : null}
        {canEdit ? (
          <div className="flex flex-wrap items-center justify-end gap-3">
            {footer ? <span className="text-xs text-muted">{footer}</span> : null}
            <Button data-testid="tool-rules-save" disabled={!dirty || loading || Boolean(loadError) || save.isPending} onClick={() => save.mutate()}>
              {save.isPending ? text.saving : text.save}
            </Button>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function ToolRuleRow({
  kind,
  tool,
  stored,
  draft,
  onChange,
  disabled,
  oidc,
  gatewayAgents,
  agents,
  agentNames,
}: {
  kind: RuleKind;
  tool: RuleTool;
  stored: StoredRule | undefined;
  draft: RuleDraft;
  onChange: (patch: Partial<RuleDraft>) => void;
  disabled: boolean;
  oidc: boolean;
  gatewayAgents: boolean;
  agents: readonly { id: string; name: string }[];
  agentNames: ReadonlyMap<string, string>;
}) {
  const t = useT();
  const text = t.toolRules;
  const id = `tool-rule-${tool.key}`;
  const readOnlyDefault = tool.readOnlyDefault ?? false;
  const readOnly = draft.readOnly ?? readOnlyDefault;
  const groupsOffered = oidc && kind !== "item";
  // 아직 IdP 토큰만 받는 oidc 모드의 Gateway 는 Agent 를 규칙에 넣을 수 없다(422 rule_agent_oidc). 항목은 역할만 받는다
  const agentsOffered = kind === "server" || (kind === "gateway" && gatewayAgents);
  // 지운 Agent 의 id 는 저장된 그대로 두고 화면에 그 사실을 보인다(1절)
  const deleted = draft.agents.filter((a) => !agentNames.has(a));
  const toggle = <T,>(list: readonly T[], value: T, on: boolean): T[] => (on ? [...list, value] : list.filter((v) => v !== value));
  const groups = draft.groups.split(",").map((g) => g.trim()).filter(Boolean);

  return (
    <details className="rounded-card border border-line bg-surface px-3 py-2" data-testid={id}>
      <summary className="flex cursor-pointer flex-wrap items-center gap-1.5 text-sm">
        <span className="font-mono">{tool.name}</span>
        {tool.name !== tool.key ? <span className="font-mono text-xs text-dim">({tool.key})</span> : null}
        {tool.stale ? (
          <Badge variant="warn" data-testid={`${id}-stale`}>
            {text.notOffered}
          </Badge>
        ) : null}
        {draft.roles.map((role) => (
          <Badge key={role} variant="accent">
            {role}
          </Badge>
        ))}
        {groups.map((g) => (
          <Badge key={g} variant="accent">
            {g}
          </Badge>
        ))}
        {draft.agents.map((a) => (
          <Badge key={a} variant="info">
            {agentNames.get(a) ?? text.deletedAgent}
          </Badge>
        ))}
        {kind !== "gateway" && readOnly ? <Badge variant="muted">{text.readOnly}</Badge> : null}
        {draft.conditions.length ? (
          <Badge variant="muted">
            {text.conditions} {draft.conditions.length}
          </Badge>
        ) : null}
      </summary>
      <div className="mt-2 flex flex-col gap-3">
        <fieldset className="flex flex-col gap-2" disabled={disabled}>
          <legend className="text-sm font-medium text-fg-soft">{text.callers}</legend>
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span className="text-xs text-muted">{text.roles}</span>
            {RULE_ROLES.map((role) => (
              <label key={role} className="flex items-center gap-1">
                <input
                  type="checkbox"
                  data-testid={`${id}-role-${role}`}
                  checked={draft.roles.includes(role)}
                  onChange={(e) => onChange({ roles: toggle<RuleRole>(draft.roles, role, e.target.checked) })}
                />
                <span className="font-mono text-xs">{role}</span>
              </label>
            ))}
          </div>
          {groupsOffered ? (
            <div className="flex flex-col gap-1">
              <Label htmlFor={`${id}-groups`} className="text-xs text-muted">
                {text.groups}
              </Label>
              <Input id={`${id}-groups`} data-testid={`${id}-groups`} value={draft.groups} onChange={(e) => onChange({ groups: e.target.value })} />
              <span className="text-xs text-muted">{text.groupsHint}</span>
            </div>
          ) : groups.length ? (
            // 인증 모드가 바뀌어 받지 않는 대상이 저장돼 있다(oidc 밖의 그룹). 서버가 그 저장을 거절하므로 걷을 수만 있다
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <span className="text-xs text-muted">{text.groups}</span>
              {groups.map((g) => (
                <label key={g} className="flex items-center gap-1">
                  <input type="checkbox" data-testid={`${id}-group-${g}`} checked onChange={() => onChange({ groups: groups.filter((v) => v !== g).join(", ") })} />
                  <span className="font-mono text-xs">{g}</span>
                </label>
              ))}
            </div>
          ) : null}
          {!agentsOffered && draft.agents.length ? (
            // 아직 IdP 토큰만 받는 oidc 모드의 Gateway 에 저장된 Agent 다(422 rule_agent_oidc). 걷을 수만 있다
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <span className="text-xs text-muted">{text.agents}</span>
              {draft.agents.map((a) => (
                <label key={a} className="flex items-center gap-1" title={a}>
                  <input type="checkbox" data-testid={`${id}-agent-${a}`} checked onChange={() => onChange({ agents: draft.agents.filter((v) => v !== a) })} />
                  <span className={agentNames.has(a) ? undefined : "text-muted"}>{agentNames.get(a) ?? text.deletedAgent}</span>
                </label>
              ))}
            </div>
          ) : null}
          {agentsOffered ? (
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <span className="text-xs text-muted">{text.agents}</span>
              {agents.length || deleted.length ? null : <span className="text-xs text-muted">{text.noAgents}</span>}
              {agents.map((a) => (
                <label key={a.id} className="flex items-center gap-1">
                  <input type="checkbox" data-testid={`${id}-agent-${a.id}`} checked={draft.agents.includes(a.id)} onChange={(e) => onChange({ agents: toggle(draft.agents, a.id, e.target.checked) })} />
                  <span>{a.name}</span>
                </label>
              ))}
              {deleted.map((a) => (
                <label key={a} className="flex items-center gap-1" title={a}>
                  <input type="checkbox" data-testid={`${id}-agent-${a}`} checked onChange={() => onChange({ agents: draft.agents.filter((v) => v !== a) })} />
                  <span className="text-muted">{text.deletedAgent}</span>
                </label>
              ))}
            </div>
          ) : null}
          {opening(stored, draft) ? (
            <Alert variant="warn" data-testid={`${id}-opens`}>
              {text.opensNotice}
            </Alert>
          ) : null}
        </fieldset>
        {kind !== "gateway" ? (
          <fieldset className="flex flex-col gap-1" disabled={disabled}>
            <label className="flex items-center gap-1.5 text-sm font-medium text-fg-soft">
              <input
                type="checkbox"
                data-testid={`${id}-read-only`}
                checked={readOnly}
                onChange={(e) => onChange({ readOnly: e.target.checked === readOnlyDefault ? null : e.target.checked })}
              />
              {text.readOnly}
            </label>
            <span className="text-xs text-muted">{text.readOnlyHint}</span>
          </fieldset>
        ) : null}
        {kind === "server" ? <ConditionsEditor id={id} tool={tool} conditions={draft.conditions} disabled={disabled} onChange={(conditions) => onChange({ conditions })} /> : null}
      </div>
    </details>
  );
}

function ConditionsEditor({
  id,
  tool,
  conditions,
  disabled,
  onChange,
}: {
  id: string;
  tool: RuleTool;
  conditions: ConditionDraft[];
  disabled: boolean;
  onChange: (next: ConditionDraft[]) => void;
}) {
  const t = useT();
  const text = t.toolRules;
  const args = conditionArguments(tool.arguments);
  const set = (i: number, patch: Partial<ConditionDraft>) => onChange(conditions.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  return (
    <fieldset className="flex flex-col gap-2" disabled={disabled}>
      <legend className="text-sm font-medium text-fg-soft">{text.conditions}</legend>
      <span className="text-xs text-muted">{text.conditionsHint}</span>
      {conditions.map((c, i) => (
        <div key={i} className="flex flex-wrap items-center gap-2" data-testid={`${id}-condition-${i}`}>
          <Select aria-label={text.argument} data-testid={`${id}-condition-${i}-path`} value={c.path} onChange={(e) => set(i, { path: e.target.value })} className="w-48">
            {/* 저장된 경로가 지금 인자 목록에 없어도 그대로 보인다. 저장하면 서버가 그 경로를 다시 검사한다 */}
            {args.some(([path]) => path === c.path) ? null : <option value={c.path}>{c.path}</option>}
            {args.map(([path]) => (
              <option key={path} value={path}>
                {path}
              </option>
            ))}
          </Select>
          <Select aria-label={text.operator} data-testid={`${id}-condition-${i}-op`} value={c.op} onChange={(e) => set(i, { op: e.target.value as RuleOperator })} className="w-40">
            {RULE_OPERATORS.map((op) => (
              <option key={op} value={op}>
                {text.operators[op]}
              </option>
            ))}
          </Select>
          <Input aria-label={text.value} data-testid={`${id}-condition-${i}-value`} value={c.text} onChange={(e) => set(i, { text: e.target.value })} className="w-48" />
          <label className="flex items-center gap-1 text-xs">
            <input type="checkbox" data-testid={`${id}-condition-${i}-optional`} checked={c.optional} onChange={(e) => set(i, { optional: e.target.checked })} />
            {text.optional}
          </label>
          <Button variant="ghost" size="sm" data-testid={`${id}-condition-${i}-remove`} onClick={() => onChange(conditions.filter((_c, j) => j !== i))}>
            {text.removeCondition}
          </Button>
        </div>
      ))}
      {conditions.some((c) => c.op === "one_of") ? <span className="text-xs text-muted">{text.valuesHint}</span> : null}
      {args.length ? (
        <div>
          <Button variant="secondary" size="sm" data-testid={`${id}-add-condition`} onClick={() => onChange([...conditions, { path: args[0][0], op: "equals", text: "", optional: false }])}>
            {text.addCondition}
          </Button>
        </div>
      ) : tool.arguments ? null : (
        <span className="text-xs text-muted" data-testid={`${id}-no-arguments`}>
          {text.noArguments}
        </span>
      )}
    </fieldset>
  );
}
