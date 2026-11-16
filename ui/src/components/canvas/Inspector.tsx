/**
 * 오른쪽 인스펙터. 선택한 노드 종류별 탭과 필드는 resource-model 5절 노드 `data` 표와 1:1 이다.
 *  - agent: 기본(역할·이름·설명·언어·붙은 모델)·프롬프트(시스템 프롬프트·응답 형식·컨텍스트 압축 "고급" 접기)·도구(Skills 절,
 *    D64)·메모리·가드레일. 모델 설정은 모델 노드에 있다(D28). 붙은 모델이 없으면 model_count 표식이다.
 *  - model: provider 셀렉트, 모델, 출력 토큰, 추론 강도(off|low|medium|high|xhigh|max, openai 는 max 없음),
 *    temperature 는 openai SDK 로 부르는 provider(openai, openrouter, zai)일 때만, fallback, 이 모델을 쓰는 에이전트 목록.
 *  - tools: 소스(Gateway·MCP Server 셀렉트) 와 그 소스의 도구 목록에서 허용 도구 체크, 도구별 승인 결정 종류, 타임아웃.
 *  - memory: Database(postgres·redis) 셀렉트와 checkpointer 칩 (D33), 장기 기억 절(켬·보관 기간, D34).
 *    guardrail: 위치·규칙(종류별 입력, injection_check 는 이 그래프의 모델 노드와 두 한도)·위반 동작·안내문. entry: 세션·한도.
 *  - knowledge: Knowledge Base 셀렉트(상태 칩), 도구 이름, 설명, top_k, min_score (D32).
 *  - 아무것도 고르지 않으면 Agent(이름·서비스 설정), 간선을 고르면 간선 정보와 삭제.
 * 텍스트 입력은 commit 없이 반영하고 패널에서 포커스가 나갈 때 commit 한다. 셀렉트·체크박스는 바로 commit 이다.
 */
import { useState, type ReactElement, type ReactNode } from "react";

import { NameFields } from "@/components/NameFields";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Field, Input, Label, Select, Textarea } from "@/components/ui/input";
import { useT } from "@/i18n";
import { checkpointerBadge } from "@/lib/dataServices";
import { kindColor } from "@/lib/kindColor";
import { embeddingText, knowledgeBaseBadge } from "@/lib/knowledge";
import { displayName } from "@/lib/names";
import { effectiveModel, sdkKind } from "@/lib/providers";
import { mainHasMemory } from "@/lib/toolSafety";
import {
  DEFAULT_EFFORT,
  EDGE_KINDS,
  EFFORTS,
  NODE_TYPES,
  effortsFor,
  longTermEnabled,
  mainAgentNode,
  type EntryData,
  type Issue,
  type KnowledgeData,
  type MemoryData,
  type ModelData,
  type NodeData,
  type NodeType,
} from "@/lib/agentGraph";

import { nodeData, type CanvasEdge, type CanvasNode, type CanvasState } from "./types";
import { AgentPanel } from "./inspector/AgentPanel";
import { GuardrailPanel, type JudgeModelOption } from "./inspector/GuardrailPanel";
import { ToolsPanel } from "./inspector/ToolsPanel";
import { AttachedList, Check, IssueList, NumberField, PanelTitle, ResourceLink, Tabs } from "./inspector/fields";
import { ProposalTexts } from "./inspector/ProposalTexts";
import { toolNamesOfSource, type InspectorOptions, type InspectorProps, type ProposalText } from "./inspector/types";

// 화면 밖에서는 이 파일에서 가져다 쓴다 (`@/components/canvas/Inspector`)
export { toolNamesOfSource };
export type { InspectorOptions, InspectorProps, ProposalText };

// -- entry -----------------------------------------------------------------------------------

