/**
 * Skills 목록 (D64, P39 4절). 카드와 표 두 모양이고 기본은 카드다(D47).
 *
 * 카드는 작은 카드(compact)다. 표시 이름, 설명 한 줄, 최신 리비전, 공유 배지, 고친 시각을 보이고 다른 테넌트가 연 Skill
 * 이면 소유 테넌트를 적는다. 클러스터에 뜨는 것이 없어 상태가 없다. 두 모양이 함께 쓰는 값은 `skillFacts` 한 곳에서
 * 만들고, 모양마다 그중 무엇을 보일지 고른다.
 */
import { Plus } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { listSkills } from "@/api/skills";
import type { Skill } from "@/api/types";
import { ListToolbar, Pagination, SearchInput, ViewToggle, useListMode, useListView } from "@/components/ListControls";
import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { ResourceDeleteDialog, RowDeleteButton } from "@/components/ResourceDeleteDialog";
import { StateBlock, StateRow } from "@/components/StateBlock";
import { SkillIllustration } from "@/components/illustrations";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { CardGrid, CardText, ListCard, editedLine } from "@/components/ui/card-grid";
import { ActionTD, ActionTH, ListTR, NameTD, Table, TBody, TD, TH, THead } from "@/components/ui/table";
import { type Messages, useT } from "@/i18n";
import type { DeleteTarget } from "@/lib/deletion";
import { nameSearchText } from "@/lib/names";
import { skillsKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";

/** 검색은 이름 둘과 설명까지 본다. 무엇을 하는 Skill 인지로 찾는 일이 많다. */
function searchText(skill: Skill): string {
  return `${nameSearchText(skill)} ${skill.latest.description}`;
}

/** 한 Skill 의 값. 카드와 표가 여기서 골라 쓴다. */
function skillFacts(t: Messages, skill: Skill, tenant: string) {
  return {
    revision: (
      <Badge variant="muted" data-testid={`skill-revision-${skill.name}`}>
        r{skill.revision}
      </Badge>
    ),
    shared:
      skill.scope === "shared" ? (
        <Badge variant="accent" data-testid={`skill-shared-${skill.name}`}>
          {t.skills.shared}
        </Badge>
      ) : null,
    // 다른 테넌트가 연 Skill 은 고치지 못하고 복제만 된다. 누구 것인지 보여야 그 사실이 읽힌다
    owner: skill.tenant === tenant ? null : skill.tenant,
    description: (
      <span className="line-clamp-2 text-muted" title={skill.latest.description} data-testid={`skill-description-${skill.name}`}>
        {skill.latest.description}
      </span>
    ),
  };
}

export function SkillListPage() {
  const t = useT();
  const text = t.skills.list;
  const tenant = useTenant();
  const q = useQuery({ queryKey: skillsKey(tenant), queryFn: listSkills });
  const [target, setTarget] = useState<DeleteTarget | null>(null);
  const [mode, setMode] = useListMode("skills");
  const view = useListView(q.data ?? [], searchText);

  // 못 불러온 것과 하나도 없는 것을 가른다. 오류를 "없습니다" 로 보이면 새로 만들게 된다
  const blocked = q.isLoading || Boolean(q.error) || !view.total;
  const state = {
    loading: q.isLoading,
    error: q.error,
    onRetry: () => void q.refetch(),
    illustration: <SkillIllustration />,
    empty: q.data?.length ? t.detail.listControls.noMatch(view.query) : text.empty(tenant),
  };
  const remove = (s: Skill) => setTarget({ id: s.id, name: s.name });

  return (
    <PageWidth data-testid="skills-page">
      <PageHeader
        title={text.title}
        testId="page-title"
        subtitle={q.data ? t.ui.deployment.summary.count(q.data.length) : undefined}
        primary={
          <Link to="/skills/new" className={buttonVariants()} data-testid="skill-new">
            <Plus size={16} aria-hidden />
            {t.skills.create}
          </Link>
        }
      />
      <ListToolbar>
        <SearchInput value={view.query} onChange={view.setQuery} placeholder={text.search} />
        <ViewToggle mode={mode} onChange={setMode} />
      </ListToolbar>
      {mode === "card" ? (
        <>
          {blocked ? (
            <StateBlock {...state} />
          ) : (
            // Skill 은 많이 쌓이는 목록이라 작은 카드를 넓은 화면에서 네 칸으로 깐다. 설명은 한 줄이고 나머지는 상세에서 읽는다
            <CardGrid testId="skill-cards" compact>
              {view.pageItems.map((s) => {
                const f = skillFacts(t, s, tenant);
                return (
                  <ListCard
                    key={s.id}
                    to={`/skills/${s.id}`}
                    item={s}
                    compact
                    data-testid={`skill-row-${s.name}`}
                    body={<CardText text={s.latest.description} testId={`skill-description-${s.name}`} />}
                    footer={
                      <>
                        {f.revision}
                        {f.shared}
                        {f.owner ? <span>{text.owner(<span data-testid={`skill-owner-${s.name}`}>{f.owner}</span>)}</span> : null}
                        <span>{editedLine(t, null, s.updated_at)}</span>
                      </>
                    }
                    menu={s.permissions.manage ? [{ label: t.common.delete, danger: true, testId: `row-delete-${s.name}`, onClick: () => remove(s) }] : []}
                  />
                );
              })}
            </CardGrid>
          )}
          <div className="mt-2">
            <Pagination view={view} />
          </div>
        </>
      ) : (
        <Card>
          <Table data-testid="skills-table">
            <THead>
              <tr>
                <TH>{text.columns.name}</TH>
                <TH>{text.columns.description}</TH>
                <TH>{text.columns.revision}</TH>
                <TH>{text.columns.owner}</TH>
                <ActionTH />
              </tr>
            </THead>
            <TBody>
              {blocked ? (
                <StateRow colSpan={5} {...state} />
              ) : (
                view.pageItems.map((s) => {
                  const f = skillFacts(t, s, tenant);
                  return (
                    <ListTR key={s.id} data-testid={`skill-row-${s.name}`}>
                      <NameTD to={`/skills/${s.id}`} item={s}>
                        {f.shared}
                      </NameTD>
                      <TD className="max-w-md text-sm">{f.description}</TD>
                      <TD>{f.revision}</TD>
                      <TD data-testid={`skill-owner-${s.name}`}>{f.owner ?? <span className="text-dim">-</span>}</TD>
                      <ActionTD>{s.permissions.manage ? <RowDeleteButton name={s.name} onClick={() => remove(s)} /> : null}</ActionTD>
                    </ListTR>
                  );
                })
              )}
            </TBody>
          </Table>
          <Pagination view={view} />
        </Card>
      )}
      <ResourceDeleteDialog resource="skill" target={target} onClose={() => setTarget(null)} />
    </PageWidth>
  );
}
