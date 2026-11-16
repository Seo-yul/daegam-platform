// /api/docs 의 Swagger UI 를 띄운다. 플랫폼 CSP(script-src 'self')가 인라인 스크립트를 막으므로
// 초기화는 같은 origin 의 이 파일이다. OpenAPI 문서 주소는 HTML 의 data-openapi-url 에서 읽고, 나머지
// 설정은 FastAPI 기본 문서 화면(swagger_ui_default_parameters)과 같다.
(function () {
  "use strict";
  var root = document.getElementById("swagger-ui");
  window.ui = SwaggerUIBundle({
    url: root.dataset.openapiUrl,
    domNode: root,
    layout: "BaseLayout",
    deepLinking: true,
    showExtensions: true,
    showCommonExtensions: true,
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
  });
})();
