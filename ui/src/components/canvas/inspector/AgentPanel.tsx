/**
 * 에이전트 노드 패널. 기본(역할·이름·설명·언어·붙은 모델)·프롬프트(시스템 프롬프트·응답 형식·컨텍스트 압축)·
 * 도구(코드 실행, Skills 절 포함)·메모리·가드레일 탭이다. 모델 설정은 모델 노드에 있다 (D28). 서브는 컴파일이 읽지 않는 다섯 칸(응답
 * 언어·응답 형식·컨텍스트 압축·내장 파일 도구·코드 실행)을 숨기고 안내 한 줄을 보인다. 저장된 값은 건드리지 않는다.
 *
 * 코드 실행(P40 9절)은 메인의 칸이다. 켜면 `code_execution` 이 생기고 끄면 지운다. 명령 실행 전 승인은 execute 를 승인·거부로
 * 묻는다. 꺼진 칸은 내장 파일 Tool 이 꺼졌거나 배포된 클러스터의 게이트가 닫혔으면 켤 수 없고 그 까닭을 보인다. 켜진 칸은 언제나
 * 끌 수 있다.
 */
import { lazy, Suspense, useState } from "react";

import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { useT, type Messages } from "@/i18n";
import { DEFAULT_COMPACTION, DEFAULT_KNOWLEDGE_TOOL_NAME, SOURCE_KINDS, agentSlug, type AgentData, type Compaction, type EdgeKind } from "@/lib/agentGraph";
import { CODE_EXECUTION_APPROVAL, type GateReason } from "@/lib/codeExecution";

import { nodeData, type CanvasNode, type CanvasState } from "../types";
import { AttachedList, AttachedModel, Check, NumberField, Tabs } from "./fields";
import { SkillsSection } from "./SkillsSection";
import { dataServiceLabel, knowledgeBaseLabel, sourceLabel, type InspectorOptions, type InspectorProps } from "./types";

const MonacoJson = lazy(() => import("@/components/MonacoJson"));

type AgentTab = "basic" | "prompt" | "tools" | "memory" | "guardrail";
type Update = (patch: Partial<AgentData>, commit?: boolean) => void;

function triggerKind(t: Compaction["trigger"]): "fraction" | "tokens" | "messages" {
  if (t.tokens != null) return "tokens";
  if (t.messages != null) return "messages";
  return "fraction";
}
function keepKind(k: Compaction["keep"]): "fraction" | "messages" {
  return k.messages != null ? "messages" : "fraction";
}

/** 응답 형식을 JSON schema 로 바꿀 때의 기본 스키마. 부를 때마다 새 객체라 노드끼리 얽히지 않는다. */
function defaultSchema(): Record<string, unknown> {
  return { type: "object", properties: {} };
}

/** 편집기에 보일 스키마 원문. text 형식이면 기본 스키마다. */
function schemaTextOf(rf: AgentData["response_format"]): string {
  return JSON.stringify(rf !== "text" ? rf.json_schema : defaultSchema(), null, 2);
}

/** 편집기 원문을 스키마로 읽지 못한 까닭. detail 은 JSON 파서의 말이다. 문장은 그릴 때 만든다. */
type SchemaError = { kind: "json"; detail: string } | { kind: "notObject" };

/** 편집기에 남은 오류. reverted 면 형식을 바꾸면서 기본 스키마로 되돌린 것이다. */
type DraftError = SchemaError & { reverted: boolean };

/** 편집기 원문을 스키마로 읽는다. JSON 객체가 아니면 그 까닭을 돌려준다. */
function parseSchemaObject(text: string): { value: Record<string, unknown> } | { error: SchemaError } {
  let v: unknown;
  try {
    v = JSON.parse(text);
  } catch (e) {
    return { error: { kind: "json", detail: (e as Error).message } };
  }
  if (!v || typeof v !== "object" || Array.isArray(v)) return { error: { kind: "notObject" } };
  return { value: v as Record<string, unknown> };
}

function draftErrorText(t: Messages, error: DraftError): string {
  const text = t.canvas.agent.schemaError;
  const reason = error.kind === "json" ? text.json(error.detail) : text.notObject;
  return error.reverted ? text.reverted(reason) : reason;
}

/** 컨텍스트 압축 접기 제목에 적는 기본값 (예: `fraction 0.85 / 0.10`). */
const DEFAULT_COMPACTION_TEXT = `fraction ${DEFAULT_COMPACTION.trigger.fraction?.toFixed(2)} / ${DEFAULT_COMPACTION.keep.fraction?.toFixed(2)}`;

