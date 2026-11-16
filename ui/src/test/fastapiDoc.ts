/**
 * 플랫폼(FastAPI)이 내는 OpenAPI 3.1 문서의 꼴을 줄여 담은 픽스처 (P59 F2). 전체 문서(`ui/openapi.json`)는 `make ui-types`
 * 가 만들고 git 이 무시하므로, 뷰어가 다뤄야 하는 꼴을 하나씩 여기에 둔다. 무엇을 왜 담았는지는
 * docs/archive/reviews/2026-10-16-p59-f0.md 에 있다.
 *
 * - `Optional[X]`: `anyOf: [X, {type: null}]` (문자열, `$ref`, 배열, 자기 자신을 가리키는 `$ref`, enum 인 질의 파라미터)
 * - 갈래가 여럿인 anyOf: `$ref` 둘, 객체·문자열·null, 문자열·정수 배열의 요소
 * - `const`, 파일 업로드의 `contentMediaType`, 키를 enum 으로 묶은 `propertyNames`
 * - 태그가 없는 오퍼레이션
 */
import type { OpenApiDocument } from "@/api/types";

const ref = (name: string) => ({ $ref: `#/components/schemas/${name}` });
const optional = (schema: Record<string, unknown>, extra: Record<string, unknown> = {}) => ({
  anyOf: [schema, { type: "null" }],
  ...extra,
});

const validationError = {
  "422": {
    description: "Validation Error",
    content: { "application/json": { schema: ref("HTTPValidationError") } },
  },
};

export const fastapiDoc: OpenApiDocument = {
  openapi: "3.1.0",
  info: { title: "daegam", version: "0.1.0" },
  paths: {
    "/api/clusters": {
      get: {
        tags: ["clusters"],
        summary: "List Clusters",
        operationId: "list_clusters_api_clusters_get",
        parameters: [
          {
            name: "state",
            in: "query",
            required: false,
            schema: optional({ enum: ["draft", "deployed"], type: "string" }, { title: "State" }),
            description: "Filter by state",
          },
        ],
        responses: {
          "200": {
            description: "Successful Response",
            content: { "application/json": { schema: { type: "array", items: ref("ClusterOut"), title: "Response List" } } },
          },
          ...validationError,
        },
      },
      post: {
        tags: ["clusters"],
        summary: "Create Cluster",
        operationId: "create_cluster_api_clusters_post",
        requestBody: { required: true, content: { "application/json": { schema: ref("ClusterCreate") } } },
        responses: { "201": { description: "Successful Response", content: { "application/json": { schema: ref("ClusterOut") } } }, ...validationError },
      },
    },
    "/api/backend-apis": {
      post: {
        tags: ["backend-apis"],
        summary: "Upload Spec",
        operationId: "upload_spec_api_backend_apis_post",
        requestBody: { required: true, content: { "multipart/form-data": { schema: ref("Body_upload_spec_api_backend_apis_post") } } },
        responses: { "201": { description: "Successful Response", content: { "application/json": { schema: {} } } }, ...validationError },
      },
    },
    "/api/knowledge-bases/{kb_id}": {
      delete: {
        tags: ["knowledge-bases"],
        summary: "Delete Knowledge Base",
        operationId: "delete_knowledge_base_api_knowledge_bases__kb_id__delete",
        parameters: [{ name: "kb_id", in: "path", required: true, schema: { type: "string", title: "Kb Id" } }],
        responses: {
          "200": { description: "Successful Response", content: { "application/json": { schema: ref("KnowledgeBaseDeleteResponse") } } },
          ...validationError,
        },
      },
    },
    "/api/settings/data-plane-auth": {
      get: {
        tags: ["settings"],
        summary: "Data Plane Auth",
        operationId: "data_plane_auth_api_settings_data_plane_auth_get",
        responses: { "200": { description: "Successful Response", content: { "application/json": { schema: ref("DataPlaneAuthOut") } } } },
      },
    },
    "/api/version": {
      get: {
        summary: "Version",
        operationId: "version_api_version_get",
        responses: { "200": { description: "Successful Response", content: { "application/json": { schema: ref("VersionInfo") } } } },
      },
    },
  },
  components: {
    schemas: {
      ClusterOut: {
        type: "object",
        title: "ClusterOut",
        required: ["id", "name"],
        properties: {
          id: { type: "string", title: "Id" },
          name: { type: "string", title: "Name" },
          note: optional({ type: "string" }, { title: "Note", description: "Free text" }),
          owner: optional(ref("Owner")),
          nodes: optional({ type: "array", items: ref("Node") }, { title: "Nodes" }),
        },
      },
      Owner: {
        type: "object",
        title: "Owner",
        properties: { sub: { type: "string", title: "Sub" } },
      },
      Node: {
        type: "object",
        title: "Node",
        properties: {
          name: { type: "string", title: "Name" },
          parent: optional(ref("Node")),
        },
      },
      ClusterCreate: {
        type: "object",
        title: "ClusterCreate",
        required: ["name", "connection"],
        properties: {
          name: { type: "string", title: "Name" },
          connection: { anyOf: [ref("KubeconfigConnection"), ref("TokenConnection")], title: "Connection" },
          partial_spec: {
            anyOf: [{ type: "object", additionalProperties: true }, { type: "string" }, { type: "null" }],
            title: "Partial Spec",
          },
        },
      },
      KubeconfigConnection: {
        type: "object",
        title: "KubeconfigConnection",
        properties: { kubeconfig: { type: "string", title: "Kubeconfig" } },
      },
      TokenConnection: {
        type: "object",
        title: "TokenConnection",
        properties: { server: { type: "string", title: "Server" } },
      },
      Body_upload_spec_api_backend_apis_post: {
        type: "object",
        title: "Body_upload_spec_api_backend_apis_post",
        required: ["file"],
        properties: { file: { type: "string", contentMediaType: "application/octet-stream", title: "File" } },
      },
      KnowledgeBaseDeleteResponse: {
        type: "object",
        title: "KnowledgeBaseDeleteResponse",
        properties: { warning: optional({ type: "string", const: "table_not_dropped" }, { title: "Warning" }) },
      },
      DataPlaneAuthOut: {
        type: "object",
        title: "DataPlaneAuthOut",
        properties: {
          render_epochs: {
            type: "object",
            additionalProperties: { type: "integer" },
            propertyNames: { enum: ["gateway", "agent"] },
            title: "Render Epochs",
          },
        },
      },
      VersionInfo: {
        type: "object",
        title: "VersionInfo",
        required: ["api_version"],
        properties: {
          api_version: { type: "string", title: "Api Version" },
          api_build: optional({ type: "string" }, { title: "Api Build" }),
        },
      },
      HTTPValidationError: {
        type: "object",
        title: "HTTPValidationError",
        properties: { detail: { type: "array", items: ref("ValidationError"), title: "Detail" } },
      },
      ValidationError: {
        type: "object",
        title: "ValidationError",
        required: ["loc", "msg", "type"],
        properties: {
          loc: { type: "array", items: { anyOf: [{ type: "string" }, { type: "integer" }] }, title: "Location" },
          msg: { type: "string", title: "Message" },
          type: { type: "string", title: "Error Type" },
        },
      },
    },
  },
};
