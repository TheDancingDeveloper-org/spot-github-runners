# Reservation implementation status

Source candidate only; no installed interface, live lease, payload or readiness.

Implemented: bounded durable custody store, strict session/stage binding,
detached signature verification through the existing source trust anchor,
PID/start ancestry and metadata VM/boot observations, launcher/Docker/admission
read-side gates, startup/runtime-update enrollment, retained uncertainty,
interrupted-stage recovery and archived release tombstones. Twenty-eight focused
synthetic controls pass, including a real file-descriptor admission lock test.
Custody resides in a private directory on persistent cache storage. Released
namespaces stay fenced; finalization requires the submitted exact ordinal and
cannot retry a refused stage. Event bounds apply before durable publication.
Trusted enrollment binds the mounted cache UUID/device; unavailable enrolled
storage refuses work. Command IDs are checked against fixed arguments, and
complete process ancestry links are revalidated before access.

The production acquire/stage/reconcile/release control endpoint is not enabled.
Trusted actual quiescence and full-source-binding producers must be connected;
the store's callback seam is not evidence by itself. No test-created owner PID,
host identity or fixture callback is a live proof.

Upload needs a root-supervised fixed receiver bound to the actual stage process.
Independent ordinary rsync SSH sessions are not that process's descendants.
The Task20 adapter owner must provide its exact transfer mapping; do not invent
covered upload activity or accept arbitrary receiver paths/argv.

Fixed heavy commands reuse the current host qualification allowlist: bootstrap,
remote-run, pack and replay. Oracle/regeneration/testing/measurement occur inside
the attested remote-run worker. Additional top-level commands require a reviewed
fixed mapping. The stage digest checks the launcher's arguments excluding the
project. No stage descriptor authorizes arbitrary uploaded command execution.

The final cleanup ordinal is a control-plane quiescence observation, not the
existing destructive source-cleanup launcher verb. It preserves all source,
target, cache and evidence paths; release changes custody only. Recovery cannot
repeat an uncertain cleanup or turn missing exit evidence into success.

Full review, complete route/transfer enrollment, remote full suites and actual
lost-SSH/child-survival proof remain required. This source must not be installed
as a working Task20 prerequisite before those seams are completed and reviewed.
