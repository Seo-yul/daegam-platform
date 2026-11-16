/**
 * Skill 상세 (D64, P39 4절). 탭은 개요·파일·리비전·접근 정책 넷이다.
 *
 * 주 동작은 "새 리비전" 이고 내보내기는 낱말 단추, 복제·삭제는 다른 상세와 같은 아이콘 단추(`DetailActions`)로 제목 줄에 선다.
 * 고치면 새 리비전이 쌓이고 이전 리비전은 바뀌지 않으므로 편집 화면이 따로 없고 새 리비전 화면(`Form.tsx`)이 최신 리비전을 채워
 * 연다. 다른 테넌트가 연 Skill 은 consume 이 있으면 복제해서 고친다.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";

import { cloneSkill, exportSkillRevision, getSkill, setSkillAccess } from "@/api/skills";
import { AccessPolicyCard } from "@/components/AccessPolicyCard";
import { DownloadButton } from "@/components/chat/DownloadButton";
import { CloneDialog } from "@/components/detail/CloneDialog";
import { DetailActions } from "@/components/DetailActions";
import { PageHeader } from "@/components/Layout";
import { NotFoundPage } from "@/components/NotFoundPage";
import { PageWidth } from "@/components/PageWidth";
import { queryGate } from "@/components/QueryGate";
import { ReferencesCard } from "@/components/ReferencesCard";
import { ResourceDeleteDialog } from "@/components/ResourceDeleteDialog";
import { StateBlock } from "@/components/StateBlock";
import { SkillFileBrowser } from "@/components/skills/SkillFileBrowser";
import { SkillRevisionsCard } from "@/components/skills/SkillRevisionsCard";
import { SkillSummaryCard } from "@/components/skills/SkillSummaryCard";
import { useSkillRevision } from "@/components/skills/useSkillRevision";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { TabBar } from "@/components/ui/tabs";
import { useT } from "@/i18n";
import { hasScope, useAuthState } from "@/lib/auth";
import { canShare } from "@/lib/hubAccess";
import { displayName, secondaryName } from "@/lib/names";
import { skillKey, skillsKey } from "@/lib/queryKeys";
import { SKILL_TENANT_ACTIONS, skillActionLabels } from "@/lib/skills";
import { useTabParam } from "@/lib/tabs";
import { useTenant } from "@/lib/tenant";
import { missingResource } from "@/lib/warningText";

const TABS = ["overview", "files", "revisions", "access"] as const;
type Tab = (typeof TABS)[number];

/** 가져오기가 상세로 넘기는 것. 건너뛴 zip 항목을 한 번 알린다. */
export interface SkillImportState {
  skipped?: string[];
}

