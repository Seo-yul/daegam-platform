import type { Messages } from "../types";
import { plural } from "./plural";

/** Database 의 목록, 폼, 상세와 연결 확인 (F6). 한국어의 DB 이름·DB 번호는 DB name·DB number 로 적어 Database 와 가른다. */
export const dataServices: Messages["dataServices"] = {
  placeholders: { display: "Orders DB", name: "orders-db" },
  list: {
    create: "Create Database",
    columns: { name: "Name", kind: "Kind", host: "Host", database: "DB name / number", verified: "Verification" },
    empty: (tenant: string): string => `Tenant ${tenant} has no Databases.`,
    title: "Database",
    search: "Search databases…",
  },
  connection: {
    hostHint: "The address the platform and Agents use to reach this Database inside the cluster, usually a Service DNS name.",
    portHint: "Usually 5432 for Postgres and 6379 for Redis.",
    database: "DB name",
    databaseHint: "The name of the database in Postgres. Redis does not use it.",
    db: "DB number",
    dbHint: "The Redis DB number. It must be 0 for an Agent's memory, because RediSearch builds indexes only in db 0.",
    usernameHint: "The account used to connect. The password goes into a K8s Secret and is not shown here again.",
    host: "Host",
    port: "Port",
    username: "Username",
  },
  tls: {
    enable: "Use a TLS port",
    enableHint:
      "Turn this on when the Redis serves a TLS port next to the plain one. The platform connects only over TLS; Agents move to the TLS port once they can use it, and MCP Servers at their next deploy.",
    port: "TLS port",
    portHint: "The port for TLS connections. Keep the Port above: Agents and MCP Servers that still connect without TLS use it.",
    ca: "CA certificate",
    caHint: "The PEM of the CA that issued the server certificate. It goes into a K8s Secret, and connections check the server certificate and the Host name against it.",
    caKeepHint: "Leave empty to keep the CA. Enter a PEM only to replace it.",
  },
  form: {
    kindHint: "Postgres or Redis. You cannot change the kind after creation.",
    passwordHint: "Leave empty for a Redis without authentication.",
    title: "New Database",
    kind: "Kind",
    kinds: { postgres: "postgres", redis: "redis" },
    cluster: "Cluster",
    password: "Password",
  },
  detail: {
    backToList: "Back to Databases",
    verify: "Check connection",
    verifying: "Checking…",
    staleCapabilities: "These results are from the last check, and the connection details have changed since. Check the connection again.",
    passwordHint: "Leave empty to keep the current password.",
    savedCredentials: "Saved. The connection details changed, so the verification was cleared. Check the connection again.",
    labels: {
      cluster: "cluster",
      secret: "secret",
      password: "password",
      passwordSet: "set",
      passwordNone: "none",
      tls: "tls",
      tlsNone: "none",
      tlsEndpoint: (port: number, secret: string): string => `port ${port} · CA ${secret}`,
      plainClosed: "plain port closed",
      plainConsumers: "plain consumers",
      plainConsumersNone: "No Agent or MCP Server connects to the plain port.",
      plainConsumersCount: (agents: number, servers: number): string => {
        const one = agents + servers === 1;
        const who = [agents ? plural(agents, "Agent", "Agents") : "", servers ? plural(servers, "MCP Server", "MCP Servers") : ""];
        return `${who.filter(Boolean).join(" and ")} still ${one ? "connects" : "connect"} to the plain port. Tell ${one ? "its owner, then redeploy it" : "their owners, then redeploy them"}.`;
      },
      plainConsumersImage: "An Agent moves to the TLS port only after the cluster's agent image is updated to the new release.",
      capabilities: "capabilities",
      edit: "Edit",
      newPassword: "New password",
    },
  },
  verify: {
    failed: "The connection failed.",
    common: {
      auth_failed: "Authentication failed. Enter the Username and New password again, save, and check again.",
    },
    postgres: {
      query_failed: "Connected, but the query was refused. Check that the verify account can read the list of extensions.",
    },
    redis: {
      select_failed: "The DB number is out of range for this Redis. Lower the DB number, or raise the databases setting on the server.",
      ping_failed: "Connected, but PING was refused. Check the permissions of the verify account.",
      io_error: "The connection dropped midway. Check the network path and the TLS settings.",
      tls_failed:
        "TLS certificate verification failed. Check that the CA certificate issued the server certificate and that the server certificate names the Host.",
      ca_missing: "The Secret with the CA certificate is missing. Enter the CA certificate again, save, and check again.",
    },
    noVersion: "No permission to read the Redis version",
    modules: (modules: string): string => `modules ${modules}`,
  },
  capability: {
    unverified: "Not verified",
    checkpointer: {
      ready: "Redis 8 ready",
      dbNotZero: (db: number): string => `Unusable with db ${db}`,
      modules_missing: "RediSearch and RedisJSON missing",
      unknown: "Account lacks permission",
    },
    pgvector: { installed: "pgvector installed", available: "pgvector can be installed", missing: "pgvector missing" },
    note: {
      checkpointer: {
        dbNotZero: (db: number): string =>
          `With db ${db}, it cannot serve as an Agent's memory, because RediSearch builds indexes only in db 0. Change the DB number above to 0. If you use it only for caching or session sharing, it works as is.`,
        modules_missing:
          "To serve as an Agent's memory, it must be Redis 8 with RediSearch and RedisJSON (the redis:8 image bundles both). If you use it only for caching or session sharing, it works as is.",
        unknown: "The verify account could not call MODULE LIST and FT._LIST. Grant it the permissions, or check with another account.",
        unverified: "Not checked yet. Check the connection to find out whether it can serve as an Agent's memory.",
      },
      pgvector: {
        missing: "Creating a Knowledge Base requires the pgvector extension. Ask a superuser to run CREATE EXTENSION vector first.",
        available:
          "The extension is not installed, but it can be. Creating a Knowledge Base tries CREATE EXTENSION, which fails at that point if this account is not a superuser.",
        unverified: "Not checked yet. Check the connection to find out whether you can create a Knowledge Base on it.",
      },
    },
  },
  status: {
    verified: "verified",
    unverified: "unverified",
  },
};
