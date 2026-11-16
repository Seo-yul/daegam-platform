import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router";

import { createDataService, listClusters, type DataServiceCreate } from "@/api/resources";
import type { DataServiceKind } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { PageHeader } from "@/components/Layout";
import { NameFields, isNameFieldError, nameFieldErrors, type NameValue } from "@/components/NameFields";
import { PageWidth } from "@/components/PageWidth";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { useT } from "@/i18n";
import { CLUSTERS_KEY } from "@/lib/queryKeys";

import { ConnectionFields, connectionValues, type ConnectionValue } from "./ConnectionFields";
import { NO_TLS, TlsFields, tlsBody, type TlsValue } from "./TlsFields";

const DEFAULT_PORT: Record<DataServiceKind, number> = { postgres: 5432, redis: 6379 };

export function DataServiceNewPage() {
  const t = useT();
  const text = t.dataServices.form;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const clusters = useQuery({ queryKey: CLUSTERS_KEY, queryFn: listClusters });

  const [names, setNames] = useState<NameValue>({ displayName: "", name: "" });
  const [kind, setKind] = useState<DataServiceKind>("postgres");
  const [clusterId, setClusterId] = useState("");
  const [conn, setConn] = useState<ConnectionValue>({ host: "", port: String(DEFAULT_PORT.postgres), database: "", db: "0", username: "" });
  const [password, setPassword] = useState("");
  const [tls, setTls] = useState<TlsValue>(NO_TLS);

  const create = useMutation({
    mutationFn: () => {
      const body: DataServiceCreate = {
        name: names.name.trim(),
        kind,
        ...connectionValues(kind, conn),
        cluster_id: clusterId || null,
        password: password || null,
      };
      const endpoint = kind === "redis" ? tlsBody(tls) : null;
      if (endpoint) body.tls = endpoint;
      if (names.displayName.trim()) body.display_name = names.displayName.trim();
      return createDataService(body);
    },
    onSuccess: async (d) => {
      await queryClient.invalidateQueries({ queryKey: ["data-services"] });
      navigate(`/data-services/${d.id}`);
    },
  });

  return (
    <PageWidth data-testid="data-service-new-page">
      <PageHeader title={text.title} testId="page-title" />
      <Card className="max-w-xl">
        <CardContent>
          <form
            className="flex flex-col gap-3"
            data-testid="data-service-form"
            onSubmit={(e) => {
              e.preventDefault();
              create.mutate();
            }}
          >
            <NameFields testId="ds" placeholders={t.dataServices.placeholders} value={names} onChange={setNames} errors={nameFieldErrors(create.error)} />
            <div className="grid grid-cols-2 gap-3">
              <Field label={text.kind} hint={text.kindHint} htmlFor="ds-kind">
                <Select
                  id="ds-kind"
                  data-testid="ds-kind"
                  value={kind}
                  onChange={(e) => {
                    const k = e.target.value as DataServiceKind;
                    setKind(k);
                    setConn((c) => ({ ...c, port: String(DEFAULT_PORT[k]) }));
                  }}
                >
                  <option value="postgres">{text.kinds.postgres}</option>
                  <option value="redis">{text.kinds.redis}</option>
                </Select>
              </Field>
              <Field label={text.cluster} htmlFor="ds-cluster" hint={t.shared.deployDialog.clusterHint}>
                <Select id="ds-cluster" data-testid="ds-cluster" value={clusterId} onChange={(e) => setClusterId(e.target.value)}>
                  <option value="">{t.shared.tenantDefault}</option>
                  {clusters.data?.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            <ConnectionFields
              kind={kind}
              value={conn}
              onChange={(p) => setConn((c) => ({ ...c, ...p }))}
              password={
                <Field label={text.password} htmlFor="ds-password" required={kind === "postgres"} hint={kind === "postgres" ? undefined : text.passwordHint}>
                  {/* 인프라 DB 비밀번호를 브라우저 암호 관리자가 저장하겠다고 묻지 않게 한다 */}
                  <Input id="ds-password" data-testid="ds-password" type="password" autoComplete="new-password" required={kind === "postgres"} value={password} onChange={(e) => setPassword(e.target.value)} />
                </Field>
              }
            />
            {kind === "redis" ? <TlsFields value={tls} onChange={(p) => setTls((v) => ({ ...v, ...p }))} hasCa={false} /> : null}
            {!isNameFieldError(create.error) ? <ErrorMessage error={create.error} testId="save-error" /> : null}
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => navigate("/data-services")}>
                {t.common.cancel}
              </Button>
              <Button type="submit" data-testid="save-button" disabled={create.isPending}>
                {create.isPending ? t.shared.saving : t.shared.save}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </PageWidth>
  );
}
