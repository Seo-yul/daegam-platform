/**
 * 화면 하나가 그리다 터져도 앱 전체가 사라지지 않게 막는 경계.
 *
 * React 는 렌더 중 예외를 잡아 주는 곳이 없으면 트리 전체를 버린다. 그래서 탭 하나를 그리다 난
 * `TypeError` 하나에 사이드바까지 통째로 날아가고 흰 화면이 남는다. 실제로 Agent 메트릭 탭이
 * 그랬다. 서버 응답 모양이 화면이 믿던 것과 달라 값 하나를 읽다 터졌는데, 사람이 본 것은
 * "무엇이 잘못됐다" 가 아니라 아무것도 없는 화면이었다. 돌아갈 길조차 없다.
 *
 * 이 경계는 껍데기 안쪽, 본문만 감싼다. 사이드바와 머리 줄은 살아 있어야 다른 곳으로 옮겨 갈 수
 * 있다. 그리고 경로나 질의가 바뀌면 오류를 지운다. 다른 화면으로 옮겼는데 앞 화면의 오류가 계속
 * 남아 있으면 그것도 고장이다.
 *
 * 여기는 마지막 그물이지 오류를 다루는 자리가 아니다. 서버가 낸 오류는 `QueryGate` 가 화면 안에서
 * 말한다. 이 경계까지 왔다는 것은 우리 코드가 못 견딘 것이므로 콘솔에도 남긴다.
 */
import { Component, type ErrorInfo, type ReactNode } from "react";
import { useLocation } from "react-router";

import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { useT } from "@/i18n";

interface Props {
  /** 이 값이 바뀌면 오류를 지우고 다시 그린다. 경로 + 질의다. */
  resetKey: string;
  children: ReactNode;
}

interface State {
  error: Error | null;
  /** 오류를 잡았을 때의 resetKey. 이것과 달라지면 화면을 옮긴 것이다. */
  key: string;
}

class Boundary extends Component<Props, State> {
  state: State = { error: null, key: this.props.resetKey };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    if (props.resetKey !== state.key) return { error: null, key: props.resetKey };
    return null;
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 개발자가 콘솔에서 읽는 말이라 사전 없이 영어로 적는다 (P49 5절)
    // eslint-disable-next-line no-console
    console.error("Failed to render the screen", error, info.componentStack);
  }

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return <RenderError error={error} onRetry={() => this.setState({ error: null })} />;
  }
}

/**
 * 경계가 본문 자리에 세우는 문구. 클래스 컴포넌트는 훅을 못 쓰므로 사전은 이 함수 컴포넌트가 `useT()` 로 읽는다. 그래야
 * 언어를 바꾸면 이 문구도 다시 그려진다.
 */
function RenderError({ error, onRetry }: { error: Error; onRetry: () => void }) {
  const t = useT();
  const text = t.shared.errorBoundary;
  return (
    <div className="flex flex-col gap-3 py-8" data-testid="render-error">
      <Alert variant="error">
        <div className="font-medium">{text.title}</div>
        {/* 우리 코드가 낸 오류다. 문구를 감추면 무엇이 잘못됐는지 물어볼 말조차 없어진다 */}
        <p className="mt-1 font-mono text-xs" data-testid="render-error-message">
          {error.message}
        </p>
        <p className="mt-2 text-xs">{text.help}</p>
      </Alert>
      <div>
        <Button variant="secondary" data-testid="render-error-retry" onClick={onRetry}>
          {text.retry}
        </Button>
      </div>
    </div>
  );
}

/** 경로와 질의가 바뀌면 저절로 풀리는 경계. 껍데기의 본문이 이것으로 `Outlet` 을 감싼다. */
export function RouteErrorBoundary({ children }: { children: ReactNode }) {
  const location = useLocation();
  return <Boundary resetKey={`${location.pathname}${location.search}`}>{children}</Boundary>;
}
