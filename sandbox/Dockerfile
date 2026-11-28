# Daegam sandbox: the per-session code execution workspace (P40 sections 2, 3 and 10, D95). The platform creates one
# bare pod per session from this image; its server (daegam_sandbox) runs as PID 1 and the Agent's commands run under
# it. The build stage follows platform/Dockerfile so the image carries the same CPython; the last stage is Debian slim
# because commands need /bin/sh, coreutils and GNU grep (deepagents' BaseSandbox calls them).
# 빌드 단계는 다른 CPython 이미지 넷과 같다(platform/tests/test_python_image_copy.py 가 맞댄다)
FROM python:3.12-slim-trixie@sha256:5b5fe805d367f8f5a359ded6ab2652b7f9a206453ee6ee789c0045ba6ec15016 AS build
# 사내 방화벽 CA (선택). make 의 DAEGAM_CA_FILE 이 빌드 비밀 daegam_ca 로 오면, 받기 단계만 시스템 CA 묶음에 그 CA 를
# 더한 묶음을 tmpfs 에 만들어 믿는다. 이미지 층에는 CA 도 묶음도 남지 않는다. 비밀이 없으면 받기는 그대로다
# (docs/guide/images.md 3절)
RUN printf '%s\n' '#!/bin/sh' \
      'if [ -s /run/secrets/daegam_ca ]; then' \
      '  cat /etc/ssl/certs/ca-certificates.crt /run/secrets/daegam_ca > /run/daegam-ca/bundle.pem || exit 1' \
      '  export SSL_CERT_FILE=/run/daegam-ca/bundle.pem PIP_CERT=/run/daegam-ca/bundle.pem' \
      '  export REQUESTS_CA_BUNDLE=/run/daegam-ca/bundle.pem NODE_EXTRA_CA_CERTS=/run/secrets/daegam_ca' \
      'fi' \
      'exec "$@"' > /usr/local/bin/with-daegam-ca \
    && chmod 0755 /usr/local/bin/with-daegam-ca
# ghcr 접근 없이 PyPI 로 uv 를 받는다 (레지스트리 순단 대응, 버전 핀 유지). 받은 wheel 은 PyPI 가 낸 sha256 으로
# 확인한다. uv 가 CPython 의 sha256 과 uv.lock 의 해시를 확인하는 자리라, 사내 CA 를 믿는 굽기에서도 uv 자체는
# 바뀌지 않아야 한다. 해시는 빌드 이미지의 pip 가 고를 수 있는 wheel 셋이다(x86_64 manylinux_2_17, aarch64
# manylinux_2_28·manylinux_2_17). uv 판을 올리면 https://pypi.org/pypi/uv/<판>/json 에서 해시를 다시 적는다
RUN --mount=type=secret,id=daegam_ca,required=false --mount=type=tmpfs,target=/run/daegam-ca \
    printf '%s\n' 'uv==0.13.6 --hash=sha256:22759b8895e1103578de6fb4cd27e94b4399d021023378d29f01333717751fba --hash=sha256:2e9a99100abe4f99605366974c4f9fe27f236615e05de8e78c0f357e38f09f97 --hash=sha256:7ec5486476d67a4214c460b5492393ee2300d84e1de17b293100a019b413568b' > /run/daegam-ca/uv.txt \
    && with-daegam-ca pip install --no-cache-dir --require-hashes --no-deps -r /run/daegam-ca/uv.txt
# 런타임 베이스에는 파이썬이 없다. uv 가 받는 독립 실행형 CPython(python-build-standalone)을 /opt/python 에 두고 그것으로
# venv 를 만든다. 런타임 단계가 같은 자리로 복사하므로 venv 의 python 링크가 그대로 맞는다. CPython 이 안에 묶어 온
# OpenSSL·SQLite 같은 라이브러리도 이 판을 따른다. uv 는 자기 판이 아는 빌드만 받으므로 CPython 판을 올릴 때는 uv 판도
# 함께 올린다(D55)
ARG CPYTHON_VERSION=3.12.16
# 런타임은 venv 로 돌고 pip 를 부르지 않는다. CPython 에 딸려 오는 pip 는 스캔에 고칠 수 있는 취약점으로 잡힐 수 있어
# 뺀다(D55). 표준 라이브러리는 .pyc 없이 온다. 런타임은 PYTHONDONTWRITEBYTECODE 라 미리 컴파일해 두지 않으면 프로세스가
# 뜰 때마다 다시 컴파일한다
RUN --mount=type=secret,id=daegam_ca,required=false --mount=type=tmpfs,target=/run/daegam-ca \
    with-daegam-ca uv python install "$CPYTHON_VERSION" --install-dir /opt/uv-python --no-bin \
    && mv /opt/uv-python/cpython-"$CPYTHON_VERSION"-linux-*-gnu /opt/python \
    && rm -r /opt/python/bin/pip* /opt/python/lib/python3.*/site-packages/pip* \
    && /opt/python/bin/python3 -m compileall -q -j0 /opt/python/lib
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PYTHON=/opt/python/bin/python3
WORKDIR /app
COPY pyproject.toml uv.lock ./
# 데이터 작업 묶음만 깐다. 서버는 표준 라이브러리만 쓰고 이 venv 를 쓰지 않는다
RUN --mount=type=secret,id=daegam_ca,required=false --mount=type=tmpfs,target=/run/daegam-ca \
    with-daegam-ca uv sync --frozen --no-dev --no-install-project
