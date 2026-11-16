/**
 * 클라이언트 설정 내보내기 대화상자 (P55a 2절·8절, D99). 허브 목록이 연다.
 *
 * 사람이 클라이언트(Claude Code, Codex)와 항목을 고르면 브라우저가 `GET /api/hub` 응답에서 설정 파일을 만들어 내려받게 한다. 파일은
 * 요청 테넌트 하나의 것이고 토큰 값은 어디에도 적지 않는다(`lib/clientExport`). 인증 갈래는 연결 카드와 같다. 플랫폼 토큰을 받는
 * 경계이고 OAuth 인가 서버가 켜졌으면 로그인과 토큰 두 갈래를 보이고, IdP 를 믿는 oidc 는 IdP 토큰 안내, header 모드는 누구나
 * 부를 수 있다는 경고다. 주소의 갈래는 서버가 정한 것(`status.address_class`)을 읽는다.
 *
 * 플랫폼 관리자는 Claude Code 관리 설정에 합칠 회사 전체 허용 목록을 함께 받고, 그다음 선택으로 독점 관리 파일을 받는다.
 */
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router";

import { listClusters } from "@/api/resources";
import type { HubItem } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { CodeSnippet } from "@/components/ui/code-snippet";
import { Modal } from "@/components/ui/modal";
import { TabBar } from "@/components/ui/tabs";
import { useT } from "@/i18n";
import { authBoundary, oauthEnabled, takesPlatformTokens, useAuthState } from "@/lib/auth";
import {
  ADMIN_FILE,
  ALLOWLIST_KEY,
  EXPORT_CLIENTS,
  EXPORT_FILE,
  allowlist as companyAllowlist,
  exclusiveFile,
  exportFile,
  exportRows,
  exportServers,
  initialSelection,
  selectable,
  tenantTokenEnv,
  type ExportClient,
  type ExportRow,
  type ExportServer,
} from "@/lib/clientExport";
import { CLIENT_LABEL, CODEX_CONFIG_PATH, CONNECT_MODES, loginCommand, readEnvCommand, type ConnectMode, type ConnectVia } from "@/lib/connectSnippets";
import { saveBlob } from "@/lib/download";
import { hubKindLabel, hubKindVariant } from "@/lib/hub";
import { displayName, secondaryName } from "@/lib/names";
import { memberTenants } from "@/lib/personalTokens";
import { CLUSTERS_KEY } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";
import { cn } from "@/lib/utils";

function save(text: string, file: { name: string; type: string }): void {
  saveBlob(new Blob([text], { type: file.type }), file.name);
}

export function ClientExportDialog({ open, onClose, items }: { open: boolean; onClose: () => void; items: readonly HubItem[] }) {
  const t = useT();
  // 몸은 열릴 때만 그린다. 열 때마다 기본 선택으로 돌아가므로 사설망 주소는 사람이 다시 골라야 한다
  return (
    <Modal open={open} title={t.hub.export.title} testId="client-export-dialog" closeTestId="client-export-close" onClose={onClose}>
      <ExportBody items={items} />
    </Modal>
  );
}

function ExportBody({ items }: { items: readonly HubItem[] }) {
  const t = useT();
  const card = t.detail.connectCard;
  const tenant = useTenant();
  const authState = useAuthState();
  const boundary = authBoundary(authState.mode);
  const platform = takesPlatformTokens(boundary, authState);
  const twoModes = platform && oauthEnabled(authState);
  const [client, setClient] = useState<ExportClient>("claude");
  // 연결 카드처럼 로그인이 먼저다
  const [mode, setMode] = useState<ConnectMode>("login");
  const rows = useMemo(() => exportRows(items), [items]);
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => initialSelection(rows));

  const via: ConnectVia = twoModes ? mode : boundary === "anonymous" ? "open" : "token";
  const chosen = rows.filter((r) => selectable(r, client) && selected.has(r.item.id)).map((r) => r.item);
  const servers = exportServers(chosen, tenant, via);
  const file = exportFile(client, servers);
  const toggle = (id: string, on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });

  return (
    <div className="flex flex-col gap-4 px-5 py-4">
      <div className="flex flex-col gap-1.5">
        <p className="text-sm font-semibold text-heading">{t.hub.export.client}</p>
        <TabBar tabs={EXPORT_CLIENTS} value={client} onChange={setClient} label={(c) => CLIENT_LABEL[c]} />
      </div>
      {twoModes ? <TabBar tabs={CONNECT_MODES} value={mode} onChange={setMode} label={(m) => card.mode[m]} /> : null}
      <ItemList rows={rows} client={client} selected={selected} onToggle={toggle} />
      <Credentials client={client} via={via} platform={platform} tenant={tenant} servers={servers} />
      <CodeSnippet
        title={client === "claude" ? card.snippets.projectJson : CODEX_CONFIG_PATH}
        code={file}
        copyLabel={card.copySettings(CLIENT_LABEL[client])}
        testId="client-export-file"
        copyTestId="client-export-copy"
      />
      <div className="flex justify-end">
        <Button data-testid="client-export-download" disabled={!servers.length} onClick={() => save(file, EXPORT_FILE[client])}>
          {t.hub.export.download}
        </Button>
      </div>
      {authState.me?.is_platform_admin ? <CompanyExport items={items} tenant={tenant} /> : null}
    </div>
  );
}