export function SkillDetailPage() {
  const t = useT();
  const text = t.skills.detail;
  const { id = "" } = useParams();
  const tenant = useTenant();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const { me } = useAuthState();
  const [tab, setTab] = useTabParam(TABS, "overview");
  const q = useQuery({ queryKey: skillKey(tenant, id), queryFn: () => getSkill(id) });
  // 개요의 머리말과 파일 탭이 최신 리비전의 본문을 쓴다
  const latest = useSkillRevision(id, q.data?.revision, tab === "overview" || tab === "files");
  const [deleting, setDeleting] = useState(false);
  const [cloning, setCloning] = useState(false);
  // 가져오기가 넘긴 건너뛴 항목은 한 번만 알린다. 붙잡은 뒤 기록의 state 를 지워 새로고침·뒤로 가기로 다시 뜨지 않게 한다
  const [imported] = useState(() => ({ id, skipped: (location.state as SkillImportState | null)?.skipped ?? [] }));
  useEffect(() => {
    if (location.state) navigate(`${location.pathname}${location.search}`, { replace: true, state: null });
  }, [location.state, location.pathname, location.search, navigate]);

  if (missingResource(q.error, q.data)) {
    return <NotFoundPage resource={t.resources.kind.skill} backTo="/skills" backLabel={t.skills.back} />;
  }
  const gate = queryGate(q);
  if (gate) return gate;
  const skill = q.data!;
  const manage = skill.permissions.manage;
  const skipped = imported.id === skill.id ? imported.skipped : [];
  const otherTenant = skill.tenant !== tenant;
  // 복제는 요청 테넌트에 새 Skill 을 만드는 쓰기라 skill:write 와 원본의 consume 이 둘 다 있어야 한다
  const canClone = hasScope(me, tenant, "skill:write") && skill.permissions.consume;
  const tabLabel: Record<Tab, string> = { overview: t.ui.tabs.standard.overview, files: text.tabs.files, revisions: text.tabs.revisions, access: t.hub.detail.tabs.access };

  return (
    <PageWidth data-testid="skill-detail-page" className="flex flex-col gap-4">
      <PageHeader
        title={displayName(skill)}
        name={secondaryName(skill)}
        testId="page-title"
        badges={
          <>
            <Badge variant="muted" data-testid="skill-revision">
              r{skill.revision}
            </Badge>
            {skill.scope === "shared" ? (
              <Badge variant="accent" data-testid="skill-shared">
                {t.skills.shared}
              </Badge>
            ) : null}
            {otherTenant ? (
              <Badge variant="info" data-testid="skill-owner-badge">
                {text.ownerBadge(skill.tenant)}
              </Badge>
            ) : null}
          </>
        }
        primary={
          manage ? (
            <Link to={`/skills/${skill.id}/edit`} className={buttonVariants()} data-testid="skill-new-revision">
              {text.newRevision}
            </Link>
          ) : null
        }
        secondary={
          <>
            {/* 실패 문장이 단추 아래로 내려가도록 감싼다(DownloadButton 의 오류 줄은 basis-full 이다) */}
            <span className="flex max-w-xs flex-wrap justify-end">
              <DownloadButton variant="secondary" size="default" label={t.skills.exportRevision(skill.revision)} testId="skill-export" download={() => exportSkillRevision(skill, skill.revision)}>
                {t.shared.export}
              </DownloadButton>
            </span>
            <DetailActions onClone={canClone ? () => setCloning(true) : undefined} onDelete={manage ? () => setDeleting(true) : undefined} />
          </>
        }
      />
      {skipped.length ? (
        <Alert variant="info" data-testid="skill-import-skipped">
          {text.skipped(skipped.join(", "))}
        </Alert>
      ) : null}

      <TabBar tabs={TABS} value={tab} onChange={setTab} label={(value) => tabLabel[value]} />

      {tab === "overview" ? (
        <div className="grid gap-4 lg:grid-cols-2">
          <SkillSummaryCard skill={skill} frontmatter={latest.data?.frontmatter} />
          <ReferencesCard resource="skill" id={skill.id} hint={text.referencesHint} latestRevision={skill.revision} />
        </div>
      ) : null}

      {tab === "files" ? (
        latest.data ? (
          <SkillFileBrowser key={skill.id} files={latest.data.files} revision={latest.data.revision} />
        ) : (
          <StateBlock loading={latest.isLoading} error={latest.error} onRetry={() => void latest.refetch()} />
        )
      ) : null}

      {tab === "revisions" ? <SkillRevisionsCard skill={skill} /> : null}

      {tab === "access" ? (
        <AccessPolicyCard
          item={skill}
          testId="skill"
          save={(access) => setSkillAccess(skill.id, access)}
          onSaved={async (updated) => {
            queryClient.setQueryData(skillKey(tenant, skill.id), updated);
            await queryClient.invalidateQueries({ queryKey: skillsKey(tenant) });
          }}
          editor={{ canShare: canShare(me, tenant, "skill:share"), shareScope: "skill:share", tenantActions: SKILL_TENANT_ACTIONS, labels: skillActionLabels(t), ownerLabel: t.skills.access.owner }}
        />
      ) : null}

      <CloneDialog
        open={cloning}
        onClose={() => setCloning(false)}
        resource={t.resources.kind.skill}
        collection="skills"
        source={skill}
        sourceNoun="definition"
        description={text.cloneBody(skill.revision)}
        placeholders={text.clonePlaceholders}
        clone={(body) => cloneSkill(skill.id, { name: body.name, display_name: body.display_name })}
      />
      <ResourceDeleteDialog resource="skill" target={deleting ? { id: skill.id, name: skill.name } : null} onClose={() => setDeleting(false)} onDeleted={() => navigate("/skills")} />
    </PageWidth>
  );
}
