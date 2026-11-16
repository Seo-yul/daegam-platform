/**
 * Skill 픽스처와 핸들러 (D64, P39 3절): /skills (PATCH·import·validate·access·clone·revisions·archive·revisions/import), skillState.
 *
 * 형식 검사는 서버 한 곳이라 목은 화면이 그리는 갈래만 흉내 낸다. validate 는 머리말 name 이 이름 규칙에 맞지 않으면 오류,
 * `scripts/` 아래 파일이면 경고를 낸다. 새 리비전은 expect_revision 이 최신과 다르면 409 revision_conflict, 파일이 최신과 같으면
 * 422 skill_unchanged 다. 복제한 r1 은 출처를 `cloned_from` 에 남기고 메모는 비운다.
 * 참조(`/skills/{id}/references`)는 references.ts 의 표가 답한다.
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { HubAccessInput, Skill, SkillFile, SkillFileInput, SkillIssue, SkillRevision } from "@/api/types";
import { emptyAccess, normalizeAccess } from "@/lib/hubAccess";
import { K8S_NAME_RE } from "@/lib/names";
import { SKILL_MD, frontmatterName } from "@/lib/skills";

import { NOW, apiError, docStore, downloadHeaders, http, record } from "./core";
import { ALL_PERMISSIONS, deriveScope } from "./hub";

const OWNER = "default";

function isScript(path: string): boolean {
  return path.startsWith("scripts/") || /\.(py|sh|js|ts)$/.test(path);
}

/** 목의 sha256 자리. 내용이 같으면 같은 값이라 리비전 차이를 흉내 낼 수 있다. */
function fakeSha(content: string): string {
  let h = 0;
  for (const ch of content) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h.toString(16).padStart(64, "0");
}

function file(path: string, content: string): SkillFile {
  return { path, content, bytes: content.length, sha256: fakeSha(content), script: isScript(path) };
}

export function skillRevisionFixture(skillId: string, revision: number, files: SkillFile[], over: Partial<SkillRevision> = {}): SkillRevision {
  return {
    id: `${skillId}-r${revision}`,
    skill_id: skillId,
    tenant: OWNER,
    revision,
    files,
    frontmatter: { name: "report-style", description: "보고서를 쓸 때 문체와 형식을 맞춥니다.", license: null, compatibility: null, metadata: {}, allowed_tools: [] },
    digest: `sha256:${fakeSha(files.map((f) => f.sha256).join(""))}`,
    bytes: files.reduce((n, f) => n + f.bytes, 0),
    note: null,
    cloned_from: null,
    created_at: NOW,
    created_by: "dev@example.com",
    ...over,
  };
}

function latestOf(rev: SkillRevision): Skill["latest"] {
  return {
    description: rev.frontmatter.description,
    digest: rev.digest,
    bytes: rev.bytes,
    files: rev.files.map(({ path, bytes, sha256, script }) => ({ path, bytes, sha256, script })),
  };
}

export function skillFixture(rev: SkillRevision, over: Partial<Skill> = {}): Skill {
  return {
    id: rev.skill_id,
    tenant: OWNER,
    name: rev.frontmatter.name,
    display_name: rev.frontmatter.name,
    revision: rev.revision,
    latest: latestOf(rev),
    access: emptyAccess(),
    access_updated_by: null,
    access_updated_at: null,
    created_at: NOW,
    updated_at: NOW,
    updated_by: "dev@example.com",
    permissions: { ...ALL_PERMISSIONS },
    scope: "tenant",
    ...over,
  };
}

const REPORT_MD_R1 = "---\nname: report-style\ndescription: 보고서를 쓸 때 문체와 형식을 맞춥니다.\n---\n# 보고서 문체\n\n1. 결론을 먼저 씁니다.\n";
const REPORT_MD_R2 = `${REPORT_MD_R1}2. 근거는 표로 정리합니다.\n`;
const TONE_MD = "# 말투\n\n존댓말을 씁니다.\n";
const COLLECT_PY = "print('collect')\n";