/**
 * 항목 목록. 주소가 없거나 자격 증명이 들 수 있는 항목은 흐리게 까닭을 달고, 사설망 주소는 흐리게 표시를 달되 고를 수 있다. Codex 를
 * 고르면 SSE 항목도 까닭을 달고 막는다. 고른 것은 남아 Claude Code 로 돌아가면 다시 든다.
 */
function ItemList({ rows, client, selected, onToggle }: { rows: readonly ExportRow[]; client: ExportClient; selected: ReadonlySet<string>; onToggle: (id: string, on: boolean) => void }) {
  const t = useT();
  const text = t.hub.export;
  const reason = (row: ExportRow) => {
    if (row.state === "noAddress") return text.noAddress;
    if (row.state === "credentials") return text.urlCredentials;
    return client === "codex" && row.sse ? text.sseCodex : null;
  };
  return (
    <fieldset className="flex flex-col gap-1.5">
      <legend className="mb-1.5 text-sm font-semibold text-heading">{text.items}</legend>
      <ul className="divide-y divide-line rounded-tile border border-line" data-testid="client-export-items">
        {rows.map((row) => {
          const { item } = row;
          const why = reason(row);
          return (
            <li key={item.id} className={cn("px-3 py-2", (row.state !== "ready" || !selectable(row, client)) && "opacity-60")} data-state={row.state}>
              <label className="flex flex-wrap items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  data-testid={`export-item-${item.name}`}
                  checked={selectable(row, client) && selected.has(item.id)}
                  disabled={!selectable(row, client)}
                  onChange={(e) => onToggle(item.id, e.target.checked)}
                />
                <span className="font-medium">{displayName(item)}</span>
                {secondaryName(item) ? <span className="font-mono text-xs text-muted">{secondaryName(item)}</span> : null}
                <Badge variant={hubKindVariant(item.kind)}>{hubKindLabel(t, item.kind)}</Badge>
                {row.direct ? (
                  <Badge variant="muted" data-testid={`export-item-${item.name}-direct`}>
                    {text.direct}
                  </Badge>
                ) : null}
                {row.state === "private" ? (
                  <Badge variant="warn" data-testid={`export-item-${item.name}-private`}>
                    {text.private}
                  </Badge>
                ) : null}
              </label>
              {why ? (
                <p className="mt-1 pl-6 text-xs text-muted" data-testid={`export-item-${item.name}-reason`}>
                  {why}
                </p>
              ) : null}
            </li>
          );
        })}
      </ul>
      {rows.some((r) => r.direct) ? <p className="text-xs text-muted" data-testid="client-export-direct-hint">{text.directHint}</p> : null}
      {rows.some((r) => r.state === "private") ? <p className="text-xs text-muted" data-testid="client-export-private-hint">{text.privateHint}</p> : null}
    </fieldset>
  );
}

/**
 * 토큰을 넣는 길. 토큰 갈래는 테넌트의 env 를 두는 명령까지 보인다. 로그인 갈래는 플랫폼을 거치는 항목마다 로그인 명령을,
 * 인증 없음은 연결 카드의 경고를 보인다. 문장은 연결 카드의 것이다.
 */
