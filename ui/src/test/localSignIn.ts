import { screen } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";

import { LOCAL_ADMIN_PASSWORD } from "./msw";

/**
 * local 모드(D40)의 로그인 화면에서 목의 관리자로 들어간다. 토큰 없이 연 화면을 껍데기가 /login 으로 보낸 뒤에
 * 부르고, 들어가면 원래 가려던 경로로 돌아온다. 소속을 바꿔 보려면 테스트가 GET /me 를 덮는다.
 */
export async function signInLocal(user: UserEvent, password = LOCAL_ADMIN_PASSWORD): Promise<void> {
  await user.type(await screen.findByTestId("login-username"), "admin");
  await user.type(screen.getByTestId("login-password"), password);
  await user.click(screen.getByTestId("login-submit"));
}
