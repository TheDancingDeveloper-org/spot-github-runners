"""Recorded runner patch: instance metadata at provisioning and after a confirmed live patch, inventory parsing,
and the dashboard rollout preview built from it. No gcloud, GitHub, or GCE call leaves the process."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
import unittest
import unittest.mock

from lib import compute_api, patch_console as pc, runner_fleet
from lib import runner_manager as manager
from lib.runner_patch import validate_patch_dict
from lib.target_config import target_label_value

REPO = "swack-tools/oxidex"
TOKEN = "ghs_REGISTRATION_SECRET_123"
BASE = {"schema": "spot-runner-patch-v1", "repository": REPO}
TARGET = validate_patch_dict({**BASE, "container": {"apt_packages": ["ripgrep"]}})
OLD = validate_patch_dict(dict(BASE))
MOUNTS = validate_patch_dict({**BASE, "container": {"mounts": [{"source": "/srv/cache", "target": "/cache"}]}})
ARM = validate_patch_dict({**BASE, "hardware": {"arch": "arm64"}})


def canonical_json(patch) -> str:
    return json.dumps(patch.to_canonical_dict(), sort_keys=True, separators=(",", ":"))


BOOT_ID = "0f6c3a52-8d1e-4b7a-9c2d-5e8f1a2b3c4d"
BOOT_TS = "2026-10-01T09:00:00.123-07:00"


def receipt_output(patch=None, *, pending=None, receipt_boot=BOOT_ID, boot=BOOT_ID, legacy=False):
    """What PATCH_RECEIPT_READ_COMMAND prints for a host (base64 state and pending marker, then the boot id)."""
    import base64

    def b64(obj):
        return base64.b64encode(json.dumps(obj).encode()).decode() if obj is not None else ""

    state = None
    if patch is not None:
        state = ({"manifest": patch.to_canonical_dict()} if legacy else
                 {"boot_id": receipt_boot, "receipt": {"receipt_schema": "gha-runner-patch-receipt-v2",
                                                       "patch_sha256": patch.patch_sha256,
                                                       "manifest": patch.to_canonical_dict()}})
    marker = {"pending_patch_sha256": pending.patch_sha256} if pending is not None else None
    return f"state:{b64(state)}\npending:{b64(marker)}\nboot:{boot}\n"


def args_for(patch, *extra):
    args = manager.args_parser().parse_args(["--max-cost", "0.3", "--instance-type", "c4d-standard-32", "--cpu", "32",
                                             "--ram", "128", "--target", REPO, "--runner-prefix", "oxidex",
                                             "--runner-count", "3", "--instance-name", "oxidex-runners-1", *extra])
    args.runner_patch = patch
    manager.select_sizing(args)
    return args


class GcloudRecorder:
    """Fake ``runner_manager.gcloud`` that also snapshots --metadata-from-file contents (the files are temporary)."""

    def __init__(self, responder=None):
        self.calls: list[list[str]] = []
        self.files: list[dict[str, str]] = []
        self.responder = responder

    def __call__(self, command, **_kwargs):
        self.calls.append(list(command))
        for arg in command:
            if arg.startswith("--metadata-from-file="):
                pairs = dict(p.split("=", 1) for p in arg.split("=", 1)[1].split(","))
                self.files.append({k: Path(v).read_text(encoding="utf-8") for k, v in pairs.items()})
        if self.responder is not None:
            return self.responder(command)
        return "[]" if command[:3] == ["compute", "disks", "list"] else ""

    def named(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


def plain_metadata(command) -> dict[str, str]:
    flag = next(a for a in command if a.startswith("--metadata="))
    return dict(p.split("=", 1) for p in flag[len("--metadata="):].split(","))


def applied_writes(cloud):
    return [c for c in cloud.named("compute", "instances", "add-metadata") if "spot-runner-patch-sha256" in plain_metadata(c)]


def pending_writes(cloud):
    return [c for c in cloud.named("compute", "instances", "add-metadata")
            if set(plain_metadata(c)) == {"spot-runner-patch-pending-sha256"}]


def live_sequence(cloud):
    """The live patch's steps, named: receipt read, pending write, live script, applied write, label, clear."""
    steps = []
    for c in cloud.calls:
        if c[:2] == ["compute", "ssh"]:
            steps.append("read-receipt" if "state.json" in c[-1] else "live-script")
        elif c[:3] == ["compute", "instances", "add-metadata"]:
            steps.append("write-applied" if "spot-runner-patch-sha256" in plain_metadata(c) else "write-pending")
        elif c[:3] == ["compute", "instances", "add-labels"]:
            steps.append("label")
        elif c[:3] == ["compute", "instances", "remove-metadata"]:
            steps.append("clear-pending" if "pending" in c[-1] else c[-1])
    return steps


def apply_metadata_writes(item, cloud):
    """Replay the recorded gcloud metadata writes onto a GCE item, as GCE would."""
    items = {e["key"]: e["value"] for e in item["metadata"]["items"]}
    files = iter(cloud.files)
    for c in cloud.calls:
        if c[:3] == ["compute", "instances", "add-metadata"]:
            items.update(plain_metadata(c))
            if any(a.startswith("--metadata-from-file=") for a in c):
                items.update(next(files))
        elif c[:3] == ["compute", "instances", "remove-metadata"]:
            for key in c[-1][len("--keys="):].split(","):
                items.pop(key, None)
    return {**item, "metadata": {"items": [{"key": k, "value": v} for k, v in items.items()]}}


class ProvisioningMetadataTests(unittest.TestCase):
    def provision(self, patch, vm=None):
        args = args_for(patch, *(["--existing-instance"] if vm else []))
        script = manager.render_startup(args, TOKEN, "group", "deploy-1")
        cloud = GcloudRecorder()
        describe = [SimpleNamespace(returncode=0 if vm else 1, stdout=json.dumps(vm) if vm else ""),
                    SimpleNamespace(returncode=0, stdout=json.dumps({"type": args.disk_type, "users": ["x"]}) if vm else "")]
        with unittest.mock.patch("lib.runner_manager.subprocess.run", side_effect=describe), \
                unittest.mock.patch("lib.runner_manager.registered_runner_rows", return_value=[]), \
                unittest.mock.patch("lib.runner_manager.gcloud", side_effect=cloud):
            manager.apply_startup(args, "us-central1-a", script)
        return args, cloud

    def assert_no_secret(self, cloud):
        for command in cloud.calls:
            self.assertNotIn(TOKEN, " ".join(command))
        for files in cloud.files:
            for key, text in files.items():
                if key != "startup-script":
                    self.assertNotIn(TOKEN, text, key)

    def test_create_records_the_patch_as_pending_only(self):
        """Codex round 13 P2: a new VM advertises its patch only as PENDING; no applied keys or label yet."""
        args, cloud = self.provision(TARGET)
        [create] = cloud.named("compute", "instances", "create")
        self.assertEqual(plain_metadata(create), {
            "spot-runner-patch-target": REPO, "spot-runner-prefix": "oxidex",
            "spot-runner-count": str(args.runner_count),  # the count select_sizing settled on
            "spot-runner-label": args.runner_label, "spot-runner-region": args.region,
            "spot-runner-registration-target": REPO,  # repo-registered: no runner group
            "spot-runner-patch-pending-sha256": TARGET.patch_sha256})
        [files] = cloud.files
        self.assertEqual(set(files), {"startup-script", "spot-runner-patch-pending"})
        self.assertIn(TOKEN, files["startup-script"])  # the token only ever travels in the startup script
        self.assertEqual(files["spot-runner-patch-pending"], canonical_json(TARGET))
        labels = next(a for a in create if a.startswith("--labels="))
        self.assertEqual(labels, f"--labels=spot-target={target_label_value(REPO)}")  # no spot-runner-patch label
        self.assertEqual(args.startup_patch, TARGET)  # what main() records once the bootstrap confirms it
        self.assert_no_secret(cloud)

    def test_org_registration_scope_and_group_are_recorded_apart_from_the_patch_repo(self):
        args = args_for(TARGET)
        args.target = "swack-tools"
        plain, _manifest, _label = manager.runner_patch_record(args, TARGET)
        self.assertEqual((plain["spot-runner-registration-target"], plain["spot-runner-group-id"],
                          plain["spot-runner-patch-target"]), ("swack-tools", str(args.runner_group_id), REPO))

    def test_create_without_patch_records_none_as_pending(self):
        _, cloud = self.provision(None)
        [create] = cloud.named("compute", "instances", "create")
        self.assertEqual(plain_metadata(create)["spot-runner-patch-pending-sha256"], "none")
        self.assertNotIn("spot-runner-patch-sha256", plain_metadata(create))
        self.assertEqual(set(cloud.files[0]), {"startup-script"})
        self.assertNotIn("spot-runner-patch=", next(a for a in create if a.startswith("--labels=")))
        self.assert_no_secret(cloud)

    def test_oversized_manifest_is_recorded_by_sha_only(self):
        big = validate_patch_dict({**BASE, "host": {"files": [{"path": "/etc/big", "content": "x" * 70_000}]}})
        _, cloud = self.provision(big)
        [create] = cloud.named("compute", "instances", "create")
        self.assertEqual(plain_metadata(create)["spot-runner-patch-pending-sha256"], big.patch_sha256)
        self.assertEqual(set(cloud.files[0]), {"startup-script"})

    def test_existing_vm_gets_pending_metadata_and_keeps_its_applied_record(self):
        vm = {"status": "RUNNING", "machineType": "zones/z/machineTypes/c4d-standard-32",
              "scheduling": {"provisioningModel": "SPOT"}, "cpuPlatform": "AMD Turin",
              "disks": [{"boot": True, "deviceName": "boot"}, {"deviceName": "runner-data"}],
              "metadata": {"items": [{"key": "spot-runner-patch", "value": canonical_json(OLD)},
                                     {"key": "spot-runner-patch-sha256", "value": OLD.patch_sha256},
                                     {"key": "spot-runner-patch-pending", "value": canonical_json(MOUNTS)}]}}
        _, cloud = self.provision(None, vm=vm)
        [add] = [c for c in cloud.named("compute", "instances", "add-metadata")]
        self.assertEqual(plain_metadata(add)["spot-runner-patch-pending-sha256"], "none")
        self.assertNotIn("spot-runner-patch-sha256", plain_metadata(add))  # the applied record is left alone
        # Only the stale pending manifest from an earlier attempt goes; the applied manifest stays.
        self.assertEqual([c[-1] for c in cloud.named("compute", "instances", "remove-metadata")],
                         ["--keys=spot-runner-patch-pending"])
        self.assertEqual([c[-1] for c in cloud.named("compute", "instances", "add-labels")],
                         [f"--labels=spot-target={target_label_value(REPO)}"])  # adopted explicitly (--target)
        self.assert_no_secret(cloud)
        _, cloud = self.provision(TARGET, vm=vm)
        self.assertEqual(cloud.named("compute", "instances", "remove-metadata"), [])
        self.assertEqual(cloud.files[0]["spot-runner-patch-pending"], canonical_json(TARGET))
        self.assertNotIn("spot-runner-patch", cloud.files[0])


