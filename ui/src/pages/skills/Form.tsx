/**
 * Skill 만들기와 새 리비전 (D64, P39 4절). `/skills/new` 는 r1 을 만들고 `/skills/:id/edit` 는 최신 리비전을 채워 새 리비전을
 * 넣는다. 고친다는 것이 곧 새 리비전이라 두 화면이 한 폼이다(`pages/README.md` 의 `Form.tsx`).
 *
 * 탭은 둘이다. "직접 작성" 은 SKILL.md 와 곁 파일을 Monaco 로 쓰고, 입력이 멈추면 서버가 검사해 문제를 파일 옆에 보인다.
 * "가져오기" 는 zip 이나 SKILL.md 를 올린다. 리소스 이름은 SKILL.md 머리말의 name 이라 만들기의 이름 칸은 그 줄을 읽고 고친다.
 * 새 리비전은 고치기 시작할 때 본 번호를 expect_revision 으로 보내 그사이 다른 사람이 넣은 리비전을 덮지 않는다
 * (409 revision_conflict). 표시 이름은 리비전이 아니라 Skill 의 것이라 PATCH 로 바꾼다. 파일이 그대로면 PATCH 만 보낸다.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Navigate, useNavigate, useParams } from "react-router";

import { addSkillRevision, createSkill, getSkill, importSkill, importSkillRevision, updateSkill } from "@/api/skills";
import type { Skill, SkillFileInput, SkillImported, SkillIssue } from "@/api/types";
import { PageHeader } from "@/components/Layout";
import { NameFields, isNameFieldError, nameFieldErrors, type NameValue } from "@/components/NameFields";
import { NotFoundPage } from "@/components/NotFoundPage";
import { PageWidth } from "@/components/PageWidth";
import { queryGate } from "@/components/QueryGate";
import { SkillFilesEditor } from "@/components/skills/SkillFilesEditor";
import { SkillImportField } from "@/components/skills/SkillImportField";
import { SkillSaveError } from "@/components/skills/SkillIssues";
import { useSkillRevision } from "@/components/skills/useSkillRevision";
import { useSkillValidation, type SkillValidationState } from "@/components/skills/useSkillValidation";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/input";
import { TabBar } from "@/components/ui/tabs";
import { type Messages, useT } from "@/i18n";
import { displayName as shownName } from "@/lib/names";
import { skillKey, skillsKey } from "@/lib/queryKeys";
import { SKILL_LIMITS, SKILL_MD, errorsIn, frontmatterName, invalidSkillIssues, skillTemplate, withFrontmatterName } from "@/lib/skills";
import { useTabParam } from "@/lib/tabs";
import { useTenant } from "@/lib/tenant";
import { useDirty, useUnsavedGuard } from "@/lib/unsavedGuard";
import { missingResource } from "@/lib/warningText";

import type { SkillImportState } from "./Detail";

const TABS = ["write", "import"] as const;
type Tab = (typeof TABS)[number];

export function SkillFormPage() {
  const { id } = useParams();
  // 다른 Skill 의 새 리비전으로 옮겨 가면 폼을 새로 연다. 같은 Skill 의 번호가 오른 것으로는 다시 열지 않는다(OpenedRevision)
  return id ? <NewRevision key={id} id={id} /> : <NewSkill />;
}

/** 새 Skill. 틀은 폼을 여는 순간의 화면 언어로 한 번 적고, 그 뒤 언어를 바꿔도 사람이 쓰는 글이라 다시 적지 않는다. */
function NewSkill() {
  const t = useT();
  const [initialFiles] = useState(() => [{ path: SKILL_MD, content: skillTemplate(t, "") }]);
  return <SkillForm initialFiles={initialFiles} />;
}

function NewRevision({ id }: { id: string }) {
  const t = useT();
  const tenant = useTenant();
  const q = useQuery({ queryKey: skillKey(tenant, id), queryFn: () => getSkill(id) });
  if (missingResource(q.error, q.data)) {
    return <NotFoundPage resource={t.resources.kind.skill} backTo="/skills" backLabel={t.skills.back} />;
  }
  return queryGate(q) ?? <OpenedRevision skill={q.data!} />;
}

/**
 * 폼을 연 때의 Skill 과 그 최신 리비전을 붙잡는다. 편집하는 동안 다른 사람이 리비전을 넣어 상세 조회의 번호가 올라도 폼은
 * 그대로이고, 처음 본 번호를 expect_revision 으로 보내 409 revision_conflict 를 받는다. 번호를 따라 폼을 다시 열면 고친 글이
 * 말없이 사라지고 남의 리비전을 덮는 저장이 통과한다.
 */
