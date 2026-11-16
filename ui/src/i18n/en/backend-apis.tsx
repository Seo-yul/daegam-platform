import type { ReactNode } from "react";

import type { Messages } from "../types";
import { plural } from "./plural";

const tools = (count: number): string => plural(count, "Tool", "Tools");

/** REST 스펙에는 SOAP 계약이 없어 x-soap 검사가 모두 통과한다. 그 검사들의 풀이 끝에 같은 문장을 붙인다. */
const REST_PASSES = "REST specs have nothing to check here, so this passes.";

/** Backend API 목록·상세·만들기·다듬기와 스펙 검사 결과. 검사 풀이는 마침표로 끝나는 문장이다. */
export const backendApis: Messages["backendApis"] = {
  kind: "Kind",
  latestVersion: "Latest version",
  created: "Created",
  connection: {
    endpointHint: "The address used to call this backend. It replaces the servers in the spec.",
    authHint: 'How to authenticate when calling the backend. With "none", no credentials are sent.',
    labels: {
      endpoint: "Endpoint",
      auth: "Auth",
      secret: "Credential Secret",
      soapHeadersSecret: "SOAP Header Secret",
    },
  },
  list: {
    create: "Create Backend API",
    columns: { name: "Name", tools: "Tools", endpoint: "Endpoint", auth: "Auth" },
    noMatch: (query: string): string => `No Backend APIs match "${query}".`,
    empty: (tenant: string): string => `Tenant ${tenant} has no Backend APIs. Upload a spec to create MCP Servers from its Tools.`,
    emptyNew: "Create Backend API",
    title: "Backend APIs",
    search: "Search backend APIs…",
  },
  detail: {
    editDocs: "Edit API docs",
    version: "Version",
    noVersion: "No saved version.",
    docsTitle: (version: number): string => `View the OpenAPI document of v${version}.`,
    openDocs: "View API docs",
    sourceFormat: "Source format",
    updated: "Updated",
    toolCount: (count: number | null): string => (count === null ? "-" : tools(count)),
    noTools: "No Tools yet, because no version has been uploaded.",
    versionColumns: { format: "Format", check: "Check" },
    noVersions: "No versions.",
    noResults: "No check results.",
    connectionTitle: "Connection",
    secretHint: "The name of the K8s Secret that holds the credentials. The platform stores only the name and never reads the value.",
    soapHeadersHint:
      "Name of the K8s Secret whose key soap_headers holds the values of the SOAP headers the WSDL declares, as a JSON object keyed by each header's part or element name. The platform stores only the name and never reads the value; deployed MCP Servers pick it up at their next deploy or redeploy.",
    saveConnection: "Save connection",
    issue: "Issue token",
    tokenColumns: { label: "Label", scope: "Scope", status: "Status" },
    noTokens: "No tokens.",
    toolsTitle: "Tools",
    versionsTitle: "Versions",
    ok: "ok",
    fail: "fail",
    reportTitle: (version: number): string => `Verify report of v${version}`,
    pass: "pass",
    warn: "warn",
    tokensTitle: "Read tokens",
    tokenLabel: "token label",
    labelPlaceholder: "label",
    active: "active",
    revoked: "revoked",
  },
  new: {
    rename: {
      required: "Enter a display name.",
      start: "Edit display name",
      input: "Display name",
      save: "Save name",
      cancel: "Cancel name edit",
    },
    sourceLoading: "Loading the registered document…",
    registered: "Registered document",
    registeredNone: "No document is registered yet. Upload the first one below.",
    useRegistered: "Load into editor",
    saveTarget: (target: string): string => `What gets saved: ${target}`,
    target: {
      generated: "the converted OpenAPI (the editor below)",
      file: (name: string): string => `the uploaded file ${name}`,
      text: "the pasted spec text",
    },
    noSaveTarget: "There is no spec to save. Upload a file or paste a spec.",
    newDocument: "New document",
    or: "or paste",
    specTextHint: "Paste an OpenAPI or WSDL document as is. Use this instead of a URL or a file.",
    filenameHint:
      "The file name for the pasted spec text. Its extension tells the format. Leave empty to use spec.yaml, spec.json or spec.wsdl, depending on the detected format. Ignored when you upload a file.",
    verifying: "Checking…",
    verify: "Check",
    converting: "Converting…",
    convert: "Convert to OpenAPI",
    previewHint: (verify: ReactNode, convert: ReactNode): ReactNode => (
      <>
        {verify} shows whether this document is usable and which Tools it yields. {convert} turns WSDL or Swagger 2.0 into OpenAPI 3.x in the editor, so you can adjust it before saving. If you save right away, the server does the same conversion.
      </>
    ),
    versionNote: (version: number): string => `Saving creates v${version}.`,
    generatedNote: "Saving registers the content of this editor as the spec.",
    nameExists: (link: ReactNode, version: number): ReactNode => (
      <>
        A Backend API with this name already exists. Saving does not create a new Backend API. It becomes v{version} of {link} and overwrites its saved Endpoint, Auth and Credential Secret with the values below. To create a new one, use a different name.
      </>
    ),
    secretHint: "K8s Secret name",
    saveVersion: "Save as new version",
    saveNewVersion: "Save new version",
    prettyView: "pretty view",
    spec: "Spec",
    uploadFile: "Upload file",
    specText: "Spec text",
    verifyResult: "Verify result",
    format: "format",
    kind: "kind",
    mixed: "mixed",
    tools: "Tools",
    generated: "Generated OpenAPI",
    profile: "Backend API",
    title: "New Backend API",
    labels: {
      filename: "Filename hint (optional)",
    },
  },
  specJob: {
    back: "Back to details",
    presets: "Presets",
    run: "Run",
    running: "Running…",
    waiting: "Waiting for results.",
    applying: "Applying…",
    preset: {
      enrich_descriptions: "Enrich descriptions",
      korean_descriptions: "Korean descriptions",
      fill_parameter_docs: "Fill parameter descriptions",
      normalize_operation_ids: "Normalize operationIds",
    },
  },
  refine: {
    enrich: {
      errors: {
        label: "Move error response descriptions into the body",
        hint: "Adds the descriptions of 4xx and 5xx responses to the operation description, so they stay when response definitions are trimmed.",
      },
      examples: {
        label: "Move examples into the body",
        hint: "Turns example values into description text, so what to send stays when examples are trimmed.",
      },
    },
    presetHint: {
      enrich_descriptions: "Fills in short or missing summaries and descriptions.",
      korean_descriptions: "Writes descriptions in Korean.",
      fill_parameter_docs: "Fills in the descriptions of parameters and properties.",
      normalize_operation_ids: "Renames operationIds consistently. Check the MCP Servers that reference this backend when you deploy them again.",
    },
    subtitle: (mode: string, version: number | null): string => `${mode}. Only the items you accept go into the new version v${version ?? "?"}.`,
    instant: "Instant",
    enrichLegend: "Move into descriptions (enrich)",
    maxDescription: "Maximum description length (max_description)",
    maxDescriptionHint: "Leave empty to keep descriptions whole.",
    maxDescriptionPlaceholder: "e.g. 300",
    dropExamples: "Remove value examples (drop_value_examples)",
    dropExamplesHint: "Removes the example values of schemas from the document.",
    instructionPlaceholder: "e.g. Write formally and leave out example values.",
    llmNotice:
      "A refinement cannot be stopped once it starts. The results appear below when it finishes. Open this address again to pick up where you left off, even after reloading or leaving.",
    unaccepted: "Items you do not accept stay as in the original.",
    apply: "Apply as new version",
    readingSpec: "Reading the original spec.",
    idle: "Select options on the left and run them to compare the original with the accepted changes side by side.",
    job: {
      creating: "Creating the job.",
      batch: (batch: number, total: number): string => `Batch ${batch}/${total}`,
      failed: (count: number): string => `${count} failed`,
      passed: "Passed",
      tokens: (count: string): string => `Tokens: ${count}`,
      more: (count: number): string => `+${count} more`,
      waitingFirst: "Waiting for the first iteration.",
      noIterations: "No iterations recorded.",
    },
    llmJob: "LLM job",
  },
  verdict: {
    columns: { level: "Level", code: "Code", count: "Count", message: "Message" },
    noItems: "No issues found.",
    caseCollision: (tool: string, names: string): string =>
      `${tool}: the arguments ${names} differ only in case. If a call sends them together, ToolHive rejects it with 400 Invalid Request.`,
    summary: (total: number, pass: number, warn: number, fail: number, skip: number): string =>
      `${plural(total, "check", "checks")}: pass ${pass} · warn ${warn} · fail ${fail}${skip ? ` · skip ${skip}` : ""}`,
    toolCount: (count: number): string => `An MCP Server made from this spec gets ${tools(count)}.`,
    expand: "Expand",
    checkColumns: { check: "Check", status: "Status", severity: "On failure", message: "Message" },
    notApplicable: "Not applicable (REST spec)",
    details: (title: string): string => `${title} details`,
    severityTitle: {
      error: "If this check fails, registration is rejected.",
      warn: "If this check fails, it stays as a warning.",
    },
    refs: "References",
    truncated: (preview: string, omitted: number): string => `${preview}\n… (${plural(omitted, "character", "characters")} omitted)`,
    noRoundtripTools: "No Tools were generated.",
    noTools: "No Tools.",
    toolTitle: "View this Tool in the API docs.",
    moreTools: (count: number): string => `+${count} more`,
    title: "Verdict",
    location: "location",
  },
  checks: {
    severity: { error: "Rejected", warn: "Warning" },
    group: {
      document: { title: "Document", description: "Checks that the document reads as OpenAPI 3.x." },
      "tool-name": { title: "Tool name", description: "The operationId becomes the MCP Tool name. Checks that it exists, follows the rules and is unique." },
      "tool-description": { title: "Tool description", description: "Checks for the text an LLM reads to select the Tool." },
      "x-soap": { title: "SOAP contract", description: "Checks that the bridge can handle the SOAP contract of a spec converted from WSDL." },
      deep: { title: "Deep checks", description: "Validates the schema and builds a real server with FastMCP. Skipped when the dependencies are missing." },
      other: { title: "Other", description: "A check this screen does not know." },
    },
    check: {
      "document.mapping": { title: "Top-level object", description: "Checks that the top level of the document is an object." },
      "document.openapi3": {
        title: "OpenAPI 3.x document",
        description: "Checks that the document is `openapi: 3.x`. Swagger 2.0 and WSDL are checked after conversion.",
      },
      "document.has-paths": { title: "paths present", description: "Checks that `paths` exists and is not empty." },
      "document.has-operations": { title: "Operations present", description: "Checks that there is at least one HTTP operation." },
      "tool-name.present": {
        title: "operationId present",
        description: "Checks that every operation has an `operationId`. This value becomes the MCP Tool name.",
      },
      "tool-name.safe": { title: "Name rules", description: "Checks that Tool names follow the MCP rules (`[A-Za-z0-9_-]`, up to 64 characters)." },
      "tool-name.normalized": { title: "Unchanged by normalization", description: "Checks that FastMCP does not change names when it normalizes them." },
      "tool-name.unique": { title: "No duplicate names", description: "Checks that no two Tool names are the same." },
      "tool-name.normalization-collision": { title: "No collisions after normalization", description: "Checks that normalization does not make two names the same." },
      "tool-description.present": {
        title: "Description present",
        description: "Checks that each operation has a summary or description. An LLM reads this text to select the Tool.",
      },
      "x-soap.version": { title: "SOAP version", description: `Checks that soapVersion is valid. ${REST_PASSES}` },
      "x-soap.endpoint": { title: "SOAP endpoint", description: `Checks that a SOAP endpoint exists. ${REST_PASSES}` },
      "x-soap.input-element": { title: "Request envelope root", description: `Checks that the root element of the request envelope is defined. ${REST_PASSES}` },
      "x-soap.output-element": { title: "Response envelope root", description: `Checks that the root element of the response envelope is defined. ${REST_PASSES}` },
      "x-soap.refs": { title: "$ref resolution", description: `Checks that every \`$ref\` in the SOAP contract resolves. ${REST_PASSES}` },
      "x-soap.choice": { title: "XSD choice", description: `Checks that each XSD choice has a form the bridge can handle. ${REST_PASSES}` },
      "x-soap.substitution": {
        title: "XSD substitution group",
        description: `Checks that each XSD substitution group has a form the bridge can handle. ${REST_PASSES}`,
      },
      "x-soap.mixed-rest": {
        title: "No SOAP and REST mix",
        description: `Checks that SOAP and REST operations are not mixed. A mixed spec is rejected. ${REST_PASSES}`,
      },
      "openapi.schema-valid": { title: "OpenAPI schema validation", description: "Checks that the document passes OpenAPI 3.x schema validation." },
      "fastmcp.roundtrip": {
        title: "FastMCP server build",
        description: "Builds a real server with FastMCP and checks that it lists Tools. The details show each Tool's name and parameters.",
      },
      "fastmcp.tool-materialized": { title: "MCP tools/list output", description: "Checks that the Tool materializes in the MCP `tools/list` format." },
    },
  },
};
