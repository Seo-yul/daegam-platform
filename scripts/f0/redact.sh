#!/usr/bin/env bash
# 표준 입력에서 자격 증명처럼 보이는 값을 지워 내보낸다. lib.sh 의 f0_redact 와 p54-f0.yml 의 diagnostics 단계가 함께
# 쓴다. 이 폴더(scripts/f0)와 함께 F0 측정 동안만 둔다.
#
# 지우는 것: Bearer·Basic 뒤의 값, JWT, Authorization 값(따옴표 안의 값과 따옴표 없는 scheme·값), token=·password=·
# client_secret= 쌍(access_token= 처럼 끝이 같은 이름 포함), JSON 의 ...token·password·client_secret 값.
# 구분자는 # 이다. 정규식에 / 와 | 가 들어 있어서다. BSD 와 GNU sed 가 같이 읽는 ERE 만 쓴다
exec sed -E \
  -e 's#([Bb]earer|[Bb]asic)[[:space:]]+[A-Za-z0-9._~+/=-]+#\1 <redacted>#g' \
  -e 's#eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*#<jwt>#g' \
  -e 's#("?[Aa]uthorization"?[[:space:]]*[:=][[:space:]]*)"[^"]*"#\1"<redacted>"#g' \
  -e 's#([Aa]uthorization[[:space:]]*[:=][[:space:]]*)[^"[:space:],}]+([[:space:]]+[^"[:space:],}]+)?#\1<redacted>#g' \
  -e 's#(([Tt]oken|[Pp]assword|[Cc]lient_secret)=)[^&[:space:]",;}]+#\1<redacted>#g' \
  -e 's#("[A-Za-z_]*([Tt]oken|[Pp]assword|[Cc]lient_secret)"[[:space:]]*:[[:space:]]*)"[^"]*"#\1"<redacted>"#g'
