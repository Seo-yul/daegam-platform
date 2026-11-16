import type { GateReason } from "@/lib/codeExecution";
import { particle } from "@/lib/particle";
import type { MissingPrefix } from "@/pages/clusters/Detail";

/** Cluster 의 목록, 등록, 상세 (F6). 클러스터는 플랫폼 수준 리소스이고 쓰기는 platform-admin 만 한다. */
export const clusters = {
  /** cluster:write 가 없는 사람에게 보이는 말(pages/clusters/permission.ts). 세 화면이 같이 쓴다. */
  readOnly: {
    /** 목록·상세·등록 화면 위의 안내. */
    notice: "클러스터는 보기만 할 수 있습니다. 등록·수정·삭제는 플랫폼 관리자에게 요청해 주세요.",
    /** 비활성화한 버튼의 title. 왜 못 누르는지를 그 자리에서 말한다. */
    title: "클러스터 등록·수정은 플랫폼 관리자만 할 수 있습니다.",
  },
  /** /clusters. Preflight·Kubernetes·ToolHive 칸은 이름 그대로다. */
  list: {
    register: "Cluster 등록",
    columns: { name: "이름", connection: "연결" },
    noMatch: (query: string): string => `"${query}"${particle(query, "과/와")} 맞는 클러스터가 없습니다.`,
    emptyWritable: "등록된 클러스터가 없습니다. 위의 Cluster 등록 버튼으로 추가해 주세요.",
    emptyReadOnly: "등록된 클러스터가 없습니다. 플랫폼 관리자에게 등록을 요청해 주세요.",
    title: "Clusters",
    search: "Search clusters…",
    home: "home",
  },
  /** /clusters/new. */
  form: {
    backToList: "Cluster 목록으로 돌아가기",
    placeholders: { display: "운영 클러스터", name: "prod-cluster" },
    nameHint: "'home'은 플랫폼이 자동 등록하는 이름이라 사용할 수 없습니다.",
    platformUrlHint: "이 클러스터의 파드가 플랫폼 API 에 접근하는 주소입니다.",
    kubeconfigFileHint: "이 클러스터에 접속할 때 쓰는 kubeconfig 입니다. 내용은 K8s Secret 에 들어가고 MongoDB 에는 이름만 남습니다.",
    kubeconfigHint: "파일을 업로드하거나 붙여 넣어 주세요. 플랫폼의 Secret 에 안전하게 저장됩니다.",
    registering: "등록 중… (preflight)",
    register: "등록",
    title: "Register Cluster",
    platformUrl: "Platform internal URL",
    kubeconfigFile: "kubeconfig 파일",
    loaded: (name: string): string => `${name} 불러옴`,
    kubeconfig: "kubeconfig",
  },
  /** 상세의 머리와 preflight 카드, 참조 카드. */
  detail: {
    /** 없는 클러스터를 열었을 때 NotFoundPage 의 문장에 들어가는 이름. */
    resource: "Cluster",
    rerunning: "실행 중…",
    rerun: "preflight 다시 실행",
    /** 오퍼레이터가 사용량 지표를 클러스터 밖으로 보내는가 (P50 3절). 켜짐은 경고 색이다. */
    usageMetrics: { on: "켜짐", off: "꺼짐" },
    /** NetworkPolicy 를 누가 집행하는가. controller 는 찾은 컨트롤러 이름이다. */
    networkPolicy: {
      enforced: (controller: string): string => `집행됨 (${controller})`,
      presumed: (controller: string): string => `집행 추정 (${controller})`,
      none: "집행하는 컨트롤러 없음",
    },
    presumedNote:
      "k3s 는 NetworkPolicy 를 k3s 안에서 집행하므로 컨트롤러 파드가 보이지 않습니다. 집행한다고 추정했지만 k3s 를 정책 없이 띄웠을 수도 있습니다. 실제로 막히는지 확인한 뒤 플랫폼 설정 DAEGAM_HOME_NETWORK_POLICY 로 적으면 추정이 아니라 확정으로 보입니다.",
    noControllerNote:
      "이 클러스터에는 NetworkPolicy 를 집행하는 컨트롤러가 없습니다. 플랫폼이 만드는 NetworkPolicy 는 여기서 아무것도 막지 않습니다. 플랫폼은 이것을 보호 수단으로 보지 않으므로 설치와 배포는 그대로 됩니다.",
    /**
     * tenant Secrets 줄 (P63). 플랫폼이 관리하는 네임스페이스마다 RoleBinding daegam-tenant-objects 가 있는가.
     * 없는 곳의 이름은 cluster:write 에게만 온다. clusterWideNote 는 플랫폼 계정이 아직 클러스터 전체의 Secret 을 읽을 때의
     * 안내이고 이 릴리스의 RBAC 파일을 적용하면 사라진다. unboundNote 는 클러스터 전체 권한 없이 RoleBinding 이 빠진
     * 네임스페이스가 있을 때다.
     */
    secretRights: {
      bound: (count: number): string => `네임스페이스 ${count}개 모두 RoleBinding 있음`,
      unbound: (unbound: number, total: number): string => `네임스페이스 ${total}개 중 ${unbound}개에 RoleBinding 없음`,
      noNamespaces: "아직 관리하는 네임스페이스 없음",
      roleWithoutSecrets: "daegam-tenant-objects 에 Secret 권한 없음",
      clusterWideNote:
        "플랫폼 계정은 아직 이 클러스터의 모든 네임스페이스에서 Secret 을 읽을 수 있습니다. 위 항목에서 모든 네임스페이스에 RoleBinding 이 있는지 확인한 뒤 이 릴리스의 RBAC 파일을 적용하면 이 권한이 없어집니다.",
      unboundNote:
        "이 클러스터는 플랫폼 계정에 클러스터 전체 Secret 권한을 주지 않으므로, RoleBinding 이 없는 네임스페이스에서는 플랫폼이 Secret 을 읽거나 쓸 수 없습니다. 그 네임스페이스에 배포하거나 플랫폼이 주기적으로 RoleBinding 을 만든 뒤 사전 점검을 다시 실행해 주세요.",
    },
    /** home 이 함께 올린 Redis 의 TLS 서버 인증서가 30일 안에 끝날 때 (P61). date 는 끝나는 때다 */
    shippedRedisCertNote: (date: string): string =>
      `플랫폼과 함께 배포한 Redis 의 TLS 서버 인증서가 ${date}에 만료됩니다. 이 설치에 사용한 배포 절차로 인증서를 갱신해 주세요. CA 도 곧 만료되면 CA 를 먼저 교체해 주세요.`,
    notRun: "preflight 를 아직 실행하지 않았습니다.",
    referencesTitle: "이 클러스터에 배포된 것",
    referencesHint: "지금 테넌트의 것만 보입니다.",
    /**
     * preflight 의 `missing` 을 접두사별로 묶어 "무엇을 하면 되는지" 로 바꾼 한 문장(groupMissing). 원문은 접어 둔다.
     * other 는 알려진 접두사가 없는 줄이다.
     */
    missing: {
      connect: "클러스터에 연결할 수 없습니다. kubeconfig 의 server 주소와 인증서를 확인해 주세요.",
      crd: "이 클러스터에 ToolHive CRD 가 없습니다. ToolHive 오퍼레이터를 먼저 설치해 주세요.",
      operator: "ToolHive 오퍼레이터가 돌고 있지 않습니다. 먼저 설치해 주세요.",
      rbac: "서비스 어카운트 권한이 모자랍니다. 이 클러스터에 대감 차트를 target: remote 로 설치하거나 업그레이드해 주세요.",
      other: "preflight 가 통과하지 못한 항목입니다.",
    } satisfies Record<MissingPrefix, string>,
    missingCount: (count: number): string => `모자란 항목 ${count}개`,
    rawLines: "원문 보기",
    /** RBAC 검사 결과 표. */
    rbac: {
      deniedOnly: (denied: number, total: number): string => `막힌 권한만 보기 (${denied}/${total})`,
      columns: { group: "API 그룹", resource: "리소스", verb: "동작", namespace: "네임스페이스", allowed: "허용" },
      coreGroup: "core",
    },
    /** 환경(DAEGAM_HOME_*)이 정하는 칸. fields 는 ", " 로 이은 필드 경로다. 두 편집 카드가 같이 쓴다. */
    envManaged: (fields: string): string =>
      `이 칸은 플랫폼 설정(DAEGAM_HOME_*)이 정하고 기동할 때마다 다시 저장합니다. 화면에서는 고칠 수 없고, 바꾸려면 배포의 ConfigMap 을 고쳐 주세요: ${fields}`,
    /** Summary·Preflight 카드의 칸 이름. */
    labels: {
      connection: "connection",
      secret: "secret",
      platformUrl: "platform internal url",
      checked: "checked",
      usageMetrics: "usage metrics",
      networkPolicy: "NetworkPolicy",
      secretRights: "tenant Secrets",
    },
  },
  /** 노출 기본값 카드(ClusterIngress). 칸 이름은 필드 이름 그대로다. */
  ingress: {
    hint: "배포 폼의 기본값과 허용 범위입니다. 이미 배포된 것은 자기 노출 스냅샷을 유지합니다.",
    enabled: "외부 노출 허용",
    allowUnauthenticated: "무인증 노출 허용",
    allowUnauthenticatedNote: "(header 인증 모드에서 노출하려면 켜야 합니다. 운영 클러스터는 끕니다)",
    classHint: "이 클러스터에서 노출을 만들 때 미리 선택해 두는 IngressClass 입니다.",
    allowedClassesHint: "쉼표로 구분합니다. 비우면 클러스터의 모든 IngressClass 를 허용합니다.",
    domainHint: "host 기본값 <name>.<tenant>.<domain> 의 도메인입니다.",
    tlsHint: "노출 폼의 TLS 기본 모드입니다. edge 는 Ingress 앞의 프록시가 TLS 를 끝내는 클러스터이고, 그 클러스터의 노출은 edge 만 됩니다.",
    tlsEdge: "edge (앞단에서 종단)",
    issuerHint: "cert-manager 모드의 기본 ClusterIssuer 입니다.",
    controllerNamespaceHint: "Ingress 컨트롤러 파드의 네임스페이스입니다. 노출 NetworkPolicy 가 여기서 오는 트래픽을 허용합니다.",
    ipBlocksHint: "hostNetwork 컨트롤러용 CIDR 목록입니다. 쉼표로 구분합니다.",
    reservedHostsHint: "배포한 것이 쓸 수 없는 host 입니다. 플랫폼 자신의 Ingress host 가 여기 들어갑니다.",
    annotationsHint: "한 줄에 하나씩 key: value 로 적습니다. 렌더링되는 모든 Ingress 에 깔립니다.",
    /** 콜론이 없는 annotation 줄. line 은 그 줄이다. */
    annotationLine: (line: string): string => `annotation 줄은 "key: value" 여야 합니다: ${line}`,
    example: "nginx 계열 예시입니다. streamable-http 세션이 오래 열리므로 프록시 타임아웃을 늘립니다.",
    applyExample: "예시 넣기",
    save: "노출 기본값 저장",
    title: "Exposure defaults (Ingress)",
    classPlaceholder: "nginx",
    allowedClasses: "Allowed IngressClasses",
    domain: "Domain",
    tls: "Default TLS",
    tlsNone: "none",
    tlsSecret: "secret",
    tlsCertManager: "cert-manager",
    issuer: "Default ClusterIssuer",
    controllerNamespace: "Controller namespace",
    controllerNamespacePlaceholder: "ingress-nginx",
    ipBlocks: "Controller IP blocks",
    reservedHosts: "Reserved hosts",
    annotations: "Ingress annotations",
    /** Ingress 카드의 칸 이름. */
    labels: {
      ingressClass: "Default IngressClass",
    },
  },
  /** 인스턴스 메타데이터 차단 카드 (T75). 켜면 테넌트 네임스페이스마다 egress 정책을 둔다. */
  metadata: {
    title: "Instance metadata",
    enabled: "테넌트 파드가 인스턴스 메타데이터 주소(169.254.169.254)에 닿지 못하게 막기",
    hint: "노드가 클라우드 VM 이고 그 주소에서 노드 메타데이터를 내주는 클러스터(OCI 등)에서 켭니다. 파드가 그 주소에서 자격 증명을 받는 클러스터(GKE Workload Identity, kube2iam, kiam 등)에서는 켜지 않습니다.",
    save: "메타데이터 차단 저장",
  },
  /** 배포 기본값 카드(JSON 편집). */
  defaults: {
    /** default_redis 의 TLS 끝점(P61)을 JSON 으로 적는 법. */
    redisTlsHint:
      "default_redis 에 tls(port 와 PEM 형식의 ca)를 넣으면 ToolHive 0.52 이상에서 MCP Server 의 세션 저장소가 그 TLS 포트로 접속합니다. address 에는 평문 포트를 그대로 두면 됩니다. CA 는 테넌트 네임스페이스마다 Secret daegam-redis-ca 로 복사됩니다. CA 복사와 TLS 사용에는 테넌트 객체 권한이 필요합니다.",
    save: "기본값 저장",
    title: "Deployment defaults",
  },
  /** preflight 결과. 목록과 상세가 같이 쓴다. */
  preflight: {
    ok: "ok",
    failed: "failed",
    notRun: "실행 전",
    title: "Preflight",
    kubernetes: "Kubernetes",
    toolhive: "ToolHive",
  },
  /**
   * 코드 실행 격리 카드(P40 8절). 점검, 결과, 용량과 메모리 한도 없이 센 파드, 노드를 함께 쓰는 위험과 그 허용이다. reason 은
   * 게이트가 닫힌 까닭이고 캔버스, Agent 경고, 배포의 409 가 같은 문장을 쓴다.
   */
  sandbox: {
    title: "코드 실행 격리",
    check: "격리 점검",
    passed: (time: string): string => `통과 (${time})`,
    failedNetwork: "NetworkPolicy 가 실제로 연결을 막지 않습니다.",
    failedUserns: "user namespace 를 쓰는 파드가 시작되지 않습니다.",
    failedSelfCheck: "샌드박스 자체 점검에서 막혀야 할 연결이 열려 있었습니다. 원인을 고친 뒤 다시 점검해 주세요.",
    couldNotRun: (reason: string): string => `점검을 실행하지 못했습니다 (${reason}).`,
    voided: "점검 뒤 클러스터가 바뀌어 다시 점검해야 합니다.",
    capacity: (count: number): string => `이 클러스터에서 동시에 쓸 수 있는 작업 공간: ${count}개`,
    assumedLimits: (count: number): string => `메모리 한도가 없는 파드 ${count}개는 요청값이나 기본값으로 계산했습니다.`,
    sharedNodes: "샌드박스가 플랫폼이나 Agent 와 같은 노드에서 실행됩니다. 커널 취약점은 이 격리로 막지 못하므로, 운영 환경에서는 샌드박스 전용 노드를 두세요.",
    acceptSharedNodes: "샌드박스가 플랫폼이나 Agent 와 노드를 함께 쓰는 위험을 알고 코드 실행을 허용합니다.",
    accepted: (name: string, time: string): string => `${name} 님이 ${time}에 허용했습니다.`,
    reason: {
      unchecked: "이 클러스터는 격리 점검을 아직 하지 않았거나 통과하지 못해 코드 실행을 쓸 수 없습니다. 플랫폼 관리자에게 클러스터 화면에서 격리 점검을 요청해 주세요.",
      unaccepted:
        "이 클러스터는 샌드박스가 다른 워크로드와 노드를 함께 쓰는데 플랫폼 관리자가 그 위험을 아직 허용하지 않아 코드 실행을 쓸 수 없습니다. 플랫폼 관리자에게 클러스터 화면에서 허용을 요청해 주세요.",
      rights: "플랫폼에 이 클러스터에서 작업 공간을 만들 권한이 없어 코드 실행을 쓸 수 없습니다. 플랫폼 관리자에게 알려 주세요.",
      capacity: "이 클러스터의 노드 메모리로는 작업 공간을 하나도 열 수 없어 코드 실행을 쓸 수 없습니다.",
    } satisfies Record<GateReason, string>,
  },
};
