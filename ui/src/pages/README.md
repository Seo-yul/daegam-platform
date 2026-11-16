# Page files

One directory is one resource, named like its URL path (`backend-apis/`, `mcp-servers/`). The routes are in
`ui/src/App.tsx`. File names say what the screen does.

| File | Role |
|---|---|
| `List.tsx` | The list |
| `Detail.tsx` | The detail. A resource without revisions is edited here too |
| `New.tsx` | Creating a resource without revisions. `backend-apis/:id/edit` reuses it to edit a Backend API, filled with the stored values and the name locked (D51) |
| `Form.tsx` | Creating and editing a resource with revisions |
| `DeployDialog.tsx` | The Agent's deploy dialog, kept in `agents/` because replicas, resources and the Agent exposure model (D27) come with it. MCP Servers and Gateways share `src/components/DeployDialog.tsx` |
| `Refine.tsx` | A version sub screen, `backend-apis/:id/versions/:n/refine?mode=minify\|llm`: refines one version's descriptions by rules or an LLM, adopts changes from a side-by-side diff and makes a new version (D25) |
| `Suggest.tsx` | `mcp-servers/:id/suggest`: tool name and description override suggestions adopted as a new revision (D37) |
| `Canvas.tsx` | The node canvas. It is the create screen (`agents/new`); a saved Agent's canvas sits in the definition tab of `Detail.tsx` (D26, D42) |

- Resources with revisions (`mcp-servers`, `gateways`, `skills`) add a revision on every save, so `Form.tsx` both
  creates and edits (`:id/edit`). For `skills` an edit is a new revision, so `skills/:id/edit` opens a new revision
  filled with the latest one (D64). `agents` follows the same revision rule with `Canvas.tsx` instead of a form; a save
  that changes only `layout` makes no revision.
- The other resources (`backend-apis`, `clusters`, `data-services`, `knowledge-bases`, `hub`) create with `New.tsx` and
  edit inside `Detail.tsx`.
- Definition and deployment are one resource (D42), so no directory handles deployments alone; the detail's deployment
  tab and the deploy dialogs are that place.
- Screens that are not resources have their own directories: `home-chat/` (the first screen `/chat`), `account/`,
  `admin/`, `audit/`, `auth/` and `tenant/`. `Placeholder.tsx` answers unknown paths.
- Detail pages enter through `queryGate` in `src/components/QueryGate.tsx` for loading and errors. The banners above the
  status card of MCP Server, Gateway and Agent details ("연결이 끊겨…", an unreachable cluster, the last error) are
  `InstanceAlerts` in `src/components/InstanceStatus.tsx`.
- Sections a detail uses (the publish dialog, exposure, the chat panel, the approval inbox) are not pages and live in
  `src/components/`, for example `PublishSection.tsx`, `ExposureSection.tsx`, `ChatPanel.tsx` and `ApprovalInbox.tsx`.
  Canvas parts are in `src/components/canvas/`.
- Screen tests live in `src/test/`, not next to the pages. A resource's tests start in `<resource>.test.tsx` and a large
  one is split by topic into `<resource>-<topic>.test.tsx` (`agents.test.tsx` with `agent-canvas`, `agent-detail`,
  `agent-chat` and `agent-sessions`; `mcp-servers.test.tsx` with `mcp-server-deployment` and `mcp-server-publish`). A
  state every screen shows the same way (list load failure, missing detail, undeployed overview, runtime feature gates)
  is one table in `resource-states.test.tsx` with a row per screen.

