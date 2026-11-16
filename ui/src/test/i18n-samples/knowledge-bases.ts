import { createElement } from "react";

import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "knowledgeBases.detail.labels.indexed": [[3, 5]],
  "knowledgeBases.list.empty": [["default"]],
  "knowledgeBases.chunking.sizeHint": [[200, 800]],
  "knowledgeBases.form.unverified": [[createElement("a", null, "orders-db")]],
  "knowledgeBases.form.dimensionsDefault": [[768]],
  "knowledgeBases.detail.deleting": [
    ["10/1/2026, 9:30:00 AM", true],
    ["10/1/2026, 9:30:00 AM", false],
  ],
  "knowledgeBases.detail.tokens": [[512]],
  "knowledgeBases.notice.reindexAll": [[3], [1]],
  "knowledgeBases.notice.uploaded": [["guide.pdf"]],
  "knowledgeBases.notice.added": [["guide.html"]],
  "knowledgeBases.notice.reindexDoc": [["guide.pdf"]],
  "knowledgeBases.documents.limits": [["PDF, DOCX, Markdown", 10, 200]],
  "knowledgeBases.documents.polling": [[5], [1]],
  "knowledgeBases.documents.sizeLimit": [[10]],
  "knowledgeBases.documents.uploading": [["guide.pdf"]],
  "knowledgeBases.deleteDialog.dropTable": [["kb_product_faq"]],
  "knowledgeBases.deleteDialog.tableNotDropped": [["table_not_dropped"]],
};

export const selectors: SampleFile["selectors"] = {
  // 벡터 테이블이 남았는지는 덧붙일 문장을 고르기만 한다
  "knowledgeBases.detail.deleting": [1],
};