# 묶음이 모두 import 되는지 보고 matplotlib 글꼴 캐시를 미리 만든다. 캐시의 글꼴 경로는 venv 안이고 런타임 단계가
# venv 를 같은 자리로 복사하므로 그대로 맞는다. 서버가 명령의 MPLCONFIGDIR(/tmp/home/.config/matplotlib)로 복사한다
RUN mkdir -p /opt/sandbox/mplconfig \
    && MPLCONFIGDIR=/opt/sandbox/mplconfig /app/.venv/bin/python -c "import numpy, pandas, openpyxl, docx, pypdf, PIL, lxml, matplotlib; matplotlib.use('Agg'); from matplotlib import font_manager, pyplot; font_manager.fontManager" \
    && ls /opt/sandbox/mplconfig/fontlist-*.json

# 명령에 셸·coreutils·GNU grep 이 있어야 해서 distroless 가 아니라 Debian slim 이다. 바탕은 digest 로 고정한다(D55)
FROM debian:trixie-slim@sha256:8aa5a7bf5a1955bddbe7d74fcdd0b91ab30e74362a8b73af672b0481b35b44a0
# 바탕 태그가 다시 구워지기 전에 Debian 이 낸 보안 판을 받는다. push-images 가 OS_PATCH_DATE 에 그날 날짜를 넘겨 아래
# 층의 빌드 캐시를 하루에 한 번 버린다(D55, deploy/dev/mock-oidc/Dockerfile 과 같다). Debian slim 의 apt 원천은
# http 이고 서명으로 확인하므로 사내 CA 는 https 원천을 쓸 때만 쓰인다. 바탕에 시스템 CA 묶음이 없으면 그 CA 만 믿는다.
# 이 단계는 이미지에 남으므로 받기 도우미 대신 그 자리에서 tmpfs 에 묶음을 만든다
ARG OS_PATCH_DATE
RUN --mount=type=secret,id=daegam_ca,required=false --mount=type=tmpfs,target=/run/daegam-ca \
    apt_ca=""; \
    if [ -s /run/secrets/daegam_ca ]; then \
      { cat /etc/ssl/certs/ca-certificates.crt 2>/dev/null; cat /run/secrets/daegam_ca; } > /run/daegam-ca/bundle.pem \
      && apt_ca="-o Acquire::https::CAInfo=/run/daegam-ca/bundle.pem"; \
    fi \
    && apt-get $apt_ca update \
    && DEBIAN_FRONTEND=noninteractive apt-get $apt_ca upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
# 루트 파일시스템은 읽기 전용이고 쓰는 자리는 파드의 메모리 emptyDir 둘(/workspace 1 GiB, /tmp 128 MiB)뿐이다.
# /uploads 와 /skills 는 작업 공간 안을 가리킨다. 파일 Tool 은 /uploads 의 추출한 글을, 코드는 원본을 읽는다(P40 1절).
# setuid·setgid 비트(su, mount, passwd 들)는 모두 뗀다. 파드는 allowPrivilegeEscalation: false 라 그 비트가 권한을
# 올리지 못하지만, 명령이 돌릴 수 있는 권한 상승 길을 이미지에서부터 없앤다
RUN mkdir -p /workspace /opt/sandbox \
    && ln -s /workspace/uploads /uploads \
    && ln -s /workspace/skills /skills \
    && find / -xdev -type f -perm /6000 -exec chmod ug-s {} + \
    && test -z "$(find / -xdev -type f -perm /6000)"
COPY --from=build /opt/python /opt/python
COPY --from=build /app/.venv /app/.venv
COPY --from=build /opt/sandbox/mplconfig /opt/sandbox/mplconfig
COPY daegam_sandbox /opt/sandbox/daegam_sandbox
# 서버는 venv 가 아니라 바탕 인터프리터를 -I 로 돌린다. -I 는 작업 디렉터리·PYTHONPATH·사용자 site 를 sys.path 에 넣지
# 않으므로 서버 꾸러미는 이 .pth 가 가리키는 읽기 전용 /opt/sandbox 에서만 온다. 바이트코드도 미리 만든다
RUN for d in /opt/python/lib/python3.*/site-packages; do echo /opt/sandbox > "$d/daegam-sandbox.pth"; done \
    && /opt/python/bin/python3 -m compileall -q /opt/sandbox/daegam_sandbox \
    && /opt/python/bin/python3 -I -c "import daegam_sandbox, sys; assert daegam_sandbox.__file__.startswith('/opt/sandbox/'), sys.path"
# 명령은 서버가 주는 고정 env 만 받는다(daegam_sandbox/config.py command_env). 아래는 이미지 기본값이다
ENV PATH=/app/.venv/bin:$PATH \
    LANG=C.UTF-8 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1
# 플랫폼이 렌더하는 파드의 runAsUser·runAsGroup 과 같은 값이다
USER 1000:1000
WORKDIR /opt/sandbox
EXPOSE 8080
# 셸 감싸기 없이 서버가 PID 1 이다. SIGTERM 이면 바로 끝나고, PID 1 이 끝나면 컨테이너의 모든 프로세스가 끝난다
ENTRYPOINT ["/opt/python/bin/python3", "-I", "-m", "daegam_sandbox"]
