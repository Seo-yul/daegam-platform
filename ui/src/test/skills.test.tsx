/**
 * Skills 화면 (D64, P39 4절). 목록, 만들기(직접 작성·가져오기와 서버 검사), 상세 네 탭, 새 리비전, 409 문구, 접근 탭을 본다.
 *
 * 형식 검사는 서버 한 곳이라 목(`msw/skills.ts`)이 그 결과를 흉내 내고 화면은 받은 issue 를 자리에 맞게 그리는지만 본다.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp, makeQueryClient } from "@/App";
import { NAV_ITEMS } from "@/components/Layout";
import { dictionary } from "@/i18n";
import { skillKey } from "@/lib/queryKeys";
import { SKILL_VALIDATE_DELAY_MS, frontmatterName, skillTemplate, withFrontmatterName } from "@/lib/skills";
import { DEFAULT_TENANT } from "@/lib/tenant";
import { submitConfirm } from "./confirm";
import { stubDownloads } from "./downloads";
import { codeEditor } from "./editor";
import { HttpResponse, apiError, http, lastExact, meDeveloper, reference, referencesState, server, skillRevisionFixture, skillState } from "./msw";
import { selectValue } from "./select";
import { advanceClock, tickingClock } from "./timers";

describe("skills", () => {
  it("사이드바의 Agents 아래에 있고, 목록 카드가 설명·리비전·공유 배지·소유 테넌트를 보인다", async () => {
    const ids = NAV_ITEMS.map((i) => i.testId);
    expect(ids.indexOf("nav-skills")).toBe(ids.indexOf("nav-agents") + 1);

    render(<TestApp initialPath="/skills" />);
    const own = await screen.findByTestId("skill-row-report-style");
    expect(own).toHaveTextContent("보고서 문체");
    expect(within(own).getByTestId("skill-revision-report-style")).toHaveTextContent("r2");
    expect(within(own).getByTestId("skill-description-report-style")).toHaveTextContent("문체와 형식");
    expect(within(own).queryByTestId("skill-owner-report-style")).not.toBeInTheDocument();

    const shared = screen.getByTestId("skill-row-web-research");
    expect(within(shared).getByTestId("skill-shared-web-research")).toHaveTextContent("공유");
    expect(within(shared).getByTestId("skill-owner-web-research")).toHaveTextContent("acme");
    // 다른 테넌트의 Skill 은 manage 가 없어 삭제가 없다
    expect(within(shared).queryByTestId("card-menu-web-research")).not.toBeInTheDocument();
  });

  it("직접 작성: 이름 칸이 SKILL.md 머리말 name 을 고치고, 서버 검사가 파일 옆에 서고, 만들면 상세로 간다", async () => {
    // 검사는 입력이 멈춘 뒤에 돈다. 그 간격을 실제로 기다리지 않도록 가짜 시계로 건너뛴다
    tickingClock();
    const user = userEvent.setup();
    render(<TestApp initialPath="/skills/new" />);
    const editor = await codeEditor("skill-files-editor");
    // 고치기 전에는 검사하지 않는다. 쓰기도 전에 빨간 줄을 띄우지 않는다
    expect(screen.getByTestId("skill-validation")).toHaveTextContent("고치기 시작하면");

    await user.type(screen.getByTestId("skill-display-name"), "Report Style");
    expect(screen.getByTestId("skill-name")).toHaveValue("report-style");
    expect(editor.value).toContain("name: report-style\n");
    await advanceClock(SKILL_VALIDATE_DELAY_MS);
    await waitFor(() => expect(screen.getByTestId("skill-validation")).toHaveTextContent("형식에 문제가 없습니다"));
    expect(lastExact("POST", "/skills/validate")?.body).toMatchObject({ files: [{ path: "SKILL.md" }] });

    // 스크립트는 받되 경고다. 파일 목록 옆에 수가, 선택한 파일 아래에 문장이 선다
    await user.type(screen.getByTestId("skill-files-add-path"), "scripts/run.sh");
    await user.click(screen.getByTestId("skill-files-add"));
    expect(screen.getByTestId("skill-files-current")).toHaveTextContent("scripts/run.sh");
    await advanceClock(SKILL_VALIDATE_DELAY_MS);
    await waitFor(() => expect(screen.getByTestId("skill-files-warnings-scripts/run.sh")).toHaveTextContent("1"));
    expect(screen.getByTestId("skill-files-issues")).toHaveTextContent("스크립트는 실행되지 않습니다");
    // 같은 경로는 다시 더하지 못한다
    await user.type(screen.getByTestId("skill-files-add-path"), "scripts/run.sh");
    expect(screen.getByTestId("skill-files-add")).toBeDisabled();
    await user.clear(screen.getByTestId("skill-files-add-path"));

    await user.click(screen.getByTestId("skill-save"));
    expect(await screen.findByTestId("skill-detail-page")).toBeInTheDocument();
    const body = lastExact("POST", "/skills")?.body as { display_name: string; files: { path: string }[] };
    expect(body.display_name).toBe("Report Style");
    expect(body.files.map((f) => f.path)).toEqual(["SKILL.md", "scripts/run.sh"]);
  });

  it("머리말 name 이 규칙에 맞지 않으면 SKILL.md 옆에 오류가 서고, 저장의 422 는 문제를 파일 옆에 한 번만 보인다", async () => {
    tickingClock();
    const user = userEvent.setup();
    server.use(
      http.post("/skills", () =>
        apiError(422, "invalid_skill", "the skill files are not valid; see issues", {
          issues: [{ path: "SKILL.md", code: "invisible_chars", message: "invisible character U+202E at line 3", severity: "error" }],
        }),
      ),
    );
    render(<TestApp initialPath="/skills/new" />);
    await user.type(screen.getByTestId("skill-name"), "Bad_Name");
    await advanceClock(SKILL_VALIDATE_DELAY_MS);
    await waitFor(() => expect(screen.getByTestId("skill-files-errors-SKILL.md")).toHaveTextContent("1"));
    expect(screen.getByTestId("skill-files-issues-name_invalid")).toHaveTextContent("이름 규칙에 맞지 않습니다");
    expect(screen.getByTestId("skill-validation")).toHaveTextContent("오류 1개");

    await user.click(screen.getByTestId("skill-save"));
    expect(await screen.findByTestId("skill-save-error")).toHaveTextContent("형식에 맞지 않습니다");
    // 검사 결과와 같은 자리(파일 옆)에 서고 저장 오류 아래에 다시 늘어놓지 않는다
    expect(screen.queryByTestId("skill-save-error-issues")).not.toBeInTheDocument();
    const issue = screen.getByTestId("skill-files-issues-invisible_chars");
    expect(issue).toHaveTextContent("보이지 않는 글자");
    // 고칠 자리는 서버 message 가 말한다
    expect(issue).toHaveTextContent("U+202E at line 3");
    expect(screen.getByTestId("skill-validation")).toHaveTextContent("오류 1개");
    // 위치가 없는 code 의 영어 message 는 보이지 않는다
    expect(screen.queryByText("name rule")).not.toBeInTheDocument();
  });

  it("가져오기: zip 을 multipart 로 보내고 건너뛴 항목을 상세에서 알린다. 2 MiB 를 넘거나 형식이 다른 파일은 그 자리에서 되돌리고, 고른 파일은 저장하지 않은 입력으로 센다", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<TestApp initialPath="/skills/new?tab=import" />);
    const big = new File([new Uint8Array(3 * 1024 * 1024)], "big.zip", { type: "application/zip" });
    await user.upload(await screen.findByTestId("skill-import-file"), big);
    // 파일 크기와 상한을 같은 단위로 적는다
    expect(screen.getByTestId("skill-import-refused")).toHaveTextContent('"big.zip"는 3.0 MiB 입니다. 가져올 수 있는 파일은 2.0 MiB 까지라 올리지 않았습니다.');
    expect(screen.getByTestId("skill-import-submit")).toBeDisabled();
    // 끌어 놓은 파일은 선택 창의 accept 를 거치지 않으므로 같은 목록으로 거른다
    const pdf = new File(["%PDF"], "guide.pdf", { type: "application/pdf" });
    fireEvent.drop(screen.getByTestId("skill-import-drop"), { dataTransfer: { files: [pdf], types: ["Files"] } });
    expect(screen.getByTestId("skill-import-refused")).toHaveTextContent("가져올 수 없는 형식입니다");
    expect(screen.queryByTestId("skill-import-chosen")).not.toBeInTheDocument();

    await user.upload(screen.getByTestId("skill-import-file"), new File(["PK"], "skill.zip", { type: "application/zip" }));
    expect(screen.getByTestId("skill-import-chosen")).toHaveTextContent("skill.zip");
    // 가져오기 탭에서 고른 파일도 저장하지 않은 입력으로 센다. 되돌린 파일은 고른 것이 아니고 표시 이름은
    // 아직 비어 있으므로, 여기서 묻는 것은 고른 zip 때문이다
    await user.click(screen.getByTestId("nav-agents"));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("skill-form-page")).toBeInTheDocument();

    await user.type(screen.getByTestId("skill-import-display"), "가져온 것");
    await user.click(screen.getByTestId("skill-import-submit"));

    expect(await screen.findByTestId("skill-import-skipped")).toHaveTextContent("다음 항목은 가져오지 않았습니다. __MACOSX/._SKILL.md");
    expect(lastExact("POST", "/skills/import")?.body).toMatchObject({ file: { name: "skill.zip" }, display_name: "가져온 것" });
    // 알림은 한 번이다. 다른 탭으로 옮겨도 이 화면에 있는 동안은 남는다
    await user.click(screen.getByTestId("tab-files"));
    expect(screen.getByTestId("skill-import-skipped")).toBeInTheDocument();
  });

  it("개요: 머리말 요약과 allowed-tools 안내, 참조 카드는 Agent 마다 핀한 리비전과 새 리비전 배지를 보이고, 제목 줄의 내보내기는 최신 리비전을 zip 으로 다운로드한다", async () => {
    const user = userEvent.setup();
    const saved = stubDownloads();
    referencesState["skills/sk-report"] = [
      reference("agent", "ag-old", { display_name: "상담 에이전트", revision: "both", pins: { current: 1, deployed: 1 } }),
      reference("agent", "ag-new", { display_name: "보고 에이전트", revision: "current", pins: { current: 2, deployed: null } }),
    ];
    render(<TestApp initialPath="/skills/sk-report" />);
    expect(await screen.findByTestId("skill-allowed-tools")).toHaveTextContent("read_file");
    expect(screen.getByTestId("skill-allowed-tools-note")).toHaveTextContent("실험 기능이며 Tool 사용 권한을 주지 않습니다");
    expect(screen.getByTestId("skill-scope")).toHaveTextContent("소유 테넌트 안에서만 보임");
    expect(screen.getByTestId("skill-license")).toHaveTextContent("Apache-2.0");
    expect(screen.getByTestId("skill-metadata-author")).toHaveTextContent("daegam");

    expect(await screen.findByTestId("references-card-revision-ag-old")).toHaveTextContent("정의 r1, 배포 r1");
    expect(screen.getByTestId("references-card-behind-ag-old")).toHaveTextContent("새 리비전 r2");
    expect(screen.getByTestId("references-card-revision-ag-new")).toHaveTextContent("정의 r2");
    expect(screen.queryByTestId("references-card-behind-ag-new")).not.toBeInTheDocument();

    const button = await screen.findByTestId("skill-export");
    expect(button).toHaveAccessibleName("r2 내보내기");
    await user.click(button);
    await waitFor(() => expect(saved).toEqual(["report-style-r2.zip"]));
  });

  it("파일 탭: 트리와 마크다운 미리 보기, 스크립트에는 실행되지 않는다는 표시", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/skills/sk-report?tab=files" />);
    const preview = await screen.findByTestId("skill-files-preview");
    expect(preview).toHaveTextContent("결론을 먼저 씁니다");
    // 머리말은 개요가 보이므로 미리 보기에서 뗀다
    expect(preview).not.toHaveTextContent("description:");

    await user.click(screen.getByTestId("skill-files-file-scripts/collect.py"));
    expect(screen.getByTestId("skill-files-script")).toHaveTextContent("실행되지 않음");
    expect(await codeEditor("skill-files-viewer")).toHaveValue("print('collect')\n");
  });

  it("리비전 탭: 목록에 메모와 digest 앞 12자가 있고, 두 리비전 사이에 바뀐 파일만 차이로 보인다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/skills/sk-report?tab=revisions" />);
    const r2 = await screen.findByTestId("skill-revision-2");
    expect(r2).toHaveTextContent("근거 표");
    expect(screen.getByTestId("skill-revision-digest-2").textContent).toHaveLength(12);

    const files = await screen.findByTestId("skill-diff-files");
    expect(within(files).getByTestId("skill-diff-file-SKILL.md")).toHaveTextContent("변경");
    expect(within(files).getByTestId("skill-diff-file-scripts/collect.py")).toHaveTextContent("추가");
    expect(within(files).queryByTestId("skill-diff-file-references/tone.md")).not.toBeInTheDocument();
    const side = (testId: string) => (screen.getByTestId(testId) as HTMLTextAreaElement).value;
    await waitFor(() => expect(side("monaco-modified")).toContain("근거는 표로"));
    expect(side("monaco-original")).toContain("결론을 먼저");
    expect(side("monaco-original")).not.toContain("근거는 표로");

    // 같은 리비전끼리는 바뀐 것이 없다. 고르개는 MCP Server·Gateway 의 정의 탭과 같은 것이다
    expect(screen.getByTestId("diff-from")).toHaveValue("1");
    expect(screen.getByTestId("diff-to")).toHaveValue("2");
    await selectValue(user, "diff-from", "2");
    expect(await screen.findByTestId("skill-diff-same")).toHaveTextContent("두 리비전의 파일이 같습니다");
  });

  it("새 리비전: 최신 리비전을 채워 열고 expect_revision 을 보내며, 저장하면 상세가 다음 번호를 보인다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/skills/sk-report" />);
    await user.click(await screen.findByTestId("skill-new-revision"));
    const editor = await codeEditor("skill-files-editor");
    expect(editor.value).toContain("근거는 표로");
    // 이름은 생성 뒤 바뀌지 않으므로 읽기 전용이고, 표시 이름은 지금 값으로 채워 연다
    expect(screen.getByTestId("skill-name")).toHaveAttribute("readonly");
    expect(screen.getByTestId("skill-display-name")).toHaveValue("보고서 문체");

    await user.type(editor, "3. 출처를 적습니다.");
    await user.type(screen.getByTestId("skill-note"), "출처");
    await user.click(screen.getByTestId("skill-save"));
    expect(await screen.findByTestId("skill-revision")).toHaveTextContent("r3");
    expect(lastExact("POST", "/skills/sk-report/revisions")?.body).toMatchObject({ expect_revision: 2, note: "출처" });
  });

  /** 다른 사람이 r3 을 넣은 것처럼 목 상태를 올린다. */
  function someoneSavedR3(): void {
    const r3 = skillRevisionFixture("sk-report", 3, skillState.revisions["sk-report"][1].files, { note: "다른 사람" });
    skillState.revisions["sk-report"] = [...skillState.revisions["sk-report"], r3];
    skillState.items = skillState.items.map((s) => (s.id === "sk-report" ? { ...s, revision: 3 } : s));
  }

  it("편집하는 동안 상세 조회가 다시 와서 리비전 번호가 올라도 고친 글이 남고, 저장은 409 를 사람 말로 알리며 고친 글을 남긴다", async () => {
    const user = userEvent.setup();
    const client = makeQueryClient();
    render(<TestApp initialPath="/skills/sk-report/edit" client={client} />);
    const editor = await codeEditor("skill-files-editor");
    await user.type(editor, "3. 고친 줄");
    await user.type(screen.getByTestId("skill-display-name"), " v2");

    someoneSavedR3();
    // 다시 연결되거나 다른 화면이 같은 키를 무효로 만들면 상세 조회가 다시 온다
    await client.refetchQueries({ queryKey: skillKey(DEFAULT_TENANT, "sk-report"), exact: true });
    expect(client.getQueryData<{ revision: number }>(skillKey(DEFAULT_TENANT, "sk-report"))?.revision).toBe(3);

    expect((await codeEditor("skill-files-editor")).value).toContain("3. 고친 줄");
    expect(screen.getByTestId("skill-display-name")).toHaveValue("보고서 문체 v2");
    // 폼은 처음 본 r2 를 고친다고 말한다
    expect(screen.getByTestId("skill-form-page")).toHaveTextContent("r2를 고쳐 r3을 만듭니다");

    // 그사이 다른 사람이 리비전을 넣었으므로 저장은 처음 본 r2 를 expect_revision 으로 보내 409 를 받는다
    await user.type(await codeEditor("skill-files-editor"), "추가");
    await user.click(screen.getByTestId("skill-save"));
    expect(await screen.findByTestId("skill-save-error")).toHaveTextContent("그 사이에 이 항목이 바뀌었습니다");
    expect(lastExact("POST", "/skills/sk-report/revisions")?.body).toMatchObject({ expect_revision: 2 });
    expect((await codeEditor("skill-files-editor")).value).toContain("추가");
  });

  it("새 리비전 폼에서 표시 이름만 바꾸면 리비전을 만들지 않고 PATCH 만 보낸다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/skills/sk-report/edit" />);
    await codeEditor("skill-files-editor");
    // 이름은 생성 뒤 바뀌지 않으므로 읽기 전용이다
    expect(screen.getByTestId("skill-name")).toHaveAttribute("readonly");
    expect(screen.getByTestId("skill-name")).toHaveValue("report-style");
    await user.clear(screen.getByTestId("skill-display-name"));
    await user.type(screen.getByTestId("skill-display-name"), "보고서 규칙");
    await user.click(screen.getByTestId("skill-save"));

    expect(await screen.findByTestId("skill-detail-page")).toBeInTheDocument();
    expect(await screen.findByTestId("page-title")).toHaveTextContent("보고서 규칙");
    expect(screen.getByTestId("skill-revision")).toHaveTextContent("r2");
    expect(lastExact("PATCH", "/skills/sk-report")?.body).toEqual({ display_name: "보고서 규칙" });
    expect(lastExact("POST", "/skills/sk-report/revisions")).toBeUndefined();
  });

  it("처음 연 그대로면 저장은 서버의 422 skill_unchanged 를 사람 말로 보이고 나가도 묻지 않으며, 저장하지 않은 입력을 두고 링크나 취소로 나가면 확인을 지난다", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<TestApp initialPath="/skills/sk-report/edit" />);
    await codeEditor("skill-files-editor");
    // 고친 것이 없으면 서버의 422 skill_unchanged 를 사람 말로 보인다
    await user.click(screen.getByTestId("skill-save"));
    expect(await screen.findByTestId("skill-save-error")).toHaveTextContent("마지막 리비전과 내용이 같아 새 리비전을 만들지 않았습니다");
    // 최신 리비전을 채운 그대로는 고친 것이 아니다. 저장이 거절된 뒤에도 그렇다
    await user.click(screen.getByRole("button", { name: "취소" }));
    expect(await screen.findByTestId("skill-detail-page")).toBeInTheDocument();
    expect(confirm).not.toHaveBeenCalled();

    await user.click(screen.getByTestId("skill-new-revision"));
    await codeEditor("skill-files-editor");
    await user.type(screen.getByTestId("skill-note"), "출처");
    await user.click(screen.getByTestId("nav-agents"));
    await user.click(screen.getByRole("button", { name: "취소" }));
    expect(confirm).toHaveBeenCalledTimes(2);
    // 아니라고 하면 그 자리에 남는다
    expect(screen.getByTestId("skill-form-page")).toBeInTheDocument();

    confirm.mockReturnValue(true);
    await user.click(screen.getByRole("button", { name: "취소" }));
    expect(await screen.findByTestId("skill-detail-page")).toBeInTheDocument();
  });

  it("manage 가 없는 Skill 의 새 리비전 주소는 상세로 돌려보낸다", async () => {
    render(<TestApp initialPath="/skills/sk-shared/edit" />);
    expect(await screen.findByTestId("skill-detail-page")).toBeInTheDocument();
    expect(screen.queryByTestId("skill-form-page")).not.toBeInTheDocument();
  });

  it("파일을 지우면 초점이 body 로 떨어지지 않고 그 뒤에 고른 파일의 줄로 간다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/skills/sk-report/edit" />);
    await codeEditor("skill-files-editor");
    await user.click(screen.getByTestId("skill-files-file-references/tone.md"));
    await user.click(screen.getByTestId("skill-files-remove-references/tone.md"));
    expect(screen.queryByTestId("skill-files-file-references/tone.md")).not.toBeInTheDocument();
    expect(screen.getByTestId("skill-files-file-SKILL.md")).toHaveFocus();
    expect(screen.getByTestId("skill-files-file-SKILL.md")).toHaveAttribute("aria-current", "true");
  });

  it("삭제가 409 in_use 면 이 테넌트의 Agent 이름과 다른 테넌트의 Agent 수를 함께 보인다", async () => {
    const user = userEvent.setup();
    referencesState["skills/sk-report"] = [reference("agent", "ag-1", { display_name: "상담 에이전트", pins: { current: 2, deployed: null } })];
    server.use(http.delete("/skills/:id", () => apiError(409, "in_use", "skill is pinned by agents", { agent_ids: ["ag-1"], deployed_agent_ids: [], other_tenant_agents: 2 })));
    render(<TestApp initialPath="/skills/sk-report" />);
    await user.click(await screen.findByTestId("delete-button"));
    await submitConfirm(user);
    const dialog = await screen.findByTestId("confirm-dialog");
    await waitFor(() => expect(dialog).toHaveTextContent("다른 리소스가 사용하고 있어"));
    expect(dialog).toHaveTextContent("Agent 상담 에이전트");
    expect(dialog).toHaveTextContent("다른 테넌트의 Agent 2개");
  });

  it("접근 탭: manage 줄에는 테넌트 칸이 없고, platform-admin 이 아니면 skill:share 가 필요하다고 말한다", async () => {
    const user = userEvent.setup();
    server.use(http.get("/me", () => HttpResponse.json(meDeveloper)));
    render(<TestApp initialPath="/skills/sk-report?tab=access" />);
    const form = await screen.findByTestId("access-form");
    await waitFor(() => expect(within(form).getByTestId("access-view-tenants")).toBeDisabled());
    expect(within(form).getByTestId("access-share-reason")).toHaveTextContent("skill:share 권한이 필요합니다");
    expect(within(form).queryByTestId("access-manage-tenants")).not.toBeInTheDocument();
    expect(within(form).getByTestId("access-manage-tenants-fixed")).toHaveTextContent("소유 테넌트 안에서만");
    expect(screen.getByTestId("skill-access-scope-hint")).toHaveTextContent("소유 테넌트 안에서만 보임");
    expect(within(form).getByTestId("access-preview-view")).toHaveTextContent("소유 테넌트");
    expect(within(form).getByTestId("access-consume")).toHaveTextContent("Agent 에 붙이고 복제할 수 있습니다");

    await selectValue(user, "access-consume-role", "tenant-admin");
    await user.click(within(form).getByTestId("access-save"));
    expect(await screen.findByTestId("access-saved")).toBeInTheDocument();
    expect(lastExact("PUT", "/skills/sk-report/access")?.body).toMatchObject({ consume: { roles: ["tenant-admin"] } });
  });

  it("다른 테넌트가 연 Skill 은 새 리비전·삭제가 없고 복제로 자기 테넌트에 베끼며, 복제본의 리비전 탭이 출처를 적는다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/skills/sk-shared" />);
    expect(await screen.findByTestId("skill-owner-badge")).toHaveTextContent("acme");
    expect(screen.queryByTestId("skill-new-revision")).not.toBeInTheDocument();
    expect(screen.queryByTestId("delete-button")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("clone-button"));

    const dialog = await screen.findByTestId("clone-dialog");
    expect(dialog).toHaveTextContent("r1을 복사하여 이 테넌트에 새 Skill 을 만듭니다");
    expect(within(dialog).getByTestId("clone-name")).toHaveValue("web-research-copy");
    await user.click(within(dialog).getByTestId("clone-submit"));
    await waitFor(() => expect(lastExact("POST", "/skills/sk-shared/clone")?.body).toEqual({ name: "web-research-copy", display_name: "web-research 복제본" }));
    // 복제본 상세로 넘어갈 때까지 기다린다. 상세 화면 testid 는 옮겨 가기 전의 원본 상세에도 있어, 그것을 집으면 곧바로
    // 화면이 바뀌어 실패하는 경쟁 상태가 있었다(바쁜 기계에서). 복제본의 제목으로 새 화면을 확인한다
    await waitFor(() => expect(screen.getByTestId("page-title")).toHaveTextContent("web-research 복제본"));
    await user.click(screen.getByTestId("tab-revisions"));
    expect(await screen.findByTestId("skill-revision-cloned-1")).toHaveTextContent("acme/web-research r1 에서 복제");
  });
});

describe("SKILL.md 머리말의 name 줄", () => {
  const md = (nameLine: string) => `---\n${nameLine}\ndescription: d\n---\n# 본문\n`;

  it("콜론 앞에 공백이 있어도 서버처럼 name 줄로 읽고, 고치면 그 줄을 바꾼다", () => {
    const text = md("name : old-name");
    expect(frontmatterName(text)).toBe("old-name");
    const next = withFrontmatterName(text, "new-name");
    expect(next).toBe(md("name: new-name"));
    expect(frontmatterName(next)).toBe("new-name");
  });

  it("YAML 이 글로 읽지 않는 이름은 따옴표로 감싸고 읽을 때 벗긴다", () => {
    for (const name of ["2024", "2024-01-01", "0x1f", "0b101", "yes", "off", "null"]) {
      const next = withFrontmatterName(md("name: x"), name);
      expect(next).toContain(`name: "${name}"\n`);
      expect(frontmatterName(next)).toBe(name);
    }
    expect(withFrontmatterName(md("name: x"), "report-style")).toContain("name: report-style\n");
    expect(skillTemplate(dictionary("ko"), "2024")).toContain('name: "2024"\n');
  });
});
