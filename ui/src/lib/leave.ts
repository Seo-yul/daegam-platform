/**
 * 이 앱 밖의 주소로 떠나는 창구. MCP 연결 동의가 서버가 준 돌아갈 주소(이 컴퓨터의 클라이언트)로 간다 (P33 7절).
 * jsdom 의 `location.assign` 은 바꿔 끼울 수 없는 속성이라 테스트는 이 객체의 `assign` 을 바꾼다.
 */
export const leave = {
  assign(url: string): void {
    window.location.assign(url);
  },
};
