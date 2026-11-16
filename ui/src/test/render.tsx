import { QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { MemoryRouter } from "react-router";

import { makeQueryClient } from "@/App";

/**
 * 화면 조각 하나를 앱과 같은 라우터·쿼리 클라이언트 안에 그린다. 앱 전체는 TestApp 으로 그린다.
 * 링크를 그리는 부품에는 라우터가, 서버 상태를 읽는 부품에는 쿼리 클라이언트가 있어야 한다. rerender 도 같은 둘 안에 그린다.
 */
export function renderWithProviders(ui: ReactElement, { initialPath = "/" }: { initialPath?: string } = {}): RenderResult {
  const client = makeQueryClient();
  function Providers({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[initialPath]}>{children}</MemoryRouter>
      </QueryClientProvider>
    );
  }
  return render(ui, { wrapper: Providers });
}
