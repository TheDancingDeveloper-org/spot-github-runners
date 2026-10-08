# Whole-run builder reservation

Source preparation only. No installation, VM spend, caps, labels or payload
execution is authorized by this document.

## Contract

One host-owned reservation spans source bootstrap, every heavy stage and child,
result packing, replay and cleanup. The reservation binds actual provider VM ID,
boot ID, run ID and attested source head. Every ordinary and qualification heavy
route must refuse a foreign reservation on that host. An empty container list
between stages is insufficient to release it.

## Narrow implementation

Add one root-owned reservation record and fixed launcher verbs for acquisition,
inspection, reconciliation and release. Reuse the existing global build lock,
admission ledger and qualification source identity. Do not create another daemon,
queue, worker registry or telemetry service.

Acquisition runs under global EX, with complete actual child/container and
admission inventories, no foreign drain or unfinished reservation. Write the
record exclusively, durably and without symlink traversal. Record the actual
owner process identity and custody token; a token alone is not permission to
submit another source or impersonate ownership. A lost acquisition acknowledgement
must be recoverable by inspection without acquiring a second reservation.

Each stage enters under the existing global lock and admission transaction,
validates exact reservation/source/boot identity, and updates its owned child
identity before execution. A foreign route refuses before source or cache work.
The reservation is accounted once: owner stages consume the reserved allocation
without double counting; foreign admission cannot bypass it. Upload, archive and
replay activity remain bound to the same reservation through final cleanup.

Controller death, lost SSH, missing acknowledgements and unknown Docker/process
observations retain the record and capacity. Reconciliation is read-only until
complete same-run child/container, admission and source/cache/upload/export lock
proof permits an explicitly owned cleanup. Never reclaim because a timer expired.

Release requires same-run terminal result and complete cleanup evidence under
global EX. Preserve failure receipts and caches. Reboot or Spot replacement
rejects old tokens and requires explicit reconciliation; it never restamps prior
proof or reports interrupted work as successful.

## Owned source paths and dependency

Infrastructure branch `codex/builder-whole-run-reservation`, based on
`f20f9f9b780fb00c82a9f2aceb0f45d87e932321`.
Planned paths: `builder/dedicated/host-launcher.py`,
`builder/dedicated/docker-admission.py`, `src/lib/host_admission.py`, a small
reservation module under `src/lib/qualification_trust/`, canonical startup asset
enrollment and focused tests. OxiDex's Task20 source adapter is owned separately
by the release session. Its request identity and stage-entry API must agree with
this contract before code integration. Pressure policy work remains in its
separate OxiDex worktree.

## Proof controls

- Ordinary and qualification foreign routes refuse during every stage gap.
- A forged token, different source, duplicate run or wrong boot cannot enter.
- Lost acquisition/release acknowledgements do not replay effects.
- Controller death, surviving child and unknown observation retain capacity.
- Nested children and result packing remain covered by one reservation.
- Exact owned cleanup releases once; foreign jobs, records and caches survive.
- Reboot/replacement invalidates access without fabricating completion.
- Normal builds without reservations keep their existing admission behavior.

Focused synthetic controls precede implementation. Signed whole-source review,
remote full suites and a coordinated actual lost-SSH/child-survival canary precede
any readiness claim. Root must inspect the complete candidate before installing.
