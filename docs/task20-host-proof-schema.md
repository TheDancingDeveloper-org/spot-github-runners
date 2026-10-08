# Task20 root proof projection, version 1

Proposed installed contract; source code exists but no installed/runtime proof.
Consumer must retain BLOCKED_LOCK until exact reviewed bytes, physical RO bind
mount, source/profile and current-container invariants are actually established.

Closed projection keys: schema, reservation_id, session, stage, host, actor,
descriptor_sha256, container, command, argv_sha256, custody_state,
active_ordinal, purpose, seal_sha256. The producer derives these from the actual
root custody record, retained descriptor, immutable source binding and observed
created container. No caller-supplied host_verified/diagnostic override exists.

Session schema 2 is the root-selected task20-v1 claim. Host is actual VM ID and
boot pair; actor is actual stage-unit PID/start. Descriptor SHA covers the complete
root request, including archive/member identities. Stage argv SHA is the exact
root-selected fixed command, matching that ordinal's descriptor. Seal SHA is the
actual immutable execution-binding JSON digest. custody_state is SOURCE_VERIFIED;
active_ordinal equals stage. Purpose is bootstrap at 3, execute at 4, diagnostic
at 5/6. Ordinal 7 is custody-only and executes no container code.

Before Docker create, container is null and never authority. Before Docker start,
the adapter validates actual full ID, image, UID1001, labels, limits and mounts,
fills the same already-bound inode and retains container-stage-N.json. Container
keys: container_id, image_id, created, caps, labels, state. No raw environment or
arbitrary command fields are projected. The mount remains read-only for UID1001.
Actual same-inode delivery and current-container binding require installed Linux
proof; source assertions and synthetic tests cannot substitute for it.

Known terminal Task20 gate failure is recorded by its attested worker as partial
FAILED with transport exit 0. Host finish may therefore advance ordered pack and
replay after actual child/container/transfer quiescence. A nonzero/unknown host
execution remains HELD; no source-side diagnostic flag reopens it. Explicit
reconciliation permits custody cleanup only, preserving all failed artifacts.

The OxiDex consumer validates the complete projection and kernel RO mount, then
uses sealed signed source and purpose-specific stage checks. It must not infer
descriptor authority from a hash's syntax or use uploaded/copyable JSON as proof.
