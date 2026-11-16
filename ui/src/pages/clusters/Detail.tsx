import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router";

import { getCluster, patchCluster, patchClusterDefaults, runPreflight } from "@/api/resources";
import type { Cluster, ClusterDefaults, ClusterIngress, Preflight } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { PageHeader } from "@/components/Layout";
import { NotFoundPage } from "@/components/NotFoundPage";
import { PageWidth } from "@/components/PageWidth";
import { ReferencesCard } from "@/components/ReferencesCard";
import { ResourceDeleteDialog } from "@/components/ResourceDeleteDialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { queryGate } from "@/components/QueryGate";
import { useT } from "@/i18n";
import { parseJsonObject } from "@/lib/json";
import { displayName, secondaryName } from "@/lib/names";
import { formatDate } from "@/lib/utils";
import { missingResource } from "@/lib/warningText";

import { useClusterReadOnly } from "./permission";
import { SandboxCard } from "./SandboxCard";

/** JSON 편집기가 다루는 키. ingress 는 아래 IngressCard 가 폼으로 다룬다. */
type JsonDefaults = Omit<ClusterDefaults, "ingress">;

/**
 * home 이 함께 올린 Redis 의 TLS 서버 인증서가 30일 안에 끝나는가 (P61). 문턱은 플랫폼의
 * SHIPPED_REDIS_CERT_WARN 과 같다. 인증서를 모르면 null 이다.
 */
const SHIPPED_REDIS_CERT_WARN_MS = 30 * 24 * 60 * 60 * 1000;

function shippedRedisCertEnd(preflight: Preflight): string | null {
  const end = preflight.shipped_redis_cert_expires_at;
  return end && Date.parse(end) - Date.now() < SHIPPED_REDIS_CERT_WARN_MS ? end : null;
}

/** preflight 의 `missing` 줄이 다는 알려진 접두사. 접두사마다 할 일 한 문장이 사전(clusters.detail.missing)에 있다. */
const MISSING_PREFIXES = ["connect", "crd", "operator", "rbac"] as const;

/** 묶음의 갈래. other 는 알려진 접두사가 없는 줄이다. */
export type MissingPrefix = (typeof MISSING_PREFIXES)[number] | "other";

/**
 * preflight 의 `missing` 을 접두사별로 묶어 "무엇을 하면 되는지" 로 바꾼다.
 *
 * 원문은 `rbac: toolhive.stacklok.dev/mcpservers:create` 꼴이 마흔 줄까지 나오고, ToolHive 를 처음 보는
 * 사람은 여기서 다음 할 일을 알아낼 수 없다. 원문 목록은 접어 두고 갈래마다 한 문장을 앞세운다.
 */
export interface MissingGroup {
  prefix: MissingPrefix;
  items: string[];
}

export function groupMissing(missing: string[]): MissingGroup[] {
  const groups: MissingGroup[] = [];
  const rest: string[] = [];
  for (const line of missing) {
    const prefix = MISSING_PREFIXES.find((known) => line.startsWith(`${known}:`));
    if (!prefix) {
      rest.push(line);
      continue;
    }
    const group = groups.find((g) => g.prefix === prefix);
    const item = line.slice(prefix.length + 1).trim();
    if (group) group.items.push(item);
    else groups.push({ prefix, items: [item] });
  }
  if (rest.length) groups.push({ prefix: "other", items: rest });
  return groups;
}

function defaultsOf(c: Cluster): JsonDefaults {
  return {
    images: c.images,
    namespaces: c.namespaces,
    scheduling: c.scheduling,
    default_redis: c.default_redis,
  };
}

const DEFAULT_KEYS: (keyof JsonDefaults)[] = ["images", "namespaces", "scheduling", "default_redis"];

