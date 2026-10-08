# Update an existing builder runtime

Use this administrative guide only when updating an occupied builder. For a
fresh instance and ordinary `just build` usage, see
[Dedicated remote builders](dedicated-builders.md).


Use the standalone `src/lib/builder_runtime_update.py` helper through the existing
authenticated root control path for an occupied dedicated builder. Its default
mode checks a reviewed plan without writing files. Capture mode records the
actual VM, boot, runtime files, and admission process; plans expire after
120 seconds. It does not provision a VM or rebuild an image.

The maintainer supplies a root-owned asset JSON file containing exactly the
25 fixed runtime targets and their reviewed bytes and hashes. The SSH
ForceCommand and sudoers entries are part of that set. Generate their bytes
with `uploader_policy_bytes` from the existing root-owned uploader mapping;
the updater rejects policy bytes that differ from that mapping. Capture a fresh
plan after source review and builder handback:

```bash
python3 -I -B builder_runtime_update.py --capture-assets assets.json \
  --assets-sha256 ASSET_FILE_SHA256 --source-main REVIEWED_SOURCE_COMMIT
```

Save the returned plan as a private root-owned file and record its SHA-256.
Check it before applying:

```bash
python3 -I -B builder_runtime_update.py --plan plan.json \
  --plan-sha256 PLAN_FILE_SHA256
python3 -I -B builder_runtime_update.py --plan plan.json \
  --plan-sha256 PLAN_FILE_SHA256 --apply
```

Applying takes the existing build lock, refuses any process carrying the
enrolled uploader UID as well as active builds and uploads,
creates an owned drain, backs up old files, and replaces only the fixed runtime
file set. It validates sshd, sudoers, and effective uploader access, then
reloads SSH and restarts admission. It preserves the image, build limits,
source worktrees, and caches. A partial failure retains the drain and backups
for explicit recovery. Success verifies installed bytes and the new admission
process while retaining the owned drain.

For a partial apply that failed before `FILES_AND_SERVICE_VERIFIED`, select
the original saved plan and its exact digest to restore only the two uploader
policies from the private backup:

```sh
python3 -I -B builder_runtime_update.py --plan original-plan.json \
  --plan-sha256 "$ORIGINAL_PLAN_SHA256" --recover-policy
```

This action requires the same provider VM, the retained pending marker and
drain, an idle host, and policy files matching either the selected backup or
the selected newly installed bytes. It validates sshd and sudoers, reloads SSH,
records `POLICY_ROLLED_BACK_HELD`, and remains held. A crash after publication
can repeat the same recovery command; a foreign policy file is preserved and
refused. Runtime files and the launcher still need separately reviewed
recovery. Policy rollback authenticates the saved plan, journal, and owned hold
before loading either the unchanged historical helper or the exact reviewed
helper already installed by that update. It retains that file identity and
trust phase through policy and work-process checks; foreign helper bytes are
refused. This command never reopens admission.

Coordinate the exact OxiDex launcher pin before this update. The helper does not
change clients or declare qualification ready. Run the reviewed Linux gate and
ordinary build checks before routing qualification work to the updated host. A successful installation
returns `INSTALLED_DRAINED`; it keeps the route off until an independently
verified client-pin handoff authorizes removal of the exact owned drain. The
updater never reopens the route automatically.

An already enrolled host with a schema-1 `reservation_enrollment` containing
`device` (a boot-local filesystem number) needs an explicit root-reviewed
migration before the new reservation source can route work. Under the held
maintenance fence, independently observe the mounted ext4 UUID, the approved
`/dev/disk/by-id/google-...` mapping, and its hardware serial; publish schema 2
with `uuid`, `by_id`, and `serial` only after those facts agree. Preserve the
existing reservation and drain throughout. A missing/foreign mount or unknown
legacy record must remain held; do not copy the old `device` value into the
new identity or treat an old source-only CI result as a host migration receipt.

The update also installs a fixed admission-service boot guard and a persistent
pending marker. A private permit in `/run` allows only the current maintenance
boot to restart admission while that marker exists. Reboot removes the permit,
so admission stays off until explicit recovery or the verified handoff. The
pending marker and volatile drain are separate from build leases.

Preparation only writes private backups and leaves the live runtime unchanged.
The first live cutover removes launcher execution permission and persists that
mode before runtime replacement. Both the previous and installed launcher stay
readable but non-executable (`0644`) through partial updates and reboot. Restoring
`0755` is part of the separately verified client-pin handoff, not automatic
installation success. Administrative controlled proofs use the authenticated
root control path; the uploader cannot bypass this hold.


### Finish an installed update

After controlled source checks and coordination of the exact client launcher
pin, capture a new observation with the original installation nonce:

```sh
python3 -I -B builder_runtime_update.py --capture-assets assets.json \
  --assets-sha256 "$ASSETS_SHA256" --source-main "$SOURCE_MAIN" \
  --finish-nonce "$INSTALLED_NONCE" \
  --client-launcher-sha256 "$CLIENT_LAUNCHER_SHA256" > finish-plan.json
```

Keep the original installation plan and journal. This capture has its own
observation time and does not extend or rewrite the original plan. Inspect the
new plan and select its exact digest through the authenticated root control path.
First check the selected plan without changing the route:

