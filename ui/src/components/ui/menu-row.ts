/**
 * 계정 메뉴의 한 줄 (P81 5절). 링크, 단추, 테마와 언어 줄이 같은 높이와 여백을 쓴다. 메뉴 밖의 단추가 이 꼴을 쓰면
 * 메뉴 줄로 보이므로 계정 메뉴 안에만 쓴다.
 */
export const MENU_ROW =
  "flex h-8 w-full items-center gap-2.5 rounded-item px-2 text-left text-[13.5px] text-fg transition-colors hover:bg-hover hover:text-heading focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring [&>svg]:shrink-0 [&>svg]:text-muted";
