import { expect, test, type Locator } from "@playwright/test";

import { downloaded } from "./download";
import { selectValue } from "./select";

/**
 * P36 묶음 B(첨부와 세션 파일, D61)의 화면을 목 API(mock-server.ts) 위의 실제 브라우저로 본다. 단위 테스트(attachments.test.tsx·
 * attachments-lib.test.ts·files-drawer.test.tsx)가 jsdom 에서 흉내만 내는 것들이 여기서 돈다. 이미지 다시 굽기(createImageBitmap·캔버스·JPEG·PNG 인코딩과
 * 상한을 넘을 때 물러서기), 끌어 놓기와 붙여 넣기의 DataTransfer, object URL 로 저장하는 다운로드, 서랍을 닫은 뒤 돌아오는 포커스다.
 *
 * Agent 는 mock-server.ts 가 넣은 `ag-files`(첨부와 세션 파일을 아는 런타임)다. 목 상태가 서버 프로세스 전역이라 직렬로 돈다.
 */

const AGENT = "ag-files";
/** 다시 구운 화면 캡처의 크기. 3000x1000 을 긴 변 1568px 로 줄인다(lib/imageResize.ts). */
const CAPTURE = { width: 3000, height: 1000 };
const BAKED = [1568, 523];
/** 목 에이전트가 알린 이미지 하나의 상한(lib/attachments.ts 의 기본값). 업로드 본문은 multipart 머리만큼 더 크다. */
const IMAGE_LIMIT = 2 * 1024 * 1024;
const MULTIPART_SLACK = 1024;

/**
 * 페이지 안에서 PNG 를 그려 입력칸에 붙여 넣는다. 왼쪽 절반만 칠해 오른쪽이 투명하다. `noise` 면 왼쪽 화소마다 색과 알파가 제멋대로라
 * PNG 로는 거의 줄지 않는다.
 */
async function pastePng(input: Locator, image: { name: string; width: number; height: number; noise?: boolean }) {
  await input.evaluate(async (el, img) => {
    const canvas = document.createElement("canvas");
    canvas.width = img.width;
    canvas.height = img.height;
    const ctx = canvas.getContext("2d")!;
    if (img.noise) {
      const pixels = ctx.createImageData(img.width / 2, img.height);
      for (let i = 0; i < pixels.data.length; i++) pixels.data[i] = Math.floor(Math.random() * 256);
      ctx.putImageData(pixels, 0, 0);
    } else {
      ctx.fillStyle = "#336699";
      ctx.fillRect(0, 0, img.width / 2, img.height);
    }
    const blob = await new Promise<Blob>((resolve) => canvas.toBlob((b) => resolve(b!), "image/png"));
    const data = new DataTransfer();
    data.items.add(new File([blob], img.name, { type: "image/png" }));
    el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
  }, image);
}