export function ClusterDetailPage() {
  const t = useT();
  const text = t.clusters.detail;
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const q = useQuery({ queryKey: ["clusters", id], queryFn: () => getCluster(id) });

  const [preflight, setPreflight] = useState<Preflight | null>(null);
  useEffect(() => {
    if (q.data) setPreflight(q.data.status.preflight);
  }, [q.data]);

  const rerun = useMutation({
    mutationFn: () => runPreflight(id),
    onSuccess: async (r) => {
      setPreflight(r.preflight);
      await queryClient.invalidateQueries({ queryKey: ["clusters"] });
    },
  });

  const [deleting, setDeleting] = useState(false);
  const readOnly = useClusterReadOnly();

  // 지워진 클러스터의 북마크를 따라오면 붉은 줄 하나만 남는다. 404 는 나갈 길이 있는 화면으로 받는다
  if (missingResource(q.error, q.data)) return <NotFoundPage resource={text.resource} backTo="/clusters" />;
  const gate = queryGate(q);
  if (gate) return gate;
  const c = q.data!;

  return (
    <PageWidth data-testid="cluster-detail-page" className="flex flex-col gap-4">
      <PageHeader
        title={displayName(c)}
        name={secondaryName(c)}
        testId="page-title"
        actions={
          <>
            <Button
              variant="secondary"
              data-testid="preflight-button"
              disabled={readOnly || rerun.isPending}
              title={readOnly ? t.clusters.readOnly.title : undefined}
              onClick={() => rerun.mutate()}
            >
              {rerun.isPending ? text.rerunning : text.rerun}
            </Button>
            {!c.home && !readOnly ? (
              <Button variant="danger" data-testid="delete-button" onClick={() => setDeleting(true)}>
                {t.common.delete}
              </Button>
            ) : null}
          </>
        }
      />
      {readOnly ? (
        <Alert variant="info" data-testid="readonly-notice">
          {t.clusters.readOnly.notice}
        </Alert>
      ) : null}
      <ResourceDeleteDialog
        resource="cluster"
        target={deleting ? { id: c.id, name: c.name } : null}
        onClose={() => setDeleting(false)}
        onDeleted={() => navigate("/clusters")}
      />
      <ErrorMessage error={rerun.error} testId="preflight-error" />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card data-testid="cluster-summary">
          <CardHeader>
            <CardTitle>{t.detail.summaryFields.title}</CardTitle>
            {c.home ? <Badge variant="info">{t.clusters.list.home}</Badge> : null}
          </CardHeader>
          <CardContent>
            <dl className="grid grid-cols-[10rem_1fr] gap-y-1 text-sm">
              <dt className="text-muted">{t.detail.summaryFields.id}</dt>
              <dd className="font-mono text-xs">{c.id}</dd>
              <dt className="text-muted">{text.labels.connection}</dt>
              <dd>
                {c.connection.mode}
                {c.connection.api_url ? <span className="ml-1 font-mono text-xs">{c.connection.api_url}</span> : null}
                {c.connection.kubecontext ? <span className="ml-1 font-mono text-xs">{c.connection.kubecontext}</span> : null}
              </dd>
              <dt className="text-muted">{text.labels.secret}</dt>
              <dd className="font-mono text-xs">{c.connection.secret_name ?? "-"}</dd>
              <dt className="text-muted">{text.labels.platformUrl}</dt>
              <dd className="font-mono text-xs">{c.platform_internal_url}</dd>
              <dt className="text-muted">{t.detail.summaryFields.updated}</dt>
              <dd>{formatDate(t, c.updated_at)}</dd>
            </dl>
          </CardContent>
        </Card>

        <Card data-testid="preflight-card">
          <CardHeader>
            <CardTitle>{t.clusters.preflight.title}</CardTitle>
            {preflight ? (
              <Badge variant={preflight.ok ? "ok" : "error"} data-testid="preflight-status">
                {preflight.ok ? t.clusters.preflight.ok : t.clusters.preflight.failed}
              </Badge>
            ) : (
              <Badge variant="muted" data-testid="preflight-status">
                {t.clusters.preflight.notRun}
              </Badge>
            )}
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {preflight ? (
              <>
                <dl className="grid grid-cols-[10rem_1fr] gap-y-1 text-sm">
                  <dt className="text-muted">{text.labels.checked}</dt>
                  <dd>{formatDate(t, preflight.checked_at)}</dd>
                  <dt className="text-muted">{t.clusters.preflight.kubernetes}</dt>
                  <dd data-testid="preflight-k8s">{preflight.k8s_version ?? "-"}</dd>
                  <dt className="text-muted">{t.clusters.preflight.toolhive}</dt>
                  <dd data-testid="preflight-toolhive">{preflight.toolhive_version ?? "-"}</dd>
                  <dt className="text-muted">{text.labels.usageMetrics}</dt>
                  <dd data-testid="preflight-usage-metrics">
                    {/* 마지막 preflight 가 오퍼레이터 env 에서 읽은 값이다(Cluster.gates). 켜져 있으면 사용량이 클러스터 밖으로 나간다 (P50 3절) */}
                    {c.gates.usage_metrics === true ? (
                      <span className="text-warn-fg">{text.usageMetrics.on}</span>
                    ) : c.gates.usage_metrics === false ? (
                      text.usageMetrics.off
                    ) : (
                      "-"
                    )}
                  </dd>
                  <dt className="text-muted">{text.labels.networkPolicy}</dt>
                  <dd data-testid="preflight-network-policy">
                    {preflight.network_policy === "enforced"
                      ? text.networkPolicy.enforced(preflight.policy_controller ?? "controller")
                      : preflight.network_policy === "presumed"
                        ? text.networkPolicy.presumed(preflight.policy_controller ?? "k3s")
                        : text.networkPolicy.none}
                  </dd>
                  <dt className="text-muted">{text.labels.secretRights}</dt>
                  <dd data-testid="preflight-secret-rights">
                    <SecretRightsValue rights={preflight.secret_rights} />
                  </dd>
                </dl>
                {preflight.network_policy === "presumed" ? (
                  <Alert variant="info" data-testid="preflight-network-policy-presumed">
                    {text.presumedNote}
                  </Alert>
                ) : null}
                {preflight.network_policy !== "unknown" ? null : (
                  <Alert variant="info" data-testid="preflight-network-policy-warning">
                    {text.noControllerNote}
                  </Alert>
                )}
                {preflight.secret_rights?.cluster_wide ? (
                  <Alert variant="warn" data-testid="preflight-cluster-secret-rights">
                    {text.secretRights.clusterWideNote}
                  </Alert>
                ) : preflight.secret_rights?.cluster_wide === false && preflight.secret_rights.unbound ? (
                  <Alert variant="warn" data-testid="preflight-unbound-secret-rights">
                    {text.secretRights.unboundNote}
                  </Alert>
                ) : null}
                {shippedRedisCertEnd(preflight) ? (
                  <Alert variant="warn" data-testid="preflight-shipped-redis-cert">
                    {text.shippedRedisCertNote(formatDate(t, preflight.shipped_redis_cert_expires_at))}
                  </Alert>
                ) : null}
                {preflight.missing.length ? <MissingList missing={preflight.missing} /> : null}
                {preflight.rbac.length ? <RbacTable rbac={preflight.rbac} /> : null}
              </>
            ) : (
              <p className="text-sm text-muted">{text.notRun}</p>
            )}
          </CardContent>
        </Card>
      </div>

      <SandboxCard cluster={c} readOnly={readOnly} />

      <ReferencesCard
        resource="cluster"
        id={c.id}
        title={text.referencesTitle}
        hint={text.referencesHint}
        testId="cluster-references-card"
      />

      <IngressCard cluster={c} />
      <MetadataCard cluster={c} />
      <DefaultsCard cluster={c} />
    </PageWidth>
  );
}

