/**
 * 캔버스 노드 7종의 카드와 간선 컴포넌트. 카드는 평평한 본문 면에 아이콘·이름·종류, 모델 칩·도구 수·Skill 수·HITL 배지·검증 표식을
 * 보인다. 포트는 간선 종류별로 자리가 다르고(lib/agentGraph PORTS, D28) 색은 그 간선 색과 같다. 연결을 끌고 있는
 * 동안에는 출발 포트의 종류로 이을 수 있는 노드만 또렷하고 나머지는 흐려진다 (포트 호환 힌트).
 */
import { BaseEdge, Handle, Position, getBezierPath, useConnection, useEdges, useNodes, type EdgeProps, type NodeProps } from "@xyflow/react";
import { BookOpen, Bot, Database, LogIn, ShieldCheck, Sparkles, Wrench } from "lucide-react";
import type { CSSProperties, ComponentType, ReactNode } from "react";

import { useT } from "@/i18n";
import { DEFAULT_EFFORT, DEFAULT_KNOWLEDGE_TOOL_NAME, EDGE_KINDS, SOURCE_KINDS, attachedModelIds, connectionText, issueText, longTermEnabled, nodePorts, type Issue, type NodePort, type NodeType } from "@/lib/agentGraph";
import { isPinBehind, pinText, pinsOf } from "@/lib/agentSkills";
import { unresolvedLabel } from "@/lib/names";
import { kindColor, kindTint } from "@/lib/kindColor";
import { cn, ownValue } from "@/lib/utils";

import { useCanvasContext } from "./context";
import { asNodeData, handleKind, nodeData, placedEdge, type CanvasEdge, type CanvasNode, type NodeDataOf, type ProposalMark } from "./types";

export const NODE_ICONS: Record<NodeType, ComponentType<{ className?: string; size?: number }>> = {
  entry: LogIn,
  agent: Bot,
  model: Sparkles,
  tools: Wrench,
  memory: Database,
  guardrail: ShieldCheck,
  knowledge: BookOpen,
};

const POSITION: Record<NodePort["side"], Position> = { left: Position.Left, right: Position.Right, top: Position.Top, bottom: Position.Bottom };

/** 제안 표시의 색. 승인 카드(`ProposalSummary.tsx` `MARK_VARIANT`)와 같이 더한 것은 ok, 바꾼 것은 info 다. */
const PROPOSAL_TONE: Record<ProposalMark, "ok" | "info"> = { added: "ok", changed: "info" };

function Chip({ children, tone = "default", testId, title }: { children: ReactNode; tone?: "default" | "warn" | "err" | "accent" | "ok" | "info"; testId?: string; title?: string }) {
  const cls = {
    default: "border-line bg-surface-strong text-fg-soft",
    info: "border-info-line bg-info-soft text-info-fg",
    warn: "border-warn-line bg-warn-soft text-warn-fg",
    err: "border-err-line bg-err-soft text-err-fg",
    accent: "border-accent-line bg-accent-soft text-heading",
    ok: "border-ok-line bg-ok-soft text-ok-fg",
  }[tone];
  return (
    <span className={cn("inline-flex max-w-full items-center gap-1 truncate rounded-badge border px-1.5 py-0.5 text-[10.5px] font-medium", cls)} data-testid={testId} title={title}>
      {children}
    </span>
  );
}

/**
 * 포트 점. 핸들 id 는 간선 종류이고 색도 그 간선 색이다. 출발점(source)만 끌 수 있고 도착점(target)은 간선이 닿는
 * 자리를 보일 뿐이다(드롭은 카드를 덮는 투명 핸들이 받는다). 역할에 맞지 않아 숨은 포트는 보이지도 않고 끌 수도
 * 없지만 핸들로는 남아 간선이 그 자리에 그려진다.
 */