/**
 * 응답 형식 편집기의 원문과 오류다. 탭을 오가도 쓰던 원문이 남도록 패널이 든다. 다른 노드를 고르면 Inspector 가
 * 패널을 새로 그려(key) 그 노드 값에서 다시 시작한다.
 */
function useSchemaDraft(value: AgentData["response_format"], update: Update) {
  const [text, setText] = useState(() => schemaTextOf(value));
  const [error, setError] = useState<DraftError | null>(null);

  function edit(next: string) {
    setText(next);
    const parsed = parseSchemaObject(next);
    if ("error" in parsed) {
      setError({ ...parsed.error, reverted: false });
      return;
    }
    setError(null);
    update({ response_format: { json_schema: parsed.value } });
  }

  function choose(kind: string) {
    if (kind === "text") {
      update({ response_format: "text" }, true);
      return;
    }
    // 편집기에 남아 있던 글이 객체가 아니면 기본 스키마로 바꾸되 이유를 남긴다.
    // 조용히 넘기면 배열 같은 값이 저장까지 갔다가 그래프 검증에서야 걸린다
    const parsed = parseSchemaObject(text);
    setError("error" in parsed ? { ...parsed.error, reverted: true } : null);
    update({ response_format: { json_schema: "error" in parsed ? defaultSchema() : parsed.value } }, true);
  }

  return { text, error, edit, choose };
}

type SchemaDraft = ReturnType<typeof useSchemaDraft>;

/** 응답 형식과 그 JSON schema 편집기다. 메인에만 있다. */
function ResponseFormatFields({ value, draft }: { value: AgentData["response_format"]; draft: SchemaDraft }) {
  const t = useT();
  const text = t.canvas.agent;
  return (
    <>
      <Field label={text.responseFormat} htmlFor="agent-response-format">
        <Select id="agent-response-format" data-testid="agent-response-format" value={value === "text" ? "text" : "json_schema"} onChange={(e) => draft.choose(e.target.value)}>
          <option value="text">{text.formatText}</option>
          <option value="json_schema">{text.formatSchema}</option>
        </Select>
      </Field>
      {value !== "text" ? (
        <div className="flex flex-col gap-1" data-testid="agent-json-schema">
          <Suspense fallback={<p className="text-xs text-muted">{text.editorLoading}</p>}>
            <MonacoJson value={draft.text} onChange={draft.edit} testId="agent-json-schema-editor" />
          </Suspense>
          {draft.error ? (
            <p className="text-xs text-err-fg" data-testid="agent-json-schema-error">
              {draftErrorText(t, draft.error)}
            </p>
          ) : (
            <p className="text-xs text-muted">{text.schemaHint}</p>
          )}
        </div>
      ) : null}
    </>
  );
}

/** 고급 접기의 컨텍스트 압축(SummarizationMiddleware 의 발동 기준과 보관량)이다. 메인에만 있다. */
function CompactionFields({ compaction, update }: { compaction: Compaction; update: Update }) {
  const t = useT();
  const text = t.canvas.compaction;
  return (
    <details className="rounded-chip border border-line px-3 py-2" data-testid="agent-advanced">
      <summary className="cursor-pointer text-sm font-medium text-fg-soft">
        {text.summary(<span className="text-xs text-muted">{text.defaults(DEFAULT_COMPACTION_TEXT)}</span>)}
      </summary>
      <div className="mt-3 flex flex-col gap-3">
        <p className="text-xs text-muted">{text.help}</p>
        <div className="grid grid-cols-2 gap-3">
          <Field label={text.trigger} htmlFor="agent-compaction-trigger-kind">
            <Select
              id="agent-compaction-trigger-kind"
              data-testid="agent-compaction-trigger-kind"
              value={triggerKind(compaction.trigger)}
              onChange={(e) => {
                const k = e.target.value as "fraction" | "tokens" | "messages";
                const trigger = k === "fraction" ? { ...DEFAULT_COMPACTION.trigger } : k === "tokens" ? { tokens: 170000 } : { messages: 20 };
                update({ context: { compaction: { ...compaction, trigger } } }, true);
              }}
            >
              <option value="fraction">{text.triggerKinds.fraction}</option>
              <option value="tokens">{text.triggerKinds.tokens}</option>
              <option value="messages">{text.triggerKinds.messages}</option>
            </Select>
          </Field>
          <NumberField
            id="agent-compaction-trigger"
            label={text.value}
            min={triggerKind(compaction.trigger) === "fraction" ? 0 : 1}
            value={compaction.trigger[triggerKind(compaction.trigger)] ?? null}
            onChange={(v) => update({ context: { compaction: { ...compaction, trigger: { [triggerKind(compaction.trigger)]: v ?? 1 } } } })}
          />
          <Field label={text.keep} htmlFor="agent-compaction-keep-kind">
            <Select
              id="agent-compaction-keep-kind"
              data-testid="agent-compaction-keep-kind"
              value={keepKind(compaction.keep)}
              onChange={(e) => {
                const k = e.target.value as "fraction" | "messages";
                update({ context: { compaction: { ...compaction, keep: k === "fraction" ? { ...DEFAULT_COMPACTION.keep } : { messages: 6 } } } }, true);
              }}
            >
              <option value="fraction">{text.keepKinds.fraction}</option>
              <option value="messages">{text.keepKinds.messages}</option>
            </Select>
          </Field>
          <NumberField
            id="agent-compaction-keep"
            label={text.value}
            min={keepKind(compaction.keep) === "fraction" ? 0 : 1}
            value={compaction.keep[keepKind(compaction.keep)] ?? null}
            onChange={(v) => update({ context: { compaction: { ...compaction, keep: { [keepKind(compaction.keep)]: v ?? 1 } } } })}
          />
        </div>
      </div>
    </details>
  );
}

