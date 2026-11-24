import type { PausedSources } from "../ko/warnings";
import type { Messages } from "../types";
import { clusters } from "./clusters";
import { plural } from "./plural";

/** 상태 경고. label 은 표 셀에 들어가므로 32자를 넘지 않는다. */
export const warnings: Messages["warnings"] = {
  codes: {
    network_policy_unenforced: {
      label: "Policies not enforced",
      text: "This cluster has no controller that enforces NetworkPolicy, so the NetworkPolicies the platform creates block nothing. The platform does not rely on them for protection, so the deployment goes ahead. Inside the cluster, this endpoint is open without authentication either way. Ask a cluster admin to install an enforcing controller such as Calico or Cilium, and until then, do not deploy sensitive backends to this cluster.",
    },
    metadata_block_unsupported: {
      label: "Metadata block not applied",
      text: "This cluster is set to block the instance metadata address, but its NetworkPolicy controller is not one the platform has confirmed (kube-router, Calico, Antrea), so the platform applies no egress policy. On other controllers this policy can also cut pods off from the API server. Block the address in the node settings or with the controller's own policy.",
    },
    metadata_block_skipped: {
      label: "Some namespaces not blocked",
      text: "These namespaces hold egress policies the platform did not make, so the platform did not add the metadata block there: NetworkPolicy only adds allowances, and the block would lift their existing restrictions. Block the address in those namespaces' own policies: {namespaces}",
    },
    unauthenticated_exposure: {
      label: "Exposed without auth",
      text: "The platform runs in header mode, so this endpoint is open to the outside without authentication. Turn off unauthenticated exposure for the cluster, or switch the platform to oidc mode.",
    },
    redis_tls_binding: {
      label: "Redis TLS binding",
      text: "The bound Redis has a TLS port, so this binding's address is a rediss:// address on the TLS port. This MCP Server runs an image you specified, so check that the image reads the binding's TLS and CA file variables and connects over TLS.",
    },
    turn_tokens_owner_cedar: {
      label: "Blocks user identity",
      text: "This Gateway runs Cedar policies saved in the Advanced tab (a ConfigMap, or policies with entities), so the platform cannot bind Agent calls to it. Every Agent on this cluster therefore calls Gateway Tools under the Agent's name instead of the user's, and if this Gateway is exposed, every Agent on every cluster does. Restate the rules in Authz policy (Cedar) and remove that policy in the Advanced tab.",
    },
    idp_names_in_policy: {
      label: "Check IdP names",
      text: "This Gateway's Authz policy (Cedar) screens callers by IdP group or client names. Platform tokens carry IdP groups only with the tenant name added and carry no client id, so after the switch to platform tokens these conditions no longer match. Move them into Tool rules by role or group.",
    },
    turn_tokens_authz_forbid: {
      label: "Check forbid policy",
      text: "This Gateway's Authz policy (Cedar) holds a forbid policy. The tokens Agents use to call Tools under a user's name carry no groups, so if that policy screens users by group, Agent calls are refused. Write the condition on the tenant (claim_tenant) instead of a group.",
    },
    rules_pending: {
      label: "Rules pending",
      text: "This Gateway is still rolling out, so changed Tool rules apply once every pod runs the new version. Until then the previous rules apply.",
    },
    rules_gateways_behind: {
      label: "Gateways updating",
      text: "Some Gateways that use this server have not applied its changed Tool rules yet, so its Tools with rules cannot be called through them for now. The platform reapplies them shortly.",
    },
    rules_front_rolling: {
      label: "Front updating",
      text: "The exposure front is still rolling out, so calls through it meet the previous Tool rules. The new rules apply once every pod has rolled.",
    },
    front_handover_pending: {
      label: "Front switch pending",
      withReason: (reason: string): string =>
        `This MCP Server's external address still goes through the previous front proxy. The new front is ${reason}, so the platform has not switched yet. If this lasts, check the new front's cluster resources in the Advanced tab.`,
    },
    conditions_unresolved: {
      label: "Conditions unresolved",
      text: "The server cannot find an argument that a condition names in its Tool, so that Tool refuses every call. Check the argument names in the Tool rules.",
    },
    rules_not_enforced: {
      label: "Rules not enforced",
      text: "In header auth mode the Tool rules' callers are not enforced. Ask a platform admin for a token auth mode if you need them.",
    },
    conditions_closed: {
      label: "Conditions not enforced",
      text: "Not every pod of this server is yet known to run an image that enforces argument conditions, so Tools with conditions are closed. They open once that is confirmed; on an old image, upgrade the cluster's runtime image and redeploy.",
    },
    rules_toolhive_old: {
      label: "Cluster lacks rule support",
      text: "This cluster's ToolHive does not support Tool rules, so Tools with rules are closed. Ask a platform admin to upgrade ToolHive.",
    },
    rules_owner_cedar_closed: {
      label: "Rules cannot apply",
      text: "This Gateway runs Cedar saved in the Advanced tab, so it cannot apply the servers' Tool rules and Tools with rules cannot be called. Remove that policy in the Advanced tab.",
    },
    rules_digest_behind: {
      label: "Rules updating",
      text: "This Gateway has not taken a backend's changed Tool rules yet, so its ruled Tools cannot be called for a moment. The platform applies them again shortly.",
    },
    published_owner_forbid: {
      label: "Backend use blocked",
      text: "This Gateway's Authz policy (Cedar) holds a forbid statement, so every call through a Gateway that uses it as a backend is refused. Move the forbid statement's condition into Tool rules.",
    },
    published_backend_closed: {
      label: "Backend refuses calls",
      text: "A published Gateway used as a backend refuses this Gateway's calls. Its Authz policy (Cedar), or that of a published Gateway below it, holds a forbid statement, or it has not been rendered to accept these calls yet. Ask whoever manages that Gateway to check it.",
    },
    published_consumer_pending: {
      label: "Waiting for backend",
      text: "A published Gateway used as a backend has not been rendered again to accept this Gateway yet, so its Tools cannot be called for a moment. The platform applies it shortly. An Agent deployed over this Gateway meanwhile does not see those Tools until it is deployed again.",
    },
    published_names_unknown: {
      label: "Tool names unknown",
      text: "The Tool names of a published Gateway used as a backend were unknown at deploy, so that Gateway is not called. Run Tool refresh on the original Gateway's backends, then deploy this Gateway again.",
    },
    rules_tool_missing: {
      label: "Ruled Tool missing",
      text: "The backend no longer offers some Tools that have rules. If the backend renamed a Tool, change the rule to the new name and deploy the Gateway again.",
    },
    code_mode_closed: {
      label: "Code mode off",
      choices: {
        tools: "A Tool this Gateway offers is no longer read-only, so Code mode was turned off. Remove that Tool from the backend's Tool selection and deploy again, or mark it read-only on its server or hub item.",
        toolhive: "This cluster's ToolHive does not support Code mode, so Code mode was turned off. Ask a platform admin to upgrade ToolHive.",
        unknown_names: "A backend's Tool names are unknown, so Code mode was turned off. Refresh the Tools of that hub item, or select that backend's Tools, and deploy again.",
        reserved_name:
          "A Tool this Gateway offers is named execute_tool_script, so Code mode was turned off. Select that backend's other Tools, or set Conflict resolution to prefix, and deploy again.",
        exposed: "This Gateway is exposed, so Code mode was turned off. Turn off exposure or Code mode and save again.",
        owner_cedar: "This Gateway runs Cedar saved in the Advanced tab, so Code mode was turned off. Remove that policy in the Advanced tab and save again.",
      },
    },
    code_mode_tools_hidden: {
      label: "Tools hidden by Code mode",
      text: "Some Tools are not reported read-only by their server, so this Gateway with Code mode on hides them and refuses their calls. For an MCP Server's Tool, upgrade the cluster's runtime image and deploy that server again; for a hub item's Tool, remove it from the backend's Tool selection.",
    },
    agent_tool_hidden: {
      label: "Tool hidden",
      text: "A rule hides some Tools this Agent selected.",
      causes: {
        not_named: "Cause: the server's or Gateway's rule does not name this Agent (add it).",
        item: "Cause: a hub item's rule (item rules admit no Agent).",
        oidc: "Cause: the Gateway still accepts only IdP tokens (rules cannot name Agents until the switch to platform tokens).",
        reissue: "Cause: this Agent's tokens are being reissued (it opens after the restart).",
      },
    },
    sandbox_gate_closed: {
      label: "Code execution unavailable",
      choices: clusters.sandbox.reason,
    },
    sandbox_override: {
      label: "Allowed without check",
      text: "Code execution is allowed without the isolation check by a development setting. Do not use this setting in production.",
    },
    runtime_tool_refusals: {
      label: "Runtime update needed",
      text: "This Agent's runtime image cannot handle refused Tool calls, so a chat fails when a Tool with rules refuses it. Upgrade the cluster's agent image and redeploy.",
    },
    tool_marks_changed: {
      label: "Read-only marks changed",
      text: "A server or hub item this Agent uses changed a Tool's read-only mark. Redeploy this Agent to apply it to its approval settings.",
    },
    outside_header_refused: {
      label: "Header value refused",
      text: "The outside server refused this hub item's header value on the platform's last call. Check the value and save it again with Replace header value.",
    },
    outside_sse_unsupported: {
      label: "SSE server not supported",
      text: "This hub item's server speaks only SSE, so the platform cannot read its Tools and Gateways that use it cannot be deployed. Create a new item with the server's streamable-http address.",
    },
    outside_header_missing: {
      label: "Header value missing",
      text: "The header value of a hub item used as a backend was not found, so it is called without the header. Ask whoever manages the hub item to save the header value again.",
    },
    // 허브 image 항목의 허용과 거부 (P55 6절)
    hub_image_unverified: {
      label: "Signature not verified",
      text: "This item's image was registered without a signature check. Create a new item from an image copied to the company registry and signed.",
    },
    hub_image_refused: {
      label: "Refused image",
      text: "A platform-admin refused this item's image. It keeps running for now, but Gateways that use this item will be refused at their next redeploy. Create the item again from a new image.",
    },
    hub_backend_refused: {
      label: "Refused backend image",
      text: "A backend of this Gateway is a hub item whose image a platform-admin refused. It keeps running for now, but its next redeploy will be refused. Remove that backend in the editor and save a new revision.",
    },
    hub_image_key_revoked: {
      label: "Signing key revoked",
      text: "The key that admitted this item's image was revoked. Create the item again from an image signed with the new key.",
    },
    hub_access_revoked: {
      label: "Hub access revoked",
      text: "A hub item's access policy has narrowed, so this Gateway can no longer select some of its backends. It keeps working for now, but the next deployment will be rejected. Ask the tenant that published the item for access, or remove that backend in Edit and save a new revision.",
    },
    skill_access_revoked: {
      label: "Skill access revoked",
      text: "A Skill's access policy has narrowed, so this Agent can no longer attach some of its Skills. It keeps working for now, but the next save or deployment will be rejected. Ask the Skill owner for access, or remove that Skill on the canvas and save a new revision.",
    },
    no_shared_state: {
      label: "Sessions not shared",
      text: "The graph has no memory node, so sessions live only inside the pod. They are lost when the pod restarts and are not shared between replicas. Add a memory node and connect a Database to keep sessions.",
    },
    pending_sessions_revision_changed: {
      label: "Sessions awaiting approval",
      text: "Some sessions are waiting for approval. When you deploy again, they continue on the new revision's graph.",
    },
    runtime_features_unknown: {
      label: "Runtime features unknown",
      text: "This image has not reported which features it supports yet. An init container checks them when the pod starts, so wait a moment and check the status again.",
    },
    evaluation_gate_off: {
      label: "Evaluation before deploy off",
      text: "Evaluations are turned off on the platform, so this deployed without the evaluation before deploy. Once they are on again, the next deploy checks it.",
    },
    runtime_feature_dropped: {
      label: "Some features left out",
      text: "The runtime image does not support some features, so they were left out of this deployment. To use them, upgrade the cluster's Agent image and deploy again.",
    },
    agent_call_cycle: {
      label: "Agent call loop",
      text: "An Agent that this Agent calls also calls this Agent. That call is refused when it runs. To break the loop, remove the Agent Tool from one of the two Agents.",
    },
    agent_source_not_serving: {
      label: "Called Agent not serving",
      text: "An Agent connected as a Tool does not take conversations now, so calls are refused.",
    },
    agent_call_timeout_short: {
      label: "Wait too short",
      text: "This Agent's turn time is not long enough beyond the called Agent's, so a call may stop before the answer comes.",
    },
    agent_calls_broken_by_deploy: {
      label: "Calls refused",
      text: "This deploy dropped the memory node or the session token budget, so Agents that call this Agent as a Tool are refused.",
    },
    tools_source_newer: {
      label: "Unknown Tool source",
      text: "Some tools nodes use a source kind from a newer platform version, so this Agent runs without them. Upgrade the platform to that version or later to use those Tools again.",
    },
    turn_tokens_unsupported: {
      label: "User identity unsupported",
      text: "This Agent's runtime image cannot pass the user who started the chat to its Tool calls, so it calls every Tool under the Agent's name. Upgrade the cluster's agent image and redeploy.",
    },
    runtime_features_invalid: {
      label: "Invalid feature list",
      text: "The feature list from the init container has an invalid format, so it was ignored. The Agent keeps running, but feature checks treat the list as empty. Check that the image is a valid release.",
    },
    builtin_tool_name: {
      label: "Tool name clashes with built-in",
      text: "A connected tool source offers a tool named like a built-in runtime tool (the file tools, task and others), so this Agent keeps the built-in and does not use that source's tool. Rename the tool at the source or connect it through a Gateway in prefix mode.",
    },
    correlation_hook_missing: {
      label: "LLM correlation header missing",
      text: "The SDK client of a model this Agent uses could not attach the correlation header (traceparent). Chats still work, but call records at an LLM gateway or provider cannot be matched to this Agent's traces by id. This is a problem in the runtime image, so tell a platform admin.",
    },
    tools_behind: {
      label: "New Tools",
      text: "The Tool source has new Tools since the deploy. Deploy again to add them.",
    },
    tools_missing: {
      label: "Missing Tools",
      text: "Some Tools fixed at deploy time are gone from the Tool source, so they are not called. Check the source or deploy again.",
    },
    platform_tool_exposed: {
      label: "Web Tools on an exposed Gateway",
      text: "This exposed Gateway reaches platform web Tools, but Platform Settings does not allow them on exposed Gateways. It keeps running, but its next redeploy is refused. Turn exposure off, or ask an admin to allow exposed Gateways.",
    },
    pinned_agents_keep_tools: {
      label: "Agents keep their Tools",
      text: "Agents using this Gateway keep the Tools fixed at their deploy until they are deployed again.",
    },
    web_backend_pinned_agents: {
      label: "Agents need a redeploy",
      text: "Agents using this Gateway can use web Tools after they are deployed again.",
    },
    outside_mark_off: {
      label: "Outside mark off",
      text: "The outside content mark is off for a Tool of an outside service. Instructions in its results can call write Tools without approval.",
    },
    read_only_by_canvas: {
      label: "Read-only by canvas",
      text: "A Tool whose server cannot say whether it is read-only is marked read-only on the canvas. If it changes data and has no approval setting, it runs without approval even in chats that hold outside content.",
    },
    outside_writes_refused: {
      label: "No writes after outside content",
      text: "This graph has no memory node, so it cannot ask for approval. Once a chat holds outside content, Tools that can change data do not run. Add a memory node to ask for approval instead.",
    },
    web_fetch_allowlist_empty: {
      label: "No allowed domains",
      text: "The allowed domains are empty, so web_fetch is refused in graphs with data Tools and in conversations with attached files.",
    },
    unattached_node: {
      label: "Unconnected node",
      text: "Some nodes cannot be reached from the entry node, so they are ignored. Check the connections on the canvas.",
    },
    unsupported_param: {
      label: "Ignored setting",
      text: "Some model settings are not supported by the selected provider, so they are ignored. Remove them from the model node, or select a provider that supports them.",
    },
    unknown_node_keys: {
      label: "Unknown node settings",
      text: "A tools node has settings this platform version does not know, so they are ignored. Check the setting names, for example allow_domains.",
    },
    knowledge_base_empty: {
      label: "Empty Knowledge Base",
      text: "The Knowledge Base that the knowledge node points to has no indexed documents, so searches return no results. Upload documents and index them.",
    },
    knowledge_base_failed: {
      label: "Documents failed to index",
      text: "Some documents in the Knowledge Base that the knowledge node points to failed to index. Check the failed documents in the Knowledge Base.",
    },
    runs_paused_by_deploy: {
      label: "Runs paused",
      text: ({ schedules, triggers, broken }: PausedSources): string =>
        `This deploy pauses ${plural(schedules, "schedule", "schedules")} and ${plural(triggers, "trigger", "triggers")} that run as someone else, and ${broken} whose settings no longer hold.`,
    },
  },
  hubCodes: {
    rules_tool_missing: {
      label: "Ruled Tool missing",
      text: "The image no longer offers some Tools that have rules. If the image renamed a Tool, change the rule to the new name and save it.",
    },
  },
  // hint 는 fixHint 의 값이고 문장 가운데 이어진다
  prose: {
    prefixFallback: {
      label: "Switched to prefix mode",
      text: (hint: string): string =>
        `Some backends have unknown Tool names, so the Gateway switches to prefix mode. Each Tool name gets the workload name in front, as in <workload>_<tool>. To keep the original names, ${hint}`,
    },
    priorityConflict: {
      label: "Name conflicts not prevented",
      text: (hint: string): string =>
        `The Gateway is in priority mode, but some backends have unknown Tool names. Colliding names cannot be renamed, and the workload registered first wins without a warning. Switch to prefix mode, or ${hint}`,
    },
    filterUnchecked: {
      label: "Tool filter not checked",
      text: (hint: string): string => `The hub item's Tool names are unknown, so the selected Tool filter could not be checked. To check it, ${hint}`,
    },
    tooManyBackends: {
      label: "Too many backends",
      text: "This Gateway has more than 20 backends. Every session calls tools/list on all of them, so consider splitting the Gateway.",
    },
  },
  // 경고 문장 가운데 쉼표 뒤에 이어지므로 소문자로 시작한다
  fixHint: {
    refresh: "refresh the Tools on the hub item.",
    redeploy: "deploy the source again. Internal and published items cannot refresh their Tools.",
    both: "refresh the Tools on the hub item, or deploy the source again if it is an internal or published item.",
  },
};