function PortHandle({ nodeId, port }: { nodeId: string; port: NodePort }) {
  const t = useT();
  const along = port.side === "left" || port.side === "right" ? { top: `${port.at * 100}%` } : { left: `${port.at * 100}%` };
  const style: CSSProperties = { ...along, background: kindColor(port.kind) };
  // 도착 포트에서도 끌 수 있다. 쌍마다 맞는 방향이 하나뿐이라(D28) 어느 쪽에서 끌든 같은 간선이 나오므로,
  // 모델을 에이전트에 붙이러 모델 쪽에서 끄는 것도 된다. 방향은 `resolveConnection` 이 맞춘다
  const label = port.hidden ? undefined : t.canvas.node.port(port.kind);
  return (
    <Handle
      type={port.direction}
      position={POSITION[port.side]}
      id={port.kind}
      data-testid={`port-${nodeId}-${port.kind}-${port.direction === "source" ? "out" : "in"}`}
      data-port-kind={port.kind}
      title={label}
      isConnectableStart={!port.hidden}
      isConnectableEnd={port.direction === "target"}
      className={cn("!h-3 !w-3 !border-2 !border-app", port.hidden && "!pointer-events-none !opacity-0")}
      style={style}
    />
  );
}

/**
 * 카드 오른쪽 위 검증 표식. 오류는 `!`, 경고는 `?` 로 글자가 다르다. 색만 다르면 색을 구분하기 어려운 사람에게
 * "고쳐야 저장되는 것" 과 "그냥 알려 주는 것" 이 같아 보인다. 종류와 개수는 aria-label 과 title 에도 싣는다.
 */
function IssueMark({ id, issues }: { id: string; issues: Issue[] }) {
  const t = useT();
  const text = t.canvas.node;
  if (!issues.length) return null;
  const errors = issues.filter((i) => i.level === "error").length;
  const count = errors || issues.length;
  const label = errors ? text.errors(errors) : text.warnings(issues.length);
  return (
    <span
      // 오류는 채운 색이다. 테두리만 두른 칩은 카드 안의 다른 칩들과 섞여 눈에 안 띈다
      className={cn("inline-flex h-5 min-w-5 items-center justify-center rounded-full border px-1 text-[10.5px] font-bold", errors ? "border-err bg-err text-on-accent" : "border-warn-line bg-warn-soft text-warn-fg")}
      data-testid={`node-mark-${id}`}
      data-level={errors ? "error" : "warning"}
      aria-label={label}
      title={[label, ...issues.map((issue) => issueText(t, issue))].join("\n")}
    >
      {errors ? "!" : "?"}
      {count > 1 ? count : ""}
    </span>
  );
}