/**
 * tenant Secrets 줄 (P63). 역할에 Secret 동사가 없으면 그것을, 아니면 RoleBinding 검사의 결과를 보인다. 네임스페이스 목록을
 * 얻지 못한 점검은 바인딩을 보지 않았으므로 "-" 다. 없는 곳의 이름은 cluster:write 에게만 온다.
 */
function SecretRightsValue({ rights }: { rights: Preflight["secret_rights"] }) {
  const text = useT().clusters.detail.secretRights;
  if (!rights) return <>-</>;
  if (rights.role === false) return <span className="text-warn-fg">{text.roleWithoutSecrets}</span>;
  if (rights.namespaces === null) return <>-</>;
  if (rights.namespaces === 0) return <>{text.noNamespaces}</>;
  if (rights.unbound === 0) return <>{text.bound(rights.namespaces)}</>;
  return (
    <span className="text-warn-fg">
      {text.unbound(rights.unbound, rights.namespaces)}
      {rights.unbound_namespaces.length ? (
        <span className="font-mono text-xs">: {rights.unbound_namespaces.join(", ")}</span>
      ) : null}
    </span>
  );
}

/** preflight 가 못 넘은 것을 접두사별로 한 문장씩 적고, 원문 목록은 접어 둔다. */
function MissingList({ missing }: { missing: string[] }) {
  const t = useT();
  const text = t.clusters.detail;
  return (
    <Alert variant="error" data-testid="preflight-missing">
      <div className="flex flex-col gap-2">
        {groupMissing(missing).map((g) => (
          <div key={g.prefix}>
            <div data-testid={`preflight-advice-${g.prefix}`}>
              {text.missing[g.prefix]} {g.items.length > 1 ? text.missingCount(g.items.length) : null}
            </div>
            <details className="mt-1">
              <summary className="cursor-pointer text-xs opacity-80">{text.rawLines}</summary>
              <ul className="mt-1 list-disc pl-4 font-mono text-xs">
                {g.items.map((it) => (
                  <li key={it}>{it}</li>
                ))}
              </ul>
            </details>
          </div>
        ))}
      </div>
    </Alert>
  );
}

