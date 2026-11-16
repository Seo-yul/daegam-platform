/** 테스트용 monaco-editor 대체 (vite.config.ts test.alias). 데코레이션·언어 등록 코드가 참조하는 것만 둔다. */
export class Range {
  constructor(
    public startLineNumber: number,
    public startColumn: number,
    public endLineNumber: number,
    public endColumn: number,
  ) {}
}

/** monacoSetup 이 라이트 테마를 등록한다. 스텁은 받기만 한다. */
export const editor = {
  defineTheme: () => undefined,
};

/** MonacoJson 이 JSON 토크나이저를 등록한다. 스텁은 받기만 한다. */
export const languages = {
  getLanguages: (): { id: string }[] => [],
  register: () => undefined,
  setMonarchTokensProvider: () => undefined,
  setLanguageConfiguration: () => undefined,
};