class BootstrapConfirmationTests(unittest.TestCase):
    """Codex round 13 P2: the applied patch keys are published only after main() confirms the bootstrap."""

    def deploy(self, *, bootstrap_error=None, short_on_check=None, later_boot=BOOT_TS, marker_boot=BOOT_TS,
               start_boot=BOOT_TS):
        cloud = GcloudRecorder()
        running = runner_fleet.GceVm("oxidex-runners-1", "us-central1-a", "RUNNING", "c4d-standard-32", True,
                                     instance_id="1", cpu_platform="AMD Turin", last_start_timestamp=BOOT_TS)

        def bootstrap(*_args):
            cloud.calls.append(["BOOTSTRAP_MARKER_CHECKED"])
            if bootstrap_error is not None:
                raise bootstrap_error

        checks = []

        def online(args):
            cloud.calls.append(["RUNNERS_ONLINE_CHECKED"])
            checks.append(1)
            return [f"oxidex-{i}" for i in range(args.runner_count - (1 if len(checks) == short_on_check else 0))]

        argv = ["manager", "--max-cost", "0.3", "--instance-type", "c4d-standard-32", "--cpu", "32", "--ram", "128",
                "--target", REPO, "--runner-prefix", "oxidex", "--runner-count", "3",
                "--instance-name", "oxidex-runners-1", "--zone", "us-central1-a", "--apply"]
        missing = SimpleNamespace(returncode=1, stdout="")
        saved = (manager.PROJECT, manager.ORG)
        with unittest.mock.patch("sys.argv", argv), unittest.mock.patch.object(manager, "configure_logging"), \
                unittest.mock.patch.object(manager, "load_env"), \
                unittest.mock.patch.object(manager, "verify_price_and_capacity", return_value=manager.Decimal("0.2")), \
                unittest.mock.patch.object(manager, "generate_registration_token", return_value=TOKEN), \
                unittest.mock.patch.object(manager, "log_project_costs"), \
                unittest.mock.patch.object(manager, "resolve_runner_patch", return_value=TARGET), \
                unittest.mock.patch.object(manager, "observed_deployment_vm",
                                  side_effect=[replace(running, last_start_timestamp=start_boot),
                                               replace(running, last_start_timestamp=marker_boot),
                                               replace(running, last_start_timestamp=later_boot)]), \
                unittest.mock.patch.object(manager, "write_deployment_receipt"), \
                unittest.mock.patch.object(manager, "wait_for_bootstrap", side_effect=bootstrap), \
                unittest.mock.patch.object(manager, "online_runners", side_effect=online), \
                unittest.mock.patch.object(manager, "sync_runner_labels"), \
                unittest.mock.patch.object(manager, "verify_runner_storage"), \
                unittest.mock.patch.object(manager, "remove_startup_metadata_if_vm_exists"), \
                unittest.mock.patch("lib.runner_manager.subprocess.run", side_effect=[missing, missing]), \
                unittest.mock.patch("lib.runner_manager.gcloud", side_effect=cloud):
            try:
                result = manager.main()
            except (Exception, SystemExit) as exc:  # the test inspects the failure, including sys.exit
                result = exc
            finally:
                manager.PROJECT, manager.ORG = saved
        return result, cloud

    def applied_writes(self, cloud):
        return ([c for c in cloud.named("compute", "instances", "add-metadata")
                 if "spot-runner-patch-sha256" in plain_metadata(c)],
                [c for c in cloud.named("compute", "instances", "add-labels") if "spot-runner-patch=" in c[-1]],
                [c for c in cloud.named("compute", "instances", "remove-metadata") if "pending" in c[-1]])

    def test_pending_first_then_applied_only_after_the_success_checks(self):
        result, cloud = self.deploy()
        self.assertEqual(result, 0)
        steps = [c[0] if len(c) == 1 else " ".join(c[:3]) for c in cloud.calls]
        create = steps.index("compute instances create")
        marker = steps.index("BOOTSTRAP_MARKER_CHECKED")
        last_online = max(i for i, step in enumerate(steps) if step == "RUNNERS_ONLINE_CHECKED")
        self.assertIn("spot-runner-patch-pending-sha256", plain_metadata(cloud.calls[create]))
        self.assertNotIn("spot-runner-patch-sha256", plain_metadata(cloud.calls[create]))
        self.assertLess(create, marker)
        self.assertEqual(steps[last_online + 1:], ["compute instances add-metadata", "compute instances add-labels",
                                                   "compute instances remove-metadata"])
        applied = cloud.calls[last_online + 1]
        self.assertEqual(plain_metadata(applied)["spot-runner-patch-sha256"], TARGET.patch_sha256)
        self.assertEqual(plain_metadata(applied)["spot-runner-patch-boot"], BOOT_TS)  # bound to the confirmed boot
        self.assertEqual(cloud.files[-1], {"spot-runner-patch": canonical_json(TARGET)})
        self.assertEqual(cloud.calls[-2][-1], f"--labels=spot-runner-patch={TARGET.patch_sha256[:16]}")
        self.assertEqual(cloud.calls[-1][-1], "--keys=spot-runner-patch-pending-sha256,spot-runner-patch-pending")

    def test_failed_bootstrap_never_publishes_the_applied_keys(self):
        for name, kwargs in (("bootstrap timed out", {"bootstrap_error": TimeoutError("no marker")}),
                             ("runner missing", {"short_on_check": 1}),
                             ("runner dropped after the storage check", {"short_on_check": 2})):
            with self.subTest(name):
                result, cloud = self.deploy(**kwargs)
                self.assertIsInstance(result, BaseException)
                self.assertEqual(self.applied_writes(cloud), ([], [], []))  # pending stays, applied stays unset
                [create] = cloud.named("compute", "instances", "create")
                self.assertEqual(plain_metadata(create)["spot-runner-patch-pending-sha256"], TARGET.patch_sha256)

    def test_restart_during_confirmation_records_nothing(self):
        """Codex round 14 P2: the applied record names the boot it was confirmed in, or nothing is recorded."""
        for later in ("2026-10-02T00:00:00.000-07:00", ""):
            with self.subTest(later=later):
                result, cloud = self.deploy(later_boot=later)
                self.assertIsInstance(result, RuntimeError)
                self.assertIn("lastStartTimestamp is unknown" if not later else "restarted while", str(result))
                self.assertEqual(self.applied_writes(cloud), ([], [], []))


    def test_reboot_after_the_marker_read_never_confirms_the_new_boot(self):
        """Codex round 22 P2: the boot is captured before the marker wait and must hold through publication."""
        reboot = "2026-10-02T00:00:00.000-07:00"
        # The VM rebooted after the marker was read but before the next observation: that observation (and any
        # later one) shows the new boot, which never confirmed anything, so nothing applied is written.
        result, cloud = self.deploy(marker_boot=reboot, later_boot=reboot)
        self.assertIsInstance(result, RuntimeError)
        self.assertIn(f"boot {BOOT_TS}, now {reboot}", str(result))
        self.assertEqual(self.applied_writes(cloud), ([], [], []))  # pending stays
        steps = [c[0] if len(c) == 1 else " ".join(c[:3]) for c in cloud.calls]
        self.assertIn("BOOTSTRAP_MARKER_CHECKED", steps)
        self.assertNotIn("RUNNERS_ONLINE_CHECKED", steps)  # aborted as soon as the change was observed

    def test_unknown_boot_before_the_marker_wait_never_confirms(self):
        result, cloud = self.deploy(start_boot="")
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("lastStartTimestamp is unknown", str(result))
        self.assertEqual(self.applied_writes(cloud), ([], [], []))

    def test_unchanged_boot_through_publication_is_confirmed(self):
        result, cloud = self.deploy()
        self.assertEqual(result, 0)
        [applied], [_label], [_pending] = self.applied_writes(cloud)
        self.assertEqual(plain_metadata(applied)["spot-runner-patch-boot"], BOOT_TS)


