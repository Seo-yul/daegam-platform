/**
 * 복제 대화상자 (D42). Agent·MCP Server·Gateway·Skill 상세가 같이 쓴다.
 *
 * 정의만 베낀 `draft` 를 만들고 배포 쪽은 따라오지 않는다. 세 상세가 같은 상태·같은 초기화·같은 폼을
 * 각자 적고 있었다. 리소스마다 다른 것은 이름과 복제 API, 노출 폼의 종류뿐이라 그것만 인자로 받는다.
 * 배포가 없는 Skill(D64)은 노출 폼 없이 이름 두 칸만 쓰고 설명 문장을 제 것으로 바꾼다.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ComponentType, type ReactNode } from "react";
import { useNavigate } from "react-router";

import type { ExposureInput } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { ExposureSection, useResolvedClusterId } from "@/components/ExposureSection";
import { NameFields, isNameFieldError, nameFieldErrors, type NamePlaceholders, type NameValue } from "@/components/NameFields";
import { Button } from "@/components/ui/button";
import { Modal } from "@/components/ui/modal";
import { type Messages, messages, useT } from "@/i18n";
import { EMPTY_EXPOSURE, exposureFieldErrors, exposureInvalid, isExposureFieldError, toExposureInput, type ExposureErrors, type ExposureForm } from "@/lib/exposure";
import { displayName, type Named } from "@/lib/names";

/** 복제본의 노출 폼 한 벌. Agent 는 CORS·허용 그룹이 더 붙은 자기 폼을 넘긴다. */
export interface CloneExposureKit<F, I> {
  empty: F;
  toInput: (f: F) => I;
  /** 칸 아래에 빨간 줄이 선 상태. 복제 버튼을 잠근다 */
  invalid: (f: F) => boolean;
  Section: ComponentType<{ clusterId: string | null; name: string; value: F; onChange: (next: F) => void; errors?: ExposureErrors; showNamespace?: boolean }>;
}

/** MCP Server·Gateway 의 노출 폼. */
export const EXPOSURE_KIT: CloneExposureKit<ExposureForm, ExposureInput> = { empty: EMPTY_EXPOSURE, toInput: toExposureInput, invalid: exposureInvalid, Section: ExposureSection };

export interface CloneDialogProps<F, I> {
  open: boolean;
  onClose: () => void;
  /** 화면에 쓰는 리소스 이름 ("MCP Server"). */
  resource: string;
  /** 목록 쿼리 키의 머리이자 상세 경로의 머리 ("mcp-servers" 면 /mcp-servers/<id>). */
  collection: "agents" | "mcp-servers" | "gateways" | "skills";
  source: Named & { revision: number };
  /** 베끼는 것. Agent 는 캔버스 그래프이고 나머지는 정의다. 부르는 말은 사전의 detail.revisionSubject 다. */
  sourceNoun: keyof Messages["detail"]["revisionSubject"];
  /** 기본 설명 문장을 대신한다. 배포가 없는 리소스가 쓴다. */
  description?: ReactNode;
  placeholders: NamePlaceholders;
  /** 클러스터 리소스 이름 미리보기의 접두어 (mcp, gw, agent). 클러스터에 뜨지 않는 리소스는 주지 않는다. */
  previewPrefix?: string;
  /** 노출 폼. 배포가 없는 리소스는 주지 않는다. */
  exposure?: CloneExposureKit<F, I>;
  clone: (body: { name: string; display_name?: string; exposure?: I }) => Promise<{ id: string }>;
}

export function CloneDialog<F, I>({ open, onClose, resource, collection, source, sourceNoun, description, placeholders, previewPrefix, exposure: kit, clone: cloneFn }: CloneDialogProps<F, I>) {
  const t = useT();
  const text = t.detail.cloneDialog;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [names, setNames] = useState<NameValue>({ displayName: "", name: "" });
  const [exposure, setExposure] = useState<F | undefined>(kit?.empty);
  // 기본 표시 이름은 사람이 고쳐 쓰는 값이라 연 때의 언어로 한 번 적는다. 언어가 바뀌었다고 입력을 되돌리지 않는다
  useEffect(() => {
    if (open) {
      setNames({ displayName: messages().detail.cloneDialog.copyName(displayName(source)), name: `${source.name}-copy` });
      setExposure(kit?.empty);
    }
  }, [open, source, kit?.empty]);

  const clone = useMutation({
    mutationFn: () =>
      cloneFn({
        name: names.name.trim(),
        display_name: names.displayName.trim() || undefined,
        exposure: kit && exposure !== undefined ? kit.toInput(exposure) : undefined,
      }),
    onSuccess: async (made) => {
      await queryClient.invalidateQueries({ queryKey: [collection] });
      onClose();
      navigate(`/${collection}/${made.id}`);
    },
  });

  return (
    <Modal open={open} title={text.title(resource)} testId="clone-dialog" closeTestId="clone-dialog-close" onClose={() => (clone.isPending ? undefined : onClose())}>
      <form
        className="flex flex-col gap-3 px-5 py-4"
        data-testid="clone-form"
        onSubmit={(e) => {
          e.preventDefault();
          clone.mutate();
        }}
      >
        <p className="text-sm text-muted">
          {description ?? text.body(t.detail.revisionSubject[sourceNoun], source.revision, resource)}
        </p>
        <NameFields testId="clone" placeholders={placeholders} value={names} onChange={setNames} preview={previewPrefix ? { prefix: previewPrefix } : undefined} errors={nameFieldErrors(clone.error)} />
        {kit && exposure !== undefined ? <CloneExposure kit={kit} name={names.name} value={exposure} onChange={setExposure} error={clone.error} /> : null}
        {!isNameFieldError(clone.error) && !isExposureFieldError(clone.error) ? <ErrorMessage error={clone.error} testId="clone-error" /> : null}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" disabled={clone.isPending} onClick={onClose}>
            {t.common.cancel}
          </Button>
          {/* 칸 아래에 "규칙에 맞지 않습니다" 가 떠 있는데 저장이 눌리면 안 된다 */}
          <Button type="submit" data-testid="clone-submit" disabled={clone.isPending || !names.name.trim() || (kit !== undefined && exposure !== undefined && kit.invalid(exposure))}>
            {clone.isPending ? text.cloning : t.shared.detailActions.clone}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

/** 복제본의 노출 폼. 노출 폼이 있는 리소스만 그리므로 클러스터 조회도 그때만 나간다. */
function CloneExposure<F, I>({ kit, name, value, onChange, error }: { kit: CloneExposureKit<F, I>; name: string; value: F; onChange: (next: F) => void; error: unknown }) {
  const t = useT();
  // 복제본은 아직 배포되지 않아 클러스터가 없다. 노출 기본값(host 템플릿·IngressClass)은 테넌트 기본 클러스터에서 끌어온다
  const clusterId = useResolvedClusterId(null);
  const Section = kit.Section;
  return (
    <div className="flex flex-col gap-3">
      {/* 노출용 둘째를 한 호출로 만드는 자리다 (D42). 여기서 켜면 그 설정이 복제본의 draft 에 남고 배포할 때 쓰인다 */}
      <p className="text-sm font-semibold text-heading">{t.detail.cloneDialog.exposure}</p>
      <Section clusterId={clusterId} name={name} value={value} onChange={onChange} errors={exposureFieldErrors(error)} showNamespace={false} />
    </div>
  );
}
