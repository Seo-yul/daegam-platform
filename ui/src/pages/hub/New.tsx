import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router";

import { createHubItem, getHubCatalog, listClusters, type HubCreate } from "@/api/resources";
import type { HubAccess, HubCatalogServer } from "@/api/types";
import { AccessPolicyEditor } from "@/components/AccessPolicyEditor";
import { ErrorMessage } from "@/components/ErrorMessage";
import { PageHeader } from "@/components/Layout";
import { NameFields, isNameFieldError, nameFieldErrors, type NameValue } from "@/components/NameFields";
import { PageWidth } from "@/components/PageWidth";
import { queryGate } from "@/components/QueryGate";
import { ContainerFields } from "@/components/hub/ContainerFields";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent } from "@/components/ui/card";
import { CodeSnippet } from "@/components/ui/code-snippet";
import { Field, Input, Select } from "@/components/ui/input";
import { useT } from "@/i18n";
import { useAuthState } from "@/lib/auth";
import { accessForPublish, canShare, emptyAccess } from "@/lib/hubAccess";
import { EMPTY_CONTAINER, catalogPrefill, catalogStillApplies, containerBody, mirrorInputs, type ContainerForm } from "@/lib/hubCatalog";
import { CLUSTERS_KEY, HUB_CATALOG_KEY } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";

type Kind = "entry" | "image";
/** entry 항목의 인증 (P86 6.1절). oauth 는 P86b 가 더한다. */
type Auth = "none" | "header";

/**
 * 외부 MCP 등록. `?catalog=<이름>` 이면 카탈로그 스냅숏의 그 서버로 폼을 채운다 (P55 8절). 이름이 스냅숏에 없으면(스냅숏을 바꾼
 * 뒤의 옛 북마크) 그 사실을 한 줄로 말하고 빈 폼이다.
 */
export function HubNewPage() {
  const [params] = useSearchParams();
  const name = params.get("catalog");
  const catalog = useQuery({ queryKey: HUB_CATALOG_KEY, queryFn: getHubCatalog, enabled: Boolean(name) });
  const gate = name ? queryGate(catalog, { testId: "hub-catalog-error" }) : null;
  if (gate) return <PageWidth data-testid="hub-new-page">{gate}</PageWidth>;
  const server = name ? (catalog.data?.servers.find((s) => s.name === name) ?? null) : null;
  // 서버가 바뀌면 폼을 그 값으로 다시 연다
  return <HubForm key={server?.name ?? ""} server={server} missing={name && !server ? name : null} admission={catalog.data?.admission ?? false} />;
}