/** 카드 공통 틀. 자식은 칩 줄이다. */
function NodeCard({ id, type, role, title, subtitle, selected, children }: { id: string; type: NodeType; role?: "main" | "sub"; title: string; subtitle: string; selected?: boolean; children?: ReactNode }) {
  const t = useT();
  const ctx = useCanvasContext();
  const issues = ctx.nodeIssues.get(id) ?? [];
  const errors = issues.filter((i) => i.level === "error").length;
  const warnings = issues.length - errors;
  const proposal = ctx.proposalMarks.get(id);
  const conn = useConnection();
  const fromOther = conn.inProgress && conn.fromNode && conn.fromNode.id !== id;
  const fromKind = handleKind(conn.fromHandle?.id);
  const problem = fromOther ? ctx.connectionHint(conn.fromNode!.id, fromKind, id) : null;
  const hint = problem ? connectionText(t, problem) : null;
  const dim = fromOther && problem !== null;
  const bright = fromOther && problem === null;
  const ports = nodePorts(type, role);
  const hasTarget = ports.some((p) => p.direction === "target");
  const Icon = NODE_ICONS[type];
  const accent = kindColor(type);
  return (
    // 힌트는 카드 밖(형제)이다. 카드 자신이 opacity-35 로 흐려지므로 안에 두면 이유가 있을 때만 뜨는 문장이
    // 늘 35% 투명도로 그려진다
    <div className="relative w-72">
      <div
        data-testid={`node-${id}`}
        data-node-type={type}
        data-selected={selected ? "true" : undefined}
        data-issue={errors ? "error" : warnings ? "warning" : undefined}
        data-proposal={proposal}
        title={hint ?? undefined}
        className={cn(
          "surface relative w-72 rounded-tile px-3.5 py-3 text-fg transition-[opacity,box-shadow,border-color]",
          selected && "border-accent shadow-raised",
          // 오류는 테두리가 아니라 링으로 낸다. 테두리를 쓰면 선택 표시(쪽빛 테두리)를 덮어
          // 고른 노드인지 알 수 없게 된다. 링은 테두리 바깥이라 둘이 같이 보인다
          errors ? "ring-2 ring-err" : warnings ? "ring-1 ring-warn" : null,
          dim && "opacity-35",
          bright && "ring-2 ring-kind-entry",
        )}
        // 오류면 카드 배경도 붉게 물들인다. backgroundImage 라 surface 의 background-color 를 덮지 않고
        // 그 위에 한 겹 깔린다. 링만으로는 노드가 작아 훑어볼 때 눈에 덜 띈다(실측)
        style={{
          borderLeftWidth: 3,
          borderLeftColor: accent,
          ...(errors ? { backgroundImage: "linear-gradient(var(--ui-err-wash), var(--ui-err-wash))" } : null),
        }}
      >
        {/* 연결 대상은 카드 전체다. 점에서 끌어 카드 아무 곳에나 놓으면 붙도록 카드를 덮는 투명 target 핸들을 두고(D26),
            간선이 실제로 닿는 자리는 종류별 포트 점이다. 출발점은 종류별 source 포트 점이다(D28). */}
        {hasTarget ? (
          <Handle
            type="target"
            position={Position.Left}
            id="card"
            className="!pointer-events-auto !absolute !inset-0 !h-full !w-full !translate-x-0 !translate-y-0 !rounded-tile !border-0 !bg-transparent"
            style={{ left: 0, top: 0, transform: "none", zIndex: conn.inProgress ? 20 : -1 }}
            isConnectableStart={false}
          />
        ) : null}
        {ports.map((p) => (
          <PortHandle key={`${p.kind}-${p.direction}`} nodeId={id} port={p} />
        ))}
        <div className="flex items-start gap-2.5">
          <span className="mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-tile" style={{ background: kindTint(accent), color: accent }} aria-hidden>
            <Icon size={15} />
          </span>
          <div className="min-w-0 flex-1">
            <div className="truncate text-[13px] font-semibold text-heading" data-testid={`node-title-${id}`}>
              {title}
            </div>
            <div className="truncate text-[11.5px] text-muted">{subtitle}</div>
          </div>
          {/* 청지기의 제안이 더하거나 바꾸는 노드다 (P48 6.5절) */}
          {proposal ? (
            <Chip tone={PROPOSAL_TONE[proposal]} testId={`node-proposal-${id}`}>
              {t.canvas.proposalMarks[proposal]}
            </Chip>
          ) : null}
          <IssueMark id={id} issues={issues} />
        </div>
        {children ? <div className="mt-2.5 flex flex-wrap gap-1">{children}</div> : null}
      </div>
      {/* 끌고 있는 동안에는 툴팁이 뜨지 않으므로 이유를 카드 아래에 그대로 적는다. 두 줄까지 보인다 */}
      {hint ? (
        <div className="pointer-events-none absolute left-0 right-0 top-full mt-1 line-clamp-2 text-center text-[10px] leading-snug text-err-fg" data-testid={`node-hint-${id}`}>
          {hint}
        </div>
      ) : null}
    </div>
  );
}

function EntryNode({ id, data, selected }: NodeProps<CanvasNode>) {
  const t = useT();
  const text = t.canvas.node;
  const d = asNodeData(data, "entry");
  return (
    <NodeCard id={id} type="entry" title={t.canvas.nodeLabels.entry} subtitle={t.canvas.nodeLabels.entry} selected={selected}>
      <Chip>{text.ttl(d.session_ttl_seconds)}</Chip>
      <Chip>{text.turns(d.max_turns)}</Chip>
      {d.rate_limit ? <Chip tone="accent">{text.perMinute(d.rate_limit.per_user_per_minute)}</Chip> : null}
      {d.greeting ? <Chip tone="ok">{t.shared.chatPanel.greeting}</Chip> : null}
    </NodeCard>
  );
}

/** 모델 노드 provider 칩의 글. provider 가 비어 있으면 null 이다. */
function providerText(d: NodeDataOf<"model">, lookup: ReturnType<typeof useCanvasContext>["lookup"]): string | null {
  if (!d.provider) return null;
  const kind = lookup.providerKind(d.provider);
  const model = d.model ?? lookup.providerModel(d.provider);
  return `${d.provider}${model ? ` · ${model}` : ""}${kind ? ` (${kind})` : ""}`;
}