test("클립으로 고른 문서, 끌어 놓은 글, 붙여 넣은 화면 캡처가 칩이 되고 보낸 말풍선 아래에 남는다", async ({ page }) => {
  await page.goto(`/agents/${AGENT}?tab=chat`);
  const input = page.getByTestId("chat-input");
  await expect(input).toBeVisible();
  const chip = (i: number) => page.getByTestId(`chat-attachments-${i}`);

  // 클립 단추가 여는 숨은 파일 칸. 목은 .pdf 를 12쪽으로 뽑는다
  await page.getByTestId("chat-attach-input").setInputFiles({ name: "환불규정.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4") });
  await expect(chip(0)).toHaveAttribute("data-status", "ready");
  await expect(chip(0)).toContainText("12쪽");

  // 끌어 놓기. 입력칸은 끌어 온 것의 types 에 "Files" 가 있을 때만 받는다
  const dropped = await page.evaluateHandle(() => {
    const data = new DataTransfer();
    data.items.add(new File(["다시 연락한다"], "메모.txt", { type: "text/plain" }));
    return data;
  });
  await input.dispatchEvent("dragover", { dataTransfer: dropped });
  await input.dispatchEvent("drop", { dataTransfer: dropped });
  await expect(chip(1)).toHaveAttribute("data-status", "ready");
  await expect(chip(1)).toContainText("메모.txt");

  // 붙여 넣기. 불투명한 큰 캡처는 업로드 전에 긴 변을 줄인 JPEG 로 다시 굽고 이름의 확장자도 맞춘다. 칩의 이름은 서버가 받은
  // 이름이고 썸네일은 구운 파일 그대로다
  await input.evaluate(async (el, size) => {
    const canvas = document.createElement("canvas");
    Object.assign(canvas, size);
    const ctx = canvas.getContext("2d")!;
    ctx.fillStyle = "#336699";
    ctx.fillRect(0, 0, size.width, size.height);
    const blob = await new Promise<Blob>((resolve) => canvas.toBlob((b) => resolve(b!), "image/png"));
    const data = new DataTransfer();
    data.items.add(new File([blob], "캡처.png", { type: "image/png" }));
    el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
  }, CAPTURE);
  await expect(chip(2)).toHaveAttribute("data-status", "ready");
  await expect(chip(2)).toContainText("캡처.jpg");
  const thumbnail = chip(2).locator("img");
  await expect(thumbnail).toHaveAttribute("src", /^data:image\/jpeg;base64,/);
  await expect.poll(() => thumbnail.evaluate((img: HTMLImageElement) => [img.naturalWidth, img.naturalHeight])).toEqual(BAKED);

  // 보내면 업로드 id 셋만 싣고, 입력칸의 칩은 비며 보낸 말풍선 아래에 칩 셋이 남는다
  await input.fill("첨부 셋을 보낸다");
  const sent = page.waitForRequest((r) => r.url().endsWith(`/agents/${AGENT}/chat`));
  await page.getByTestId("chat-send").click();
  expect(JSON.parse((await sent).postData() ?? "{}").attachments).toHaveLength(3);
  await expect(page.getByTestId("chat-attachments")).toHaveCount(0);
  const bubbleChips = page.getByTestId("chat-transcript").locator('[data-testid^="chat-message-"][data-testid$="-attachments"]');
  await expect(bubbleChips.locator("li")).toHaveCount(3);
  await expect(page.getByTestId("chat-error")).toHaveCount(0);
});

