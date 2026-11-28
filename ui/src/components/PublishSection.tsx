import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router";

import { ApiError } from "@/api/client";
import { listGateways, publishMcpServer, unpublishMcpServer } from "@/api/resources";
import type { HubAccess, MCPServer } from "@/api/types";
import { AccessPolicyEditor } from "@/components/AccessPolicyEditor";
import { ErrorBody, ErrorMessage } from "@/components/ErrorMessage";
import { NameFields, isNameFieldError, nameFieldErrors, type NameValue } from "@/components/NameFields";
import { ReferencesNotice, useReferences } from "@/components/ReferencesCard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Modal } from "@/components/ui/modal";
import { useT } from "@/i18n";
import { hasScope, useAuthState } from "@/lib/auth";
import { accessForPublish, canShare, emptyAccess } from "@/lib/hubAccess";
import { displayName, resolveName } from "@/lib/names";
import { referenceNames } from "@/lib/references";
import { useTenant } from "@/lib/tenant";

/*
 * MCP Server 의 허브 게시 (docs/guide/hub-gateway.md 6절, D23). 상세 헤더와 목록 행이 같은 조각을 쓴다.
 *  - 게시 배지 + 허브 링크 (published-badge, hub-link)
 *  - 게시 다이얼로그: 이름(기본 리소스 이름) + 정책 편집기(접힘). POST /mcp-servers/{id}/publish
 *  - 게시 해제 확인: DELETE /mcp-servers/{id}/publish. 409 in_use 면 참조 중인 Gateway 를 링크로 안내
 *  - 삭제 확인의 참조 경고: hub_item.gateway_ids 가 있으면 이 서버가 사라질 때 Degraded 가 될 Gateway
 *
 * 게시는 배포된 MCP Server 에만 뜻이 있다 (D42). 허브 항목이 가리키는 것은 클러스터에 떠 있는 엔드포인트이고
 * `draft` 에는 그것이 없다. 버튼을 그릴지는 부르는 화면이 상태를 보고 정한다.
 */

/** 게시 버튼은 hub:write, 해제는 항목의 manage 판정이다. */
export function usePublishRights(server: Pick<MCPServer, "hub_item"> | null | undefined): { canPublish: boolean; canUnpublish: boolean } {
  const tenant = useTenant();
  const { me } = useAuthState();
  return {
    canPublish: hasScope(me, tenant, "hub:write"),
    canUnpublish: Boolean(server?.hub_item?.permissions.manage),
  };
}

/** 참조 중인 Gateway 를 이름 링크로. 목록을 아직 못 받았으면 id 그대로다. */
export function GatewayRefs({ ids, testId = "gateway-ref" }: { ids: string[]; testId?: string }) {
  const t = useT();
  const tenant = useTenant();
  const gateways = useQuery({ queryKey: ["gateways", tenant], queryFn: listGateways, enabled: ids.length > 0 });
  if (!ids.length) return null;
  return (
    <ul className="mt-1 flex flex-wrap gap-1">
      {ids.map((id) => (
        <li key={id}>
          <Link className="font-mono text-xs underline-offset-2 hover:underline" to={`/gateways/${id}`} data-testid={`${testId}-${id}`}>
            {resolveName(t, gateways.data, id)}
          </Link>
        </li>
      ))}
    </ul>
  );
}

/** 삭제 확인 대화상자에 끼우는 참조 경고. 게시 항목이 없거나 참조가 없으면 아무것도 그리지 않는다. */
export function McpServerDeleteWarning({ server }: { server: Pick<MCPServer, "hub_item"> | null | undefined }) {
  const t = useT();
  const ids = server?.hub_item?.gateway_ids ?? [];
  if (!ids.length) return null;
  return (
    <div className="rounded-chip border border-warn-line bg-warn-soft px-3 py-2 text-xs text-warn-fg" data-testid="delete-referenced-warning">
      {t.detail.publishSection.deleteWarning(ids.length)}
      <GatewayRefs ids={ids} testId="delete-referenced-gateway" />
    </div>
  );
}

