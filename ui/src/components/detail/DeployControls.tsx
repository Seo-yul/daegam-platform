/**
 * MCP Server·Gateway 상세가 같이 쓰는 배포 조작 (P73 2절). 배포·재배포·배포 해제 mutation, 대화상자 열림, 바쁨 규칙,
 * 낭독 알림을 `useDeployControls` 가 갖고, 머리의 배포·재배포 버튼과 재배포·배포 해제 확인 대화상자를 여기서 그린다.
 * 배포 대화상자의 본문과 덧붙임(Gateway 의 백엔드 보고·순환 경로), 게시 버튼, 대화상자의 문장은 화면이 넘긴다.
 * Agent 는 배포·재배포 대화상자가 노출을 고치고 API 가 달라 쓰지 않는다.
 */
import { useMutation, useQueryClient, type UseMutationResult } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { useNavigate } from "react-router";

import type { Exposure, ExposureInput, ResourceState } from "@/api/types";
import { ClusterLink } from "@/components/ClusterLink";
import { ErrorBody, ErrorMessage } from "@/components/ErrorMessage";
import { ExposureCard } from "@/components/ExposureCard";
import { ReferencesNotice, useReferences } from "@/components/ReferencesCard";
import { isBusyState, isRedeployableState } from "@/components/StateBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { LiveRegion, useAnnounce } from "@/components/ui/live-region";
import type { DeployBody } from "@/components/DeployDialog";
import { useT } from "@/i18n";
import type { DeletableResource } from "@/lib/deletion";
import { displayName } from "@/lib/names";
import { referenceNames } from "@/lib/references";

/** 배포 응답. 배포 응답에만 경고가 붙는다. */
interface Deployed {
  id: string;
  warnings?: string[];
}

export interface DeployApi<T extends Deployed> {
  deploy: (id: string, body: DeployBody) => Promise<T>;
  redeploy: (id: string) => Promise<T>;
  undeploy: (id: string) => Promise<unknown>;
}

export interface DeployControls<T extends Deployed> {
  resource: DeletableResource;
  id: string;
  deploy: UseMutationResult<T, Error, DeployBody>;
  redeploy: UseMutationResult<T, Error, void>;
  undeploy: UseMutationResult<unknown, Error, void>;
  deploying: boolean;
  setDeploying: (open: boolean) => void;
  confirmingRedeploy: boolean;
  setConfirmingRedeploy: (open: boolean) => void;
  confirmingUndeploy: boolean;
  setConfirmingUndeploy: (open: boolean) => void;
  /** 배포 해제가 409 로 막히면 그 응답의 id 를 이 표로 이름으로 바꿔 보인다 (D43). */
  undeployNames: ReadonlyMap<string, string>;
  /** 이 문서가 바뀌는 중이거나 조작 하나가 진행 중이다. */
  busy: (state: ResourceState) => boolean;
  invalidate: () => Promise<void>;
  /** 대화상자가 닫히는 것 말고는 아무 일도 없는 것처럼 보이는 자리다. 낭독기에게는 여기서 말한다. */
  announcer: ReactNode;
}

/**
 * 한 리소스의 배포 조작. collection 은 목록 쿼리 키의 첫 조각이자 상세 경로(`/<collection>/<id>`)다.
 * 배포 응답의 경고는 다음 폴링이 지우지 않게 화면 state 로 넘긴다.
 */