class AdoptionOwnershipTests(unittest.TestCase):
    """Codex round 10 P1-b: --existing-instance validates and sets the target ownership label before any change."""

    def adopt(self, labels, *options, registrations=None, metadata=()):
        args = manager.args_parser().parse_args(["--max-cost", "0.3", "--instance-type", "c4d-standard-32",
                                                 "--cpu", "32", "--ram", "128", "--instance-name", "oxidex-runners-1",
                                                 "--existing-instance", *options])
        manager.select_sizing(args)
        vm = {"status": "RUNNING", "machineType": "zones/z/machineTypes/c4d-standard-32",
              "scheduling": {"provisioningModel": "SPOT"}, "cpuPlatform": "AMD Turin", "labels": labels,
              "disks": [{"boot": True, "deviceName": "boot"}, {"deviceName": "runner-data"}],
              "metadata": {"items": [{"key": k, "value": v} for k, v in metadata]}}
        cloud = GcloudRecorder()
        self.listed = []

        def rows(scope):
            self.listed.append(scope.name)
            found = (registrations or {}).get(scope.name, [])
            if isinstance(found, Exception):
                raise found
            return [{"id": i, "name": name} for i, name in enumerate(found, 1)]

        describe = [SimpleNamespace(returncode=0, stdout=json.dumps(vm)),
                    SimpleNamespace(returncode=0, stdout=json.dumps({"type": args.disk_type, "users": ["x"]}))]
        with unittest.mock.patch("lib.runner_manager.subprocess.run", side_effect=describe), \
                unittest.mock.patch("lib.runner_manager.registered_runner_rows", side_effect=rows), \
                unittest.mock.patch("lib.runner_manager.gcloud", side_effect=cloud):
            try:
                manager.apply_startup(args, "us-central1-a", "#!/bin/bash\n")
                return None, cloud
            except RuntimeError as exc:
                return exc, cloud

    def instance_mutations(self, cloud):
        return [c[:3] for c in cloud.calls if c[:2] == ["compute", "instances"]]

    def test_vm_labeled_for_another_target_is_refused_before_any_change(self):
        for labels, options in (({"spot-target": target_label_value("someone/else")}, ["--target", REPO]),
                                ({"spot-target": target_label_value("swack-tools")}, ["--target", REPO]),
                                ({"spot-target": target_label_value(REPO)}, [])):
            error, cloud = self.adopt(labels, *options)
            with self.subTest(labels=labels, options=options):
                self.assertIsInstance(error, RuntimeError)
                self.assertIn("owned by another target", str(error))
                self.assertEqual(self.instance_mutations(cloud), [])  # nothing touched, nothing registered

    def test_unlabeled_vm_is_labeled_first_when_explicitly_targeted(self):
        error, cloud = self.adopt({}, "--target", REPO)
        self.assertIsNone(error)
        mutations = self.instance_mutations(cloud)
        self.assertEqual(mutations[0], ["compute", "instances", "add-labels"])
        self.assertEqual(cloud.calls[[c[:2] for c in cloud.calls].index(["compute", "instances"])][-1],
                         f"--labels=spot-target={target_label_value(REPO)}")
        self.assertLess(mutations.index(["compute", "instances", "add-labels"]),
                        mutations.index(["compute", "instances", "add-metadata"]))  # before the startup script
        self.assertLess(mutations.index(["compute", "instances", "add-metadata"]),
                        mutations.index(["compute", "instances", "reset"]))

    def test_matching_label_and_legacy_default_org_hosts_are_left_alone(self):
        for labels, options in (({"spot-target": target_label_value(REPO)}, ["--target", REPO]), ({}, [])):
            error, cloud = self.adopt(labels, *options)
            with self.subTest(labels=labels):
                self.assertIsNone(error)
                target_labels = [c for c in cloud.named("compute", "instances", "add-labels")
                                 if c[-1].startswith("--labels=spot-target=")]
                self.assertEqual(target_labels, [])

    def test_unlabeled_vm_with_runners_registered_elsewhere_is_never_relabeled(self):
        """Codex round 14 P1: persisted registrations to another scope refuse adoption before any change."""
        mine = ["oxidex-oxidex-runners-1-1", "oxidex-oxidex-runners-1-2"]
        for registrations, metadata, scope in (
                ({"swack-tools": mine}, (), "swack-tools"),  # the default org owns unlabeled hosts
                ({"someone/else": ["gha-oxidex-runners-1-1"]},
                 (("spot-runner-registration-target", "someone/else"),), "someone/else")):  # recorded scope
            error, cloud = self.adopt({}, "--target", REPO, registrations=registrations, metadata=metadata)
            with self.subTest(scope=scope):
                self.assertIsInstance(error, RuntimeError)
                self.assertIn(f"registered to another target ({scope}:", str(error))
                self.assertIn("drain the host", str(error))
                self.assertEqual(self.instance_mutations(cloud), [])  # no label, no metadata, no reset
                self.assertIn(REPO, self.listed)
                self.assertIn("swack-tools", self.listed)

    def test_registrations_under_the_new_target_or_for_other_vms_do_not_block_adoption(self):
        error, cloud = self.adopt({}, "--target", REPO, registrations={
            REPO: ["oxidex-oxidex-runners-1-1"],  # already this target's
            "swack-tools": ["oxidex-oxidex-runners-10-1", "oxidex-runners-1", "oxidex-oxidex-runners-1-x"]})
        self.assertIsNone(error)
        self.assertEqual(self.instance_mutations(cloud)[0], ["compute", "instances", "add-labels"])

    def test_unverifiable_registration_scope_refuses_adoption(self):
        for failing in (REPO, "swack-tools"):
            error, cloud = self.adopt({}, "--target", REPO,
                                      registrations={failing: RuntimeError("command failed (1): gh api ... 403")})
            with self.subTest(failing=failing):
                self.assertIsInstance(error, RuntimeError)
                self.assertIn("Cannot verify which GitHub scope", str(error))
                self.assertEqual(self.instance_mutations(cloud), [])
        error, cloud = self.adopt({}, "--target", REPO, metadata=(("spot-runner-registration-target", "bad scope!"),))
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("Cannot verify which GitHub scope", str(error))
        self.assertEqual(self.instance_mutations(cloud), [])

    def test_main_checks_registrations_before_starting_a_retained_host(self):
        vm = {"status": "TERMINATED", "machineType": "zones/z/machineTypes/c4d-standard-32",
              "scheduling": {"provisioningModel": "SPOT"}, "labels": {}, "metadata": {"items": []}, "disks": []}
        argv = ["manager", "--max-cost", "0.3", "--instance-type", "c4d-standard-32", "--cpu", "32", "--ram", "128",
                "--target", REPO, "--instance-name", "oxidex-runners-1", "--zone", "us-central1-a",
                "--existing-instance", "--apply"]
        observed = runner_fleet.GceVm("oxidex-runners-1", "us-central1-a", "TERMINATED", "c4d-standard-32", True,
                                      instance_id="1")
        saved = (manager.PROJECT, manager.ORG)
        try:
            with unittest.mock.patch("sys.argv", argv), unittest.mock.patch.object(manager, "configure_logging"), \
                    unittest.mock.patch.object(manager, "load_env"), \
                    unittest.mock.patch.object(manager, "observed_deployment_vm", return_value=observed), \
                    unittest.mock.patch.object(manager, "verify_price_and_capacity",
                                               return_value=manager.Decimal("0.2")), \
                    unittest.mock.patch("lib.runner_manager.subprocess.run",
                               return_value=SimpleNamespace(returncode=0, stdout=json.dumps(vm))), \
                    unittest.mock.patch.object(manager, "registered_runner_rows",
                                      side_effect=lambda scope: [{"id": 1, "name": "oxidex-oxidex-runners-1-1"}]
                                      if scope.name == "swack-tools" else []), \
                    unittest.mock.patch.object(manager, "start_retained_strict_host") as start, \
                    unittest.mock.patch.object(manager, "generate_registration_token") as token, \
                    unittest.mock.patch("lib.runner_manager.gcloud") as cloud:
                with self.assertRaisesRegex(RuntimeError, "registered to another target"):
                    manager.main()
        finally:
            manager.PROJECT, manager.ORG = saved
        start.assert_not_called()
        token.assert_not_called()
        cloud.assert_not_called()

    def test_unlabeled_vm_needs_an_explicit_target_outside_the_default_org(self):
        error, cloud = self.adopt({}, "--org", "another-org")
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("pass --target", str(error))
        self.assertEqual(self.instance_mutations(cloud), [])


