import type { ReactNode } from "react";

import type { DataServiceKind } from "@/api/types";
import { Field, Input } from "@/components/ui/input";
import { useT } from "@/i18n";

/** 접속 정보 입력칸의 값이다. 칸에 적힌 문자열 그대로라 서버로 보낼 때는 connectionValues 로 바꾼다. */
export interface ConnectionValue {
  host: string;
  port: string;
  database: string;
  db: string;
  username: string;
}

/** 서버 본문에 들어갈 접속 정보다. database 는 postgres 에만, db 는 redis 에만 담긴다. */
export interface ConnectionBody {
  host: string;
  port: number;
  username: string | null;
  database?: string | null;
  db?: number | null;
}

/** 입력칸 값을 서버 본문의 모양으로 바꾼다. 빈 칸은 null 이 되고 종류에 맞지 않는 칸은 싣지 않는다. */
export function connectionValues(kind: DataServiceKind, v: ConnectionValue): ConnectionBody {
  const body: ConnectionBody = { host: v.host.trim(), port: Number(v.port), username: v.username.trim() || null };
  if (kind === "postgres") body.database = v.database.trim() || null;
  else body.db = v.db === "" ? null : Number(v.db);
  return body;
}

/**
 * Host·Port·DB 이름(또는 DB 번호)·Username 칸이다.
 *
 * 비밀번호는 만들 때는 필수이고 고칠 때는 "비우면 바꾸지 않음" 이라 뜻이 달라서 각 페이지가 `password` 로 넘긴다.
 * Username 옆 자리에 그대로 놓인다.
 */
export function ConnectionFields({
  kind,
  value,
  onChange,
  password,
}: {
  kind: DataServiceKind;
  value: ConnectionValue;
  onChange: (patch: Partial<ConnectionValue>) => void;
  password: ReactNode;
}) {
  const t = useT();
  const text = t.dataServices.connection;
  return (
    <>
      <div className="grid grid-cols-[1fr_8rem] gap-3">
        <Field label={text.host} hint={text.hostHint} htmlFor="ds-host" required>
          <Input id="ds-host" data-testid="ds-host" required value={value.host} onChange={(e) => onChange({ host: e.target.value })} />
        </Field>
        <Field label={text.port} hint={text.portHint} htmlFor="ds-port" required>
          <Input id="ds-port" data-testid="ds-port" type="number" min={1} max={65535} required value={value.port} onChange={(e) => onChange({ port: e.target.value })} />
        </Field>
      </div>
      {kind === "postgres" ? (
        <Field label={text.database} hint={text.databaseHint} htmlFor="ds-database">
          <Input id="ds-database" data-testid="ds-database" value={value.database} onChange={(e) => onChange({ database: e.target.value })} />
        </Field>
      ) : (
        <Field label={text.db} hint={text.dbHint} htmlFor="ds-db">
          <Input id="ds-db" data-testid="ds-db" type="number" min={0} value={value.db} onChange={(e) => onChange({ db: e.target.value })} />
        </Field>
      )}
      <div className="grid grid-cols-2 gap-3">
        <Field label={text.username} hint={text.usernameHint} htmlFor="ds-username">
          <Input id="ds-username" data-testid="ds-username" value={value.username} onChange={(e) => onChange({ username: e.target.value })} />
        </Field>
        {password}
      </div>
    </>
  );
}