function EntryPanel({ data, update }: { data: EntryData; update: (patch: Partial<EntryData>, commit?: boolean) => void }) {
  const t = useT();
  const text = t.canvas.entry;
  const [tab, setTab] = useState<"basic" | "limits">("basic");
  return (
    <>
      <Tabs
        tabs={[
          { id: "basic", label: t.canvas.tabs.basic },
          { id: "limits", label: t.canvas.tabs.limits },
        ]}
        value={tab}
        onChange={setTab}
        testId="inspector-tabs"
      />
      {tab === "basic" ? (
        <>
          <Field label={t.shared.chatPanel.greeting} htmlFor="entry-greeting" hint={text.greetingHint}>
            <Textarea id="entry-greeting" data-testid="entry-greeting" rows={3} className="font-sans" value={data.greeting ?? ""} onChange={(e) => update({ greeting: e.target.value || null })} />
          </Field>
          <NumberField id="entry-session-ttl" label={text.sessionTtl} min={1} value={data.session_ttl_seconds} onChange={(v) => update({ session_ttl_seconds: v ?? 1 })} hint={text.sessionTtlHint} />
          <NumberField id="entry-max-turns" label={text.maxTurns} min={1} value={data.max_turns} onChange={(v) => update({ max_turns: v ?? 1 })} />
        </>
      ) : (
        <>
          <Check
            id="entry-rate-limit-enabled"
            label={text.rateLimit}
            hint={t.canvas.needsMemory}
            checked={data.rate_limit !== null}
            onChange={(on) => update({ rate_limit: on ? { per_user_per_minute: 20 } : null }, true)}
          />
          {data.rate_limit ? <NumberField id="entry-rate-limit" label={text.perMinute} min={1} value={data.rate_limit.per_user_per_minute} onChange={(v) => update({ rate_limit: { per_user_per_minute: v ?? 1 } })} /> : null}
          <NumberField id="entry-max-concurrent" label={text.maxConcurrent} min={1} value={data.max_concurrent_sessions} onChange={(v) => update({ max_concurrent_sessions: v ?? 1 })} />
          <NumberField id="entry-max-input" label={text.maxInput} min={1} value={data.max_input_chars} onChange={(v) => update({ max_input_chars: v ?? 1 })} hint={text.maxInputHint} />
        </>
      )}
    </>
  );
}

// -- model -----------------------------------------------------------------------------------

