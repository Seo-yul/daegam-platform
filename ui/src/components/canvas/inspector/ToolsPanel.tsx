/**
 * MCP 노드 패널. 소스(Gateway·MCP Server)와 그 소스의 도구 목록에서 허용 도구 체크, 도구별 승인 결정 종류,
 * 타임아웃이다. 소스가 게시한 Agent 면(P46 6.2절) Tool 은 그 항목의 ask_* 하나라 허용 목록과 표시 절 대신 그 Tool 과 호출의
 * 안내를 보인다(./AgentCallNotes.tsx). 부르는 방식을 작업으로 바꾸면(6.7절) 허용 Tool 이 작업 Tool 다섯이 되고 타임아웃이 없다.
 *
 * 검사 뒤에는 바깥 내용 뒤의 승인 규칙(P43)의 표시를 보인다. 바깥 표시(외부 내용을 가져오는 Tool), 승인 기억 켜기, 읽기 전용
 * 표시, 외부 내용 뒤의 승인·거절 배지다(lib/toolSafety.ts, 절과 배지는 ./toolMarks.tsx).
 *
 * 셀렉트에는 배포 상태를 함께 적는다. 정의와 배포가 한 문서라(D42) 목록에는 안 올린 것도 함께 서는데,
 * 도구 소스는 배포된 것만 쓸 수 있어서 고르고 나서야 표식을 보면 왜 막히는지 알 수 없다.
 */
import { useMemo, useState } from "react";
import { Link } from "react-router";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { Field, Input, Label, Select } from "@/components/ui/input";
import { useT } from "@/i18n";
import { AGENT_SOURCE, DECISIONS, SOURCE_KINDS, type Decision, type ToolsData } from "@/lib/agentGraph";
import { idpDataPlane, useAuthState } from "@/lib/auth";
import { gatewayToolTimeout } from "@/lib/gateway";
import { CODE_MODE_TOOL } from "@/lib/hub";
import { AGENT_TOOL_TIMEOUT_SECONDS } from "@/lib/timeouts";
import { ORIGIN_SCRIPT, TASK_TOOLS, assignsTasks, canvasMarkApplies } from "@/lib/toolSafety";

import { AgentCallNotes } from "./AgentCallNotes";
import { ResourceLink, Tabs } from "./fields";
import { ApprovalBadge, OutsideSection, ReadOnlySection, without } from "./toolMarks";
import { sourceChoices, sourcePath, toolNamesOfSource, type InspectorOptions } from "./types";

/** 소스 종류를 바꾸면 비우는 칸. 앞 소스의 Tool 이름을 대는 규칙이라 새 소스에는 맞지 않는다. */
const SOURCE_RULES_RESET: Partial<ToolsData> = { allow_tools: [], approval: {}, timeouts: {} };
/** Agent 소스로 바꾸면 더 비우는 칸. 그 소스는 읽기 전용 표시와 바깥 표시를 받지 않는다(서버 `invalid_node_data`). */
const AGENT_SOURCE_RESET: Partial<ToolsData> = { ...SOURCE_RULES_RESET, read_only: [], outside: {}, rememberable: [] };

/** Agent 소스의 부르는 방식. 저장되는 것은 허용 Tool 이다. wait 는 빈 목록(ask_* 하나), task 는 작업 Tool 다섯이다. */
type AgentMode = "wait" | "task";

/** 쉼표로 적은 도메인 목록. 공백과 빈 칸은 뺀다. 공개 접미사 같은 규칙은 서버 검사가 본다. */
function domainList(value: string): string[] {
  return value
    .split(",")
    .map((d) => d.trim().toLowerCase())
    .filter(Boolean);
}

/** 쉼표로 적는 도메인 칸. 친 글을 그대로 두고 목록만 노드에 싣는다. 목록을 다시 이어 보이면 친 쉼표가 사라진다. */
function DomainField({ id, label, hint, value, onChange }: { id: string; label: string; hint: string; value: string[]; onChange: (v: string[]) => void }) {
  const [draft, setDraft] = useState(value.join(", "));
  return (
    <Field label={label} htmlFor={id} hint={hint}>
      <Input
        id={id}
        data-testid={id}
        className="h-8 font-mono text-xs"
        value={draft}
        onChange={(e) => {
          setDraft(e.target.value);
          onChange(domainList(e.target.value));
        }}
      />
    </Field>
  );
}

