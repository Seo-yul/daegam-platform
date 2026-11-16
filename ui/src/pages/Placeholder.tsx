import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { useT } from "@/i18n";

/** 아직 없는 화면(준비 중) 또는 라우트 밖 경로(not found). */
export function PlaceholderPage({ title, pending = false }: { title?: string; pending?: boolean }) {
  const t = useT();
  const text = t.ui.placeholder;
  return (
    <PageWidth data-testid="placeholder-page">
      <PageHeader title={title ?? t.detail.notFoundPage.title} testId="page-title" subtitle={pending ? text.pending : text.notFound} />
    </PageWidth>
  );
}