```sh
python3 -I -B builder_runtime_update.py --plan finish-plan.json \
  --plan-sha256 "$FINISH_PLAN_SHA256" \
  --client-launcher-sha256 "$CLIENT_LAUNCHER_SHA256"
```

Then explicitly finish:

```sh
python3 -I -B builder_runtime_update.py --plan finish-plan.json \
  --plan-sha256 "$FINISH_PLAN_SHA256" --finish \
  --client-launcher-sha256 "$CLIENT_LAUNCHER_SHA256"
```

Finish verifies the successful installation journal and current host under the
existing build lock. It keeps the launcher held through final checks and records a durable release
decision before restoring execution. It then removes its persistent pending
marker and removes the owned drain last. It returns
`REOPENED_UNQUALIFIED`, with qualification readiness false. Run one actual
ordinary OxiDex Just build and the qualification checks afterward. The client
selector requires an executable launcher and no drain, so this ordinary build
cannot be a pre-finish check. Do not treat reopening as proof of an app build.

If finish fails, inspect its journal and actual files before recovery. Its
failure handler attempts to close execution only on the exact installed
launcher while the build lock remains held. It never rolls back runtime bytes,
clears leases, stops builds, or modifies unrelated services. A changed or foreign
file needs explicit recovery.

### Recover an interrupted finish

For an interrupted finish on the same boot and unchanged admission process,
capture with the recovery action as well as the original nonce and client pin:

```sh
python3 -I -B builder_runtime_update.py --capture-assets assets.json \
  --assets-sha256 "$ASSETS_SHA256" --source-main "$SOURCE_MAIN" \
  --finish-nonce "$INSTALLED_NONCE" --recover-finish \
  --client-launcher-sha256 "$CLIENT_LAUNCHER_SHA256" > recovery-plan.json
```

Inspect the new observation and select its digest. Recovery is explicit:

```sh
python3 -I -B builder_runtime_update.py --plan recovery-plan.json \
  --plan-sha256 "$RECOVERY_PLAN_SHA256" --recover-finish \
  --client-launcher-sha256 "$CLIENT_LAUNCHER_SHA256"
```

Recovery checks the successful installation and known finish stages under the
existing exclusive lock. It closes execution only on the selected launcher,
restores missing owned update markers, and retains the drain. It appends a
`FINISH_RECOVERED_HELD` record; it does not rewrite the original journal or
observation time. Capture a fresh finish plan afterward. Never replay apply or
remove journal entries to retry.

A changed boot, admission process, runtime file, or foreign marker is refused.
Those cases need a separately reviewed recovery scope. This command does not
restart services, reopen builds, or declare qualification readiness.

### Complete a committed release after reboot

If reboot interrupted an already committed release, use `--recover-committed`
in the capture command instead of `--recover-finish`. The new schema-2
observation preserves the original journal and boot history. It records either
an exactly inactive unit or the verified PID and start ticks of its running
admission process. It refuses changed runtime files, image, configuration,
client pin, or unknown service state.

```sh
python3 -I -B builder_runtime_update.py --capture-assets assets.json \
  --assets-sha256 "$ASSETS_SHA256" --source-main "$SOURCE_MAIN" \
  --finish-nonce "$INSTALLED_NONCE" --recover-committed \
  --client-launcher-sha256 "$CLIENT_LAUNCHER_SHA256" > reboot-plan.json
```

After inspecting that fresh observation and selecting its exact digest:

```sh
python3 -I -B builder_runtime_update.py --plan reboot-plan.json \
  --plan-sha256 "$REBOOT_PLAN_SHA256" --recover-committed \
  --client-launcher-sha256 "$CLIENT_LAUNCHER_SHA256"
```

Recovery rechecks the durable release decision under the build lock. It retains
the persistent pending marker and uses an owned current-boot permit to start
only an inactive admission unit. After verifying its process, socket, and current
host state, it records a durable checkpoint, removes its markers, and releases
the drain. A missing pending marker is recreated before startup.
If the exact process is already running after a partial attempt, recovery checks
it without another start command. A failed start or postflight retains the persistent marker and owned
drain; another reboot removes the permit and leaves admission held. Successful recovery remains unqualified until actual OxiDex build and
gate checks pass.

After reboot, recovery initializes an absent volatile build lock without
replacing an existing inode. A held post-commit launcher remains recoverable:
the saved release decision must match every file, with only its execution mode
held. Recovery restores execution through the verified descriptor while the
owned drain remains. It rechecks freshness after publishing its checkpoint and
before drain release.

Permission transitions publish durable intent before changing the selected
launcher. Recovery checks both sides of that transition against the same inode
and bytes. Slow recovery observations must pass the selected plan's freshness
check again before any permission change.

The release journal distinguishes the observed held files from the authorized
executable state. A committed decision is permission to complete that transition,
not evidence that it already happened. A completed re-hold keeps that authority
for checked recovery after another reboot.

If unlink succeeded but directory sync failed, the helper reports
`RELEASE_UNLINKED_SYNC_UNCONFIRMED` and exits75. It states that the drain was
removed and does not claim a protective hold or close launcher execution.
Inspect actual jobs, leases and files before any further recovery; do not replay
apply or treat this result as a held installation.

The transaction pins the authorized journal identity and complete prefix. Only
verified writes by that transaction advance the pin. Unexpected replacement or
appended history is preserved and refused, including during failure holding.
Selected reboot plans must match the complete schema before recovery effects.