/** RBAC 검사 결과. 마흔 줄이 넘을 수 있어 "막힌 것만" 을 먼저 볼 수 있게 한다. */
function RbacTable({ rbac }: { rbac: Preflight["rbac"] }) {
  const t = useT();
  const text = t.clusters.detail.rbac;
  const denied = rbac.filter((r) => !r.allowed);
  const [deniedOnly, setDeniedOnly] = useState(denied.length > 0);
  const rows = deniedOnly ? denied : rbac;
  return (
    <div className="flex flex-col gap-2">
      <label className="flex items-center gap-2 text-xs text-muted">
        <input type="checkbox" data-testid="rbac-denied-only" checked={deniedOnly} onChange={(e) => setDeniedOnly(e.target.checked)} />
        {text.deniedOnly(denied.length, rbac.length)}
      </label>
      <Table data-testid="preflight-rbac">
        <THead>
          <tr>
            {/* 오른쪽 카드는 좁아서 머리글이 글자마다 줄을 바꾼다. 머리글은 한 줄로 둔다 */}
            <TH className="whitespace-nowrap">{text.columns.group}</TH>
            <TH className="whitespace-nowrap">{text.columns.resource}</TH>
            <TH className="whitespace-nowrap">{text.columns.verb}</TH>
            <TH className="whitespace-nowrap">{text.columns.namespace}</TH>
            <TH className="whitespace-nowrap">{text.columns.allowed}</TH>
          </tr>
        </THead>
        <TBody>
          {rows.map((r, i) => (
            <TR key={i}>
              <TD className="font-mono text-xs">{r.group || text.coreGroup}</TD>
              <TD className="font-mono text-xs">{r.resource}</TD>
              <TD>{r.verb}</TD>
              <TD>{r.namespace ?? "*"}</TD>
              <TD>
                <Badge variant={r.allowed ? "ok" : "error"}>{r.allowed ? t.detail.summaryFields.yes : t.detail.summaryFields.no}</Badge>
              </TD>
            </TR>
          ))}
        </TBody>
      </Table>
    </div>
  );
}

// -- 노출 기본값 (ClusterIngress, resource-model 9·11절) ----------------------------------------

/** 폼 문자열 상태. 목록은 쉼표, annotation 은 `key: value` 한 줄씩이다. */
interface IngressForm {
  enabled: boolean;
  allow_unauthenticated: boolean;
  class_name: string;
  allowed_classes: string;
  domain: string;
  tls: ClusterIngress["tls"];
  default_issuer: string;
  annotations: string;
  controller_namespace: string;
  controller_ip_blocks: string;
  reserved_hosts: string;
}

function listText(xs: string[] | null | undefined): string {
  return (xs ?? []).join(", ");
}

