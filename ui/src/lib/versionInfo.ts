/**
 * GET /version (API 판과 플랫폼 이미지의 빌드). 사이드바의 빌드 표시(VersionBadge)와 플랫폼 API 문서 팝업(P59)이 같은
 * 응답을 같은 키로 본다.
 */
import { useQuery } from "@tanstack/react-query";

import { getVersion } from "@/api/resources";
import { VERSION_KEY } from "@/lib/queryKeys";

export function useVersionInfo() {
  return useQuery({ queryKey: VERSION_KEY, queryFn: getVersion, staleTime: 60_000 });
}
