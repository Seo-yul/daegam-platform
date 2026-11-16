/**
 * 사전 함수의 견본 인자 (P49). 영역마다 파일 하나이고 키는 `영역.화면.역할` 경로다. 인자를 받는 함수를 사전에 더하면 그
 * 영역 파일에 견본을 더한다. 묶음마다 두 언어를 불러 영어에 한글이 없는지 본다. 문장을 고르는 인자는 값마다 묶음을 둔다.
 * 첫 묶음의 문자열과 1000 아래의 수는 두 언어의 출력에 모두 나와야 하고(두 로캘이 같게 적는 수), React 요소는 그
 * 마크업이 나와야 한다. 문장을 고르기만 하고 출력에 적지 않는 매개변수는 `selectors` 에 위치를 적는다.
 * 영역 파일은 `samples` 와 `selectors` 를 이름 그대로 내보낸다.
 */
export interface SampleFile {
  samples: Readonly<Record<string, readonly (readonly unknown[])[]>>;
  selectors?: Readonly<Record<string, readonly number[]>>;
}
