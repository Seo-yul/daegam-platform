/**
 * Skill API 호출 (D64, P39 3절). 화면은 이 함수들만 쓰고 경로 문자열을 직접 쓰지 않는다.
 *
 * 가져오기 둘은 multipart 라 생성 클라이언트로 부르지 못하고 `postFile` 로 부른다. 내보내기는 헤더 인증이 필요한 내려받기라
 * `lib/download.ts` 가 받는다. 참조(`/skills/{id}/references`)는 다른 리소스와 같이 `resources.listReferences` 가 부른다.
 */
import { downloadFile } from "@/lib/download";

import { api, postFile, unwrap } from "./client";
import type {
  HubAccessInput,
  Skill,
  SkillCloneBody,
  SkillFileInput,
  SkillImported,
  SkillRevision,
  SkillRevisionMeta,
  SkillValidation,
} from "./types";

function skillPath(id: string, rest = ""): string {
  return `/skills/${encodeURIComponent(id)}${rest}`;
}

export async function listSkills(): Promise<Skill[]> {
  return unwrap(await api.GET("/skills"));
}

export async function getSkill(id: string): Promise<Skill> {
  return unwrap(await api.GET("/skills/{skill_id}", { params: { path: { skill_id: id } } }));
}

/** 머리말 `name` 이 리소스 이름이고 r1 을 만든다. 형식이 틀리면 422 `invalid_skill` 의 `issues` 에 모두 실린다. */
export async function createSkill(body: { files: SkillFileInput[]; display_name?: string; note?: string }): Promise<Skill> {
  return unwrap(await api.POST("/skills", { body }));
}

/** zip 이나 SKILL.md 하나로 만든다. */
export async function importSkill(file: File, displayName?: string): Promise<SkillImported> {
  return postFile<SkillImported>("/skills/import", file, displayName ? { display_name: displayName } : {});
}

/** 저장 없이 검사한다. name 을 주면 그 이름의 Skill 에 넣을 새 리비전으로 본다. */
export async function validateSkill(body: { files: SkillFileInput[]; name?: string }): Promise<SkillValidation> {
  return unwrap(await api.POST("/skills/validate", { body }));
}

/** 표시 이름만 바꾼다. 리비전은 늘지 않는다. manage 가 있어야 한다. */
export async function updateSkill(id: string, body: { display_name: string }): Promise<Skill> {
  return unwrap(await api.PATCH("/skills/{skill_id}", { params: { path: { skill_id: id } }, body }));
}

export async function deleteSkill(id: string): Promise<void> {
  unwrap(await api.DELETE("/skills/{skill_id}", { params: { path: { skill_id: id } } }));
}

/** 정책 저장. 경계를 넓히면 skill:share 이고 manage 에 다른 테넌트를 넣으면 422 `cross_tenant_manage_unsupported` 다. */
export async function setSkillAccess(id: string, access: HubAccessInput): Promise<Skill> {
  return unwrap(await api.PUT("/skills/{skill_id}/access", { params: { path: { skill_id: id } }, body: access }));
}

/** 요청 테넌트에 새 Skill 로 베낀다. consume 이 있어야 한다. */
export async function cloneSkill(id: string, body: SkillCloneBody): Promise<Skill> {
  return unwrap(await api.POST("/skills/{skill_id}/clone", { params: { path: { skill_id: id } }, body }));
}

/** 새것부터. 파일 본문은 없다. */
export async function listSkillRevisions(id: string): Promise<SkillRevisionMeta[]> {
  return unwrap(await api.GET("/skills/{skill_id}/revisions", { params: { path: { skill_id: id } } }));
}

export async function getSkillRevision(id: string, n: number): Promise<SkillRevision> {
  return unwrap(await api.GET("/skills/{skill_id}/revisions/{n}", { params: { path: { skill_id: id, n } } }));
}

/** 새 리비전. 그사이 다른 리비전이 들어왔으면 409 `revision_conflict`, 내용이 같으면 422 `skill_unchanged` 다. */
export async function addSkillRevision(id: string, body: { files: SkillFileInput[]; note?: string; expect_revision: number }): Promise<Skill> {
  return unwrap(await api.POST("/skills/{skill_id}/revisions", { params: { path: { skill_id: id } }, body }));
}

export async function importSkillRevision(id: string, file: File, expectRevision: number): Promise<SkillImported> {
  return postFile<SkillImported>(skillPath(id, "/revisions/import"), file, { expect_revision: String(expectRevision) });
}

/** 리비전을 zip 으로 내보낸다. 응답 헤더에 이름이 없으면 `<name>-r<n>.zip` 이다. */
export async function exportSkillRevision(skill: { id: string; name: string }, n: number): Promise<void> {
  await downloadFile(skillPath(skill.id, `/revisions/${n}/archive`), `${skill.name}-r${n}.zip`);
}
