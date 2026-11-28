/**
 * Agent 캔버스 (D26 F4). Agent 의 정의를 그리는 화면이다.
 *
 * 정의와 배포가 한 리소스가 되면서(D42) 이 화면은 두 자리에 선다. `/agents/new` 에서는 화면 하나로 서서
 * 새 Agent 를 만들고, 저장된 뒤에는 상세의 정의 탭 안에 들어간다(`embedded`). 어느 쪽이든 왼쪽 팔레트,
 * 가운데 캔버스, 오른쪽 인스펙터, 아래 테스트 채팅 서랍은 같다.
 *
 * 이 파일에는 편집 상태와 mutation 배선만 남기고, 읽기(쿼리와 어댑터)는 useCanvasData 가 맡는다.
 * 저장 본문은 `{name, display_name, graph, service, layout}` 이고 layout 만 바뀐 저장은 리비전을 만들지 않는다.
 * 검증 배지는 편집 중에는 클라이언트 규칙, 검증·저장 뒤에는 서버 응답을 보인다.
 * 단축키: ⌘Z 되돌리기, ⇧⌘Z 다시 하기, ⌘S 저장, Delete 선택 삭제(캔버스가 처리).
 *
 * 주소에 청지기의 제안(proposal·session)이 있으면 그 제안을 기준 위의 저장하지 않은 편집으로 올린다(P48 6.5절,
 * useCanvasProposal). 더하고 바꾸는 노드에 표시를 달고, 지우는 노드는 안내 줄(ProposalBar)에 적고, 바뀌는 글의 앞과 뒤
 * 전체는 인스펙터가 보인다. 좌표가 없는 노드는 자동 배치로 놓는다. 저장 길은 둘이다. "제안대로 저장" 은 청지기의 호출을
 * 승인하고, 캔버스의 저장은 사람의 판을 기준 리비전(expect_revision)으로 저장한 뒤 그 호출을 고정 사유로 닫는다.
 * 저장하지 않은 변경이 있다는 것은 턴의 문맥으로 청지기에게 간다.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";

import { ApiError } from "@/api/client";
import { createAgent, getAgentRevision, putAgent, validateAgentGraph } from "@/api/resources";
import type { AgentSaved, AgentTemplate, AgentUpdateBody, AgentValidateResponse, AssistantProposalView } from "@/api/types";
import { ErrorBody, ErrorMessage } from "@/components/ErrorMessage";
import { isNameFieldError, nameFieldErrors, type NameValue } from "@/components/NameFields";
import { AgentCanvas } from "@/components/canvas/AgentCanvas";
import { Inspector } from "@/components/canvas/Inspector";
import { Palette } from "@/components/canvas/Palette";
import { autoLayout, placeUnplaced } from "@/components/canvas/autoLayout";
import { EMPTY_CANVAS, makeEdge, makeNode, placedEdge, toCanvas, toPlaced, type CanvasEdge, type CanvasState } from "@/components/canvas/types";
import { useCanvasHistory } from "@/components/canvas/useCanvasHistory";
import { AgentIllustration } from "@/components/illustrations";
import { Alert } from "@/components/ui/card";
import { type Messages, messages, useT } from "@/i18n";
import {
  CONTEXT_ISSUE_CODES,
  DEFAULT_SERVICE,
  MODEL_ATTACH_OFFSET,
  attachedModelIds,
  canonicalJson,
  countIssues,
  defaultNodeData,
  deserializeGraph,
  fromTemplate,
  mainAgentNode,
  serializeGraph,
  toApiGraph,
  toServiceSettings,
  uniqueEdgeId,
  uniqueNodeId,
  validateGraph,
  type GraphDocument,
  type Issue,
  type ModelData,
  type NodeData,
  type NodeType,
  type PlacedEdge,
  type PlacedNode,
  type ServiceSettings,
  type ApiGraph,
  type Graph,
  type Layout,
} from "@/lib/agentGraph";
import { canvasTarget, setCanvasUnsaved } from "@/lib/assistant";
import { displayName } from "@/lib/names";
import { useTenant } from "@/lib/tenant";
import { useTheme } from "@/lib/theme";
import { useUnsavedGuard } from "@/lib/unsavedGuard";

import { CanvasHeader } from "./CanvasHeader";
import { ProposalBar } from "./ProposalBar";
import { TestChatDrawer } from "./TestChatDrawer";
import { useCanvasData } from "./useCanvasData";
import { useCanvasProposal } from "./useCanvasProposal";
import { useProposalContent } from "./useProposalContent";

interface ServerIssues {
  key: string;
  issues: Issue[];
}

/**
 * 저장 본문의 동일성 키. 편집 중인 것(bodyKey)과 마지막으로 저장하거나 불러온 것(savedKey)을 이 하나로 만든다.
 * 두 자리가 따로 적으면 한쪽에만 필드가 늘어 `dirty` 가 켜진 채 풀리지 않는다.
 */