/** 소유 테넌트의 Skill 하나와 리비전 둘. r2 는 SKILL.md 를 고치고 스크립트를 더했다. */
export function reportRevisions(): SkillRevision[] {
  return [
    skillRevisionFixture("sk-report", 1, [file(SKILL_MD, REPORT_MD_R1), file("references/tone.md", TONE_MD)], { note: "처음" }),
    skillRevisionFixture("sk-report", 2, [file(SKILL_MD, REPORT_MD_R2), file("references/tone.md", TONE_MD), file("scripts/collect.py", COLLECT_PY)], {
      note: "근거 표",
      frontmatter: {
        name: "report-style",
        description: "보고서를 쓸 때 문체와 형식을 맞춥니다.",
        license: "Apache-2.0",
        compatibility: null,
        metadata: { author: "daegam" },
        allowed_tools: ["read_file", "web_search"],
      },
    }),
  ];
}

/** 다른 테넌트(acme)가 platform-admin 의 정책으로 연 Skill. 보고 붙이되 고치지 못한다. */
export function sharedRevisions(): SkillRevision[] {
  const md = "---\nname: web-research\ndescription: 웹에서 자료를 찾는 절차입니다.\n---\n# 조사\n";
  return [skillRevisionFixture("sk-shared", 1, [file(SKILL_MD, md)], { tenant: "acme", frontmatter: { name: "web-research", description: "웹에서 자료를 찾는 절차입니다.", license: null, compatibility: null, metadata: {}, allowed_tools: [] } })];
}

export const skillState: { items: Skill[]; revisions: Record<string, SkillRevision[]> } = { items: [], revisions: {} };

export function resetSkillState(): void {
  const report = reportRevisions();
  const shared = sharedRevisions();
  skillState.revisions = { "sk-report": report, "sk-shared": shared };
  skillState.items = [
    skillFixture(report[1], { display_name: "보고서 문체" }),
    skillFixture(shared[0], {
      tenant: "acme",
      scope: "shared",
      access: { view: { tenants: ["*"], roles: [], groups: [] }, consume: { tenants: ["*"], roles: [], groups: [] }, manage: { tenants: [], roles: [], groups: [] } },
      permissions: { view: true, consume: true, manage: false },
    }),
  ];
}
resetSkillState();

const skillStore = docStore(skillState, "skill");

function validate(files: SkillFileInput[], name?: string): SkillIssue[] {
  const md = files.find((f) => f.path === SKILL_MD);
  const issues: SkillIssue[] = [];
  if (!md) issues.push({ path: null, code: "skill_md_missing", message: "put SKILL.md at the root of the skill", severity: "error" });
  const fmName = md ? frontmatterName(md.content) : "";
  if (md && !K8S_NAME_RE.test(fmName)) issues.push({ path: SKILL_MD, code: "name_invalid", message: "name rule", severity: "error" });
  if (md && name && fmName !== name) issues.push({ path: SKILL_MD, code: "name_mismatch", message: `name must stay '${name}'`, severity: "error" });
  for (const f of files) if (isScript(f.path)) issues.push({ path: f.path, code: "skill_scripts_inert", message: "scripts do not run; the agent reads them as text", severity: "warning" });
  return issues;
}

/** 검사를 지난 파일로 리비전을 만든다. 오류가 있으면 422 invalid_skill 이다. */
function revisionFrom(skillId: string, n: number, files: SkillFileInput[], name?: string, note?: string | null): SkillRevision | Response {
  const errors = validate(files, name).filter((i) => i.severity === "error");
  if (errors.length) return apiError(422, "invalid_skill", "the skill files are not valid; see issues", { issues: errors });
  const md = files.find((f) => f.path === SKILL_MD)!;
  const fmName = frontmatterName(md.content);
  return skillRevisionFixture(skillId, n, files.map((f) => file(f.path, f.content)), {
    note: note ?? null,
    frontmatter: { name: fmName, description: "새로 쓴 Skill 입니다.", license: null, compatibility: null, metadata: {}, allowed_tools: [] },
  });
}

