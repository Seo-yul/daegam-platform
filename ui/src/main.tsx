import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import "./index.css";
import { reloadOnceOnPreloadError } from "./lib/preloadReload";

// 배포 뒤 옛 탭이 지워진 지연 청크를 부르면 한 번 새로 불러 새 index.html 을 받는다 (D52)
reloadOnceOnPreloadError(window);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
