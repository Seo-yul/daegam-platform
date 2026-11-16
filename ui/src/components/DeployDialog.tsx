/**
 * 배포 대화상자 (D42). MCP Server 와 Gateway 가 같이 쓴다.
 *
 * 정의와 배포가 한 리소스가 되면서 배포는 화면 이동이 아니라 문서에 거는 동작이 됐다. 그래서 옛 Deploy
 * 페이지 대신 상세·목록 어디서나 열 수 있는 대화상자다. 정하는 것은 클러스터와 네임스페이스, 외부 노출
 * 셋뿐이다. 이름·리비전은 문서가 이미 갖고 있어 고를 것이 없다.
 *
 * Agent 는 replicas·리소스와 다른 노출 모델(D27)이 더 붙어 자기 대화상자를 따로 둔다.
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { listClusters } from "@/api/resources";
import type { ExposureInput } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { ExposureSection, useResolvedClusterId } from "@/components/ExposureSection";
import { Button } from "@/components/ui/button";
import { Field, Select } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { useT } from "@/i18n";
import { EMPTY_EXPOSURE, exposureFieldErrors, isExposureFieldError, toExposureInput, type ExposureForm } from "@/lib/exposure";
import { CLUSTERS_KEY } from "@/lib/queryKeys";

/** MCP Server 와 Gateway 의 배포 본문은 모양이 같다. */
export interface DeployBody {
  cluster_id?: string | null;
  namespace?: string | null;
  exposure?: ExposureInput | null;
}

export function DeployDialog({
  open,
  title,
  name,
  description,
  pending,
  error,
  extra,
  onSubmit,
  onClose,
}: {
  open: boolean;
  /** 대화상자 제목. "MCP Server 배포" 처럼 무엇을 올리는지 적는다. */
  title: string;
  /** 리소스의 k8s 이름. 노출 섹션이 호스트 기본값을 여기서 만든다. */
  name: string;
  description: string;
  pending: boolean;
  error: unknown;
  /** 서버가 거부한 이유를 자세히 적는 자리 (예: Gateway 의 409 backends_not_ready 백엔드 표). */
  extra?: React.ReactNode;
  onSubmit: (body: DeployBody) => void;
  onClose: () => void;
}) {
  const t = useT();
  const text = t.shared.deployDialog;
  const clusters = useQuery({ queryKey: CLUSTERS_KEY, queryFn: listClusters, enabled: open });
  const [clusterId, setClusterId] = useState("");
  const [exposure, setExposure] = useState<ExposureForm>(EMPTY_EXPOSURE);
  const resolvedClusterId = useResolvedClusterId(clusterId);

  return (
    <Modal open={open} title={title} testId="deploy-dialog" closeTestId="deploy-dialog-close" onClose={() => (pending ? undefined : onClose())}>
      <form
        className="flex flex-col gap-3 px-5 py-4"
        data-testid="deploy-form"
        onSubmit={(e) => {
          e.preventDefault();
          onSubmit({
            cluster_id: clusterId || null,
            namespace: exposure.namespace || null,
            exposure: toExposureInput(exposure),
          });
        }}
      >
        <p className="text-sm text-muted">{description}</p>
        <Field label={text.labels.cluster} htmlFor="deploy-cluster" hint={text.clusterHint}>
          <Select
            id="deploy-cluster"
            data-testid="deploy-cluster"
            value={clusterId}
            onChange={(e) => {
              setClusterId(e.target.value);
              // 클러스터가 바뀌면 네임스페이스·옵션이 달라지므로 노출 섹션을 처음부터 다시 채운다
              setExposure(EMPTY_EXPOSURE);
            }}
          >
            <option value="">{t.shared.tenantDefault}</option>
            {clusters.data?.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </Select>
        </Field>
        <fieldset className="flex flex-col gap-3 rounded-card border border-line px-4 py-3">
          <legend className="px-1 text-sm font-semibold text-heading">{text.exposureLegend}</legend>
          {/* 클러스터 선택이 바뀌면(같은 클러스터로 풀리더라도) 섹션을 새로 마운트해 기본값을 다시 채운다 */}
          <ExposureSection key={`${clusterId}:${resolvedClusterId ?? ""}`} clusterId={resolvedClusterId} name={name} value={exposure} onChange={setExposure} errors={exposureFieldErrors(error)} />
        </fieldset>
        {!isExposureFieldError(error) ? <ErrorMessage error={error} testId="deploy-error" /> : null}
        {extra}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" disabled={pending} onClick={onClose}>
            {t.common.cancel}
          </Button>
          <Button type="submit" data-testid="deploy-submit" disabled={pending}>
            {pending ? text.deploying : text.submit}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
