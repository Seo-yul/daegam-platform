import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router";

import {
  getDataService,
  getDataServicePlainConsumers,
  patchDataService,
  verifyDataService,
  type DataServicePatch,
} from "@/api/resources";
import type { DataServiceVerifyResult, PlainRedisConsumer } from "@/api/types";
import { ClusterLink } from "@/components/ClusterLink";
import { ErrorMessage } from "@/components/ErrorMessage";
import { PageHeader } from "@/components/Layout";
import { NameFields, isNameFieldError, nameFieldErrors, type NameValue } from "@/components/NameFields";
import { NotFoundPage } from "@/components/NotFoundPage";
import { PageWidth } from "@/components/PageWidth";
import { ReferencesCard } from "@/components/ReferencesCard";
import { ResourceDeleteDialog } from "@/components/ResourceDeleteDialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/input";
import { type Messages, useT } from "@/i18n";
import { capabilityNames, redisVerify, redisVerifyText } from "@/lib/dataServices";
import { displayName, secondaryName } from "@/lib/names";
import { queryGate } from "@/components/QueryGate";
import { useTenant } from "@/lib/tenant";
import { formatDate } from "@/lib/utils";
import { missingResource } from "@/lib/warningText";

import { ConnectionFields, connectionValues, type ConnectionValue } from "./ConnectionFields";
import { NO_TLS, TlsFields, tlsBody, type TlsValue } from "./TlsFields";
import { capabilityBadge, capabilityNote, verifyErrorText } from "./verifyText";

