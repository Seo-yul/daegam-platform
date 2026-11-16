/** 인스펙터 패널들이 같이 쓰는 작은 조각. 어느 패널에도 속하지 않는 것만 둔다. */
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router";

import { Alert } from "@/components/ui/card";
import { Field, Input, Label } from "@/components/ui/input";
import { tabClass } from "@/components/ui/tabs";
import { useT } from "@/i18n";
import { DEFAULT_EFFORT, issueText, type Issue } from "@/lib/agentGraph";
import { cn } from "@/lib/utils";

import { nodeData, type CanvasNode } from "../types";
import type { InspectorOptions } from "./types";

/**
 * 고른 리소스로 가는 작은 링크 (D43 넷째). 캔버스는 지금까지 닫힌 화면이라 여기서 고른 MCP
 * Server·Database·Knowledge Base 로 나갈 길이 하나도 없었다.
 *
 * 새 탭으로 연다. 같은 탭에서 옮기면 저장하지 않은 그래프를 잃기 때문이다.
 */
export function ResourceLink({ to, label, testId }: { to: string | null; label?: string; testId?: string }) {
  const t = useT();
  if (!to) return null;
  return (
    <Link className="text-xs text-muted underline-offset-2 hover:underline" to={to} target="_blank" rel="noreferrer" data-testid={testId}>
      {label ?? t.canvas.open}
    </Link>
  );
}

/**
 * 숫자 칸. 입력 중인 원문은 여기서 들고, 숫자가 되는 순간만 위로 올린다. 비운 채 두면 nullable 은 null 을, 아니면
 * 포커스가 나갈 때 min(없으면 0) 으로 되돌린다. 지우고 다시 치는 동안 값이 끼어들지 않게 하려는 것이다.
 */
export function NumberField({ id, label, value, onChange, min, hint, placeholder, nullable = false }: { id: string; label: string; value: number | null | undefined; onChange: (v: number | null) => void; min?: number; hint?: string; placeholder?: string; nullable?: boolean }) {
  const [text, setText] = useState(value == null ? "" : String(value));
  useEffect(() => {
    setText((cur) => (value == null ? (cur.trim() === "" || Number(cur) === 0 ? cur : "") : Number(cur) === value ? cur : String(value)));
  }, [value]);
  return (
    <Field label={label} htmlFor={id} hint={hint}>
      <Input
        id={id}
        data-testid={id}
        type="number"
        min={min}
        step="any"
        placeholder={placeholder}
        value={text}
        onChange={(e) => {
          const raw = e.target.value;
          setText(raw);
          if (raw === "") {
            if (nullable) onChange(null);
            return;
          }
          const n = Number(raw);
          if (Number.isFinite(n)) onChange(n);
        }}
        onBlur={() => {
          if (text.trim() === "" && !nullable) {
            const fallback = min ?? 0;
            setText(String(fallback));
            onChange(fallback);
          }
        }}
      />
    </Field>
  );
}

