/**
 * MCP 토큰 발급자 카드 (P53b 6.9절). Platform Settings 아래에 놓인다.
 *
 * oidc 모드에서 Gateway·노출한 MCP Server·노출한 Agent 가 받는 사람 토큰의 발급자와 그 전환을 보인다. 바꾸는 것은 설치 env
 * `DAEGAM_DATA_PLANE_ISSUER` 라 단추가 없다. `GET /settings/data-plane-auth` 의 `data_plane_issuer` 를 읽는다. 그 값은 oidc
 * 모드에서만 있으므로 다른 모드에서는 부르지도 않고 카드도 없다. 웹 Tool 카드도 같은 응답을 읽으므로 같은 키를 쓴다. 플랫폼을
 * 믿는 동안 남은 항목이 있으면 다시 읽는다. 문서는 다른 테넌트의 것이 섞이므로 링크 없이 테넌트와 이름만 보인다.
 */
import { useQuery } from "@tanstack/react-query";

import { getDataPlaneAuth } from "@/api/resources";
import type { DataPlaneIssuer } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Badge } from "@/components/ui/badge";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { type Messages, useT } from "@/i18n";
import { useAuthState } from "@/lib/auth";
import { resourceLabel } from "@/lib/deletion";
import { DATA_PLANE_AUTH_KEY } from "@/lib/queryKeys";
import { TELEMETRY_POLL_MS } from "@/lib/usage";
import { formatDate } from "@/lib/utils";

const PLATFORM = "platform";

/** 전환이 바꾸거나 열어 두는 목록. 사전의 `lists` 와 같은 순서로 보인다. */
const LISTS = [
  "tenants_foreign_mcp_group",
  "gateways_idp_names",
  "gateways_permit",
  "exposed_agents_foreign_groups",
  "exposed_agents_open",
  "exposed_agents_one_issuer",
  "tenants_agent_client_ids",
  "agents_gateway_sources",
  "gateways_published_refused",
] as const satisfies readonly (keyof DataPlaneIssuer)[];

type PendingKind = DataPlaneIssuer["clusters"][number]["pending_documents"][number]["kind"];

/** 렌더를 기다리는 문서의 종류를 화면의 자원 이름으로. 공유 서버의 복제본만 자원 이름이 없다. */
const PENDING_KINDS = { gateway: "gateway", mcp_server: "mcp-server", agent: "agent", hub_item: "hub", shared_clone: null } as const satisfies Record<PendingKind, string | null>;

function pendingKindText(t: Messages, kind: PendingKind): string {
  const resource = PENDING_KINDS[kind];
  return resource ? resourceLabel(t, resource) : t.admin.platformSettings.issuer.sharedClone;
}

/** 목록 하나의 줄들. 문서는 `테넌트 / 이름` 이고, 노출한 Agent 는 그룹, 붙잡은 테넌트는 저장한 mcp_group 을 붙인다. */
function listLines(s: DataPlaneIssuer, key: (typeof LISTS)[number]): string[] {
  switch (key) {
    case "tenants_foreign_mcp_group":
      return s.tenants_foreign_mcp_group.map((r) => `${r.tenant}: ${r.mcp_group}`);
    case "tenants_agent_client_ids":
      return s.tenants_agent_client_ids;
    case "exposed_agents_foreign_groups":
      return s.exposed_agents_foreign_groups.map((d) => `${d.tenant} / ${d.name}: ${d.groups.join(", ")}`);
    default:
      return s[key].map((d) => `${d.tenant} / ${d.name}`);
  }
}

/** 다시 읽을 때. 플랫폼을 믿고 남은 항목이 있을 때다. IdP 를 믿는 동안은 확인하지 않으므로 pending 이 늘 0 이다. */
export function switching(s: DataPlaneIssuer | null | undefined): boolean {
  return !!s && s.trusted === PLATFORM && s.pending > 0;
}

export function IssuerSwitchCard() {
  const auth = useAuthState();
  const isPlatformAdmin = auth.me?.is_platform_admin ?? false;
  const oidc = auth.mode === "oidc";
  // 플랫폼 관리자만 읽는다. GET /me 로 그것을 안 뒤에, 값이 있는 oidc 모드에서만 부른다
  const status = useQuery({
    queryKey: DATA_PLANE_AUTH_KEY,
    queryFn: getDataPlaneAuth,
    enabled: isPlatformAdmin && oidc,
    refetchInterval: (q) => (switching(q.state.data?.data_plane_issuer) ? TELEMETRY_POLL_MS : false),
  });
  const t = useT();
  if (!oidc) return null;
  const s = status.data?.data_plane_issuer;
  const text = t.admin.platformSettings.issuer;
  return (
    <Card className="mt-4" data-testid="ps-issuer">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        {s ? (
          <Badge variant={s.trusted === PLATFORM ? "ok" : "muted"} data-testid="ps-issuer-trusted">
            {text.trusted[s.trusted]}
          </Badge>
        ) : null}
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        <p className="text-xs text-muted">{text.body}</p>
        <ErrorMessage error={status.error} testId="ps-issuer-load-error" />
        {s ? <IssuerStatus s={s} /> : null}
      </CardContent>
    </Card>
  );
}

