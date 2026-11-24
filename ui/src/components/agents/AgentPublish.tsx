/**
 * Agent 의 허브 게시 (P46 6.1절). 게시한 Agent 만 같은 테넌트의 다른 Agent 가 캔버스에서 Tool 로 붙인다.
 *  - 게시한 항목: 허브 목록에서 이 Agent 를 가리키는 `agent` 항목을 찾는다. Agent 응답은 항목을 싣지 않는다
 *  - 게시 대화상자: 이름과 정책은 MCP Server 의 게시와 같은 뼈대(PublishDialog)이고 Tool 설명과 호출을 믿는 선택이 더 있다.
 *    POST /agents/{id}/publish
 *  - 게시 해제 확인: DELETE /agents/{id}/publish. 이 항목을 Tool 로 쓰는 Agent 가 있어도 해제되므로 그 Agent 들을 먼저 보인다
 *
 * 게시는 턴을 받는 Agent 에만 된다. 단추를 그릴지는 상세 화면(pages/agents/Detail.tsx)이 상태와 권한을 보고 정한다.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { listHubItems, publishAgent, unpublishAgent } from "@/api/resources";
import type { Agent, HubItem } from "@/api/types";
import { Check } from "@/components/canvas/inspector/fields";
import { ErrorBody } from "@/components/ErrorMessage";
import { PublishDialog } from "@/components/PublishSection";
import { ReferencesNotice } from "@/components/ReferencesCard";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Field, Textarea } from "@/components/ui/input";
import { useT } from "@/i18n";
import { type AgentData, mainAgentNode } from "@/lib/agentGraph";
import { hubItemOfAgent } from "@/lib/hub";
import { hubItemsKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";

/** Tool 설명 길이의 위 끝 (서버 `store/hub.AGENT_DESCRIPTION_MAX`). */
const DESCRIPTION_MAX = 1024;
/** 게시와 해제가 끝나면 다시 읽는 목록. 항목이 생기거나 사라지고, 이 Agent 를 Tool 로 쓰는 Agent 의 상태 경고가 바뀐다. */
const PUBLISH_INVALIDATES = ["hub", "agents"] as const;

/** 이 Agent 를 게시한 허브 항목. 게시하지 않았거나 아직 목록을 받지 못했으면 undefined 다. */
export function useAgentHubItem(agentId: string): HubItem | undefined {
  const tenant = useTenant();
  const q = useQuery({ queryKey: hubItemsKey(tenant), queryFn: listHubItems, enabled: Boolean(agentId) });
  return hubItemOfAgent(q.data, agentId);
}

/** 게시 대화상자. Tool 설명은 메인 에이전트의 설명으로 채워 두고, 호출을 믿는 선택은 꺼 둔다. */
export function PublishAgentDialog({ agent, open, onClose }: { agent: Pick<Agent, "id" | "name" | "display_name" | "graph">; open: boolean; onClose: () => void }) {
  const t = useT();
  const text = t.agents.detail.publish;
  const [description, setDescription] = useState("");
  const [trusted, setTrusted] = useState(false);
  const mainDescription = (mainAgentNode(agent.graph.nodes)?.data as Partial<AgentData> | undefined)?.description ?? "";
  useEffect(() => {
    if (open) {
      setDescription(mainDescription);
      setTrusted(false);
    }
    // 폴링으로 Agent 객체가 바뀌어도 열려 있는 동안 입력을 지우지 않는다
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, agent.id]);

  return (
    <PublishDialog
      source={agent}
      open={open}
      onClose={onClose}
      lead={text.hint}
      placeholders={{ display: t.canvas.profile.displayPlaceholder, name: "support-agent" }}
      publish={(input) => publishAgent(agent.id, { ...input, description: description.trim(), calls_trusted: trusted })}
      invalidate={PUBLISH_INVALIDATES}
    >
      <Field label={text.description.label} htmlFor="publish-description" hint={text.description.hint} required>
        <Textarea
          id="publish-description"
          data-testid="publish-description"
          rows={3}
          className="font-sans"
          required
          maxLength={DESCRIPTION_MAX}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </Field>
      <Check id="publish-calls-trusted" label={text.trusted.label} hint={text.trusted.hint} checked={trusted} onChange={setTrusted} />
      <p className="text-xs text-muted" data-testid="publish-answer-notice">
        {text.answerNotice}
      </p>
    </PublishDialog>
  );
}

/** 게시 해제 확인. 이 항목을 Tool 로 쓰는 Agent 가 있어도 해제되므로 참조를 보내기 전에 보인다. */
export function UnpublishAgentDialog({ agentId, item, open, onClose }: { agentId: string; item: Pick<HubItem, "id" | "name"> | undefined; open: boolean; onClose: () => void }) {
  const text = useT().detail.publishSection;
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: () => unpublishAgent(agentId),
    onSuccess: async () => {
      for (const key of PUBLISH_INVALIDATES) await queryClient.invalidateQueries({ queryKey: [key] });
      onClose();
    },
  });
  const { reset } = remove;
  useEffect(() => {
    if (!open) reset();
  }, [open, reset]);

  return (
    <ConfirmDialog
      open={open && item !== undefined}
      title={text.unpublish}
      description={
        <>
          <p className="font-medium text-heading" data-testid="confirm-target">
            {item?.name}
          </p>
          <ReferencesNotice resource="hub" id={item?.id} open={open} testId="unpublish-references" />
        </>
      }
      confirmLabel={text.unpublish}
      pendingLabel={text.unpublishing}
      pending={remove.isPending}
      error={remove.error ? <ErrorBody error={remove.error} /> : null}
      onConfirm={() => remove.mutate()}
      onCancel={onClose}
    />
  );
}