function OpenedRevision({ skill: current }: { skill: Skill }) {
  const [skill] = useState(() => current);
  const latest = useSkillRevision(skill.id, skill.revision);
  // 새 리비전은 manage 가 있어야 넣는다. 저장할 수 없는 폼을 열어 두지 않고 상세로 돌려보낸다(D43)
  if (!skill.permissions.manage) return <Navigate to={`/skills/${skill.id}`} replace />;
  const gate = queryGate({ ...latest, isLoading: !latest.data && !latest.error });
  if (gate) return gate;
  return <SkillForm skill={skill} initialFiles={latest.data!.files.map(({ path, content }) => ({ path, content }))} />;
}

/** 두 파일 목록이 경로와 내용까지 같은가. 차례는 따지지 않는다. */
function sameFiles(a: readonly SkillFileInput[], b: readonly SkillFileInput[]): boolean {
  const key = (files: readonly SkillFileInput[]) => JSON.stringify([...files].sort((x, y) => x.path.localeCompare(y.path)));
  return key(a) === key(b);
}

function SkillForm({ skill, initialFiles }: { skill?: Skill; initialFiles: SkillFileInput[] }) {
  const t = useT();
  const text = t.skills.form;
  const tabLabel: Record<Tab, string> = { write: text.write, import: text.import };
  const tenant = useTenant();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [tab, setTab] = useTabParam(TABS, "write");
  const [files, setFiles] = useState(initialFiles);
  const [touched, setTouched] = useState(false);
  const [display, setDisplay] = useState(skill ? shownName(skill) : "");
  const [note, setNote] = useState("");
  const [upload, setUpload] = useState<File | null>(null);
  const validation = useSkillValidation(files, skill?.name, touched);

  const skillMd = files.find((f) => f.path === SKILL_MD)?.content ?? "";
  // 새 리비전의 이름은 바뀌지 않는다. 머리말을 다르게 고치면 검사가 name_mismatch 로 알린다
  const names: NameValue = { displayName: display, name: skill?.name ?? frontmatterName(skillMd) };
  const displayInput = display.trim() || undefined;

  const save = useMutation({
    mutationFn: async () => {
      const memo = note.trim() || undefined;
      if (!skill) return createSkill({ files, display_name: displayInput, note: memo });
      const renamed = display.trim() !== shownName(skill);
      let out = skill;
      // 파일이 그대로면 리비전을 만들지 않는다. 표시 이름만 바꾸는 저장이 422 skill_unchanged 에 막히지 않게 한다. 둘 다
      // 그대로면 서버가 skill_unchanged 로 그 사실을 말하게 보낸다
      if (!renamed || !sameFiles(files, initialFiles)) out = await addSkillRevision(skill.id, { files, note: memo, expect_revision: skill.revision });
      if (renamed) out = await updateSkill(skill.id, { display_name: display.trim() });
      return out;
    },
    onSuccess: saved,
  });
  const importing = useMutation({
    mutationFn: (file: File) => (skill ? importSkillRevision(skill.id, file, skill.revision) : importSkill(file, displayInput)),
    onSuccess: saved,
  });
  // 저장이 받은 422 의 문제는 파일 옆에 선다. 검사 결과와 같은 자리라 두 번 그리지 않는다. 다시 고치면 검사 결과로 돌아간다
  const saveIssues = invalidSkillIssues(save.error);
  const issues = saveIssues ?? validation.result?.issues ?? [];
  // 저장하지 않은 입력을 두고 나가려 하면 묻는다 (D43 다섯째). 올린 파일은 이름으로 센다. 새 리비전은 최신 리비전을 채운
  // 상태로 열리므로 처음 그린 값이 기준이다. 저장이나 가져오기가 시작되면 지킬 것이 없다
  const dirty = useDirty(JSON.stringify({ files, display, note, upload: upload?.name ?? null }));
  const submitted = [save, importing].some((m) => m.isPending || m.isSuccess);
  const confirmLeave = useUnsavedGuard(dirty && !submitted);

  function changeFiles(next: SkillFileInput[]) {
    setFiles(next);
    setTouched(true);
    save.reset();
  }
  // 만들기의 이름 칸은 머리말의 name 줄을 고친다. 리소스 이름의 원본은 SKILL.md 하나다
  function changeNames(next: NameValue) {
    setDisplay(next.displayName);
    if (!skill && next.name !== names.name) changeFiles(files.map((f) => (f.path === SKILL_MD ? { ...f, content: withFrontmatterName(f.content, next.name) } : f)));
  }

  async function saved(made: Skill | SkillImported) {
    // 목록 키가 상세·리비전 키의 앞부분이라 이 테넌트의 Skill 캐시를 한 번에 비운다
    await queryClient.invalidateQueries({ queryKey: skillsKey(tenant) });
    const skipped = "skipped" in made ? made.skipped : [];
    const state: SkillImportState | undefined = skipped.length ? { skipped } : undefined;
    navigate(`/skills/${made.id}`, { state });
  }
  const cancel = () => {
    if (confirmLeave()) navigate(skill ? `/skills/${skill.id}` : "/skills");
  };

  return (
    <PageWidth data-testid="skill-form-page" className="flex flex-col gap-4">
      <PageHeader
        title={skill ? text.newRevisionTitle(shownName(skill)) : t.skills.create}
        name={skill?.name}
        subtitle={skill ? text.revisionSubtitle(skill.revision) : text.createSubtitle}
        testId="page-title"
      />
      <TabBar tabs={TABS} value={tab} onChange={setTab} label={(value) => tabLabel[value]} />
      {tab === "write" ? (
        <Card>
          <CardContent>
            <form
              className="flex flex-col gap-4"
              data-testid="skill-form"
              onSubmit={(e) => {
                e.preventDefault();
                save.mutate();
              }}
            >
              <NameFields
                testId="skill"
                inline
                placeholders={text.placeholders}
                value={names}
                onChange={changeNames}
                readOnlyName={Boolean(skill)}
                errors={nameFieldErrors(save.error)}
                nameHint={skill ? undefined : text.nameHint}
              />
              <Field label={text.note} htmlFor="skill-note" hint={text.noteHint}>
                <Input id="skill-note" data-testid="skill-note" maxLength={SKILL_LIMITS.note} value={note} onChange={(e) => setNote(e.target.value)} />
              </Field>
              <ValidationLine state={validation} touched={touched} saveIssues={saveIssues} />
              <SkillFilesEditor files={files} onChange={changeFiles} issues={issues} disabled={save.isPending} />
              <SkillSaveError error={isNameFieldError(save.error) ? null : save.error} withIssues={false} testId="skill-save-error" />
              <div className="flex justify-end gap-2">
                <Button variant="ghost" onClick={cancel}>
                  {t.common.cancel}
                </Button>
                <Button type="submit" data-testid="skill-save" disabled={save.isPending}>
                  {save.isPending ? t.shared.saving : skill ? text.saveRevision : t.shared.create}
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="flex flex-col gap-4">
            {skill ? null : (
              <Field label={text.displayName} htmlFor="skill-import-display" hint={text.importDisplayHint}>
                <Input id="skill-import-display" data-testid="skill-import-display" value={display} onChange={(e) => setDisplay(e.target.value)} />
              </Field>
            )}
            <SkillImportField file={upload} onFile={setUpload} disabled={importing.isPending} />
            <SkillSaveError error={importing.error} testId="skill-import-error" />
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={cancel}>
                {t.common.cancel}
              </Button>
              <Button data-testid="skill-import-submit" disabled={!upload || importing.isPending} onClick={() => upload && importing.mutate(upload)}>
                {importing.isPending ? text.importing : text.import}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}
    </PageWidth>
  );
}

/**
 * 검사 결과 한 줄. 문제의 자리는 파일 옆이 말하고 여기는 수만 센다. 저장이 422 로 돌려준 문제가 있으면 그것을 센다(파일 옆에
 * 선 것도 그것이다).
 */
function ValidationLine({ state, touched, saveIssues }: { state: SkillValidationState; touched: boolean; saveIssues: SkillIssue[] | null }) {
  const t = useT();
  const text = t.skills.form.validation;
  const issues = saveIssues ?? state.result?.issues ?? [];
  const errors = errorsIn(issues).length;
  const line = saveIssues
    ? countText(t, errors, issues.length - errors)
    : !touched
      ? text.idle
      : state.checking
        ? text.checking
        : state.error
          ? text.failed
          : countText(t, errors, issues.length - errors);
  return (
    <p className={errors ? "text-xs text-err-fg" : "text-xs text-muted"} data-testid="skill-validation" aria-live="polite">
      {line}
    </p>
  );
}

function countText(t: Messages, errors: number, warnings: number): string {
  const text = t.skills.form.validation;
  if (errors) return text.errors(errors, warnings);
  if (warnings) return text.warnings(warnings);
  return text.clean;
}
