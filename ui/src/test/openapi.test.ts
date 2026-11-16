import {
  deref,
  filterGroups,
  findOperation,
  flattenSchema,
  groupByTag,
  infoOf,
  listOperations,
  operationParameters,
  requestBodyOf,
  resolveRef,
  responsesOf,
  schemaTypeLabel,
  serversOf,
  soapContractOf,
  tokenizeText,
} from "@/lib/openapi";

import { fastapiDoc } from "./fastapiDoc";
import { specOrdersSoap, specPetstore } from "./petstore";

/** fastapiDoc 의 components.schemas 한 개. */
function schemaOf(name: string): Record<string, unknown> {
  return resolveRef(fastapiDoc, `#/components/schemas/${name}`) as Record<string, unknown>;
}

function propertyOf(name: string, property: string): unknown {
  return (schemaOf(name).properties as Record<string, unknown>)[property];
}

describe("lib/openapi", () => {
  it("resolveRef walks JSON pointers inside the document and unescapes ~1/~0", () => {
    expect(resolveRef(specPetstore, "#/components/schemas/Error")).toMatchObject({ type: "object" });
    expect(resolveRef(specPetstore, "#/paths/~1pets/get/operationId")).toBe("listPets");
    expect(resolveRef(specPetstore, "#/components/schemas/Nope")).toBeUndefined();
    expect(resolveRef(specPetstore, "other.yaml#/Pet")).toBeUndefined();
    expect(resolveRef(specPetstore, "#")).toBe(specPetstore);
  });

  it("deref follows $ref chains and keeps the last name", () => {
    const r = deref(specPetstore, { $ref: "#/components/schemas/Pet" });
    expect(r.name).toBe("Pet");
    expect(r.ref).toBe("#/components/schemas/Pet");
    expect(r.value).toHaveProperty("allOf");
    expect(deref(specPetstore, { type: "string" })).toEqual({ value: { type: "string" }, ref: null, name: null });
    expect(deref(specPetstore, { $ref: "#/components/schemas/Nope" }).value).toBeUndefined();
    expect(deref(specPetstore, "x").value).toBeUndefined();
  });

  it("listOperations flattens paths × methods with ids, tags and deprecation", () => {
    const ops = listOperations(specPetstore);
    expect(ops.map((o) => `${o.method} ${o.path}`)).toEqual(["get /pets", "post /pets", "get /pets/{petId}", "delete /pets/{petId}"]);
    expect(ops.map((o) => o.id)).toEqual(["listPets", "createPet", "getPet", "deletePet"]);
    expect(ops[3].deprecated).toBe(true);
    expect(ops[3].tags).toEqual([]);
    expect(findOperation(specPetstore, "getPet")?.path).toBe("/pets/{petId}");
    // operationId 가 없으면 `method path` 가 id 다
    const anon = listOperations({ paths: { "/x": { get: {} } } });
    expect(anon[0].id).toBe("get /x");
    expect(anon[0].operationId).toBeNull();
  });

  it("groupByTag orders declared tags first and puts untagged operations under default", () => {
    const groups = groupByTag(specPetstore);
    expect(groups.map((g) => g.tag)).toEqual(["pets", "default"]);
    expect(groups[0].description).toBe("Everything about pets");
    expect(groups[0].operations.map((o) => o.id)).toEqual(["listPets", "createPet", "getPet"]);
    expect(groups[1].operations.map((o) => o.id)).toEqual(["deletePet"]);
    // 선언만 되고 쓰이지 않은 태그는 빠진다
    expect(groupByTag({ tags: [{ name: "unused" }], paths: { "/a": { get: { operationId: "a", tags: ["z"] } } } }).map((g) => g.tag)).toEqual(["z"]);
  });

  it("filterGroups matches id, path, summary and method case-insensitively", () => {
    const groups = groupByTag(specPetstore);
    expect(filterGroups(groups, "").length).toBe(2);
    expect(filterGroups(groups, "PETID").flatMap((g) => g.operations.map((o) => o.id))).toEqual(["getPet", "deletePet"]);
    expect(filterGroups(groups, "create").flatMap((g) => g.operations.map((o) => o.id))).toEqual(["createPet"]);
    expect(filterGroups(groups, "delete").flatMap((g) => g.operations.map((o) => o.id))).toEqual(["deletePet"]);
    expect(filterGroups(groups, "zzz")).toEqual([]);
  });

  it("schemaTypeLabel names refs, arrays, formats, enums and nullable", () => {
    expect(schemaTypeLabel(specPetstore, { $ref: "#/components/schemas/Pet" })).toBe("Pet");
    expect(schemaTypeLabel(specPetstore, { type: "array", items: { $ref: "#/components/schemas/Pet" } })).toBe("array<Pet>");
    expect(schemaTypeLabel(specPetstore, { type: "integer", format: "int64" })).toBe("integer(int64)");
    expect(schemaTypeLabel(specPetstore, { type: "string", nullable: true })).toBe("string | null");
    expect(schemaTypeLabel(specPetstore, { type: ["string", "null"] })).toBe("string | null");
    expect(schemaTypeLabel(specPetstore, { oneOf: [{ type: "string" }] })).toBe("string");
    expect(schemaTypeLabel(specPetstore, { properties: { a: {} } })).toBe("object");
    expect(schemaTypeLabel(specPetstore, { $ref: "#/components/schemas/Nope" })).toBe("Nope");
  });

  it("flattenSchema merges allOf, resolves $ref and stops at circular references", () => {
    const rows = flattenSchema(specPetstore, { $ref: "#/components/schemas/Pet" });
    expect(rows.map((r) => [r.name, r.depth, r.type, r.required, r.circular])).toEqual([
      ["name", 0, "string", true, false],
      ["tag", 0, "string", false, false],
      ["id", 0, "integer(int64)", true, false],
      ["owner", 0, "Owner", false, false],
      ["name", 1, "string", false, false],
      ["pets", 1, "array<Pet>", false, true],
    ]);
    expect(rows[0].description).toBe("Pet name");
    expect(rows[3].ref).toBe("Owner");
    // 배열 루트는 items 의 속성을 바로 편다
    const list = flattenSchema(specPetstore, { type: "array", items: { $ref: "#/components/schemas/Error" } });
    expect(list.map((r) => r.name)).toEqual(["code", "message"]);
    // 원시형 루트는 행이 없다
    expect(flattenSchema(specPetstore, { type: "string" })).toEqual([]);
  });

  it("flattenSchema shows oneOf variants, additionalProperties, enums and deprecated", () => {
    const doc = {
      components: { schemas: { A: { type: "object", properties: { a: { type: "string", enum: ["x", "y"], deprecated: true } } } } },
    };
    const rows = flattenSchema(doc, {
      type: "object",
      properties: {
        choice: { oneOf: [{ $ref: "#/components/schemas/A" }, { type: "integer" }] },
        map: { type: "object", additionalProperties: { type: "boolean" } },
      },
    });
    expect(rows.map((r) => [r.name, r.depth, r.type])).toEqual([
      ["choice", 0, "A | integer"],
      ["A", 1, "A"],
      ["a", 2, "string"],
      ["option 2", 1, "integer"],
      ["map", 0, "object"],
      ["{key}", 1, "boolean"],
    ]);
    expect(rows[2].enumValues).toEqual(["x", "y"]);
    expect(rows[2].deprecated).toBe(true);
  });

  it("operationParameters merges path-level parameters and resolves $ref parameters", () => {
    const list = findOperation(specPetstore, "listPets")!;
    expect(operationParameters(specPetstore, list)).toEqual([
      { name: "limit", in: "query", required: false, type: "integer(int32)", description: "How many items to return", deprecated: false, enumValues: null },
      { name: "status", in: "query", required: false, type: "string", description: "Filter by status", deprecated: false, enumValues: ["available", "sold"] },
    ]);
    const get = findOperation(specPetstore, "getPet")!;
    expect(operationParameters(specPetstore, get)).toEqual([
      { name: "petId", in: "path", required: true, type: "string", description: "The id of the pet", deprecated: false, enumValues: null },
    ]);
    // 오퍼레이션 파라미터가 같은 name+in 의 경로 파라미터를 덮는다
    const over = { paths: { "/a/{id}": { parameters: [{ name: "id", in: "path", description: "path-level" }], get: { parameters: [{ name: "id", in: "path", description: "op-level" }] } } } };
    expect(operationParameters(over, listOperations(over)[0]).map((p) => p.description)).toEqual(["op-level"]);
  });

  it("requestBodyOf and responsesOf expose content types with flattened schemas", () => {
    const create = findOperation(specPetstore, "createPet")!;
    const body = requestBodyOf(specPetstore, create)!;
    expect(body.required).toBe(true);
    expect(body.description).toBe("The pet to add");
    expect(body.contents[0].contentType).toBe("application/json");
    expect(body.contents[0].type).toBe("NewPet");
    expect(body.contents[0].rows.map((r) => r.name)).toEqual(["name", "tag"]);
    expect(requestBodyOf(specPetstore, findOperation(specPetstore, "listPets")!)).toBeNull();

    const responses = responsesOf(specPetstore, findOperation(specPetstore, "listPets")!);
    expect(responses.map((r) => r.status)).toEqual(["200", "default"]);
    expect(responses[0].contents[0].type).toBe("array<Pet>");
    expect(responses[0].contents[0].rows.map((r) => r.name)).toEqual(["name", "tag", "id", "owner", "name", "pets"]);
    expect(responses[1].contents[0].type).toBe("Error");
    expect(responsesOf(specPetstore, findOperation(specPetstore, "getPet")!)[1]).toEqual({ status: "404", description: "Not found", contents: [] });
  });

  it("soapContractOf reads the x-soap block and infoOf/serversOf read the header", () => {
    expect(soapContractOf(findOperation(specPetstore, "getPet")!)).toBeNull();
    expect(soapContractOf(findOperation(specOrdersSoap, "CreateOrder")!)).toEqual({
      operation: "CreateOrder",
      soapAction: "http://example.com/orders/CreateOrder",
      soapVersion: "1.1",
      style: "document",
      endpoint: "http://localhost:18081/orders",
      input: { element: "CreateOrder", namespace: "http://example.com/orders" },
      output: { element: "CreateOrderResponse", namespace: "http://example.com/orders" },
    });
    expect(infoOf(specPetstore)).toEqual({ title: "Petstore", version: "1.0.0", description: expect.stringContaining("A sample pet store.") });
    expect(infoOf({})).toEqual({ title: "Untitled API", version: null, description: null });
    expect(serversOf(specPetstore)).toEqual([{ url: "https://petstore.example.com/v1", description: "production" }]);
  });

  it("tokenizeText splits paragraphs, line breaks, code, links and bare URLs", () => {
    expect(tokenizeText("A `b` [c](https://x.io/c) https://y.io\nnext\n\nlast")).toEqual([
      [
        { kind: "text", text: "A " },
        { kind: "code", text: "b" },
        { kind: "text", text: " " },
        { kind: "link", text: "c", href: "https://x.io/c" },
        { kind: "text", text: " " },
        { kind: "link", text: "https://y.io", href: "https://y.io" },
        { kind: "break" },
        { kind: "text", text: "next" },
      ],
      [{ kind: "text", text: "last" }],
    ]);
  });
});

