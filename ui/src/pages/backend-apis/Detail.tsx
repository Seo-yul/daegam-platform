import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import {
  getBackendApi,
  getVersionDetail,
  issueToken,
  listTokens,
  listVersions,
  patchBackendApi,
  revokeToken,
} from "@/api/resources";
import type { BackendAPI, IssuedToken } from "@/api/types";
import { ApiDocsDialog } from "@/components/ApiDocsDialog";
import { Trash2 } from "lucide-react";

import { ErrorMessage } from "@/components/ErrorMessage";
import { GenerateMenu } from "@/components/GenerateMenu";
import { PageHeader } from "@/components/Layout";
import { NotFoundPage } from "@/components/NotFoundPage";
import { PageWidth } from "@/components/PageWidth";
import { ReferencesCard } from "@/components/ReferencesCard";
import { ResourceDeleteDialog } from "@/components/ResourceDeleteDialog";
import { OneTimeToken } from "@/components/tokens/OneTimeToken";
import { CheckResultsTable, ToolList } from "@/components/VerdictPanel";
import { Badge, levelVariant } from "@/components/ui/badge";
import { IconButton } from "@/components/ui/icon-button";
import { Chevron } from "@/components/ui/icons";
import { Button, buttonVariants } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { EmptyRow, Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { AUTH_KINDS, displayName, secondaryName } from "@/lib/names";
import { queryGate } from "@/components/QueryGate";
import { useTenant } from "@/lib/tenant";
import { formatDate } from "@/lib/utils";
import { countCheckResults } from "@/lib/verifyChecks";
import { missingResource } from "@/lib/warningText";

export function BackendApiDetailPage() {
  const t = useT();
  const text = t.backendApis.detail;
  const { id = "" } = useParams();
  const tenant = useTenant();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const profile = useQuery({
    queryKey: ["backend-apis", tenant, id],
    queryFn: () => getBackendApi(id),
  });
  const versions = useQuery({
    queryKey: ["backend-apis", tenant, id, "versions"],
    queryFn: () => listVersions(id),
  });
  const tokens = useQuery({
    queryKey: ["backend-apis", tenant, id, "tokens"],
    queryFn: () => listTokens(id),
  });

  // 처음 선택은 ?version= (Refine 적용 뒤 새 버전으로 돌아올 때) 이고, 없거나 목록에 없으면 최신 버전이다.
  // 방금 만든 버전은 캐시된 목록에 아직 없을 수 있으므로 다시 받아오는 동안은 기다린다.
  const [params] = useSearchParams();
  const wantedVersion = Number(params.get("version"));
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  useEffect(() => {
    if (selectedVersion !== null || !versions.data?.length) return;
    const wanted = versions.data.find((v) => v.version === wantedVersion);
    if (!wanted && wantedVersion > 0 && versions.isFetching) return;
    setSelectedVersion((wanted ?? versions.data[versions.data.length - 1]).version);
  }, [versions.data, versions.isFetching, selectedVersion, wantedVersion]);
  const versionDetail = useQuery({
    queryKey: ["backend-apis", tenant, id, "versions", selectedVersion],
    queryFn: () => getVersionDetail(id, selectedVersion as number),
    enabled: selectedVersion !== null,
  });

  const [deleting, setDeleting] = useState(false);

  // API 문서 팝업. operationId 가 있으면 그 도구를 선택한 채 연다.
  const [docs, setDocs] = useState<{ open: boolean; operationId: string | null }>({ open: false, operationId: null });
  const openDocs = (operationId: string | null = null) => setDocs({ open: true, operationId });

  // Verify report 펼침. 버전마다 사용자가 누른 값을 기억하고, 누르기 전에는 warn·fail 이 있을 때만 펼친다.
  // 접힘은 버전마다가 아니라 화면 하나의 상태다. 버전을 갈아 가며 같은 보고서를 볼 때 펼친 것이
  // 버전마다 따로 기억되면 다음 버전에서 또 펴야 한다. null 이면 아직 사람이 건드리지 않은 것이라
  // 경고·실패가 있는 쪽을 열어 둔다
  const [reportOpen, setReportOpen] = useState<boolean | null>(null);

  // 지워진 리소스의 북마크를 따라오면 붉은 줄 하나만 남는다. 404 는 나갈 길이 있는 화면으로 받는다
  if (missingResource(profile.error, profile.data)) return <NotFoundPage resource={t.resources.kind["backend-api"]} backTo="/backend-apis" />;
  const gate = queryGate(profile);
  if (gate) return gate;
  const p = profile.data!;

  const docsVersion = selectedVersion ?? p.latest_version ?? null;
  const results = versionDetail.data?.report?.results ?? [];
  const counts = countCheckResults(results);
  const isReportOpen = selectedVersion !== null ? (reportOpen ?? counts.warn + counts.fail > 0) : false;

  return (
    <PageWidth data-testid="backend-api-detail-page" className="flex flex-col gap-4">
      <PageHeader
        title={displayName(p)}
        name={secondaryName(p)}
        testId="page-title"
        /* 주 동작은 API 문서 편집이다. 스펙이 바뀌면 여기로 온다. 저장하면 새 문서가 아니라 이 문서의
           새 버전이 된다. 접속 정보는 아래 카드가, 표시 이름은 편집 화면의 제목이 맡는다 */
        primary={
          <Link to={`/backend-apis/${p.id}/edit`} className={buttonVariants()} data-testid="edit-button">
            {text.editDocs}
          </Link>
        }
        secondary={
          /* 더보기에 삭제 하나뿐이었다. 접을 값이 없어 아이콘으로 내놓는다 */
          <IconButton label={t.common.delete} testId="delete-button" danger onClick={() => setDeleting(true)}>
            <Trash2 size={16} />
          </IconButton>
        }

      />
      <ResourceDeleteDialog
        resource="backend-api"
        target={deleting ? { id: p.id, name: p.name } : null}
        onClose={() => setDeleting(false)}
        onDeleted={() => navigate("/backend-apis")}
      />

      <ApiDocsDialog
        profileId={p.id}
        profileName={p.name}
        version={docsVersion}
        open={docs.open}
        operationId={docs.operationId}
        onClose={() => setDocs({ open: false, operationId: null })}
      />

      {/* 개요는 버전·도구·접속 정보·참조 넷이다 (D44 셋째) */}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card data-testid="profile-summary">
          <CardHeader>
            <CardTitle>{text.version}</CardTitle>
            <div className="flex items-center gap-2">
              {p.kind ? <Badge>{p.kind}</Badge> : null}
              <Button
                size="sm"
                variant="outline"
                data-testid="open-api-docs"
                disabled={docsVersion === null}
                title={docsVersion === null ? text.noVersion : text.docsTitle(docsVersion)}
                onClick={() => openDocs()}
              >
                {text.openDocs}
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            <dl className="grid grid-cols-[8rem_1fr] gap-y-1 text-sm">
              <dt className="text-muted">{t.backendApis.latestVersion}</dt>
              <dd data-testid="summary-latest-version">{p.latest_version ?? "-"}</dd>
              <dt className="text-muted">{text.sourceFormat}</dt>
              <dd>{p.source_format ?? "-"}</dd>
              <dt className="text-muted">{t.backendApis.created}</dt>
              <dd>{formatDate(t, p.created_at)}</dd>
              <dt className="text-muted">{text.updated}</dt>
              <dd>{formatDate(t, p.updated_at)}</dd>
              <dt className="text-muted">{t.detail.summaryFields.id}</dt>
              <dd className="font-mono text-xs">{p.id}</dd>
            </dl>
          </CardContent>
        </Card>

        <ConnectionCard profile={p} />

        <Card data-testid="backend-tools-card">
          <CardHeader>
            <CardTitle>{text.toolsTitle}</CardTitle>
            <Badge variant="muted" data-testid="summary-tool-count">
              {text.toolCount(p.tool_count ?? null)}
            </Badge>
          </CardHeader>
          <CardContent>
            {p.tool_names?.length ? (
              <ToolList names={p.tool_names} onSelect={(name) => openDocs(name)} />
            ) : (
              <p className="text-sm text-muted">{text.noTools}</p>
            )}
          </CardContent>
        </Card>

        <ReferencesCard resource="backend-api" id={p.id} />
      </div>

      <Card data-testid="versions-card">
        <CardHeader>
          <CardTitle>{text.versionsTitle}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <ErrorMessage error={versions.error} />
          <Table data-testid="versions-table">
            <THead>
              <tr>
                <TH>{text.version}</TH>
                <TH>{t.backendApis.kind}</TH>
                <TH>{text.versionColumns.format}</TH>
                <TH>{t.shared.columns.tools}</TH>
                <TH>{text.versionColumns.check}</TH>
                <TH>s2o</TH>
                <TH>{t.backendApis.created}</TH>
                <TH></TH>
              </tr>
            </THead>
            <TBody>
              {versions.data?.length ? (
                versions.data.map((v) => (
                  <TR
                    key={v.version}
                    data-testid={`version-row-${v.version}`}
                    data-selected={v.version === selectedVersion ? "true" : undefined}
                    className={v.version === selectedVersion ? "bg-hover" : "cursor-pointer"}
                    onClick={() => setSelectedVersion(v.version)}
                  >
                    <TD>v{v.version}</TD>
                    <TD>{v.kind}</TD>
                    <TD>{v.source_format}</TD>
                    <TD>{v.tool_count}</TD>
                    <TD>
                      <Badge variant={levelVariant(v.report?.ok ?? null)}>{v.report?.ok ? text.ok : text.fail}</Badge>
                    </TD>
                    <TD>{v.s2o_version}</TD>
                    <TD>{formatDate(t, v.created_at)}</TD>
                    <TD className="text-right">
                      <GenerateMenu profileId={id} version={v.version} />
                    </TD>
                  </TR>
                ))
              ) : (
                <EmptyRow colSpan={8}>{versions.isLoading ? t.common.loading : text.noVersions}</EmptyRow>
              )}
            </TBody>
          </Table>

          {selectedVersion !== null ? (
            <div data-testid="version-report">
              <button
                type="button"
                data-testid="verify-report-toggle"
                aria-expanded={isReportOpen}
                aria-controls="verify-report-body"
                className="flex w-full items-center gap-2 rounded-item py-1 text-left text-sm font-medium text-fg hover:text-heading focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring"
                onClick={() => setReportOpen(!isReportOpen)}
              >
                <Chevron open={isReportOpen} />
                <span>{text.reportTitle(selectedVersion)}</span>
                {results.length ? (
                  <span className="flex items-center gap-1" data-testid="verify-report-counts">
                    <Badge variant="ok">
                      {text.pass} {counts.pass}
                    </Badge>
                    <Badge variant={counts.warn ? "warn" : "muted"}>
                      {text.warn} {counts.warn}
                    </Badge>
                    <Badge variant={counts.fail ? "error" : "muted"}>
                      {text.fail} {counts.fail}
                    </Badge>
                  </span>
                ) : versionDetail.isLoading ? (
                  <span className="text-xs text-muted">{t.common.loading}</span>
                ) : null}
              </button>
              {isReportOpen ? (
                <div id="verify-report-body" data-testid="verify-report-body" className="mt-2">
                  <ErrorMessage error={versionDetail.error} />
                  {results.length ? (
                    <CheckResultsTable results={results} kind={versionDetail.data?.kind} />
                  ) : (
                    <p className="text-sm text-muted">{versionDetail.isLoading ? t.common.loading : text.noResults}</p>
                  )}
                </div>
              ) : null}
            </div>
          ) : null}
        </CardContent>
      </Card>

      <TokensCard
        profileId={id}
        tokens={tokens.data ?? []}
        loading={tokens.isLoading}
        error={tokens.error}
        onChanged={() => queryClient.invalidateQueries({ queryKey: ["backend-apis", tenant, id, "tokens"] })}
      />
    </PageWidth>
  );
}

function ConnectionCard({ profile }: { profile: BackendAPI }) {
  const t = useT();
  const text = t.backendApis.detail;
  const queryClient = useQueryClient();
  const [endpoint, setEndpoint] = useState(profile.endpoint ?? "");
  const [auth, setAuth] = useState(profile.auth);
  const [secret, setSecret] = useState(profile.credential_secret ?? "");
  const [soapSecret, setSoapSecret] = useState(profile.soap_headers_secret ?? "");
  // SOAP 헤더 Secret 은 WSDL 에서 만든 백엔드에만 뜻이 있다. 이미 값이 있으면 종류와 상관없이 보여 지울 수 있게 한다
  const showSoap = profile.kind === "soap" || Boolean(profile.soap_headers_secret);
  useEffect(() => {
    setEndpoint(profile.endpoint ?? "");
    setAuth(profile.auth);
    setSecret(profile.credential_secret ?? "");
    setSoapSecret(profile.soap_headers_secret ?? "");
  }, [profile]);

  const patch = useMutation({
    mutationFn: () =>
      patchBackendApi(profile.id, {
        endpoint: endpoint.trim() || null,
        auth,
        credential_secret: secret.trim() || null,
        ...(showSoap ? { soap_headers_secret: soapSecret.trim() || null } : {}),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["backend-apis"] }),
  });

  return (
    <Card data-testid="connection-card">
      <CardHeader>
        <CardTitle>{text.connectionTitle}</CardTitle>
      </CardHeader>
      <CardContent>
        <form
          className="flex flex-col gap-3"
          data-testid="connection-form"
          onSubmit={(e) => {
            e.preventDefault();
            patch.mutate();
          }}
        >
          <Field label={t.backendApis.connection.labels.endpoint} hint={t.backendApis.connection.endpointHint} htmlFor="conn-endpoint">
            <Input id="conn-endpoint" data-testid="conn-endpoint" value={endpoint} onChange={(e) => setEndpoint(e.target.value)} />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label={t.backendApis.connection.labels.auth} hint={t.backendApis.connection.authHint} htmlFor="conn-auth">
              <Select id="conn-auth" data-testid="conn-auth" value={auth} onChange={(e) => setAuth(e.target.value)}>
                {AUTH_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label={t.backendApis.connection.labels.secret} hint={text.secretHint} htmlFor="conn-secret">
              <Input id="conn-secret" data-testid="conn-secret" value={secret} disabled={auth === "none"} onChange={(e) => setSecret(e.target.value)} />
            </Field>
          </div>
          {showSoap ? (
            <Field label={t.backendApis.connection.labels.soapHeadersSecret} hint={text.soapHeadersHint} htmlFor="conn-soap-secret">
              <Input id="conn-soap-secret" data-testid="conn-soap-secret" value={soapSecret} onChange={(e) => setSoapSecret(e.target.value)} />
            </Field>
          ) : null}
          <ErrorMessage error={patch.error} testId="connection-error" />
          {patch.isSuccess ? <Alert variant="ok" data-testid="connection-saved">{t.shared.saved}</Alert> : null}
          <div className="flex justify-end">
            <Button type="submit" data-testid="connection-save" disabled={patch.isPending}>
              {text.saveConnection}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function TokensCard({
  profileId,
  tokens,
  loading,
  error,
  onChanged,
}: {
  profileId: string;
  tokens: { token_id: string; label: string | null; scope: string[]; created_at: string; revoked_at: string | null; active: boolean }[];
  loading: boolean;
  error: unknown;
  onChanged: () => unknown;
}) {
  const t = useT();
  const text = t.backendApis.detail;
  const [label, setLabel] = useState("");
  const [issued, setIssued] = useState<IssuedToken | null>(null);

  const issue = useMutation({
    mutationFn: () => issueToken(profileId, label),
    onSuccess: (token) => {
      setIssued(token);
      setLabel("");
      void onChanged();
    },
  });
  const revoke = useMutation({
    mutationFn: (tokenId: string) => revokeToken(profileId, tokenId),
    onSuccess: () => void onChanged(),
  });

  return (
    <Card data-testid="tokens-card">
      <CardHeader>
        <CardTitle>{text.tokensTitle}</CardTitle>
        <form
          className="flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            issue.mutate();
          }}
        >
          <Input
            aria-label={text.tokenLabel}
            data-testid="token-label"
            className="h-8 w-48"
            placeholder={text.labelPlaceholder}
            value={label}
            onChange={(e) => setLabel(e.target.value)}
          />
          <Button size="sm" type="submit" data-testid="token-issue" disabled={issue.isPending}>
            {text.issue}
          </Button>
        </form>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <ErrorMessage error={error ?? issue.error ?? revoke.error} />
        {issued ? <OneTimeToken token={issued.token} /> : null}
        <Table data-testid="tokens-table">
          <THead>
            <tr>
              <TH>{text.tokenColumns.label}</TH>
              <TH>{text.tokenColumns.scope}</TH>
              <TH>{t.backendApis.created}</TH>
              <TH>{text.tokenColumns.status}</TH>
              <TH></TH>
            </tr>
          </THead>
          <TBody>
            {tokens.length ? (
              tokens.map((token) => (
                <TR key={token.token_id} data-testid={`token-row-${token.token_id}`}>
                  <TD>{token.label ?? <span className="text-dim">-</span>}</TD>
                  <TD className="font-mono text-xs">{token.scope.join(", ") || "*"}</TD>
                  <TD>{formatDate(t, token.created_at)}</TD>
                  <TD>
                    <Badge variant={token.active ? "ok" : "muted"}>{token.active ? text.active : text.revoked}</Badge>
                  </TD>
                  <TD className="text-right">
                    {token.active ? (
                      <Button
                        size="sm"
                        variant="outline"
                        data-testid={`token-revoke-${token.token_id}`}
                        disabled={revoke.isPending}
                        onClick={() => revoke.mutate(token.token_id)}
                      >
                        {t.account.tokenTable.revoke}
                      </Button>
                    ) : null}
                  </TD>
                </TR>
              ))
            ) : (
              <EmptyRow colSpan={5}>{loading ? t.common.loading : text.noTokens}</EmptyRow>
            )}
          </TBody>
        </Table>
      </CardContent>
    </Card>
  );
}