/** 메인 에이전트의 코드 실행 칸과 명령 실행 전 승인이다. gate 는 배포된 클러스터의 게이트가 닫힌 까닭이다. */
function CodeExecutionFields({ data, update, gate }: { data: AgentData; update: Update; gate: GateReason | null }) {
  const t = useT();
  const text = t.canvas.codeExecution;
  const on = Boolean(data.code_execution);
  // 키가 없으면 서버 기본값과 같이 켜진 것으로 읽는다 (agent_graph.py 의 BuiltinTools.filesystem)
  const filesystem = data.builtin_tools?.filesystem ?? true;
  const reason = !filesystem ? text.filesystemReason : gate ? t.clusters.sandbox.reason[gate] : null;
  const asks = (data.code_execution?.approval.length ?? 0) > 0;
  return (
    <div className="flex flex-col gap-2" data-testid="agent-code-execution">
      <Check
        id="agent-code-execution-on"
        label={text.label}
        hint={text.hint}
        checked={on}
        disabled={!on && reason !== null}
        onChange={(v) => update({ code_execution: v ? { approval: [] } : undefined }, true)}
      />
      {on ? (
        <div className="pl-6">
          <Check id="agent-code-execution-approval" label={text.approval} checked={asks} onChange={(v) => update({ code_execution: { approval: v ? [...CODE_EXECUTION_APPROVAL] : [] } }, true)} />
        </div>
      ) : null}
      {reason ? (
        <p className="text-xs text-warn-fg" data-testid="agent-code-execution-reason">
          {reason}
        </p>
      ) : null}
    </div>
  );
}