export function useDeployControls<T extends Deployed>({ resource, collection, id, api }: { resource: DeletableResource; collection: string; id: string; api: DeployApi<T> }): DeployControls<T> {
  const t = useT();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [deploying, setDeploying] = useState(false);
  const [confirmingRedeploy, setConfirmingRedeploy] = useState(false);
  const [confirmingUndeploy, setConfirmingUndeploy] = useState(false);
  const undeployNames = referenceNames(useReferences(resource, id, confirmingUndeploy).data ?? []);
  const { message, count, announce } = useAnnounce();
  const invalidate = () => queryClient.invalidateQueries({ queryKey: [collection] });

  const deploy = useMutation({
    mutationFn: (body: DeployBody) => api.deploy(id, body),
    onSuccess: async (doc) => {
      setDeploying(false);
      announce(t.ui.deployment.started.deploy);
      await invalidate();
      navigate(`/${collection}/${doc.id}`, { replace: true, state: { warnings: doc.warnings ?? [] } });
    },
  });
  const redeploy = useMutation({
    mutationFn: () => api.redeploy(id),
    onSuccess: async () => {
      setConfirmingRedeploy(false);
      announce(t.ui.deployment.started.redeploy);
      await invalidate();
    },
  });
  const undeploy = useMutation({
    mutationFn: () => api.undeploy(id),
    onSuccess: async () => {
      setConfirmingUndeploy(false);
      announce(t.ui.deployment.started.undeploy);
      await invalidate();
    },
  });

  return {
    resource,
    id,
    deploy,
    redeploy,
    undeploy,
    deploying,
    setDeploying,
    confirmingRedeploy,
    setConfirmingRedeploy,
    confirmingUndeploy,
    setConfirmingUndeploy,
    undeployNames,
    busy: (state) => isBusyState(state) || deploy.isPending || redeploy.isPending || undeploy.isPending,
    invalidate,
    announcer: <LiveRegion key={count} message={message} testId="deploy-announce" />,
  };
}

/** 머리의 주 동작. 초안이면 배포, 다시 올릴 수 있으면 재배포, 그 밖에는 없다. */
export function DeployPrimaryButton({ controls, state, canDeploy }: { controls: DeployControls<Deployed>; state: ResourceState; canDeploy: boolean }) {
  const t = useT();
  if (!canDeploy) return null;
  const busy = controls.busy(state);
  if (state === "draft") {
    return (
      <Button data-testid="deploy-button" disabled={busy} onClick={() => controls.setDeploying(true)}>
        {t.shared.deployDialog.submit}
      </Button>
    );
  }
  if (!isRedeployableState(state)) return null;
  return (
    <Button data-testid="redeploy-button" disabled={busy} onClick={() => controls.setConfirmingRedeploy(true)}>
      {controls.redeploy.isPending ? t.shared.detailActions.redeploying : t.shared.detailActions.redeploy}
    </Button>
  );
}

/** 재배포·배포 해제 확인. 문장은 화면이 넘기고, 이름과 참조 안내는 여기서 붙인다. */
export function DeployConfirmDialogs({
  controls,
  item,
  redeployBody,
  undeployBody,
}: {
  controls: DeployControls<Deployed>;
  item: { id: string; name: string; display_name?: string | null };
  redeployBody: ReactNode;
  undeployBody: ReactNode;
}) {
  const t = useT();
  const actions = t.shared.detailActions;
  return (
    <>
      <ConfirmDialog
        open={controls.confirmingRedeploy}
        title={actions.redeploy}
        description={<p>{redeployBody}</p>}
        confirmLabel={actions.redeploy}
        pendingLabel={actions.redeploying}
        pending={controls.redeploy.isPending}
        error={controls.redeploy.error ? <ErrorBody error={controls.redeploy.error} /> : null}
        onConfirm={() => controls.redeploy.mutate()}
        onCancel={() => controls.setConfirmingRedeploy(false)}
      />
      <ConfirmDialog
        open={controls.confirmingUndeploy}
        title={actions.undeploy}
        description={
          <>
            <p className="font-medium text-heading">{displayName(item)}</p>
            {undeployBody}
            <ReferencesNotice resource={controls.resource} id={item.id} open={controls.confirmingUndeploy} testId="undeploy-references" />
          </>
        }
        confirmLabel={actions.undeploy}
        pendingLabel={actions.undeploying}
        pending={controls.undeploy.isPending}
        error={controls.undeploy.error ? <ErrorBody error={controls.undeploy.error} names={controls.undeployNames} /> : null}
        onConfirm={() => controls.undeploy.mutate()}
        onCancel={() => controls.setConfirmingUndeploy(false)}
      />
    </>
  );
}

