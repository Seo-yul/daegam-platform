/** Monaco 편집기 워커. vite 의 `?worker` import 라 별도 모듈로 두고 테스트에서는 스텁으로 바꾼다. */
import EditorWorker from "monaco-editor/editor/editor.worker?worker";

export function createWorker(): Worker {
  return new EditorWorker();
}