class LivePatchMetadataTests(unittest.TestCase):
    def run_live(self, receipt, script_output, pending="", receipt_text=None, observed=None):
        args = args_for(TARGET, "--patch-live")
        vm = runner_fleet.GceVm(args.instance_name, "us-central1-a", "RUNNING", "c4d-standard-32", True,
                                instance_id="1", cpu_platform="AMD Turin", patch_pending_sha256=pending,
                                last_start_timestamp=BOOT_TS)

        def respond(command):
            if command[:2] == ["compute", "ssh"] and "state.json" in command[-1]:
                return receipt_output(receipt) if receipt_text is None else receipt_text
            if command[:2] == ["compute", "ssh"]:
                return script_output
            return ""

        cloud = GcloudRecorder(respond)
        listing = [[vm]] + [[later] for later in (observed or [vm] * 3)]
        with unittest.mock.patch("lib.runner_manager.list_gcloud_vms", side_effect=listing), \
                unittest.mock.patch("lib.runner_manager.gcloud", side_effect=cloud), \
                unittest.mock.patch("lib.runner_manager.sync_runner_labels"), \
                    unittest.mock.patch("lib.runner_manager.runner_label_sync_plan"):
            try:
                result = manager.apply_live_patch(args, "us-central1-a")
            except RuntimeError as exc:
                result = exc
        return result, cloud

    def test_metadata_is_updated_only_after_the_host_confirms(self):
        for output in ("something went wrong\n", f"LIVE_PATCH_APPLIED:{OLD.patch_sha256}\n"):  # another patch's marker
            result, cloud = self.run_live(OLD, output)
            self.assertIsInstance(result, RuntimeError)
            # Only the attempt is announced (pending) before the script; nothing applied, labeled, or cleared.
            self.assertEqual(live_sequence(cloud), ["read-receipt", "write-pending", "live-script"])
        result, cloud = self.run_live(OLD, f"ok\nLIVE_PATCH_APPLIED:{TARGET.patch_sha256}\n")
        self.assertEqual(result, "hot_patch")
        self.assertEqual(live_sequence(cloud), ["read-receipt", "write-pending", "live-script", "write-applied", "label",
                                                "clear-pending"])
        [pending] = pending_writes(cloud)
        self.assertEqual(plain_metadata(pending), {"spot-runner-patch-pending-sha256": TARGET.patch_sha256})
        self.assertEqual(cloud.files[0], {"spot-runner-patch-pending": canonical_json(TARGET)})
        [applied] = applied_writes(cloud)
        self.assertEqual(plain_metadata(applied)["spot-runner-patch-sha256"], TARGET.patch_sha256)
        self.assertEqual(cloud.files[-1]["spot-runner-patch"], canonical_json(TARGET))
        self.assertEqual(cloud.calls[-1][-1], "--keys=spot-runner-patch-pending-sha256,spot-runner-patch-pending")

    def test_failed_live_patch_leaves_the_host_pending_so_reverting_is_never_noop(self):
        """Codex round 15 P2: A recorded, live B fails part-way. GCE must show pending B, so a preview for A is
        unknown (not noop); a later successful B clears it."""
        host_a = gce_item("oxidex-runners-1", sha=OLD.patch_sha256, manifest=canonical_json(OLD))

        def preview(item, desired):
            [host] = compute_api.runner_host_records([item])
            [got] = pc.classify_hosts([host], desired, pc.UnavailableHotPatchExecutor())
            return got

        self.assertEqual(preview(host_a, OLD).classification, "noop")
        result, cloud = self.run_live(OLD, "docker build failed\n")
        self.assertIsInstance(result, RuntimeError)
        failed = apply_metadata_writes(host_a, cloud)
        got = preview(failed, OLD)
        self.assertEqual(got.classification, "unknown")
        self.assertIn(f"pending, unconfirmed: provisioning requested patch {TARGET.patch_sha256[:12]}", got.detail)
        self.assertEqual(preview(failed, TARGET).classification, "unknown")
        [vm] = [runner_fleet.GceVm("oxidex-runners-1", "us-central1-a", "RUNNING", "c4d-standard-32", True,
                                   **runner_fleet.patch_record_from_gce_item(failed))]
        result, cloud = self.run_live(OLD, f"LIVE_PATCH_APPLIED:{TARGET.patch_sha256}\n",
                                      pending=vm.patch_pending_sha256)
        self.assertEqual(result, "hot_patch")
        done = apply_metadata_writes(failed, cloud)
        self.assertEqual(runner_fleet.patch_record_from_gce_item(done)["patch_pending_sha256"], "")
        self.assertEqual(preview(done, TARGET).classification, "noop")
        self.assertEqual(preview(done, OLD).classification, "hot_patch")

    def test_oversized_pending_manifest_is_replaced_by_a_placeholder(self):
        big = validate_patch_dict({**BASE, "host": {"files": [{"path": "/etc/big", "content": "x" * 70_000}]}})
        args = args_for(big)
        cloud = GcloudRecorder()
        with unittest.mock.patch("lib.runner_manager.gcloud", side_effect=cloud):
            manager.record_pending_patch_metadata(args, "us-central1-a", big)
        [write] = cloud.calls
        self.assertEqual(plain_metadata(write), {"spot-runner-patch-pending-sha256": big.patch_sha256})
        self.assertEqual(json.loads(cloud.files[0]["spot-runner-patch-pending"])["patch_sha256"], big.patch_sha256)

    def test_label_sync_preflight_failure_stops_before_the_host_changes(self):
        args = args_for(TARGET, "--patch-live")
        vm = runner_fleet.GceVm(args.instance_name, "us-central1-a", "RUNNING", "c4d-standard-32", True,
                                instance_id="1", cpu_platform="AMD Turin")
        cloud = GcloudRecorder(lambda c: receipt_output(OLD) if c[:2] == ["compute", "ssh"] else "")
        with unittest.mock.patch("lib.runner_manager.list_gcloud_vms", return_value=[vm]), \
                unittest.mock.patch("lib.runner_manager.gcloud", side_effect=cloud), \
                unittest.mock.patch("lib.runner_manager.runner_label_sync_plan",
                           side_effect=RuntimeError("Runner group 10 is not enabled for swack-tools/oxidex")), \
                unittest.mock.patch("lib.runner_manager.sync_runner_labels") as sync:
            with self.assertRaisesRegex(RuntimeError, "Runner group"):
                manager.apply_live_patch(args, "us-central1-a")
        self.assertEqual(live_sequence(cloud), ["read-receipt"])  # no pending write, no script
        sync.assert_not_called()
        self.assertIs(args.deployment_vm, vm)  # strict pools verify cached identities against the observed VM

    def test_label_sync_plan_checks_without_writing(self):
        args = args_for(TARGET, "--patch-live")
        rows = [{"id": n, "name": f"oxidex-{args.instance_name}-{n}", "labels": []} for n in range(1, args.runner_count + 1)]
        commands = []

        def run(cmd, **_kw):
            commands.append(cmd)
            return "\n".join(json.dumps(r) for r in rows)

        with unittest.mock.patch("lib.runner_manager.run", side_effect=run):
            manager.runner_label_sync_plan(args)
            self.assertTrue(all("PUT" not in c for c in commands))
            rows.pop()
            with self.assertRaisesRegex(RuntimeError, "did not list all expected runners"):
                manager.runner_label_sync_plan(args)

    def test_noop_backfills_from_the_hosts_own_receipt(self):
        result, cloud = self.run_live(TARGET, "")
        self.assertEqual(result, "noop")
        self.assertEqual(len([c for c in cloud.calls if c[:2] == ["compute", "ssh"]]), 1)  # no script was run
        self.assertEqual(plain_metadata(cloud.named("compute", "instances", "add-metadata")[0])["spot-runner-patch-sha256"],
                         TARGET.patch_sha256)

    def test_pending_host_is_never_confirmed_by_its_receipt_alone(self):
        """Codex round 13 P2: a matching receipt on an unconfirmed bootstrap re-applies live before recording."""
        result, cloud = self.run_live(TARGET, "partial output\n", pending=TARGET.patch_sha256)
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual(live_sequence(cloud), ["read-receipt", "write-pending", "live-script"])  # the script ran
        result, cloud = self.run_live(TARGET, f"LIVE_PATCH_APPLIED:{TARGET.patch_sha256}\n", pending=TARGET.patch_sha256)
        self.assertEqual(result, "hot_patch")
        self.assertEqual(live_sequence(cloud), ["read-receipt", "write-pending", "live-script", "write-applied", "label",
                                                "clear-pending"])  # pending cleared last
        self.assertEqual(cloud.calls[-1][-1], "--keys=spot-runner-patch-pending-sha256,spot-runner-patch-pending")

    def test_only_a_completion_receipt_from_this_boot_skips_the_live_apply(self):
        """Codex round 14 P2: failed-then-noop. A pending marker, a legacy receipt, or a receipt from an earlier
        boot never counts as proof; the CLI re-applies live instead of recording from it."""
        for name, text in (("pending attempt", receipt_output(TARGET, pending=TARGET)),
                           ("failed attempt after an older patch", receipt_output(OLD, pending=TARGET)),
                           ("legacy receipt", receipt_output(TARGET, legacy=True)),
                           ("rebooted", receipt_output(TARGET, receipt_boot="1" * 8 + BOOT_ID[8:]))):
            with self.subTest(name):
                result, cloud = self.run_live(None, "build failed\n", receipt_text=text)
                self.assertIsInstance(result, RuntimeError)  # the live script ran and did not confirm
                self.assertEqual(live_sequence(cloud), ["read-receipt", "write-pending", "live-script"])
        result, cloud = self.run_live(TARGET, "")  # a completion receipt from this boot
        self.assertEqual(result, "noop")
        self.assertEqual(live_sequence(cloud), ["read-receipt", "write-applied", "label"])  # no script, no pending
        self.assertEqual(plain_metadata(applied_writes(cloud)[0])["spot-runner-patch-boot"], BOOT_TS)

    def test_live_patch_binds_the_record_to_the_boot_and_refuses_a_restart(self):
        result, cloud = self.run_live(OLD, f"LIVE_PATCH_APPLIED:{TARGET.patch_sha256}\n")
        self.assertEqual(result, "hot_patch")
        self.assertEqual(plain_metadata(applied_writes(cloud)[0])["spot-runner-patch-boot"], BOOT_TS)
        args = args_for(TARGET)
        rebooted = runner_fleet.GceVm(args.instance_name, "us-central1-a", "RUNNING", "c4d-standard-32", True,
                                      instance_id="1", cpu_platform="AMD Turin",
                                      last_start_timestamp="2026-10-02T00:00:00.000-07:00")
        result, cloud = self.run_live(OLD, f"LIVE_PATCH_APPLIED:{TARGET.patch_sha256}\n", observed=[rebooted])
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("restarted while", str(result))
        self.assertEqual(live_sequence(cloud), ["read-receipt", "write-pending", "live-script"])  # stays pending
        with self.assertRaisesRegex(RuntimeError, "without the boot"):
            manager.record_patch_metadata(args, "us-central1-a", TARGET, boot="")

    def test_refused_classifications_record_nothing(self):
        for receipt in (ARM, MOUNTS):
            # The target has no mounts and x86_64, so an arm host needs a new VM and a mounts host recreated containers.
            result, cloud = self.run_live(receipt, f"LIVE_PATCH_APPLIED:{TARGET.patch_sha256}")
            self.assertIsInstance(result, RuntimeError, receipt.patch_sha256)
            self.assertEqual(live_sequence(cloud), ["read-receipt"])  # no pending write, no script, no record

    def test_dashboard_executor_path_records_only_after_confirmation(self):
        ex = pc.GcloudHotPatchExecutor("proj", which=lambda _n: "/usr/bin/gcloud")
        host = {"name": "oxidex-runners-1", "zone": "us-central1-a", "machine_type": "c4d-standard-32",
                "runner_prefix": "oxidex", "runners_count": 3, "runner_label": "oxidex-spot-ci",
                "runner_region": "us-central1", "patch_target": REPO,
                "registration_target": "swack-tools", "runner_group_id": 10}
        vm = runner_fleet.GceVm("oxidex-runners-1", "us-central1-a", "RUNNING", "c4d-standard-32", True,
                                last_start_timestamp=BOOT_TS)
        for output, expect_metadata in (("no marker", False), (f"LIVE_PATCH_APPLIED:{TARGET.patch_sha256}", True)):
            def respond(command, _output=output):
                if command[:2] == ["compute", "ssh"] and "state.json" in command[-1]:
                    return receipt_output(OLD)
                return _output if command[:2] == ["compute", "ssh"] else ""

            cloud = GcloudRecorder(respond)
            with unittest.mock.patch("lib.runner_manager.list_gcloud_vms", return_value=[vm]), \
                    unittest.mock.patch("lib.runner_manager.gcloud", side_effect=cloud), \
                    unittest.mock.patch("lib.runner_manager.sync_runner_labels"), \
                    unittest.mock.patch("lib.runner_manager.runner_label_sync_plan"):
                if expect_metadata:
                    self.assertEqual(ex.apply(host, TARGET), {"result": "applied"})
                else:
                    with self.assertRaises(RuntimeError):
                        ex.apply(host, TARGET)
            self.assertEqual(live_sequence(cloud),
                             ["read-receipt", "write-pending", "live-script"]
                             + (["write-applied", "label", "clear-pending"] if expect_metadata else []), output)


