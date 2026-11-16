/**
 * 화면 언어 전환 (P49). 계정 메뉴의 한 줄이다 (P81 5절). 언어마다 자기 말로 적은 이름을 단추로 두고 사전을 거치지
 * 않는다. 읽지 못하는 화면에서도 자기 언어를 찾을 수 있어야 해서다. 머리글 "Language" 는 테마의 "Theme" 처럼 두 언어에서 같다.
 */
import { MENU_ROW } from "@/components/ui/menu-row";
import { useT } from "@/i18n";
import { LANGUAGES, LANGUAGE_NAMES, setLanguage, useLanguage } from "@/lib/language";
import { cn } from "@/lib/utils";

export function LanguageSwitch() {
  const t = useT();
  const language = useLanguage();
  return (
    <div role="group" aria-label={t.shell.languageSwitch.label} data-testid="language-switch" className={cn(MENU_ROW, "justify-between hover:bg-transparent")}>
      <span>{t.shell.languageSwitch.label}</span>
      <span className="inline-flex gap-0.5 rounded-item border border-line bg-sunken p-0.5">
        {LANGUAGES.map((option) => (
          <button
            key={option}
            type="button"
            lang={option}
            aria-pressed={option === language}
            data-testid={`language-option-${option}`}
            className={cn(
              "rounded-badge px-2 py-0.5 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring",
              option === language ? "bg-surface font-semibold text-heading shadow-hairline" : "text-muted hover:text-fg",
            )}
            onClick={() => setLanguage(option)}
          >
            {LANGUAGE_NAMES[option]}
          </button>
        ))}
      </span>
    </div>
  );
}
