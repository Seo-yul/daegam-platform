import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router";

import { Button, buttonVariants } from "@/components/ui/button";
import { Alert, Card, CardContent } from "@/components/ui/card";
import { useT } from "@/i18n";
import { handleCallback, login } from "@/lib/auth";

/**
 * /callback: OIDC Authorization Code 응답을 처리하고 원래 경로로 돌아간다. 처리는 한 페이지 로드에 한 번이다(handleCallback).
 * 다시 로그인한 첫 시도가 새 로그인을 받지 못해 둘째 시도로 떠나면 돌아갈 곳이 없어 이 화면에 머문다.
 */
export function CallbackPage() {
  const t = useT();
  const text = t.auth.callback;
  const navigate = useNavigate();
  // 라이브러리가 던진 오류의 원문이다. 화면 문장은 그릴 때 사전에서 고른다
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    handleCallback()
      .then((returnTo) => {
        if (returnTo !== null) navigate(returnTo || "/", { replace: true });
      })
      .catch((e: Error) => setError(e.message));
  }, [navigate]);
  if (error) {
    // 붉은 줄 하나만 남기면 주소창을 직접 고치는 수밖에 없다. 다시 해 볼 길과 나갈 길을 준다
    return (
      <Card className="max-w-xl">
        <CardContent className="flex flex-col items-start gap-3">
          <Alert variant="error" data-testid="callback-error">
            {text.failed}
            <div className="mt-1 text-xs opacity-80">{error}</div>
          </Alert>
          <div className="flex gap-2">
            <Button data-testid="callback-retry" onClick={() => void login()}>
              {text.retry}
            </Button>
            <Link to="/" className={buttonVariants({ variant: "outline" })} data-testid="callback-home">
              {text.home}
            </Link>
          </div>
        </CardContent>
      </Card>
    );
  }
  return (
    <p className="text-sm text-muted" data-testid="callback-page">
      {t.auth.signingIn}
    </p>
  );
}
