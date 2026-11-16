/**
 * Agent 에 붙인 Skill 의 이름·고정한 리비전과 뒤처짐 배지 (D64, P39 5절). 캔버스 인스펙터의 Skills 절과 Agent 개요의 정의
 * 요약이 같은 모양을 쓴다. 이름은 Skill 상세로 가는 링크다.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { listSkills } from "@/api/skills";
import { Badge } from "@/components/ui/badge";
import { useT } from "@/i18n";
import { graphSkillPins, isPinBehind, pinText, skillInfo, type SkillInfo, type SkillPin } from "@/lib/agentSkills";
import { skillsKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";

/**
 * `표시 이름 · r3` 과, 더 새 리비전이 있으면 "새 리비전 r5" 배지. `newTab` 이면 링크를 새 탭으로 연다. 캔버스는 같은 탭에서
 * 옮기면 저장하지 않은 그래프를 잃는다.
 */
export function SkillPinLabel({ pin, info, ready, testId, newTab = false }: { pin: SkillPin; info: SkillInfo | null; ready: boolean; testId: string; newTab?: boolean }) {
  const t = useT();
  const text = pinText(t, pin, info, ready);
  return (
    <>
      {info ? (
        <Link className="truncate text-fg underline-offset-2 hover:underline" to={`/skills/${pin.id}`} target={newTab ? "_blank" : undefined} rel={newTab ? "noreferrer" : undefined} data-testid={`${testId}-name`}>
          {text}
        </Link>
      ) : (
        <span className="truncate text-muted" data-testid={`${testId}-name`}>
          {text}
        </span>
      )}
      {info && isPinBehind(pin, info) ? (
        <Badge variant="warn" data-testid={`${testId}-behind`}>
          {t.skills.newerRevision(info.latest)}
        </Badge>
      ) : null}
    </>
  );
}

/** Agent 개요의 "Skills" 줄. 그래프가 고정한 Skill 마다 한 줄이고 없으면 `-` 다. */
export function AgentSkillsSummary({ graph }: { graph: { nodes?: readonly { type: string; data?: unknown }[] } }) {
  const tenant = useTenant();
  const pins = graphSkillPins(graph);
  // 붙인 Skill 이 없는 Agent 는 목록을 부르지 않는다
  const skills = useQuery({ queryKey: skillsKey(tenant), queryFn: listSkills, enabled: pins.length > 0 });
  if (!pins.length) return <>-</>;
  return (
    <ul className="flex flex-col gap-0.5">
      {pins.map((pin) => (
        <li key={pin.id} className="flex flex-wrap items-center gap-1.5">
          <SkillPinLabel pin={pin} info={skillInfo(skills.data ?? [], pin.id)} ready={skills.isSuccess} testId={`summary-skill-${pin.id}`} />
        </li>
      ))}
    </ul>
  );
}