function AgentNode({ id, data, selected }: NodeProps<CanvasNode>) {
  const t = useT();
  const text = t.canvas.node;
  const d = asNodeData(data, "agent");
  const ctx = useCanvasContext();
  const edges = useEdges<CanvasEdge>();
  const nodes = useNodes<CanvasNode>();
  const out = edges.filter((e) => e.source === id);
  const toolNodes = out.filter((e) => e.data?.kind === "tools").map((e) => nodes.find((n) => n.id === e.target)).filter(Boolean) as CanvasNode[];
  const subs = out.filter((e) => e.data?.kind === "delegate").length;
  const knowledge = out.filter((e) => e.data?.kind === "knowledge").length;
  const hitl = toolNodes.some((n) => Object.keys(nodeData(n, "tools").approval ?? {}).length > 0);
  const modelNode = attachedModelIds(id, out.map(placedEdge))
    .map((mid) => nodes.find((n) => n.id === mid))
    .find((n) => n?.type === "model");
  const model = modelNode ? nodeData(modelNode, "model") : null;
  const provider = model ? providerText(model, ctx.lookup) : null;
  // D64: 붙인 Skill 은 카드에 개수만 보이고 이름은 칩의 툴팁에 둔다. 더 새 리비전이 있는 것이 있으면 경고색이다
  const skills = pinsOf(d).map((pin) => ({ pin, info: ctx.lookup.skill(pin.id) }));
  return (
    <NodeCard id={id} type="agent" role={d.role} title={d.name || t.canvas.unnamed} subtitle={text.role[d.role]} selected={selected}>
      {model ? (
        <Chip tone={provider ? "accent" : "warn"} testId={`node-model-${id}`}>
          {model.name || modelNode!.id} · {provider ?? t.canvas.providerUnset}
        </Chip>
      ) : (
        <Chip tone="err" testId={`node-model-${id}`}>
          {text.noModel}
        </Chip>
      )}
      <Chip testId={`node-tools-${id}`}>{text.tools(toolNodes.length)}</Chip>
      {knowledge ? <Chip testId={`node-knowledge-${id}`}>{text.knowledge(knowledge)}</Chip> : null}
      {skills.length ? (
        <Chip tone={skills.some((s) => isPinBehind(s.pin, s.info)) ? "warn" : "default"} testId={`node-skills-${id}`} title={skills.map((s) => pinText(t, s.pin, s.info, ctx.lookup.ready)).join("\n")}>
          {text.skills(skills.length)}
        </Chip>
      ) : null}
      {subs ? <Chip>{text.subs(subs)}</Chip> : null}
      {hitl ? (
        <Chip tone="warn" testId={`node-hitl-${id}`}>
          {text.hitl}
        </Chip>
      ) : null}
      {d.response_format !== "text" ? <Chip>{text.json}</Chip> : null}
    </NodeCard>
  );
}

function ModelNode({ id, data, selected }: NodeProps<CanvasNode>) {
  const t = useT();
  const text = t.canvas.node;
  const d = asNodeData(data, "model");
  const ctx = useCanvasContext();
  const edges = useEdges<CanvasEdge>();
  const users = edges.filter((e) => e.target === id && e.data?.kind === "model").length;
  const provider = providerText(d, ctx.lookup);
  return (
    <NodeCard id={id} type="model" title={d.name || t.canvas.unnamed} subtitle={t.canvas.nodeLabels.model} selected={selected}>
      {provider ? (
        <Chip tone="accent" testId={`node-provider-${id}`}>
          {provider}
        </Chip>
      ) : (
        <Chip tone="warn" testId={`node-provider-${id}`}>
          {t.canvas.providerUnset}
        </Chip>
      )}
      <Chip>
        {text.effort} {d.reasoning?.effort ?? DEFAULT_EFFORT}
      </Chip>
      {d.temperature != null ? <Chip>
          {text.temp} {d.temperature}
        </Chip> : null}
      {d.fallback ? <Chip testId={`node-fallback-${id}`}>{d.fallback.provider ? text.fallback(d.fallback.provider) : text.fallbackUnset}</Chip> : null}
      <Chip tone={users > 1 ? "accent" : "default"} testId={`node-agents-${id}`}>
        {users > 1 ? text.sharedBy(users) : text.agents(users)}
      </Chip>
    </NodeCard>
  );
}