/** 외부 MCP 등록 폼. entry 는 url(+transport), image 는 image/port/tools_filter 와 컨테이너 칸. 접근 정책은 접힌 편집기다 (D23). */
function HubForm({ server, missing, admission }: { server: HubCatalogServer | null; missing: string | null; admission: boolean }) {
  const t = useT();
  const text = t.hub.new;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const clusters = useQuery({ queryKey: CLUSTERS_KEY, queryFn: listClusters });
  const tenant = useTenant();
  const { me } = useAuthState();
  // 폼은 서버 이름으로 다시 열리므로(key) 렌더마다 같은 값이다
  const prefill = server ? catalogPrefill(server) : null;

  const [names, setNames] = useState<NameValue>({ displayName: prefill?.displayName ?? "", name: prefill?.name ?? "" });
  const [kind, setKind] = useState<Kind>(prefill?.kind ?? "entry");
  const [clusterId, setClusterId] = useState("");
  const [url, setUrl] = useState(prefill?.url ?? "");
  const [transport, setTransport] = useState(prefill?.transport ?? "");
  // 외부 서버(entry)는 SSE 를 받지 않는다(outside_sse_unsupported). image 로 sse 를 고른 뒤 바꾸면 기본값으로 돌린다
  const changeKind = (next: Kind) => {
    setKind(next);
    if (next !== "image" && transport === "sse") setTransport("");
  };
  const [image, setImage] = useState(prefill?.image ?? "");
  const [port, setPort] = useState(prefill?.port ?? "");
  const [toolsFilter, setToolsFilter] = useState(prefill?.toolsFilter ?? "");
  const [container, setContainer] = useState<ContainerForm>(prefill?.container ?? EMPTY_CONTAINER);
  const [access, setAccess] = useState<HubAccess>(emptyAccess());
  const [auth, setAuth] = useState<Auth>(prefill?.headerName ? "header" : "none");
  const [headerName, setHeaderName] = useState(prefill?.headerName ?? "");
  const [headerValue, setHeaderValue] = useState("");

  // 카탈로그 항목은 서버가 받는 꼴일 때만 그 서버의 이름을 싣는다 (5·7절). 사람이 주소, 전송, 헤더, 이미지를 바꾸면 그냥 등록이다
  const fromCatalog = prefill !== null && catalogStillApplies(prefill, { kind, image, url, transport, auth, headerName });
  // stdio 컨테이너는 포트를 받지 않는다(422 invalid_value)
  const stdio = kind === "image" && container.stdio;
  const changeContainer = (next: ContainerForm) => {
    setContainer(next);
    if (next.stdio) setPort("");
  };
  // Secret env 가 있는 항목은 다른 테넌트에 열 수 없다(422 secret_not_shareable)
  const hasSecretEnv = kind === "image" && container.secretEnv.some((e) => e.name.trim());

  const create = useMutation({
    mutationFn: () => {
      const body: HubCreate = { name: names.name.trim(), kind, cluster_id: clusterId || null };
      if (names.displayName.trim()) body.display_name = names.displayName.trim();
      const policy = accessForPublish(access);
      if (policy) body.access = policy;
      if (kind === "image") {
        body.image = image.trim();
        if (port && !stdio) body.port = Number(port);
        if (transport) body.transport = transport as HubCreate["transport"];
        const filter = toolsFilter
          .split(",")
          .map((name) => name.trim())
          .filter(Boolean);
        if (filter.length) body.tools_filter = filter;
        Object.assign(body, containerBody(container));
      } else {
        body.url = url.trim();
        if (transport) body.transport = transport as HubCreate["transport"];
        if (kind === "entry" && auth === "header") body.auth = { kind: "header", header_name: headerName.trim(), header_value: headerValue };
      }
      if (fromCatalog && prefill?.catalog) body.catalog = prefill.catalog;
      return createHubItem(body);
    },
    onSuccess: async (item) => {
      await queryClient.invalidateQueries({ queryKey: ["hub"] });
      navigate(`/hub/${item.id}`);
    },
  });

  return (
    <PageWidth data-testid="hub-new-page">
      <PageHeader title={text.title} testId="page-title" />
      {missing ? (
        <Alert variant="warn" className="mb-3 max-w-xl" data-testid="hub-catalog-missing">
          {t.hub.catalog.noMatch(missing)}
        </Alert>
      ) : null}
      {server && fromCatalog ? <CatalogNotice server={server} admission={admission} platformAdmin={me?.is_platform_admin ?? false} /> : null}
      <Card className="max-w-xl">
        <CardContent>
          <form
            className="flex flex-col gap-3"
            data-testid="hub-form"
            onSubmit={(e) => {
              e.preventDefault();
              create.mutate();
            }}
          >
            {/* image 는 MCPServer 를 `hub-<name>` 으로 만든다. entry 는 URL 만 등록하므로 미리보기가 없다 */}
            <NameFields testId="hub" placeholders={text.placeholders} value={names} onChange={setNames} preview={kind === "entry" ? undefined : { prefix: "hub" }} errors={nameFieldErrors(create.error)} />
            <div className="grid grid-cols-2 gap-3">
              <Field label={text.kind} htmlFor="hub-kind" hint={text.kindHint}>
                <Select id="hub-kind" data-testid="hub-kind" value={kind} onChange={(e) => changeKind(e.target.value as Kind)}>
                  <option value="entry">{text.kinds.entry}</option>
                  <option value="image">{text.kinds.image}</option>
                </Select>
              </Field>
              <Field label={text.cluster} htmlFor="hub-cluster" hint={t.shared.deployDialog.clusterHint}>
                <Select id="hub-cluster" data-testid="hub-cluster" value={clusterId} onChange={(e) => setClusterId(e.target.value)}>
                  <option value="">{t.shared.tenantDefault}</option>
                  {clusters.data?.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>

            {kind !== "image" ? (
              <Field label={text.url} hint={text.urlHint} htmlFor="hub-url">
                <Input id="hub-url" data-testid="hub-url" required placeholder="https://mcp.example.com/mcp" value={url} onChange={(e) => setUrl(e.target.value)} />
              </Field>
            ) : (
              <>
                <Field label={text.image} hint={text.imageHint} htmlFor="hub-image">
                  <Input id="hub-image" data-testid="hub-image" required placeholder="ghcr.io/acme/some-mcp:1.0" value={image} onChange={(e) => setImage(e.target.value)} />
                </Field>
                <Field label={text.port} htmlFor="hub-port" hint={text.portHint}>
                  <Input id="hub-port" data-testid="hub-port" type="number" min={1} max={65535} disabled={stdio} value={port} onChange={(e) => setPort(e.target.value)} />
                </Field>
                <Field label={text.toolsFilter} htmlFor="hub-tools-filter" hint={text.toolsFilterHint}>
                  <Input id="hub-tools-filter" data-testid="hub-tools-filter" placeholder={text.toolsFilterPlaceholder} value={toolsFilter} onChange={(e) => setToolsFilter(e.target.value)} />
                </Field>
                <ContainerFields value={container} onChange={changeContainer} server={server} />
              </>
            )}
            {kind === "entry" ? (
              <>
                <Field label={text.labels.auth} htmlFor="hub-auth" hint={text.authHint}>
                  <Select id="hub-auth" data-testid="hub-auth" value={auth} onChange={(e) => setAuth(e.target.value as Auth)}>
                    <option value="none">{text.auths.none}</option>
                    <option value="header">{text.auths.header}</option>
                  </Select>
                </Field>
                {auth === "header" ? (
                  <div className="grid grid-cols-2 gap-3">
                    <Field label={text.labels.headerName} htmlFor="hub-header-name">
                      <Input id="hub-header-name" data-testid="hub-header-name" autoComplete="off" required placeholder={text.headerNamePlaceholder} value={headerName} onChange={(e) => setHeaderName(e.target.value)} />
                    </Field>
                    <Field label={text.labels.headerValue} htmlFor="hub-header-value" hint={text.headerValueHint}>
                      <Input id="hub-header-value" data-testid="hub-header-value" type="password" autoComplete="new-password" required value={headerValue} onChange={(e) => setHeaderValue(e.target.value)} />
                    </Field>
                  </div>
                ) : null}
              </>
            ) : null}
            <Field label={text.transport} htmlFor="hub-transport" hint={kind === "entry" ? text.transportHint.entry : text.transportHint.other}>
              <Select id="hub-transport" data-testid="hub-transport" required={kind === "entry"} value={transport} onChange={(e) => setTransport(e.target.value)}>
                <option value="">{t.shared.defaultValue}</option>
                <option value="streamable-http">{text.transports["streamable-http"]}</option>
                {/* sse 는 image 항목의 컨테이너만 고른다. SSE 로만 말하는 외부 서버(entry)는 서버가 거절한다(outside_sse_unsupported) */}
                {kind === "image" ? <option value="sse">{text.transports.sse}</option> : null}
              </Select>
            </Field>

            {/* 비우면 게시한 테넌트 안의 기본 정책이다. 다른 테넌트에 열기(hub:share)는 platform-admin 만 */}
            <AccessPolicyEditor value={access} onChange={setAccess} canShare={canShare(me, tenant, "hub:share")} crossTenant={!hasSecretEnv} collapsible testId="access" />

            {!isNameFieldError(create.error) ? <ErrorMessage error={create.error} testId="save-error" /> : null}
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => navigate("/hub")}>
                {t.common.cancel}
              </Button>
              <Button type="submit" data-testid="save-button" disabled={create.isPending}>
                {create.isPending ? text.registering : text.register}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </PageWidth>
  );
}