def gce_item(name, *, sha=None, manifest=None, target=REPO, label=True, status="RUNNING", extra=(), pending=None,
             pending_manifest=None, boot=BOOT_TS, last_start=BOOT_TS):
    items = [{"key": "startup-script", "value": f"#!/bin/bash\nTOKEN={TOKEN}"}, *extra]
    if pending is not None:
        items.append({"key": "spot-runner-patch-pending-sha256", "value": pending})
        if sha is None:
            items.append({"key": "spot-runner-patch-target", "value": target})
    if pending_manifest is not None:
        items.append({"key": "spot-runner-patch-pending", "value": pending_manifest})
    if sha is not None:
        items.append({"key": "spot-runner-patch-sha256", "value": sha})
        if boot is not None:
            items.append({"key": "spot-runner-patch-boot", "value": boot})
        items.append({"key": "spot-runner-patch-target", "value": target})
        items.append({"key": "spot-runner-prefix", "value": "oxidex"})
        items.append({"key": "spot-runner-count", "value": "3"})
        items.append({"key": "spot-runner-label", "value": "oxidex-spot-ci"})
        items.append({"key": "spot-runner-region", "value": "us-central1"})
        items.append({"key": "spot-runner-heavy-pool", "value": "3"})
        items.append({"key": "spot-runner-registration-target", "value": "swack-tools"})
        items.append({"key": "spot-runner-group-id", "value": "10"})
    if manifest is not None:
        items.append({"key": "spot-runner-patch", "value": manifest})
    labels = {"spot-target": target_label_value(target)} if label else {}
    item = {"name": name, "zone": "projects/p/zones/us-central1-a", "status": status,
            "machineType": "projects/p/zones/us-central1-a/machineTypes/c4d-standard-32",
            "labels": labels, "metadata": {"items": items}}
    if last_start is not None:
        item["lastStartTimestamp"] = last_start
    return item