function ToolsNode({ id, data, selected }: NodeProps<CanvasNode>) {
  const t = useT();
  const text = t.canvas.node;
  const d = asNodeData(data, "tools");
  const ctx = useCanvasContext();
  // 목록을 아직 못 받았으면 못 찾은 것이 아니라 모르는 것이다. 여는 동안 멀쩡한 참조를 지워진 것으로 말하지 않는다
  const name = d.source?.id ? (ctx.lookup.sourceName(d.source.kind, d.source.id) ?? unresolvedLabel(t, ctx.lookup.ready)) : null;
  const approvals = Object.keys(d.approval ?? {}).length;
  return (
    <NodeCard id={id} type="tools" title={name ?? text.sourceUnset} subtitle={SOURCE_KINDS[d.source?.kind ?? "gateway_instance"].label} selected={selected}>
      <Chip testId={`node-tools-${id}`}>{d.allow_tools?.length ? text.allowed(d.allow_tools.length) : text.allTools}</Chip>
      {approvals ? (
        <Chip tone="warn" testId={`node-hitl-${id}`}>
          {text.hitl} {approvals}
        </Chip>
      ) : null}
      {Object.keys(d.timeouts ?? {}).length ? <Chip>{text.timeouts(Object.keys(d.timeouts).length)}</Chip> : null}
    </NodeCard>
  );
}

function MemoryNode({ id, data, selected }: NodeProps<CanvasNode>) {
  const t = useT();
  const text = t.canvas.node;
  const d = asNodeData(data, "memory");
  const ctx = useCanvasContext();
  const dsId = d.checkpointer?.data_service_id ?? "";
  const name = dsId ? (ctx.lookup.dataServiceName(dsId) ?? unresolvedLabel(t, ctx.lookup.ready)) : null;
  // D33: 체크포인터는 postgres 나 redis 다. 카드 폭이 좁으므로 고른 Database 의 kind 를 그대로 보인다
  const kind = dsId ? ctx.lookup.dataServiceKind(dsId) : null;
  return (
    <NodeCard id={id} type="memory" title={name ?? t.canvas.databaseUnset} subtitle={kind ? text.checkpointerOf(kind) : text.checkpointer} selected={selected}>
      <Chip>{text.sessionIndex}</Chip>
      <Chip>{text.sharedCounter}</Chip>
      {/* D34: 장기 기억은 켤 때만 보인다. 보관 기간이 없으면 안 지운다는 뜻이라 그것도 칩에 적는다 */}
      {longTermEnabled(d) ? (
        <Chip tone="accent" testId={`node-long-term-${id}`}>
          {text.longTerm(d.long_term?.retention_days == null ? text.keepForever : t.common.time.days(d.long_term.retention_days))}
        </Chip>
      ) : null}
    </NodeCard>
  );
}

function GuardrailNode({ id, data, selected }: NodeProps<CanvasNode>) {
  const t = useT();
  const text = t.canvas.node;
  const d = asNodeData(data, "guardrail");
  const position = ownValue(t.canvas.guardrail.positions, d.position) ?? d.position;
  return (
    <NodeCard id={id} type="guardrail" title={text.guardrail(position)} subtitle={t.canvas.nodeLabels.guardrail} selected={selected}>
      <Chip>{text.rules(d.rules?.length ?? 0)}</Chip>
      <Chip tone={d.on_violation === "block" ? "err" : "warn"}>{t.canvas.guardrail.violations[d.on_violation === "block" ? "block" : "flag"]}</Chip>
      {(d.rules ?? []).map((r, i) => (
        <Chip key={i}>{r.kind}</Chip>
      ))}
    </NodeCard>
  );
}

