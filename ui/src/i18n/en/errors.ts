import { DOCUMENT_FORMAT_LABEL, EVALUATION_MAX_CASE_RUNS, FEEDBACK_EXPORT_MAX_DAYS, SESSION_TITLE_MAX_CHARS } from "@/lib/limits";

import type { Messages } from "../types";
import { plural } from "./plural";

/**
 * 서버 오류 코드의 문장. 무엇이 잘못됐는지와 무엇을 하면 되는지를 두 문장 이상으로 적고, 코드를 넣지 않으며, 대시로 설명을
 * 잇지 않는다. 검증은 validation, 다듬기는 refine, 비서는 the assistant, 대화는 conversation 하나로 적는다.
 */
export const errors: Messages["errors"] = {
  codes: {
    // -- 참조·상태 때문에 막히는 것, 장기 기억 (D34) ---------------------------------
    rules_too_large:
      "The Tool rules compile to more Cedar than the limit allows. Give Tools the same callers, or set rules on fewer Tools.",
    rule_names_unknown_tool: "A rule names a Tool that is not in the selected Tools. Check the Tool selection.",
    rule_names_unknown_gateway_tool: "A rule names a Tool this Gateway does not offer. Check the Gateway's Tools.",
    rule_unknown_argument: "A condition names an argument this Tool does not have. Check the argument name.",
    rule_argument_type: "A condition's value does not match the argument's type. Write it again in the argument's type.",
    rule_array_path: "Conditions cannot apply to arguments inside an array. Select an argument outside the array.",
    rule_custom_image: "Servers on a custom image cannot hold argument conditions. Switch to the default runtime image and save again.",
    rule_unknown_agent: "A rule names an Agent this tenant does not have. Select it again from the list.",
    rule_agent_oidc: "While Gateways accept only IdP tokens, a Gateway rule cannot name an Agent. Until then every Agent of the tenant calls with the same client.",
    rules_owner_cedar: "This Gateway runs Cedar saved in the Advanced tab, so Tool rules cannot apply. Remove that policy in the Advanced tab and save again.",
    rules_toolhive_old: "This cluster's ToolHive does not support Tool rules, so a server with rules cannot be deployed. Ask a platform admin to upgrade ToolHive.",
    long_term_schema_failed:
      "Could not create the schema for long-term memory. The Database account needs the CREATE privilege. Grant it, then deploy again.",
    not_memory_owner:
      "Long-term memory belongs to its user, so only that user or a tenant admin can delete it. Sign in as that user, or ask a tenant admin.",
    long_term_off:
      "This Agent does not use long-term memory. To use it, turn on long-term memory in the memory node of the Agent definition and deploy again.",
    memory_not_found: "Could not find this memory file. It may have been deleted, so refresh the list.",
    in_use: "Other resources are using this, so you cannot delete it. Remove the connections in the list below first.",
    instances_exist: "Resources are still deployed to this cluster, so you cannot delete it. Undeploy or delete them first.",
    sandbox_check_running: "An isolation check is already running on this cluster. View the result when it finishes.",
    sandbox_check_stale:
      "The isolation check result has changed or did not pass, so it can't be allowed. Reload the page to see the latest result, then try again.",
    sandbox_limit_range:
      "A LimitRange in this namespace caps memory below what one workspace needs, so an Agent with code execution cannot be deployed here. Ask a cluster admin to raise the LimitRange's maximum memory.",
    tokens_active:
      "This Backend API still has active read tokens, so you cannot delete it. Revoke the tokens first, or select Revoke tokens and delete.",
    published: "This is still on the hub. Unpublish it first.",
    not_published: "This is not published on the hub. Publish it first.",
    home_cluster: "You cannot delete the home cluster. The platform manages it directly.",
    state_conflict: "The status changed before this action could run. Refresh the page to see the current status, then try again.",
    reprice_running: "A reprice is already running. Try again after it finishes.",
    revision_conflict: "This item changed in the meantime. Refresh the page to review the changes, then try again.",
    deleting: "This is being deleted, so you cannot change it. Try again after the deletion finishes.",
    in_progress: "Another operation, such as a deploy or undeploy, is in progress, so you cannot do this now. Try again after it finishes.",
    indexing: "Documents are still being indexed. Try again once they are done.",
    not_ready: "The hub item is not ready yet, so it has no address. Try again after it becomes Ready.",
    backends_not_ready: "Some backends are not Ready yet. Wait until every backend is Ready, then try again.",

    // -- 없는 것 -------------------------------------------------------------------
    not_found: "Could not find what you requested. It may have been deleted, so select it again from the list.",
    unknown_cluster: "Could not find the selected cluster. Select it again from the list.",
    unknown_data_service: "Could not find the selected Database. Select it again from the list.",
    unknown_hub_item: "Could not find the selected hub item. Select it again from the hub list.",
    unknown_backend_api: "Could not find the selected Backend API. Select it again from the list.",
    unknown_spec_version: "This Backend API does not have that version. Select a version from the list.",
    unknown_mcp_server: "Could not find the connected MCP Server. It may have been deleted, so check the MCP Servers list.",
    source_not_found: "Could not find the source Backend API or version. Select it again from the list.",
    session_not_found: "Could not find the session. Start a new conversation.",
    secret_missing: "A required Secret is missing from the cluster. Create the Secret again or check its name.",
    instance_token_missing: "The token Secret is missing. Deploy again to recreate it, then try again.",

    // -- 이름·중복 -----------------------------------------------------------------
    name_conflict: "That name is already taken. Enter a different name.",
    name_immutable: "You cannot change the name after creation. Only the display name can change.",
    invalid_name: "The name does not follow the naming rules. Fix it to match the rules shown next to the field.",
    invalid_display_name: "The display name does not follow the naming rules. Fix it to match the rules shown next to the field.",
    reserved_name: "'home' is a cluster name reserved by the platform. Enter a different name.",
    k8s_name_conflict: "A Kubernetes resource with this name already exists. Enter a different name.",
    host_conflict: "Another deployment is already using this host. A host can be exposed in only one place across all clusters. Enter a different host name.",
    host_reserved: "This host is reserved for the platform. Enter a different host name.",
    duplicate_backend: "You selected the same hub item twice as a backend. Keep only one.",
    duplicate_backend_name: "Two backends have hub items with the same name, so their workload names collide. Remove one, or select a hub item with another name.",
    duplicate_env_prefix: "Two bindings use the same env_prefix. Give each binding a different prefix.",
    duplicate_mapping: "The same group and tenant pair appears twice. Delete the duplicate row.",
    duplicate_sub: "The same user appears twice. Delete the duplicate row.",
    duplicate_tools: "The selected Tool list has the same Tool twice. Remove the duplicate.",
    duplicate_tool_name: "With the overrides applied, two Tools get the same name. Rename the clashing Tools so their names differ.",

    // -- 폼 입력 -------------------------------------------------------------------
    empty_patch: "Nothing changed. Change at least one value, then save.",
    no_changes: "There are no changes to apply. Select at least one change, then apply.",
    invalid_auth: "This authentication method is not available. Select a method from the list, and use wsse only with SOAP backends.",
    credential_required: "The selected authentication method needs a credential Secret. Enter the Secret name in credential_secret.",
    invalid_credential_secret: "The credential Secret name is not valid. Enter a valid Kubernetes Secret name.",
    invalid_soap_headers_secret: "The SOAP header Secret name is not valid. Enter a valid Kubernetes Secret name.",
    invalid_endpoint: "The endpoint is not an http(s) address. Enter an address that starts with http or https.",
    invalid_backends: "The backend settings are invalid. Put at least one entry in the backends list, and do not send the old single field with it.",
    no_backends: "A Gateway needs at least one backend. Select a backend from the hub.",
    invalid_scope: "The scope format is invalid. Write it as a list of strings in the form kind:id.",
    invalid_role: "This role is not recognized. Select a role from the list.",
    invalid_tenant: "The tenant name does not follow the rules. Check the name.",
    invalid_tool_name: "A Tool name is not valid. Fix the name override so it follows the Tool naming rules.",
    unknown_tools: "You selected Tools that the backend does not provide. Refresh the Tool list, then select again.",
    invalid_format: "The format value is invalid. Select yaml or json.",
    invalid_input: "The request is not in the expected form. Select a file or enter the body text.",
    invalid_request: "The request contains fields that cannot be sent together. Select one, as the page describes, and send again.",
    invalid_yaml: "The YAML is not valid. Fix it so it is a single mapping.",
    invalid_minify_options: "The minify options are invalid. Check the option values.",
    invalid_kind: "You cannot create this kind of hub item directly. Create it by publishing an MCP Server or a Gateway.",

    // -- 클러스터·네임스페이스·노출 -------------------------------------------------
    cluster_unreachable: "Could not connect to the cluster. Check the cluster status and kubeconfig, then try again.",
    cluster_forbidden: "The cluster refused the platform's request. Ask the cluster administrator to check the platform account's permissions.",
    cluster_mismatch: "The selected resources are on different clusters. Select resources from the same cluster.",
    platform_item: "Platform Tools cannot be deleted or refreshed. Turn them on or off in Platform Settings.",
    platform_tool_disabled: "This platform Tool is turned off. Ask an admin to turn it on in Platform Settings.",
    platform_tool_not_exposable:
      "Platform web Tools cannot be added to an exposed Gateway. Turn exposure off, or ask an admin to turn on 'Allow in exposed Gateways' in Platform Settings.",
    web_gateway_unknown_names:
      "A Gateway with web Tools must know the Tool names of every backend. Refresh the Tools of the hub items whose names are unknown, or select that backend's Tools.",
    web_backend_unpinned_agents:
      "Some Agents using this Gateway were deployed without a fixed Tool list. Redeploy them first, then add web Tools.",
    web_backend_unknown_pins:
      "Some Agents using this Gateway were deployed while its Tool names were unknown. Select their Tools on the canvas, or deploy the Gateway once without web Tools and redeploy those Agents, then add web Tools.",
    web_tools_unknown_names:
      "The Tool names of a Tool source used beside web Tools are unknown. Select the Tools on this node, or refresh the Tools of the hub item.",
    domain_is_public_suffix:
      "Public suffixes such as com, co.kr or github.io cannot be listed. Enter a specific domain under them.",
    invalid_ca_bundle: "The CA bundle must be PEM certificates of at most 64 KiB. Check the certificate text.",
    cluster_not_allowed: "This cluster is not allowed for this tenant. Select a cluster from the allow list in Tenant Settings, or ask an admin to add it.",
    cluster_rights_missing:
      "This cluster lacks the rights the platform uses in tenant namespaces (the ClusterRole daegam-tenant-objects). Ask the cluster administrator to apply the latest rbac.yaml and run preflight again.",
    no_cluster: "Could not determine which cluster to use. Select a cluster, or set a default cluster in Tenant Settings.",
    no_home_cluster: "There is no home cluster to store the credentials in. Ask an admin to register a home cluster.",
    namespace_not_allowed: "This namespace is not allowed for this tenant. Select one from the allow list in Tenant Settings.",
    namespace_no_longer_allowed:
      "This deployment's namespace is no longer on the tenant's allow list, so it cannot be redeployed. Add the namespace back to the allow list in Tenant Settings, or undeploy it and deploy again.",
    namespace_claimed: "Another tenant is using this namespace. Select a different namespace.",
    ingress_disabled: "This cluster does not allow external exposure. Ask an admin to allow exposure on the cluster.",
    ingress_class_not_allowed: "This IngressClass is not on the cluster's allow list. Select an allowed one, or ask an admin to add it.",
    unknown_ingress_class: "This cluster does not have that IngressClass. Select an IngressClass that exists.",
    unknown_issuer: "This cluster does not have that ClusterIssuer. Select an existing issuer, or ask an admin to create it.",
    cert_manager_unavailable:
      "This cluster has no cert-manager, so certificates cannot be issued automatically. Specify a TLS Secret yourself, or ask an admin to install cert-manager.",
    tls_secret_invalid: "The TLS Secret is missing or has the wrong format. Enter the name of a kubernetes.io/tls Secret in the same namespace.",
    invalid_host: "The host name does not follow the rules. Use a lowercase DNS-1123 name.",
    invalid_cors_origin: "The CORS allowed origin has the wrong format. Write it like https://example.com, without a path.",
    unauthenticated_exposure:
      "This would expose the route without authentication, because the platform runs in header mode. Ask an admin to switch the platform to oidc or local auth mode.",
    redis_required: "With more than one replica, Redis is required as the session store. Add a Redis connection, or set a default Redis for the cluster.",
    locked_field: "The platform manages this field, so you cannot change it. Remove it from the override.",
    ignored_by_operator: "The ToolHive proxyrunner overwrites this field, so your value has no effect. Remove it from the override.",
    runtime_timeout: "The Agent runtime did not respond in time. Try again in a moment.",
    runtime_unreachable: "Could not connect to the Agent runtime. Check that the Agent is Ready, then try again.",
    tools_unavailable: "The Agent has not loaded its Tools yet. Check that its MCP Servers and Gateways are Ready, then try again.",
    dependencies_unavailable:
      "The Agent has not connected to its Database yet. Check that the Database is Ready and read the reason in the Agent's status message, then try again.",
    runtime_image_too_old: "The runtime image does not support features this Agent uses. Upgrade the Agent image on the cluster, then deploy again.",
    redeploy_required:
      "This Agent's graph now has web Tools, but some of its Tools nodes were deployed without a fixed Tool list. Deploy the Agent again.",

    // -- 권한·테넌트 ---------------------------------------------------------------
    forbidden: "You do not have permission to do this. Ask an admin for permission.",
    hub_item_not_consumable: "You do not have permission to use this hub item as a backend. Ask the item's owner for access.",
    hub_item_newer:
      "This item was made by a newer platform version, so this version cannot deploy it or change its access. Upgrade the platform to that version or later and try again.",
    // 허브 image 항목의 허용과 거부 (P55 5·6절)
    hub_images_unset:
      "Catalog images cannot be imported because image signature checks are not set up. Ask a platform-admin to set up hub images in Platform Settings.",
    image_not_admitted: "This image's signature could not be verified. Check that a platform-admin copied it to the company registry and signed it.",
    image_catalog_conflict: "Signatures on this image name different catalog servers, so it cannot be registered. Ask a platform-admin to check it.",
    image_signatures_too_many: "This image carries too many signatures to check them all. Ask a platform-admin to check it.",
    image_outside_registry: "Hub images must be under the registry path set in Platform Settings.",
    image_refused: "A platform-admin refused this image, so it cannot be registered.",
    refused_image_not_shareable: "An item whose image a platform-admin refused cannot be opened further to other tenants.",
    hub_backend_refused: "A hub item whose image a platform-admin refused cannot be a backend. Remove that backend in the editor and save again.",
    registry_unreachable: "The company registry could not be reached, so the image could not be checked. Try again later.",
    registry_credential_refused:
      "The company registry refused the read credential. Ask a platform-admin to check the hub image credential in Platform Settings.",
    hub_images_secret_unavailable: "The company registry's read credential cannot be read right now, so the image could not be checked. Try again later.",
    hub_prefix_overlap:
      "The hub image path overlaps a cluster's product image path, so it cannot be saved. Enter a path that does not overlap any product image path.",
    catalog_settings_admin_only: "Only a platform-admin can use Args or Env names that differ from the catalog. Ask a platform-admin to register it.",
    catalog_server_admin_only:
      "This catalog server is not in the current catalog, or its arguments need values, so only a platform-admin can register it. Ask a platform-admin to register it.",
    secret_not_shareable: "Items with Secret env cannot be opened to other tenants.",
    env_reserved: "This env name is set by the platform or ToolHive, or can change the code that runs, so it cannot be used. Use another name.",
    env_must_be_secret: "The catalog marks this variable as secret, so put it in Secret env.",
    env_required_missing: "A variable the catalog marks as required is missing. Add it to Env or Secret env.",
    cross_tenant_unsupported: "This kind of hub item cannot be shared with other tenants. Remove the other tenants from its sharing settings.",
    outside_header_reserved: "This header name is set by the Gateway or by MCP itself, so it cannot be used. Enter a different header name.",
    outside_header_value_invalid: "The header value holds control characters or is over 8 KiB. Check the value and enter it again.",
    outside_auth_not_https:
      "A hub item that carries auth needs an https URL; only a Service address in this tenant's namespaces is exempt. Enter an https address.",
    outside_oauth_unavailable: "Auth that connects each person's account is not available yet. Select none or header.",
    outside_sse_unsupported: "Outside MCP servers that speak only SSE cannot be registered. Register the server's streamable-http address instead.",
    not_session_owner: "This session belongs to another user. Try again in your own session.",
    tenant_mismatch: "This session belongs to another tenant. Switch tenants, then open it again.",
    agent_oauth_missing: "agent_oauth is not set in Tenant Settings, so a Gateway cannot be used as a Tool. Set it in Tenant Settings first.",

    // -- 개인 MCP 토큰 (P32) -------------------------------------------------------
    reauth_required: "For security, you need to sign in at the IdP again. Sign in, then try once more.",
    not_a_member: "You are not a member of this tenant, so you cannot get a token. Select a tenant you belong to.",
    token_limit: "You have reached the limit of personal MCP tokens. Revoke tokens you do not use, or issue one later.",

    // -- Skill (D64) --------------------------------------------------------------
    invalid_skill: "The Skill files do not match the format. Fix the issues shown, then try again.",
    skill_unchanged: "The content matches the last revision, so no new revision was created. Edit the files, then save again.",
    skill_limit: "This tenant has reached its Skill limit. Delete Skills you do not use, then try again.",
    skill_quota_exceeded:
      "This tenant has used up its storage for Skill revisions. Delete Skills you do not use, or ask an admin to raise the limit in Platform Settings.",
    cross_tenant_manage_unsupported: "Only the owning tenant can have the manage permission. Remove other tenants from the manage list.",
    skill_not_consumable: "You do not have permission to attach this Skill to an Agent. Ask the Skill's owner for access, or remove the Skill from the Agent.",

    // -- 허브·게이트웨이 -----------------------------------------------------------
    cycle: "Following the backends leads back to this Gateway. Remove the backend that closes the loop.",
    published_oidc_unsupported:
      "While Gateways accept only IdP tokens, a published Gateway cannot be a backend. Select a different backend, or try again after a platform admin switches to platform tokens.",
    published_owner_forbid:
      "This published Gateway cannot be a backend because its Authz policy (Cedar) holds a forbid statement, which cannot reliably screen people who come through another Gateway. Ask whoever manages the original Gateway to move the condition into Tool rules, or select a different backend.",
    published_script_tool:
      "A published Gateway's execute_tool_script cannot be selected in another Gateway, because this Gateway's Tool rules do not apply to the calls inside a script. Remove execute_tool_script from the selection.",
    published_names_unknown:
      "The Tool names this published Gateway offers are unknown, so it cannot be a backend. Run Tool refresh on the original Gateway's backends and deploy it again, then try again.",
    published_proxy_unsupported:
      "A Gateway published in proxy mode cannot be a backend. Ask whoever manages the original Gateway to publish it in entry mode as well and select that item, or select a different backend.",
    tools_list_failed: "Could not get the Tool list from the backend. Check that the item is Ready, then refresh the Tools again.",

    // -- LLM provider --------------------------------------------------------------
    no_provider: "No LLM provider is registered. Ask an admin to register a provider in Platform Settings.",
    unknown_provider: "This LLM provider is not registered. Select a provider registered in Platform Settings.",
    provider_unavailable: "The assistant has no LLM provider to use. Ask an admin to register a provider in Platform Settings.",
    provider_not_supported: "This provider is a stub, so it cannot generate specs. Select a real provider.",
    stub_provider_disallowed: "This installation does not allow the stub provider. Register a real provider.",
    unknown_kind: "This provider type is not supported. Change it to a supported type.",
    no_secret: "Could not find the provider's key Secret. Check the Secret name in Platform Settings.",
    no_api_key: "The Secret holds no API key. In Platform Settings, select a Secret that holds the key.",
    provider_no_key: "Could not read the provider's API key. Check the key Secret in Platform Settings.",
    no_secret_store: "There is no home cluster to read the key Secret from. Ask an admin to connect a home cluster.",
    auth: "The provider rejected the key. Check in Platform Settings that the key value is correct.",
    connection: "Could not reach the provider. Check the endpoint address and the network.",
    provider_error: "The provider returned an error. Try again in a moment, and if it keeps happening, check the provider settings.",
    llm_gateway_unreachable: "Could not reach the LLM gateway. Try again shortly. If this keeps happening, ask a platform admin to check the gateway.",
    llm_gateway_denied: "The LLM gateway refused the call. The token may have expired or been revoked. If this keeps happening, contact a platform admin.",
    llm_gateway_rate_limited: "The LLM gateway's per-minute token limit was reached. Try again in about a minute.",
    no_signer: "The platform has no signing key, so it cannot issue an LLM gateway token. Contact a platform admin.",
    llm_token_missing: "This Agent calls its LLM providers directly, so it has no LLM token to replace. Try again after it moves to the gateway route.",
    provider_rate_limited: "The LLM provider's request limit was reached. Try again in a moment.",
    rate_limited: "You sent too many requests in a short time. Try again in a moment.",
    quota_exhausted:
      "The LLM provider's credits are used up, or its usage limit was reached. Check billing and limits in the provider console, then try again.",
    refusal: "The model refused the request. Change the instructions and try again.",
    empty_response: "The stub provider has no prepared response. Select a real provider.",

    // -- 스펙 파이프라인 (Backend API 업로드·다듬기) ----------------------------
    too_large: "The file exceeds the upload limit. Split it into smaller files and upload them.",
    input_too_large: "The input exceeds the limit. Shorten it.",
    source_too_large: "The spec is too large to refine. Make it smaller or split it.",
    unsupported: "The document is not recognized as WSDL, Swagger 2.0 or OpenAPI 3.x. Check the format and upload it again.",
    conversion: "Could not convert the spec to OpenAPI. Check that the source document is valid WSDL, Swagger or OpenAPI.",
    mixed: "This spec contains both SOAP and REST operations. Split it into two specs and upload them separately.",
    verify: "The spec failed validation. Fix the failed items in the validation results.",
    verify_incomplete: "Validation skipped some checks and could not finish. Report this to an admin.",
    verdict_error: "The spec did not pass validation. Fix the errors in the validation results, then save again.",
    source_not_verified: "The source version has validation errors. Fix those errors first, then refine.",
    source_mismatch: "The refine result was made from a different version than this one. Run refine again on this version.",
    surface_changed: "The change touches a field that must stay the same. Leave that change out and apply again.",
    unknown_change: "You selected a change that is not in the list. Refresh the page to load the change list again.",
    empty_input: "The input is empty. Fill in at least one of text, URL, examples or a partial spec.",
    empty_instruction: "There is nothing to refine. Select a preset or write instructions.",
    integrity_error: "The stored data is inconsistent. Report this to an admin.",
    job_active: "The job is still running. Try again after it finishes.",
    job_not_done: "The refine job has not finished yet. Apply it after it finishes.",
    job_not_found: "Could not find the refine job. Run refine again.",
    job_not_refine: "Only results produced by refine can be applied. Run refine, then apply.",
    too_many_jobs: "This tenant has too many jobs in progress. Start again after some of them finish.",
    url_blocked: "This address cannot be used. Enter a public http(s) address.",
    fetch_failed: "Could not fetch the document from the address. Check that the address is correct and that the server responds.",
    fetch_redirect: "The address redirected too many times or to a blocked address. Enter the final address directly.",
    fetch_status: "The server at the address responded with an error. Check the address and try again in a moment.",
    fetch_too_large: "The fetched document exceeds the upload limit. Use a smaller document, or keep only the parts you need.",

    // -- Knowledge -----------------------------------------------------------------
    invalid_data_service_kind: "The selected Database is the wrong type. A Knowledge Base needs a postgres Database, so select one.",
    data_service_immutable: "You cannot change the Database after creation. To use a different Database, create a new Knowledge Base.",
    embedding_immutable: "You cannot change the embedding settings after creation. To use a different embedding, create a new Knowledge Base.",
    embedding_not_supported: "This provider has no embedding API. Select a provider that supports embeddings.",
    embedding_failed: "Could not create embeddings. Check the provider key and model name, then index again.",
    dimensions_required: "The platform does not know the dimensions of this embedding model. Enter the dimensions yourself.",
    pgvector_missing: "This PostgreSQL does not have the pgvector extension. Ask a superuser to run CREATE EXTENSION vector first.",
    query_failed: "The Database query failed. Check the connection details and the account's permissions.",
    too_many_documents: "The Knowledge Base has reached its document limit. Delete some documents, then add more.",
    unsupported_format: `This document format is not supported. Upload a document in one of these formats: ${DOCUMENT_FORMAT_LABEL}.`,
    db_unreachable: "Could not connect to the Database. Check the Host and Port, and the egress route from the platform namespace.",
    auth_failed: "Database authentication failed. Check the Database's Username and New password.",
    unknown_database: "The server has no database with that name. Check the database name you entered.",
    too_many_chunks: "The document has more chunks than the limit. Split the document or raise limits.max_chunks.",
    table_missing: "The vector table is missing from the Database. Create the Knowledge Base again.",
    empty_document: "No text was found in the document. If it is a scan, replace it with a file that contains text and upload it again.",
    extract_failed: "Could not read text from the document. Check that the file is not damaged and contains text, then upload it again.",
    body_missing: "The document body is missing from storage. Upload the file again.",
    encrypted_document: "The document is password-protected, so it cannot be opened. Remove the password and upload it again.",
    invalid_archive: "The document file has an invalid structure. Check that the file is not damaged, then upload it again.",
    archive_too_large: "The document's content is too large to read. Split the document and upload the parts.",
    extract_timeout: "Reading the document took longer than 30 seconds. Split the document and upload the parts.",
    extract_unavailable: "Could not reach the document reading service. Try again in a moment.",
    extract_busy: "The document reading service is busy. Try again in a moment.",
    upload_rate_limited: "You reached the per-minute upload limit. Wait a minute, then attach the files again.",
    too_many_uploads: "You have too many unsent attachments. Delete the ones you will not use, then attach again.",
    upload_busy: "A document you attached earlier is still being read. Attach again in a moment.",
    unknown_upload: "Could not find the attached file. Uploads may expire after an hour, so attach it again.",
    too_many_attachments: "You attached more files than one message allows. Attach fewer files and send again.",
    attachments_too_large: "This conversation has reached its limit on attachment size or number of images. Start a new conversation and attach again.",
    invalid_attachment: "Could not read the attached file. Check the file name and format, then attach it again.",
    images_unsupported: "This Agent's model does not accept images. Remove the images and send again.",
    attachments_need_filesystem: "This Agent does not accept document attachments. To use them, turn on the file Tools in the Agent definition and deploy again.",
    embedding_dimension_mismatch:
      "The embedding response has different dimensions from the Knowledge Base. If you changed the model, create a new Knowledge Base.",
    url_unreachable: "Could not fetch the URL. Check that it is a public address, not a private or cluster one, and that the document is 10 MB or less.",

    // -- 비서 ----------------------------------------------------------------------
    assistant_disabled: "The assistant is turned off in Platform Settings. Ask an admin to turn it on.",
    turn_in_progress: "The current reply has not finished yet. Send your message after it finishes.",
    turn_cancelled: "You stopped the request. The model has stopped, but Tool calls already sent may still complete.",
    turn_aborted: "The Agent could not finish the answer. Send it again.",
    turn_access_lost: "The answer was stopped because your access could not be confirmed.",
    turn_tokens_unavailable: "The tokens for calling Tools under your name could not be created. Send the message again shortly.",
    turn_grant_mismatch: "The turn did not start because the owner of this chat could not be confirmed. If this keeps happening, tell a platform admin.",
    no_turn_in_progress: "No reply is in progress. The reply has already finished, so send your next message.",
    runtime_feature_missing: "The deployed runtime image does not support this feature. Upgrade the image, then deploy again.",
    agent_waking: "The Agent's pod was stopped while unused and is starting again. Try again in a moment.",
    agent_asleep: "This Agent's pod is stopped while unused. Wake it and try again.",
    agent_busy: "The Agent is answering, so its pod cannot be stopped now. Try again after the answer ends.",
    agent_sleep_ineligible: "This Agent cannot scale to zero. See why in the Deployment tab.",
    preview_limit: "This tenant has reached its number of open previews. End another preview and try again.",
    preview_start_limit: "You have started as many previews as you can today. Try again tomorrow, or deploy to chat.",
    preview_disabled: "Previews are turned off on this platform. Deploy to chat.",
    preview_not_found: "There is no open preview. Start the preview again.",
    daily_budget_exceeded: "This Agent has used all of today's tokens. Try again tomorrow.",
    service_accounts_off: "This deployment does not use service accounts. To use them again, ask a platform admin.",
    runtime_rolling: "The Agent is switching to a new version or is not ready yet. Try again in a moment.",
    nothing_to_regenerate: "There is no reply to regenerate. Send a new message.",
    max_regenerations: "This reply has used all of today's regenerations. Continue with a new message.",
    max_input_chars: "The message is too long for this Agent. Shorten it and send it again.",
    run_closed: "This run is no longer waiting for a decision. It was already decided, stopped or refused at its deadline.",
    runs_unsupported: "This Agent's runtime image does not support runs. Upgrade the agent image and redeploy.",
    runs_need_memory: "Runs need a memory node. Attach one on the canvas and try again.",
    run_budget_required: "Runs need a session token budget. Set one in the Agent settings and try again.",
    runs_need_events: "Runs are unavailable because the platform is not receiving Agent events. Tell a platform admin.",
    trigger_needs_approval: "A webhook trigger needs an approval rule on every Tool of this Agent that writes. Add the rules on the canvas and try again.",
    schedule_too_frequent: "A schedule cannot run more often than every 5 minutes. Make the interval longer.",
    schedule_invalid_cron: "The repeat rule cannot be read. Write it as a five-field cron expression.",
    run_source_limit: "An Agent holds at most 20 schedules and 20 triggers. Delete unused ones and try again.",
    unknown_time_zone: "Unknown time zone. Write an IANA time zone name such as Asia/Seoul.",
    invalid_expiry: "Set the expiry between 1 and 365 days.",
    invalid_title: `A conversation name must be between 1 and ${SESSION_TITLE_MAX_CHARS} characters. Fix the name and save again.`,
    session_delete_incomplete: "The conversation history is too long to delete at once. Delete it again to remove the rest.",
    turn_not_found: "This reply was regenerated or removed from the conversation, so it cannot be rated. Refresh the page.",
    feedback_not_found: "This rating was already withdrawn. Refresh the page.",
    nothing_to_roll_back: "The revision you selected matches the deployed definition, so there is nothing to roll back. Select another revision.",
    evaluation_required:
      "This Agent must pass an evaluation before it deploys. Run an evaluation of this definition and deploy again once it passes, or give a reason and deploy without evaluation.",
    evaluation_consent_missing: "The person who chatted has not agreed to use this conversation as an evaluation case. Write the case yourself.",
    evaluation_case_pii:
      "The copied question or answer still holds text that looks like an email address, phone number, resident registration number or card number. Remove or replace the marked parts and save again.",
    evaluation_run_active: "An evaluation of this Agent is already running. Try again after it ends.",
    evaluation_run_limit: "This tenant has started all of today's evaluation runs. Try again tomorrow.",
    evaluation_set_changed: "The set changed after you reviewed it. Review the changed cases and run again.",
    evaluation_grader_provider: "This revision's model nodes do not use the grader's provider. Select a model of a provider this revision uses as the grader.",
    evaluation_set_in_gate: "The evaluation before deploy uses this set, so it cannot be deleted. Turn the gate off or point it at another set, then delete it.",
    evaluations_disabled: "Evaluations are turned off on this platform. Ask a platform admin.",
    evaluation_set_full: `A set's cases times its repeats can be at most ${EVALUATION_MAX_CASE_RUNS}. Remove cases or lower the repeats.`,
    export_range_too_long: `You can export feedback for up to ${plural(FEEDBACK_EXPORT_MAX_DAYS, "day", "days")} at a time. Shorten the range and export again.`,
    export_too_large: "There is too much feedback to export. Narrow the range with the rating filter and export again.",
    citation_not_found:
      "Could not find the original document for this source. The document may have been deleted or the Agent deployed again, so refresh the page.",
    source_masked:
      "This Agent has masking rules, so the original cannot be opened. Use the document name and excerpt shown for this source instead.",
    invalid_path: "The file path is invalid. Select the file again from the list.",
    file_too_large: "The file is too large to open or download. Ask the Agent to save the content in smaller parts.",
    file_refused_executable:
      "Files that can run code when opened cannot be downloaded: executables, shortcuts, disk images, documents with macros or embedded objects, and archives that hold them.",
    file_not_found:
      "Could not find this file. It may have disappeared when a reply was regenerated, so open this conversation's files again to see what is there now.",
    not_pending: "There is nothing waiting for approval. Refresh the page.",
    decision_count_mismatch: "The number of decisions sent does not match the number of pending calls. Refresh the page and approve again.",
    decision_mismatch: "The decisions sent do not match the pending calls. Refresh the page and approve again.",
    proposal_changed: "The proposal changed after you opened it. Check the new proposal and decide again.",
    remember_refused: "This decision cannot be remembered. Reload the page and decide again.",
    edit_name_mismatch:
      "The edited Tool name differs from the Tool in the approval request. Keep the Tool name, change only the arguments, and send again.",
  },
  fallback: "Could not process the request.",
  noChange: "Nothing changed, so nothing was saved.",
  refNames: {
    mcp_server_ids: "MCP Server",
    deployed_mcp_server_ids: "Deployed MCP Server",
    gateway_ids: "Gateway",
    agent_ids: "Agent",
    deployed_agent_ids: "Deployed Agent",
    knowledge_base_ids: "Knowledge Base",
    token_ids: "Read token",
  },
  refCountNames: {
    other_tenant_agents: "Agents in other tenants",
  },
  refNamed: (label: string, name: string): string => `${label} ${name}`,
  // 단수와 복수를 가를 수 없는 이름(Agents in other tenants)도 받으므로 수를 괄호에 적는다
  refCount: (label: string, count: number): string => `${label} (${count})`,
};
