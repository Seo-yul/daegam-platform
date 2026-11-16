#!/usr/bin/env bash
# e2e-gateway STEP 9i 의 허브 레지스트리 (P55 F8). 릴리스 후보의 k3d 잡(core 조각)이 클러스터 앞뒤로 부른다.
#
# 플랫폼의 검증기는 https 로만 레지스트리를 부르고 referrers API 로 서명을 읽는다(P55 6절). 그래서 허브 레지스트리는 TLS 를
# 켠 zot 이고, 일회용 CA 가 그 인증서를 낸다. 노드의 containerd 는 k3d 의 registries.yaml 로, 플랫폼은 Platform Settings 의
# network.ca_bundle 로(STEP 9i 가 넣는다), 러너의 미러는 DAEGAM_CA_FILE·SSL_CERT_FILE 로 그 CA 를 믿는다. 노드, 플랫폼 파드,
# 러너가 모두 같은 이름과 포트 k3d-hub:<E2E_HUB_PORT> 로 부른다. upstream 은 러너의 평문 registry 하나다. 미러가 이 컴퓨터의
# 시험용 레지스트리만 평문 http 로 읽기 때문이다. 포트는 lib/e2e_ports.sh 의 것이다.
#
#   certs DIR               CA, 서버 인증서, zot 설정, k3d 의 registries.yaml 을 DIR 에 만든다. 클러스터를 만들기 전에 부르고,
#                           k3d cluster create 에 --registry-config DIR/registries.yaml 과
#                           --volume DIR/ca.crt:/etc/daegam-hub/ca.crt@server:* 를 준다
#   up DIR NETWORK CONTEXT  zot 을 docker 네트워크 NETWORK 에 k3d-hub 로 띄워 러너의 127.0.0.1 에 같은 포트로 열고, upstream
#                           registry 를 127.0.0.1 에 띄운다. 러너의 /etc/hosts(sudo)와 클러스터 CONTEXT 의 CoreDNS NodeHosts 에
#                           k3d-hub 를 적고, 미러가 읽는 ~/.docker/config.json 에 k3d-hub 의 자리 자격 증명을 둔다(zot 은 인증이
#                           없다)
#   down                    두 컨테이너를 지우고 /etc/hosts 의 k3d-hub 줄과 ~/.docker/config.json 의 자리 자격 증명을 뺀다.
#                           CoreDNS 의 줄은 클러스터와 함께 사라진다
#
# STEP 9i 는 E2E_HUB_DIR(=DIR)가 있어야 돈다. zot 이미지는 이 컴퓨터의 아키텍처의 것이고(ZOT_IMAGE 로 바꾼다), upstream 은
# registry 의 판을 고정한다(UPSTREAM_IMAGE).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib/die.sh"
. "$HERE/lib/e2e_ports.sh"

HUB_NAME=k3d-hub
HUB_PORT="${E2E_HUB_PORT:-$PORT_HUB_REGISTRY}"
UPSTREAM_NAME=daegam-e2e-upstream
UPSTREAM_PORT="${E2E_HUB_UPSTREAM_PORT:-$PORT_HUB_UPSTREAM}"
case "$(uname -m)" in
  arm64 | aarch64) ZOT_ARCH=arm64 ;;
  *) ZOT_ARCH=amd64 ;;
esac
ZOT_IMAGE="${ZOT_IMAGE:-ghcr.io/project-zot/zot-linux-$ZOT_ARCH:v2.1.21}"
UPSTREAM_IMAGE="${UPSTREAM_IMAGE:-registry:2.8.3}"

certs() {
  local dir="$1"
  mkdir -p "$dir"
  # RSA 다. LibreSSL 의 EC 키는 곡선을 이름 대신 매개변수로 적어 zot(Go)이 읽지 못한다
  openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=daegam-e2e-hub-ca" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign" \
    -keyout "$dir/ca.key" -out "$dir/ca.crt" 2>/dev/null || die "CA failed"
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$HUB_NAME" \
    -keyout "$dir/server.key" -out "$dir/server.csr" 2>/dev/null || die "server key failed"
  printf 'subjectAltName=DNS:%s,DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n' "$HUB_NAME" >"$dir/server.ext"
  openssl x509 -req -in "$dir/server.csr" -CA "$dir/ca.crt" -CAkey "$dir/ca.key" -CAcreateserial -days 2 \
    -extfile "$dir/server.ext" -out "$dir/server.crt" 2>/dev/null || die "server certificate failed"
  # 일회용 시험 키다. zot 이 어떤 사용자로 돌든 읽게 연다
  chmod 644 "$dir/server.key"
  # Docker schema 2 이미지(docker push 의 기본 꼴)를 받도록 docker2s2 를 켠다 (P55 6절 3단계)
  cat >"$dir/zot.json" <<JSON
{
  "distSpecVersion": "1.1.1",
  "storage": {"rootDirectory": "/var/lib/registry", "gc": false},
  "http": {
    "address": "0.0.0.0",
    "port": "$HUB_PORT",
    "tls": {"cert": "/etc/zot/server.crt", "key": "/etc/zot/server.key"},
    "compat": ["docker2s2"]
  },
  "log": {"level": "warn"}
}
JSON
  cat >"$dir/registries.yaml" <<YAML
mirrors:
  "$HUB_NAME:$HUB_PORT":
    endpoint: ["https://$HUB_NAME:$HUB_PORT"]
configs:
  "$HUB_NAME:$HUB_PORT":
    tls:
      ca_file: /etc/daegam-hub/ca.crt
YAML
  echo "e2e-hub-registry: certs in $dir"
}