/**
 * 카탈로그에서 연 폼의 머리 (P55 8절). 이미지 서버면 허브 이미지 설정이 꺼졌을 때 그 까닭을, 켜졌으면 플랫폼 관리자에게 미러 작업의
 * 입력을 복사할 수 있게, 다른 멤버에게 미러는 플랫폼 관리자가 한다는 한 줄과 인자에 채울 값이 있는 서버는 플랫폼 관리자만 등록한다는
 * 줄을 보인다. 플랫폼은 등록 전에 미러를 찾아보지 않는다.
 */
function CatalogNotice({ server, admission, platformAdmin }: { server: HubCatalogServer; admission: boolean; platformAdmin: boolean }) {
  const t = useT();
  const text = t.hub.new;
  const inputs = mirrorInputs(server);
  return (
    <div className="mb-3 flex max-w-xl flex-col gap-2" data-testid="hub-catalog-notice">
      <Alert variant="info" data-testid="hub-from-catalog">
        {text.fromCatalog(server.title)}
      </Alert>
      {server.kind !== "image" ? null : !admission ? (
        <Alert variant="warn" data-testid="hub-admission-off">
          {t.hub.catalog.admissionOff}
        </Alert>
      ) : platformAdmin && inputs ? (
        <Alert variant="info" data-testid="hub-mirror-admin">
          {text.mirrorAdmin}
          <div className="mt-2">
            <CodeSnippet code={inputs} copyLabel={text.mirrorCopy} testId="hub-mirror-inputs" copyTestId="hub-mirror-copy" />
          </div>
        </Alert>
      ) : (
        <Alert variant="info" data-testid="hub-mirror-member">
          {text.mirrorMember}
        </Alert>
      )}
      {server.args_variables && !platformAdmin ? (
        <Alert variant="warn" data-testid="hub-catalog-admin-only">
          {t.errors.codes.catalog_server_admin_only}
        </Alert>
      ) : null}
    </div>
  );
}