/** 지식 노드 (D32). 제목은 Knowledge Base 표시 이름, 부제는 도구 이름, 칩은 `indexed/documents` 다. */
function KnowledgeNode({ id, data, selected }: NodeProps<CanvasNode>) {
  const t = useT();
  const text = t.canvas.node;
  const d = asNodeData(data, "knowledge");
  const ctx = useCanvasContext();
  const kbId = d.knowledge_base_id ?? "";
  const name = kbId ? (ctx.lookup.knowledgeBaseName(kbId) ?? unresolvedLabel(t, ctx.lookup.ready)) : null;
  const status = kbId ? ctx.lookup.knowledgeBaseStatus(kbId) : null;
  return (
    <NodeCard id={id} type="knowledge" title={name ?? t.canvas.knowledgeBaseUnset} subtitle={d.tool_name || DEFAULT_KNOWLEDGE_TOOL_NAME} selected={selected}>
      {status ? (
        <Chip tone={status.indexed ? "ok" : "warn"} testId={`node-kb-${id}`}>
          {text.documents(status.indexed, status.documents)}
        </Chip>
      ) : (
        <Chip tone="warn" testId={`node-kb-${id}`}>
          {kbId ? text.unknownKnowledgeBase : t.canvas.unset}
        </Chip>
      )}
      <Chip>top_k {d.top_k ?? 5}</Chip>
      {d.min_score != null ? <Chip>min_score {d.min_score}</Chip> : null}
    </NodeCard>
  );
}

export const nodeTypes = {
  entry: EntryNode,
  agent: AgentNode,
  model: ModelNode,
  tools: ToolsNode,
  memory: MemoryNode,
  guardrail: GuardrailNode,
  knowledge: KnowledgeNode,
};

/** 종류별 색의 베지어 간선. 검증 오류가 있으면 점선이다. */
function KindEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data, selected, markerEnd }: EdgeProps<CanvasEdge>) {
  const ctx = useCanvasContext();
  const kind = data?.kind ?? EDGE_KINDS[0];
  const [path] = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition });
  const issues = ctx.edgeIssues.get(id) ?? [];
  const hasError = issues.some((i) => i.level === "error");
  return (
    <BaseEdge
      id={id}
      path={path}
      markerEnd={markerEnd}
      interactionWidth={18}
      data-issue={hasError ? "error" : undefined}
      // 오류인 간선은 색과 점선으로 말한다. 노드가 링을 쓰는 것과 같은 뜻이고 선에는 링을 못 두른다.
      // 고른 간선은 쪽빛으로 굵게 그린다. 빛을 두르지 않으므로 색이 갈래 색과 갈려야 보인다
      style={{ stroke: hasError ? "var(--ui-err)" : selected ? "var(--ui-accent)" : kindColor(kind), strokeWidth: selected ? 2.6 : hasError ? 2.4 : 1.8, strokeDasharray: hasError ? "6 4" : undefined, opacity: selected ? 1 : 0.9 }}
    />
  );
}

export const edgeTypes = { kind: KindEdge };

/** 범례 안의 키 안내. 글쇠는 고정폭 칩으로, 하는 일은 그 옆에 적는다. */
function KeyHint({ keys, children }: { keys: string[]; children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1">
      {keys.map((k) => (
        <kbd key={k} className="rounded-[4px] border border-line bg-surface-strong px-1 py-px font-sans text-[10px] leading-[1.4] text-fg">
          {k}
        </kbd>
      ))}
      <span>{children}</span>
    </span>
  );
}

/**
 * 간선 색 범례와 선택 방법. 여러 개 고르기는 눌러 보기 전에는 알 수 없어서 캔버스가 직접 말한다.
 * 노드가 하나도 없으면 고를 것이 없으므로 키 안내는 내지 않는다.
 */
export function EdgeLegend({ showSelectHint }: { showSelectHint?: boolean }) {
  const t = useT();
  const text = t.canvas.legend;
  return (
    <div className="surface flex max-w-[min(56ch,60vw)] flex-col gap-1 rounded-chip px-3 py-1.5 text-[10.5px] text-muted" data-testid="canvas-legend">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {EDGE_KINDS.map((k) => (
          <span key={k} className="inline-flex items-center gap-1.5">
            <span aria-hidden className="inline-block h-[3px] w-4 rounded-full" style={{ background: kindColor(k) }} />
            {t.canvas.edgeLabels[k]}
          </span>
        ))}
      </div>
      {showSelectHint ? (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-line pt-1 text-dim" data-testid="canvas-select-hint">
          <KeyHint keys={["⇧"]}>{text.areaSelect}</KeyHint>
          <KeyHint keys={["⌘"]}>{text.multiSelect}</KeyHint>
          <KeyHint keys={["Delete"]}>{t.common.delete}</KeyHint>
        </div>
      ) : null}
    </div>
  );
}