function IssuerStatus({ s }: { s: DataPlaneIssuer }) {
  const t = useT();
  const text = t.admin.platformSettings.issuer;
  const trusted = s.trusted === PLATFORM;
  const lists = LISTS.map((key) => ({ key, lines: listLines(s, key) })).filter((l) => l.lines.length);
  return (
    <>
        {s.error ? (
          <Alert variant="error" data-testid="ps-issuer-held">
            {text.held}
          </Alert>
        ) : null}
        {trusted ? (
          <p className="text-xs" data-testid="ps-issuer-progress">
            {s.pending ? text.pending(s.pending) : text.done}
          </p>
        ) : null}
        {trusted && s.clusters.length ? <ClusterTable s={s} /> : null}
        {s.jwks_unreachable_clusters.length ? (
          <Alert variant="warn" data-testid="ps-issuer-jwks">
            {text.jwksUnreachable(s.jwks_unreachable_clusters.join(", "))}
          </Alert>
        ) : null}
        <p className="text-xs text-muted" data-testid="ps-issuer-token-life">
          {text.tokenLife(s.idp_token_life.largest_seconds, s.idp_token_life.limit_seconds)}
        </p>
        {s.idp_token_life.warn ? (
          <Alert variant="warn" data-testid="ps-issuer-token-life-warn">
            {text.tokenLifeWarn}
          </Alert>
        ) : null}
        <section className="flex flex-col gap-1.5 text-xs" aria-label={text.listsTitle} data-testid="ps-issuer-lists">
          <span className="font-medium">{text.listsTitle}</span>
          {lists.length ? (
            lists.map(({ key, lines }) => <NameList key={key} title={text.lists[key].label} note={text.lists[key].note} lines={lines} testId={`ps-issuer-list-${key}`} />)
          ) : (
            <span className="text-muted" data-testid="ps-issuer-lists-empty">
              {text.listsEmpty}
            </span>
          )}
        </section>
    </>
  );
}

/** 클러스터마다의 전환. 렌더를 기다리는 문서와 플랫폼 토큰을 아직 받지 않은 곳은 표 아래에 접어 두고 이름을 적는다. */
function ClusterTable({ s }: { s: DataPlaneIssuer }) {
  const t = useT();
  const text = t.admin.platformSettings.issuer;
  const docs = s.clusters.flatMap((c) =>
    c.pending_documents.map((d) => `${c.cluster_id}: ${pendingKindText(t, d.kind)} ${d.tenant} / ${d.name}${d.blocked ? ` (${text.blocked})` : ""}`),
  );
  const verifiers = s.clusters.flatMap((c) =>
    c.waiting_verifiers.map((v) => `${c.cluster_id}: ${v.kind} ${v.namespace}/${v.name} (${v.refused ? text.refused : text.notCalled})`),
  );
  return (
    <div className="flex flex-col gap-2" data-testid="ps-issuer-clusters">
      <div className="overflow-x-auto">
        <Table>
          <THead>
            <TR>
              <TH>{text.columns.cluster}</TH>
              <TH>{text.columns.probedAt}</TH>
              <TH>{text.columns.platformNamespaces}</TH>
              <TH>{text.columns.idpNamespaces}</TH>
              <TH>{text.columns.pendingDocuments}</TH>
              <TH>{text.columns.waitingVerifiers}</TH>
              <TH>{text.columns.verifiedVerifiers}</TH>
            </TR>
          </THead>
          <TBody>
            {s.clusters.map((c) => (
              <TR key={c.cluster_id} data-testid={`ps-issuer-cluster-${c.cluster_id}`}>
                <TD className="font-mono">{c.cluster_id}</TD>
                <TD>
                  {c.probed_at ? formatDate(t, c.probed_at) : text.notProbed}
                  {c.error ? (
                    <span className="block text-err-fg">
                      {text.probeFailed}: {c.error}
                    </span>
                  ) : null}
                </TD>
                <TD>{c.platform_namespaces.length}</TD>
                <TD>{c.idp_namespaces.length}</TD>
                <TD>{c.pending_documents.length}</TD>
                <TD>{c.waiting_verifiers.length}</TD>
                <TD>{c.verified_verifiers.length}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      </div>
      {docs.length ? <NameList title={text.pendingDocumentsTitle} lines={docs} testId="ps-issuer-pending-documents" /> : null}
      {verifiers.length ? <NameList title={text.waitingVerifiersTitle} lines={verifiers} testId="ps-issuer-waiting-verifiers" /> : null}
    </div>
  );
}

/** 접어 둔 이름 목록. 제목에 줄 수를 붙이고, note 가 있으면 펼친 첫 줄에 보인다. */
function NameList({ title, note, lines, testId }: { title: string; note?: string; lines: string[]; testId: string }) {
  return (
    <details className="text-xs" data-testid={testId}>
      <summary className="cursor-pointer">
        {title} ({lines.length})
      </summary>
      {note ? <p className="mt-1 text-muted">{note}</p> : null}
      <ul className="ml-4 list-disc font-mono">
        {lines.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
    </details>
  );
}