function parseList(text: string): string[] {
  return text
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export function annotationsText(a: Record<string, string> | null | undefined): string {
  return Object.entries(a ?? {})
    .map(([k, v]) => `${k}: ${v}`)
    .join("\n");
}

/** `key: value` 줄을 annotation 맵으로. 값의 따옴표는 뗀다. 콜론이 없는 줄은 badLine 으로 돌려주고 문장은 그릴 때 만든다. */
export function parseAnnotations(text: string): { value: Record<string, string>; badLine?: string } {
  const out: Record<string, string> = {};
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const i = line.indexOf(":");
    if (i <= 0) return { value: out, badLine: line };
    const key = line.slice(0, i).trim();
    const value = line
      .slice(i + 1)
      .trim()
      .replace(/^"(.*)"$/, "$1");
    out[key] = value;
  }
  return { value: out };
}

function ingressFormOf(ing: ClusterIngress): IngressForm {
  return {
    enabled: ing.enabled,
    allow_unauthenticated: ing.allow_unauthenticated,
    class_name: ing.class_name ?? "",
    allowed_classes: listText(ing.allowed_classes),
    domain: ing.domain ?? "",
    tls: ing.tls,
    default_issuer: ing.default_issuer ?? "",
    annotations: annotationsText(ing.annotations),
    controller_namespace: ing.controller_namespace ?? "",
    controller_ip_blocks: listText(ing.controller_ip_blocks),
    reserved_hosts: listText(ing.reserved_hosts),
  };
}

export function ingressOfForm(f: IngressForm): { value: ClusterIngress; badLine?: string } {
  const ann = parseAnnotations(f.annotations);
  if (ann.badLine !== undefined) return { value: {} as ClusterIngress, badLine: ann.badLine };
  return {
    value: {
      enabled: f.enabled,
      allow_unauthenticated: f.allow_unauthenticated,
      class_name: f.class_name.trim() || null,
      allowed_classes: parseList(f.allowed_classes),
      domain: f.domain.trim() || null,
      tls: f.tls,
      default_issuer: f.default_issuer.trim() || null,
      annotations: Object.keys(ann.value).length ? ann.value : null,
      controller_namespace: f.controller_namespace.trim() || null,
      controller_ip_blocks: parseList(f.controller_ip_blocks),
      reserved_hosts: parseList(f.reserved_hosts),
    },
  };
}

/** streamable-http 세션이 오래 열려 있으므로 nginx 계열은 프록시 타임아웃을 늘려야 한다. */
export const NGINX_ANNOTATION_EXAMPLE = [
  "nginx.ingress.kubernetes.io/proxy-read-timeout: 3600",
  "nginx.ingress.kubernetes.io/proxy-send-timeout: 3600",
  "nginx.ingress.kubernetes.io/proxy-body-size: 16m",
  "nginx.ingress.kubernetes.io/proxy-buffering: off",
].join("\n");

/** 노출 기본값과 허용 범위를 폼으로 편집한다. 배포된 것은 자기 exposure 스냅샷만 보므로 돌고 있는 것은 안 바뀐다. */
/** 환경(DAEGAM_HOME_*)이 기동마다 덮어쓰는 필드 경로(home 만, D54). 화면에서는 고칠 수 없다. */
function envManaged(cluster: Cluster, prefix: string): string[] {
  return (cluster.env_managed ?? []).filter((p) => p.startsWith(`${prefix}.`) || p === prefix);
}

function EnvManagedNotice({ fields, testId }: { fields: string[]; testId: string }) {
  const t = useT();
  if (!fields.length) return null;
  return (
    <Alert variant="info" data-testid={testId}>
      {t.clusters.detail.envManaged(fields.join(", "))}
    </Alert>
  );
}

