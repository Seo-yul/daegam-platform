/**
 * Skill 상세의 리비전 탭 (D64, P39 4절). 위는 리비전 목록(메모, 만든 사람, 시각, digest 앞 12자)이고 아래는 두 리비전의
 * 파일별 차이(`MonacoDiff`)다. 복제로 만든 r1 은 메모 자리에 그 출처(`acme/demo r3 에서 복제`)를 적는다.
 *
 * 두 리비전 고르기는 MCP Server·Gateway 의 정의 탭과 같은 고르개(`overview/RevisionDiff.tsx`)다. 바뀐 파일은 목록 응답의
 * sha256 으로 고르므로 파일 본문은 차이를 볼 때만 받는다. 쓴 리비전은 바뀌지 않아 본문 캐시는 오래 둔다. 좌우로 나눌지는
 * 스펙 다듬기와 같은 폭 규칙(`useNarrowerThan`)으로 정한다.
 */
import { useQuery } from "@tanstack/react-query";
import { lazy, Suspense, useState } from "react";

import { exportSkillRevision, listSkillRevisions } from "@/api/skills";
import type { Skill, SkillRevisionMeta } from "@/api/types";
import { DownloadButton } from "@/components/chat/DownloadButton";
import { EditorSkeleton } from "@/components/EditorSkeleton";
import { RevisionDiffSelects, useRevisionPair } from "@/components/overview/RevisionDiff";
import { StateBlock } from "@/components/StateBlock";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { FileListButton } from "@/components/ui/file-list";
import { ActionTH, Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { DIFF_SIDE_BY_SIDE_MIN, useNarrowerThan } from "@/lib/media";
import { skillRevisionsKey } from "@/lib/queryKeys";
import { type FileChange, fileLanguage, revisionChanges, shortDigest } from "@/lib/skills";
import { useTenant } from "@/lib/tenant";
import { formatDate } from "@/lib/utils";

import { useSkillRevision } from "./useSkillRevision";

const MonacoDiff = lazy(() => import("@/components/MonacoDiff"));

const DIFF_HEIGHT = "28rem";

const CHANGE_VARIANT: Record<FileChange, BadgeProps["variant"]> = { added: "ok", removed: "warn", changed: "info" };

export function SkillRevisionsCard({ skill }: { skill: Skill }) {
  const t = useT();
  const text = t.skills.revisions;
  const tenant = useTenant();
  const q = useQuery({ queryKey: skillRevisionsKey(tenant, skill.id), queryFn: () => listSkillRevisions(skill.id) });
  const revisions = q.data ?? [];

  return (
    <div className="flex flex-col gap-4">
      <Card data-testid="skill-revisions-card">
        <CardHeader>
          <CardTitle>{text.title}</CardTitle>
          <span className="text-xs text-muted">{text.latest(skill.revision)}</span>
        </CardHeader>
        <CardContent>
          <StateBlock loading={q.isLoading} error={q.error} onRetry={() => void q.refetch()}>
            <Table data-testid="skill-revisions-table">
              <THead>
                <tr>
                  <TH>{text.columns.revision}</TH>
                  <TH>{text.columns.note}</TH>
                  <TH>{text.columns.author}</TH>
                  <TH>{text.columns.time}</TH>
                  <TH>{text.digest}</TH>
                  <ActionTH />
                </tr>
              </THead>
              <TBody>
                {revisions.map((r) => (
                  <TR key={r.revision} data-testid={`skill-revision-${r.revision}`}>
                    <TD className="font-mono">r{r.revision}</TD>
                    <TD>
                      <RevisionNote revision={r} />
                    </TD>
                    <TD>{r.created_by ?? "-"}</TD>
                    <TD>{formatDate(t, r.created_at)}</TD>
                    <TD className="font-mono text-xs" title={r.digest} data-testid={`skill-revision-digest-${r.revision}`}>
                      {shortDigest(r.digest)}
                    </TD>
                    <TD className="text-right">
                      <div className="flex flex-wrap justify-end gap-1">
                        <DownloadButton plain variant="ghost" label={t.skills.exportRevision(r.revision)} testId={`skill-revision-export-${r.revision}`} download={() => exportSkillRevision(skill, r.revision)}>
                          {t.shared.export}
                        </DownloadButton>
                      </div>
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </StateBlock>
        </CardContent>
      </Card>
      {/* 다른 Skill 로 옮겨 가면 고른 두 리비전과 파일을 처음부터 다시 정한다 */}
      {revisions.length > 1 ? <RevisionDiff key={skill.id} skill={skill} revisions={revisions} /> : null}
    </div>
  );
}

/** 메모 칸. 복제로 만든 리비전은 출처를 함께 적는다. 둘 다 없으면 `-` 다. */
function RevisionNote({ revision }: { revision: SkillRevisionMeta }) {
  const t = useT();
  const source = revision.cloned_from;
  if (!revision.note && !source) return <span className="text-dim">-</span>;
  return (
    <>
      {source ? (
        <span className="block text-muted" data-testid={`skill-revision-cloned-${revision.revision}`}>
          {t.skills.revisions.clonedFrom(source.tenant, source.name, source.revision)}
        </span>
      ) : null}
      {revision.note}
    </>
  );
}

function RevisionDiff({ skill, revisions }: { skill: Skill; revisions: SkillRevisionMeta[] }) {
  const t = useT();
  const text = t.skills.revisions;
  // 기본은 바로 앞 리비전과 최신이다. 목록은 새것부터 온다
  const pair = useRevisionPair(revisions, "newest-first");
  const [picked, setPicked] = useState<string | null>(null);
  const [box, narrow] = useNarrowerThan<HTMLDivElement>(DIFF_SIDE_BY_SIDE_MIN);

  const changes = revisionChanges(pair.revFrom?.files ?? [], pair.revTo?.files ?? []);
  const path = changes.some((c) => c.path === picked) ? picked : (changes[0]?.path ?? null);
  const before = useSkillRevision(skill.id, pair.revFrom?.revision, path !== null);
  const after = useSkillRevision(skill.id, pair.revTo?.revision, path !== null);
  const contentOf = (files: { path: string; content: string }[] | undefined) => files?.find((f) => f.path === path)?.content ?? "";

  return (
    <Card data-testid="skill-diff-card">
      <CardHeader>
        <CardTitle>{text.compare}</CardTitle>
        <RevisionDiffSelects revisions={revisions} state={pair} />
      </CardHeader>
      <CardContent className="grid gap-4 lg:grid-cols-[15rem_minmax(0,1fr)]">
        <ul className="flex min-w-0 flex-col gap-0.5" aria-label={text.changedFiles} data-testid="skill-diff-files">
          {changes.length ? (
            changes.map((c) => (
              <li key={c.path}>
                <FileListButton active={c.path === path} testId={`skill-diff-file-${c.path}`} className="font-mono" onClick={() => setPicked(c.path)}>
                  <Badge variant={CHANGE_VARIANT[c.change]}>{t.detail.specDiff.kind[c.change]}</Badge>
                  <span className="min-w-0 truncate">{c.path}</span>
                </FileListButton>
              </li>
            ))
          ) : (
            <li className="text-sm text-muted" data-testid="skill-diff-same">
              {text.same}
            </li>
          )}
        </ul>
        <div ref={box} className="min-w-0">
          {path ? (
            <StateBlock loading={before.isLoading || after.isLoading} error={before.error ?? after.error}>
              <Suspense fallback={<EditorSkeleton height={DIFF_HEIGHT} />}>
                <MonacoDiff
                  key={`${path}:${narrow ? "inline" : "split"}`}
                  original={contentOf(before.data?.files)}
                  modified={contentOf(after.data?.files)}
                  sideBySide={!narrow}
                  inlineBreakpoint={DIFF_SIDE_BY_SIDE_MIN}
                  language={fileLanguage(path)}
                  height={DIFF_HEIGHT}
                  testId="skill-diff-editor"
                />
              </Suspense>
            </StateBlock>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}