function sameContent(a: readonly SkillFileInput[], b: readonly SkillFileInput[]): boolean {
  const key = (files: readonly SkillFileInput[]) => files.map((f) => `${f.path}\0${f.content}`).sort().join("\n");
  return key(a) === key(b);
}

function created(rev: SkillRevision, displayName?: string | null): Skill {
  const skill = skillFixture(rev, { display_name: displayName || rev.frontmatter.name });
  skillState.revisions[skill.id] = [rev];
  skillState.items = [...skillState.items, skill];
  return skill;
}

function advanced(skill: Skill, rev: SkillRevision): Skill {
  skillState.revisions[skill.id] = [...(skillState.revisions[skill.id] ?? []), rev];
  return skillStore.replace({ ...skill, revision: rev.revision, latest: latestOf(rev) });
}

/** 가져오기 목은 올린 파일 글을 SKILL.md 하나로 본다. zip 이면 이름이 .zip 이고 건너뛴 항목을 하나 적는다. */
function uploaded(body: Record<string, unknown>): { files: SkillFileInput[]; skipped: string[] } {
  const part = body.file as { name: string; text: string };
  const zip = part.name.endsWith(".zip");
  const content = zip ? "---\nname: imported\ndescription: 가져온 Skill 입니다.\n---\n# 가져옴\n" : part.text;
  return { files: [{ path: SKILL_MD, content }], skipped: zip ? ["__MACOSX/._SKILL.md"] : [] };
}

