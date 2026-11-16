/** 인스펙터 시험의 옵션이다. 목록은 비어 있고 다 받았다. 시험마다 필요한 칸만 덮는다. */
import type { InspectorOptions } from "@/components/canvas/Inspector";

export function inspectorOptions(over: Partial<InspectorOptions> = {}): InspectorOptions {
  return { providers: [], gateways: [], mcpServers: [], dataServices: [], knowledgeBases: [], skills: [], ready: true, ...over };
}