/**
 * 재배포 오류. 대화상자가 열려 있는 동안에는 그 안에 보인다. 뒤에 겹쳐 그리면 dim 에 가려 못 읽는다. 화면이 경고
 * 아래, 배포 알림 위에 둔다.
 */
export function RedeployError({ controls }: { controls: DeployControls<Deployed> }) {
  return !controls.confirmingRedeploy ? <ErrorMessage error={controls.redeploy.error} testId="redeploy-error" /> : null;
}

/** 배포 탭이 읽는 문서의 칸. MCP Server 와 Gateway 가 같은 모양이다. */
export interface DeployedItem {
  name: string;
  state: ResourceState;
  revision: number;
  deployed_revision?: number | null;
  cluster_id?: string | null;
  k8s?: { namespace: string; name: string } | null;
  exposure: Exposure;
}

/** 아직 올리지 않은 문서의 배포 탭. 무엇이 올라갈지 말하고 배포로 이끈다. Agent 배포 탭도 쓴다. */
export function DeploymentDraftCard({ canDeploy, onDeploy, body }: { canDeploy: boolean; onDeploy: () => void; body: ReactNode }) {
  const t = useT();
  return (
    <Card data-testid="deployment-empty">
      <CardContent className="flex flex-col items-start gap-3 py-8">
        <p className="text-sm font-medium text-heading">{t.detail.deployable.notDeployed}</p>
        <p className="text-sm text-muted">{body}</p>
        {canDeploy ? (
          <Button data-testid="deployment-deploy" onClick={onDeploy}>
            {t.shared.deployDialog.submit}
          </Button>
        ) : null}
      </CardContent>
    </Card>
  );
}

/**
 * 배포 탭. 초안이면 배포로 이끄는 빈 카드이고, 아니면 올라간 자리(클러스터·네임스페이스·리비전)와 노출 편집이다.
 * 종류마다 다른 줄(`lines`, dt·dd 쌍)과 카드(`children`)는 화면이 넘긴다. 노출만 바꾸는 PATCH 는 정의 리비전을
 * 올리지 않고 재배포만 한다.
 */
export function DeploymentTab({
  item,
  canDeploy,
  onDeploy,
  onSaved,
  emptyBody,
  hint,
  lines,
  children,
  saveExposure,
}: {
  item: DeployedItem;
  canDeploy: boolean;
  onDeploy: () => void;
  onSaved: () => unknown;
  emptyBody: ReactNode;
  hint: ReactNode;
  lines?: ReactNode;
  children?: ReactNode;
  saveExposure: (exposure: ExposureInput) => Promise<unknown>;
}) {
  const t = useT();
  if (item.state === "draft") return <DeploymentDraftCard canDeploy={canDeploy} onDeploy={onDeploy} body={emptyBody} />;
  return (
    <div className="flex flex-col gap-4">
      <Card data-testid="deployment-card">
        <CardHeader>
          <CardTitle>{t.ui.tabs.standard.deployment}</CardTitle>
          <span className="text-xs text-muted">{hint}</span>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-[9rem_1fr] gap-y-1 text-sm">
            <dt className="text-muted">{t.detail.deployable.labels.cluster}</dt>
            <dd data-testid="deployment-cluster">
              <ClusterLink id={item.cluster_id ?? null} testId="deployment-cluster-link" />
            </dd>
            <dt className="text-muted">{t.detail.deployable.labels.namespace}</dt>
            <dd className="font-mono text-xs">{item.k8s ? `${item.k8s.namespace}/${item.k8s.name}` : "-"}</dd>
            <dt className="text-muted">{t.detail.deployable.deployedRevision}</dt>
            <dd>r{item.deployed_revision ?? "?"}</dd>
            {lines}
          </dl>
        </CardContent>
      </Card>
      {children}
      {/* 노출 편집은 올라간 자리를 알아야 한다. 둘 중 하나라도 없으면 아직 그릴 것이 없다 */}
      {item.k8s && item.cluster_id ? (
        <ExposureCard clusterId={item.cluster_id} namespace={item.k8s.namespace} name={item.name} exposure={item.exposure} onSave={saveExposure} onSaved={onSaved} />
      ) : null}
    </div>
  );
}
