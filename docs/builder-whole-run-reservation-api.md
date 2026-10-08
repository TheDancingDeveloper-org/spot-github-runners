# Proposed whole-run reservation API, version 1

This is an implementation contract, not an installed interface. The existing
launcher does not implement these verbs. No live effects are authorized.

## Ordering

Use claim-bound reservation **before prepare/upload**, not acquisition after
`verify-source`. Only the small detached signed source-attestation document and
signature cross the control channel before acquisition. They are control data,
not the candidate bundle, bootstrap code or fixtures.

The root control caller supplies the reviewed session descriptor. The host
verifies the existing `oxidex-qualification-source-v1` signature against its
root-owned maintainer anchor, binds the exact document hash, and acquires under
global EX before creating the candidate upload namespace. This authenticates a
source claim; it does not prove uploaded bytes or permit code execution.
Unsigned-source mode cannot use this pre-upload path.

Preparation and upload then occur inside that reservation. Existing full
`verify-source` validates the actual bundle, commit/tree, attestation evidence and
sealed namespace. It must match every claimed identity; only that successful
result promotes CLAIMED to SOURCE_VERIFIED. A mismatch retains the reservation
as HELD for owned cleanup, rather than reopening the host to another build.

## Fixed control verbs

Root-controlled API: `reserve SESSION`, `inspect RESERVATION`,
`stage RESERVATION ORDINAL`, `reconcile RESERVATION`, `release RESERVATION`.
SESSION is a bounded JSON document carried by the existing authenticated control
transport, with no shell interpolation. `stage` selects a preapproved entry;
it does not accept arbitrary uploaded argv or writable host paths. No uploader
verb may declare itself root-approved or host-verified.

Reserve request keys (exact schema; unknown keys rejected):

```json
{
  "schema": 1,
  "project": "qualification-<head12>-<nonce32>",
  "attempt": "final-rN",
  "expected_vm_id": "<numeric provider identity>",
  "expected_boot": "<observed UUID>",
  "head": "<40 hex>",
  "tree": "<40 hex>",
  "bundle_sha256": "<64 hex>",
  "attestation_sha256": "<64 hex>",
  "attestation_namespace": "oxidex-qualification-source-v1",
  "stages_sha256": "<64 hex of reviewed ordered stage descriptors>"
}
```

The root-reviewed stage descriptors bind ordinal, stage name, fixed command ID,
exact argv digest and candidate/target/evidence path roles. Host code derives
the actual paths from the project and fixed roots, checks the supplied roles
against those paths, and validates the command against the existing allowlist.
The attestation signature authenticates source only. Stage authorization comes
from the separately root-controlled reviewed descriptor; do not extend the
meaning of the existing source signature.

The host-generated record adds reservation ID (cryptographic nonce), actual VM
and boot identities, owner UID/PID/start, creation time, state, current ordinal
and last durable event sequence. These are host observations, not request fields.
Stages are ordered prepare, upload, verify-source, bootstrap, oracle, regenerate,
test, measure, pack, replay, cleanup. A reviewed descriptor may omit optional
stages; it cannot reorder or repeat completed stages. Task20 uses one host execute
ordinal with authenticated ordered internal step rows and descendant evidence.
Those internal steps are not independently callable host commands. The same
binding covers nested children; see the separate Task20 profile proposal.

## Results and uncertain recovery

Reserve returns the immutable identity projection and durable event sequence.
Lost acknowledgement is resolved by `inspect` using the original project and
claim digest; retry cannot allocate a second reservation. The reservation ID
is an identifier, not a bearer authorization token.

Stage results record actual launched PID/start, full container ID when present,
submitted exact ordinal, exit status, log hash and descendant quiescence. `inspect` and `reconcile` never
execute a missing stage or infer success from process absence. Unknown process,
Docker, upload/export lock or host identity observations retain HELD custody.

Release requires the same root-controlled descriptor, actual terminal stage
records and complete owned cleanup under global EX. Lost release acknowledgement
is answered from a retained RELEASED tombstone. A completed ordinal cannot be
rerun under the same reservation. Failure cleanup is explicit and preserves
evidence and cache; there is no automatic rollback or expiry-based reclamation.

All heavy launcher, Docker admission, source import, upload and result-export
entrypoints must check the reservation. Until that enrollment is complete,
the API cannot claim whole-run exclusion. VM/boot replacement refuses old stage
tokens and records interruption; explicit reconciliation precedes new ownership.
Custody records and tombstones live in the private persistent cache directory
`/mnt/runner-data/remote-build/qualification-reservations`, not the disposable
boot disk. A replaced VM encounters the original identity-bound hold.