class InventoryTests(unittest.TestCase):
    def test_patch_record_parse(self):
        full = runner_fleet.patch_record_from_gce_item(gce_item("a", sha=TARGET.patch_sha256,
                                                                manifest=canonical_json(TARGET)))
        self.assertEqual(full, {"patch_sha256": TARGET.patch_sha256, "patch_manifest_json": canonical_json(TARGET),
                                "patch_target": REPO, "patch_pending_sha256": "", "patch_boot": BOOT_TS})
        self.assertEqual(runner_fleet.patch_record_from_gce_item(gce_item("b", sha="none", manifest="{}"))["patch_manifest_json"], "")
        for bad_sha in ("abc", "Z" * 64, "", " "):
            self.assertEqual(runner_fleet.patch_record_from_gce_item(gce_item("c", sha=bad_sha))["patch_sha256"], "", bad_sha)
        oversized = runner_fleet.patch_record_from_gce_item(gce_item("d", sha=TARGET.patch_sha256, manifest="x" * 70_000))
        self.assertEqual(oversized["patch_manifest_json"], "")
        self.assertEqual(runner_fleet.patch_record_from_gce_item(gce_item("e")),
                         {"patch_sha256": "", "patch_manifest_json": "", "patch_target": "", "patch_pending_sha256": "",
                          "patch_boot": ""})

    def test_inventory_carries_the_recorded_boot_and_last_start(self):
        item = gce_item("a", sha=TARGET.patch_sha256, manifest=canonical_json(TARGET))
        [host] = compute_api.runner_host_records([item])
        self.assertEqual((host["patch_boot"], host["last_start_timestamp"]), (BOOT_TS, BOOT_TS))
        bad = gce_item("b", sha=TARGET.patch_sha256, boot="not a timestamp,=", last_start="x" * 50)
        [host] = compute_api.runner_host_records([bad])
        self.assertEqual((host["patch_boot"], host["last_start_timestamp"]), ("", ""))

        def command_json(args):
            if args[1:4] == ["compute", "instances", "list"]:
                return [{**item, "scheduling": {"provisioningModel": "SPOT"}}]
            return [] if args[1:4] == ["compute", "disks", "list"] else {}

        with unittest.mock.patch("lib.runner_fleet._command_json", side_effect=command_json):
            [vm] = runner_fleet.list_gcloud_vms("p")
        self.assertEqual((vm.patch_boot, vm.last_start_timestamp), (BOOT_TS, BOOT_TS))

    def test_pending_record_parse(self):
        """Codex round 13 P2: the pending keys survive inventory parsing; a damaged one still reads as pending."""
        fresh = runner_fleet.patch_record_from_gce_item(gce_item("f", pending=TARGET.patch_sha256,
                                                                 pending_manifest=canonical_json(TARGET)))
        self.assertEqual(fresh, {"patch_sha256": "", "patch_manifest_json": "", "patch_target": REPO,
                                 "patch_pending_sha256": TARGET.patch_sha256, "patch_boot": ""})
        self.assertEqual(runner_fleet.patch_record_from_gce_item(gce_item("g", pending="NONE"))["patch_pending_sha256"],
                         "none")
        for bad in ("abc", "", "Z" * 64):
            self.assertEqual(runner_fleet.patch_record_from_gce_item(gce_item("h", pending=bad))["patch_pending_sha256"],
                             "invalid", bad)
        manifest_only = gce_item("i", pending_manifest=canonical_json(TARGET))
        self.assertEqual(runner_fleet.patch_record_from_gce_item(manifest_only)["patch_pending_sha256"], "invalid")
        unlabeled = gce_item("j", pending=TARGET.patch_sha256, label=False)
        self.assertEqual([h["name"] for h in compute_api.runner_host_records([unlabeled])], ["j"])
        self.assertEqual(runner_fleet.patch_record_from_gce_item({"metadata": "garbage"})["patch_sha256"], "")

    def test_list_gcloud_vms_carries_the_recorded_patch(self):
        rows = [gce_item("oxidex-runners-1", sha=TARGET.patch_sha256, manifest=canonical_json(TARGET)),
                gce_item("oxidex-runners-2")]
        rows[0]["scheduling"] = {"provisioningModel": "SPOT"}

        def command_json(args):
            if args[1:4] == ["compute", "instances", "list"]:
                return rows
            return [] if args[1:4] == ["compute", "disks", "list"] else {}

        with unittest.mock.patch("lib.runner_fleet._command_json", side_effect=command_json):
            vms = runner_fleet.list_gcloud_vms("p")
        self.assertEqual((vms[0].patch_sha256, vms[0].patch_target), (TARGET.patch_sha256, REPO))
        self.assertEqual(json.loads(vms[0].patch_manifest_json), json.loads(canonical_json(TARGET)))
        self.assertEqual((vms[1].patch_sha256, vms[1].patch_manifest_json), ("", ""))

    def test_compute_api_listing_and_host_records(self):
        pages = [
            {"items": {"zones/a": {"instances": [gce_item("h1", sha=OLD.patch_sha256, manifest=canonical_json(OLD))]},
                       "zones/b": {"warning": {}}}, "nextPageToken": "t2"},
            {"items": {"zones/c": {"instances": [gce_item("legacy"),
                                                  {**gce_item("control", label=False), "labels": {"spot-role": "control"}}]}},
             "unreachables": []},
        ]
        seen = []

        class FakeApi:
            async def get_json(self, path, query=None):
                seen.append((path, dict(query or {})))
                return pages[len(seen) - 1]

        items, unreachables = asyncio.run(compute_api.list_instances(FakeApi(), "proj"))
        self.assertEqual([i["name"] for i in items], ["h1", "legacy", "control"])
        self.assertEqual(seen[1][1]["pageToken"], "t2")
        self.assertIn("metadata/items", seen[0][1]["fields"])
        hosts = compute_api.runner_host_records(items)
        self.assertEqual([h["name"] for h in hosts], ["h1", "legacy"])  # the control VM is not a runner host
        self.assertEqual((hosts[0]["zone"], hosts[0]["machine_type"], hosts[0]["runner_prefix"], hosts[0]["runners_count"],
                          hosts[0]["runner_label"], hosts[0]["runner_region"], hosts[0]["heavy_runner_pool"],
                          hosts[0]["registration_target"], hosts[0]["runner_group_id"]),
                         ("us-central1-a", "c4d-standard-32", "oxidex", 3, "oxidex-spot-ci", "us-central1", 3,
                          "swack-tools", 10))
        self.assertNotIn("runner_label", hosts[1])  # legacy host: refused by the executor, never defaulted
        bad = gce_item("bad", sha=OLD.patch_sha256, extra=())
        for entry in bad["metadata"]["items"]:
            if entry["key"] == "spot-runner-label":
                entry["value"] = "bad label,with=separators"
            if entry["key"] == "spot-runner-heavy-pool":
                entry["value"] = "9"
        parsed = runner_fleet.runner_deployment_from_gce_item(bad)
        self.assertNotIn("runner_label", parsed)
        self.assertNotIn("heavy_runner_pool", parsed)
        self.assertNotIn(TOKEN, json.dumps(hosts))  # startup scripts never reach host records
        self.assertEqual(hosts[1]["patch_sha256"], "")


