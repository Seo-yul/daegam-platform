/**
 * tools 노드 패널에서 소스가 게시한 Agent 일 때의 안내다 (P46 6.2·6.6·6.7절). 답을 기다리는 노드의 Tool 은 ask_* 하나이고 늘 쓰기이며
 * 결과는 늘 외부 내용이다. 작업을 맡기는 노드의 Tool 은 다섯이고 효과와 표시가 Tool 마다 정해져 있다. 어느 쪽이든 허용 목록과 표시
 * 절이 없다. 부르는 대화의 글이 호출받은 Agent 의 LLM provider 와 본문 기록으로 가므로 검사 뒤에 그 둘을 보인다.
 */
import type { AgentCall } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { useT } from "@/i18n";
import { TASK_ANSWER_TOOL, TASK_READ_TOOLS, TASK_TOOLS } from "@/lib/toolSafety";

export function AgentCallNotes({ tool, call, task }: { tool: string | undefined; call: AgentCall | undefined; task: boolean }) {
  const t = useT();
  const text = t.canvas.tools;
  // 검사가 읽은 이름이 먼저다. 검사 전에는 허브 목록의 항목 이름에서 만든 이름이다
  const name = call?.tool ?? tool;
  const recording = text.agentRecording[call?.record_bodies ? "on" : "off"];
  return (
    <div className="flex flex-col gap-2" data-testid="tools-agent-call">
      {task ? (
        <ul className="flex flex-col gap-1" data-testid="tools-agent-task-tools">
          {TASK_TOOLS.map((taskTool) => (
            <li key={taskTool} className="flex items-center gap-2 text-sm">
              <span className="font-mono text-xs text-fg">{taskTool}</span>
              <Badge variant={TASK_READ_TOOLS.has(taskTool) ? "muted" : "warn"} data-testid={`tool-effect-${taskTool}`}>
                {text.effect[TASK_READ_TOOLS.has(taskTool) ? "read" : "write"]}
              </Badge>
              {taskTool === TASK_ANSWER_TOOL ? <Badge variant="muted">{t.chat.tool.outside}</Badge> : null}
            </li>
          ))}
        </ul>
      ) : name ? (
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
        {task ? text.agentTaskMarks : text.agentMarks}
      </p>
      {call ? (
        <p className="text-xs text-muted" data-testid="tools-agent-data-flow">
          {text.agentDataFlow(call.provider, recording)}
        </p>
      ) : null}
    </div>
  );
}
