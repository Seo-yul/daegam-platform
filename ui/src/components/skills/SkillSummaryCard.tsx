/**
 * Skill 상세 개요의 요약 (D64, P39 4절). 최신 리비전 머리말의 description·license·compatibility·allowed-tools·metadata 와
 * 소유 테넌트·범위를 보인다.
 *
 * allowed-tools 는 규격이 실험 기능이라 하고 우리는 집행하지 않는다. 도구와 승인은 캔버스의 tools 노드가 정하므로 칩 옆에
 * 권한이 아니라는 말을 붙여 읽는 사람이 오해하지 않게 한다.
 */
import type { Skill, SkillFrontmatter } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useT } from "@/i18n";
import { scopeHint } from "@/lib/hubAccess";
import { formatBytes, formatDate } from "@/lib/utils";

export function SkillSummaryCard({ skill, frontmatter }: { skill: Skill; frontmatter: SkillFrontmatter | undefined }) {
  const t = useT();
  const text = t.skills.summary;
  const metadata = Object.entries(frontmatter?.metadata ?? {});
  return (
    <Card data-testid="skill-summary">
      <CardHeader>
        <CardTitle>{t.detail.summaryFields.title}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="whitespace-pre-wrap text-sm text-fg" data-testid="skill-description">
          {frontmatter?.description ?? skill.latest.description}
        </p>
        <dl className="grid grid-cols-[9rem_1fr] gap-y-1 text-sm">
          <dt className="text-muted">{text.tenant}</dt>
          <dd data-testid="skill-owner">{skill.tenant}</dd>
          <dt className="text-muted">{text.scope}</dt>
          <dd data-testid="skill-scope">
            {skill.scope}
            <span className="ml-2 text-xs text-muted">{scopeHint(t, skill.scope, t.skills.access.owner)}</span>
          </dd>
          {frontmatter?.license ? (
            <>
              <dt className="text-muted">{text.license}</dt>
              <dd data-testid="skill-license">{frontmatter.license}</dd>
            </>
          ) : null}
          {frontmatter?.compatibility ? (
            <>
              <dt className="text-muted">{text.compatibility}</dt>
              <dd className="whitespace-pre-wrap" data-testid="skill-compatibility">
                {frontmatter.compatibility}
              </dd>
            </>
          ) : null}
          {frontmatter?.allowed_tools.length ? (
            <>
              <dt className="text-muted">{text.allowedTools}</dt>
              <dd className="flex flex-wrap items-center gap-1" data-testid="skill-allowed-tools">
                {frontmatter.allowed_tools.map((tool) => (
                  <Badge key={tool} variant="muted" className="font-mono">
                    {tool}
                  </Badge>
                ))}
                <span className="text-xs text-muted" data-testid="skill-allowed-tools-note">
                  {text.allowedToolsNote}
                </span>
              </dd>
            </>
          ) : null}
          {metadata.map(([key, value]) => (
            <div key={key} className="contents">
              <dt className="truncate font-mono text-xs text-muted">metadata.{key}</dt>
              <dd className="break-all" data-testid={`skill-metadata-${key}`}>
                {value}
              </dd>
            </div>
          ))}
          <dt className="text-muted">{text.labels.size}</dt>
          <dd>{text.size(formatBytes(skill.latest.bytes), skill.latest.files.length)}</dd>
          <dt className="text-muted">{t.detail.summaryFields.updated}</dt>
          <dd>
            {formatDate(t, skill.updated_at)}
            {skill.updated_by ? ` ${t.shared.byUser(skill.updated_by)}` : ""}
          </dd>
        </dl>
      </CardContent>
    </Card>
  );
}
