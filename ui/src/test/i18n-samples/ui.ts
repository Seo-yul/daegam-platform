import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "ui.cardGrid.edited": [["3 minutes ago"]],
  "ui.cardGrid.menu": [["orders-api"]],
  "ui.cardGrid.menuTrigger": [["orders-api"]],
  "ui.files.tooLarge": [["report.pdf", "11.0 MB", "LIMIT"]],
  "ui.deployment.summary.count": [[12], [1]],
  "ui.deployment.summary.failed": [[2]],
  "ui.deployment.summary.progressing": [[3]],
  "ui.deployment.summary.mixed": [[4, 5]],
  "ui.names.nameTooLong": [[40]],
  "ui.names.displayNameTooLong": [[90]],
  "ui.pointer.schemaRefs": [[3], [1]],
  "ui.providers.routing.only": [["moonshotai → deepinfra"]],
  "ui.references.viaHubItem": [["REVISION"]],
  "ui.references.pinnedCurrent": [[3]],
  "ui.references.pinnedDeployed": [[2]],
};
