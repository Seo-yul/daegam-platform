import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import type { HubAccess } from "@/api/types";
import { AccessPolicyEditor } from "@/components/AccessPolicyEditor";
import { dictionary } from "@/i18n";
import { describeSubjects, emptyAccess, isEmptyAccess, normalizeAccess } from "@/lib/hubAccess";
import { selectValue } from "./select";

/** 편집기는 controlled 다. 테스트는 마지막 onChange 값을 읽는다. */
function Harness({ canShare, crossTenant, onChange, initial }: { canShare: boolean; crossTenant?: boolean; onChange: (v: HubAccess) => void; initial?: HubAccess }) {
  const [value, setValue] = useState<HubAccess>(initial ?? emptyAccess());
  return (
    <AccessPolicyEditor
      value={value}
      onChange={(v) => {
        setValue(v);
        onChange(v);
      }}
      canShare={canShare}
      crossTenant={crossTenant}
      testId="access"
    />
  );
}

describe("hubAccess helpers", () => {
  it("normalizes partial input and detects the empty policy", () => {
    const a = normalizeAccess({ consume: { groups: ["g"] } });
    expect(a.consume).toEqual({ tenants: [], roles: [], groups: ["g"] });
    expect(a.view).toEqual({ tenants: [], roles: [], groups: [] });
    expect(isEmptyAccess(a)).toBe(false);
    expect(isEmptyAccess(normalizeAccess(null))).toBe(true);
  });

  it("describes subjects with the per-action default role", () => {
    const ko = dictionary("ko");
    expect(describeSubjects(ko, "view", { tenants: [], roles: [], groups: [] })).toBe("게시한 테넌트 · viewer 이상");
    expect(describeSubjects(ko, "consume", { tenants: ["*"], roles: [], groups: [] })).toBe("모든 테넌트 · developer 이상");
    expect(describeSubjects(ko, "manage", { tenants: ["billing"], roles: ["tenant-admin"], groups: ["g"] })).toBe("테넌트 billing · role tenant-admin 이상 · 그룹 g");
  });

  it("describes subjects in English with the owner first in sentence case", () => {
    const en = dictionary("en");
    expect(describeSubjects(en, "view", { tenants: [], roles: [], groups: [] })).toBe("Publishing tenant · viewer or higher");
    expect(describeSubjects(en, "manage", { tenants: ["billing"], roles: ["tenant-admin"], groups: ["g"] })).toBe("Tenants: billing · role: tenant-admin or higher · groups: g");
  });
});

describe("AccessPolicyEditor", () => {
  it("renders three rows and assembles roles: [value], tenant chips, * and groups", async () => {
    const user = userEvent.setup();
    let last: HubAccess = emptyAccess();
    render(<Harness canShare onChange={(v) => (last = v)} />);
    const fields = screen.getByTestId("access-fields");
    for (const a of ["view", "consume", "manage"]) expect(within(fields).getByTestId(`access-${a}`)).toBeInTheDocument();
    expect(screen.getByTestId("access-hint")).toHaveTextContent("view 는 viewer 이상, consume·manage 는 developer 이상");

    await selectValue(user, "access-view-role", "developer");
    expect(last.view.roles).toEqual(["developer"]);
    await selectValue(user, "access-view-role", "");
    expect(last.view.roles).toEqual([]);

    await user.type(screen.getByTestId("access-consume-tenants"), "billing, crm{enter}");
    expect(last.consume.tenants).toEqual(["billing", "crm"]);
    expect(screen.getByTestId("access-consume-tenants-chip-billing")).toBeInTheDocument();
    await user.click(screen.getByTestId("access-consume-tenants-all"));
    expect(last.consume.tenants).toEqual(["*", "billing", "crm"]);
    await user.click(screen.getByTestId("access-consume-tenants-remove-billing"));
    expect(last.consume.tenants).toEqual(["*", "crm"]);
    await user.click(screen.getByTestId("access-consume-tenants-all"));
    expect(last.consume.tenants).toEqual(["crm"]);

    // blur 로도 값이 들어가고 중복은 무시한다
    await user.type(screen.getByTestId("access-manage-groups"), "g-pay");
    await user.tab();
    await user.type(screen.getByTestId("access-manage-groups"), "g-pay{enter}");
    expect(last.manage.groups).toEqual(["g-pay"]);
  });

  it("without hub:share the tenant input and * are disabled with the reason, but existing chips can still be removed", async () => {
    const user = userEvent.setup();
    let last: HubAccess = emptyAccess();
    const initial = normalizeAccess({ view: { tenants: ["*", "billing"] } });
    render(<Harness canShare={false} onChange={(v) => (last = v)} initial={initial} />);
    expect(screen.getByTestId("access-view-tenants")).toBeDisabled();
    expect(screen.getByTestId("access-consume-tenants-all")).toBeDisabled();
    expect(screen.getByTestId("access-share-reason")).toHaveTextContent("다른 테넌트에 열려면 hub:share 권한이 필요합니다");
    // 좁히기(해제·칩 제거)는 manage 만으로 된다
    const all = screen.getByTestId("access-view-tenants-all");
    expect(all).toBeChecked();
    expect(all).toBeEnabled();
    await user.click(screen.getByTestId("access-view-tenants-remove-billing"));
    expect(last.view.tenants).toEqual(["*"]);
    await user.click(all);
    expect(last.view.tenants).toEqual([]);
    expect(all).toBeDisabled();
    expect(screen.getByTestId("access-view-role")).toBeEnabled();
  });

  it("crossTenant=false locks the whole tenant row even for platform-admin", () => {
    render(<Harness canShare crossTenant={false} onChange={() => {}} />);
    expect(screen.getByTestId("access-manage-tenants")).toBeDisabled();
    expect(screen.getByTestId("access-manage-tenants-all")).toBeDisabled();
    expect(screen.getByTestId("access-cross-tenant-reason")).toHaveTextContent("다른 테넌트에 열 수 없습니다");
    expect(screen.queryByTestId("access-share-reason")).not.toBeInTheDocument();
  });

  it("collapsible mode starts closed and toggles the fields", async () => {
    const user = userEvent.setup();
    render(<AccessPolicyEditor value={emptyAccess()} onChange={() => {}} canShare collapsible testId="access" />);
    expect(screen.getByTestId("access-toggle")).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("access-fields")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("access-toggle"));
    expect(screen.getByTestId("access-fields")).toBeInTheDocument();
  });
});
