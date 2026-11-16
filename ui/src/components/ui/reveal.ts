/**
 * 포인터를 올리거나 초점이 올 때만 보이는 곁 단추의 클래스 (P36). 포인터가 없는 화면(터치)에서는 늘 보인다. 묶음 안의 어느
 * 요소에 초점이 와도 보이므로 키보드로 옮겨 다닐 때 숨은 단추에 초점이 가지 않는다. Tailwind 는 소스의 정적 문자열만 읽으므로
 * 그룹 이름마다 상수를 따로 둔다.
 */

/** 채팅 말풍선(`group/bubble`) 곁의 복사·수정 단추. */
export const BUBBLE_ACTION_REVEAL = "opacity-0 group-focus-within/bubble:opacity-100 group-hover/bubble:opacity-100 [@media(hover:none)]:opacity-100";

/** 대화 목록 행(`group/row`) 끝의 더보기·휴지통 단추. */
export const ROW_ACTION_REVEAL = "opacity-0 group-focus-within/row:opacity-100 group-hover/row:opacity-100 [@media(hover:none)]:opacity-100";
