/** 첨부의 순수 규칙 (P36 9절): 형식 판정, 칩의 사실, 되살린 전사의 첨부, 받은 수 판정과 이미지 다시 굽기. */
import { beforeEach, describe, expect, it } from "vitest";

import { lastTurn, rewindLastTurn, type ChatItem } from "@/components/chat";
import { dictionary } from "@/i18n";
import { itemsFromTranscript } from "@/lib/assistant";
import { IMAGE_ACCEPT, attachmentKind, attachmentsLost, factsText, refusalText, transcriptAttachments } from "@/lib/attachments";
import { JPEG_QUALITY, MAX_IMAGE_SIDE, SHRINK_FACTOR, rebakeImage } from "@/lib/imageResize";
import { DOCUMENT_ACCEPT } from "@/lib/knowledge";

import { png, stubCanvas } from "./images";
import { ATTACHMENTS_FIXTURE, TURN_FIXTURE } from "./runtimeFixtures";

describe("첨부 규칙", () => {
  it("형식은 확장자가 먼저이고 이미지는 media type 으로도 가르며 받는 확장자는 서버 표와 같다", () => {
    expect(attachmentKind({ name: "보고서.PDF", type: "" })).toBe("document");
    expect(attachmentKind({ name: "화면.jpeg", type: "" })).toBe("image");
    // 붙여 넣은 화면 캡처는 이름에 확장자가 없을 수 있다
    expect(attachmentKind({ name: "image", type: "image/png" })).toBe("image");
    expect(attachmentKind({ name: "설치.exe", type: "application/x-msdownload" })).toBeNull();
    expect(IMAGE_ACCEPT.split(",")).toEqual([".gif", ".jpeg", ".jpg", ".png", ".webp"]);
  });

  it("받지 않는 형식의 문구는 받는 확장자 목록에서 만들어 빠지는 형식이 없다", () => {
    const languages = [dictionary("ko"), dictionary("en")];
    const formatErrors = languages.map((dict) => dict.errors.codes.unsupported_format);
    const refusals = languages.map((dict) => refusalText(dict, { code: "unsupported" }));
    for (const ext of DOCUMENT_ACCEPT.split(",").filter((e) => ![".markdown", ".htm"].includes(e))) {
      for (const text of [...formatErrors, ...refusals]) expect(text).toContain(ext.slice(1));
    }
    for (const text of refusals) expect(text).toContain("gif · jpg · png · webp");
  });

  it("런타임 표본의 첨부 턴 done 이 보낸 수만큼 받았다고 말하는 칸을 화면이 읽는다", () => {
    // 목은 이 칸 이름으로 받은 수를 싣는다. 런타임이 이름을 바꾸면 표본이 바뀌어 여기서 걸린다
    const sent = transcriptAttachments((ATTACHMENTS_FIXTURE.transcript[0] as { attachments?: unknown }).attachments).length;
    expect(sent).toBe(3);
    expect(attachmentsLost(sent, { event: "done", data: ATTACHMENTS_FIXTURE.events.done })).toBe(false);
    expect(attachmentsLost(sent, { event: "done", data: TURN_FIXTURE.events.done })).toBe(true);
  });

  it("칩의 사실은 쪽이나 시트, 글자 수이고 이미지는 크기다", () => {
    const ko = dictionary("ko");
    expect(factsText(ko, { name: "a.pdf", kind: "document", bytes: 9, pages: 12, textChars: 38211 })).toBe("12쪽 · 38,211자");
    expect(factsText(ko, { name: "a.xlsx", kind: "document", bytes: 9, sheets: 3 })).toBe("시트 3개");
    expect(factsText(ko, { name: "a.png", kind: "image", bytes: 204800 })).toBe("200.0 KB");
  });

  it("되살린 전사의 사용자 항목은 첨부를 이름과 크기로 들고, 메시지 수정과 다시 생성도 칩을 남긴다", () => {
    const items = itemsFromTranscript(ATTACHMENTS_FIXTURE.transcript);
    const user = items[0] as Extract<ChatItem, { kind: "user" }>;
    expect(user.attachments).toEqual([
      { name: "보고서.pdf", kind: "document", bytes: 98213, pages: 12, sheets: null, truncated: false },
      { name: "매출.xlsx", kind: "document", bytes: 1500000, pages: null, sheets: 3, truncated: true },
      { name: "화면.png", kind: "image", bytes: 204800, pages: null, sheets: null, truncated: false },
    ]);
    const turn = lastTurn(items);
    expect(turn).not.toBeNull();
    const edited = rewindLastTurn(items, turn!, "고친 질문");
    expect(edited.at(-1)).toEqual({ ...user, text: "고친 질문" });
    // 모양이 틀린 항목은 버린다
    expect(transcriptAttachments([{ kind: "text" }, null, "x"])).toEqual([]);
  });
});