function ModelPanel({ node, data, update, state, options }: { node: CanvasNode; data: ModelData; update: (patch: Partial<ModelData>, commit?: boolean) => void; state: CanvasState; options: InspectorOptions }) {
  const t = useT();
  const text = t.canvas.model;
  const select = t.shared.select;
  const provider = options.providers.find((p) => p.name === data.provider) ?? null;
  const sdk = sdkKind(provider?.kind);
  const efforts = effortsFor(provider?.kind, data.model ?? effectiveModel(provider));
  const fallbackProvider = data.fallback ? options.providers.find((p) => p.name === data.fallback?.provider) ?? null : null;
  const users = state.edges
    .filter((e) => e.target === node.id && e.data?.kind === "model")
    .map((e) => state.nodes.find((n) => n.id === e.source))
    .filter(Boolean) as CanvasNode[];
  return (
    <>
      <PanelTitle id="model">{t.canvas.nodeLabels.model}</PanelTitle>
      <Field label={t.canvas.fields.name} htmlFor="model-name" hint={text.nameHint}>
        <Input id="model-name" data-testid="model-name" value={data.name} onChange={(e) => update({ name: e.target.value })} />
      </Field>
      <Field label={text.labels.provider} htmlFor="model-provider" hint={text.providerHint}>
        <Select id="model-provider" data-testid="model-provider" value={data.provider} placeholder={select} onChange={(e) => update({ provider: e.target.value }, true)}>
          <option value="">{select}</option>
          {options.providers.map((p) => (
            <option key={p.name} value={p.name}>
              {p.name} ({p.kind}{p.model ? ` · ${p.model}` : ""})
            </option>
          ))}
        </Select>
      </Field>
      <Field label={t.canvas.nodeLabels.model} htmlFor="model-model" hint={text.modelHint}>
        <Input id="model-model" data-testid="model-model" className="font-mono" placeholder={effectiveModel(provider) ?? ""} value={data.model ?? ""} onChange={(e) => update({ model: e.target.value || null })} />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <NumberField id="model-max-output-tokens" label={text.maxOutputTokens} min={1} value={data.max_output_tokens} onChange={(v) => update({ max_output_tokens: v ?? 1 })} />
        <Field label={text.effort} htmlFor="model-effort" hint={provider?.kind === "zai" ? text.effortHintGlm : sdk === "openai" ? text.effortHintOpenai : text.effortHint}>
          <Select id="model-effort" data-testid="model-effort" value={data.reasoning?.effort ?? DEFAULT_EFFORT} onChange={(e) => update({ reasoning: { effort: e.target.value as ModelData["reasoning"]["effort"] } }, true)}>
            {EFFORTS.map((ef) => (
              <option key={ef} value={ef} disabled={!efforts.includes(ef)}>
                {ef}
              </option>
            ))}
          </Select>
        </Field>
      </div>
      {sdk === "openai" ? (
        <NumberField id="model-temperature" label={text.labels.temperature} min={0} nullable value={data.temperature} onChange={(v) => update({ temperature: v })} hint={text.temperatureHint} />
      ) : (
        <p className="text-xs text-muted" data-testid="model-temperature-note">
          {text.temperatureNote(data.temperature != null)}
        </p>
      )}
      <Check
        id="model-fallback-enabled"
        label={text.fallback}
        hint={text.fallbackHint}
        checked={data.fallback !== null}
        onChange={(on) => update({ fallback: on ? { provider: options.providers.find((p) => p.name !== data.provider)?.name ?? "", model: null } : null }, true)}
      />
      {data.fallback ? (
        <div className="grid grid-cols-2 gap-3">
          <Field label={text.fallbackProvider} htmlFor="model-fallback-provider">
            <Select id="model-fallback-provider" data-testid="model-fallback-provider" value={data.fallback.provider} placeholder={select} onChange={(e) => update({ fallback: { provider: e.target.value, model: data.fallback?.model ?? null } }, true)}>
              <option value="">{select}</option>
              {options.providers.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name} ({p.kind})
                </option>
              ))}
            </Select>
          </Field>
          <Field label={text.fallbackModel} htmlFor="model-fallback-model">
            <Input id="model-fallback-model" data-testid="model-fallback-model" className="font-mono" placeholder={fallbackProvider?.model ?? ""} value={data.fallback.model ?? ""} onChange={(e) => update({ fallback: { provider: data.fallback?.provider ?? "", model: e.target.value || null } })} />
          </Field>
        </div>
      ) : null}
      <AttachedList label={text.users} nodes={users} empty={text.usersEmpty} lookup={(n) => `${nodeData(n, "agent").name} (${nodeData(n, "agent").role})`} />
    </>
  );
}

// -- memory / guardrail / knowledge -------------------------------------------------------------