function docKey(doc: GraphDocument, service: ServiceSettings, names: NameValue): string {
  return canonicalJson({ graph: doc.graph, service, layout: doc.layout, display_name: names.displayName.trim(), name: names.name.trim() });
}

/** 배치를 뺀 내용의 열쇠. 불러온 청지기의 제안 위에서 사람이 고쳤는지 본다(노드를 옮긴 것은 고친 것이 아니다). */
function contentKey(graph: Graph, service: ServiceSettings, names: NameValue): string {
  return canonicalJson({ graph, service, display_name: names.displayName.trim(), name: names.name.trim() });
}

/**
 * 저장된 그래프와 배치를 캔버스에 올릴 노드로. 저장된 자리가 없는 노드(청지기가 만들거나 더한 노드)는 자동 배치로 놓는다
 * (P48 6.7절). 격자에 두면 다른 노드 위에 겹친다.
 */
function placeLoaded(graph: ApiGraph, layout: Layout | null | undefined): { nodes: PlacedNode[]; edges: PlacedEdge[] } {
  const { nodes, edges } = deserializeGraph(graph, layout);
  const positions = new Map(placeUnplaced(toCanvas(nodes, edges), new Set(Object.keys(layout ?? {}))).map((n) => [n.id, n.position]));
  return { nodes: nodes.map((n) => ({ ...n, position: positions.get(n.id) ?? n.position })), edges };
}

/** 캔버스 위 안내 한 줄. 문장 대신 무엇이 일어났는지와 그 값을 들고, 그릴 때 화면 언어로 문장을 만든다. */
type CanvasNotice =
  | { code: "template"; name: string }
  | { code: "created" | "revisionCreated" | "revisionLoaded"; revision: number }
  | { code: "layoutOnly" | "nameRequired" };

function noticeText(t: Messages, notice: CanvasNotice): string {
  const text = t.agents.canvasPage.notice;
  switch (notice.code) {
    case "template":
      return text.template(notice.name);
    case "layoutOnly":
    case "nameRequired":
      return text[notice.code];
    default:
      return text[notice.code](notice.revision);
  }
}

function isEditable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable;
}