class PreviewFromRecordedPatchTests(unittest.TestCase):
    def test_preview_classifies_real_hosts_and_explains_unknowns(self):
        tampered = json.loads(canonical_json(OLD))
        tampered["host"]["apt_packages"] = ["curl"]
        items = [
            gce_item("old", sha=OLD.patch_sha256, manifest=canonical_json(OLD)),
            gce_item("same", sha=TARGET.patch_sha256, manifest=canonical_json(TARGET)),
            gce_item("mounts", sha=MOUNTS.patch_sha256, manifest=canonical_json(MOUNTS)),
            gce_item("arm", sha=ARM.patch_sha256, manifest=canonical_json(ARM)),
            gce_item("unpatched", sha="none"),
            gce_item("sha-only-same", sha=TARGET.patch_sha256),
            gce_item("sha-only-other", sha=OLD.patch_sha256),
            gce_item("tampered", sha=OLD.patch_sha256, manifest=json.dumps(tampered)),
            gce_item("legacy"),
            gce_item("stopped", sha=OLD.patch_sha256, manifest=canonical_json(OLD), status="TERMINATED"),
            gce_item("elsewhere", sha=OLD.patch_sha256, manifest=canonical_json(OLD), target="someone/else"),
        ]
        hosts = compute_api.runner_host_records(items)
        console = pc.PatchConsole("swack-tools/spot-runners-config", token="t", host_source=lambda: hosts)
        with unittest.mock.patch.object(pc.PatchConsole, "effective_patch",
                               return_value=(TARGET, {"draft": False, "head_sha": "a" * 40})):
            result = console.preview(REPO, instances=[{"name": "ignored-state-record"}])
        got = {h["name"]: (h["classification"], h["detail"]) for h in result["hosts"]}
        self.assertEqual({n: c for n, (c, _) in got.items()}, {
            "old": "hot_patch", "same": "noop", "mounts": "recreate_containers", "arm": "replace_vm",
            "unpatched": "hot_patch", "sha-only-same": "noop", "sha-only-other": "unknown", "tampered": "unknown",
            "legacy": "unknown"})
        self.assertIn("provisioned before patch recording", got["legacy"][1])
        self.assertIn("does not match", got["tampered"][1])
        self.assertIn("only the sha256", got["sha-only-other"][1])
        self.assertEqual(got["old"][1], "recorded in instance metadata")
        self.assertFalse(result["hot_patch_allowed"])  # no executor on the control VM, and some hosts block it

    def test_pending_hosts_preview_as_unknown_never_noop(self):
        """Codex round 13 P2: a requested, unconfirmed patch is unknown, even when the applied keys or a live
        receipt match the target."""
        items = [
            gce_item("bootstrapping", pending=TARGET.patch_sha256, pending_manifest=canonical_json(TARGET)),
            gce_item("failed-rebootstrap", sha=TARGET.patch_sha256, manifest=canonical_json(TARGET),
                     pending=TARGET.patch_sha256),
            gce_item("pending-none", pending="none"),
            gce_item("pending-damaged", pending="garbage"),
            gce_item("confirmed", sha=TARGET.patch_sha256, manifest=canonical_json(TARGET)),
        ]
        hosts = compute_api.runner_host_records(items)

        class ReceiptSaysTarget:
            available = True
            reason = ""

            def read_manifest(self, _host):
                return TARGET.to_canonical_dict()

        for executor in (pc.UnavailableHotPatchExecutor(), ReceiptSaysTarget()):
            with self.subTest(executor=type(executor).__name__):
                got = {h.host["name"]: h for h in pc.classify_hosts(hosts, TARGET, executor)}
                for name in ("bootstrapping", "failed-rebootstrap", "pending-none", "pending-damaged"):
                    self.assertEqual(got[name].classification, "unknown", name)
                    self.assertIn("pending, unconfirmed", got[name].detail)
                    self.assertIsNone(got[name].current_patch_sha256)
                self.assertIn(f"patch {TARGET.patch_sha256[:12]}", got["bootstrapping"].detail)
                self.assertIn("requested no patch", got["pending-none"].detail)
                self.assertIn("unreadable patch record", got["pending-damaged"].detail)
                self.assertEqual(got["confirmed"].classification, "noop")

    def test_applied_record_from_another_boot_previews_as_unknown(self):
        """Codex round 14 P2: a reboot since confirmation, or no recorded boot, makes the applied record stale."""
        items = [
            gce_item("same-boot", sha=TARGET.patch_sha256, manifest=canonical_json(TARGET)),
            gce_item("rebooted", sha=TARGET.patch_sha256, manifest=canonical_json(TARGET),
                     last_start="2026-10-02T00:00:00.000-07:00"),
            gce_item("unbound", sha=TARGET.patch_sha256, manifest=canonical_json(TARGET), boot=None),
            gce_item("no-last-start", sha=TARGET.patch_sha256, manifest=canonical_json(TARGET), last_start=None),
            gce_item("unpatched-rebooted", sha="none", last_start="2026-10-02T00:00:00.000-07:00"),
        ]
        got = {h.host["name"]: h for h in pc.classify_hosts(compute_api.runner_host_records(items), TARGET,
                                                             pc.UnavailableHotPatchExecutor())}
        self.assertEqual(got["same-boot"].classification, "noop")
        for name in ("rebooted", "unbound", "no-last-start", "unpatched-rebooted"):
            self.assertEqual(got[name].classification, "unknown", name)
        self.assertIn("rebooted since the patch was confirmed", got["rebooted"].detail)
        self.assertIn("reprovision or re-run --patch-live", got["rebooted"].detail)
        self.assertIn("no boot binding", got["unbound"].detail)
        self.assertIn("lastStartTimestamp is unknown", got["no-last-start"].detail)

    def test_executor_receipts_count_as_proof_only_when_complete(self):
        """Codex round 14 P2: with an executor, a non-proof receipt naming the target is hot_patch, never noop."""
        from lib.runner_patch import parse_patch_receipt

        receipts = {"complete": receipt_output(TARGET), "pending": receipt_output(TARGET, pending=TARGET),
                    "legacy": receipt_output(TARGET, legacy=True),
                    "rebooted": receipt_output(TARGET, receipt_boot="1" * 8 + BOOT_ID[8:]),
                    "older-with-pending": receipt_output(OLD, pending=TARGET), "none": receipt_output()}

        class Receipts:
            available = True
            reason = ""

            def read_manifest(self, host):
                return parse_patch_receipt(receipts[host["name"]])

        hosts = compute_api.runner_host_records([gce_item(n, sha=OLD.patch_sha256) for n in receipts])
        got = {h.host["name"]: h for h in pc.classify_hosts(hosts, TARGET, Receipts())}
        self.assertEqual({n: h.classification for n, h in got.items()},
                         {"complete": "noop", "pending": "hot_patch", "legacy": "hot_patch", "rebooted": "hot_patch",
                          "older-with-pending": "hot_patch", "none": "hot_patch"})
        self.assertIn("has not completed", got["pending"].detail)
        self.assertIn("predates completion receipts", got["legacy"].detail)
        self.assertIn("rebooted since the completion receipt", got["rebooted"].detail)
        self.assertIn("re-applies it", got["rebooted"].detail)
        self.assertIn("has not completed", got["older-with-pending"].detail)

    def test_host_source_failure_is_a_clear_error(self):
        def broken():
            raise RuntimeError("compute.instances.list denied")

        console = pc.PatchConsole("swack-tools/spot-runners-config", token="t", host_source=broken)
        with unittest.mock.patch.object(pc.PatchConsole, "effective_patch", return_value=(TARGET, {"draft": False})):
            with self.assertRaisesRegex(pc.GitHubRestError, "Could not list runner hosts"):
                console.preview(REPO, [])
        self.assertIsNone(pc.host_source_from_env("p", {}))
        self.assertIsNotNone(pc.host_source_from_env("p", {"DASHBOARD_FLEET_INVENTORY": "gce"}))

    def test_control_vm_enables_the_gce_inventory(self):
        script = (Path(__file__).resolve().parents[1] / "cloud-init" / "control-vm-startup.sh").read_text()
        self.assertIn("printf 'DASHBOARD_FLEET_INVENTORY=gce\\n'", script)


def unrecorded_item(name, **extra):
    """A GCE instance with neither the spot-target label nor any patch metadata (pre-recording fleet)."""
    item = gce_item(name, label=False)
    item.update(extra)
    return item


class UnrecordedRunnerHostTests(unittest.TestCase):
    """PR #44 P2: runner VMs created before target labels and patch recording are listed, as unknown."""

    def test_legacy_runner_hosts_are_listed_by_name_prefix(self):
        items = [unrecorded_item("oxidex-runners-asiaeast2-b-1700000000"),
                 unrecorded_item("spot-runners-old"), unrecorded_item("newznab-spot-runners-1"),
                 unrecorded_item("oxidex-spot-runners-2"),
                 # Foreign VMs: other names, a control VM, and a name that only resembles a prefix.
                 unrecorded_item("my-workstation"), unrecorded_item("builder-oxidex-1"),
                 unrecorded_item("spot-control-a", labels={"spot-role": "control"}),
                 unrecorded_item("oxidex-runners-ctl", labels={"spot-role": "control"}),
                 unrecorded_item("oxidex-runnersx"), unrecorded_item("prefix-oxidex-runners-1"),
                 gce_item("labeled-host")]
        hosts = {h["name"]: h for h in compute_api.runner_host_records(items)}
        self.assertEqual(sorted(hosts), sorted(["oxidex-runners-asiaeast2-b-1700000000", "spot-runners-old",
                                                "newznab-spot-runners-1", "oxidex-spot-runners-2", "labeled-host"]))
        self.assertTrue(hosts["spot-runners-old"]["unrecorded_host"])
        self.assertEqual(hosts["spot-runners-old"]["role"], "cicd_runner")
        self.assertNotIn("unrecorded_host", hosts["labeled-host"])
        self.assertNotIn("TOKEN", json.dumps(hosts))  # startup scripts are still dropped

    def test_legacy_host_previews_as_unknown_for_every_repository(self):
        items = [gce_item("old", sha=OLD.patch_sha256, manifest=canonical_json(OLD)),
                 unrecorded_item("oxidex-runners-legacy"), unrecorded_item("foreign-vm")]
        hosts = compute_api.runner_host_records(items)
        for repository in (REPO, "someone/else"):
            self.assertIn("oxidex-runners-legacy", [h["name"] for h in pc.runner_hosts(hosts, repository)])
        console = pc.PatchConsole("swack-tools/spot-runners-config", token="t", host_source=lambda: hosts)
        with unittest.mock.patch.object(pc.PatchConsole, "effective_patch",
                                        return_value=(TARGET, {"draft": False, "head_sha": "a" * 40})):
            result = console.preview(REPO, instances=[])
        got = {h["name"]: h for h in result["hosts"]}
        self.assertEqual(sorted(got), ["old", "oxidex-runners-legacy"])
        self.assertEqual(got["old"]["classification"], "hot_patch")
        legacy = got["oxidex-runners-legacy"]
        self.assertEqual(legacy["classification"], "unknown")
        self.assertIn("provisioned before patch recording; reprovision or run --patch-live", legacy["detail"])
        self.assertFalse(result["hot_patch_allowed"])

    def test_legacy_host_blocks_hot_patch_instead_of_partial_success(self):
        items = [gce_item("old", sha=OLD.patch_sha256, manifest=canonical_json(OLD)),
                 unrecorded_item("oxidex-runners-legacy")]
        hosts = compute_api.runner_host_records(items)

        class Executor:
            available = True
            reason = ""
            applied = []
            reads = []

            def read_manifest(self, host):
                self.reads.append(host["name"])
                return OLD.to_canonical_dict()

            def apply(self, host, patch):
                self.applied.append(host["name"])
                return {"result": "applied"}

        executor = Executor()
        console = pc.PatchConsole("swack-tools/spot-runners-config", token="t", host_source=lambda: hosts,
                                  executor=executor)
        with unittest.mock.patch.object(pc.PatchConsole, "effective_patch",
                                        return_value=(TARGET, {"draft": False, "head_sha": "a" * 40})):
            preview = console.preview(REPO, instances=[])
            self.assertFalse(preview["hot_patch_allowed"])
            with self.assertRaises(pc.ConflictError) as ctx:
                console.hot_patch(REPO, TARGET.patch_sha256, instances=[])
        self.assertEqual(ctx.exception.body["status"], "refused")
        got = {h["name"]: h["classification"] for h in ctx.exception.body["hosts"]}
        self.assertEqual(got, {"old": "hot_patch", "oxidex-runners-legacy": "unknown"})
        self.assertEqual(executor.applied, [])  # nothing was patched, so there is no partial result
        self.assertNotIn("oxidex-runners-legacy", executor.reads)  # an unknown-target host is never read live

    def test_runner_host_prefixes_are_shared_with_price_guard(self):
        import inspect
        from lib import price_guard
        self.assertIn("oxidex-runners-", runner_fleet.RUNNER_HOST_NAME_PREFIXES)
        self.assertNotIn('"oxidex-runners-", "newznab-spot-runners-"', inspect.getsource(price_guard))


