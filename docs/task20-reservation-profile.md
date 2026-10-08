# Proposed Task20 profile

Not implemented or installed. Current Task19 validation/runtime cannot accept
the Task20 packet or execute Task20 stages. Preserve Task19 unchanged.

The root-reviewed session must explicitly select `profile: task20-v1`; supported
profiles will be `task19-v1` and `task20-v1`. Bind selection into the canonical
session/descriptor identity. Uploaded packet labels or JSON cannot select host
authority. This changes the proposed API schema and requires coordinated client
and host implementation before any use.

Closed Task20 packet, twelve members:

1. `reference/approved-linux-perl.json`
2. `repository.bundle`
3. `maintainer.allowed_signers`
4. `rust-toolchain.toml`
5. `task20_bootstrap.py`
6. `qualification_source.py`
7. `source-attestation/document.json`
8. `source-attestation/document.sig`
9. `source-attestation/github-commit.json`
10. `source-attestation/pgp-proof.txt`
11. `source-attestation/integration-ref.json`
12. `task20-round.json`

The Rust pin maps to the selected Git root. Bootstrap and verifier map to
`tools/remote-build/task20_bootstrap.py` and
`tools/remote-build/qualification_source.py` in that same authenticated tree.
The bundle and attestation match the original claim and full source proof.
The round JSON has exactly schema, round, head, tree, run_id and bundle_sha256;
all values match the root claim. The oracle envelope matches a separately
approved digest. Receiver verifies archive SHA, actual byte count and every
closed member without executing uploaded code or accepting arbitrary paths.

Fixed proposed IDs: prepare, upload-receive, verify-source, task20-bootstrap,
task20-execute, task20-pack, task20-replay, cleanup. Ordinals are zero-based in
the infrastructure store; the author's mapping table numbers stages from one.
Do not silently reinterpret result ordinals.

The Task20 bootstrap must consume and reverify the existing read-only sealed
checkout, then create only its owned worktree. Its current absent-checkout clone
contract is incompatible. A root-owned Task20 runtime selects authenticated
source blobs and fixed arguments, separately from Task19's qualification.py.

Execute, pack and replay entrypoints do not exist in the frozen packet/tree.
Their exact Git paths must be supplied in a new authenticated candidate before
the allowlist is implemented. They are never spliced as loose uploaded helpers.
Cleanup is actual quiescence/custody only; preserve all source, target, cache,
result archives and evidence. No Task19 pack/replay substitution is valid.
