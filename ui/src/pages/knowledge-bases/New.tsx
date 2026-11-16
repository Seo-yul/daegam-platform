import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { createKnowledgeBase, getPlatformSettings, listDataServices } from "@/api/resources";
import type { KnowledgeBaseCreate } from "@/api/types";
import { KnowledgeErrorMessage } from "@/components/KnowledgeBadges";
import { PageHeader } from "@/components/Layout";
import { NameFields, isNameFieldError, nameFieldErrors, type NameValue } from "@/components/NameFields";
import { PageWidth } from "@/components/PageWidth";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { useT } from "@/i18n";
import { DEFAULT_CHUNKING, DEFAULT_MAX_CHUNKS, effectiveEmbeddingModel, knownDimensions, supportsEmbedding } from "@/lib/knowledge";
import { displayName } from "@/lib/names";
import { PLATFORM_SETTINGS_KEY, dataServicesKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";

import { ChunkingFields, type ChunkingValue } from "./ChunkingFields";

/**
 * Knowledge Base 생성 (D32). Database 는 postgres 만, provider 는 임베딩을 지원하는 것(openai·stub)만 고를 수 있다.
 * 모델·차원은 provider 기본을 placeholder 로 보이고 바꿀 수 있다. 알려진 모델이 아니면 차원이 필수다(422 dimensions_required).
 * POST 가 Database 에 접속해 확장·테이블을 만들므로 pgvector_missing·db_unreachable 같은 422 가 여기서 바로 보인다.
 */
export function KnowledgeBaseNewPage() {
  const t = useT();
  const text = t.knowledgeBases.form;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const tenant = useTenant();
  const dataServices = useQuery({ queryKey: dataServicesKey(tenant), queryFn: listDataServices });
  const settings = useQuery({ queryKey: PLATFORM_SETTINGS_KEY, queryFn: getPlatformSettings });

  const [names, setNames] = useState<NameValue>({ displayName: "", name: "" });
  const [dataServiceId, setDataServiceId] = useState("");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [dimensions, setDimensions] = useState("");
  const [chunking, setChunking] = useState<ChunkingValue>({ size: String(DEFAULT_CHUNKING.size), overlap: String(DEFAULT_CHUNKING.overlap), maxChunks: String(DEFAULT_MAX_CHUNKS) });

  const postgres = (dataServices.data ?? []).filter((d) => d.kind === "postgres");
  const ds = postgres.find((d) => d.id === dataServiceId) ?? null;
  /**
   * verify 가 남긴 capabilities.pgvector. 서버는 verify 가 성공했을 때만 채우므로 세 갈래가 아니라 네
   * 갈래다. undefined 는 "확인한 적이 없다" 는 뜻이고, 방금 등록한 Database 가 딱 그 상태다. 그것을
   * 침묵으로 두면 Create 를 누르고 몇 초를 기다린 뒤에야 422 를 본다 (POST 가 그 자리에서 접속한다).
   */
  const pgvector = ds?.capabilities?.pgvector;
  const embeddingProviders = (settings.data?.llm_providers ?? []).filter((p) => supportsEmbedding(p.kind));
  const p = embeddingProviders.find((x) => x.name === provider) ?? null;
  const modelPlaceholder = effectiveEmbeddingModel(p) ?? "";
  const effectiveModel = model.trim() || modelPlaceholder;
  const known = knownDimensions(effectiveModel);
  const dimensionsRequired = Boolean(effectiveModel) && known === null;

  const create = useMutation({
    mutationFn: () => {
      const body: KnowledgeBaseCreate = {
        name: names.name.trim(),
        data_service_id: dataServiceId,
        embedding: { provider, model: model.trim() || null, dimensions: dimensions.trim() ? Number(dimensions) : null },
        chunking: { size: Number(chunking.size), overlap: Number(chunking.overlap) },
        limits: { max_chunks: Number(chunking.maxChunks) },
      };
      if (names.displayName.trim()) body.display_name = names.displayName.trim();
      return createKnowledgeBase(body);
    },
    onSuccess: async (kb) => {
      await queryClient.invalidateQueries({ queryKey: ["knowledge-bases"] });
      navigate(`/knowledge-bases/${kb.id}`);
    },
  });

  return (
    <PageWidth data-testid="knowledge-base-new-page">
      <PageHeader title={text.title} testId="page-title" />
      <Card className="max-w-xl">
        <CardContent>
          <form
            className="flex flex-col gap-3"
            data-testid="knowledge-base-form"
            onSubmit={(e) => {
              e.preventDefault();
              create.mutate();
            }}
          >
            <NameFields testId="kb" placeholders={t.knowledgeBases.placeholders} value={names} onChange={setNames} errors={nameFieldErrors(create.error)} />
            <Field label={text.dataService} htmlFor="kb-data-service" hint={text.dataServiceHint}>
              <Select id="kb-data-service" data-testid="kb-data-service" required value={dataServiceId} placeholder={t.shared.select} onChange={(e) => setDataServiceId(e.target.value)}>
                <option value="">{t.shared.select}</option>
                {postgres.map((d) => (
                  <option key={d.id} value={d.id}>
                    {displayName(d)} ({d.host}:{d.port}/{d.database ?? ""})
                  </option>
                ))}
              </Select>
            </Field>
            {dataServices.isError ? (
              <p className="text-xs text-err-fg" data-testid="kb-data-services-error">
                {text.dataServicesError}
              </p>
            ) : dataServices.isSuccess && !postgres.length ? (
              <p className="text-xs text-warn-fg" data-testid="kb-no-postgres">
                {text.noPostgres}
              </p>
            ) : null}
            {pgvector === "missing" ? (
              <Alert variant="warn" data-testid="kb-pgvector-warning">
                {text.pgvectorMissing}
              </Alert>
            ) : pgvector === "available" ? (
              <Alert variant="warn" data-testid="kb-pgvector-available">
                {text.pgvectorAvailable}
              </Alert>
            ) : ds && pgvector === undefined ? (
              <Alert variant="info" data-testid="kb-unverified-warning">
                {text.unverified(
                  <Link className="font-medium underline underline-offset-2" to={`/data-services/${ds.id}`} data-testid="kb-unverified-link">
                    {text.unverifiedLink}
                  </Link>,
                )}
              </Alert>
            ) : null}
            <Field label={text.provider} htmlFor="kb-provider" hint={text.providerHint}>
              <Select
                id="kb-provider"
                data-testid="kb-provider"
                required
                value={provider}
                placeholder={t.shared.select}
                onChange={(e) => {
                  setProvider(e.target.value);
                  setModel("");
                  setDimensions("");
                }}
              >
                <option value="">{t.shared.select}</option>
                {embeddingProviders.map((x) => (
                  <option key={x.name} value={x.name}>
                    {x.name} ({x.kind}{effectiveEmbeddingModel(x) ? ` · ${effectiveEmbeddingModel(x)}` : ""})
                  </option>
                ))}
              </Select>
            </Field>
            {settings.isError ? (
              <p className="text-xs text-err-fg" data-testid="kb-settings-error">
                {text.settingsError}
              </p>
            ) : settings.isSuccess && !embeddingProviders.length ? (
              <p className="text-xs text-warn-fg" data-testid="kb-no-provider">
                {text.noProvider}
              </p>
            ) : null}
            <div className="grid grid-cols-[1fr_8rem] gap-3">
              <Field label={text.model} htmlFor="kb-model" hint={text.modelHint}>
                <Input id="kb-model" data-testid="kb-model" className="font-mono" placeholder={modelPlaceholder} value={model} onChange={(e) => setModel(e.target.value)} />
              </Field>
              <Field label={text.dimensions} htmlFor="kb-dimensions" hint={dimensionsRequired ? text.dimensionsRequired : known ? text.dimensionsDefault(known) : text.dimensionsPick}>
                <Input id="kb-dimensions" data-testid="kb-dimensions" type="number" min={1} required={dimensionsRequired} placeholder={known ? String(known) : ""} value={dimensions} onChange={(e) => setDimensions(e.target.value)} />
              </Field>
            </div>
            <ChunkingFields value={chunking} onChange={(patch) => setChunking((c) => ({ ...c, ...patch }))} required />
            {!isNameFieldError(create.error) ? <KnowledgeErrorMessage error={create.error} testId="save-error" /> : null}
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => navigate("/knowledge-bases")}>
                {t.common.cancel}
              </Button>
              <Button type="submit" data-testid="save-button" disabled={create.isPending}>
                {create.isPending ? text.creating : t.shared.create}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </PageWidth>
  );
}
