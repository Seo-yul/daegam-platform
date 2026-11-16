#!/bin/sh
# daegam-ui 의 preStop (D52). 파드가 내려갈 때 Traefik 에서 먼저 빠진 뒤 멈추게 한다.
#   daegam-ui-drain <표시 전 초> <표시 뒤 초>
# 앞의 초만큼 그대로 낸 뒤 내려가는 중 표시를 두고 뒤의 초만큼 더 낸다. 표시가 있으면 준비 확인(nginx.conf 의 8081
# /ready)이 503 이고, 다시 뜬 컨테이너의 기동(start.sh)이 표시를 지운다. 세 파일이 같은 경로를 쓰는지는 이미지 빌드의
# 셀프 테스트가 본다. 몇 초씩 기다릴지는 Traefik 에 달려 있어 매니페스트가 정한다
set -eu
sleep "$1"
: > /tmp/daegam/draining
sleep "$2"
