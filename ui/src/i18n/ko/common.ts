import { particle } from "@/lib/particle";

/** `{label}을/를`. 복사 단추의 세 문장이 같이 쓴다. */
const object = (label: string): string => `${label}${particle(label, "을/를")}`;

/** 여러 화면이 같이 쓰는 말. F1·F2 만 더한다. */
export const common = {
  confirm: "확인",
  cancel: "취소",
  processing: "처리 중…",
  loading: "불러오는 중입니다…",
  networkError: "서버에 연결하지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
  /** 이름을 입력해야 지워지는 확인 대화상자. */
  nameConfirm: { label: "이름 확인", hint: "삭제하려면 이름을 입력해 주세요." },
  /** `···` 메뉴. */
  menu: { label: "동작", more: "더보기" },
  /** 복사 단추. label 은 문장 가운데 들어가는 명사다(주소, 토큰). */
  copy: {
    defaultLabel: "주소",
    action: (label: string): string => `${label} 복사`,
    copied: (label: string): string => `${object(label)} 복사했습니다`,
    failed: (label: string): string => `${object(label)} 복사하지 못했습니다`,
    title: "복사",
    copiedTitle: "복사됨",
    failedTitle: (label: string): string => `복사하지 못했습니다. ${object(label)} 직접 선택해 복사해 주세요.`,
  },
  /** 삭제 단추와 그 진행 중 문구. */
  delete: "삭제",
  deleting: "삭제 중…",
  /** 시간의 단위. 흐른 시간(formatDuration)은 단위를 둘까지, 얼마 전인지(formatAgo)는 하나만 적는다. */
  time: {
    underMinute: "1분 미만",
    seconds: (seconds: number): string => `${seconds}초`,
    minutesSeconds: (minutes: number, seconds: number): string => `${minutes}분 ${seconds}초`,
    ms: (value: string): string => `${value} ms`,
    minutes: (minutes: number): string => `${minutes}분`,
    hours: (hours: number): string => `${hours}시간`,
    hoursMinutes: (hours: number, minutes: number): string => `${hours}시간 ${minutes}분`,
    days: (days: number): string => `${days}일`,
    daysHours: (days: number, hours: number): string => `${days}일 ${hours}시간`,
    justNow: "방금",
    minutesAgo: (minutes: number): string => `${minutes}분 전`,
    hoursAgo: (hours: number): string => `${hours}시간 전`,
    daysAgo: (days: number): string => `${days}일 전`,
  },
};