function Credentials({ client, via, platform, tenant, servers }: { client: ExportClient; via: ConnectVia; platform: boolean; tenant: string; servers: readonly ExportServer[] }) {
  const t = useT();
  const text = t.hub.export;
  const card = t.detail.connectCard;
  const { me } = useAuthState();
  if (via === "open") {
    return (
      <Alert variant="warn" data-testid="client-export-anonymous">
        {card.anonymous}
      </Alert>
    );
  }
  if (via === "login") {
    const served = servers.filter((s) => !s.direct).map((s) => loginCommand(client, s.name));
    return (
      <div className="flex flex-col gap-2" data-testid="client-export-login">
        <p className="text-sm text-muted">{card.login}</p>
        {served.length ? (
          <CodeSnippet
            title={card.snippets.commandLine}
            code={served.join("\n")}
            copyLabel={card.readPatCopy}
            note={client === "claude" ? card.snippets.claude.login.json : card.snippets.codexLogin}
            testId="client-export-login-commands"
          />
        ) : null}
        <p className="text-xs text-muted">{card.loginFallback}</p>
      </div>
    );
  }
  const env = tenantTokenEnv(tenant);
  // 토큰은 그 테넌트의 소속 줄이 있어야 받는다. 연결 카드처럼 멤버가 아니면 발급을 권하지 않고 멤버 계정을 말한다
  const member = memberTenants(me).includes(tenant);
  const link = (
    <Link to="/account/tokens" className="text-link underline underline-offset-2 hover:text-link-hover" data-testid="client-export-tokens-link">
      {t.account.menu.tokens}
    </Link>
  );
  return (
    <div className="flex flex-col gap-2" data-testid="client-export-token">
      <Alert variant="info" data-testid="client-export-token-hint">
        {text.tokenHint(tenant)}
      </Alert>
      {/* IdP 토큰만 받는 데이터 플레인은 플랫폼 토큰을 받지 않는다. 연결 카드처럼 IdP 토큰을 넣으라고 한다 */}
      {!platform ? (
        <Alert variant="info">{card.oidc(env)}</Alert>
      ) : member ? (
        <p className="text-sm text-muted" data-testid="client-export-token-steps">
          {text.tokenSteps(link, env)}
        </p>
      ) : (
        <Alert variant="info" data-testid="client-export-not-member">
          {card.notMember}
        </Alert>
      )}
      <CodeSnippet title={card.readPatTitle} code={readEnvCommand(env)} copyLabel={card.readPatCopy} note={card.readPatNote} testId="client-export-read-pat" />
    </div>
  );
}

/**
 * 플랫폼 관리자의 회사 전체 목록. 허용 목록은 모든 테넌트의 모든 멤버에게 열린 직접 연결 항목과 클러스터의 노출 도메인을 담는다.
 * 노출 도메인이 빠진 목록은 사람들의 내보내기를 막으므로 클러스터를 다 읽기 전에는 내려받지 못한다. 패턴으로 적지 못한 것은 이름을 알린다. 독점 관리 파일은 사람들의 서버를
 * 모두 막으므로 펼쳐야 보이는 둘째 선택이다.
 */
function CompanyExport({ items, tenant }: { items: readonly HubItem[]; tenant: string }) {
  const t = useT();
  const text = t.hub.export;
  const clusters = useQuery({ queryKey: CLUSTERS_KEY, queryFn: listClusters });
  const allowlist = companyAllowlist(items, clusters.data ?? []);
  const exclusive = exclusiveFile(items, tenant);
  return (
    <section className="flex flex-col gap-2 border-t border-line pt-4" data-testid="client-export-company">
      <h3 className="text-sm font-semibold text-heading">{text.allowlist}</h3>
      <p className="text-xs text-muted">{text.allowlistHint}</p>
      <ErrorMessage error={clusters.error} testId="client-export-clusters-error" />
      {/* 패턴으로 적을 수 없어 빠진 것은 조용히 버리지 않는다. 관리자가 관리 설정에 직접 넣는다 */}
      {allowlist.skipped.length ? (
        <Alert variant="warn" data-testid="client-export-allowlist-skipped">
          {text.allowlistSkipped(allowlist.skipped.join(", "))}
        </Alert>
      ) : null}
      <CodeSnippet title={ALLOWLIST_KEY} code={allowlist.file} testId="client-export-allowlist" copyTestId="client-export-allowlist-copy" />
      <div className="flex justify-end">
        <Button variant="secondary" data-testid="client-export-allowlist-download" disabled={!clusters.isSuccess} onClick={() => save(allowlist.file, ADMIN_FILE.allowlist)}>
          {text.download}
        </Button>
      </div>
      <details className="flex flex-col gap-2" data-testid="client-export-exclusive">
        <summary className="cursor-pointer text-sm font-semibold text-heading">{text.exclusive}</summary>
        <div className="mt-2 flex flex-col gap-2">
          <Alert variant="warn">{text.exclusiveHint}</Alert>
          <CodeSnippet title={ADMIN_FILE.exclusive.name} code={exclusive} testId="client-export-exclusive-file" copyTestId="client-export-exclusive-copy" />
          <div className="flex justify-end">
            <Button variant="secondary" data-testid="client-export-exclusive-download" onClick={() => save(exclusive, ADMIN_FILE.exclusive)}>
              {text.download}
            </Button>
          </div>
        </div>
      </details>
    </section>
  );
}
