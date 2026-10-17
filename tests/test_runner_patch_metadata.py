"""Recorded runner patch: instance metadata at provisioning and after a confirmed live patch, inventory parsing,
and the dashboard rollout preview built from it. No gcloud, GitHub, or GCE call leaves the process."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

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


class ProvisioningMetadataTests(unittest.TestCase):
    def provision(self, patch, vm=None):
        args = args_for(patch, *(["--existing-instance"] if vm else []))
        script = manager.render_startup(args, TOKEN, "group", "deploy-1")
        cloud = GcloudRecorder()
        describe = [SimpleNamespace(returncode=0 if vm else 1, stdout=json.dumps(vm) if vm else ""),
                    SimpleNamespace(returncode=0, stdout=json.dumps({"type": args.disk_type, "users": ["x"]}) if vm else "")]
        with mock.patch("lib.runner_manager.subprocess.run", side_effect=describe), \
                mock.patch("lib.runner_manager.gcloud", side_effect=cloud):
            manager.apply_startup(args, "us-central1-a", script)
        return args, cloud

    def assert_no_secret(self, cloud):
        for command in cloud.calls:
            self.assertNotIn(TOKEN, " ".join(command))
        for files in cloud.files:
            for key, text in files.items():
                if key != "startup-script":
                    self.assertNotIn(TOKEN, text, key)

    def test_create_records_manifest_sha_target_and_label(self):
        args, cloud = self.provision(TARGET)
        [create] = cloud.named("compute", "instances", "create")
        self.assertEqual(plain_metadata(create), {
            "spot-runner-patch-target": REPO, "spot-runner-prefix": "oxidex",
            "spot-runner-count": str(args.runner_count),  # the count select_sizing settled on
            "spot-runner-label": args.runner_label, "spot-runner-region": args.region,
            "spot-runner-registration-target": REPO,  # repo-registered: no runner group
            "spot-runner-patch-sha256": TARGET.patch_sha256})
        [files] = cloud.files
        self.assertEqual(set(files), {"startup-script", "spot-runner-patch"})
        self.assertIn(TOKEN, files["startup-script"])  # the token only ever travels in the startup script
        self.assertEqual(files["spot-runner-patch"], canonical_json(TARGET))
        self.assertEqual(validate_patch_dict(json.loads(files["spot-runner-patch"])).patch_sha256, TARGET.patch_sha256)
        labels = next(a for a in create if a.startswith("--labels="))
        self.assertIn(f"spot-runner-patch={TARGET.patch_sha256[:16]}", labels)
        self.assertIn(f"spot-target={target_label_value(REPO)}", labels)
        self.assert_no_secret(cloud)

    def test_org_registration_scope_and_group_are_recorded_apart_from_the_patch_repo(self):
        args = args_for(TARGET)
        args.target = "swack-tools"
        plain, _manifest, _label = manager.runner_patch_record(args, TARGET)
        self.assertEqual((plain["spot-runner-registration-target"], plain["spot-runner-group-id"],
                          plain["spot-runner-patch-target"]), ("swack-tools", str(args.runner_group_id), REPO))

    def test_create_without_patch_records_none(self):
        _, cloud = self.provision(None)
        [create] = cloud.named("compute", "instances", "create")
        self.assertEqual(plain_metadata(create)["spot-runner-patch-sha256"], "none")
        self.assertEqual(set(cloud.files[0]), {"startup-script"})
        self.assertIn("spot-runner-patch=none", next(a for a in create if a.startswith("--labels=")))
        self.assert_no_secret(cloud)

    def test_oversized_manifest_is_recorded_by_sha_only(self):
        big = validate_patch_dict({**BASE, "host": {"files": [{"path": "/etc/big", "content": "x" * 70_000}]}})
        _, cloud = self.provision(big)
        [create] = cloud.named("compute", "instances", "create")
        self.assertEqual(plain_metadata(create)["spot-runner-patch-sha256"], big.patch_sha256)
        self.assertEqual(set(cloud.files[0]), {"startup-script"})

    def test_existing_vm_gets_metadata_label_and_stale_manifest_removed(self):
        vm = {"status": "RUNNING", "machineType": "zones/z/machineTypes/c4d-standard-32",
              "scheduling": {"provisioningModel": "SPOT"}, "cpuPlatform": "AMD Turin",
              "disks": [{"boot": True, "deviceName": "boot"}, {"deviceName": "runner-data"}],
              "metadata": {"items": [{"key": "spot-runner-patch", "value": canonical_json(OLD)}]}}
        _, cloud = self.provision(None, vm=vm)
        [add] = [c for c in cloud.named("compute", "instances", "add-metadata")]
        self.assertEqual(plain_metadata(add)["spot-runner-patch-sha256"], "none")
        self.assertEqual(cloud.named("compute", "instances", "remove-metadata")[0][-1], "--keys=spot-runner-patch")
        self.assertEqual(cloud.named("compute", "instances", "add-labels")[0][-1], "--labels=spot-runner-patch=none")
        self.assert_no_secret(cloud)
        _, cloud = self.provision(TARGET, vm=vm)
        self.assertEqual(cloud.named("compute", "instances", "remove-metadata"), [])
        self.assertEqual(cloud.files[0]["spot-runner-patch"], canonical_json(TARGET))


class LivePatchMetadataTests(unittest.TestCase):
    def run_live(self, receipt, script_output):
        args = args_for(TARGET, "--patch-live")
        vm = runner_fleet.GceVm(args.instance_name, "us-central1-a", "RUNNING", "c4d-standard-32", True,
                                instance_id="1", cpu_platform="AMD Turin")

        def respond(command):
            if command[:2] == ["compute", "ssh"] and "state.json" in command[-1]:
                return json.dumps({"manifest": receipt.to_canonical_dict()}) if receipt else ""
            if command[:2] == ["compute", "ssh"]:
                return script_output
            return ""

        cloud = GcloudRecorder(respond)
        with mock.patch("lib.runner_manager.list_gcloud_vms", return_value=[vm]), \
                mock.patch("lib.runner_manager.gcloud", side_effect=cloud), \
                mock.patch("lib.runner_manager.sync_runner_labels"), \
                    mock.patch("lib.runner_manager.runner_label_sync_plan"):
            try:
                result = manager.apply_live_patch(args, "us-central1-a")
            except RuntimeError as exc:
                result = exc
        return result, cloud

    def test_metadata_is_updated_only_after_the_host_confirms(self):
        result, cloud = self.run_live(OLD, "something went wrong\n")
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual(cloud.named("compute", "instances", "add-metadata"), [])
        self.assertEqual(cloud.named("compute", "instances", "add-labels"), [])
        result, cloud = self.run_live(OLD, f"LIVE_PATCH_APPLIED:{OLD.patch_sha256}\n")  # another patch's marker
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual(cloud.named("compute", "instances", "add-metadata"), [])
        result, cloud = self.run_live(OLD, f"ok\nLIVE_PATCH_APPLIED:{TARGET.patch_sha256}\n")
        self.assertEqual(result, "hot_patch")
        order = [c[:3] for c in cloud.calls]
        apply_index = max(i for i, c in enumerate(cloud.calls) if c[:2] == ["compute", "ssh"])
        self.assertEqual(order[apply_index + 1:], [["compute", "instances", "add-metadata"],
                                                    ["compute", "instances", "add-labels"]])
        self.assertEqual(plain_metadata(cloud.calls[apply_index + 1])["spot-runner-patch-sha256"], TARGET.patch_sha256)
        self.assertEqual(cloud.files[-1]["spot-runner-patch"], canonical_json(TARGET))

    def test_label_sync_preflight_failure_stops_before_the_host_changes(self):
        args = args_for(TARGET, "--patch-live")
        vm = runner_fleet.GceVm(args.instance_name, "us-central1-a", "RUNNING", "c4d-standard-32", True,
                                instance_id="1", cpu_platform="AMD Turin")
        cloud = GcloudRecorder(lambda c: json.dumps({"manifest": OLD.to_canonical_dict()})
                               if c[:2] == ["compute", "ssh"] else "")
        with mock.patch("lib.runner_manager.list_gcloud_vms", return_value=[vm]), \
                mock.patch("lib.runner_manager.gcloud", side_effect=cloud), \
                mock.patch("lib.runner_manager.runner_label_sync_plan",
                           side_effect=RuntimeError("Runner group 10 is not enabled for swack-tools/oxidex")), \
                mock.patch("lib.runner_manager.sync_runner_labels") as sync:
            with self.assertRaisesRegex(RuntimeError, "Runner group"):
                manager.apply_live_patch(args, "us-central1-a")
        self.assertEqual(len([c for c in cloud.calls if c[:2] == ["compute", "ssh"]]), 1)  # only the receipt read
        self.assertEqual(cloud.named("compute", "instances", "add-metadata"), [])
        sync.assert_not_called()
        self.assertIs(args.deployment_vm, vm)  # strict pools verify cached identities against the observed VM

    def test_label_sync_plan_checks_without_writing(self):
        args = args_for(TARGET, "--patch-live")
        rows = [{"id": n, "name": f"oxidex-{args.instance_name}-{n}", "labels": []} for n in range(1, args.runner_count + 1)]
        commands = []

        def run(cmd, **_kw):
            commands.append(cmd)
            return "\n".join(json.dumps(r) for r in rows)

        with mock.patch("lib.runner_manager.run", side_effect=run):
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

    def test_refused_classifications_record_nothing(self):
        for receipt in (ARM, MOUNTS):
            # The target has no mounts and x86_64, so an arm host needs a new VM and a mounts host recreated containers.
            result, cloud = self.run_live(receipt, f"LIVE_PATCH_APPLIED:{TARGET.patch_sha256}")
            self.assertIsInstance(result, RuntimeError, receipt.patch_sha256)
            self.assertEqual(cloud.named("compute", "instances", "add-metadata"), [])

    def test_dashboard_executor_path_records_only_after_confirmation(self):
        ex = pc.GcloudHotPatchExecutor("proj", which=lambda _n: "/usr/bin/gcloud")
        host = {"name": "oxidex-runners-1", "zone": "us-central1-a", "machine_type": "c4d-standard-32",
                "runner_prefix": "oxidex", "runners_count": 3, "runner_label": "oxidex-spot-ci",
                "runner_region": "us-central1", "patch_target": REPO,
                "registration_target": "swack-tools", "runner_group_id": 10}
        vm = runner_fleet.GceVm("oxidex-runners-1", "us-central1-a", "RUNNING", "c4d-standard-32", True)
        for output, expect_metadata in (("no marker", False), (f"LIVE_PATCH_APPLIED:{TARGET.patch_sha256}", True)):
            def respond(command, _output=output):
                if command[:2] == ["compute", "ssh"] and "state.json" in command[-1]:
                    return json.dumps({"manifest": OLD.to_canonical_dict()})
                return _output if command[:2] == ["compute", "ssh"] else ""

            cloud = GcloudRecorder(respond)
            with mock.patch("lib.runner_manager.list_gcloud_vms", return_value=[vm]), \
                    mock.patch("lib.runner_manager.gcloud", side_effect=cloud), \
                    mock.patch("lib.runner_manager.sync_runner_labels"), \
                    mock.patch("lib.runner_manager.runner_label_sync_plan"):
                if expect_metadata:
                    self.assertEqual(ex.apply(host, TARGET), {"result": "applied"})
                else:
                    with self.assertRaises(RuntimeError):
                        ex.apply(host, TARGET)
            self.assertEqual(bool(cloud.named("compute", "instances", "add-metadata")), expect_metadata, output)


def gce_item(name, *, sha=None, manifest=None, target=REPO, label=True, status="RUNNING", extra=()):
    items = [{"key": "startup-script", "value": f"#!/bin/bash\nTOKEN={TOKEN}"}, *extra]
    if sha is not None:
        items.append({"key": "spot-runner-patch-sha256", "value": sha})
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
    return {"name": name, "zone": "projects/p/zones/us-central1-a", "status": status,
            "machineType": "projects/p/zones/us-central1-a/machineTypes/c4d-standard-32",
            "labels": labels, "metadata": {"items": items}}


class InventoryTests(unittest.TestCase):
    def test_patch_record_parse(self):
        full = runner_fleet.patch_record_from_gce_item(gce_item("a", sha=TARGET.patch_sha256,
                                                                manifest=canonical_json(TARGET)))
        self.assertEqual(full, {"patch_sha256": TARGET.patch_sha256, "patch_manifest_json": canonical_json(TARGET),
                                "patch_target": REPO})
        self.assertEqual(runner_fleet.patch_record_from_gce_item(gce_item("b", sha="none", manifest="{}"))["patch_manifest_json"], "")
        for bad_sha in ("abc", "Z" * 64, "", " "):
            self.assertEqual(runner_fleet.patch_record_from_gce_item(gce_item("c", sha=bad_sha))["patch_sha256"], "", bad_sha)
        oversized = runner_fleet.patch_record_from_gce_item(gce_item("d", sha=TARGET.patch_sha256, manifest="x" * 70_000))
        self.assertEqual(oversized["patch_manifest_json"], "")
        self.assertEqual(runner_fleet.patch_record_from_gce_item(gce_item("e")),
                         {"patch_sha256": "", "patch_manifest_json": "", "patch_target": ""})
        self.assertEqual(runner_fleet.patch_record_from_gce_item({"metadata": "garbage"})["patch_sha256"], "")

    def test_list_gcloud_vms_carries_the_recorded_patch(self):
        rows = [gce_item("oxidex-runners-1", sha=TARGET.patch_sha256, manifest=canonical_json(TARGET)),
                gce_item("oxidex-runners-2")]
        rows[0]["scheduling"] = {"provisioningModel": "SPOT"}

        def command_json(args):
            if args[1:4] == ["compute", "instances", "list"]:
                return rows
            return [] if args[1:4] == ["compute", "disks", "list"] else {}

        with mock.patch("lib.runner_fleet._command_json", side_effect=command_json):
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
        with mock.patch.object(pc.PatchConsole, "effective_patch",
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

    def test_host_source_failure_is_a_clear_error(self):
        def broken():
            raise RuntimeError("compute.instances.list denied")

        console = pc.PatchConsole("swack-tools/spot-runners-config", token="t", host_source=broken)
        with mock.patch.object(pc.PatchConsole, "effective_patch", return_value=(TARGET, {"draft": False})):
            with self.assertRaisesRegex(pc.GitHubRestError, "Could not list runner hosts"):
                console.preview(REPO, [])
        self.assertIsNone(pc.host_source_from_env("p", {}))
        self.assertIsNotNone(pc.host_source_from_env("p", {"DASHBOARD_FLEET_INVENTORY": "gce"}))

    def test_control_vm_enables_the_gce_inventory(self):
        script = (Path(__file__).resolve().parents[1] / "cloud-init" / "control-vm-startup.sh").read_text()
        self.assertIn("printf 'DASHBOARD_FLEET_INVENTORY=gce\\n'", script)


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
        cloud = GcloudRecorder(lambda c: json.dumps({"manifest": OLD.to_canonical_dict()})
                               if c[:2] == ["compute", "ssh"] and "state.json" in c[-1]
                               else f"LIVE_PATCH_APPLIED:{patch.patch_sha256}")
        with mock.patch("lib.runner_manager.list_gcloud_vms", return_value=[vm]), \
                mock.patch("lib.runner_manager.gcloud", side_effect=cloud), \
                mock.patch("lib.runner_manager.runner_label_sync_plan"), \
                mock.patch("lib.runner_manager.sync_runner_labels"):
            try:
                return manager.apply_live_patch(args, "us-central1-a"), cloud
            except RuntimeError as exc:
                return exc, cloud

    def test_cli_live_patch_refuses_undersized_or_unmeasured_hosts_before_changing_anything(self):
        base = dict(zone="us-central1-a", status="RUNNING", machine_type="c4d-standard-32", spot=True,
                    instance_id="1", cpu_platform="AMD Turin")
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
        self.assertEqual(len(cloud.named("compute", "instances", "add-metadata")), 1)

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
        with mock.patch.object(pc.PatchConsole, "effective_patch", return_value=(BIG, {"draft": False})):
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
        with mock.patch.object(manager, "apply_live_patch") as apply:
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

        with mock.patch.object(compute_api, "get_auth", return_value=compute_api.Auth("tok", "proj")), \
                mock.patch.object(compute_api, "list_instances", side_effect=listing), \
                mock.patch.object(compute_api, "machine_type_capacity", side_effect=measured):
            hosts = pc.gce_host_source("proj")()
        self.assertEqual([(h["vcpu"], h["ram_gib"]) for h in hosts], [(32, 128.0), (32, 128.0)])
        with mock.patch.object(compute_api, "get_auth", return_value=compute_api.Auth("tok", "proj")), \
                mock.patch.object(compute_api, "list_instances", side_effect=listing), \
                mock.patch.object(compute_api, "machine_type_capacity", side_effect=capacity):
            hosts = pc.gce_host_source("proj")()
        self.assertTrue(all("vcpu" not in h for h in hosts))  # unknown, never guessed


if __name__ == "__main__":
    unittest.main()
