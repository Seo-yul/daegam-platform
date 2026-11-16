/**
 * Agent 배포 대화상자 (D42). MCP Server·Gateway 의 공용 대화상자와 달리 replicas·리소스와
 * 다른 노출 모델(D27, cors_origins·allowed_groups)이 붙어 따로 둔다.
 *
 * 그래프 검증은 캔버스(정의 탭)가 이미 보이고 있고 서버가 배포 때 다시 한다. 여기서 또 한 벌 돌리면
 * 같은 판정을 세 곳에서 유지해야 하므로, 이 대화상자는 배포 자리만 정하고 거부는 서버 응답으로 받는다.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router";

import { ApiError } from "@/api/client";
import { deployAgent, listClusters } from "@/api/resources";
import type { Agent } from "@/api/types";
import { AgentExposureSection, EMPTY_AGENT_EXPOSURE, agentExposureInvalid, toAgentExposureInput, type AgentExposureForm } from "@/components/AgentExposureSection";
import { ScaleToZeroField } from "@/components/agents/AgentSleep";
import { EvaluationSkip, NO_SKIP, skipBody, skipIncomplete, type SkipState } from "@/components/agents/EvaluationSkip";
import { ErrorMessage } from "@/components/ErrorMessage";
import { useResolvedClusterId } from "@/components/ExposureSection";
import { EMPTY_DRAFT, ResourcesEditor, resourcesFromDraft, type ResourcesDict, type ResourcesDraft } from "@/components/ResourcesEditor";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { useT } from "@/i18n";
import { mainHasMemory } from "@/lib/agentGraph";
import { scaleToZeroBlock, windowMinutes } from "@/lib/agentSleep";
import { exposureFieldErrors, isExposureFieldError } from "@/lib/exposure";
import { displayName, k8sResourceName } from "@/lib/names";
import { CLUSTERS_KEY } from "@/lib/queryKeys";

export function AgentDeployDialog({ agent, open, onClose, onStarted }: { agent: Agent; open: boolean; onClose: () => void; /** 배포가 실제로 시작됐을 때. 상세가 낭독기에게 알린다 (D43). */ onStarted?: () => void }) {
  const t = useT();
  const text = t.agents.deployDialog;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const clusters = useQuery({ queryKey: CLUSTERS_KEY, queryFn: listClusters, enabled: open });

  const [clusterId, setClusterId] = useState("");
  const [replicas, setReplicas] = useState("1");
  const [resources, setResources] = useState(EMPTY_DRAFT);
  const [exposure, setExposure] = useState<AgentExposureForm>(EMPTY_AGENT_EXPOSURE);
  // 쓰지 않으면 0 으로 줄여도 되는가 (P44 6절). 문서의 배포 설정이고 처음에는 켜져 있다
  const [scaleToZero, setScaleToZero] = useState(agent.scale_to_zero);
  // 배포를 막은 resources 입력. 오류 문장은 그릴 때 이것으로 다시 만들어 언어를 바꿔도 따라간다
  const [badResources, setBadResources] = useState<ResourcesDraft | null>(null);
  // 배포 게이트가 막은 뒤에 평가 없이 배포하는 까닭 (P45 12절)
  const [skip, setSkip] = useState<SkipState>(NO_SKIP);
  const resolvedClusterId = useResolvedClusterId(clusterId);

  // 열 때마다 처음 값으로 되돌린다. 닫았다 다시 연 사람은 지난번 입력을 기억하지 못한다
  useEffect(() => {
    if (open) {
      setClusterId("");
      setReplicas("1");
      setResources(EMPTY_DRAFT);
      setExposure(EMPTY_AGENT_EXPOSURE);
      setScaleToZero(agent.scale_to_zero);
      setBadResources(null);
      setSkip(NO_SKIP);
    }
  }, [open, agent.scale_to_zero]);

  // resources 는 onSubmit 이 이미 풀어 검사한 값을 받는다. 여기서 다시 풀면 같은 오류를 두 곳에서 다룬다
  const deploy = useMutation({
    mutationFn: (res: ResourcesDict | null) =>
      deployAgent(agent.id, {
        cluster_id: clusterId || null,
        namespace: exposure.namespace || null,
        replicas: Number(replicas) || 1,
        resources: res,
        exposure: toAgentExposureInput(exposure),
        scale_to_zero: scaleToZero,
        ...skipBody(skip),
      }),
    onSuccess: async (made) => {
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      onClose();
      onStarted?.();
      navigate(`/agents/${made.id}`, { replace: true, state: { warnings: made.warnings ?? [] } });
    },
  });

  const nameConflict = deploy.error instanceof ApiError && deploy.error.code === "k8s_name_conflict";
  const absorbed = isExposureFieldError(deploy.error);
  const exposureInvalid = agentExposureInvalid(exposure);
  const resourcesError = badResources ? resourcesFromDraft(t, badResources).error : null;

  return (
    <Modal open={open} title={text.title} testId="deploy-dialog" closeTestId="deploy-dialog-close" onClose={() => (deploy.isPending ? undefined : onClose())}>
      <form
        className="flex flex-col gap-3 px-5 py-4"
        data-testid="deploy-form"
        onSubmit={(e) => {
          e.preventDefault();
          const res = resourcesFromDraft(t, resources);
          if (res.error) {
            setBadResources(resources);
            return;
          }
          setBadResources(null);
          deploy.mutate(res.value);
        }}
      >
        <p className="text-sm text-muted">{text.body(<span className="font-medium text-heading">{displayName(agent)}</span>, agent.revision)}</p>
        <div className="grid grid-cols-2 gap-3">
          <Field label={t.shared.deployDialog.labels.cluster} htmlFor="ag-cluster" hint={t.shared.deployDialog.clusterHint}>
            <Select
              id="ag-cluster"
              data-testid="ag-cluster"
              value={clusterId}
              onChange={(e) => {
                setClusterId(e.target.value);
                // 클러스터가 바뀌면 네임스페이스·옵션이 달라지므로 노출 섹션을 처음부터 다시 채운다
                setExposure(EMPTY_AGENT_EXPOSURE);
              }}
            >
              <option value="">{t.shared.tenantDefault}</option>
              {clusters.data?.map((c) => (
                <option key={c.id} value={c.id}>
                  {displayName(c)}
                </option>
              ))}
            </Select>
          </Field>
          <Field label={text.labels.replicas} htmlFor="ag-replicas" hint={text.replicasHint}>
            <Input id="ag-replicas" data-testid="ag-replicas" type="number" min={1} max={10} value={replicas} onChange={(e) => setReplicas(e.target.value)} />
          </Field>
        </div>
        <ResourcesEditor testId="ag" value={resources} onChange={setResources} />
        {/* 배포는 지금의 정의를 핀하므로 memory 노드는 정의에서, 노출은 아래 입력에서 읽는다. 첫 배포에는 상태가 없어 쉬는 창을
            모르므로 분 없는 풀이가 선다 (P44 6절) */}
        <ScaleToZeroField
          checked={scaleToZero}
          onChange={setScaleToZero}
          block={scaleToZeroBlock({ remembers: mainHasMemory(agent.graph), exposed: exposure.enabled, status: agent.status })}
          minutes={windowMinutes(agent.status)}
        />
        <fieldset className="flex flex-col gap-3 rounded-card border border-line px-4 py-3">
          <legend className="px-1 text-sm font-semibold text-heading">{t.shared.deployDialog.exposureLegend}</legend>
          {/* 클러스터 선택이 바뀌면(같은 클러스터로 풀리더라도) 섹션을 새로 마운트해 기본값을 다시 채운다 */}
          <AgentExposureSection key={`${clusterId}:${resolvedClusterId ?? ""}`} clusterId={resolvedClusterId} name={agent.name} value={exposure} onChange={setExposure} errors={exposureFieldErrors(deploy.error)} />
        </fieldset>
        <p className="text-xs text-muted" data-testid="ag-preview">
          {text.preview(
            <code className="font-mono text-fg">{k8sResourceName("agent", agent.name)}</code>,
            exposure.namespace ? <span className="font-mono"> ({exposure.namespace})</span> : null,
          )}
        </p>
        {resourcesError ? (
          <Alert variant="error" data-testid="form-error">
            {`resources: ${resourcesError}`}
          </Alert>
        ) : null}
        {nameConflict ? (
          <Alert variant="error" data-testid="name-conflict">
            {text.nameConflict}
          </Alert>
        ) : !absorbed ? (
          <ErrorMessage error={deploy.error} testId="deploy-error" />
        ) : null}
        <EvaluationSkip agent={agent} error={deploy.error} value={skip} onChange={setSkip} onLeave={onClose} testId="deploy-skip" />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" disabled={deploy.isPending} onClick={onClose}>
            {t.common.cancel}
          </Button>
          <Button type="submit" data-testid="deploy-submit" disabled={deploy.isPending || exposureInvalid || skipIncomplete(skip)}>
            {deploy.isPending ? t.shared.deployDialog.deploying : t.shared.deployDialog.submit}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
