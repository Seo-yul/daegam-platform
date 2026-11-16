/// <reference types="vite/client" />

/** vite.config.ts 의 define. 빌드 시점의 UI 버전 (package.json version + 커밋). */
declare const __UI_BUILD__: string;
declare const __UI_RELEASE__: string | null;