describe("이미지 다시 굽기", () => {
  const LIMIT = 1000;
  let canvas: ReturnType<typeof stubCanvas>;

  beforeEach(() => {
    canvas = stubCanvas();
  });

  it("작은 이미지도 다시 구워 JPEG 로 바꾸고 이름의 확장자를 맞춘다", async () => {
    const baked = await rebakeImage(png("작은.png"), LIMIT);
    expect(canvas.bitmap).toHaveBeenCalledOnce();
    expect(canvas.drawImage).toHaveBeenCalledWith(expect.anything(), 0, 0, 100, 80);
    expect(canvas.toBlob).toHaveBeenCalledWith(expect.any(Function), "image/jpeg", JPEG_QUALITY);
    expect([baked.name, baked.type]).toEqual(["작은.jpg", "image/jpeg"]);
    expect(canvas.close).toHaveBeenCalledOnce();
  });

  it("투명한 곳이 있으면 PNG 로 굽고 긴 변을 상한으로 줄인다", async () => {
    const stub = stubCanvas({ width: 3136, height: 2000, alpha: 0 });
    const baked = await rebakeImage(new File(["x"], "로고.webp", { type: "image/webp" }), LIMIT);
    expect(stub.drawImage).toHaveBeenCalledWith(expect.anything(), 0, 0, MAX_IMAGE_SIDE, 1000);
    expect(stub.toBlob).toHaveBeenCalledWith(expect.any(Function), "image/png", undefined);
    expect([baked.name, baked.type]).toEqual(["로고.png", "image/png"]);
  });

  it("PNG 가 상한을 넘으면 같은 크기의 흰 바탕 JPEG 로 물러선다", async () => {
    const stub = stubCanvas({ width: 1568, height: 1000, alpha: 0, bytes: (type) => (type === "image/png" ? LIMIT * 4 : LIMIT / 2) });
    const baked = await rebakeImage(png("도표.png"), LIMIT);
    expect([baked.name, baked.type, baked.size]).toEqual(["도표.jpg", "image/jpeg", LIMIT / 2]);
    expect(stub.fillRect).toHaveBeenCalledWith(0, 0, 1568, 1000);
    expect(stub.drawImage).toHaveBeenLastCalledWith(expect.anything(), 0, 0, 1568, 1000);
  });

  it("JPEG 도 상한을 넘으면 긴 변을 0.75배씩 줄여 들어갈 때까지 다시 굽는다", async () => {
    const side = MAX_IMAGE_SIDE * SHRINK_FACTOR * SHRINK_FACTOR;
    const stub = stubCanvas({ width: 3136, height: 3136, bytes: (_, width) => (width > side ? LIMIT * 2 : LIMIT) });
    const baked = await rebakeImage(png("사진.png"), LIMIT);
    expect([baked.type, baked.size]).toEqual(["image/jpeg", LIMIT]);
    expect(stub.drawImage).toHaveBeenLastCalledWith(expect.anything(), 0, 0, Math.round(side), Math.round(side));
    expect(stub.toBlob).toHaveBeenCalledTimes(3);
  });
});