function MemoryPanel({ node, data, update, state, options }: { node: CanvasNode; data: MemoryData; update: (patch: Partial<MemoryData>, commit?: boolean) => void; state: CanvasState; options: InspectorOptions }) {
  const t = useT();
  const text = t.canvas.memory;
  const select = t.shared.select;
  // D33: Postgres 또는 Redis 8. redis 는 verify 가 남긴 capabilities.checkpointer 가 ready 여야 검증을 통과한다
  const stores = options.dataServices.filter((d) => d.kind === "postgres" || d.kind === "redis");
  const selected = stores.find((d) => d.id === (data.checkpointer?.data_service_id ?? "")) ?? null;
  const chip = selected?.kind === "redis" ? checkpointerBadge(t, selected.capabilities?.checkpointer, selected.db) : null;

  // D34: 장기 기억은 메인 에이전트가 내장 파일 도구로 /memories/ 를 읽고 쓴다. 켤 수 없는 조건은 여기서 말한다
  const main = mainAgentNode(state.nodes) ?? null;
  const mainAgent = main ? nodeData(main, "agent") : null;
  const attached = main ? state.edges.some((e) => e.source === main.id && e.target === node.id && e.data?.kind === "memory") : false;
  const longTermOn = longTermEnabled(data);
  const retention = data.long_term?.retention_days ?? null;
  const blocked = !mainAgent
    ? text.noMain
    : !attached
      ? text.notAttached(mainAgent.name)
      : // 키가 없으면 서버 기본값과 같이 켜진 것으로 읽는다 (agent_graph.py 의 BuiltinTools.filesystem)
        !(mainAgent.builtin_tools?.filesystem ?? true)
        ? text.filesystemOff(mainAgent.name)
        : null;
  return (
    <>
      <PanelTitle id="memory">{t.canvas.nodeLabels.memory}</PanelTitle>
      <Field label={text.labels.database} htmlFor="memory-data-service" hint={text.databaseHint}>
        <Select id="memory-data-service" data-testid="memory-data-service" value={data.checkpointer?.data_service_id ?? ""} placeholder={select} onChange={(e) => update({ checkpointer: { data_service_id: e.target.value } }, true)}>
          <option value="">{select}</option>
          {stores.map((d) => (
            <option key={d.id} value={d.id}>
              {displayName(d)} ({d.kind}) {d.host}:{d.port}/{d.kind === "redis" ? (d.db ?? "") : (d.database ?? "")}
            </option>
          ))}
        </Select>
        {selected ? (
          <div className="mt-1">
            <ResourceLink to={`/data-services/${selected.id}`} label={t.canvas.openResource("Database")} testId="memory-data-service-link" />
          </div>
        ) : null}
      </Field>
      {chip ? (
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted" data-testid="memory-checkpointer-status">
          <Badge variant={chip.variant} data-testid="memory-checkpointer">
            {chip.text}
          </Badge>
          <span className="font-mono">
            {text.checkpointer} {selected?.capabilities?.checkpointer ?? text.unverified}
          </span>
        </div>
      ) : null}
      {!stores.length ? <p className="text-xs text-warn-fg">{text.noStores}</p> : null}

      <div className="flex flex-col gap-2 border-t border-line pt-3" data-testid="memory-long-term">
        <Label>{text.longTerm}</Label>
        <p className="text-xs text-muted" data-testid="memory-long-term-vs-knowledge">
          {text.longTermVsKnowledge}
        </p>
        <Check
          id="memory-long-term-enabled"
          label={text.longTermEnable}
          hint={text.longTermHint}
          checked={longTermOn}
          disabled={!longTermOn && blocked !== null}
          onChange={(on) => update({ long_term: on ? { retention_days: retention } : undefined }, true)}
        />
        {blocked ? (
          <p className="text-xs text-warn-fg" data-testid="memory-long-term-blocked">
            {blocked}
          </p>
        ) : null}
        {longTermOn ? (
          <>
            <NumberField
              id="memory-retention-days"
              label={text.retention}
              min={1}
              nullable
              placeholder={text.retentionPlaceholder}
              value={retention}
              onChange={(v) => update({ long_term: { retention_days: v } })}
              hint={text.retentionHint}
            />
            <p className="text-xs text-muted" data-testid="memory-retention-note">
              {retention == null ? text.keptForever : text.retentionNote(retention)}
            </p>
            <p className="text-xs text-muted" data-testid="memory-long-term-scope">
              {text.longTermScope}
            </p>
          </>
        ) : null}
      </div>
    </>
  );
}

