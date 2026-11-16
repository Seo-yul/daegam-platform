import { vi } from "vitest";

import { stubProperty } from "./globals";

export interface CanvasStub {
  width?: number;
  height?: number;
  /** 255 보다 작으면 투명한 화소가 있는 이미지다. */
  alpha?: number;
  /** 구운 판의 바이트 수. 형식과 캔버스 너비로 정한다. 주지 않으면 몇 바이트다. */
  bytes?: (type: string, width: number) => number;
}

/**
 * 다시 굽기(lib/imageResize.ts)가 부르는 브라우저 그림 API 를 흉내 낸다. jsdom 에는 createImageBitmap 도 캔버스 그리기도 없다.
 * 부른 테스트 동안만 꽂히고 돌려준 목으로 그린 크기와 구운 형식을 본다.
 */
export function stubCanvas({ width = 100, height = 80, alpha = 255, bytes }: CanvasStub = {}) {
  const close = vi.fn();
  const bitmap = vi.fn(async () => ({ width, height, close }));
  stubProperty(window, "createImageBitmap", bitmap);
  const drawImage = vi.fn();
  const fillRect = vi.fn();
  const getContext = vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
    () => ({ drawImage, fillRect, getImageData: () => ({ data: new Uint8ClampedArray([1, 2, 3, alpha, 1, 2, 3, 255]) }) }) as unknown as CanvasRenderingContext2D,
  );
  const toBlob = vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation(function (this: HTMLCanvasElement, callback, type = "image/png") {
    callback(new Blob([bytes ? new Uint8Array(bytes(type, this.width)) : "baked"], { type }));
  });
  return { bitmap, close, drawImage, fillRect, getContext, toBlob };
}

/** 붙여 넣거나 고른 PNG 이미지. 다시 굽기 흉내는 내용을 읽지 않는다. */
export function png(name = "화면.png"): File {
  return new File(["\x89PNG raw with exif"], name, { type: "image/png" });
}
