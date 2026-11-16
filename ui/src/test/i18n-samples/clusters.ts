import type { SampleFile } from "./types";

export const samples: SampleFile["samples"] = {
  "clusters.form.loaded": [["prod.kubeconfig"]],
  "clusters.list.noMatch": [["prod"]],
  "clusters.detail.networkPolicy.enforced": [["calico"]],
  "clusters.detail.networkPolicy.presumed": [["k3s"]],
  "clusters.detail.secretRights.bound": [[2], [1]],
  "clusters.detail.secretRights.unbound": [[1, 3], [2, 2]],
  "clusters.detail.shippedRedisCertNote": [["2026-11-03 09:00"]],
  "clusters.detail.missingCount": [[3], [1]],
  "clusters.detail.rbac.deniedOnly": [[2, 40]],
  "clusters.detail.envManaged": [["ingress.domain, ingress.tls"]],
  "clusters.ingress.annotationLine": [["no-colon"]],
  "clusters.sandbox.passed": [["2026-11-09 17:00"]],
  "clusters.sandbox.couldNotRun": [["pull"]],
  "clusters.sandbox.capacity": [[3], [1]],
  "clusters.sandbox.assumedLimits": [[2], [1]],
  "clusters.sandbox.accepted": [["root", "2026-11-09 17:00"]],
};
