# Hub signature fixtures (P55 F0 item 4)

Real cosign v3.1.3 output (`cosign sign --key ... --signing-config <config without services>`, so no transparency log
and the default sigstore bundle v0.3) on a throwaway zot v2.1.21 that ran on a loopback port and was removed after.
The registry host appears in no file: referrer manifests and bundles carry only digests. The subjects sat under
`hub/registry.example.com/team/server` and `hub/registry.example.com/team/other` with prefix `hub`, so a test that
serves these bytes binds them to `<prefix>/registry.example.com/team/<name>`. The keys are throwaway P-256 keys made
for these files; their private halves were not kept.

| File | What it is |
|---|---|
| `hub.pub` | The hub public key that signed `verified`, `approved`, `no-annotations` and `other-digest` |
| `other.pub` | A second public key, which signed `other-key` (wrong key; with `hub.pub` it is a rotation pair) |
| `server.digest`, `other.digest` | The subject index digests |
| `server-index.json`, `other-index.json` | The subject OCI image indexes (two platforms each), byte for byte |
| `server-referrers.json` | zot's `GET /v2/<repo>/referrers/<server digest>` response: `verified`, `approved`, `no-annotations` and `other-key`, no `Link` |
| `other-referrers.json` | The same for `other.digest`: `other-digest` |
| `verified.referrer.json`, `verified.bundle.json` | Hub key; upstream, `provenance=verified`, catalog, and a job-style run |
| `approved.referrer.json`, `approved.bundle.json` | Hub key; upstream, `provenance=approved`, a make-style run, no catalog annotation |
| `no-annotations.referrer.json`, `no-annotations.bundle.json` | Hub key; no `dev.daegam.hub.*` annotations, as a product image signature |
| `other-key.referrer.json`, `other-key.bundle.json` | `other.pub`'s key; the same annotations as `verified` |
| `other-digest.referrer.json`, `other-digest.bundle.json` | Hub key; signs `other.digest`. Served in `server.digest`'s referrers, it is the "bundle moved to another digest" case |

Each `*.referrer.json` is the referrer manifest exactly as stored: `artifactType`
`application/vnd.dev.sigstore.bundle.v0.3+json`, the empty config, one layer of that media type whose blob is the
matching `*.bundle.json`, and `subject` naming the signed digest. The `subject` is not signed. The signed content is
the DSSE payload in the bundle: an in-toto Statement v1 whose single subject carries the digest and the
`dev.daegam.hub.*` annotations.

Expected verifier results with `hub.pub` configured, for `server.digest`: `verified` and `approved` are collected;
`no-annotations` is not (no hub annotations); `other-key` is not (no configured key verifies it); `other-digest` is not
(signed digest differs). With `hub.pub` and `other.pub` configured, `other-key` is collected too.
