/**
 * lazy Monaco 가 받아지는 동안의 자리. 편집기와 같은 높이를 미리 차지해 받아진 뒤에 아래 줄이 밀려나지 않게 한다.
 */
export function EditorSkeleton({ height }: { height: string }) {
  return <div className="animate-pulse rounded-md border border-line bg-hover" style={{ height }} aria-hidden />;
}