export const skillHandlers: HttpHandler[] = [
  http.get("/skills", async ({ request }) => {
    await record(request);
    return HttpResponse.json(skillState.items);
  }),
  http.post("/skills", async ({ request }) => {
    const r = await record(request);
    const body = r.body as { files: SkillFileInput[]; display_name?: string; note?: string };
    const rev = revisionFrom("sk-new", 1, body.files, undefined, body.note);
    if (rev instanceof Response) return rev;
    return HttpResponse.json(created(rev, body.display_name), { status: 201 });
  }),
  http.post("/skills/import", async ({ request }) => {
    const r = await record(request);
    const body = r.body as Record<string, unknown>;
    const up = uploaded(body);
    const rev = revisionFrom("sk-imported", 1, up.files);
    if (rev instanceof Response) return rev;
    return HttpResponse.json({ ...created(rev, body.display_name as string | undefined), skipped: up.skipped }, { status: 201 });
  }),
  http.post("/skills/validate", async ({ request }) => {
    const r = await record(request);
    const body = r.body as { files: SkillFileInput[]; name?: string };
    const issues = validate(body.files, body.name);
    const ok = !issues.some((i) => i.severity === "error");
    return HttpResponse.json({ frontmatter: null, issues, digest: ok ? "sha256:0" : null, bytes: body.files.reduce((n, f) => n + f.content.length, 0) });
  }),
  http.get("/skills/:id", async ({ request, params }) => {
    await record(request);
    const skill = skillStore.findOr404(params.id);
    return skill instanceof Response ? skill : HttpResponse.json(skill);
  }),
  http.delete("/skills/:id", async ({ request, params }) => {
    await record(request);
    const skill = skillStore.findOr404(params.id);
    if (skill instanceof Response) return skill;
    if (!skill.permissions.manage) return apiError(403, "forbidden", "manage permission on this skill required", { action: "manage" });
    skillStore.remove(skill.id);
    return HttpResponse.json({ id: skill.id, deleted: true });
  }),
  http.patch("/skills/:id", async ({ request, params }) => {
    const r = await record(request);
    const skill = skillStore.findOr404(params.id);
    if (skill instanceof Response) return skill;
    const body = r.body as { display_name: string };
    return HttpResponse.json(skillStore.replace({ ...skill, display_name: body.display_name.trim() || skill.name }));
  }),
  // 정책 저장. manage 에 소유 테넌트 밖의 테넌트를 넣으면 422 다
  http.put("/skills/:id/access", async ({ request, params }) => {
    const r = await record(request);
    const skill = skillStore.findOr404(params.id);
    if (skill instanceof Response) return skill;
    const access = normalizeAccess(r.body as HubAccessInput);
    if (access.manage.tenants.some((t) => t !== skill.tenant)) return apiError(422, "cross_tenant_manage_unsupported", "manage stays in the owner tenant");
    return HttpResponse.json(skillStore.replace({ ...skill, access, scope: deriveScope(access, skill.tenant), access_updated_by: "header-admin", access_updated_at: NOW }));
  }),
  http.post("/skills/:id/clone", async ({ request, params }) => {
    const r = await record(request);
    const source = skillStore.findOr404(params.id);
    if (source instanceof Response) return source;
    const body = r.body as { name: string; display_name?: string };
    const last = skillState.revisions[source.id].at(-1)!;
    const rev = skillRevisionFixture("sk-clone", 1, last.files, {
      frontmatter: { ...last.frontmatter, name: body.name },
      cloned_from: { tenant: source.tenant, name: source.name, revision: last.revision },
    });
    return HttpResponse.json(created(rev, body.display_name), { status: 201 });
  }),
  http.get("/skills/:id/revisions", async ({ request, params }) => {
    await record(request);
    const revs = skillState.revisions[String(params.id)] ?? [];
    return HttpResponse.json([...revs].reverse().map((rev) => ({ ...rev, files: rev.files.map(({ path, bytes, sha256, script }) => ({ path, bytes, sha256, script })) })));
  }),
  http.get("/skills/:id/revisions/:n", async ({ request, params }) => {
    await record(request);
    const rev = (skillState.revisions[String(params.id)] ?? []).find((x) => x.revision === Number(params.n));
    return rev ? HttpResponse.json(rev) : apiError(404, "not_found", "no such revision");
  }),
  http.get("/skills/:id/revisions/:n/archive", async ({ request, params }) => {
    await record(request);
    const skill = skillStore.findOr404(params.id);
    if (skill instanceof Response) return skill;
    return new HttpResponse("PK", { headers: downloadHeaders(`${skill.name}-r${String(params.n)}.zip`) });
  }),
  http.post("/skills/:id/revisions", async ({ request, params }) => {
    const r = await record(request);
    const skill = skillStore.findOr404(params.id);
    if (skill instanceof Response) return skill;
    const body = r.body as { files: SkillFileInput[]; note?: string; expect_revision: number };
    if (body.expect_revision !== skill.revision) return apiError(409, "revision_conflict", "another revision was saved first; reload and try again", { revision: skill.revision });
    const last = skillState.revisions[skill.id]?.at(-1);
    if (last && sameContent(last.files, body.files)) return apiError(422, "skill_unchanged", "the files are the same as the latest revision");
    const rev = revisionFrom(skill.id, skill.revision + 1, body.files, skill.name, body.note);
    if (rev instanceof Response) return rev;
    return HttpResponse.json(advanced(skill, rev), { status: 201 });
  }),
  http.post("/skills/:id/revisions/import", async ({ request, params }) => {
    const r = await record(request);
    const skill = skillStore.findOr404(params.id);
    if (skill instanceof Response) return skill;
    const body = r.body as Record<string, unknown>;
    if (Number(body.expect_revision) !== skill.revision) return apiError(409, "revision_conflict", "another revision was saved first", { revision: skill.revision });
    const up = uploaded(body);
    const rev = revisionFrom(skill.id, skill.revision + 1, up.files, skill.name);
    if (rev instanceof Response) return rev;
    return HttpResponse.json({ ...advanced(skill, rev), skipped: up.skipped }, { status: 201 });
  }),
];
