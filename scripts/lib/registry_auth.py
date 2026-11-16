"""docker login 이 둔 레지스트리 자격 증명을 읽는다 (D53).

registry-pull-secret.sh·image-report.sh·resolve-images.sh 의 파이썬 조각이
`PYTHONPATH="$HERE/lib" python3 -` 로 읽어 쓴다. 자격 증명 도우미(credHelpers 의 그 호스트
항목, 없으면 credsStore)가 있으면 그것에 묻고, 없으면 ~/.docker/config.json 의 auths 에 적힌
auth 를 푼다. 어느 쪽에도 없으면 트레이스백 대신 docker login 을 하라는 한 줄로 끝낸다.
레지스트리에 GET 하는 조각은 `Registry` 로 요청을 만들고 platform/tests/test_registry_auth.py 가
그 주소를 단위 테스트한다. 맥의 시스템 python3(3.9)에서도 돌아야 하므로 3.10 이후 문법을 쓰지
않는다.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request


def credentials(host: str) -> tuple[str, str]:
    """host 의 (사용자, 비밀번호)."""
    miss = f"no credentials for {host}. run: docker login {host}"
    try:
        with open(os.path.expanduser("~/.docker/config.json")) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        sys.exit(miss)
    store = (cfg.get("credHelpers") or {}).get(host) or cfg.get("credsStore")
    if store:
        helper = [f"docker-credential-{store}", "get"]
        got = subprocess.run(helper, input=host, capture_output=True, text=True)
        if got.returncode != 0:
            sys.exit(f"{miss} (credential store: {store})")
        cred = json.loads(got.stdout)
        return cred["Username"], cred["Secret"]
    auth = ((cfg.get("auths") or {}).get(host) or {}).get("auth")
    if not auth:
        sys.exit(miss)
    user, _, secret = base64.b64decode(auth).decode().partition(":")
    return user, secret


def basic_auth(host: str) -> str:
    """HTTP `Authorization: Basic` 뒤에 붙는 값 (base64 of user:secret)."""
    user, secret = credentials(host)
    return base64.b64encode(f"{user}:{secret}".encode()).decode()


def is_loopback(host: str) -> bool:
    """`이름[:포트]` 가 이 컴퓨터(localhost, 127.0.0.0/8, ::1)를 가리키는지. oci_registry 도 이 규칙으로
    평문 http 를 고른다."""
    name = urllib.parse.urlsplit(f"//{host}").hostname or ""
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


class Registry:
    """`호스트/프로젝트` 레지스트리(`registry.example.com/daegam`)에 docker login 의 자격으로 GET 한다.
    자격 증명은 만들 때 한 번만 읽는다. 도커처럼 이 컴퓨터의 레지스트리만 평문 http 로 부른다.
    스크립트를 로컬의 버리는 registry:2 에 대고 시험할 때가 그 경우다."""

    def __init__(self, registry: str):
        self.host, _, self.project = registry.partition("/")
        self._base = f"{'http' if is_loopback(self.host) else 'https'}://{self.host}"
        self._auth = basic_auth(self.host)

    def request(self, path: str, accept: str | None = None) -> urllib.request.Request:
        """호스트 뒤의 path(`/v2/...`)로 가는 인증된 GET."""
        headers = {"Authorization": f"Basic {self._auth}"}
        if accept:
            headers["Accept"] = accept
        return urllib.request.Request(f"{self._base}{path}", headers=headers)
