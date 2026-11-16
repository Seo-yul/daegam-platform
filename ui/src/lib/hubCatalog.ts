/**
 * 카탈로그 스냅숏(`GET /hub/catalog`)을 화면이 쓰는 꼴로 (P55 4·7·8절). 대화상자는 서버를 고를 수 있는지와 그 까닭을, 등록 폼은
 * 서버에서 채울 값과 플랫폼 관리자에게 보일 미러 작업의 입력을 여기서 얻는다. 무엇을 등록할 수 있는지는 서버가 정한다
 * (`needs_login`, `needs_credentials`, `import_header`, `mirrored_image`). 화면은 그 값을 읽기만 한다.
 */
import type { HubCreate } from "@/api/resources";
import type { HubCatalogServer } from "@/api/types";

export const CATALOG_KINDS = ["all", "image", "remote"] as const;
export type CatalogKind = (typeof CATALOG_KINDS)[number];

/**
 * 서버를 고를 수 없는 까닭. null 이면 고를 수 있다. 이미지는 허브 이미지 설정이 켜져 허브 사본의 참조가 있어야 하고,
 * 원격은 로그인이 필요 없고, 자격 증명이 필요하면 비밀 헤더 하나(`import_header`)로 가져올 수 있어야 한다(7절, P86 A3).
 */
export type CatalogBlock = "needsLogin" | "needsCredentials" | "admissionOff";

export function catalogBlock(server: HubCatalogServer, admission: boolean): CatalogBlock | null {
  if (server.kind === "image") return admission && server.mirrored_image ? null : "admissionOff";
  if (server.needs_login) return "needsLogin";
  if (server.needs_credentials && !server.import_header) return "needsCredentials";
  return null;
}

/**
 * 종류와 검색어로 거른 서버. 검색은 이름, 제목, 설명에서 대소문자를 가리지 않는다. 목록 화면의 `useListView` 와 같은 검색이지만
 * 대화상자는 쪽을 나누지 않아 거르기만 한다.
 */
export function filterCatalog(servers: readonly HubCatalogServer[], query: string, kind: CatalogKind): HubCatalogServer[] {
  const q = query.trim().toLowerCase();
  return servers.filter((s) => (kind === "all" || s.kind === kind) && (!q || `${s.name} ${s.title} ${s.description}`.toLowerCase().includes(q)));
}

/** env 칸의 한 줄. Secret env 줄의 값은 화면에 다시 보이지 않고 저장할 때만 간다. */
export interface EnvRow {
  name: string;
  value: string;
}

/** image 항목의 컨테이너 칸 (P55 5절). */
export interface ContainerForm {
  stdio: boolean;
  args: string[];
  env: EnvRow[];
  secretEnv: EnvRow[];
}

export const EMPTY_CONTAINER: ContainerForm = { stdio: false, args: [], env: [], secretEnv: [] };

/** 요청의 컨테이너 칸. */
export type ContainerBody = Pick<HubCreate, "stdio" | "args" | "env" | "secret_env">;

/**
 * 폼 → 요청의 컨테이너 칸. 이름이 빈 줄은 뺀다. 값이 빈 Env 줄은 보내지 않는다. 값이 빈 Secret env 줄은 이름만 보내고 값은 상세의
 * "Secret 값 바꾸기" 로 넣는다(5절).
 */
export function containerBody(form: ContainerForm): ContainerBody {
  const body: ContainerBody = {};
  if (form.stdio) body.stdio = true;
  const args = form.args.filter((a) => a.trim());
  if (args.length) body.args = args;
  const env = form.env.filter((e) => e.name.trim() && e.value).map((e) => ({ name: e.name.trim(), value: e.value }));
  if (env.length) body.env = env;
  const secrets = form.secretEnv.filter((e) => e.name.trim()).map((e) => (e.value ? { name: e.name.trim(), value: e.value } : { name: e.name.trim() }));
  if (secrets.length) body.secret_env = secrets;
  return body;
}