BIG = validate_patch_dict({**BASE, "hardware": {"min_vcpu": 64, "min_ram_gib": 256},
                           "container": {"apt_packages": ["ripgrep"]}})
VBMI = validate_patch_dict({**BASE, "hardware": {"isa": ["avx512-vbmi2"]}})


class HostCapacityTests(unittest.TestCase):
    """Codex round 8: hardware.min_vcpu/min_ram_gib (and arch/ISA) are checked against the observed host."""

    def test_capacity_rules(self):
        from lib.runner_patch import host_capacity_problems as check

        violations, unknowns = check(BIG, machine_type="n2-standard-32", vcpu=32, ram_gib=128.0)
        self.assertEqual(len(violations), 2)
        self.assertIn("32 vCPU", violations[0])
        self.assertIn("128 GiB", violations[1])
        self.assertEqual(unknowns, [])
        self.assertEqual(check(BIG, machine_type="n2-highmem-64", vcpu=64, ram_gib=512.0), ([], []))
        self.assertEqual(len(check(BIG, machine_type="n2-standard-32", vcpu=None, ram_gib=None)[1]), 2)
        self.assertIn("arm64", check(ARM, machine_type="n2-standard-32", vcpu=32, ram_gib=128)[0][0])
        self.assertEqual(check(ARM, machine_type="c4a-standard-32", vcpu=32, ram_gib=128), ([], []))
        self.assertEqual(check(VBMI, machine_type="c4d-standard-32", vcpu=32, ram_gib=128, cpu_platform="AMD Turin"),
                         ([], []))
        self.assertIn("does not provide", check(VBMI, machine_type="n2-standard-32", vcpu=32, ram_gib=128,
                                                cpu_platform="Intel Cascade Lake")[0][0])
        self.assertIn("CPU platform is unknown", check(VBMI, machine_type="c4d-standard-32", vcpu=32, ram_gib=128)[1][0])
        self.assertIn("machine type is unknown", check(TARGET, machine_type="", vcpu=32, ram_gib=128)[1][0])

    def run_live(self, vm, patch):
        # The operator's arguments claim a 64 vCPU / 256 GiB host (so select_sizing passes); only the observed VM
        # tells the truth, which is exactly the bypass this check closes.
        args = args_for(patch, "--patch-live", "--cpu", "64", "--ram", "256")
        cloud = GcloudRecorder(lambda c: receipt_output(OLD)
                               if c[:2] == ["compute", "ssh"] and "state.json" in c[-1]
                               else f"LIVE_PATCH_APPLIED:{patch.patch_sha256}")
        with unittest.mock.patch("lib.runner_manager.list_gcloud_vms", return_value=[vm]), \
                unittest.mock.patch("lib.runner_manager.gcloud", side_effect=cloud), \
                unittest.mock.patch("lib.runner_manager.runner_label_sync_plan"), \
                unittest.mock.patch("lib.runner_manager.sync_runner_labels"):
            try:
                return manager.apply_live_patch(args, "us-central1-a"), cloud
            except RuntimeError as exc:
                return exc, cloud

    def test_cli_live_patch_refuses_undersized_or_unmeasured_hosts_before_changing_anything(self):
        base = dict(zone="us-central1-a", status="RUNNING", machine_type="c4d-standard-32", spot=True,
                    instance_id="1", cpu_platform="AMD Turin", last_start_timestamp=BOOT_TS)
        for vcpu, ram in ((32, 128.0), (None, None)):
            vm = runner_fleet.GceVm("oxidex-runners-1", vcpu=vcpu, ram_gib=ram, **base)
            result, cloud = self.run_live(vm, BIG)
            with self.subTest(vcpu=vcpu):
                self.assertIsInstance(result, RuntimeError)
                self.assertIn("cannot be live-applied", str(result))
                self.assertEqual(cloud.calls, [])  # not even the receipt read: nothing touched the host
        big_vm = runner_fleet.GceVm("oxidex-runners-1", vcpu=64, ram_gib=256.0, **base)
        result, cloud = self.run_live(big_vm, BIG)
        self.assertEqual(result, "hot_patch")
        self.assertEqual(live_sequence(cloud), ["read-receipt", "write-pending", "live-script", "write-applied", "label",
                                                "clear-pending"])

    def test_preview_classifies_undersized_hosts_as_replace_vm(self):
        hosts = [
            {**compute_api.runner_host_records([gce_item("small", sha=OLD.patch_sha256, manifest=canonical_json(OLD))])[0],
             "vcpu": 32, "ram_gib": 128.0},
            {**compute_api.runner_host_records([gce_item("large", sha=OLD.patch_sha256, manifest=canonical_json(OLD))])[0],
             "vcpu": 64, "ram_gib": 256.0},
            compute_api.runner_host_records([gce_item("unmeasured", sha=OLD.patch_sha256,
                                                      manifest=canonical_json(OLD))])[0],
        ]
        console = pc.PatchConsole("swack-tools/spot-runners-config", token="t", host_source=lambda: hosts)
        with unittest.mock.patch.object(pc.PatchConsole, "effective_patch", return_value=(BIG, {"draft": False})):
            result = console.preview(REPO, [])
        got = {h["name"]: (h["classification"], h["detail"]) for h in result["hosts"]}
        self.assertEqual({n: c for n, (c, _) in got.items()},
                         {"small": "replace_vm", "large": "hot_patch", "unmeasured": "unknown"})
        self.assertIn("undersized", got["small"][1])
        self.assertIn("requires at least 64", got["small"][1])
        self.assertIn("vCPU count is unknown", got["unmeasured"][1])
        self.assertFalse(result["hot_patch_allowed"])

    def test_executor_refuses_undersized_hosts_without_running_anything(self):
        ex = pc.GcloudHotPatchExecutor("proj", which=lambda _n: "/usr/bin/gcloud")
        record = {"name": "h", "zone": "us-central1-a", "machine_type": "n2-standard-32", "runner_prefix": "oxidex",
                  "runners_count": 3, "runner_label": "l", "runner_region": "us-central1", "patch_target": REPO,
                  "registration_target": "swack-tools", "runner_group_id": 10, "vcpu": 32, "ram_gib": 128.0}
        with unittest.mock.patch.object(manager, "apply_live_patch") as apply:
            for bad in (record, {k: v for k, v in record.items() if k not in ("vcpu", "ram_gib")}):
                with self.assertRaisesRegex(pc.HostRefusedError, "refused"):
                    ex.apply(bad, BIG)
            apply.assert_not_called()
            ex.apply({**record, "vcpu": 64, "ram_gib": 256.0}, BIG)
            apply.assert_called_once()

    def test_inventory_carries_observed_capacity(self):
        class FakeApi:
            async def get_json(self, path, query=None):
                self.path, self.query = path, query
                return {"guestCpus": 32, "memoryMb": 131072}

        api = FakeApi()
        spec = asyncio.run(compute_api.machine_type_capacity(api, "proj", "us-central1-a", "n2-standard-32"))
        self.assertEqual(spec, {"vcpu": 32, "ram_gib": 128.0})
        self.assertTrue(api.path.endswith("/zones/us-central1-a/machineTypes/n2-standard-32"))
        item = gce_item("h1", sha=OLD.patch_sha256, manifest=canonical_json(OLD))
        item["cpuPlatform"] = "AMD Turin"
        self.assertEqual(compute_api.runner_host_records([item])[0]["cpu_platform"], "AMD Turin")

        async def listing(_api, _project):
            return [item, gce_item("h2", sha=OLD.patch_sha256, manifest=canonical_json(OLD))], []

        async def capacity(_api, _project, zone, machine_type):
            raise RuntimeError("denied")

        async def measured(_api, _project, zone, machine_type):
            return {"vcpu": 32, "ram_gib": 128.0}

        with unittest.mock.patch.object(compute_api, "get_auth", return_value=compute_api.Auth("tok", "proj")), \
                unittest.mock.patch.object(compute_api, "list_instances", side_effect=listing), \
                unittest.mock.patch.object(compute_api, "machine_type_capacity", side_effect=measured):
            hosts = pc.gce_host_source("proj")()
        self.assertEqual([(h["vcpu"], h["ram_gib"]) for h in hosts], [(32, 128.0), (32, 128.0)])
        with unittest.mock.patch.object(compute_api, "get_auth", return_value=compute_api.Auth("tok", "proj")), \
                unittest.mock.patch.object(compute_api, "list_instances", side_effect=listing), \
                unittest.mock.patch.object(compute_api, "machine_type_capacity", side_effect=capacity):
            hosts = pc.gce_host_source("proj")()
        self.assertTrue(all("vcpu" not in h for h in hosts))  # unknown, never guessed


if __name__ == "__main__":
    unittest.main()