/** 지식 노드 (D32). Knowledge Base 셀렉트와 상태 칩, 도구 이름(TOOL_NAME_RE, 예약 이름 거부는 검증 표식), 설명, top_k, min_score. */
function KnowledgePanel({ data, update, options }: { data: KnowledgeData; update: (patch: Partial<KnowledgeData>, commit?: boolean) => void; options: InspectorOptions }) {
  const t = useT();
  const text = t.canvas.knowledge;
  const select = t.shared.select;
  const kbs = options.knowledgeBases;
  const kb = kbs.find((k) => k.id === data.knowledge_base_id) ?? null;
  const badge = kb ? knowledgeBaseBadge(kb) : null;
  return (
    <>
      <PanelTitle id="knowledge">{t.canvas.nodeLabels.knowledge}</PanelTitle>
      <Field label={t.canvas.knowledge.labels.base} htmlFor="knowledge-base" hint={text.baseHint}>
        <Select id="knowledge-base" data-testid="knowledge-base" value={data.knowledge_base_id ?? ""} placeholder={select} onChange={(e) => update({ knowledge_base_id: e.target.value }, true)}>
          <option value="">{select}</option>
          {kbs.map((k) => (
            <option key={k.id} value={k.id}>
              {text.option(displayName(k), k.embedding.model, k.status.indexed, k.status.documents)}
            </option>
          ))}
        </Select>
        {kb ? (
          <div className="mt-1">
            <ResourceLink to={`/knowledge-bases/${kb.id}`} label={t.canvas.openResource("Knowledge Base")} testId="knowledge-base-link" />
          </div>
        ) : null}
      </Field>
      {kb && badge ? (
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted" data-testid="knowledge-base-status">
          <Badge variant={badge.variant} data-testid="knowledge-base-state">
            {badge.text}
          </Badge>
          <span>
            {t.canvas.node.documents(kb.status.indexed, kb.status.documents)}
            {kb.status.failed ? ` ${t.shared.failedDocuments(kb.status.failed)}` : ""}
          </span>
          <span className="font-mono">{embeddingText(kb)}</span>
        </div>
      ) : null}
      {!kbs.length ? (
        <p className="text-xs text-warn-fg" data-testid="knowledge-base-empty">
          {text.empty}
        </p>
      ) : null}
      <Field label={text.toolName} htmlFor="knowledge-tool-name" hint={text.toolNameHint}>
        <Input id="knowledge-tool-name" data-testid="knowledge-tool-name" className="font-mono" value={data.tool_name ?? ""} onChange={(e) => update({ tool_name: e.target.value })} />
      </Field>
      <Field label={t.canvas.fields.description} htmlFor="knowledge-description" hint={text.descriptionHint}>
        <Textarea id="knowledge-description" data-testid="knowledge-description" rows={3} className="font-sans" placeholder={`Search the knowledge base '${kb ? displayName(kb) : "…"}' for passages relevant to a query.`} value={data.description ?? ""} onChange={(e) => update({ description: e.target.value })} />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <NumberField id="knowledge-top-k" label="top_k" min={1} value={data.top_k} onChange={(v) => update({ top_k: v ?? 1 })} hint={text.topKHint} />
        <NumberField id="knowledge-min-score" label="min_score" min={0} nullable value={data.min_score} onChange={(v) => update({ min_score: v })} hint={text.minScoreHint} />
      </div>
      {/* 바깥 내용 표시(P43 2절). 없으면 참이다 */}
      <Check id="knowledge-outside" label={text.outside} hint={text.outsideHint} checked={data.outside ?? true} onChange={(outside) => update({ outside }, true)} />
    </>
  );
}

// -- Agent·간선 ------------------------------------------------------------------------------

