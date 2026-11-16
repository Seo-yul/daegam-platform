/** 테스트용 components/monacoWorker 대체. */
export function createWorker(): Worker {
  throw new Error("no worker in tests");
}
