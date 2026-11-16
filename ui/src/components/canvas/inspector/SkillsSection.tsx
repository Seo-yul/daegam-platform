/**
 * 에이전트 패널 도구 탭의 Skills 절 (D64, P39 5절). 메인과 서브 모두에 있다.
 *
 * 붙인 Skill 은 한 줄씩 `표시 이름 · r3` 으로 보이고, 더 새 리비전이 있으면 "새 리비전 r5" 배지와 "최신으로" 단추가 붙는다.
 * 단추는 그 Skill 을 붙인 모든 에이전트 노드의 고정을 함께 올린다. 노드마다 리비전이 다르면 저장이 막히기 때문이다
 * (skill_revision_mismatch). 붙이기는 consume 이 되는 Skill 만 고르고 그때의 최신 리비전으로 고정한다. 규칙 위반은 패널
 * 위의 이슈 목록이 서버와 같은 code 로 알린다.
 *
 * 누른 단추가 사라지면 초점이 body 로 떨어져 키보드 사용자가 패널 처음부터 다시 밟아야 한다. 빼기 뒤에는 고르개로, 최신으로
 * 뒤에는 절 제목으로 초점을 옮긴다. 고르개는 고를 것이 없어도 disabled 대신 aria-disabled 라 붙인 직후에도 초점을 지킨다.
 */
import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { SkillPinLabel } from "@/components/skills/AttachedSkills";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { IconButton } from "@/components/ui/icon-button";
import { Field, Select } from "@/components/ui/input";
import { useT, type Messages } from "@/i18n";
import type { AgentData } from "@/lib/agentGraph";
import { MAX_SKILLS_PER_AGENT, attachSkill, detachSkill, isPinBehind, pinName, pinsOf, skillInfo, toSkillInfo, upgradeSkill } from "@/lib/agentSkills";
import { useTenant } from "@/lib/tenant";

import { nodeData, type CanvasState } from "../types";
import type { InspectorOptions, InspectorProps } from "./types";

const PICKER_ID = "agent-skill-add";

/** 고르개 아래의 도움말. 고를 수 없으면 그 까닭이다. 목록을 아직 못 받았으면 없다고 하지 않는다. */
function pickerHint(t: Messages, full: boolean, total: number, ready: boolean): string {
  const text = t.canvas.skills;
  if (full) return text.full(MAX_SKILLS_PER_AGENT);
  if (!total) return ready ? text.none : text.loading;
  return text.consumeOnly;
}

/** 사라진 단추 대신 초점을 받을 자리. */
type FocusTarget = "picker" | "heading";

export function SkillsSection({ nodeId, data, state, options, onChangeNodeData }: { nodeId: string; data: AgentData; state: CanvasState; options: InspectorOptions; onChangeNodeData: InspectorProps["onChangeNodeData"] }) {
  const t = useT();
  const text = t.canvas.skills;
  const tenant = useTenant();
  const box = useRef<HTMLDivElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const [focusAfter, setFocusAfter] = useState<FocusTarget | null>(null);
  const pins = pinsOf(data);
  const attached = new Set(pins.map((p) => p.id));
  const candidates = options.skills.filter((s) => s.permissions.consume && !attached.has(s.id));
  const full = pins.length >= MAX_SKILLS_PER_AGENT;
  const locked = full || !candidates.length;

  // 노드 data 가 바뀐 렌더가 그려진 뒤에 옮긴다. 그때는 누른 단추가 이미 DOM 에서 빠졌다
  useEffect(() => {
    if (!focusAfter) return;
    setFocusAfter(null);
    if (focusAfter === "heading") heading.current?.focus();
    else box.current?.querySelector<HTMLElement>(`#${PICKER_ID}`)?.focus();
  }, [focusAfter]);

  /** 그 Skill 을 붙인 모든 에이전트 노드를 함께 올리고 마지막에 한 번 undo 지점을 만든다. */
  function raise(skillId: string, revision: number) {
    const changed = state.nodes.flatMap((n) => {
      const next = n.type === "agent" ? upgradeSkill(nodeData(n, "agent"), skillId, revision) : null;
      return next ? [{ id: n.id, next }] : [];
    });
    changed.forEach(({ id, next }, i) => onChangeNodeData(id, next, i === changed.length - 1));
    setFocusAfter("heading");
  }

  function detach(skillId: string) {
    onChangeNodeData(nodeId, detachSkill(data, skillId), true);
    setFocusAfter("picker");
  }

  return (
    <div ref={box} className="flex flex-col gap-2 border-t border-line pt-3" data-testid="agent-skills">
      <h3 ref={heading} tabIndex={-1} className="rounded-item text-sm font-medium text-fg-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring" data-testid="agent-skills-heading">
        {text.heading}
      </h3>
      <p className="text-xs text-muted">{text.intro}</p>
      {pins.length ? (
        <ul className="flex flex-col gap-1">
          {pins.map((pin) => {
            const info = skillInfo(options.skills, pin.id);
            const name = pinName(t, info, options.ready);
            const testId = `agent-skill-${pin.id}`;
            return (
              <li key={pin.id} className="flex flex-wrap items-center gap-1.5 rounded-item border border-line bg-surface-strong py-1 pl-2.5 pr-1 text-xs" data-testid={testId}>
                <SkillPinLabel pin={pin} info={info} ready={options.ready} testId={testId} newTab />
                {info && info.owner !== tenant ? <span className="text-muted">{info.owner}</span> : null}
                {info && !info.consumable ? (
                  <Badge variant="error" data-testid={`${testId}-not-consumable`}>
                    {text.notConsumable}
                  </Badge>
                ) : null}
                <span className="ml-auto flex items-center gap-1">
                  {info && isPinBehind(pin, info) ? (
                    <Button variant="outline" size="sm" aria-label={text.upgradeLabel(name)} data-testid={`${testId}-upgrade`} onClick={() => raise(pin.id, info.latest)}>
                      {text.upgrade}
                    </Button>
                  ) : null}
                  <IconButton label={text.detachLabel(name)} danger testId={`${testId}-detach`} onClick={() => detach(pin.id)}>
                    <X size={14} />
                  </IconButton>
                </span>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="text-xs text-muted">{text.empty}</p>
      )}
      <Field label={text.attach} htmlFor={PICKER_ID} hint={pickerHint(t, full, options.skills.length, options.ready)}>
        <Select
          id={PICKER_ID}
          data-testid={PICKER_ID}
          value=""
          placeholder={text.pick}
          aria-disabled={locked || undefined}
          onChange={(e) => {
            const skill = candidates.find((s) => s.id === e.target.value);
            if (skill) onChangeNodeData(nodeId, attachSkill(data, skill), true);
          }}
        >
          {candidates.map((s) => {
            const info = toSkillInfo(s);
            return (
              <option key={s.id} value={s.id}>
                {info.label} · r{info.latest}
                {info.owner !== tenant ? ` · ${info.owner}` : ""}
                {info.scripts ? ` · ${text.withScripts}` : ""}
              </option>
            );
          })}
        </Select>
      </Field>
    </div>
  );
}
