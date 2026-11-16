# daegam-ui (D52). 컨텍스트는 ui/ 다:
#   docker build -f ui/Dockerfile --build-arg GIT_SHA=$(scripts/build-stamp.sh ui) ui
# UI 정적 빌드를 nginx 가 낸다. 같은 호스트의 /api/·/.well-known/·/healthz 는 Ingress 가 플랫폼으로 보낸다.

# --- 1. vite 빌드 -> /ui/dist ----------------------------------------------------
# 산출물은 아키텍처와 무관한 정적 파일이라 빌드하는 기계의 아키텍처로 돈다. amd64 이미지를 arm64 에서 구울 때
# vite 가 QEMU 아래서 돌지 않는다
FROM --platform=$BUILDPLATFORM node:24-alpine@sha256:a34ab6d7f6f4813f4d0c5469d74685604b962fbdbf5793a7bc7b4998ecb867c8 AS build
ARG GIT_SHA=""
# 릴리스 태그(push-images RELEASE=...). vite.config.ts 가 build.json 의 release 와 화면 배지에 적는다
ARG RELEASE=""
WORKDIR /ui
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
COPY package.json package-lock.json ./
RUN --mount=type=secret,id=daegam_ca,required=false --mount=type=tmpfs,target=/run/daegam-ca \
    with-daegam-ca npm ci --no-audit --no-fund
COPY . ./
# vite.config.ts 가 git 으로 build.json 의 commit 을 적는다. 컨텍스트에 .git 이 없으므로 GIT_SHA 빌드 인자(구성요소
# 도장)를 답하는 작은 git 심이 대신한다(비면 commit 없이 ui_build 만 남는다)
RUN printf '#!/bin/sh\necho "%s"\n' "$GIT_SHA" > /usr/local/bin/git && chmod +x /usr/local/bin/git \
    && npm run build

# --- 2. nginx (uid 101, STOPSIGNAL SIGQUIT 는 베이스 이미지 것을 그대로 쓴다) -------------
FROM nginxinc/nginx-unprivileged:1.30.6-alpine3.24@sha256:f5116ed4606f3eb015dd4a8e28cb9b351476dc0ad4bc6e9f93b19cec130cdf03
# 베이스 태그가 다시 구워지기 전에 알파인이 낸 보안 판을 받는다(D55). 베이스의 사용자(101)로 돌아간다. nginx 와
# nginx-module-* 는 베이스가 nginx.org 에서 받은 것이라 이 올리기가 닿지 않는다. 그 고친 판은 베이스 태그를 올려야 온다
USER root
# push-images 가 OS_PATCH_DATE 에 그날 날짜를 넘겨 아래 층의 빌드 캐시를 하루에 한 번 버린다. 바탕 digest 는
# 고정이라(make base-pins 가 옮긴다) 캐시된 층이 옛 패키지를 그대로 실어 배포판의 보안 판이 따라오지 않기 때문이다(D55)
ARG OS_PATCH_DATE
# 이 단계는 이미지에 남으므로 받기 도우미 대신 그 자리에서 tmpfs 에 묶음을 만든다. tmpfs 라 층에 남지 않는다
RUN --mount=type=secret,id=daegam_ca,required=false --mount=type=tmpfs,target=/run/daegam-ca \
    if [ -s /run/secrets/daegam_ca ]; then \
      cat /etc/ssl/certs/ca-certificates.crt /run/secrets/daegam_ca > /run/daegam-ca/bundle.pem \
      && export SSL_CERT_FILE=/run/daegam-ca/bundle.pem; \
    fi \
    && apk upgrade --no-cache
USER 101
COPY docker/nginx.conf /etc/nginx/nginx.conf
COPY docker/security.conf /etc/nginx/daegam/security.conf
COPY --chmod=0755 docker/daegam-csp.sh /usr/local/bin/daegam-csp
COPY --chmod=0755 docker/start.sh /usr/local/bin/daegam-ui-start
COPY --chmod=0755 docker/drain.sh /usr/local/bin/daegam-ui-drain
COPY --chmod=0755 docker/selftest.sh /usr/local/bin/daegam-ui-selftest
COPY --from=build /ui/dist /usr/share/nginx/html
# 이미지의 셸로 CSP 대표 사례를 돌리고 설정 문법을 본다. 하나라도 실패하면 이미지가 만들어지지 않는다. 만든 파일은
# 지운다(파드에서는 /tmp 가 emptyDir 이다). 셀프 테스트는 nginx 를 8080·8081 로 띄우므로 제 네트워크 네임스페이스에서
# 돈다. 여러 아키텍처를 함께 구우면 두 셀프 테스트가 빌더의 네트워크를 함께 써 포트가 부딪친다
RUN --network=none /usr/local/bin/daegam-ui-selftest \
    && /usr/local/bin/daegam-ui-start --check \
    && rm -rf /tmp/daegam /tmp/nginx
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/daegam-ui-start"]

LABEL org.opencontainers.image.title="daegam-ui"
LABEL org.opencontainers.image.licenses="LicenseRef-PolyForm-Strict-1.0.0"
