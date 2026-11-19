/**
 * 카탈로그 대화상자 (P55 8절). 허브 목록의 "카탈로그에서 가져오기" 가 연다.
 *
 * 이 플랫폼 판과 함께 실린 스냅숏(`GET /hub/catalog`)을 검색과 종류로 거른다. 서버마다 제목, 설명, tier, 종류를 보이고, 이미지는
 * 출처와 아키텍처를, 원격은 7절의 표시를 단다. 고를 수 있는 서버를 누르면 `/hub/new?catalog=<이름>` 이 그 서버의 값으로 채워져
 * 열린다. 고를 수 없는 서버는 흐리게 까닭을 달고(`lib/hubCatalog` 의 `catalogBlock`), 비밀 헤더 하나로 가져오는 원격은 그 안내를
 * 단다(P86 A3). 플랫폼은 등록 전에 미러를 찾아보지 않는다.
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router";

import { getHubCatalog } from "@/api/resources";
import type { HubCatalogServer } from "@/api/types";
import { SearchInput } from "@/components/ListControls";
import { queryGate } from "@/components/QueryGate";
import { Badge } from "@/components/ui/badge";
import { Modal } from "@/components/ui/modal";
import { TabBar } from "@/components/ui/tabs";
import { type Messages, useT } from "@/i18n";
import { hubKindLabel, hubKindVariant } from "@/lib/hub";
import { CATALOG_KINDS, type CatalogBlock, type CatalogKind, catalogBlock, filterCatalog } from "@/lib/hubCatalog";
import { HUB_CATALOG_KEY } from "@/lib/queryKeys";
import { cn, ownValue } from "@/lib/utils";

export function CatalogDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  // 몸은 열릴 때만 그린다. 다시 열면 검색어와 종류가 처음으로 돌아간다
  return (
    <Modal open={open} title={t.hub.catalog.title} testId="hub-catalog-dialog" closeTestId="hub-catalog-close" onClose={onClose}>
      <CatalogBody />
    </Modal>
  );
}

/** 고를 수 없는 까닭의 한 줄. */
function blockText(t: Messages, block: CatalogBlock): string {
  const text = t.hub.catalog;
  const lines: Record<CatalogBlock, string> = {
    sseOnly: text.sseOnlyHint,
    needsLogin: text.needsLoginHint,
    needsCredentials: text.needsCredentialsHint,
    admissionOff: text.admissionOff,
  };
  return lines[block];
}

function CatalogBody() {
  const t = useT();
  const text = t.hub.catalog;
  const navigate = useNavigate();
  const q = useQuery({ queryKey: HUB_CATALOG_KEY, queryFn: getHubCatalog });
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<CatalogKind>("all");
  const gate = queryGate(q, { testId: "hub-catalog-error" });
  if (gate) return <div className="px-5 py-4">{gate}</div>;
  const catalog = q.data!;
  const shown = filterCatalog(catalog.servers, query, kind);
  return (
    <div className="flex min-h-0 flex-col gap-3 px-5 py-4">
      <p className="text-xs text-muted" data-testid="hub-catalog-hint">
        {text.hint}
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <SearchInput value={query} onChange={setQuery} placeholder={text.search} testId="hub-catalog-search" />
        <TabBar tabs={CATALOG_KINDS} value={kind} onChange={setKind} label={(k) => text.kinds[k]} />
      </div>
      {shown.length ? (
        <ul className="max-h-[55vh] divide-y divide-line overflow-y-auto rounded-tile border border-line" data-testid="hub-catalog-list">
          {shown.map((server) => (
            <CatalogRow key={server.name} server={server} block={catalogBlock(server, catalog.admission)} onPick={() => navigate(`/hub/new?catalog=${encodeURIComponent(server.name)}`)} />
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted" data-testid="hub-catalog-empty">
          {/* 검색어 없이 비면 그 종류의 서버가 없는 것이다 */}
          {query.trim() ? text.noMatch(query.trim()) : text.empty}
        </p>
      )}
    </div>
  );
}

function CatalogRow({ server, block, onPick }: { server: HubCatalogServer; block: CatalogBlock | null; onPick: () => void }) {
  const t = useT();
  const text = t.hub.catalog;
  const testId = `hub-catalog-${server.name}`;
  return (
    <li className={cn("flex flex-col gap-1 px-3 py-2", block && "opacity-60")} data-testid={testId} data-blocked={block ?? undefined}>
      <div className="flex flex-wrap items-center gap-2">
        {block ? (
          <span className="text-sm font-semibold text-heading">{server.title}</span>
        ) : (
          <button type="button" className="text-left text-sm font-semibold text-link underline-offset-2 hover:underline" data-testid={`${testId}-pick`} onClick={onPick}>
            {server.title}
          </button>
        )}
        <span className="font-mono text-xs text-muted">{server.name}</span>
        <Badge variant="muted">{ownValue(text.tier, server.tier) ?? server.tier}</Badge>
        {/* 허브 목록과 같은 종류 배지다 */}
        <Badge variant={hubKindVariant(server.kind)}>{hubKindLabel(t, server.kind)}</Badge>
        {server.kind === "image" ? (
          <Badge variant={server.provenance ? "ok" : "muted"} data-testid={`${testId}-provenance`}>
            {server.provenance ? text.provenance.available : text.provenance.none}
          </Badge>
        ) : null}
        {server.needs_login ? <Badge variant="warn">{text.needsLogin}</Badge> : null}
        {server.needs_credentials ? <Badge variant="warn">{text.needsCredentials}</Badge> : null}
      </div>
      {server.description ? <p className="line-clamp-2 text-xs text-muted">{server.description}</p> : null}
      {server.kind === "image" && server.platforms.length ? (
        <p className="text-xs text-muted" data-testid={`${testId}-platforms`}>
          {text.platforms} <span className="font-mono">{server.platforms.join(", ")}</span>
        </p>
      ) : null}
      {block ? (
        <p className="text-xs text-warn-fg" data-testid={`${testId}-reason`}>
          {blockText(t, block)}
        </p>
      ) : server.import_header ? (
        <p className="text-xs text-muted" data-testid={`${testId}-header`}>
          {text.headerImportHint(server.import_header)}
        </p>
      ) : null}
    </li>
  );
}