export function AgentPanel({ node, data, update, state, options, onChangeNodeData }: { node: CanvasNode; data: AgentData; update: Update; state: CanvasState; options: InspectorOptions; onChangeNodeData: InspectorProps["onChangeNodeData"] }) {
  const t = useT();
  const text = t.canvas.agent;
  const [tab, setTab] = useState<AgentTab>("basic");
  const isMain = data.role === "main";
  // 역할을 서브로 바꾸면 메모리·가드레일 탭이 사라진다. 그 탭에 있었으면 기본으로 돌린다
  const active: AgentTab = !isMain && (tab === "memory" || tab === "guardrail") ? "basic" : tab;
  const out = state.edges.filter((e) => e.source === node.id);
  const attached = (k: EdgeKind) => out.filter((e) => e.data?.kind === k).map((e) => state.nodes.find((n) => n.id === e.target)).filter(Boolean) as CanvasNode[];
  const draft = useSchemaDraft(data.response_format, update);

  return (
    <>
      <Tabs
        // 메모리와 가드레일은 메인 에이전트에만 붙는다(D28). 서브에는 탭 자체를 두지 않는다
        tabs={[
          { id: "basic", label: t.canvas.tabs.basic },
          { id: "prompt", label: t.canvas.tabs.prompt },
          { id: "tools", label: t.canvas.tabs.tools },
          ...(isMain ? ([{ id: "memory", label: t.canvas.nodeLabels.memory }, { id: "guardrail", label: t.canvas.nodeLabels.guardrail }] as const) : []),
        ]}
        value={active}
        onChange={setTab}
        testId="inspector-tabs"
      />
      {!isMain ? (
        // 서브는 응답 언어·응답 형식·컨텍스트 압축·내장 파일 도구·코드 실행 칸이 없다. 컴파일이 읽지 않는 값이다
        <p className="text-xs text-muted" data-testid="agent-sub-notice">
          {text.subNotice}
        </p>
      ) : null}
      {active === "basic" ? (
        <>
          <Field label={text.role} htmlFor="agent-role" hint={text.roleHint}>
            <Select id="agent-role" data-testid="agent-role" value={data.role} onChange={(e) => update({ role: e.target.value as AgentData["role"] }, true)}>
              <option value="main">{t.canvas.node.role.main}</option>
              <option value="sub">{t.canvas.node.role.sub}</option>
            </Select>
          </Field>
          <Field label={t.canvas.fields.name} htmlFor="agent-name" hint={text.nameHint}>
            <Input id="agent-name" data-testid="agent-name" className="font-mono" value={data.name} onChange={(e) => update({ name: e.target.value })} onBlur={(e) => update({ name: agentSlug(e.target.value) || data.name })} />
          </Field>
          <Field label={t.canvas.fields.description} htmlFor="agent-description" hint={text.descriptionHint[data.role]}>
            <Textarea id="agent-description" data-testid="agent-description" rows={3} className="font-sans" value={data.description} onChange={(e) => update({ description: e.target.value })} />
          </Field>
          {isMain ? (
            <Field label={text.language} htmlFor="agent-language" hint={text.languageHint}>
              <Input id="agent-language" data-testid="agent-language" value={data.language ?? ""} onChange={(e) => update({ language: e.target.value || null })} />
            </Field>
          ) : null}
          <AttachedModel nodes={attached("model")} options={options} />
        </>
      ) : null}
      {active === "prompt" ? (
        <>
          <Field label={text.systemPrompt} htmlFor="agent-system-prompt">
            <Textarea id="agent-system-prompt" data-testid="agent-system-prompt" rows={10} className="font-sans" value={data.system_prompt} onChange={(e) => update({ system_prompt: e.target.value })} />
          </Field>
          {isMain ? <ResponseFormatFields value={data.response_format} draft={draft} /> : null}
          {isMain ? <CompactionFields compaction={data.context?.compaction ?? DEFAULT_COMPACTION} update={update} /> : null}
        </>
      ) : null}
      {active === "tools" ? (
        <>
          {isMain ? (
            // 메인 전용이다(D26). 같은 모델을 쓰는 서브에 따로 줄 수 없어 메인 값이 서브에도 걸린다
            <Check id="agent-builtin-filesystem" label={text.filesystem} hint={text.filesystemHint} checked={data.builtin_tools?.filesystem ?? true} onChange={(v) => update({ builtin_tools: { filesystem: v } }, true)} />
          ) : null}
          {isMain ? <CodeExecutionFields data={data} update={update} gate={options.codeExecutionGate ?? null} /> : null}
          <div className="grid grid-cols-3 gap-3">
            <NumberField id="agent-max-tool-calls" label={text.maxToolCalls} min={1} value={data.max_tool_calls_per_turn} onChange={(v) => update({ max_tool_calls_per_turn: v ?? 1 })} />
            <NumberField id="agent-turn-timeout" label={text.turnTimeout} min={1} value={data.turn_timeout_seconds} onChange={(v) => update({ turn_timeout_seconds: v ?? 1 })} />
            <NumberField id="agent-retries" label={text.retries} min={0} value={data.retries} onChange={(v) => update({ retries: v ?? 0 })} />
          </div>
          <AttachedList label={text.tools} nodes={attached("tools")} empty={text.toolsEmpty} lookup={(n) => `${SOURCE_KINDS[nodeData(n, "tools").source.kind].label} · ${sourceLabel(t, nodeData(n, "tools").source, options)}`} />
          <AttachedList label={text.knowledge} nodes={attached("knowledge")} empty={text.knowledgeEmpty} lookup={(n) => `${nodeData(n, "knowledge").tool_name || DEFAULT_KNOWLEDGE_TOOL_NAME} · ${knowledgeBaseLabel(t, nodeData(n, "knowledge").knowledge_base_id, options)}`} />
          <SkillsSection nodeId={node.id} data={data} state={state} options={options} onChangeNodeData={onChangeNodeData} />
          {isMain ? <AttachedList label={text.delegates} nodes={attached("delegate")} empty={text.delegatesEmpty} lookup={(n) => nodeData(n, "agent").name} /> : null}
        </>
      ) : null}
      {active === "memory" ? (
        <AttachedList label={text.checkpointer} nodes={attached("memory")} empty={text.checkpointerEmpty} lookup={(n) => dataServiceLabel(t, nodeData(n, "memory").checkpointer?.data_service_id, options)} />
      ) : null}
      {active === "guardrail" ? (
        <AttachedList label={text.guardrails} nodes={attached("guardrail")} empty={text.guardrailsEmpty} lookup={(n) => text.guardrailRow(nodeData(n, "guardrail").position, nodeData(n, "guardrail").rules?.length ?? 0)} />
      ) : null}
    </>
  );
}
