# Daegam mcp-runtime: fixed image. The spec (/config/openapi.yaml) and profile
# (/config/profile.json) are injected by an initContainer (P1 D안).
# 빌드 단계는 런타임 베이스와 같은 Debian 13 이다. 런타임 단계가 이 단계의 glibc 로캘 데이터를 가져다 쓴다
FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
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
# 의존만 깐다. 소스는 런타임 단계가 따로 복사하고 PYTHONPATH 로 읽는다. 소스만 바뀐 커밋은 venv 층을 그대로 두고
# 소스 층만 새로 만들어, 레지스트리에 올릴 것이 작아진다
RUN --mount=type=secret,id=daegam_ca,required=false --mount=type=tmpfs,target=/run/daegam-ca \
    with-daegam-ca uv sync --frozen --no-dev --no-install-project

# 셸도 패키지 관리자도 없는 베이스다. 바탕은 digest 로 고정하고 OS 보안 판은 make base-pins 가 고정을 옮길 때 따라온다(D55)
FROM gcr.io/distroless/cc-debian13:latest@sha256:809d04afa1be033c441f76bb0051878c342fb76d0ea236bc6dc94b655fa0fbd6
WORKDIR /app
# 베이스에는 C.UTF-8 로캘 데이터가 없다. LANG 이 없는 로캘을 가리키면 setlocale(LC_ALL, "") 이 실패하므로 빌드 단계의
# libc-bin 에서 가져온다
COPY --from=build /usr/lib/locale/C.utf8 /usr/lib/locale/C.utf8
COPY --from=build /opt/python /opt/python
COPY --from=build /app/.venv /app/.venv
COPY runtime /app/runtime
# 시스템 CA 묶음은 베이스가 SSL_CERT_FILE 로 가리킨다
ENV PATH=/app/.venv/bin:$PATH \
    LANG=C.UTF-8 \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    RUNTIME_SPEC_PATH=/config/openapi.yaml \
    RUNTIME_PROFILE_PATH=/config/profile.json
# 매니페스트의 runAsUser·runAsGroup 과 같은 값이다. 베이스의 /etc/passwd 에는 이 uid 가 없어 getpass.getuser() 처럼
# 이름을 찾는 호출은 실패한다
USER 1000:1000
EXPOSE 8000
ENTRYPOINT ["python", "-m", "runtime.main"]
