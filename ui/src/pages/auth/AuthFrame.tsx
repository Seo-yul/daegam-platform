import type { ReactNode } from "react";

import { DaegamMark } from "@/components/icons/brand";
import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";

/**
 * 껍데기(Layout) 밖 인증 화면의 틀. 한지색 바탕 가운데 카드 하나에 로고와 제목을 둔다.
 * 로그인(D40)과 MCP 클라이언트 연결 동의(P33)가 같이 쓴다. 카드는 메뉴·대화상자처럼 떠 있는 면이라 깊은 그림자를 받는다 (P81 3절).
 */
export function AuthFrame({ title, testId, wide = false, children }: { title: ReactNode; testId: string; wide?: boolean; children: ReactNode }) {
  return (
    <main className="flex min-h-screen items-center justify-center bg-app px-4" data-testid={testId}>
      <Card className={cn("w-full shadow-popover", wide ? "max-w-md" : "max-w-sm")}>
        <CardContent className="flex flex-col gap-5 px-6 py-6">
          <div className="flex items-center gap-2.5">
            <DaegamMark size={28} className="shrink-0" aria-hidden />
            <h1 className="text-lg font-bold tracking-[-0.01em] text-heading">{title}</h1>
          </div>
          {children}
        </CardContent>
      </Card>
    </main>
  );
}
