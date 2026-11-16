/**
 * 빈 상태의 얼굴이 되는 선 일러스트 아홉 (D44 다섯째 원칙, P24 F4).
 *
 * 리소스마다 그림 하나다. 글만 한 줄 있는 빈 자리는 화면이 비었는지 고장났는지 알려 주지 않으므로,
 * 무엇을 만드는 자리인지 한눈에 보이는 은유를 하나씩 둔다. 선만 쓰고 채우지 않는다. 선은 조용한 먹색이고,
 * 그 그림의 뜻이 모이는 한 곳(`Accent`)만 쪽빛이다 (P81 6절). 테마가 바뀌면 토큰이 따라가므로 라이트·다크에
 * 따로 그릴 것이 없다.
 *
 * 움직임은 넣지 않는다. 빈 자리는 오래 보는 자리가 아니고, 움직이지 않으면 `prefers-reduced-motion`
 * 을 따로 볼 것도 없다.
 */
import type { ReactNode } from "react";

import { MCP_LOGO_PATH } from "@/components/icons/brand";
import { type Messages, useT } from "@/i18n";
import { cn } from "@/lib/utils";

export interface IllustrationProps {
  className?: string;
}

/** 그림의 이름. 낭독기가 읽는 그림 설명을 사전에서 찾는 키다. */
type IllustrationName = keyof Messages["detail"]["illustrations"];

/** 그림 안에서 쪽빛으로 서는 한 묶음. 나머지 선은 틀의 먹색을 받는다. */
function Accent({ children }: { children: ReactNode }) {
  return <g stroke="var(--ui-accent)">{children}</g>;
}

/** 아홉 그림이 함께 쓰는 틀. 220×120 안에 선 굵기 2 로 그린다. */
function Frame({ name, className, children }: IllustrationProps & { name: IllustrationName; children: ReactNode }) {
  const t = useT();
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 220 120"
      width="220"
      height="120"
      role="img"
      aria-label={t.detail.illustrations[name]}
      className={cn("h-auto max-w-full", className)}
      fill="none"
      stroke="var(--ui-label)"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {children}
    </svg>
  );
}

/** Backend API. 스펙 문서 하나에서 화살표가 밖으로 나간다. */
export function BackendApiIllustration({ className }: IllustrationProps) {
  return (
    <Frame name="backendApi" className={className}>
      <rect x="34" y="20" width="74" height="80" rx="10" />
      <path d="M52 44h38M52 60h38M52 76h24" />
      <Accent>
        <path d="M120 60h58" />
        <path d="M164 46l14 14-14 14" />
      </Accent>
    </Frame>
  );
}

/** MCP Server. 상자 하나에 도구 표식 셋이 들었다. */
export function McpServerIllustration({ className }: IllustrationProps) {
  return (
    <Frame name="mcpServer" className={className}>
      <rect x="40" y="22" width="140" height="76" rx="12" />
      <path d="M40 44h140" />
      <circle cx="54" cy="33" r="3" />
      <rect x="62" y="58" width="26" height="26" rx="7" />
      <Accent>
        <rect x="97" y="58" width="26" height="26" rx="7" />
      </Accent>
      <rect x="132" y="58" width="26" height="26" rx="7" />
    </Frame>
  );
}

/** Gateway. 선 셋이 한 노드로 모여 하나로 나간다. */
export function GatewayIllustration({ className }: IllustrationProps) {
  return (
    <Frame name="gateway" className={className}>
      <circle cx="28" cy="28" r="4" />
      <circle cx="28" cy="60" r="4" />
      <circle cx="28" cy="92" r="4" />
      <path d="M32 28h26c16 0 20 32 40 32" />
      <path d="M32 60h66" />
      <path d="M32 92h26c16 0 20-32 40-32" />
      <path d="M98 60h28" />
      <Accent>
        <circle cx="146" cy="60" r="20" />
      </Accent>
      <path d="M166 60h26" />
    </Frame>
  );
}

/** Agent. 말풍선 안에 작은 그래프가 있다. */
export function AgentIllustration({ className }: IllustrationProps) {
  return (
    <Frame name="agent" className={className}>
      <path d="M54 16h112a14 14 0 0 1 14 14v44a14 14 0 0 1-14 14H88l-24 18 4-18h-14a14 14 0 0 1-14-14V30a14 14 0 0 1 14-14Z" />
      <Accent>
        <circle cx="74" cy="44" r="7" />
        <circle cx="110" cy="64" r="7" />
        <circle cx="146" cy="40" r="7" />
        <path d="M80 48l24 12M116 60l24-13" />
      </Accent>
    </Frame>
  );
}

/** Database. 원기둥 하나다. */
export function DatabaseIllustration({ className }: IllustrationProps) {
  return (
    <Frame name="database" className={className}>
      <ellipse cx="110" cy="30" rx="46" ry="14" />
      <path d="M64 30v58c0 7.7 20.6 14 46 14s46-6.3 46-14V30" />
      <Accent>
        <path d="M64 59c0 7.7 20.6 14 46 14s46-6.3 46-14" />
      </Accent>
    </Frame>
  );
}

/** Knowledge Base. 겹친 문서를 돋보기로 들여다본다. */
export function KnowledgeBaseIllustration({ className }: IllustrationProps) {
  return (
    <Frame name="knowledgeBase" className={className}>
      <rect x="48" y="12" width="82" height="70" rx="9" />
      <rect x="34" y="28" width="82" height="70" rx="9" />
      <path d="M50 50h50M50 66h30" />
      <Accent>
        <circle cx="152" cy="62" r="22" />
        <path d="M168 78l20 20" />
      </Accent>
    </Frame>
  );
}

/** Skill (D64). 체크 표시가 붙은 절차서 한 장이다. */
export function SkillIllustration({ className }: IllustrationProps) {
  return (
    <Frame name="skill" className={className}>
      <rect x="62" y="12" width="96" height="96" rx="12" />
      <Accent>
        <path d="M78 38l6 6 10-12M78 62l6 6 10-12" />
      </Accent>
      <path d="M104 40h38M104 64h38" />
      <path d="M80 88h8M104 88h28" />
    </Frame>
  );
}

/** Cluster. 노드 셋이 고리로 이어져 있다. */
export function ClusterIllustration({ className }: IllustrationProps) {
  return (
    <Frame name="cluster" className={className}>
      <Accent>
        <circle cx="110" cy="26" r="14" />
      </Accent>
      <circle cx="64" cy="88" r="14" />
      <circle cx="156" cy="88" r="14" />
      <path d="M102 37L72 77" />
      <path d="M78 88h64" />
      <path d="M148 77L118 37" />
    </Frame>
  );
}

/** Hub. 카드가 격자로 늘어선 카탈로그다. */
export function HubIllustration({ className }: IllustrationProps) {
  const t = useT();
  // 허브는 은유가 아니라 MCP 그 자체를 보인다. 공식 마크를 24x24 에서 3.75배로 키워 틀 가운데에 둔다.
  // 마크는 선이 아니라 면이라 Frame(stroke) 을 쓰지 않는다. 마크 하나뿐이라 통째로 쪽빛이다
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 220 120"
      width="220"
      height="120"
      role="img"
      aria-label={t.detail.illustrations.hub}
      className={cn("h-auto max-w-full", className)}
      fill="var(--ui-accent)"
    >
      <g transform="translate(65 15) scale(3.75)">
        <path d={MCP_LOGO_PATH} />
      </g>
    </svg>
  );
}