function ProfilePanel({ names, onChangeNames, readOnlyName, nameErrors, service, onChangeService }: Pick<InspectorProps, "names" | "onChangeNames" | "readOnlyName" | "nameErrors" | "service" | "onChangeService">) {
  const t = useT();
  const text = t.canvas.profile;
  const [tab, setTab] = useState<"basic" | "service">("basic");
  return (
    <>
      <Tabs
        tabs={[
          { id: "basic", label: t.canvas.tabs.basic },
          { id: "service", label: t.canvas.tabs.service },
        ]}
        value={tab}
        onChange={setTab}
        testId="inspector-tabs"
      />
      {tab === "basic" ? (
        <>
          <NameFields testId="ap" placeholders={{ display: text.displayPlaceholder, name: "support-agent" }} value={names} onChange={onChangeNames} readOnlyName={readOnlyName} errors={nameErrors} />
          <p className="text-xs text-muted">{text.selectHint}</p>
        </>
      ) : (
        <>
          <NumberField id="service-session-tokens" label={text.sessionTokens} min={1} nullable value={service.budget.session_tokens} onChange={(v) => onChangeService({ ...service, budget: { ...service.budget, session_tokens: v } })} hint={text.sessionTokensHint} />
          <NumberField id="service-daily-tokens" label={text.dailyTokens} min={1} nullable value={service.budget.daily_tokens} onChange={(v) => onChangeService({ ...service, budget: { ...service.budget, daily_tokens: v } })} hint={t.canvas.needsMemory} />
          <Check id="service-record-bodies" label={text.recordBodies} hint={text.recordBodiesHint} checked={service.observability.record_bodies} onChange={(v) => onChangeService({ ...service, observability: { record_bodies: v } })} />
        </>
      )}
    </>
  );
}

function EdgePanel({ edge, state, onDelete, issues }: { edge: CanvasEdge; state: CanvasState; onDelete: () => void; issues: Issue[] }) {
  const t = useT();
  const labels = t.canvas.nodeLabels;
  const kind = edge.data?.kind ?? EDGE_KINDS[0];
  const src = state.nodes.find((n) => n.id === edge.source);
  const dst = state.nodes.find((n) => n.id === edge.target);
  return (
    <>
      <div className="flex items-center gap-2">
        <span aria-hidden className="inline-block h-[3px] w-6 rounded-full" style={{ background: kindColor(kind) }} />
        <span className="text-sm font-semibold text-heading">{t.canvas.edge.title(kind)}</span>
        <Badge variant="muted" className="font-mono">
          {edge.id}
        </Badge>
      </div>
      <dl className="grid grid-cols-[4rem_1fr] gap-y-1 text-sm">
        <dt className="text-muted">{t.canvas.edge.from}</dt>
        <dd className="font-mono text-xs">
          {edge.source} <span className="text-muted">({src ? labels[src.type ?? NODE_TYPES[0]] : "?"})</span>
        </dd>
        <dt className="text-muted">{t.canvas.edge.to}</dt>
        <dd className="font-mono text-xs">
          {edge.target} <span className="text-muted">({dst ? labels[dst.type ?? NODE_TYPES[0]] : "?"})</span>
        </dd>
      </dl>
      <IssueList issues={issues} />
      <div>
        <Button variant="danger" size="sm" data-testid="inspector-delete" onClick={onDelete}>
          {t.canvas.edge.delete}
        </Button>
      </div>
    </>
  );
}

// -- 인스펙터 ----------------------------------------------------------------------------------

/** 판정 모델(injection_check)로 고를 수 있는 이 그래프의 모델 노드다. 줄은 모델 노드의 이름이고 비어 있으면 id 다. */
function judgeModelOptions(nodes: readonly CanvasNode[]): JudgeModelOption[] {
  return nodes.filter((n) => n.type === "model").map((n) => ({ id: n.id, name: nodeData(n, "model").name || n.id }));
}

/**
 * 노드 종류별 패널. 종류마다 case 가 하나씩이고 default 가 없어서, 노드 종류를 더하고 case 를 빠뜨리면 반환형 검사로
 * 컴파일이 깨진다(checkNodeData 와 같은 방식). 상태(탭·편집기 원문)를 가진 패널은 노드 id 를 key 로 받아 노드마다 새로 선다.
 */