export function ToolsPanel({
  nodeId,
  data,
  update,
  options,
  hasMemory,
}: {
  nodeId: string;
  data: ToolsData;
  update: (patch: Partial<ToolsData>, commit?: boolean) => void;
  options: InspectorOptions;
  /** 메인 에이전트에 memory 노드가 붙었는가. 없으면 바깥 내용이 들어온 대화의 쓰기는 거절된다(P43 8절). */
  hasMemory: boolean;
}) {
  const t = useT();
  const text = t.canvas.tools;
  // Gateway 가 아직 IdP 토큰만 받는 동안은 Agent 가 agent_oauth 로 부른다는 안내다 (P53b 6.1절)
  const idp = idpDataPlane(useAuthState());
  const isAgent = data.source.kind === AGENT_SOURCE;
  const isTask = isAgent && assignsTasks(data.allow_tools);
  const sourceHint = data.source.kind === AGENT_SOURCE ? undefined : data.source.kind === "gateway_instance" && idp ? text.sourceHintIdpGateway : text.sourceHint[data.source.kind];
  const [tab, setTab] = useState<"tools" | "approval" | "timeouts">("tools");
  const [extra, setExtra] = useState("");
  // Tool 효과(P39b 14절 4부). 마지막 검사의 값이고 검사 전에는 없다
  const effects = options.toolEffects?.[nodeId];
  const graphHasWeb = Object.values(options.toolEffects ?? {}).some((byTool) => Object.values(byTool).includes("web"));
  const nodeHasWeb = effects ? Object.values(effects).includes("web") : false;
  // 백엔드 종류(P43)와 서버나 허브 항목의 표시가 서는 이름(P52 4절). 마지막 검사의 값이고 검사 전에는 없다
  const origins = options.toolOrigins?.[nodeId];
  const marked = options.toolMarked?.[nodeId];
  const readOnly = data.read_only ?? [];
  const rememberable = data.rememberable ?? [];
  // 읽기 전용으로 표시할 수 있는 Tool 이다. 아직 표시하지 않은 쓰기 Tool 가운데 서버가 읽기 전용 여부를 알려 주지 않고 서버나 항목의
  // 표시도 서지 않는 Tool 만이다
  const markable = effects ? Object.keys(effects).filter((tool) => effects[tool] === "write" && !readOnly.includes(tool) && canvasMarkApplies(origins?.[tool], marked?.includes(tool))) : [];
  const showReadOnly = markable.length > 0 || readOnly.length > 0;
  // code mode Gateway 의 execute_tool_script 다 (P56 6.5절). 배포 스냅샷에는 없고 검사가 백엔드 종류 script 로 본다. 소스의 Tool 로 함께 보인다
  const offersScript = origins?.[CODE_MODE_TOOL] === ORIGIN_SCRIPT;
  const known = useMemo(() => {
    const listed = toolNamesOfSource(data.source.kind, data.source.id, options);
    return listed && offersScript && !listed.includes(CODE_MODE_TOOL) ? [...listed, CODE_MODE_TOOL] : listed;
  }, [data.source.kind, data.source.id, options, offersScript]);
  const allow = data.allow_tools ?? [];
  // 승인 기억은 이름을 아는 노드에서만 효과가 있다(서버 ChoiceRules). 고른 Tool 이거나, 고른 것이 없으면 검사가 소스의 이름으로 본 Tool 이다
  const namesKnown = (tool: string) => allow.includes(tool) || (!allow.length && effects?.[tool] !== undefined);
  // 작업을 맡기는 노드의 Tool 은 다섯으로 정해져 있다. 그 밖에는 소스가 내는 이름과 규칙이 댄 이름이다
  const names = useMemo(
    () => (isTask ? [...TASK_TOOLS] : [...new Set([...(known ?? []), ...allow, ...Object.keys(data.approval ?? {}), ...Object.keys(data.timeouts ?? {})])]),
    [isTask, known, allow, data.approval, data.timeouts],
  );
  // Agent 가 Gateway 보다 먼저 끊는 Tool (P50 7절). 소스가 Gateway 일 때만 그 배포 스냅샷의 백엔드 타임아웃과 맞댄다.
  // 허용 Tool 을 정했으면 그것만 본다(비면 전부다)
  const shorter = useMemo(() => {
    const gw = data.source.kind === "gateway_instance" ? options.gateways.find((g) => g.id === data.source.id) : undefined;
    if (!gw) return [];
    return (allow.length ? names.filter((tool) => allow.includes(tool)) : names).flatMap((tool) => {
      const gateway = gatewayToolTimeout(gw, tool);
      const agent = data.timeouts?.[tool] ?? AGENT_TOOL_TIMEOUT_SECONDS;
      return gateway !== null && agent < gateway ? [{ tool, agent, gateway }] : [];
    });
  }, [data.source.kind, data.source.id, data.timeouts, names, allow, options.gateways]);
  const sources = sourceChoices(data.source.kind, options);
  const sourceLink = sourcePath(data.source, options);
  // 호출 하나가 기다리는 시간의 기본값이다. 검사가 호출받는 Agent 의 턴 시간에서 계산해 준다
  const agentWait = options.agentCalls?.[nodeId]?.timeout_seconds;

  function changeKind(kind: ToolsData["source"]["kind"]) {
    update({ source: { kind, id: "" }, ...(kind === AGENT_SOURCE ? AGENT_SOURCE_RESET : SOURCE_RULES_RESET) }, true);
  }

  /** 부르는 방식을 바꾼다. 앞 방식의 Tool 이름을 대는 규칙은 새 방식에 맞지 않아 비운다. */
  function changeMode(mode: AgentMode) {
    update({ allow_tools: mode === "task" ? [...TASK_TOOLS] : [], approval: {}, timeouts: {}, rememberable: [] }, true);
  }

  function toggleAllow(tool: string, on: boolean) {
    update({ allow_tools: on ? [...allow, tool] : without(allow, tool) }, true);
  }
  function toggleDecision(tool: string, d: Decision, on: boolean) {
    const cur = data.approval?.[tool] ?? [];
    const next = on ? [...cur, d] : cur.filter((x) => x !== d);
    const approval = { ...(data.approval ?? {}) };
    if (next.length) approval[tool] = next;
    else delete approval[tool];
    // 승인 기억은 승인 설정이 있는 Tool 에만 있다. 설정을 지우면 함께 지운다
    update(next.length || !rememberable.includes(tool) ? { approval } : { approval, rememberable: without(rememberable, tool) }, true);
  }
  function toggleRememberable(tool: string, on: boolean) {
    update({ rememberable: on ? [...rememberable, tool] : without(rememberable, tool) }, true);
  }
  function setTimeout_(tool: string, v: number | null) {
    const timeouts = { ...(data.timeouts ?? {}) };
    if (v == null || v < 1) delete timeouts[tool];
    else timeouts[tool] = v;
    update({ timeouts });
  }

  return (
    <>
      {shorter.length ? (
        <Alert variant="warn" className="flex flex-col gap-1 text-xs" data-testid="timeout-shorter">
          {shorter.map((w) => (
            <span key={w.tool} data-testid={`timeout-shorter-${w.tool}`}>
              {text.timeoutShorter(w.tool, w.agent, w.gateway)}
            </span>
          ))}
        </Alert>
      ) : null}
      <Tabs
        tabs={[
          { id: "tools", label: t.canvas.tabs.tools },
          { id: "approval", label: text.approval },
          { id: "timeouts", label: text.timeouts },
        ]}
        value={tab}
        onChange={setTab}
        testId="inspector-tabs"
      />
      {tab === "tools" ? (
        <>
          <div className="grid grid-cols-[9rem_1fr] gap-3">
            <Field label={text.sourceKind} htmlFor="tools-source-kind">
              {/* 값은 저장되는 그래프의 것이라 그대로 두고 화면 글자만 새 이름이다 */}
              <Select id="tools-source-kind" data-testid="tools-source-kind" value={data.source.kind} onChange={(e) => changeKind(e.target.value as ToolsData["source"]["kind"])}>
                <option value="gateway_instance">{t.resources.kind.gateway}</option>
                <option value="mcp_instance">{t.resources.kind["mcp-server"]}</option>
                <option value={AGENT_SOURCE}>{text.sources.agent.group}</option>
              </Select>
            </Field>
            <Field label={text.source} htmlFor="tools-source" hint={sourceHint}>
              <Select id="tools-source" data-testid="tools-source" value={data.source.id} placeholder={t.shared.select} onChange={(e) => update({ source: { ...data.source, id: e.target.value } }, true)}>
                <option value="">{t.shared.select}</option>
                {sources.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.label}
                  </option>
                ))}
              </Select>
              {sourceLink ? (
                <div className="mt-1">
                  <ResourceLink to={sourceLink} label={t.canvas.openResource(SOURCE_KINDS[data.source.kind].label)} testId="tools-source-link" />
                </div>
              ) : null}
            </Field>
          </div>
          {isAgent ? (
            <>
              <Field label={text.agentMode.label} htmlFor="tools-agent-mode" hint={text.agentMode.hint[isTask ? "task" : "wait"]}>
                <Select id="tools-agent-mode" data-testid="tools-agent-mode" value={isTask ? "task" : "wait"} onChange={(e) => changeMode(e.target.value as AgentMode)}>
                  <option value="wait">{text.agentMode.wait}</option>
                  <option value="task">{text.agentMode.task}</option>
                </Select>
              </Field>
              <AgentCallNotes tool={known?.[0]} call={options.agentCalls?.[nodeId]} task={isTask} />
            </>
          ) : (
            <>
              <div className="flex flex-col gap-1">
                <Label>{text.allow}</Label>
                <p className="text-xs text-muted">{allow.length ? text.allowCount(allow.length) : text.allowAll}</p>
                {names.length ? (
                  <ul className="flex flex-col gap-1" data-testid="tools-allow-list">
                    {names.map((tool) => (
                      <li key={tool} className="flex items-center gap-2 text-sm">
                        <input id={`tool-allow-${tool}`} data-testid={`tool-allow-${tool}`} type="checkbox" checked={allow.includes(tool)} onChange={(e) => toggleAllow(tool, e.target.checked)} />
                        <label htmlFor={`tool-allow-${tool}`} className="font-mono text-xs text-fg">
                          {tool}
                        </label>
                        {known && !known.includes(tool) ? <Badge variant="warn">{text.notInSource}</Badge> : null}
                        {effects?.[tool] ? (
                          <Badge variant={effects[tool] === "web" ? "info" : effects[tool] === "write" ? "warn" : "muted"} data-testid={`tool-effect-${tool}`}>
                            {text.effect[effects[tool]]}
                          </Badge>
                        ) : null}
                        <ApprovalBadge tool={tool} unruled={effects?.[tool] === "write" && !data.approval?.[tool]?.length} hasMemory={hasMemory} />
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-xs text-muted" data-testid="tools-no-list">
                    {data.source.id ? text.listUnknown : text.pickSource}
                  </p>
                )}
                <div className="flex gap-2">
                  <Input data-testid="tool-allow-extra" className="h-8 font-mono text-xs" placeholder={text.extraPlaceholder} value={extra} onChange={(e) => setExtra(e.target.value)} />
                  <Button
                    size="sm"
                    variant="outline"
                    data-testid="tool-allow-add"
                    disabled={!extra.trim() || allow.includes(extra.trim())}
                    onClick={() => {
                      toggleAllow(extra.trim(), true);
                      setExtra("");
                    }}
                  >
                    {t.shared.add}
                  </Button>
                </div>
              </div>
              {offersScript ? (
                <p className="text-xs text-muted" data-testid="tools-code-mode-hint">
                  {text.codeModeHint}
                </p>
              ) : null}
              {effects ? null : (
                <p className="text-xs text-muted" data-testid="tools-effects-unknown">
                  {text.effectsUnknown}
                </p>
              )}
              {effects && origins ? <OutsideSection effects={effects} origins={origins} outside={data.outside} onChange={(outside) => update({ outside }, true)} /> : null}
              {showReadOnly ? <ReadOnlySection readOnly={readOnly} markable={markable} origins={origins} marked={marked} onChange={(read_only) => update({ read_only }, true)} /> : null}
              {nodeHasWeb || data.allow_domains?.length || data.block_domains?.length ? (
                <div className="flex flex-col gap-2" data-testid="tools-domains">
                  <DomainField id="tools-allow-domains" label={text.allowDomains} hint={text.allowDomainsHint} value={data.allow_domains ?? []} onChange={(allow_domains) => update({ allow_domains })} />
                  <DomainField id="tools-block-domains" label={text.blockDomains} hint={text.blockDomainsHint} value={data.block_domains ?? []} onChange={(block_domains) => update({ block_domains })} />
                </div>
              ) : null}
              {options.toolEffects && !graphHasWeb ? (
                <p className="text-xs text-muted" data-testid="tools-web-hint">
                  {text.webHint}{" "}
                  <Link className="underline underline-offset-2" to="/gateways/new?backend=web-search&backend=web-fetch" data-testid="tools-web-hint-link">
                    {text.webHintLink}
                  </Link>
                </p>
              ) : null}
            </>
          )}
        </>
      ) : null}
      {tab === "approval" ? (
        <div className="flex flex-col gap-2">
          {/* 체크박스·입력에 도구 이름을 aria-label 로 실어 준다. 이름이 옆 칸에 글로 있어도
              스크린 리더는 컨트롤만 읽어서 도구가 열 개면 같은 이름 열 개가 된다 */}
          <p className="text-xs text-muted">{text.approvalHelp}</p>
          <p className="text-xs text-muted">{text.rememberableHint}</p>
          {names.length ? (
            <ul className="flex flex-col gap-1.5" data-testid="tools-approval-list">
              {names.map((tool) => (
                <li key={tool} className="rounded-item border border-line bg-surface-strong px-2.5 py-2">
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-xs text-fg">{tool}</span>
                    {data.approval?.[tool]?.length ? <Badge variant="warn">{t.canvas.node.hitl}</Badge> : null}
                  </div>
                  <div className="mt-1.5 flex flex-wrap gap-3">
                    {DECISIONS.map((d) => (
                      <label key={d} className="flex items-center gap-1 text-xs text-fg-soft">
                        <input aria-label={`${tool} ${d}`} data-testid={`approval-${tool}-${d}`} type="checkbox" checked={data.approval?.[tool]?.includes(d) ?? false} onChange={(e) => toggleDecision(tool, d, e.target.checked)} />
                        {d}
                      </label>
                    ))}
                  </div>
                  {data.approval?.[tool]?.length && namesKnown(tool) ? (
                    <label className="mt-1.5 flex items-center gap-1 text-xs text-fg-soft">
                      <input aria-label={`${tool} ${text.rememberable}`} data-testid={`approval-${tool}-rememberable`} type="checkbox" checked={rememberable.includes(tool)} onChange={(e) => toggleRememberable(tool, e.target.checked)} />
                      {text.rememberable}
                    </label>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-muted">{text.allowFirst}</p>
          )}
        </div>
      ) : null}
      {tab === "timeouts" ? (
        <div className="flex flex-col gap-2">
          <p className="text-xs text-muted" data-testid="tools-timeouts-help">
            {isTask ? text.agentTaskTimeout : isAgent ? text.agentTimeout : text.timeoutsHelp}
          </p>
          {isTask ? null : names.length ? (
            names.map((tool) => (
              <div key={tool} className="grid grid-cols-[1fr_7rem] items-center gap-2">
                <span className="font-mono text-xs text-fg">{tool}</span>
                <Input aria-label={text.timeoutLabel(tool)} data-testid={`timeout-${tool}`} type="number" min={1} className="h-8 text-xs" placeholder={isAgent && agentWait ? String(agentWait) : text.timeoutDefault} value={data.timeouts?.[tool] ?? ""} onChange={(e) => setTimeout_(tool, e.target.value === "" ? null : Number(e.target.value))} />
              </div>
            ))
          ) : (
            <p className="text-xs text-muted">{text.allowFirst}</p>
          )}
        </div>
      ) : null}
    </>
  );
}
