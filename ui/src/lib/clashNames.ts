/**
 * `builtin_tool_name` 이슈의 `data` 에서 문장에 끼울 이름이다. 서버는 `{clashes: [{agent, names}]}` 를 싣는다
 * (platform/daegam/services/tool_pins.py `builtin_name_issues`). Tool 마다 `<Tool> (<에이전트 이름>)` 이고 쉼표로
 * 잇는다. 에이전트 이름은 사용자가 캔버스에서 지은 것이라 사전에 두지 않는다. 꼴이 맞지 않는 값은 건너뛴다.
 */
export function clashNames(data: Record<string, unknown> | null | undefined): string {
  const clashes = data?.clashes;
  if (!Array.isArray(clashes)) return "";
  const out: string[] = [];
  for (const clash of clashes as unknown[]) {
    if (typeof clash !== "object" || clash === null) continue;
    const { agent, names } = clash as { agent?: unknown; names?: unknown };
    if (typeof agent !== "string" || !Array.isArray(names)) continue;
    for (const name of names) if (typeof name === "string") out.push(`${name} (${agent})`);
  }
  return out.join(", ");
}