function IngressCard({ cluster }: { cluster: Cluster }) {
  const t = useT();
  const text = t.clusters.ingress;
  const queryClient = useQueryClient();
  const readOnly = useClusterReadOnly();
  const managed = envManaged(cluster, "ingress");
  const locked = (field: keyof ClusterIngress) => managed.includes(`ingress.${field}`);
  const [form, setForm] = useState<IngressForm>(() => ingressFormOf(cluster.ingress));
  // 콜론이 없는 annotation 줄. 문장은 그릴 때 만든다
  const [badLine, setBadLine] = useState<string | null>(null);
  useEffect(() => {
    setForm(ingressFormOf(cluster.ingress));
    setBadLine(null);
  }, [cluster]);

  const save = useMutation({
    mutationFn: (ingress: ClusterIngress) => patchClusterDefaults(cluster.id, { ingress }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["clusters"] }),
  });

  function set<K extends keyof IngressForm>(key: K, v: IngressForm[K]) {
    setForm((f) => ({ ...f, [key]: v }));
  }

  function submit() {
    const r = ingressOfForm(form);
    if (r.badLine !== undefined) {
      setBadLine(r.badLine);
      return;
    }
    setBadLine(null);
    save.mutate(r.value);
  }

  return (
    <Card data-testid="ingress-card">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        <span className="text-xs text-muted">{text.hint}</span>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <EnvManagedNotice fields={managed} testId="ingress-home-notice" />
        <div className="flex flex-wrap gap-6">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" data-testid="ci-enabled" disabled={locked("enabled")} checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} />
            {text.enabled}
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" data-testid="ci-allow-unauth" disabled={locked("allow_unauthenticated")} checked={form.allow_unauthenticated} onChange={(e) => set("allow_unauthenticated", e.target.checked)} />
            {text.allowUnauthenticated}
            <span className="text-xs text-muted">{text.allowUnauthenticatedNote}</span>
          </label>
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          <Field label={text.labels.ingressClass} hint={text.classHint} htmlFor="ci-class">
            <Input id="ci-class" data-testid="ci-class" disabled={locked("class_name")} placeholder={text.classPlaceholder} value={form.class_name} onChange={(e) => set("class_name", e.target.value)} />
          </Field>
          <Field label={text.allowedClasses} htmlFor="ci-allowed-classes" hint={text.allowedClassesHint}>
            <Input id="ci-allowed-classes" data-testid="ci-allowed-classes" disabled={locked("allowed_classes")} value={form.allowed_classes} onChange={(e) => set("allowed_classes", e.target.value)} />
          </Field>
          <Field label={text.domain} htmlFor="ci-domain" hint={text.domainHint}>
            <Input id="ci-domain" data-testid="ci-domain" disabled={locked("domain")} placeholder="apps.example.com" value={form.domain} onChange={(e) => set("domain", e.target.value)} />
          </Field>
          <Field label={text.tls} hint={text.tlsHint} htmlFor="ci-tls">
            <Select id="ci-tls" data-testid="ci-tls" disabled={locked("tls")} value={form.tls} onChange={(e) => set("tls", e.target.value as ClusterIngress["tls"])}>
              <option value="none">{text.tlsNone}</option>
              <option value="secret">{text.tlsSecret}</option>
              <option value="cert-manager">{text.tlsCertManager}</option>
              <option value="edge">{text.tlsEdge}</option>
            </Select>
          </Field>
          <Field label={text.issuer} htmlFor="ci-issuer" hint={text.issuerHint}>
            <Input id="ci-issuer" data-testid="ci-issuer" disabled={locked("default_issuer")} value={form.default_issuer} onChange={(e) => set("default_issuer", e.target.value)} />
          </Field>
          <Field label={text.controllerNamespace} htmlFor="ci-controller-ns" hint={text.controllerNamespaceHint}>
            <Input id="ci-controller-ns" data-testid="ci-controller-ns" disabled={locked("controller_namespace")} placeholder={text.controllerNamespacePlaceholder} value={form.controller_namespace} onChange={(e) => set("controller_namespace", e.target.value)} />
          </Field>
          <Field label={text.ipBlocks} htmlFor="ci-ip-blocks" hint={text.ipBlocksHint}>
            <Input id="ci-ip-blocks" data-testid="ci-ip-blocks" disabled={locked("controller_ip_blocks")} placeholder="10.0.0.0/8" value={form.controller_ip_blocks} onChange={(e) => set("controller_ip_blocks", e.target.value)} />
          </Field>
          <Field label={text.reservedHosts} htmlFor="ci-reserved-hosts" hint={text.reservedHostsHint}>
            <Input id="ci-reserved-hosts" data-testid="ci-reserved-hosts" disabled={locked("reserved_hosts")} value={form.reserved_hosts} onChange={(e) => set("reserved_hosts", e.target.value)} />
          </Field>
        </div>
        <Field label={text.annotations} htmlFor="ci-annotations" hint={text.annotationsHint}>
          <Textarea id="ci-annotations" data-testid="ci-annotations" disabled={locked("annotations")} rows={4} value={form.annotations} onChange={(e) => set("annotations", e.target.value)} />
        </Field>
        <div className="rounded-chip border border-line-soft bg-surface px-3 py-2 text-xs" data-testid="ingress-annotation-example">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-muted">{text.example}</span>
            <Button size="sm" variant="ghost" data-testid="ingress-example-apply" onClick={() => set("annotations", form.annotations.trim() ? `${form.annotations.trim()}\n${NGINX_ANNOTATION_EXAMPLE}` : NGINX_ANNOTATION_EXAMPLE)}>
              {text.applyExample}
            </Button>
          </div>
          <pre className="whitespace-pre-wrap font-mono">{NGINX_ANNOTATION_EXAMPLE}</pre>
        </div>
        {badLine !== null ? (
          <Alert variant="error" data-testid="ingress-form-error">
            {text.annotationLine(badLine)}
          </Alert>
        ) : null}
        <ErrorMessage error={save.error} testId="ingress-error" />
        {save.isSuccess ? <Alert variant="ok" data-testid="ingress-saved">{t.shared.saved}</Alert> : null}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setForm(ingressFormOf(cluster.ingress))}>
            {t.shared.revert}
          </Button>
          <Button data-testid="ingress-save" disabled={readOnly || save.isPending} title={readOnly ? t.clusters.readOnly.title : undefined} onClick={submit}>
            {save.isPending ? t.shared.saving : text.save}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * 인스턴스 메타데이터 차단 (T75). 켜면 다음 재적용 틱이 테넌트 네임스페이스마다 egress 정책을 두고, 끄면 지운다. 정책 컨트롤러가
 * 확인된 것이 아니면 서버가 정책을 두지 않고 경고(metadata_block_unsupported)를 싣는다. home 은 환경(DAEGAM_HOME_BLOCK_METADATA)이 정할 수 있다.
 */
