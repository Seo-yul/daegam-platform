import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Pencil, X } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { ApiError } from "@/api/client";
import { convertSpec, getVersionSource, listBackendApis, patchBackendApi, uploadSpec, verifySpec, type SpecInput } from "@/api/resources";
import type { BackendAPI, ConvertResponse, Verdict, VerifyReport, VerifyResponse } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { PageHeader } from "@/components/Layout";
import { NameFields, isNameFieldError, nameFieldErrors, type NameValue } from "@/components/NameFields";
import { NotFoundPage } from "@/components/NotFoundPage";
import { PageWidth } from "@/components/PageWidth";
import { queryGate } from "@/components/QueryGate";
import { SpecEditor } from "@/components/SpecEditor";
import { CheckResultsTable, ToolList, VerdictPanel } from "@/components/VerdictPanel";
import { Badge } from "@/components/ui/badge";
import { IconButton } from "@/components/ui/icon-button";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { useInlineEdit } from "@/components/ui/useInlineEdit";
import { useT } from "@/i18n";
import { AUTH_KINDS, displayName, validateDisplayName } from "@/lib/names";
import { prettyDoc } from "@/lib/prettyDoc";
import { useTenant } from "@/lib/tenant";
import { useDirty, useUnsavedGuard } from "@/lib/unsavedGuard";

/**
 * 422 오류 본문에 실려 온 검사 결과.
 *
 * 서버는 `verdict_error` 에 verdict 를 싣고, 저장이 검증에서 막히면 report 까지 함께 싣는다. 이것을
 * 버리면 화면에 남는 것은 한 문장뿐이라 무엇을 몇 번째 줄에서 고쳐야 하는지 알 길이 없다.
 * 상세 화면이 저장된 버전에 쓰는 것과 같은 표로 그린다.
 */
function ErrorVerdict({ error, testId }: { error: unknown; testId: string }) {
  if (!(error instanceof ApiError)) return null;
  const verdict = error.extra<Verdict>("verdict");
  const results = error.extra<VerifyReport>("report")?.results ?? [];
  if (!verdict && !results.length) return null;
  return (
    <div className="flex flex-col gap-3" data-testid={testId}>
      {verdict ? <VerdictPanel verdict={verdict} /> : null}
      {results.length ? <CheckResultsTable results={results} /> : null}
    </div>
  );
}

/** 붙여넣은 텍스트를 업로드 파일로. 확장자는 verify 가 감지한 형식을 따른다. */
/** 스펙 칸의 보기. 문서의 첫 줄이라 번역하지 않는다. */
const SPEC_EXAMPLE = "openapi: 3.0.0 ...";

function textToFile(text: string, filename: string | null, sourceFormat: string | null): File {
  let name = filename?.trim() || "";
  if (!name) {
    if (sourceFormat === "wsdl") name = "spec.wsdl";
    else {
      try {
        JSON.parse(text);
        name = "spec.json";
      } catch {
        name = "spec.yaml";
      }
    }
  }
  return new File([text], name, { type: "text/plain" });
}

/**
 * 제목 자리에서 표시 이름만 고친다. 편집 화면 전용이고, 끄면 세 조각이 모두 비어 있다.
 *
 * 문서와 함께 올리지 않고 PATCH 하나로 끝내는 것은, 이름을 고치려고 스펙을 다시 올릴 이유가 없어서다.
 * 버전은 문서가 바뀔 때만 생긴다. 제목·배지·아래 안내 세 자리에 흩어지는 조각을 여기서 한 번에 만든다.
 */