# 러너가 k3d-hub 를 127.0.0.1 로 찾게 한다. 이미 있으면 그대로 둔다
hosts_entry() {
  grep -qE "[[:space:]]$HUB_NAME([[:space:]]|\$)" /etc/hosts || echo "127.0.0.1 $HUB_NAME" | sudo tee -a /etc/hosts >/dev/null
}

# 플랫폼 파드가 k3d-hub 를 찾게 CoreDNS 의 NodeHosts 에 zot 의 네트워크 주소를 적는다. CoreDNS 가 그 파일을 다시 읽는다
coredns_entry() {
  local dir="$1" network="$2" context="$3" ip
  ip="$(docker inspect -f "{{(index .NetworkSettings.Networks \"$network\").IPAddress}}" "$HUB_NAME")" || die "no address for $HUB_NAME on $network"
  [ -n "$ip" ] || die "no address for $HUB_NAME on $network"
  kubectl --context "$context" -n kube-system get configmap coredns -o json >"$dir/coredns.json" || die "coredns ConfigMap not readable"
  python3 - "$dir/coredns.json" "$ip" "$HUB_NAME" <<'PY' || die "coredns NodeHosts not updated"
import json, sys
path, ip, name = sys.argv[1:4]
cm = json.load(open(path))
lines = [l for l in cm["data"].get("NodeHosts", "").splitlines() if l.split()[1:] != [name]]
cm["data"]["NodeHosts"] = "\n".join(lines + [f"{ip} {name}"]) + "\n"
for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"):
    cm["metadata"].pop(k, None)
json.dump(cm, open(path, "w"))
PY
  kubectl --context "$context" apply -f "$dir/coredns.json" >/dev/null || die "coredns ConfigMap not applied"
}

# 미러는 허브 레지스트리에 docker login 의 자격 증명으로 올린다(scripts/hub_mirror.py registry_client). zot 은 인증이 없으므로
# 자리 값을 둔다(add) 또는 뺀다(remove). 자격 증명 도우미를 쓰는 docker 설정에는 손대지 않고 멈춘다
docker_auth() {
  local action="$1" config="$HOME/.docker/config.json"
  [ "$action" = add ] || [ -f "$config" ] || return 0
  mkdir -p "$HOME/.docker"
  [ -f "$config" ] || echo '{}' >"$config"
  python3 - "$config" "$HUB_NAME:$HUB_PORT" "$action" <<'PY' || die "$config not updated"
import base64, json, sys
path, host, action = sys.argv[1:4]
cfg = json.load(open(path))
if action == "remove":
    (cfg.get("auths") or {}).pop(host, None)
elif cfg.get("credsStore") or host in (cfg.get("credHelpers") or {}):
    sys.exit(f"{path} uses a credential helper; run: docker login {host}")
else:
    cfg.setdefault("auths", {})[host] = {"auth": base64.b64encode(b"e2e:e2e").decode()}
json.dump(cfg, open(path, "w"), indent=2)
PY
}

wait_hub() {
  local dir="$1" i
  for ((i = 0; i < 30; i++)); do
    curl -fsS --cacert "$dir/ca.crt" "https://$HUB_NAME:$HUB_PORT/v2/" >/dev/null 2>&1 && return 0
    sleep 1
  done
  die "$HUB_NAME did not answer on https://$HUB_NAME:$HUB_PORT/v2/"
}

up() {
  local dir="$1" network="$2" context="$3"
  [ -f "$dir/zot.json" ] || die "run certs $dir first"
  docker rm -f "$HUB_NAME" "$UPSTREAM_NAME" >/dev/null 2>&1 || true
  docker run -d --name "$HUB_NAME" --network "$network" -p "127.0.0.1:$HUB_PORT:$HUB_PORT" \
    -v "$dir/zot.json:/etc/zot/config.json:ro" -v "$dir/server.crt:/etc/zot/server.crt:ro" \
    -v "$dir/server.key:/etc/zot/server.key:ro" "$ZOT_IMAGE" >/dev/null || die "zot did not start ($ZOT_IMAGE)"
  docker run -d --name "$UPSTREAM_NAME" -p "127.0.0.1:$UPSTREAM_PORT:5000" "$UPSTREAM_IMAGE" >/dev/null ||
    die "the upstream registry did not start ($UPSTREAM_IMAGE)"
  hosts_entry
  wait_hub "$dir"
  coredns_entry "$dir" "$network" "$context"
  docker_auth add
  echo "e2e-hub-registry: $HUB_NAME:$HUB_PORT (TLS, CA $dir/ca.crt) and 127.0.0.1:$UPSTREAM_PORT are up"
}

down() {
  docker rm -f "$HUB_NAME" "$UPSTREAM_NAME" >/dev/null 2>&1 || true
  docker_auth remove
  if grep -qE "[[:space:]]$HUB_NAME([[:space:]]|\$)" /etc/hosts; then
    grep -vE "[[:space:]]$HUB_NAME([[:space:]]|\$)" /etc/hosts >"${TMPDIR:-/tmp}/daegam-e2e-hosts.$$"
    sudo cp "${TMPDIR:-/tmp}/daegam-e2e-hosts.$$" /etc/hosts
    rm -f "${TMPDIR:-/tmp}/daegam-e2e-hosts.$$"
  fi
  echo "e2e-hub-registry: down"
}

case "${1:-}" in
  certs) [ $# -eq 2 ] || die "usage: $0 certs DIR"; certs "$2" ;;
  up) [ $# -eq 4 ] || die "usage: $0 up DIR NETWORK CONTEXT"; up "$2" "$3" "$4" ;;
  down) down ;;
  *) die "usage: $0 certs DIR | up DIR NETWORK CONTEXT | down" ;;
esac