/** 게시 배지와 허브 링크. 목록 행에서는 suffix 로 testid 를 나눈다. */
export function PublishedBadge({ server, suffix = "" }: { server: Pick<MCPServer, "hub_item">; suffix?: string }) {
  return <HubItemBadge item={server.hub_item} suffix={suffix} />;
}

/** 게시한 허브 항목의 배지와 링크. MCP Server 와 Agent 의 머리가 같이 쓴다. 게시하지 않았으면 아무것도 그리지 않는다. */
export function HubItemBadge({ item, suffix = "" }: { item: { id: string; name: string } | null | undefined; suffix?: string }) {
  const t = useT();
  if (!item) return null;
  return (
    <span className="inline-flex items-center gap-1.5">
      <Badge variant="ok" data-testid={`published-badge${suffix}`}>
        {t.detail.publishSection.published}
      </Badge>
      <Link className="text-xs underline-offset-2 hover:underline" to={`/hub/${item.id}`} data-testid={`hub-link${suffix}`}>
        {item.name}
      </Link>
    </span>
  );
}

/** 게시 대화상자가 보내는 이름과 정책. 이름을 비우면 원본의 이름을 쓴다. */
export interface PublishInput {
  name: string | null;
  display_name?: string;
  access: ReturnType<typeof accessForPublish>;
}

/**
 * 게시 대화상자의 뼈대. 이름 두 개는 원본의 것이 기본이고 정책 편집기는 접혀 있다. 원본에서 파생한 항목이라 다른 테넌트에는 열 수
 * 없다. MCP Server 와 Agent 의 게시가 같이 쓰고, 원본마다 다른 칸은 children 으로 이름 아래에 끼운다.
 */