export function AgentCanvasPage({ embedded = false }: { embedded?: boolean } = {}) {
  const t = useT();
  const text = t.agents.canvasPage;
  const { id } = useParams();
  const isNew = !id;
  const tenant = useTenant();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [search] = useSearchParams();
  const theme = useTheme();

  const history = useCanvasHistory(EMPTY_CANVAS);
  const { state, set, reset, commit, undo, redo, canUndo, canRedo } = history;
  const [service, setService] = useState<ServiceSettings>(DEFAULT_SERVICE);
  const [names, setNames] = useState<NameValue>({ displayName: "", name: "" });
  const [selection, setSelection] = useState<{ nodeId: string | null; edgeId: string | null }>({ nodeId: null, edgeId: null });
  const [serverIssues, setServerIssues] = useState<ServerIssues | null>(null);
  const [savedKey, setSavedKey] = useState<string | null>(null);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [fitViewKey, setFitViewKey] = useState(0);
  const [focus, setFocus] = useState<{ id: string | null; key: number; keepZoom: boolean }>({ id: null, key: 0, keepZoom: false });
  const [notice, setNotice] = useState<CanvasNotice | null>(null);
  const [viewingRevision, setViewingRevision] = useState<number | null>(null);

  // 문서와 검증. 검증 문맥은 편집 중인 그래프가 고정한 Skill 리비전까지 읽으므로 문서 다음에 받는다
  const doc = useMemo(() => {
    const { nodes, edges } = toPlaced(state);
    return serializeGraph(nodes, edges);
  }, [state]);
  const { options, ctx, context, lookup, templates, agent } = useCanvasData(tenant, id, doc.graph);
  const graphKey = useMemo(() => canonicalJson({ graph: doc.graph, service }), [doc.graph, service]);
  // 참조 목록을 아직 못 읽었으면 그 목록으로만 알 수 있는 표식은 보류한다. 빈 목록을 "참조가 지워졌다" 로 읽어
  // 거짓 오류를 내고 저장까지 잠그던 자리다 (서버는 이 검사를 저장 때 다시 한다)
  const clientIssues = useMemo(() => {
    const all = validateGraph(doc.graph, service, ctx);
    return context.ready ? all : all.filter((i) => !CONTEXT_ISSUE_CODES.has(i.code));
  }, [doc.graph, service, ctx, context.ready]);
  const showServer = serverIssues !== null && serverIssues.key === graphKey;
  const issues = showServer ? serverIssues.issues : clientIssues;
  const errorCount = countIssues(issues).errors;
  const hasErrors = errorCount > 0;
  const bodyKey = useMemo(() => docKey(doc, service, names), [doc, service, names]);
  const dirty = savedKey !== bodyKey;
  // 저장한 적 있는 문서가 바뀌었거나, 새 캔버스에 그린 것이 있다. 새로 연 빈 캔버스는 잃을 것이 없다
  const unsaved = dirty && (Boolean(agent.data) || Boolean(state.nodes.length));
  const currentContentKey = useMemo(() => contentKey(doc.graph, service, names), [doc.graph, service, names]);

  /** 문서 하나를 캔버스에 올린다. 불러오기·템플릿·리비전이 함께 쓴다. 되돌리기 이력과 서버 표식을 비우고 화면을 다시 맞춘다. */
  const showGraph = useCallback(
    (nodes: PlacedNode[], edges: PlacedEdge[], svc: ServiceSettings) => {
      reset(toCanvas(nodes, edges));
      setService(svc);
      setServerIssues(null);
      setFitViewKey((k) => k + 1);
    },
    [reset],
  );

  // 문서 불러오기 (id 마다 한 번)
  useEffect(() => {
    if (isNew || !agent.data || loadedFor === agent.data.id) return;
    const p = agent.data;
    const { nodes, edges } = placeLoaded(p.graph, p.layout);
    const svc = toServiceSettings(p.service);
    const loadedNames = { displayName: displayName(p), name: p.name };
    showGraph(nodes, edges, svc);
    setNames(loadedNames);
    setLoadedFor(p.id);
    setViewingRevision(null);
    setSavedKey(docKey(serializeGraph(nodes, edges), svc, loadedNames));
  }, [isNew, agent.data, loadedFor, showGraph]);

  // 새 Agent: ?template= 으로 바로 템플릿을 불러온다
  const templateParam = search.get("template");
  useEffect(() => {
    if (!isNew || loadedFor || !templates.list.length) return;
    const template = templateParam ? (templates.list.find((x) => x.id === templateParam) ?? null) : null;
    if (template) applyTemplate(template, true);
    setLoadedFor("new");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isNew, templates.list, templateParam, loadedFor]);

  function applyTemplate(template: AgentTemplate, silent = false) {
    if (!silent && state.nodes.length && !window.confirm(messages().agents.canvasPage.replaceWithTemplate)) return;
    content.setAside();
    const { nodes, edges, service: svc } = fromTemplate(template);
    showGraph(nodes, edges, svc);
    if (isNew && !names.name) setNames({ displayName: template.display_name, name: template.id.replace(/-agent$/, "") || template.id });
    setSelection({ nodeId: null, edgeId: null });
    setNotice({ code: "template", name: template.display_name });
  }

  // 청지기의 제안 (P48 6.5절). 이 캔버스의 대상의 제안만 열고 같은 대상의 나중 제안만 불러오자고 한다
  const proposal = useCanvasProposal({
    tenant,
    target: canvasTarget(id ?? null),
    onApproved: (saved) => {
      // 청지기의 호출이 저장했다. 새 Agent 는 그 상세로 가고, 바꾸기는 저장된 리비전을 다시 올린다
      if (isNew) {
        navigate(`/agents/${saved.id}`, { replace: true });
        return;
      }
      void queryClient.invalidateQueries({ queryKey: ["agents"] }).then(() => {
        content.forget();
        setLoadedFor(null);
        setNotice({ code: "revisionCreated", revision: saved.revision });
      });
    },
  });
  // 바꾸기는 저장된 Agent 를 먼저 올린 뒤 그 위에 얹는다. 새 Agent 는 템플릿을 고른 주소가 아니면 바로 올린다
  const content = useProposalContent({
    tenant,
    agentId: id,
    agent: agent.data,
    open: proposal.state.kind === "open" ? proposal.state.view : null,
    ready: isNew ? !templateParam || loadedFor === "new" : loadedFor === id,
    contentKey: currentContentKey,
    unsaved,
    selectedNodeId: selection.nodeId,
    draw: drawProposal,
  });

  /**
   * 제안을 기준 위의 저장하지 않은 편집으로 그리고 그린 내용의 열쇠를 돌려준다. 저장된 자리가 있는 노드는 그 자리에, 없는
   * 노드는 자동 배치로 놓는다(새 Agent 는 저장된 자리가 없다).
   */
  function drawProposal(v: AssistantProposalView): string {
    const { nodes, edges } = placeLoaded(v.graph, v.kind === "update" ? agent.data?.layout : null);
    const svc = toServiceSettings(v.service);
    const nextNames = v.kind === "create" ? { displayName: v.display_name ?? "", name: v.name } : { ...names, displayName: v.display_name ?? names.displayName };
    showGraph(nodes, edges, svc);
    setNames(nextNames);
    setSelection({ nodeId: null, edgeId: null });
    setViewingRevision(null);
    setNotice(null);
    return contentKey(serializeGraph(nodes, edges).graph, svc, nextNames);
  }

  /**
   * 팔레트에서 추가(클릭·드롭). 모델 노드는 선택된 에이전트에 아직 모델이 없으면 바로 그 에이전트에 잇고, 위치를 주지
   * 않았으면 그 에이전트의 위 오른쪽 모델 포트 근처에 둔다 (D28 편의). 지식 노드도 선택된 에이전트가 있으면 바로 잇고
   * 오른쪽 지식 포트 근처에 둔다(한 에이전트에 여럿 붙으므로 개수는 보지 않는다, D32).
   */
  const addNode = useCallback(
    (type: NodeType, position?: { x: number; y: number }) => {
      // 팔레트를 눌러 놓은 노드는 화면 밖에 앉을 수 있다. 확대해 놓고 쓰는 중이면 추가된 줄도 모른다.
      // 만든 id 를 집어 두었다가 그 노드로 화면을 옮긴다. 끌어다 놓은 것은 이미 보이는 자리라 그대로 둔다
      let created: string | null = null;
      set((prev) => {
        const nid = uniqueNodeId(type, prev.nodes.map((n) => n.id));
        created = nid;
        const hasMain = Boolean(mainAgentNode(prev.nodes));
        const data: NodeData = type === "agent" ? defaultNodeData("agent", hasMain ? "sub" : "main") : defaultNodeData(type);
        if (type === "agent" && hasMain) (data as { name: string }).name = nid;
        if (type === "model") (data as ModelData).name = nid;
        const edges: CanvasEdge[] = prev.edges.map((e) => ({ ...e, selected: false }));
        let pos = position ?? { x: 80 + Math.max(0, ...prev.nodes.map((n) => n.position.x + 300)), y: 200 };
        const selected = prev.nodes.find((n) => n.selected && n.type === "agent");
        if (type === "model" && selected && !attachedModelIds(selected.id, prev.edges.map(placedEdge)).length) {
          edges.push(makeEdge(uniqueEdgeId(selected.id, nid, edges.map((e) => e.id)), selected.id, nid, "model"));
          if (!position) pos = { x: selected.position.x + MODEL_ATTACH_OFFSET.x, y: selected.position.y + MODEL_ATTACH_OFFSET.y };
        }
        if (type === "knowledge" && selected) {
          const attached = prev.edges.filter((e) => e.source === selected.id && e.data?.kind === "knowledge").length;
          edges.push(makeEdge(uniqueEdgeId(selected.id, nid, edges.map((e) => e.id)), selected.id, nid, "knowledge"));
          if (!position) pos = { x: selected.position.x + 320, y: selected.position.y + 40 + attached * 150 };
        }
        return { nodes: [...prev.nodes.map((n) => ({ ...n, selected: false })), makeNode(nid, type, data, pos, true)], edges };
      }, true);
      if (!position && created) setFocus((f) => ({ id: created, key: f.key + 1, keepZoom: true }));
    },
    [set],
  );

  const changeNodeData = useCallback(
    (nid: string, data: NodeData, doCommit = false) => {
      set((prev) => ({ ...prev, nodes: prev.nodes.map((n) => (n.id === nid ? { ...n, data: data as unknown as Record<string, unknown> } : n)) }), doCommit);
    },
    [set],
  );

  const deleteSelection = useCallback(() => {
    set((prev) => {
      const nodeIds = new Set(prev.nodes.filter((n) => n.selected).map((n) => n.id));
      return {
        nodes: prev.nodes.filter((n) => !nodeIds.has(n.id)),
        edges: prev.edges.filter((e) => !e.selected && !nodeIds.has(e.source) && !nodeIds.has(e.target)),
      };
    }, true);
    setSelection({ nodeId: null, edgeId: null });
  }, [set]);

  const onCanvasChange = useCallback((updater: (prev: CanvasState) => CanvasState, doCommit: boolean) => set(updater, doCommit), [set]);

  function runAutoLayout() {
    set((prev) => ({ ...prev, nodes: autoLayout(prev) }), true);
    setFitViewKey((k) => k + 1);
  }

  // 마지막 검사의 Tool 효과(P39b 14절 4부), 백엔드 종류와 바깥 내용 뒤의 승인 규칙인가(P43), Agent 소스가 부르는 Agent(P46 6.2절). 인스펙터의 tools 패널이 효과
  // 배지, 승인 표시, 바깥 표시를 단다. 검사한 그래프에만 맞으므로 이슈처럼 그 그래프의 키에 묶고, 그래프가 바뀌면 보이지 않는다
  const [review, setReview] = useState<{ key: string; result: AgentValidateResponse } | null>(null);
  const validate = useMutation({
    mutationFn: () => validateAgentGraph({ graph: toApiGraph(doc.graph), service }, id),
    onSuccess: (res) => {
      setServerIssues({ key: graphKey, issues: res.issues });
      setReview({ key: graphKey, result: res });
    },
  });
  const reviewed = review?.key === graphKey ? review.result : null;
  const inspectorOptions = useMemo(
    () => (reviewed ? { ...options, toolEffects: reviewed.tool_effects, toolOrigins: reviewed.tool_origins, toolMarked: reviewed.tool_marked, agentCalls: reviewed.agent_calls } : options),
    [options, reviewed],
  );

  const save = useMutation({
    mutationFn: (): Promise<AgentSaved> => {
      // 제안을 올린 내용이면 제안이 닫혔거나 결정된 뒤에도 그 기준 리비전이 최신일 때만 저장된다. 그 사이 다른 저장이
      // 있었으면 409 revision_conflict 다 (P48 6.7절)
      const expect = !isNew && content.base !== null ? { expect_revision: content.base } : {};
      const body: AgentUpdateBody = { name: names.name.trim(), display_name: names.displayName.trim() || undefined, graph: toApiGraph(doc.graph), service, layout: doc.layout, ...expect };
      return isNew ? createAgent(body) : putAgent(id as string, body);
    },
    onSuccess: async (saved) => {
      setServerIssues({ key: graphKey, issues: saved.issues });
      setSavedKey(bodyKey);
      setViewingRevision(null);
      // 사람이 제 판을 저장했다. 청지기의 호출을 고정 사유로 닫아 그 저장에서 이어 가게 한다. 닫지 못해도 저장은 그대로다
      if (content.loadedId) proposal.closeAfterOwnSave(content.loadedId, { revision: saved.revision, ...(isNew ? { agentId: saved.id } : {}) });
      content.forget();
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      if (isNew) {
        // 새로 만든 것은 상세로 간다. 거기서 개요·배포 탭이 함께 열리고 이 캔버스는 정의 탭이 된다
        navigate(`/agents/${saved.id}`, { replace: true });
        setNotice({ code: "created", revision: saved.revision });
        return;
      }
      setNotice(saved.revision_created ? { code: "revisionCreated", revision: saved.revision } : { code: "layoutOnly" });
    },
    onError: (e) => {
      if (e instanceof ApiError && e.code === "invalid_graph") {
        const list = e.extra<Issue[]>("issues");
        if (Array.isArray(list)) setServerIssues({ key: graphKey, issues: list });
      }
    },
  });

  const loadRevision = useMutation({
    mutationFn: (n: number) => getAgentRevision(id as string, n),
    onSuccess: (rev) => {
      const { nodes, edges } = placeLoaded(rev.graph, agent.data?.layout);
      showGraph(nodes, edges, toServiceSettings(rev.service));
      setViewingRevision(rev.revision);
      setSelection({ nodeId: null, edgeId: null });
      setNotice({ code: "revisionLoaded", revision: rev.revision });
    },
  });

  /** 이름 칸으로 데려간다. 그 칸은 아무 노드도 고르지 않았을 때만 인스펙터에 있으므로 선택부터 푼다. */
  const focusNameField = useCallback(() => {
    setSelection({ nodeId: null, edgeId: null });
    setNotice({ code: "nameRequired" });
    window.setTimeout(() => document.querySelector<HTMLInputElement>("[data-testid='ap-name']")?.focus(), 0);
  }, []);

  // 단축키
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      if (!mod) return;
      const key = e.key.toLowerCase();
      if (key === "s") {
        e.preventDefault();
        // 이름이 비어 조용히 아무 일도 없던 자리다. 어디를 채워야 하는지 데려간다
        if (!save.isPending && !names.name.trim()) focusNameField();
        else if (!save.isPending && !hasErrors) save.mutate();
        return;
      }
      if (isEditable(e.target)) return;
      if (key === "z") {
        e.preventDefault();
        if (e.shiftKey) redo();
        else undo();
      } else if (key === "y") {
        e.preventDefault();
        redo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [undo, redo, save, names.name, hasErrors, focusNameField]);

  function focusIssue(issue: Issue) {
    if (issue.node_id) {
      set((prev) => ({ nodes: prev.nodes.map((n) => ({ ...n, selected: n.id === issue.node_id })), edges: prev.edges.map((e) => ({ ...e, selected: false })) }));
      setSelection({ nodeId: issue.node_id, edgeId: null });
      setFocus((f) => ({ id: issue.node_id, key: f.key + 1, keepZoom: false }));
    } else if (issue.edge_id) {
      set((prev) => ({ nodes: prev.nodes.map((n) => ({ ...n, selected: false })), edges: prev.edges.map((e) => ({ ...e, selected: e.id === issue.edge_id })) }));
      setSelection({ nodeId: null, edgeId: issue.edge_id });
    }
  }

  const p = agent.data;

  /**
   * 저장이 꺼진 이유. null 이면 저장할 수 있고, 버튼의 disabled 도 이 하나로 정한다.
   * 이름 칸은 노드를 고르면 인스펙터에서 사라지므로 어디를 채워야 하는지 자리까지 적는다.
   */
  const saveDisabledReason = save.isPending
    ? text.saveDisabled.saving
    : !names.name.trim()
      ? text.saveDisabled.noName
      : !state.nodes.length
        ? text.saveDisabled.noNodes
        : hasErrors
          ? text.saveDisabled.errors(errorCount)
          : !isNew && !dirty
            ? text.saveDisabled.unchanged
            : null;
  const canSave = saveDisabledReason === null;

  // 저장하지 않은 변경이 있으면 떠나기 전에 묻는다
  useUnsavedGuard(unsaved, text.leave);
  // 청지기에게도 알린다. 제안을 올린 캔버스는 그 제안 위에서 고친 것만 센다. 제안을 불러온 것만으로 "저장하지 않은 변경"
  // 이 되면 다듬는 메시지마다 청지기가 저장부터 하라고 한다 (P48 6.5절)
  const unsavedForSteward = content.loadedId ? content.edited : unsaved;
  useEffect(() => setCanvasUnsaved(unsavedForSteward), [unsavedForSteward]);
  useEffect(() => () => setCanvasUnsaved(false), []);

  if (!isNew && agent.isLoading) return <p className="text-sm text-muted">{t.common.loading}</p>;
  if (!isNew && agent.error && !agent.data) return <ErrorMessage error={agent.error} />;

  return (
    /* 높이는 본문 패널의 남은 높이를 flex 로 받는다. 독립 화면은 main 의 자식이고, 정의 탭은 상세의 PageWidth 가
       같은 사슬을 잇는다. 창 높이에서 머리 줄 높이를 빼는 식의 수는 껍데기가 바뀌면 어긋나므로 두지 않는다.
       최소 높이는 둘이 같다. 더 낮추면 노드가 여덟만 돼도 fitView 가 최소 배율까지 내려가고, 그 배율에서는
       포트 점이 2px 남짓이라 끌어서 연결하는 것 자체가 불가능해진다. 창이 그보다 낮으면 본문이 스크롤된다 */
    <div data-testid="agent-canvas-page" className="flex min-h-[36rem] flex-1 flex-col gap-3">
      <CanvasHeader
        isNew={isNew}
        title={isNew ? text.newTitle : names.displayName || (p?.name ?? "")}
        agent={p}
        viewingRevision={viewingRevision}
        onSelectRevision={(n) => {
          // 리비전을 불러오면 지금 편집 중인 것이 그 자리에서 사라지고 되돌리기 이력도 비워진다. 템플릿과 같이 묻는다
          if (dirty && !window.confirm(messages().agents.canvasPage.loadRevision(n))) return;
          content.setAside();
          if (p && n === p.revision) setLoadedFor(null);
          else loadRevision.mutate(n);
        }}
        dirty={dirty}
        hasNodes={Boolean(state.nodes.length)}
        issues={issues}
        showServer={showServer}
        onFocusIssue={focusIssue}
        canUndo={canUndo}
        canRedo={canRedo}
        onUndo={undo}
        onRedo={redo}
        onAutoLayout={runAutoLayout}
        onValidate={() => validate.mutate()}
        validating={validate.isPending}
        onSave={() => save.mutate()}
        saving={save.isPending}
        canSave={canSave}
        saveDisabledReason={saveDisabledReason}
        contextLoading={!context.ready && !context.error}
        embedded={embedded}
      />
      {notice ? (
        <Alert variant="info" data-testid="canvas-notice" className="flex items-center justify-between">
          <span>{noticeText(t, notice)}</span>
          <button type="button" className="text-xs underline-offset-2 hover:underline" onClick={() => setNotice(null)}>
            {t.ui.modal.close}
          </button>
        </Alert>
      ) : null}
      {/* 못 읽은 것만 이름으로 말하고 까닭(권한·서버 오류)은 공용 오류 문장으로 붙인다. 권한 문제는 새로고침으로 풀리지 않는다 */}
      {context.error ? (
        <Alert variant="warn" data-testid="canvas-context-error">
          <p>{text.contextError(context.failed)}</p>
          <div className="mt-1">
            <ErrorBody error={context.error} />
          </div>
        </Alert>
      ) : null}
      {/* 이름 오류는 보통 패널의 입력 밑에 붙는다. 노드·간선이 선택돼 있으면 그 패널이
          보이지 않으므로 배너로 낸다. 아니면 저장을 눌러도 아무 반응이 없는 것처럼 보인다 */}
      {!isNameFieldError(save.error) || selection.nodeId || selection.edgeId ? <ErrorMessage error={save.error} testId="save-error" /> : null}
      <ErrorMessage error={validate.error} testId="validate-error" />
      <ProposalBar
        state={proposal.state}
        shown={Boolean(content.shown)}
        declined={content.declined}
        onApprove={() => {
          // 제안대로 저장하면 캔버스에서 고친 것은 사라진다(저장된 리비전을 다시 올리거나 새 Agent 로 간다). 고친 것이 있으면 묻는다
          if (content.edited && !window.confirm(messages().agents.canvasPage.approveOverEdits)) return;
          proposal.approve();
        }}
        approving={proposal.approving}
        approveError={proposal.approveError}
        onLoad={content.load}
        onLoadLatest={proposal.loadLatest}
      />
      <ErrorMessage error={loadRevision.error} testId="revision-error" />

      <div className="flex min-h-0 flex-1 gap-3">
        <Palette templates={templates.list} templatesLoading={templates.isLoading} templatesError={templates.error} onAdd={(type) => addNode(type)} onTemplate={(template) => applyTemplate(template)} />
        <div className="surface relative min-w-0 flex-1 overflow-hidden rounded-card">
          <AgentCanvas state={state} onChange={onCanvasChange} issues={issues} lookup={lookup} colorMode={theme === "dark" ? "dark" : "light"} onDropNode={(type, pos) => addNode(type, pos)} onSelectionChange={setSelection} fitViewKey={fitViewKey} focusNodeId={focus.id} focusKey={focus.key} focusKeepZoom={focus.keepZoom} proposalMarks={content.marks} />
          {/* 상자만 클릭을 받는다. 나머지는 캔버스가 받아야 끌어다 놓기가 막히지 않는다 */}
          {!state.nodes.length ? (
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center p-4">
              <div className="surface pointer-events-auto max-w-md rounded-card px-6 py-5 text-center text-sm text-muted" data-testid="canvas-empty">
                <AgentIllustration className="mx-auto mb-3 w-[180px]" />
                <p className="text-[13px] font-semibold text-heading">{text.empty.title}</p>
                <p className="mt-1.5">{text.empty.body}</p>
                {templates.list.length ? (
                  <div className="mt-3 flex flex-col gap-1.5">
                    <span className="text-[11px] text-dim">{text.empty.templates}</span>
                    {templates.list.map((template) => (
                      <button
                        key={template.id}
                        type="button"
                        data-testid={`canvas-empty-template-${template.id}`}
                        onClick={() => applyTemplate(template)}
                        className="rounded-item border border-accent-line bg-accent-soft px-3 py-2 text-left text-[12.5px] font-semibold text-heading transition-colors hover:border-accent"
                      >
                        {template.display_name}
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}
        </div>
        <Inspector
          state={state}
          selection={selection}
          issues={issues}
          options={inspectorOptions}
          onChangeNodeData={changeNodeData}
          onCommit={commit}
          onDeleteSelection={deleteSelection}
          service={service}
          onChangeService={setService}
          names={names}
          onChangeNames={setNames}
          readOnlyName={!isNew}
          nameErrors={nameFieldErrors(save.error)}
          proposalTexts={content.texts}
        />
      </div>

      <TestChatDrawer isNew={isNew} agent={p} dirty={dirty} />
    </div>
  );
}
