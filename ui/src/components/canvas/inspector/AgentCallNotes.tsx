/**
 * tools 노드 패널에서 소스가 게시한 Agent 일 때의 안내다 (P46 6.2·6.6절). 그 소스의 Tool 은 ask_* 하나이고 늘 쓰기이며 결과는 늘 외부
 * 내용이라 허용 목록과 표시 절이 없다. 부르는 대화의 글이 호출받은 Agent 의 LLM provider 와 본문 기록으로 가므로 검사 뒤에 그 둘을
 * 보인다.
 */
import type { AgentCall } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { useT } from "@/i18n";

export function AgentCallNotes({ tool, call }: { tool: string | undefined; call: AgentCall | undefined }) {
  const t = useT();
  const text = t.canvas.tools;
  // 검사가 읽은 이름이 먼저다. 검사 전에는 허브 목록의 항목 이름에서 만든 이름이다
  const name = call?.tool ?? tool;
  const recording = text.agentRecording[call?.record_bodies ? "on" : "off"];
  return (
    <div className="flex flex-col gap-2" data-testid="tools-agent-call">
      {name ? (
        <div className="flex items-center gap-2 text-sm">
          <span className="font-mono text-xs text-fg" data-testid="tools-agent-tool">
            {name}
          </span>
          <Badge variant="warn">{text.effect.write}</Badge>
          <Badge variant="muted">{t.chat.tool.outside}</Badge>
        </div>
      ) : (
        <p className="text-xs text-muted" data-testid="tools-no-list">
          {text.pickSource}
        </p>
      )}
      <p className="text-xs text-muted" data-testid="tools-agent-marks">
        {text.agentMarks}
      </p>
      {call ? (
        <p className="text-xs text-muted" data-testid="tools-agent-data-flow">
          {text.agentDataFlow(call.provider, recording)}
        </p>
      ) : null}
    </div>
  );
}
