/**
 * Backend API 버전 행의 "Generate" 메뉴 (D25). 두 항목이 Refine 화면으로 간다.
 *   규칙으로 정리   → /backend-apis/:id/versions/:n/refine?mode=minify
 *   LLM 으로 다듬기 → /backend-apis/:id/versions/:n/refine?mode=llm
 *
 * 포털·키보드·자리 잡기는 `ui/action-menu` 가 한다 (D44 둘째로 그리로 옮겼다). 여기 남은 것은 항목 둘과
 * 트리거 모양뿐이다. 행 클릭(버전 선택)과 겹치지 않게 트리거·패널의 클릭을 전파하지 않는 것도 그쪽 규칙이다.
 */
import { ActionMenu, type MenuItem } from "@/components/ui/action-menu";
import { Button } from "@/components/ui/button";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

/** 다듬는 방식. 이름은 사전의 `shared.generateMenu.mode` 이고 Refine 화면의 제목도 그것을 쓴다. */
export type RefineMode = "minify" | "llm";

export function refinePath(profileId: string, version: number, mode: RefineMode): string {
  return `/backend-apis/${encodeURIComponent(profileId)}/versions/${version}/refine?mode=${mode}`;
}

export function GenerateMenu({ profileId, version }: { profileId: string; version: number }) {
  const t = useT();
  const text = t.shared.generateMenu;
  const items: MenuItem[] = (["minify", "llm"] as RefineMode[]).map((mode) => ({
    label: text.mode[mode],
    hint: text.hint[mode],
    to: refinePath(profileId, version, mode),
    testId: `generate-${mode}`,
  }));

  return (
    <ActionMenu
      items={items}
      label={text.label}
      panelTestId="generate-menu-panel"
      panelClassName="w-64"
      trigger={(props, open) => (
        <Button
          {...props}
          size="sm"
          variant="outline"
          data-testid="generate-menu"
          title={text.title(version)}
        >
          {text.label}
          <svg aria-hidden="true" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" className={cn("size-3.5 transition-transform duration-150", open && "rotate-180")}>
            <path d="M4 6l4 4 4-4" />
          </svg>
        </Button>
      )}
    />
  );
}
