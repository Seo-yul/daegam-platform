/**
 * SSE 응답 조각. Agent 채팅(턴·결정·다시 생성)과 플랫폼 비서(턴·결정)는 같은 모양의 스트림을 낸다. 이벤트 하나가
 * `event:`·`data:` 두 줄 블록이고 빈 줄로 끝난다(lib/chatStream.ts 가 읽는 모양). 목 핸들러와 스트림을 직접 다루는 테스트가
 * 여기서 블록과 응답을 짓는다.
 */
import { HttpResponse } from "msw";

export interface SseEvent {
  event: string;
  data: unknown;
}

const SSE_HEADERS = { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" };

const encoder = new TextEncoder();

/**
 * 턴의 토큰 사용량. done 이벤트, 전사, 세션 상세가 같은 모양을 쓴다. `cached` 는 입력 가운데 provider 가 캐시에서 읽은 몫이고
 * 런타임과 플랫폼 비서 모두 기본값 0 으로 늘 싣는다.
 */
export function tokenUsage(input: number, output: number, cached = 0) {
  return { input_tokens: input, output_tokens: output, total_tokens: input + output, cached_input_tokens: cached };
}

/** 이벤트 하나의 블록이다. */
export function sseBlock({ event, data }: SseEvent): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function streamResponse(stream: ReadableStream<Uint8Array>, headers: Record<string, string> = {}): Response {
  return new HttpResponse(stream, { headers: { ...SSE_HEADERS, ...headers } });
}

/** 대본의 블록을 조각조각 흘려보내고 닫는다. 브라우저·Node fetch·http-middleware 모두 스트림으로 받는다. */
export function sseResponse(events: SseEvent[], delayMs = 5): Response {
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      for (const ev of events) {
        controller.enqueue(encoder.encode(sseBlock(ev)));
        if (delayMs) await new Promise((r) => setTimeout(r, delayMs));
      }
      controller.close();
    },
  });
  return streamResponse(stream);
}

/** 열어 둔 SSE 응답과, 그 스트림에 이벤트를 밀어 넣고 닫는 함수다. */
export interface HeldSse {
  response: Response;
  /** 이벤트 하나를 흘려보낸다. */
  push: (ev: SseEvent) => void;
  /** 스트림을 닫는다. 받는 쪽에서는 턴이 끝난다. */
  close: () => void;
}

interface HeldSseOptions {
  /** 응답에 더할 헤더다(런타임이 알리는 세션 id). */
  headers?: Record<string, string>;
  /** 받는 쪽이 읽기를 끊으면(중단) 부른다. */
  onCancel?: () => void;
}

/**
 * 닫을 때까지 열려 있는 SSE 응답. 앞 블록(prelude)만 흘려보낸 채 기다리고, 그 뒤의 이벤트와 닫기는 돌려준 push·close
 * 로 한다. 턴이 도는 동안의 화면(생각하는 중, 중단, 중지)을 보는 데 쓴다.
 */
export function heldSse(prelude: SseEvent[] = [], { headers, onCancel }: HeldSseOptions = {}): HeldSse {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
    cancel() {
      onCancel?.();
    },
  });
  const push = (ev: SseEvent) => controller.enqueue(encoder.encode(sseBlock(ev)));
  for (const ev of prelude) push(ev);
  return { response: streamResponse(stream, headers), push, close: () => controller.close() };
}
