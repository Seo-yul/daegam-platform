/**
 * 이미지를 업로드하기 전에 브라우저에서 다시 굽는다 (P36 9절).
 *
 * `createImageBitmap` 으로 풀어 긴 변을 1568px 이하로 캔버스에 그리고, 투명한 곳이 있는 이미지는 PNG 로, 나머지는 JPEG 품질
 * 0.85 로 굽는다. 작은 이미지도 늘 다시 구워 EXIF 의 위치 정보가 서버로 가지 않는다. 1568px 는 모델이 이미지를 줄여 보는
 * 긴 변이라 그보다 큰 이미지는 토큰만 더 쓴다. 이름의 확장자는 구운 형식으로 바꾼다. 서버는 확장자로 형식을 가르고 런타임은
 * 그 형식을 매직 바이트와 맞춰 보기 때문이다.
 *
 * 구운 파일이 바이트 상한을 넘으면 물러선다. PNG 는 같은 크기의 흰 바탕 JPEG 로 다시 굽는다. 투명한 화소가 하나라도 있으면 PNG 라
 * 1568px PNG 는 2MiB 를 쉽게 넘는다. JPEG 도 넘으면 긴 변을 0.75배씩 줄여 다시 굽고, 그래도 넘으면 마지막 판을 돌려줘 부르는
 * 쪽이 상한 문구로 거절한다. 풀지 못한 이미지는 `ImageDecodeError` 다. 손상된 파일이거나 브라우저가 풀기에 너무 큰 이미지다.
 */
import { splitExtension } from "@/lib/utils";

/** 다시 구운 이미지의 긴 변 상한(px). */
export const MAX_IMAGE_SIDE = 1568;
export const JPEG_QUALITY = 0.85;
/** 상한을 넘는 JPEG 의 긴 변을 한 번에 줄이는 비율. 여섯 번 줄이면 1568px 가 약 280px 가 된다. */
export const SHRINK_FACTOR = 0.75;
const MAX_SHRINKS = 6;

const PNG = "image/png";
const JPEG = "image/jpeg";
const EXTENSION: Record<string, string> = { [PNG]: ".png", [JPEG]: ".jpg" };
/** 알파 값이 이것보다 작은 화소가 하나라도 있으면 투명한 이미지다. */
const OPAQUE = 255;
/** JPEG 로 물러설 때 투명한 곳에 까는 바탕. 깔지 않으면 JPEG 에서 검게 된다. */
const JPEG_BACKGROUND = "#ffffff";

/** 이미지를 풀지 못했다. 손상된 파일이거나 브라우저가 풀기에 너무 큰 이미지다. */
export class ImageDecodeError extends Error {}

async function decode(file: File): Promise<ImageBitmap> {
  try {
    return await createImageBitmap(file, { imageOrientation: "from-image" });
  } catch (e) {
    throw new ImageDecodeError(e instanceof Error ? e.message : String(e));
  }
}

/** 긴 변을 `side` 안으로 줄여 캔버스에 그린다. 그보다 작으면 그대로다. `background` 를 주면 먼저 깐다. */
function paint(bitmap: ImageBitmap, side: number, background?: string): { canvas: HTMLCanvasElement; ctx: CanvasRenderingContext2D } {
  const scale = Math.min(1, side / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("canvas is not available");
  if (background) {
    ctx.fillStyle = background;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }
  ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  return { canvas, ctx };
}

function hasTransparency(ctx: CanvasRenderingContext2D, width: number, height: number): boolean {
  const { data } = ctx.getImageData(0, 0, width, height);
  for (let i = 3; i < data.length; i += 4) if (data[i] < OPAQUE) return true;
  return false;
}

function toBlob(canvas: HTMLCanvasElement, type: string): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("image encoding failed"))), type, type === JPEG ? JPEG_QUALITY : undefined);
  });
}

/** 흰 바탕 JPEG 로 긴 변 `side` 안에 굽는다. */
function bakeJpeg(bitmap: ImageBitmap, side: number): Promise<Blob> {
  return toBlob(paint(bitmap, side, JPEG_BACKGROUND).canvas, JPEG);
}

function renamed(name: string, type: string): string {
  return `${splitExtension(name).stem}${EXTENSION[type]}`;
}

/**
 * 이미지 하나를 `maxBytes` 안으로 다시 구운 파일. 물러서도 넘으면 마지막 판이고 크기는 부르는 쪽이 다시 잰다. JPEG 는 투명한 곳이
 * 없으므로 화소를 보지 않는다.
 */
export async function rebakeImage(file: File, maxBytes: number): Promise<File> {
  const bitmap = await decode(file);
  try {
    let side = Math.min(MAX_IMAGE_SIDE, Math.max(bitmap.width, bitmap.height));
    const { canvas, ctx } = paint(bitmap, side);
    let type = file.type !== JPEG && hasTransparency(ctx, canvas.width, canvas.height) ? PNG : JPEG;
    let blob = await toBlob(canvas, type);
    if (blob.size > maxBytes && type === PNG) {
      type = JPEG;
      blob = await bakeJpeg(bitmap, side);
    }
    for (let i = 0; blob.size > maxBytes && i < MAX_SHRINKS; i++) {
      side = Math.round(side * SHRINK_FACTOR);
      blob = await bakeJpeg(bitmap, side);
    }
    return new File([blob], renamed(file.name, type), { type });
  } finally {
    bitmap.close();
  }
}