/** 등록 폼에 채울 값. kind 가 entry 면 원격을 비밀 헤더 하나의 entry 항목으로 가져오는 것이다 (P86 A3). */
export interface CatalogPrefill {
  kind: "image" | "remote" | "entry";
  name: string;
  displayName: string;
  image: string;
  port: string;
  container: ContainerForm;
  toolsFilter: string;
  url: string;
  transport: string;
  headerName: string;
  /** 요청의 `catalog`. 주소에 템플릿 변수가 있는 원격은 catalog 와 함께 만들 수 없어 null 이다 (4절). */
  catalog: string | null;
}

/**
 * 카탈로그 서버에서 등록 폼의 값. 이미지는 허브 사본의 참조를 쓰고, 선언한 변수는 비밀이면 Secret env 줄로, 아니면 기본값을 든 Env
 * 줄로 둔다. stdio 컨테이너는 포트를 받지 않는다. Tool 필터는 카탈로그의 Tool 목록이고 폼에서 고칠 수 있다.
 */
export function catalogPrefill(server: HubCatalogServer): CatalogPrefill {
  const base: CatalogPrefill = {
    kind: "remote",
    name: server.name,
    displayName: server.title,
    image: "",
    port: "",
    container: EMPTY_CONTAINER,
    toolsFilter: "",
    url: "",
    transport: server.transport,
    headerName: "",
    catalog: server.name,
  };
  if (server.kind === "image") {
    return {
      ...base,
      kind: "image",
      image: server.mirrored_image ?? "",
      port: server.port && !server.stdio ? String(server.port) : "",
      container: {
        stdio: server.stdio,
        args: [...server.args],
        env: server.env.filter((e) => !e.secret).map((e) => ({ name: e.name, value: e.default ?? "" })),
        secretEnv: server.env.filter((e) => e.secret).map((e) => ({ name: e.name, value: "" })),
      },
      toolsFilter: server.tools.join(", "),
    };
  }
  return {
    ...base,
    kind: server.import_header ? "entry" : "remote",
    url: server.url ?? "",
    headerName: server.import_header ?? "",
    catalog: server.url_template ? null : server.name,
  };
}

/**
 * 플랫폼 관리자가 미러 작업에 넣을 값 (6절, `make hub-mirror` 와 미러 job 의 입력). 출처가 없는 서버는 승인(HUB_APPROVE)이 있어야
 * 미러가 서명한다. 원격에는 없다.
 */
export function mirrorInputs(server: HubCatalogServer): string | null {
  if (server.kind !== "image" || !server.image) return null;
  const lines = [`HUB_UPSTREAM=${server.image.repository}:${server.image.tag}`, `HUB_SERVER=${server.name}`];
  if (!server.provenance) lines.push("HUB_APPROVE=yes");
  return lines.join("\n");
}

/** 카탈로그가 그 변수에 단 것: 설명과 꼭 필요한가. 폼의 줄 옆에 보인다. 카탈로그가 선언하지 않은 이름이면 null 이다. */
export function catalogVariable(server: HubCatalogServer | null | undefined, name: string): { description: string; required: boolean } | null {
  const found = server?.env.find((e) => e.name === name.trim());
  return found ? { description: found.description, required: found.required } : null;
}

/**
 * 요청에 카탈로그 서버의 이름을 실을 것인가 (5·7절). 서버가 받는 꼴일 때만 싣는다. 이미지는 허브 사본의 참조를 그대로 둔 image
 * 항목이다. 원격은 주소와 전송을 그대로 둔 remote 나 entry 항목이고, 비밀 헤더 하나로 가져오는 원격(P86 A3)은 그 헤더 이름의 header
 * 인증을 든 entry 항목이어야 한다. 이 밖이면 카탈로그 항목이 아니라 그냥 등록이다.
 */
export function catalogStillApplies(
  prefill: CatalogPrefill,
  form: { kind: string; image: string; url: string; transport: string; auth: string; headerName: string },
): boolean {
  if (!prefill.catalog) return false;
  if (prefill.kind === "image") return form.kind === "image" && form.image.trim() === prefill.image;
  if (form.kind !== "remote" && form.kind !== "entry") return false;
  if (form.url.trim() !== prefill.url || form.transport !== prefill.transport) return false;
  if (!prefill.headerName) return form.kind === "remote" || form.auth === "none";
  return form.kind === "entry" && form.auth === "header" && form.headerName.trim().toLowerCase() === prefill.headerName.toLowerCase();
}
