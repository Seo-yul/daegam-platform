import type { ReactNode } from "react";

import { particle } from "@/lib/particle";

/** 테넌트 설정 (/tenant/settings). 칸 이름과 카드 제목은 두 언어 모두 영어다(D36). */
export const tenant = {
  settings: {
    /** tenant 는 테넌트 이름을 감싼 요소이고 link 는 Platform Settings 로 가는 링크다. */
    scopeHint: (tenant: ReactNode, link: ReactNode): ReactNode => (
      <>
        이 설정은 테넌트 {tenant}에만 적용됩니다. 비운 항목은{" "}
        {link}의 기본값을 따릅니다.
      </>
    ),
    readOnly: "이 테넌트의 관리자가 아니라 보기만 할 수 있습니다.",
    hints: {
      cluster: "비우면 Platform Settings 의 기본 클러스터를 사용합니다.",
      namespace: "비우면 클러스터·플랫폼의 namespace template 을 따릅니다.",
      /** 네임스페이스를 옮기는 것은 settings:write 다(D40). 그 권한이 없을 때의 풀이다. */
      namespaceLocked: "테넌트가 배포되는 자리를 옮기는 값이라 플랫폼 관리자만 바꿉니다.",
      labels: "테넌트 네임스페이스에 붙일 라벨입니다. JSON 객체로 적습니다.",
      quota: "테넌트 네임스페이스에 걸 ResourceQuota 의 spec.hard 입니다. JSON 객체로 적고 비우면 만들지 않습니다.",
      bindings: "MCP Server 와 Agent 의 정의에 바인딩이 없을 때 쓰는 Database 입니다.",
    },
    title: (tenant: string): string => `Tenant Settings: ${tenant}`,
    platformSettings: "Platform Settings",
    clustersTitle: "Clusters",
    updated: "updated",
    defaultCluster: "Default cluster",
    platformDefault: "(플랫폼 기본값)",
    restrictClusters: "Restrict allowed clusters",
    namespaceTitle: "Namespace and quota",
    namespaceName: "Namespace name",
    namespaceLabels: "Namespace labels (JSON)",
    quota: "ResourceQuota spec.hard (JSON)",
    bindingsTitle: "Default bindings",
  },
  /** Members 카드. manual 소속만 고친다. */
  members: {
    add: "멤버 직접 추가",
    save: "멤버 저장",
    columns: { subject: "주체", role: "역할", source: "출처" },
    pickUser: "(사용자를 선택하세요)",
    empty: "멤버가 없습니다.",
    /** 저장 전에 걸린 manual 소속의 문제. */
    formError: {
      subRequired: "주체를 입력해 주세요.",
      duplicate: (sub: string): string => `주체 ${sub}${particle(sub, "이/가")} 두 번 있습니다.`,
    },
    title: "Members",
    synced: "그룹에서 동기화됨",
    manual: "직접 추가함",
  },
  /** Service accounts 카드 (P53b 6.7절). 사람 없이 MCP 를 부르는 프로그램의 계정과 그 토큰이다. */
  serviceAccounts: {
    title: "Service accounts",
    help: "CI 처럼 사람 없이 MCP 를 부르는 프로그램에 주는 계정입니다. 이 계정의 토큰으로 이 테넌트의 Gateway, 노출한 MCP Server, 노출한 Agent 를 부릅니다. Kubernetes 의 서비스 어카운트와는 다른 대감의 계정입니다.",
    new: "새 서비스 어카운트",
    empty: "서비스 어카운트가 없습니다.",
    roleHint: "이 계정의 토큰에 실리는 역할입니다. Tool 규칙은 이 역할로 판정하고, 내 역할보다 높게 선택할 수 없습니다.",
    deleteBody: "이 계정의 토큰이 모두 회수되고, 그 토큰을 넣은 클라이언트는 16분 안에 거절됩니다. 지운 계정은 되살릴 수 없습니다.",
    /** DAEGAM_SERVICE_ACCOUNTS=off. 카드는 계정을 쓸 수 없는 채로 보인다. */
    off: "이 배포본에서는 서비스 어카운트를 사용하지 않습니다. 남아 있는 계정의 토큰은 모두 회수되었고 새 토큰을 만들 수 없습니다. 다시 사용하려면 플랫폼 관리자에게 문의해 주세요.",
    /** DAEGAM_SERVICE_ACCOUNTS=platform-admin 일 때 테넌트 관리자에게 보인다. */
    platformAdminOnly: "이 배포본에서는 플랫폼 관리자만 서비스 어카운트와 그 토큰을 만듭니다. 이미 만든 토큰은 그대로 사용할 수 있습니다.",
  },
};
