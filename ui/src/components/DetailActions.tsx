/**
 * 상세 화면 제목 줄의 공통 동작 셋: 배포 해제·복제·삭제.
 *
 * MCP Server·Gateway·Agent 상세가 같은 셋을 같은 뜻으로 쓴다. 전에는 셋 다 `···` 더보기 안에
 * 숨어 있었는데, 세 항목뿐이라 접을 값이 없고 한 번 더 눌러야 보이는 값도 없었다.
 *
 * 배포 해제만 낱말로 서고 복제·삭제는 아이콘이다. 배포 해제는 지금 돌고 있는 것을 내리는 일이라
 * 무엇을 하는지 글자로 읽혀야 하고, 나머지 둘은 어느 화면에서나 같은 뜻이라 그림 하나로 선다.
 * 삭제는 손을 올렸을 때만 붉어진다 (`IconButton` 의 danger).
 *
 * 낱말 버튼은 `danger`(붉은 글자)다. 돌고 있는 것을 멈추는 일이라 주 동작(`default`, 먹 바탕)과 색으로 갈리고,
 * 테두리 없이 글자만 서서 머리 줄에서 주 동작보다 앞에 나서지 않는다 (P81 4절).
 *
 * `onUndeploy` 를 주지 않으면 그 버튼은 서지 않는다. 돌고 있지 않거나 배포 권한이 없을 때다. 복제·삭제도 같다. 배포가 없는
 * Skill 상세는 권한(복제는 consume·skill:write, 삭제는 manage)이 있을 때만 그 둘을 넘긴다.
 */
import { CopyPlus, PowerOff, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { IconButton } from "@/components/ui/icon-button";
import { useT } from "@/i18n";

export function DetailActions({
  busy,
  onUndeploy,
  onClone,
  onDelete,
}: {
  busy?: boolean;
  /** 없으면 배포 해제 버튼을 그리지 않는다. */
  onUndeploy?: () => void;
  /** 없으면 복제 버튼을 그리지 않는다. */
  onClone?: () => void;
  /** 없으면 삭제 버튼을 그리지 않는다. */
  onDelete?: () => void;
}) {
  const t = useT();
  const text = t.shared.detailActions;
  return (
    <>
      {onUndeploy ? (
        <Button variant="danger" data-testid="undeploy-button" disabled={busy} onClick={onUndeploy}>
          <PowerOff size={14} />
          {text.undeploy}
        </Button>
      ) : null}
      {onClone ? (
        <IconButton label={text.clone} testId="clone-button" bordered disabled={busy} onClick={onClone}>
          <CopyPlus size={16} />
        </IconButton>
      ) : null}
      {onDelete ? (
        <IconButton label={t.common.delete} testId="delete-button" danger bordered disabled={busy} onClick={onDelete}>
          <Trash2 size={16} />
        </IconButton>
      ) : null}
    </>
  );
}