function useInlineRename({ enabled, current, save }: { enabled: boolean; current: string; save: (displayName: string) => Promise<unknown> }): {
  title?: ReactNode;
  badges?: ReactNode;
  feedback?: ReactNode;
} {
  const t = useT();
  const text = t.backendApis.new.rename;
  const [renaming, setRenaming] = useState(false);
  const edit = useInlineEdit("", { onEnter: () => void submit(), onEscape: cancel });
  // `validateDisplayName` 은 만들기 폼의 규칙이라 빈 값을 통과시킨다 ("비우면 Name 을 그대로 쓴다").
  // 여기서는 비우는 것이 곧 지우는 것이라 서버가 422 로 막는다. 누를 수 있는 채로 두지 않는다
  const error = renaming ? (validateDisplayName(t, edit.value) ?? (edit.value.trim() ? null : text.required)) : null;
  const canSave = renaming && !error && !edit.saving;
  async function submit() {
    if (!canSave) return;
    if (await edit.run(() => save(edit.value.trim()))) setRenaming(false);
  }
  function start() {
    edit.setValue(current);
    setRenaming(true);
  }
  function cancel() {
    // 닫을 때 지운다. 다음에 열 때까지 앞선 실패가 화면에 남아 있으면 방금 난 일로 읽힌다
    edit.setValue("");
    setRenaming(false);
  }

  if (!enabled) return {};
  if (!renaming) {
    return {
      badges: (
        <IconButton label={text.start} testId="rename-button" onClick={start}>
          <Pencil size={15} />
        </IconButton>
      ),
    };
  }
  return {
    title: (
      <input
        autoFocus
        aria-label={text.input}
        data-testid="rename-input"
        className="w-[min(24rem,100%)] min-w-0 rounded-item border border-line bg-surface px-2 py-0.5 font-[inherit] text-[length:inherit] text-heading outline-none focus-visible:ring-2 focus-visible:ring-focus-ring"
        value={edit.value}
        onChange={(e) => edit.setValue(e.target.value)}
        onKeyDown={edit.onKeyDown}
      />
    ),
    badges: (
      <span className="flex items-center gap-1">
        <IconButton label={text.save} testId="rename-save" disabled={!canSave} onClick={() => void submit()}>
          <Check size={15} />
        </IconButton>
        <IconButton label={text.cancel} testId="rename-cancel" onClick={cancel}>
          <X size={15} />
        </IconButton>
      </span>
    ),
    feedback: (
      <div className="mb-4 flex flex-col gap-1" data-testid="rename-feedback">
        {error ? <p className="text-xs text-err-fg">{error}</p> : null}
        <ErrorMessage error={edit.error} testId="rename-error" />
      </div>
    ),
  };
}

/**
 * 만들기와 문서 편집이 같은 화면이다.
 *
 * 서버는 같은 이름으로 올리면 새 문서를 만들지 않고 그 문서에 버전을 붙인다. 그래서 화면도 하나면
 * 되는데, 들어온 길에 따라 무엇을 묻는지가 다르다.
 *
 * **편집은 문서만 다룬다** (`/backend-apis/:id/edit`). Endpoint·Auth·Credential Secret 은 상세의
 * "접속 정보 저장" 이 이미 PATCH 로 고치므로 여기에 또 두면 같은 값을 고치는 자리가 둘이 된다. Name 은
 * 생성 뒤 바꿀 수 없어(D24) 보여 줄 것도 없다. Display name 만 제목 옆 연필로 그 자리에서 고치고, 그것도
 * 업로드가 아니라 PATCH 다. 그래서 편집 화면의 저장은 "새 버전 저장" 하나뿐이다.
 */
