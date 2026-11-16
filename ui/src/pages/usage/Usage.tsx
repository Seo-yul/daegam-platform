import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { UsageReport } from "@/components/usage/UsageReport";
import { useT } from "@/i18n";

/** 테넌트의 사용량과 비용 (P37 13절). usage:read 가 있어야 연다. */
export function UsagePage() {
  const text = useT().usage;
  return (
    <PageWidth>
      <PageHeader title={text.title} subtitle={text.subtitle} testId="usage-header" />
      <UsageReport platform={false} />
    </PageWidth>
  );
}