test("투명한 그림은 PNG 로 굽고, 상한을 넘는 투명한 그림은 흰 바탕 JPEG 로 물러서며, 전사 위에 끌어 놓은 파일도 첨부한다", async ({ page }) => {
  await page.goto(`/agents/${AGENT}?tab=chat`);
  const input = page.getByTestId("chat-input");
  await expect(input).toBeVisible();
  const chip = (i: number) => page.getByTestId(`chat-attachments-${i}`);
  const uploads = () => page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith(`/agents/${AGENT}/uploads`));

  // 오른쪽이 투명한 작은 그림은 투명도를 지키는 PNG 그대로다
  await pastePng(input, { name: "로고.png", width: 400, height: 300 });
  await expect(chip(0)).toHaveAttribute("data-status", "ready");
  await expect(chip(0)).toContainText("로고.png");
  await expect(chip(0).locator("img")).toHaveAttribute("src", /^data:image\/png;base64,/);

  // 알파까지 제멋대로인 1568px 그림은 PNG 로 2MiB 를 훌쩍 넘는다. 흰 바탕 JPEG 로 물러서고(필요하면 긴 변도 줄여) 상한 안으로 업로드한다
  const sent = uploads();
  await pastePng(input, { name: "잡음.png", width: 1568, height: 1568, noise: true });
  // 큰 본문은 Playwright 가 postData 로 넘기지 않아 브라우저가 단 Content-Length 로 잰다
  expect(Number(await (await sent).headerValue("content-length"))).toBeLessThanOrEqual(IMAGE_LIMIT + MULTIPART_SLACK);
  await expect(chip(1)).toHaveAttribute("data-status", "ready", { timeout: 15_000 });
  await expect(chip(1)).toContainText("잡음.jpg");
  const thumbnail = chip(1).locator("img");
  await expect(thumbnail).toHaveAttribute("src", /^data:image\/jpeg;base64,/);
  // 투명했던 오른쪽은 흰 바탕이다. 바탕을 깔지 않으면 JPEG 가 알파를 버리며 검게 칠한다
  const right = await thumbnail.evaluate(async (img: HTMLImageElement) => {
    await img.decode();
    const canvas = document.createElement("canvas");
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext("2d")!;
    ctx.drawImage(img, 0, 0);
    return { width: img.naturalWidth, pixel: Array.from(ctx.getImageData(Math.floor(img.naturalWidth * 0.9), Math.floor(img.naturalHeight / 2), 1, 1).data) };
  });
  expect(right.width).toBeLessThanOrEqual(1568);
  expect(Math.min(...right.pixel.slice(0, 3))).toBeGreaterThan(240);

  // 입력칸을 빗나가 전사 위에 놓아도 첨부하고, 브라우저가 파일을 열지 않게 기본 동작을 막는다
  const transcript = page.getByTestId("chat-transcript");
  const dropped = await page.evaluateHandle(() => {
    const data = new DataTransfer();
    data.items.add(new File(["옮겨 놓은 메모"], "옮긴.txt", { type: "text/plain" }));
    return data;
  });
  await transcript.dispatchEvent("dragenter", { dataTransfer: dropped });
  await expect(page.getByTestId("chat-column")).toHaveAttribute("data-drop-over", "true");
  const prevented = await transcript.evaluate((el, data) => {
    const drop = new DragEvent("drop", { dataTransfer: data, bubbles: true, cancelable: true });
    el.dispatchEvent(drop);
    return drop.defaultPrevented;
  }, dropped);
  expect(prevented).toBe(true);
  await expect(page.getByTestId("chat-column")).not.toHaveAttribute("data-drop-over");
  await expect(chip(2)).toHaveAttribute("data-status", "ready");
  await expect(chip(2)).toContainText("옮긴.txt");
});

test("파일 서랍은 업로드한 파일과 에이전트가 만든 파일을 나눠 보이고, 미리 보기와 다운로드가 돌며, 닫으면 파일 단추로 돌아간다", async ({ page }) => {
  await page.goto("/chat");
  await selectValue(page, "home-agent-select", AGENT);
  await page.getByTestId("home-session-s-1").click();
  const open = page.getByTestId("session-files-open");
  await open.click();
  const drawer = page.getByTestId("session-files");
  await expect(drawer.getByTestId("session-files-upload")).toContainText("업로드한 파일");
  await expect(drawer.getByTestId("session-file-/uploads/보고서.pdf.txt")).toBeVisible();
  await expect(drawer.getByTestId("session-files-agent")).toContainText("Agent 가 만든 파일");
  await expect(drawer.getByTestId("session-files-agent-notice")).toHaveText("에이전트가 파일 Tool 로 쓴 텍스트 파일입니다.");

  // .md 는 마크다운으로 그린다(지연 청크). 미리 보기 상한을 넘는 파일은 다운로드만 된다
  await drawer.getByTestId("session-file-preview-/reports/요약.md").click();
  await expect(drawer.getByTestId("session-file-content").locator("strong")).toHaveText("7일");
  await expect(drawer.getByTestId("session-file-preview-/data/큰 표.csv")).toHaveCount(0);

  // 헤더 인증이라 링크를 걸 수 없어 받아서 object URL 로 저장한다. 이름은 응답 헤더의 filename* 다
  const saved = await downloaded(page, drawer.getByTestId("session-file-download-/notes/할 일.txt"));
  expect(saved.name).toBe("할 일.txt");
  expect(saved.text).toBe("고객에게 다시 연락한다.");

  await page.keyboard.press("Escape");
  await expect(drawer).toHaveCount(0);
  await expect(open).toBeFocused();
});