export function PublishDialog({
  source,
  open,
  onClose,
  lead,
  namePreview,
  placeholders,
  nameHint,
  publish,
  invalidate,
  children,
}: {
  source: { id: string; name: string; display_name?: string | null } | null;
  open: boolean;
  onClose: () => void;
  /** 대화상자의 첫 줄. */
  lead: ReactNode;
  /** 이름 칸 아래에 보이는 클러스터 리소스 이름의 머리. 항목이 CR 을 갖지 않으면 없다. */
  namePreview?: { prefix: string };
  /** 빈 이름 칸의 예시와 이름 칸 아래의 풀이. 원본의 종류를 말하므로 부르는 쪽이 준다. */
  placeholders: { display: string; name: string };
  nameHint?: string;
  publish: (input: PublishInput) => Promise<unknown>;
  /** 게시가 끝나면 무효화하는 쿼리 키의 머리. */
  invalidate: readonly string[];
  children?: ReactNode;
}) {
  const t = useT();
  const text = t.detail.publishSection;
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const { me } = useAuthState();
  const [names, setNames] = useState<NameValue>({ displayName: "", name: "" });
  const [access, setAccess] = useState<HubAccess>(emptyAccess());
  useEffect(() => {
    if (open) {
      setNames(source ? { displayName: displayName(source), name: source.name } : { displayName: "", name: "" });
      setAccess(emptyAccess());
    }
    // 폴링으로 source 객체가 바뀌어도 열려 있는 동안 입력을 지우지 않게 이름만 본다
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, source?.id, source?.name, source?.display_name]);

  const mutation = useMutation({
    mutationFn: () =>
      publish({
        name: names.name.trim() || null,
        ...(names.displayName.trim() ? { display_name: names.displayName.trim() } : {}),
        access: accessForPublish(access),
      }),
    onSuccess: async () => {
      for (const key of invalidate) await queryClient.invalidateQueries({ queryKey: [key] });
      onClose();
    },
  });
  const { reset } = mutation;
  useEffect(() => {
    if (!open) reset();
  }, [open, reset]);

  return (
    <Modal open={open && source !== null} title={text.publishTitle} testId="publish-dialog" closeTestId="publish-dialog-close" onClose={onClose}>
      <form
        className="flex flex-col gap-3 px-5 py-4"
        data-testid="publish-form"
        onSubmit={(e) => {
          e.preventDefault();
          mutation.mutate();
        }}
      >
        <p className="text-sm text-muted">{lead}</p>
        <NameFields testId="publish" placeholders={placeholders} value={names} onChange={setNames} preview={namePreview} required={false} inline errors={nameFieldErrors(mutation.error)} />
        {nameHint ? <p className="text-xs text-muted">{nameHint}</p> : null}
        {children}
        <AccessPolicyEditor value={access} onChange={setAccess} canShare={canShare(me, tenant, "hub:share")} crossTenant={false} collapsible testId="access" />
        {!isNameFieldError(mutation.error) ? <ErrorMessage error={mutation.error} testId="publish-error" /> : null}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={mutation.isPending}>
            {t.common.cancel}
          </Button>
          <Button type="submit" data-testid="publish-submit" disabled={mutation.isPending}>
            {mutation.isPending ? text.publishing : text.publish}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

/** MCP Server 의 게시 대화상자. 허브 항목 CR 은 `hub-<name>` 이다. */
export function PublishMcpServerDialog({ server, open, onClose }: { server: Pick<MCPServer, "id" | "name" | "display_name"> | null; open: boolean; onClose: () => void }) {
  const text = useT().detail.publishSection;
  const name = server ? displayName(server) : "";
  return (
    <PublishDialog
      source={server}
      open={open}
      onClose={onClose}
      lead={text.lead(name, <span className="font-medium text-heading">{name}</span>)}
      namePreview={{ prefix: "hub" }}
      placeholders={text.placeholders}
      nameHint={text.nameHint}
      publish={(input) => publishMcpServer(server!.id, input)}
      invalidate={["mcp-servers", "hub"]}
    />
  );
}

/** 게시 해제 확인. 409 in_use 면 참조 중인 Gateway 를 링크로 보이고 대화상자는 열어 둔다. */
export function UnpublishMcpServerDialog({ server, open, onClose }: { server: Pick<MCPServer, "id" | "name" | "hub_item"> | null; open: boolean; onClose: () => void }) {
  const t = useT();
  const text = t.detail.publishSection;
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: () => unpublishMcpServer(server!.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });
      await queryClient.invalidateQueries({ queryKey: ["hub"] });
      onClose();
    },
  });
  const { reset } = remove;
  useEffect(() => {
    if (!open) reset();
  }, [open, reset]);
  const inUse = remove.error instanceof ApiError && remove.error.code === "in_use" ? (remove.error.extra<string[]>("gateway_ids") ?? []) : [];
  // 해제를 막는 것은 허브 항목을 쓰는 Gateway 다. 409 를 기다리지 않고 먼저 보인다 (D43)
  const itemId = server?.hub_item?.id ?? "";
  const names = referenceNames(useReferences("hub", itemId, open && Boolean(itemId)).data ?? []);

  return (
    <ConfirmDialog
      open={open && server !== null}
      title={text.unpublish}
      description={
        <>
          <p className="font-medium text-heading" data-testid="confirm-target">
            {server?.hub_item?.name ?? server?.name}
          </p>
          <p>{text.unpublishInternal}</p>
          <ReferencesNotice resource="hub" id={itemId} open={open} testId="unpublish-references" />
        </>
      }
      confirmLabel={text.unpublish}
      pendingLabel={text.unpublishing}
      pending={remove.isPending}
      error={
        remove.error ? (
          <div data-testid="unpublish-error">
            <ErrorBody error={remove.error} names={names} />
            {inUse.length ? (
              <div className="mt-1">
                {text.inUse}
                <GatewayRefs ids={inUse} testId="unpublish-ref" />
              </div>
            ) : null}
          </div>
        ) : null
      }
      onConfirm={() => remove.mutate()}
      onCancel={onClose}
    />
  );
}