/** 켬·끔 한 줄. `disabled` 는 켤 수 없는 조건이 있을 때이고, 왜 못 켜는지는 호출부가 바로 아래에 적는다. */
export function Check({ id, label, checked, onChange, hint, disabled = false }: { id: string; label: string; checked: boolean; onChange: (v: boolean) => void; hint?: string; disabled?: boolean }) {
  return (
    <label className={cn("flex items-start gap-2 text-sm", disabled ? "text-muted" : "text-fg-soft")} htmlFor={id}>
      <input id={id} data-testid={id} type="checkbox" className="mt-1" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span>
        {label}
        {hint ? <span className="block text-xs text-muted">{hint}</span> : null}
      </span>
    </label>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange, testId }: { tabs: { id: T; label: string }[]; value: T; onChange: (t: T) => void; testId: string }) {
  return (
    <div className="flex flex-wrap gap-1 border-b border-line" role="tablist" data-testid={testId}>
      {tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          aria-selected={value === t.id}
          data-testid={`${testId}-${t.id}`}
          className={tabClass(value === t.id, "sm")}
          onClick={() => onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

/**
 * 탭이 하나뿐인 패널의 제목 줄. 탭 모양(테두리·글꼴)과 `inspector-tabs` testid 를 탭과 똑같이 쓰지만
 * 누를 것이 없다. 탭을 흉내 내려고 `onChange` 에 빈 함수를 넘기던 자리를 대신한다.
 */
export function PanelTitle({ id, children }: { id: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap gap-1 border-b border-line" data-testid="inspector-tabs">
      <span className={tabClass(true, "sm")} data-testid={`inspector-tabs-${id}`}>
        {children}
      </span>
    </div>
  );
}

/** 고른 노드·간선의 검증 이슈. 문장은 화면 언어(issueText)이고 code 는 뒤에 작은 회색 글씨로 둔다. */
export function IssueList({ issues }: { issues: Issue[] }) {
  const t = useT();
  if (!issues.length) return null;
  return (
    <div className="flex flex-col gap-1" data-testid="inspector-issues">
      {issues.map((i, k) => (
        <Alert key={k} variant={i.level === "error" ? "error" : "warn"} className="text-xs" title={i.code}>
          {issueText(t, i)} <span className="font-mono text-[10px] text-muted">{i.code}</span>
        </Alert>
      ))}
    </div>
  );
}

export function AttachedList({ label, nodes, empty, lookup }: { label: string; nodes: CanvasNode[]; empty: string; lookup: (n: CanvasNode) => string }) {
  return (
    <div className="flex flex-col gap-1">
      <Label>{label}</Label>
      {nodes.length ? (
        <ul className="flex flex-col gap-1">
          {nodes.map((n) => (
            <li key={n.id} className="flex items-center justify-between rounded-item border border-line bg-surface-strong px-2.5 py-1.5 text-xs" data-testid={`attached-${n.id}`}>
              <span className="font-mono text-fg">{n.id}</span>
              <span className="text-muted">{lookup(n)}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs text-muted">{empty}</p>
      )}
    </div>
  );
}

/** 에이전트 기본 탭의 "붙은 모델". 없으면 model_count 표식과 잇는 법을 보인다. 모델 설정은 모델 노드를 골라 바꾼다. */
export function AttachedModel({ nodes, options }: { nodes: CanvasNode[]; options: InspectorOptions }) {
  const t = useT();
  const text = t.canvas.agent;
  return (
    <div className="flex flex-col gap-1" data-testid="agent-model">
      <Label>{text.model}</Label>
      {nodes.length ? (
        <ul className="flex flex-col gap-1">
          {nodes.map((n) => {
            const d = nodeData(n, "model");
            const provider = options.providers.find((p) => p.name === d.provider) ?? null;
            const model = d.model ?? provider?.model ?? null;
            return (
              <li key={n.id} className="flex items-center justify-between gap-2 rounded-item border border-line bg-surface-strong px-2.5 py-1.5 text-xs" data-testid={`attached-${n.id}`}>
                <span className="font-mono text-fg">{n.id}</span>
                <span className="truncate text-muted">
                  {d.name} · {d.provider ? `${d.provider}${model ? ` · ${model}` : ""}` : t.canvas.providerUnset} · {t.canvas.node.effort} {d.reasoning?.effort ?? DEFAULT_EFFORT}
                </span>
              </li>
            );
          })}
        </ul>
      ) : (
        <Alert variant="error" className="text-xs" data-testid="agent-model-missing">
          {text.noModel}
        </Alert>
      )}
      {nodes.length > 1 ? <p className="text-xs text-err-fg">{t.canvas.connection.oneModel}</p> : null}
      {nodes.length ? <p className="text-xs text-muted">{text.modelHint}</p> : null}
    </div>
  );
}