export function DataServiceDetailPage() {
  const t = useT();
  const text = t.dataServices.detail;
  const { id = "" } = useParams();
  const tenant = useTenant();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const q = useQuery({ queryKey: ["data-services", tenant, id], queryFn: () => getDataService(id) });
  const hasTls = q.data?.kind === "redis" && q.data.tls != null;
  const plain = useQuery({
    queryKey: ["data-services", tenant, id, "plain-consumers"],
    queryFn: () => getDataServicePlainConsumers(id),
    enabled: hasTls,
  });

  const [names, setNames] = useState<NameValue>({ displayName: "", name: "" });
  const [conn, setConn] = useState<ConnectionValue>({ host: "", port: "", database: "", db: "", username: "" });
  const [password, setPassword] = useState("");
  const [tls, setTls] = useState<TlsValue>(NO_TLS);
  // 바뀐 것이 없어 보내지 않았는가. 문장은 그릴 때 만든다
  const [noChange, setNoChange] = useState(false);
  const [verifyResult, setVerifyResult] = useState<DataServiceVerifyResult | null>(null);
  // 방금 저장한 PATCH 가 접속 정보를 건드렸는가. 서버가 그때 verified_at 을 지우므로 화면이 그것을 말한다
  const [savedCredentials, setSavedCredentials] = useState(false);

  useEffect(() => {
    const d = q.data;
    if (!d) return;
    setNames({ displayName: displayName(d), name: d.name });
    setConn({ host: d.host, port: String(d.port), database: d.database ?? "", db: d.db === null ? "" : String(d.db), username: d.username ?? "" });
    // CA 는 화면에 다시 보이지 않는다. 비워 두면 서버가 지금 CA 를 둔다
    setTls(d.tls ? { enabled: true, port: String(d.tls.port), ca: "" } : NO_TLS);
  }, [q.data]);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["data-services"] });

  // 바뀐 것만 담는다. 비면 PATCH 가 422 empty_patch 라 보내지 않고 화면에서 알려 준다
  function buildPatch(): DataServicePatch {
    const d = q.data!;
    const body: DataServicePatch = {};
    // name 은 생성 뒤 바꿀 수 없어 보내지 않는다. display_name 만 바뀌었을 때 보낸다
    const display = names.displayName.trim();
    if (display && display !== displayName(d)) body.display_name = display;
    const c = connectionValues(d.kind, conn);
    if (c.host !== d.host) body.host = c.host;
    if (c.port !== d.port) body.port = c.port;
    if (c.database !== undefined && c.database !== d.database) body.database = c.database;
    if (c.db !== undefined && c.db !== d.db) body.db = c.db;
    if (c.username !== d.username) body.username = c.username;
    if (password) body.password = password;
    if (d.kind === "redis") {
      const endpoint = tlsBody(tls);
      // 끄거나, 켜거나, 포트를 바꾸거나, 새 CA 를 넣었을 때만 보낸다
      if (!endpoint) {
        if (d.tls) body.tls = null;
      } else if (!d.tls || endpoint.port !== d.tls.port || endpoint.ca) {
        body.tls = endpoint;
      }
    }
    return body;
  }

  const patch = useMutation({
    mutationFn: (body: DataServicePatch) => patchDataService(id, body),
    onSuccess: async (_d, body) => {
      setPassword("");
      setTls((v) => ({ ...v, ca: "" }));
      setVerifyResult(null);
      // 서버는 주소(host·port·database·db·tls)나 username·password 가 온 PATCH 에서 verified_at 을
      // 지운다 (services/data_services.py, P68·P61)
      setSavedCredentials(
        [body.host, body.port, body.database, body.db, body.username, body.password, body.tls].some((v) => v !== undefined),
      );
      await invalidate();
    },
  });

  const verify = useMutation({
    mutationFn: () => verifyDataService(id),
    onSuccess: async (r) => {
      setVerifyResult(r);
      await invalidate();
    },
  });

  const [deleting, setDeleting] = useState(false);

  // 지워진 Database 의 북마크를 따라온 사람에게 빨간 줄 하나만 주지 않는다
  if (missingResource(q.error, q.data)) {
    return <NotFoundPage resource={t.resources.kind["data-service"]} backTo="/data-services" backLabel={text.backToList} />;
  }
  const gate = queryGate(q);
  if (gate) return gate;
  const d = q.data!;
  // redis 의 verify 는 PING 뒤 INFO·MODULE LIST 결과를 redis 에 싣는다 (D33). 타입이 느슨해 여기서 좁힌다
  const redis = verifyResult ? redisVerify(verifyResult) : null;
  // db 가 0 이 아닌 Redis 는 모듈이 다 있어도 체크포인터로 못 쓴다. verify 가 ok 여도 경고로 내린다 (P17 F5)
  const checkpointerNote = redis ? capabilityNote(t, "checkpointer", redis.checkpointer, d.db) : null;
  // 접속 정보를 고친 뒤 남아 있는 capabilities 는 지난 확인의 결과다. 지금 사실이 아닐 수 있다
  const staleCapabilities = !d.verified_at && capabilityNames(d.kind).some((name) => d.capabilities[name] !== undefined);

  return (
    <PageWidth data-testid="data-service-detail-page" className="flex flex-col gap-4">
      <PageHeader
        title={displayName(d)}
        name={secondaryName(d)}
        testId="page-title"
        actions={
          <>
            <Button variant="secondary" data-testid="verify-button" disabled={verify.isPending} onClick={() => verify.mutate()}>
              {verify.isPending ? text.verifying : text.verify}
            </Button>
            <Button variant="danger" data-testid="delete-button" onClick={() => setDeleting(true)}>
              {t.common.delete}
            </Button>
          </>
        }
      />
      <ResourceDeleteDialog
        resource="data-service"
        target={deleting ? { id: d.id, name: d.name } : null}
        onClose={() => setDeleting(false)}
        onDeleted={() => navigate("/data-services")}
      />
      <ErrorMessage error={verify.error} testId="verify-error" />
      {verifyResult ? (
        // 접속만 됐다고 초록으로 끝내면 checkpointer 가 모자란 Redis 를 "문제 없음" 으로 읽는다
        <Alert variant={!verifyResult.ok ? "error" : checkpointerNote ? "warn" : "ok"} data-testid="verify-result">
          {verifyResult.ok ? (
            `Connection ok (${verifyResult.detail ?? "verified"})`
          ) : (
            <>
              <div data-testid="verify-hint">{verifyErrorText(t, d.kind, verifyResult.error) ?? t.dataServices.verify.failed}</div>
              <p className="mt-1 font-mono text-xs opacity-80" data-testid="verify-detail">
                {verifyResult.error ?? t.detail.phaseBadge.unknown} {verifyResult.detail ?? ""}
              </p>
            </>
          )}
          {redis ? (
            <p className="mt-1 font-mono text-xs" data-testid="verify-redis">
              {redisVerifyText(t, redis)}
            </p>
          ) : null}
          {checkpointerNote ? (
            <p className="mt-1 text-xs" data-testid="verify-checkpointer-note">
              {checkpointerNote}
            </p>
          ) : null}
        </Alert>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card data-testid="ds-summary">
          <CardHeader>
            <CardTitle>{t.detail.summaryFields.title}</CardTitle>
            <Badge>{d.kind}</Badge>
          </CardHeader>
          <CardContent>
            <dl className="grid grid-cols-[8rem_1fr] gap-y-1 text-sm">
              <dt className="text-muted">{t.detail.summaryFields.id}</dt>
              <dd className="font-mono text-xs">{d.id}</dd>
              <dt className="text-muted">{text.labels.cluster}</dt>
              <dd>
                <ClusterLink id={d.cluster_id} />
              </dd>
              <dt className="text-muted">{text.labels.secret}</dt>
              <dd className="font-mono text-xs">{d.secret_name}</dd>
              <dt className="text-muted">{text.labels.password}</dt>
              <dd>{d.has_password ? text.labels.passwordSet : text.labels.passwordNone}</dd>
              {d.kind === "redis" ? (
                <>
                  <dt className="text-muted">{text.labels.tls}</dt>
                  <dd className="font-mono text-xs" data-testid="ds-tls-summary">
                    {d.tls ? text.labels.tlsEndpoint(d.tls.port, d.tls.ca_secret) : text.labels.tlsNone}
                    {d.tls && d.plain_closed ? ` · ${text.labels.plainClosed}` : null}
                  </dd>
                </>
              ) : null}
              {hasTls ? (
                <>
                  <dt className="text-muted">{text.labels.plainConsumers}</dt>
                  <dd data-testid="ds-plain-consumers">
                    {plain.isError ? (
                      <ErrorMessage error={plain.error} testId="ds-plain-consumers-error" />
                    ) : plain.data ? (
                      <PlainConsumers labels={text.labels} rows={plain.data} />
                    ) : null}
                  </dd>
                </>
              ) : null}
              <dt className="text-muted">{t.dataServices.status.verified}</dt>
              <dd data-testid="ds-verified-at">
                {d.verified_at ? <Badge variant="ok">{formatDate(t, d.verified_at)}</Badge> : <Badge variant="muted">{t.dataServices.status.unverified}</Badge>}
              </dd>
              <dt className="text-muted">{text.labels.capabilities}</dt>
              <dd className="flex flex-col gap-2" data-testid="ds-capabilities">
                {capabilityNames(d.kind).map((name) => {
                  const value: string | undefined = d.capabilities[name];
                  const badge = capabilityBadge(t, name, value, d.db);
                  const note = capabilityNote(t, name, value, d.db);
                  return (
                    <div key={name} className="flex flex-col gap-1">
                      <span className="flex flex-wrap items-center gap-2" data-testid={`ds-capability-${name}`}>
                        <Badge variant={staleCapabilities ? "muted" : badge.variant}>{badge.text}</Badge>
                        <span className="font-mono text-xs text-muted">
                          {name} {value ?? t.dataServices.status.unverified}
                        </span>
                      </span>
                      {note ? (
                        <p className="text-xs text-muted" data-testid={`ds-capability-note-${name}`}>
                          {note}
                        </p>
                      ) : null}
                    </div>
                  );
                })}
                {staleCapabilities ? (
                  <p className="text-xs text-warn-fg" data-testid="ds-capabilities-stale">
                    {text.staleCapabilities}
                  </p>
                ) : null}
              </dd>
              <dt className="text-muted">{t.detail.summaryFields.updated}</dt>
              <dd>{formatDate(t, d.updated_at)}</dd>
            </dl>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{text.labels.edit}</CardTitle>
          </CardHeader>
          <CardContent>
            <form
              className="flex flex-col gap-3"
              data-testid="data-service-edit-form"
              onSubmit={(e) => {
                e.preventDefault();
                const body = buildPatch();
                if (!Object.keys(body).length) {
                  setNoChange(true);
                  return;
                }
                setNoChange(false);
                patch.mutate(body);
              }}
            >
              <NameFields testId="ds" placeholders={t.dataServices.placeholders} value={names} onChange={setNames} readOnlyName errors={nameFieldErrors(patch.error)} />
              <ConnectionFields
                kind={d.kind}
                value={conn}
                onChange={(p) => setConn((c) => ({ ...c, ...p }))}
                password={
                  <Field label={text.labels.newPassword} htmlFor="ds-password" hint={text.passwordHint}>
                    <Input id="ds-password" data-testid="ds-password" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
                  </Field>
                }
              />
              {d.kind === "redis" ? <TlsFields value={tls} onChange={(p) => setTls((v) => ({ ...v, ...p }))} hasCa={d.tls !== null} /> : null}
              {!isNameFieldError(patch.error) ? <ErrorMessage error={patch.error} testId="save-error" /> : null}
              {noChange ? <Alert variant="info" data-testid="ds-notice">{t.errors.noChange}</Alert> : null}
              {patch.isSuccess && !noChange ? (
                <Alert variant={savedCredentials ? "warn" : "ok"} data-testid="saved">
                  {savedCredentials ? text.savedCredentials : t.shared.saved}
                </Alert>
              ) : null}
              <div className="flex justify-end">
                <Button type="submit" data-testid="save-button" disabled={patch.isPending}>
                  {patch.isPending ? t.shared.saving : t.shared.save}
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      </div>

      <ReferencesCard resource="data-service" id={d.id} />
    </PageWidth>
  );
}

type DetailLabels = Messages["dataServices"]["detail"]["labels"];

/** 평문 포트로 접속하는 것의 요약. 한 MCP Server 가 바인딩과 세션 저장소로 두 줄이어도 하나로 센다 */
function plainSummary(labels: DetailLabels, rows: PlainRedisConsumer[]): { sentence: string; names: string[]; image: boolean } {
  const unique = [...new Map(rows.map((r) => [`${r.kind}:${r.id}`, r])).values()];
  const agents = unique.filter((r) => r.kind === "agent").length;
  return {
    sentence: unique.length ? labels.plainConsumersCount(agents, unique.length - agents) : labels.plainConsumersNone,
    names: unique.map((r) => r.name),
    image: rows.some((r) => r.reason === "runtime_image"),
  };
}

function PlainConsumers({ labels, rows }: { labels: DetailLabels; rows: PlainRedisConsumer[] }) {
  const s = plainSummary(labels, rows);
  return (
    <>
      {s.sentence}
      {s.image ? <span className="block text-xs text-muted">{labels.plainConsumersImage}</span> : null}
      {s.names.length ? <span className="block font-mono text-xs text-muted">{s.names.join(", ")}</span> : null}
    </>
  );
}