function MetadataCard({ cluster }: { cluster: Cluster }) {
  const t = useT();
  const text = t.clusters.metadata;
  const queryClient = useQueryClient();
  const readOnly = useClusterReadOnly();
  const managed = envManaged(cluster, "block_metadata");
  const [on, setOn] = useState(cluster.block_metadata);
  useEffect(() => setOn(cluster.block_metadata), [cluster]);

  const save = useMutation({
    mutationFn: (block_metadata: boolean) => patchCluster(cluster.id, { block_metadata }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["clusters"] }),
  });
  const unsupported = cluster.warnings.some((w) => w.startsWith("metadata_block_unsupported"));
  // 관리자의 egress 정책이 있어 차단을 두지 않은 네임스페이스. 스윕이 Cluster 에 적는다
  const skipped = cluster.warnings.some((w) => w.startsWith("metadata_block_skipped")) ? (cluster.status.metadata_skipped ?? []) : [];
  const locked = managed.length > 0;

  return (
    <Card data-testid="metadata-card">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <EnvManagedNotice fields={managed} testId="metadata-home-notice" />
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" data-testid="cm-block-metadata" disabled={locked} checked={on} onChange={(e) => setOn(e.target.checked)} />
          {text.enabled}
        </label>
        <p className="text-xs text-muted" data-testid="metadata-hint">
          {text.hint}
        </p>
        {unsupported ? (
          <Alert variant="warn" data-testid="metadata-unsupported">
            {t.warnings.codes.metadata_block_unsupported.text}
          </Alert>
        ) : null}
        {skipped.length ? (
          <Alert variant="warn" data-testid="metadata-skipped">
            {t.warnings.codes.metadata_block_skipped.text.replace("{namespaces}", skipped.join(", "))}
          </Alert>
        ) : null}
        <ErrorMessage error={save.error} testId="metadata-error" />
        {save.isSuccess ? <Alert variant="ok" data-testid="metadata-saved">{t.shared.saved}</Alert> : null}
        <div className="flex justify-end">
          <Button
            data-testid="metadata-save"
            disabled={readOnly || locked || save.isPending}
            title={readOnly ? t.clusters.readOnly.title : undefined}
            onClick={() => save.mutate(on)}
          >
            {save.isPending ? t.shared.saving : text.save}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

/** 배포 기본값(images, namespaces, scheduling, default_redis) 을 JSON 으로 편집한다. */
function DefaultsCard({ cluster }: { cluster: Cluster }) {
  const t = useT();
  const queryClient = useQueryClient();
  const readOnly = useClusterReadOnly();
  const [json, setJson] = useState(() => JSON.stringify(defaultsOf(cluster), null, 2));
  const [parseError, setParseError] = useState<string | null>(null);
  // 바뀐 키가 없어 보내지 않았는가. 문장은 그릴 때 만든다
  const [noChange, setNoChange] = useState(false);
  useEffect(() => {
    setJson(JSON.stringify(defaultsOf(cluster), null, 2));
    setParseError(null);
    setNoChange(false);
  }, [cluster]);

  const save = useMutation({
    mutationFn: (defaults: Partial<ClusterDefaults>) => patchClusterDefaults(cluster.id, defaults),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["clusters"] }),
  });

  function submit() {
    const { value, error } = parseJsonObject(json);
    // parseJsonObject 는 빈 칸을 "값 없음" 으로 넘긴다. 여기서는 보낼 것이 없으므로 잘못된 입력이다
    if (!value) {
      setParseError(error ?? "invalid JSON: empty");
      return;
    }
    const unknown = Object.keys(value).filter((k) => !DEFAULT_KEYS.includes(k as keyof JsonDefaults));
    if (unknown.length) {
      setParseError(`unknown keys: ${unknown.join(", ")} (allowed: ${DEFAULT_KEYS.join(", ")})`);
      return;
    }
    setParseError(null);
    // 바뀐 키만 보낸다
    const current = defaultsOf(cluster);
    const body: Partial<Record<keyof JsonDefaults, unknown>> = {};
    for (const k of DEFAULT_KEYS) {
      if (k in value && JSON.stringify(value[k]) !== JSON.stringify(current[k])) body[k] = value[k];
    }
    if (!Object.keys(body).length) {
      setNoChange(true);
      return;
    }
    setNoChange(false);
    save.mutate(body as Partial<ClusterDefaults>);
  }

  return (
    <Card data-testid="defaults-card">
      <CardHeader>
        <CardTitle>{t.clusters.defaults.title}</CardTitle>
        <span className="text-xs text-muted">{DEFAULT_KEYS.join(" · ")}</span>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <EnvManagedNotice
          fields={(cluster.env_managed ?? []).filter(
            (p) => !p.startsWith("ingress.") && p !== "platform_internal_url" && p !== "block_metadata",
          )}
          testId="defaults-env-notice"
        />
        <Textarea data-testid="defaults-json" rows={18} value={json} onChange={(e) => setJson(e.target.value)} />
        <p className="text-xs text-muted" data-testid="defaults-redis-tls-hint">
          {t.clusters.defaults.redisTlsHint}
        </p>
        {parseError ? (
          <Alert variant="error" data-testid="defaults-parse-error">
            {parseError}
          </Alert>
        ) : null}
        <ErrorMessage error={save.error} testId="defaults-error" />
        {noChange ? (
          <Alert variant="info" data-testid="defaults-notice">
            {t.errors.noChange}
          </Alert>
        ) : null}
        {save.isSuccess && !noChange ? <Alert variant="ok" data-testid="defaults-saved">{t.shared.saved}</Alert> : null}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setJson(JSON.stringify(defaultsOf(cluster), null, 2))}>
            {t.shared.revert}
          </Button>
          <Button data-testid="defaults-save" disabled={readOnly || save.isPending} title={readOnly ? t.clusters.readOnly.title : undefined} onClick={submit}>
            {save.isPending ? t.shared.saving : t.clusters.defaults.save}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