function nodePanel(node: CanvasNode, type: NodeType, update: <T extends NodeData>(patch: Partial<T>, commit?: boolean) => void, props: InspectorProps): ReactElement {
  const { state, options } = props;
  switch (type) {
    case "entry":
      return <EntryPanel data={nodeData(node, "entry")} update={update} />;
    case "agent":
      // Skills 절은 키를 지우기도 하고(빼기) 같은 Skill 을 붙인 다른 노드도 고치므로(최신으로) 덧쓰는 update 말고 이것을 받는다
      return <AgentPanel key={node.id} node={node} data={nodeData(node, "agent")} update={update} state={state} options={options} onChangeNodeData={props.onChangeNodeData} />;
    case "model":
      return <ModelPanel key={node.id} node={node} data={nodeData(node, "model")} update={update} state={state} options={options} />;
    case "tools":
      return <ToolsPanel key={node.id} nodeId={node.id} data={nodeData(node, "tools")} update={update} options={options} hasMemory={mainHasMemory(state.nodes, state.edges)} />;
    case "memory":
      return <MemoryPanel key={node.id} node={node} data={nodeData(node, "memory")} update={update} state={state} options={options} />;
    case "guardrail":
      return <GuardrailPanel data={nodeData(node, "guardrail")} update={update} models={judgeModelOptions(state.nodes)} />;
    case "knowledge":
      return <KnowledgePanel data={nodeData(node, "knowledge")} update={update} options={options} />;
  }
}

export function Inspector(props: InspectorProps) {
  const t = useT();
  const { state, selection, issues, onChangeNodeData, onCommit, onDeleteSelection } = props;
  const node = selection.nodeId ? state.nodes.find((n) => n.id === selection.nodeId) ?? null : null;
  const edge = !node && selection.edgeId ? state.edges.find((e) => e.id === selection.edgeId) ?? null : null;
  const nodeIssues = node ? issues.filter((i) => i.node_id === node.id) : [];
  const edgeIssues = edge ? issues.filter((i) => i.edge_id === edge.id) : [];

  let body: ReactNode;
  let heading: ReactNode;
  if (node) {
    const type = node.type ?? NODE_TYPES[0];
    const update = <T extends NodeData>(patch: Partial<T>, commit = false) => onChangeNodeData(node.id, { ...(node.data as unknown as T), ...patch }, commit);
    heading = (
      <div className="flex items-center gap-2">
        <span className="text-sm font-semibold text-heading">{t.canvas.nodeLabels[type]}</span>
        <Badge variant="muted" className="font-mono" data-testid="inspector-node-id">
          {node.id}
        </Badge>
        <Button variant="ghost" size="sm" className="ml-auto text-err-fg" data-testid="inspector-delete" onClick={onDeleteSelection}>
          {t.common.delete}
        </Button>
      </div>
    );
    body = nodePanel(node, type, update, props);
  } else if (edge) {
    heading = null;
    body = <EdgePanel edge={edge} state={state} onDelete={onDeleteSelection} issues={edgeIssues} />;
  } else {
    heading = <span className="text-sm font-semibold text-heading">{t.canvas.profile.heading}</span>;
    body = <ProfilePanel {...props} />;
  }

  return (
    <aside
      // nokey: 이 안에서는 xyflow 의 삭제 키(Delete·Backspace)가 노드를 지우지 않는다.
      // Select 는 button[role=combobox] 라 xyflow 의 기본 입력 판정에 걸리지 않는다
      className="nokey surface flex w-[22rem] shrink-0 flex-col gap-3 overflow-y-auto rounded-card p-4"
      data-testid="inspector"
      data-selected-node={node?.id ?? undefined}
      data-selected-edge={edge?.id ?? undefined}
      onBlur={(e) => {
        // 패널 밖으로 포커스가 나갈 때만 undo 지점을 만든다. 안에서 옮기는 것은 편집 중이다
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) onCommit();
      }}
    >
      {heading}
      {node ? <IssueList issues={nodeIssues} /> : null}
      {node && props.proposalTexts?.length ? <ProposalTexts texts={props.proposalTexts} /> : null}
      {body}
    </aside>
  );
}
