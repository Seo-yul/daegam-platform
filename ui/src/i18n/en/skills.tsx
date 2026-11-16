import type { ReactNode } from "react";

import { SKILL_LIMITS } from "@/lib/limits";
import { NAME_MAX } from "@/lib/names";

import type { Messages } from "../types";
import { plural } from "./plural";

const fileCount = (count: number): string => plural(count, "file", "files");

/** Skill 목록·상세·만들기와 검사 결과, 새 SKILL.md 틀. 정책 문장 가운데 들어가는 owner 는 소문자 명사다. */
export const skills: Messages["skills"] = {
  create: "Create Skill",
  back: "Back to Skills",
  shared: "Shared",
  exportRevision: (revision: number): string => `Export r${revision}`,
  newerRevision: (latest: number): string => `Newer revision r${latest}`,
  access: {
    owner: "owning tenant",
    actions: {
      consume: { title: "consume", hint: "Can be attached to Agents and cloned" },
      manage: { title: "manage", hint: "Add revisions, change the policy and delete" },
    },
  },
  template: {
    description: "Describe in one or two sentences when to use this Skill.",
    heading: "Steps",
    step: "List the steps for the agent to follow, in order.",
  },
  issues: {
    too_many_files: `There are too many files. A Skill can hold up to ${SKILL_LIMITS.files} files, including SKILL.md.`,
    file_too_large: `This file is too large. SKILL.md can be up to ${SKILL_LIMITS.skillMdKib} KiB, and other files up to ${SKILL_LIMITS.fileKib} KiB, so shorten it or split it into several files.`,
    revision_too_large: `The files add up to more than ${SKILL_LIMITS.revisionKib} KiB. Make the files smaller or split them.`,
    path_invalid: "The path does not follow the rules. Use a relative path that starts with a letter or digit, such as references/tone.md.",
    path_duplicate: "This path already exists. Paths that differ only in case count as the same path, so use a different path.",
    skill_md_missing: "There is no SKILL.md in the top folder. Add a SKILL.md file.",
    not_text: "This file is not UTF-8 text. Save it as UTF-8 text and add it again.",
    control_chars: "The file contains control characters. Delete every control character except tabs and line breaks.",
    invisible_chars: "The file contains characters that do not show on screen. Delete the characters at the location below.",
    frontmatter_missing: "SKILL.md does not start with frontmatter. At the top, write name and description between two --- lines.",
    frontmatter_invalid: "The frontmatter cannot be read. Write it as YAML in key: value form.",
    name_invalid: `The name in the frontmatter does not follow the naming rules. Use lowercase letters, digits and hyphens, up to ${NAME_MAX} characters, and put a name made only of digits, such as 2024, in quotes.`,
    name_mismatch: "The name in the frontmatter cannot change. Set it back to this Skill's name.",
    description_missing: "The frontmatter has no description. Describe in one or two sentences when to use this Skill.",
    description_too_long: `The description can be up to ${SKILL_LIMITS.description} characters. Shorten it.`,
    compatibility_too_long: `The compatibility field can be up to ${SKILL_LIMITS.compatibility} characters. Shorten it.`,
    license_too_long: `The license field can be up to ${SKILL_LIMITS.license} characters. Shorten it.`,
    metadata_invalid: `The metadata field must map string keys to string values. Put numbers in quotes, and use up to ${SKILL_LIMITS.metadataKeys} keys.`,
    allowed_tools_invalid: `The allowed-tools field must list Tool names separated by spaces or commas. List up to ${SKILL_LIMITS.allowedTools} Tools.`,
    unknown_frontmatter: "The frontmatter has unsupported keys. The Skill still saves, but the Agent ignores them.",
    skill_scripts_inert: "Scripts do not run. The Agent reads script files only as text.",
    archive_invalid: "The zip file cannot be opened. Check that the zip is not damaged.",
    archive_too_large: `The zip is too large. Reduce it to at most ${SKILL_LIMITS.zipEntries} entries and ${SKILL_LIMITS.revisionKib} KiB uncompressed.`,
    archive_encrypted: "A password-protected zip cannot be imported. Remove the password and import the zip again.",
    archive_symlink: "The zip contains symbolic links. Remove the links and create the zip again.",
    archive_path_invalid: "The zip contains invalid paths. Create the zip again without absolute paths, parent folders (..) or backslashes.",
  },
  severity: { error: "Error", warning: "Warning" },
  list: {
    empty: (tenant: string): string => `Tenant ${tenant} has no Skills. Write the steps for the Agent to follow in SKILL.md to create one, or import a zip.`,
    owner: (tenant: ReactNode): ReactNode => <>Owned by {tenant}</>,
    columns: { name: "Name", description: "Description", revision: "Revision", owner: "Owning tenant" },
    title: "Skills",
    search: "Search skills…",
  },
  detail: {
    tabs: { files: "Files", revisions: "Revisions" },
    ownerBadge: (tenant: string): string => `Tenant ${tenant}`,
    newRevision: "New revision",
    skipped: (items: string): string => `These entries were not imported: ${items}`,
    referencesHint: "Agents with this Skill attached and the revision each one pins",
    cloneBody: (revision: number): string =>
      `Copies the latest revision r${revision} into a new Skill in this tenant. The revision history and access policy are not copied, and the new Skill starts at r1.`,
    clonePlaceholders: { display: "Report style copy", name: "report-style-copy" },
  },
  summary: {
    allowedToolsNote: "Experimental, and grants no permission to use Tools",
    size: (bytes: string, count: number): string => `${bytes} · ${fileCount(count)}`,
    tenant: "tenant",
    scope: "scope",
    license: "license",
    compatibility: "compatibility",
    allowedTools: "allowed-tools",
    labels: {
      size: "size",
    },
  },
  files: {
    count: (revision: number, count: number): string => `r${revision} · ${fileCount(count)}`,
    listLabel: "Skill files",
    script: "Script",
    scriptBadge: "Does not run",
    title: "Files",
  },
  editor: {
    addPath: "Path of the file to add",
    add: "Add file",
    duplicate: "A file with this path already exists.",
  },
  importField: {
    hint: "Drag a zip or SKILL.md here, or select one. If every entry in the zip sits in one folder, the files in that folder are imported.",
    file: "File to import",
    wrongType: (name: string): string => `"${name}" cannot be imported. Select a zip file or SKILL.md.`,
    sizeLimit: (size: string): string => `${size} per import`,
  },
  revisions: {
    latest: (revision: number): string => `Latest r${revision}`,
    columns: { revision: "Revision", note: "Note", author: "Created by", time: "Created" },
    clonedFrom: (tenant: string, name: string, revision: number): string => `Cloned from ${tenant}/${name} r${revision}`,
    compare: "Compare revisions",
    changedFiles: "Changed files",
    same: "The two revisions have the same files.",
    title: "Revisions",
    digest: "digest",
  },
  form: {
    write: "Write",
    import: "Import",
    newRevisionTitle: (name: string): string => `New revision of ${name}`,
    createSubtitle: "Write the steps for the Agent to follow in SKILL.md and any text files that go with it.",
    revisionSubtitle: (revision: number): string => `Edits r${revision} to make r${revision + 1}. Earlier revisions stay as they are.`,
    placeholders: { display: "Report style", name: "report-style" },
    nameHint: "The same value as name in the SKILL.md frontmatter.",
    note: "Revision note",
    noteHint: "Shown in the list on the Revisions tab. Optional.",
    saveRevision: "Save new revision",
    importDisplayHint: "Leave empty to use the name in the SKILL.md frontmatter.",
    importing: "Importing…",
    validation: {
      idle: "The server checks the format once you start editing.",
      checking: "Checking…",
      failed: "The check request failed. The server checks again when you save.",
      errors: (errors: number, warnings: number): string =>
        `Found ${plural(errors, "error", "errors")} and ${plural(warnings, "warning", "warnings")}. Fix the errors before saving.`,
      warnings: (warnings: number): string => `Found ${plural(warnings, "warning", "warnings")}, but you can still save.`,
      clean: "No format problems.",
    },
    displayName: "Display name",
  },
};
