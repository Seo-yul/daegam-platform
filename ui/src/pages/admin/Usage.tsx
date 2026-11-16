import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { UsageReport } from "@/components/usage/UsageReport";
import { useT } from "@/i18n";

/** 플랫폼 전체의 사용량, 가격 규칙이 없는 모델, 원장 건강 (P37 13절). platform-admin 만 연다. */
export function PlatformUsagePage() {
  const text = useT().usage;
  return (
    <PageWidth>
      <PageHeader title={text.platformTitle} subtitle={text.subtitle} testId="platform-usage-header" />
      <UsageReport platform />
    </PageWidth>
  );
}
