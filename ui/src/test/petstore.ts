/**
 * 문서 뷰어 테스트용 OpenAPI 3.0 Petstore 축약본. 오퍼레이션 4개에 `$ref`(파라미터·스키마)·allOf·
 * 순환(Pet → Owner → pets: Pet[])·enum·deprecated 를 한 번씩 담았다.
 */
import type { OpenApiDocument } from "@/api/types";

export const specPetstore: OpenApiDocument = {
  openapi: "3.0.3",
  info: {
    title: "Petstore",
    version: "1.0.0",
    description: "A sample pet store.\nSee [the guide](https://example.com/guide) and `listPets`.",
  },
  servers: [{ url: "https://petstore.example.com/v1", description: "production" }],
  tags: [{ name: "pets", description: "Everything about pets" }],
  paths: {
    "/pets": {
      get: {
        operationId: "listPets",
        tags: ["pets"],
        summary: "List all pets",
        parameters: [
          {
            name: "limit",
            in: "query",
            required: false,
            description: "How many items to return",
            schema: { type: "integer", format: "int32" },
          },
          { $ref: "#/components/parameters/Status" },
        ],
        responses: {
          "200": {
            description: "A paged array of pets",
            content: { "application/json": { schema: { type: "array", items: { $ref: "#/components/schemas/Pet" } } } },
          },
          default: {
            description: "unexpected error",
            content: { "application/json": { schema: { $ref: "#/components/schemas/Error" } } },
          },
        },
      },
      post: {
        operationId: "createPet",
        tags: ["pets"],
        summary: "Create a pet",
        requestBody: {
          required: true,
          description: "The pet to add",
          content: { "application/json": { schema: { $ref: "#/components/schemas/NewPet" } } },
        },
        responses: {
          "201": {
            description: "Created",
            content: { "application/json": { schema: { $ref: "#/components/schemas/Pet" } } },
          },
        },
      },
    },
    "/pets/{petId}": {
      parameters: [{ name: "petId", in: "path", required: true, description: "The id of the pet", schema: { type: "string" } }],
      get: {
        operationId: "getPet",
        tags: ["pets"],
        summary: "Info for a specific pet",
        description: "Returns the pet.\n\nThe owner is included when known.",
        responses: {
          "200": {
            description: "Expected response",
            content: { "application/json": { schema: { $ref: "#/components/schemas/Pet" } } },
          },
          "404": { description: "Not found" },
        },
      },
      delete: {
        operationId: "deletePet",
        summary: "Delete a pet",
        deprecated: true,
        responses: { "204": { description: "Deleted" } },
      },
    },
  },
  components: {
    parameters: {
      Status: {
        name: "status",
        in: "query",
        description: "Filter by status",
        schema: { type: "string", enum: ["available", "sold"] },
      },
    },
    schemas: {
      NewPet: {
        type: "object",
        required: ["name"],
        properties: {
          name: { type: "string", description: "Pet name" },
          tag: { type: "string" },
        },
      },
      Pet: {
        allOf: [
          { $ref: "#/components/schemas/NewPet" },
          {
            type: "object",
            required: ["id"],
            properties: {
              id: { type: "integer", format: "int64" },
              owner: { $ref: "#/components/schemas/Owner" },
            },
          },
        ],
      },
      Owner: {
        type: "object",
        properties: {
          name: { type: "string" },
          pets: { type: "array", items: { $ref: "#/components/schemas/Pet" } },
        },
      },
      // x-internal-id 와 code.example 은 minify(규칙 정리) 픽스처가 지우는 대상이다 (D25). 문서 뷰어는 둘 다 그리지 않는다.
      Error: {
        type: "object",
        required: ["code", "message"],
        "x-internal-id": "err-1",
        properties: { code: { type: "integer", example: 500 }, message: { type: "string" } },
      },
    },
  },
};

/** WSDL 에서 변환된 모양의 최소 문서. x-soap 블록 표시를 검사한다. */
export const specOrdersSoap: OpenApiDocument = {
  openapi: "3.0.3",
  info: { title: "OrderService", version: "1" },
  paths: {
    "/CreateOrder": {
      post: {
        operationId: "CreateOrder",
        summary: "Creates a new order",
        "x-soap": {
          operation: "CreateOrder",
          soapAction: "http://example.com/orders/CreateOrder",
          soapVersion: "1.1",
          style: "document",
          endpoint: "http://localhost:18081/orders",
          input: { element: "CreateOrder", namespace: "http://example.com/orders" },
          output: { element: "CreateOrderResponse", namespace: "http://example.com/orders" },
        },
        requestBody: {
          content: {
            "application/json": {
              schema: { type: "object", required: ["customer"], properties: { customer: { type: "string" } } },
            },
          },
        },
        responses: { "200": { description: "ok" } },
      },
    },
  },
};
