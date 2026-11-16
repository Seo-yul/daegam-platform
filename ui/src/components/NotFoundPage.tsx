/**
 * 없는 리소스를 열었을 때의 화면.
 *
 * 지금 상세 라우트는 404 를 빨간 줄 하나로만 보여 준다. 지워진 리소스의 북마크나 오래된 감사 행의
 * 링크를 따라온 사람에게는 제목도 다음 행동도 없어 사이드바 말고는 나갈 길이 없다. 여기서는
 * 무엇을 못 찾았는지 말하고 목록으로 돌아가는 길을 준다.
 */
import { Link } from "react-router";

import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useT } from "@/i18n";

export interface NotFoundPageProps {
  /** 못 찾은 것의 이름. 화면에 쓰는 리소스 이름 그대로다 ("MCP Server", "Knowledge Base"). */
  resource: string;
  /** 돌아갈 목록 경로. */
  backTo: string;
  /** 돌아가는 링크의 문구. 기본은 사전의 "목록으로 돌아가기" 다. */
  backLabel?: string;
}

export function NotFoundPage({ resource, backTo, backLabel }: NotFoundPageProps) {
  const t = useT();
  const text = t.detail.notFoundPage;
  return (
    <Card data-testid="not-found-page">
      <CardContent className="flex flex-col items-start gap-3 py-10">
        <h1 className="text-2xl leading-tight font-bold tracking-[-0.015em] text-heading" data-testid="not-found-title">
          {text.title}
        </h1>
        <p className="text-sm text-muted">{text.body(resource)}</p>
        <Link to={backTo} className={buttonVariants({ variant: "outline" })} data-testid="not-found-back">
          {backLabel ?? text.back}
        </Link>
      </CardContent>
    </Card>
  );
}
