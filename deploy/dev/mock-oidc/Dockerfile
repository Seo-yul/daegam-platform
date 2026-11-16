# Daegam mock OIDC provider (dev/e2e only). Never deploy to production.
FROM python:3.12-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS build
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
    printf '%s\n' 'uv==0.11.26 --hash=sha256:4dcf4e0b5b5cbdc242dcb002f1f8d99e7cf8c043609869228a9ce15e095c0b18 --hash=sha256:866ae8d28f7381c15de0906a284c1e97916424c635bf40f7960b3fc889cd725e --hash=sha256:1cd9ba4951681ce17f1703106266fcbe27aaa7d37f07d53cce8b5686d68a8755' > /run/daegam-ca/uv.txt \
    && with-daegam-ca pip install --no-cache-dir --require-hashes --no-deps -r /run/daegam-ca/uv.txt
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=secret,id=daegam_ca,required=false --mount=type=tmpfs,target=/run/daegam-ca \
    with-daegam-ca uv sync --frozen --no-dev --no-install-project
# 프로젝트 자체는 깔지 않는다. 깔면 빌드 백엔드(hatchling)를 해시 없이 받는다. 소스는 PYTHONPATH 로 읽는다
COPY mock_oidc ./mock_oidc

FROM python:3.12-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
# 베이스 태그가 다시 구워지기 전에 Debian 이 낸 보안 판을 받는다. 런타임은 uv 가 만든 venv 로 돌고 pip 를 부르지
# 않는다. 베이스 이미지의 pip 는 고칠 수 있는 취약점으로 잡히므로 뺀다(D55)
# push-images 가 OS_PATCH_DATE 에 그날 날짜를 넘겨 아래 층의 빌드 캐시를 하루에 한 번 버린다. 바탕 digest 는
# 고정이라(make base-pins 가 옮긴다) 캐시된 층이 옛 패키지를 그대로 실어 배포판의 보안 판이 따라오지 않기 때문이다(D55)
ARG OS_PATCH_DATE
# 이 단계는 이미지에 남으므로 받기 도우미 대신 그 자리에서 tmpfs 에 묶음을 만든다. apt 는 자기 CA 설정을 읽는다
RUN --mount=type=secret,id=daegam_ca,required=false --mount=type=tmpfs,target=/run/daegam-ca \
    apt_ca=""; \
    if [ -s /run/secrets/daegam_ca ]; then \
      cat /etc/ssl/certs/ca-certificates.crt /run/secrets/daegam_ca > /run/daegam-ca/bundle.pem \
      && apt_ca="-o Acquire::https::CAInfo=/run/daegam-ca/bundle.pem"; \
    fi \
    && apt-get $apt_ca update \
    && DEBIAN_FRONTEND=noninteractive apt-get $apt_ca upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 1000 --no-create-home --shell /usr/sbin/nologin app \
    && python -m pip uninstall --yes --quiet pip
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/mock_oidc /app/mock_oidc
ENV PATH=/app/.venv/bin:$PATH PYTHONPATH=/app PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
USER 1000
EXPOSE 8080
CMD ["uvicorn", "mock_oidc.app:app", "--host", "0.0.0.0", "--port", "8080"]