describe("lib/openapi on the platform's FastAPI document (P59)", () => {
  it("labels Optional[X] as X | null and a union of several branches with every branch", () => {
    expect(schemaTypeLabel(fastapiDoc, propertyOf("ClusterOut", "note"))).toBe("string | null");
    expect(schemaTypeLabel(fastapiDoc, propertyOf("ClusterOut", "owner"))).toBe("Owner | null");
    expect(schemaTypeLabel(fastapiDoc, propertyOf("ClusterOut", "nodes"))).toBe("array<Node> | null");
    expect(schemaTypeLabel(fastapiDoc, propertyOf("ClusterCreate", "connection"))).toBe("KubeconfigConnection | TokenConnection");
    expect(schemaTypeLabel(fastapiDoc, propertyOf("ClusterCreate", "partial_spec"))).toBe("object | string | null");
    expect(schemaTypeLabel(fastapiDoc, propertyOf("ValidationError", "loc"))).toBe("array<string | integer>");
    // 3.1 의 파일 업로드는 format 대신 contentMediaType 이다
    expect(schemaTypeLabel(fastapiDoc, propertyOf("Body_upload_spec_api_backend_apis_post", "file"))).toBe("string(application/octet-stream)");
  });

  it("flattens Optional[X] into X's properties and stops at a reference to itself", () => {
    const rows = flattenSchema(fastapiDoc, { $ref: "#/components/schemas/ClusterOut" });
    expect(rows.map((r) => [r.name, r.depth, r.type, r.circular])).toEqual([
      ["id", 0, "string", false],
      ["name", 0, "string", false],
      ["note", 0, "string | null", false],
      ["owner", 0, "Owner | null", false],
      ["sub", 1, "string", false],
      ["nodes", 0, "array<Node> | null", false],
      ["name", 1, "string", false],
      ["parent", 1, "Node | null", true],
    ]);
    // 설명은 anyOf 바깥(pydantic Field)에 있고, Optional 의 이름이 행의 참조다
    expect(rows[2].description).toBe("Free text");
    expect(rows[3].ref).toBe("Owner");
  });

  it("gives each non-null branch of a union its own row", () => {
    const rows = flattenSchema(fastapiDoc, { $ref: "#/components/schemas/ClusterCreate" });
    expect(rows.map((r) => [r.name, r.depth, r.type])).toEqual([
      ["name", 0, "string"],
      ["connection", 0, "KubeconfigConnection | TokenConnection"],
      ["KubeconfigConnection", 1, "KubeconfigConnection"],
      ["kubeconfig", 2, "string"],
      ["TokenConnection", 1, "TokenConnection"],
      ["server", 2, "string"],
      ["partial_spec", 0, "object | string | null"],
      ["option 1", 1, "object"],
      ["option 2", 1, "string"],
    ]);
  });

  it("shows const as a one-value enum and the keys of propertyNames on the {key} row", () => {
    const warning = flattenSchema(fastapiDoc, { $ref: "#/components/schemas/KnowledgeBaseDeleteResponse" });
    expect(warning.map((r) => [r.name, r.type, r.enumValues])).toEqual([["warning", "string | null", ["table_not_dropped"]]]);
    const epochs = flattenSchema(fastapiDoc, { $ref: "#/components/schemas/DataPlaneAuthOut" });
    expect(epochs.map((r) => [r.name, r.depth, r.type, r.enumValues])).toEqual([
      ["render_epochs", 0, "object", null],
      ["{key}", 1, "integer", ["gateway", "agent"]],
    ]);
  });

  it("reads an Optional enum query parameter and groups untagged operations under default", () => {
    const list = findOperation(fastapiDoc, "list_clusters_api_clusters_get")!;
    expect(operationParameters(fastapiDoc, list)).toEqual([
      { name: "state", in: "query", required: false, type: "string | null", description: "Filter by state", deprecated: false, enumValues: ["draft", "deployed"] },
    ]);
    expect(groupByTag(fastapiDoc).map((g) => [g.tag, g.operations.map((o) => o.path)])).toContainEqual(["default", ["/api/version"]]);
  });

  it("stops on an Optional that refers to itself", () => {
    const loop = { $ref: "#/components/schemas/Loop" };
    const doc = {
      components: { schemas: { Loop: { anyOf: [loop, { type: "null" }] } } },
      paths: { "/x": { get: { operationId: "x", parameters: [{ name: "q", in: "query", schema: loop }] } } },
    };
    expect(schemaTypeLabel(doc, loop)).toBe("Loop | null");
    expect(schemaTypeLabel(doc, { anyOf: [loop, { type: "null" }] })).toBe("Loop | null");
    const rows = flattenSchema(doc, { type: "object", properties: { loop } });
    expect(rows.map((r) => [r.name, r.type, r.enumValues])).toEqual([["loop", "Loop | null", null]]);
    expect(operationParameters(doc, findOperation(doc, "x")!)[0].enumValues).toBeNull();
  });
});
