# Hub mirror job

The hub admits an `image` item only for a digest under the hub prefix that the hub key signed. Mirroring copies an
upstream image there byte for byte and signs it. The platform holds no push credential and no signing key, so the
mirror runs outside it, in one of two ways:

- **`make hub-mirror`** on a machine that holds the hub key and a registry login that can push under the hub prefix.
  It is the default path.
- **This job**, in a private repository of its own. Use it when the repository has Actions minutes and an account that
  can push only under the hub prefix.

Both run `scripts/hub_mirror.py`, which checks the upstream signature against the catalog's provenance, copies the
digest, signs it with no transparency log and reads the signature back with the platform verifier's rules. Key
rotation (`make hub-resign`) runs only on a machine; the job runs `mirror` only.

## Repository setup

1. Create a private repository that holds nothing but `.github/workflows/mirror.yml`, a copy of `mirror.yml` here.
2. Add these repository variables:
   - `DAEGAM_REPOSITORY`: the public platform repository, which the job checks out without a token for the script
     and the catalog snapshot.
   - `DAEGAM_REF`: the full 40-character commit SHA of the platform release whose catalog snapshot the hub follows.
     The job refuses anything else, such as a tag or a branch. Change it at each snapshot refresh.
   - `HUB_PREFIX`: the hub prefix, `host[:port][/path]`. It must not equal, contain or sit inside a product image
     prefix.
3. Add these secrets:
   - `HUB_PRIVATE_KEY` and `HUB_KEY_PASSWORD`: the hub key, a cosign P-256 key of its own. It is not the release key.
   - `HUB_PUSH_USERNAME` and `HUB_PUSH_PASSWORD`: a registry account that can push only under the hub prefix and
     cannot delete.

On the free plan the repository has no branch protection, so anyone with write access can change the job and read the
key. Its collaborators are the key holders. The platform code that runs with the key is the commit `DAEGAM_REF` names,
so only whoever sets the repository's variables decides it; a push to the platform repository changes nothing until
that variable moves. Each signature records the run (`dev.daegam.hub.run`: who started it, the run URL, the workflow
commit and ref, the platform commit, the catalog release and the upstream ref), and that record is trusted only as far
as the run URL leads to GitHub's own record.

## Running it

Start **hub-mirror** by hand with the upstream reference and its tag. Name the catalog server for a catalog image.
Set **approve** only for an image whose catalog server has no provenance, or for an image outside the catalog; the
job refuses it when provenance exists, and a failed provenance check is final.
