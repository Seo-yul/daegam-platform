import type { SpecChange } from "@/api/types";
import { dictionary } from "@/i18n";
import { applyChanges, changeLabel, formatPointer, groupChanges, parentPointer, parsePointer, schemaOperationRefs, summarizeChanges } from "@/lib/pointer";
import { toYaml } from "@/lib/yaml";
import { linesForPointers } from "@/lib/yamlPaths";

import { specPetstore } from "./petstore";

describe("json pointer", () => {
  it("parses and formats RFC 6901 tokens with ~0 and ~1", () => {
    expect(parsePointer("")).toEqual([]);
    expect(parsePointer("/paths/~1pets~1{petId}/get/summary")).toEqual(["paths", "/pets/{petId}", "get", "summary"]);
    expect(parsePointer("/a~0b/c")).toEqual(["a~b", "c"]);
    expect(formatPointer(["paths", "/pets", "get"])).toBe("/paths/~1pets/get");
    expect(parentPointer("/paths/~1pets/get")).toBe("/paths/~1pets");
    expect(parentPointer("/paths")).toBe("");
    expect(parentPointer("")).toBeNull();
    expect(() => parsePointer("paths")).toThrow();
  });

  it("applies changed/added/removed without touching the original and creates missing parents", () => {
    const changes: SpecChange[] = [
      { id: "1", pointer: "/paths/~1pets/get/summary", kind: "changed", before: "List all pets", after: "List every pet" },
      { id: "2", pointer: "/paths/~1pets/post/description", kind: "added", before: null, after: "Creates a pet." },
      { id: "3", pointer: "/components/schemas/Error/x-internal-id", kind: "removed", before: "err-1", after: null },
      { id: "4", pointer: "/x-s2o/minify", kind: "added", before: null, after: { enrich: ["errors"] } },
    ];
    const out = applyChanges(specPetstore, changes);
    const paths = out.paths as Record<string, Record<string, Record<string, unknown>>>;
    expect(paths["/pets"].get.summary).toBe("List every pet");
    expect(paths["/pets"].post.description).toBe("Creates a pet.");
    const schemas = (out.components as { schemas: Record<string, Record<string, unknown>> }).schemas;
    expect(schemas.Error["x-internal-id"]).toBeUndefined();
    expect((out["x-s2o"] as { minify: unknown }).minify).toEqual({ enrich: ["errors"] });
    // 원본은 그대로다
    const orig = specPetstore.paths as Record<string, Record<string, Record<string, unknown>>>;
    expect(orig["/pets"].get.summary).toBe("List all pets");
    expect(orig["/pets"].post.description).toBeUndefined();
    expect(specPetstore["x-s2o"]).toBeUndefined();
  });

  it("removes array items from the highest index so earlier indexes stay valid", () => {
    const doc = { list: ["a", "b", "c", "d"] };
    const out = applyChanges(doc, [
      { id: "1", pointer: "/list/1", kind: "removed", before: "b", after: null },
      { id: "2", pointer: "/list/3", kind: "removed", before: "d", after: null },
    ]);
    expect(out.list).toEqual(["a", "c"]);
  });

  it("counts operations referencing each schema through allOf and items", () => {
    const refs = schemaOperationRefs(specPetstore);
    // Error: listPets 의 default 응답만
    expect(refs.get("Error")).toBe(1);
    // NewPet: createPet 직접, listPets·getPet 은 Pet(allOf) 을 거쳐서
    expect(refs.get("NewPet")).toBe(3);
    // Owner: Pet.owner 로 listPets·createPet·getPet
    expect(refs.get("Owner")).toBe(3);
  });

  it("groups by operation in document order, then schemas with reference counts", () => {
    const changes: SpecChange[] = [
      { id: "s", pointer: "/components/schemas/NewPet/properties/name/description", kind: "changed", before: "Pet name", after: "Name" },
      { id: "b", pointer: "/paths/~1pets~1{petId}/get/description", kind: "changed", before: "x", after: "y" },
      { id: "a", pointer: "/paths/~1pets/get/summary", kind: "changed", before: "x", after: "y" },
      { id: "a2", pointer: "/paths/~1pets/get/parameters/0/description", kind: "changed", before: "x", after: "y" },
      { id: "i", pointer: "/info/description", kind: "changed", before: "x", after: "y" },
    ];
    const groups = groupChanges(dictionary("ko"), specPetstore, changes);
    expect(groups.map((g) => g.key)).toEqual(["op:get:/pets", "op:get:/pets/{petId}", "schema:NewPet", "info"]);
    expect(groups[0].title).toBe("GET /pets");
    expect(groups[0].subtitle).toBe("listPets");
    expect(groups[0].changes.map((c) => c.id)).toEqual(["a", "a2"]);
    expect(changeLabel(groups[0], groups[0].changes[1])).toBe("parameters/0/description");
    expect(groups[2].subtitle).toBe("operation 3개가 참조");
    expect(summarizeChanges(specPetstore, groups)).toEqual({ totalOperations: 4, changedOperations: 2 });
  });

  it("maps pointers to YAML lines, including sequence indexes and keys with slashes", () => {
    const yaml = toYaml(specPetstore);
    const lines = yaml.split("\n");
    const found = linesForPointers(yaml, ["/paths/~1pets/get/summary", "/paths/~1pets/get/parameters/1", "/components/schemas/Error", "/nope/x"]);
    const at = (p: string) => found.find((f) => f.path === p)?.lines ?? [];
    expect(lines[at("/paths/~1pets/get/summary")[0] - 1]).toContain("summary: List all pets");
    expect(lines[at("/paths/~1pets/get/parameters/1")[0] - 1]).toContain("$ref: \"#/components/parameters/Status\"");
    expect(lines[at("/components/schemas/Error")[0] - 1]).toMatch(/^\s+Error:/);
    expect(at("/nope/x")).toEqual([]);
  });
});
