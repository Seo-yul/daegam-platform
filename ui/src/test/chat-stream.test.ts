import { applyChatEvent, type ChatItem } from "@/components/chat";
import { parseSseChunk, readChatStream } from "@/lib/chatStream";

describe("chat stream parsing", () => {
  it("splits complete SSE blocks and keeps the partial rest", () => {
    const { events, rest } = parseSseChunk('event: token\ndata: {"text":"a"}\n\nevent: done\ndata: {"session_id":"s","turn":1}\n\nevent: tok');
    expect(events).toEqual([
      { event: "token", data: { text: "a" } },
      { event: "done", data: { session_id: "s", turn: 1 } },
    ]);
    expect(rest).toBe("event: tok");
    // CRLF 와 주석 줄, JSON 이 아닌 data
    expect(parseSseChunk(": ping\r\nevent: error\r\ndata: oops\r\n\r\n").events).toEqual([{ event: "error", data: { text: "oops" } }]);
  });

  it("reads a ReadableStream chunked mid-event", async () => {
    const enc = new TextEncoder();
    const chunks = ['event: token\ndata: {"te', 'xt":"hello"}\n\nevent: tool_call\ndata: {"id":"c1","name":"Add","args":{"a":1}}\n\n', 'event: done\ndata: {"session_id":"s","turn":1,"usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3},"tool_calls":1,"text":"hello"}'];
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        for (const ch of chunks) c.enqueue(enc.encode(ch));
        c.close();
      },
    });
    const out: string[] = [];
    for await (const ev of readChatStream(stream)) out.push(ev.event);
    expect(out).toEqual(["token", "tool_call", "done"]);
  });

  it("closes the earlier bubble when a token of another agent label starts a new one", () => {
    let items: ChatItem[] = [{ kind: "user", text: "hi" }];
    items = applyChatEvent(items, { event: "token", data: { text: "7", agent: "helper" } });
    items = applyChatEvent(items, { event: "token", data: { text: "8", agent: "other" } });
    // 더 이어지지 않는 앞 말풍선은 닫힌다(런타임 전사와 같다)
    expect(items[1]).toMatchObject({ agent: "helper", text: "7", streaming: false });
    expect(items[2]).toMatchObject({ agent: "other", text: "8", streaming: true });
  });

  it("applyChatEvent folds tokens, attaches tool results and closes streaming on interrupt/done", () => {
    let items: ChatItem[] = [{ kind: "user", text: "hi" }];
    items = applyChatEvent(items, { event: "token", data: { text: "he" } });
    items = applyChatEvent(items, { event: "token", data: { text: "llo" } });
    expect(items[1]).toEqual({ kind: "assistant", text: "hello", agent: undefined, streaming: true });
    items = applyChatEvent(items, { event: "tool_call", data: { id: "c1", name: "Add", args: { a: 1 } } });
    expect(items[1]).toMatchObject({ streaming: false });
    items = applyChatEvent(items, { event: "tool_result", data: { id: "c1", name: "Add", status: "success", content: "7" } });
    expect(items[2]).toMatchObject({ kind: "tool_call", result: { content: "7" } });
    items = applyChatEvent(items, { event: "token", data: { text: "7", agent: "helper" } });
    items = applyChatEvent(items, { event: "interrupt", data: { session_id: "s", action_requests: [{ name: "Divide", args: {} }], review_configs: [] } });
    expect(items[3]).toMatchObject({ kind: "assistant", agent: "helper", streaming: false });
    expect(items[4]).toMatchObject({ kind: "interrupt", resolved: false });
    items = applyChatEvent(items, { event: "guardrail", data: { action: "block", message: "no" } });
    items = applyChatEvent(items, { event: "done", data: { session_id: "s", turn: 2, usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 }, tool_calls: 0, text: "" } });
    expect(items[5]).toMatchObject({ kind: "guardrail", action: "block" });
    expect(items[6]).toMatchObject({ kind: "done", turn: 2 });
  });
});