export function BackendApiNewPage() {
  const t = useT();
  const text = t.backendApis.new;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const tenant = useTenant();

  const { id: editingId } = useParams();
  const editing = Boolean(editingId);
  // 아래 넷은 만들기 전용이다. 편집에서는 그리지 않는다
  const [names, setNames] = useState<NameValue>({ displayName: "", name: "" });
  const [endpoint, setEndpoint] = useState("");
  const [auth, setAuth] = useState<(typeof AUTH_KINDS)[number]>("none");
  const [credentialSecret, setCredentialSecret] = useState("");
  const [soapSecret, setSoapSecret] = useState("");

  const [file, setFile] = useState<File | null>(null);
  const [specText, setSpecText] = useState("");
  const [filename, setFilename] = useState("");

  const [verifyResult, setVerifyResult] = useState<VerifyResponse | null>(null);
  const [generated, setGenerated] = useState<ConvertResponse | null>(null);
  const [yaml, setYaml] = useState("");

  // 업로드는 이름으로 기존 Backend API 를 먼저 찾고, 있으면 새로 만들지 않고 그것에 버전을 붙이면서
  // 접속 정보를 폼 값으로 덮는다 (platform/daegam/api/backend_apis.py 의 업서트). 화면은 "New" 라고만
  // 하므로 여기서 목록을 읽어 겹침을 저장 전에 말한다.
  const profiles = useQuery({ queryKey: ["backend-apis", tenant], queryFn: listBackendApis });
  const editTarget = editingId ? profiles.data?.find((p) => p.id === editingId) : undefined;
  // 편집에서 이름은 고칠 수 없는 값이라 폼이 아니라 대상에서 온다 (D24)
  const name = editing ? (editTarget?.name ?? "") : names.name.trim();
  const existing: BackendAPI | undefined = editTarget ?? (name ? profiles.data?.find((p) => p.name === name) : undefined);
  const nextVersion = (existing?.latest_version ?? 0) + 1;

  // 편집이면 지금 무엇이 등록돼 있는지부터 보여 준다. 변환된 OpenAPI(`/spec`) 가 아니라 올린 원본이라야
  // WSDL 로 올린 백엔드에서도 사람이 올린 그 문서가 나온다
  const sourceVersion = editTarget?.latest_version ?? null;
  const source = useQuery({
    queryKey: ["backend-api-source", editingId, sourceVersion],
    queryFn: () => getVersionSource(editingId as string, sourceVersion as number),
    enabled: Boolean(editingId) && sourceVersion !== null,
  });
  const sourceGate = queryGate(source, { label: text.sourceLoading, testId: "registered-spec-error", keepStale: false });

  // 올린 그대로를 보관하므로 한 줄짜리 문서가 그대로 저장돼 있을 수 있다. 읽으라고 띄우는 자리라
  // 들여쓴 판을 고를 수 있게 한다. 기본은 끔이다. "등록된 문서" 는 저장된 그대로가 먼저다
  const [pretty, setPretty] = useState(false);
  const prettified = useMemo(() => (source.data ? prettyDoc(source.data.text) : null), [source.data]);
  const canPretty = prettified !== null && prettified !== (source.data?.text ?? null);
  const prettySource = pretty && canPretty ? prettified : null;

  const rename = useInlineRename({
    enabled: editing,
    current: editTarget ? displayName(editTarget) : "",
    save: async (value) => {
      await patchBackendApi(editingId as string, { display_name: value });
      await queryClient.invalidateQueries({ queryKey: ["backend-apis"] });
    },
  });

  /**
   * 입력이 바뀌면 앞선 Verify·Generate 결과를 버린다.
   *
   * 저장은 편집기에 무언가 남아 있으면 그것을 올리므로, 스펙 A 로 Generate 한 뒤 B 를 넣고 저장하면
   * 화면에는 B 가 보이는데 등록되는 것은 A 였다.
   */
  function clearResults() {
    setVerifyResult(null);
    setGenerated(null);
    setYaml("");
  }

  function currentInput(): SpecInput | null {
    if (file) return { file };
    if (specText.trim()) return { text: specText, filename: filename || undefined };
    return null;
  }

  const verify = useMutation({
    mutationFn: (input: SpecInput) => verifySpec(input),
    onSuccess: (r) => setVerifyResult(r),
  });

  const generate = useMutation({
    mutationFn: (input: SpecInput) => convertSpec(input),
    onSuccess: (r) => {
      setGenerated(r);
      setYaml(r.yaml);
    },
  });

  // 상세의 접속 정보 카드와 같다: SOAP 스펙에서 이 칸을 보이고, 값이 든 칸은 스펙을 고쳐 검사 결과가 지워져도 둔다
  const showSoap = verifyResult?.kind === "soap" || soapSecret !== "";

  const save = useMutation({
    mutationFn: async () => {
      let upload: File;
      if (generated && yaml.trim()) {
        // Generate 를 거쳤으면 편집기의 OpenAPI yaml 이 원본이 된다
        upload = new File([yaml], `${name || "spec"}.openapi.yaml`, { type: "text/yaml" });
      } else if (file) {
        upload = file;
      } else {
        upload = textToFile(specText, filename, verifyResult?.source_format ?? null);
      }
      // 편집은 문서만 올린다. 접속 정보를 함께 보내면 상세의 "접속 정보 저장" 과 같은 값을 두 자리에서
      // 쓰게 되고, 여기서 안 보내면 서버가 저장된 값을 그대로 둔다 (api/backend_apis.upload_spec)
      if (editing) return uploadSpec({ file: upload, name });
      return uploadSpec({
        file: upload,
        name,
        display_name: names.displayName.trim() || undefined,
        endpoint: endpoint || undefined,
        auth,
        credential_secret: credentialSecret || undefined,
        soap_headers_secret: soapSecret.trim() || undefined,
      });
    },
    onSuccess: async (r) => {
      await queryClient.invalidateQueries({ queryKey: ["backend-apis"] });
      navigate(`/backend-apis/${r.id}`);
    },
  });

  // 저장하지 않은 입력을 두고 나가려 하면 묻는다 (D43 다섯째). 올린 파일은 이름으로 센다.
  // 편집은 폼을 채우지 않으므로 빈 상태가 그대로 기준이다
  const draft = JSON.stringify({ names, endpoint, auth, credentialSecret, soapSecret, file: file?.name ?? null, text: specText, filename, yaml });
  const dirty = useDirty(draft);
  const confirmLeave = useUnsavedGuard(dirty && !save.isPending && !save.isSuccess);
  // 취소는 온 곳으로 돌아간다. 편집은 상세에서 왔으므로 목록으로 보내면 한 번 더 찾아 들어가야 한다
  const backTo = editing ? `/backend-apis/${editingId}` : "/backend-apis";

  const input = currentInput();
  const canSave = Boolean(name) && (Boolean(input) || Boolean(yaml.trim()));
  const busy = verify.isPending || generate.isPending || save.isPending;
  // 무엇이 올라가는지는 save 의 분기와 같은 순서다
  const saveTarget = generated && yaml.trim() ? text.target.generated : file ? text.target.file(file.name) : specText.trim() ? text.target.text : null;

  // 없는 id 로 편집에 들어오면 이름이 잠긴 빈 폼이 남아 저장도 못 한다. 목록을 다 읽은 뒤에 판단한다
  if (editing && profiles.data && !editTarget) return <NotFoundPage resource={t.resources.kind["backend-api"]} backTo="/backend-apis" />;

  // 화면 조각을 먼저 만들어 둔다. 편집과 만들기가 같은 조각을 다른 자리에 놓기 때문이다.
  // 편집은 "등록된 문서 | 새 문서" 가 나란히 서야 무엇을 무엇으로 바꾸는지가 한눈에 보인다
  const registeredCard = editing ? (
      <Card data-testid="registered-spec">
        <CardHeader>
          <CardTitle>{text.registered}</CardTitle>
          {sourceVersion ? <Badge data-testid="registered-spec-version">v{sourceVersion}</Badge> : null}
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {/* 질의를 걸지 않는 경우다. gate 도 오류도 없어 아무 말 없는 빈 카드가 남는다 */}
          {sourceVersion === null && profiles.data ? (
            <p className="text-sm text-muted" data-testid="registered-spec-none">
              {text.registeredNone}
            </p>
          ) : null}
          {sourceGate}
          {source.data ? (
            <>
              <SpecEditor value={prettySource ?? source.data.text} readOnly height="18rem" testId="registered-spec-text" />
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-xs text-muted" data-testid="registered-spec-format">
                  {source.data.source_format} · source.{source.data.source_ext}
                </span>
                {/* 한 줄로 밀어 넣은 JSON 이나 줄바꿈 없는 WSDL 은 그대로 띄우면 읽을 수가 없다.
                    보기만 바꾸고 저장된 본문은 건드리지 않는다. 못 알아보는 문서면 아예 안 보인다 */}
                {canPretty ? (
                  <label className="flex items-center gap-1.5 text-xs text-muted">
                    <input type="checkbox" className="size-3.5" data-testid="registered-spec-pretty" checked={pretty} onChange={(e) => setPretty(e.target.checked)} />
                    {text.prettyView}
                  </label>
                ) : null}
                {/* 원본을 그대로 가져와야 확장자가 유지된다. WSDL 을 OpenAPI 로 덮으면 출처가 바뀐다 */}
                <Button
                  variant="secondary"
                  data-testid="use-registered-spec"
                  disabled={busy}
                  onClick={() => {
                    if (!source.data) return;
                    setSpecText(source.data.text);
                    setFilename(`spec.${source.data.source_ext}`);
                    setFile(null);
                    clearResults();
                  }}
                >
                  {text.useRegistered}
                </Button>
              </div>
            </>
          ) : null}
        </CardContent>
      </Card>
  ) : null;

  // 저장 피드백은 만들기와 편집이 같은 것을 다른 자리에 놓는다. 만들기만 이름 오류를 칸 아래로
  // 내려보내므로(NameFields 가 그린다) 그때는 여기서 한 번 더 적지 않는다
  const saveFeedback = (
    <>
      {isNameFieldError(save.error) && !editing ? null : <ErrorMessage error={save.error} testId="save-error" />}
      <ErrorVerdict error={save.error} testId="save-error-verdict" />
      <p className="text-xs text-muted" data-testid="save-target">
        {saveTarget ? text.saveTarget(saveTarget) : text.noSaveTarget}
      </p>
    </>
  );

  const specCard = (
    <Card>
      <CardHeader>
        <CardTitle>{editing ? text.newDocument : text.spec}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <Field label={text.uploadFile} htmlFor="spec-file" hint="WSDL, Swagger 2.0, OpenAPI 3.x">
          <Input
            id="spec-file"
            type="file"
            data-testid="spec-file"
            onChange={(e) => {
              const f = e.target.files?.[0] ?? null;
              setFile(f);
              if (f) setSpecText("");
              clearResults();
            }}
          />
        </Field>
        <div className="text-center text-xs text-dim">{text.or}</div>
        <Field label={text.specText} hint={text.specTextHint} htmlFor="spec-text">
          {/* 줄 번호와 접기가 있어야 긴 스펙을 읽는다. Monaco 는 무거워 lazy 다 */}
          <SpecEditor
            value={specText}
            placeholder={SPEC_EXAMPLE}
            testId="spec-text"
            onChange={(next) => {
              setSpecText(next);
              if (next) setFile(null);
              clearResults();
            }}
          />
        </Field>
        <Field
          label={text.labels.filename}
          htmlFor="spec-filename"
          hint={text.filenameHint}
        >
          <Input
            id="spec-filename"
            data-testid="spec-filename"
            value={filename}
            placeholder="orders.yaml"
            onChange={(e) => {
              setFilename(e.target.value);
              clearResults();
            }}
          />
        </Field>
        {/* 둘 다 저장 전에 미리 보는 것이라 눌러도 아무것도 저장되지 않는다. "생성" 이라고만 적으면
            무엇이 생기는지 알 수 없어 Backend API 를 만드는 버튼으로 읽힌다 */}
        <div className="flex flex-wrap gap-2">
          <Button
            variant="secondary"
            data-testid="verify-button"
            disabled={!input || busy}
            onClick={() => input && verify.mutate(input)}
          >
            {verify.isPending ? text.verifying : text.verify}
          </Button>
          <Button
            variant="secondary"
            data-testid="generate-button"
            disabled={!input || busy}
            onClick={() => input && generate.mutate(input)}
          >
            {generate.isPending ? text.converting : text.convert}
          </Button>
        </div>
        <p className="text-xs text-muted" data-testid="preview-hint">
          {text.previewHint(<b className="font-medium text-fg-soft">{text.verify}</b>, <b className="font-medium text-fg-soft">{text.convert}</b>)}
        </p>
        <ErrorMessage error={verify.error} testId="verify-error" />
        <ErrorVerdict error={verify.error} testId="verify-error-verdict" />
        <ErrorMessage error={generate.error} testId="generate-error" />
        <ErrorVerdict error={generate.error} testId="generate-error-verdict" />
        {/* 편집에는 폼 카드가 없다. 무엇이 저장되는지는 그것을 고르는 자리 옆에 적는다 */}
        {editing ? (
          <>
            {/* 만들기 쪽 경고와 뜻이 다르다. 그쪽은 "이름이 겹친다" 는 경고이고 이쪽은 설명이다 */}
            {existing ? (
              <Alert variant="info" data-testid="bp-version-note">
                {text.versionNote(nextVersion)}
              </Alert>
            ) : null}
            {saveFeedback}
          </>
        ) : null}
      </CardContent>
    </Card>
  );

  const verifyCard = verifyResult ? (
  <Card data-testid="verify-result">
    <CardHeader>
      <CardTitle>{text.verifyResult}</CardTitle>
      <div className="flex items-center gap-2 text-xs text-muted">
        <span>
          {text.format} <Badge data-testid="verify-source-format">{verifyResult.source_format}</Badge>
        </span>
        <span>
          {text.kind} <Badge data-testid="verify-kind">{verifyResult.kind ?? "-"}</Badge>
        </span>
        {verifyResult.mixed ? <Badge variant="error">{text.mixed}</Badge> : null}
      </div>
    </CardHeader>
    <CardContent className="flex flex-col gap-3">
      <VerdictPanel verdict={verifyResult.verdict} />
      {/* 검사별 결과는 무엇을 어디서 고치면 되는지까지 적혀 있다. 상세 화면과 같은 표다 */}
      {verifyResult.report?.results?.length ? (
        <CheckResultsTable results={verifyResult.report.results} kind={verifyResult.kind} />
      ) : null}
      <div>
        <div className="mb-1 text-sm font-medium">
          {text.tools} <span className="text-muted">({verifyResult.tool_names.length})</span>
        </div>
        <ToolList names={verifyResult.tool_names} />
      </div>
    </CardContent>
  </Card>
) : null;

  const generateCard = generated ? (
  <Card data-testid="generate-result">
    <CardHeader>
      <CardTitle>{text.generated}</CardTitle>
      <Badge variant={generated.verdict.level === "ok" ? "ok" : "warn"} data-testid="generate-verdict-level">
        {generated.verdict.level}
      </Badge>
    </CardHeader>
    <CardContent>
      <SpecEditor value={yaml} onChange={setYaml} testId="generated-yaml" height="30rem" />
      <p className="mt-1 text-xs text-muted">{text.generatedNote}</p>
    </CardContent>
  </Card>
) : null;

  // 편집은 문서만 다룬다. 접속 정보는 상세의 "접속 정보 저장" 이, 이름은 제목 옆 연필이 맡는다
  const formCard = editing ? null : (
  <Card>
    <CardHeader>
      <CardTitle>{text.profile}</CardTitle>
    </CardHeader>
    <CardContent>
      <form
        className="flex flex-col gap-3"
        data-testid="backend-api-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (canSave) save.mutate();
        }}
      >
        <NameFields testId="bp" value={names} onChange={setNames} errors={nameFieldErrors(save.error)} />
        {existing ? (
          <Alert variant="warn" data-testid="bp-name-exists">
            {text.nameExists(
              <Link className="font-medium underline underline-offset-2" to={`/backend-apis/${existing.id}`}>
                {displayName(existing)}
              </Link>,
              nextVersion,
            )}
          </Alert>
        ) : null}
        <Field label={t.backendApis.connection.labels.endpoint} hint={t.backendApis.connection.endpointHint} htmlFor="bp-endpoint">
          <Input
            id="bp-endpoint"
            data-testid="bp-endpoint"
            value={endpoint}
            placeholder="https://backend.example.com"
            onChange={(e) => setEndpoint(e.target.value)}
          />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label={t.backendApis.connection.labels.auth} hint={t.backendApis.connection.authHint} htmlFor="bp-auth">
            <Select
              id="bp-auth"
              data-testid="bp-auth"
              value={auth}
              onChange={(e) => setAuth(e.target.value as (typeof AUTH_KINDS)[number])}
            >
              {AUTH_KINDS.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </Select>
          </Field>
          <Field label={t.backendApis.connection.labels.secret} htmlFor="bp-secret" hint={text.secretHint}>
            <Input
              id="bp-secret"
              data-testid="bp-secret"
              value={credentialSecret}
              disabled={auth === "none"}
              onChange={(e) => setCredentialSecret(e.target.value)}
            />
          </Field>
        </div>
        {showSoap ? (
          <Field label={t.backendApis.connection.labels.soapHeadersSecret} hint={t.backendApis.detail.soapHeadersHint} htmlFor="bp-soap-secret">
            <Input id="bp-soap-secret" data-testid="bp-soap-secret" value={soapSecret} onChange={(e) => setSoapSecret(e.target.value)} />
          </Field>
        ) : null}
        {saveFeedback}
        <div className="flex justify-end gap-2">
          <Button
            variant="ghost"
            data-testid="cancel-button"
            onClick={() => {
              if (confirmLeave()) navigate(backTo);
            }}
          >
            {t.common.cancel}
          </Button>
          <Button type="submit" data-testid="save-button" disabled={!canSave || busy}>
            {save.isPending ? t.shared.saving : existing ? text.saveVersion : t.shared.save}
          </Button>
        </div>
      </form>
    </CardContent>
  </Card>
  );

  return (
    <PageWidth data-testid="backend-api-new-page">
      {/* 편집은 무엇을 고치는지가 제목이다. 리소스 종류만 적힌 제목은 어느 것인지 말해 주지 않는다 */}
      <PageHeader
        title={rename.title ?? (editing ? (editTarget ? displayName(editTarget) : t.resources.kind["backend-api"]) : text.title)}
        name={editing ? editTarget?.name : undefined}
        testId="page-title"
        /* 이름을 고치려고 다른 화면으로 보내지 않는다. 연필은 제목 옆이 제자리다 */
        badges={rename.badges}
        /* 편집의 저장은 문서 하나뿐이라 폼 안이 아니라 화면 맨 위에 둔다 */
        primary={
          editing ? (
            <Button data-testid="save-button" disabled={!canSave || busy} onClick={() => save.mutate()}>
              {save.isPending ? t.shared.saving : text.saveNewVersion}
            </Button>
          ) : undefined
        }
        secondary={
          editing ? (
            <Button
              variant="ghost"
              data-testid="cancel-button"
              onClick={() => {
                if (confirmLeave()) navigate(backTo);
              }}
            >
              {t.common.cancel}
            </Button>
          ) : undefined
        }
      />
      {rename.feedback}
      {editing ? (
        <>
          {/* 칸마다 담긴 것의 길이가 달라 늘리지 않는다. 늘리면 짧은 쪽에 빈 바닥이 생긴다 */}
          <div className="grid items-start gap-4 lg:grid-cols-2">
            {registeredCard}
            {specCard}
          </div>
          {verifyResult || generated ? (
            <div className="mt-4 flex flex-col gap-4">
              {verifyCard}
              {generateCard}
            </div>
          ) : null}
        </>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          <div className="flex flex-col gap-4">{specCard}</div>
          <div className="flex flex-col gap-4">
            {verifyCard}
            {generateCard}
            {formCard}
          </div>
        </div>
      )}
    </PageWidth>
  );
}
