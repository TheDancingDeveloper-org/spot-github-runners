from decimal import Decimal
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, MagicMock, patch

from lib.compute_api import Offer
from lib.runner_fleet import GceVm, GitHubRunner
from lib.target_config import TargetConfig, resolve_target
from lib import runner_manager as manager
from lib.spot_runner_autoscaler import (
    begin_price_rotation,
    guard_fleet_mutation,
    max_allowed_hosts,
    read_scale_state,
    reconcile_prices,
    removable_spot_vms,
    rotation_spend_allowance,
    step_price_rotation,
)
from lib import runner_drain


class TargetConfigTests(unittest.TestCase):
    def test_repo_target_detection(self):
        target = resolve_target("xbmc4lyfe/newznab-tmux")
        self.assertTrue(target.is_repo)
        self.assertFalse(target.is_org)
        self.assertEqual(target.name, "xbmc4lyfe/newznab-tmux")
        self.assertEqual(target.owner, "xbmc4lyfe")
        self.assertEqual(target.repo, "newznab-tmux")
        self.assertEqual(target.api_path, "repos/xbmc4lyfe/newznab-tmux")
        self.assertEqual(target.registration_token_url, "repos/xbmc4lyfe/newznab-tmux/actions/runners/registration-token")
        self.assertEqual(target.runners_url, "repos/xbmc4lyfe/newznab-tmux/actions/runners")

    def test_org_target_detection(self):
        target = resolve_target("swack-tools")
        self.assertTrue(target.is_org)
        self.assertFalse(target.is_repo)
        self.assertEqual(target.name, "swack-tools")
        self.assertEqual(target.owner, "swack-tools")
        self.assertIsNone(target.repo)
        self.assertEqual(target.api_path, "orgs/swack-tools")
        self.assertEqual(target.registration_token_url, "orgs/swack-tools/actions/runners/registration-token")
        self.assertEqual(target.runners_url, "orgs/swack-tools/actions/runners")

    def test_target_serialization_roundtrip(self):
        target = resolve_target("xbmc4lyfe/newznab-tmux")
        data = target.to_dict()
        restored = TargetConfig.from_dict(data)
        self.assertEqual(target.name, restored.name)
        self.assertEqual(target.target_type, restored.target_type)

    def test_repo_target_rejects_runner_group(self):
        with self.assertRaises(ValueError):
            TargetConfig(name="xbmc4lyfe/newznab-tmux", runner_group_id=10)

    def test_empty_target_rejected(self):
        with self.assertRaises(ValueError):
            resolve_target("")

    def test_valid_tricky_personal_repo_names(self):
        """Verify that legitimate complex repo names produce valid GitHub URLs and API endpoints."""
        cases = [
            ("user/repo.with.dots", "repos/user/repo.with.dots", "repo.with.dots"),
            ("user/repo-with-dashes", "repos/user/repo-with-dashes", "repo-with-dashes"),
            ("user/repo_with_underscores", "repos/user/repo_with_underscores", "repo_with_underscores"),
            ("user-name.org/repo-123.v2", "repos/user-name.org/repo-123.v2", "repo-123.v2"),
        ]
        for name, expected_api, expected_repo in cases:
            target = resolve_target(name)
            self.assertTrue(target.is_repo)
            self.assertEqual(target.api_path, expected_api)
            self.assertEqual(target.repo, expected_repo)
            self.assertEqual(target.github_url, f"https://github.com/{name}")

    def test_target_strips_surrounding_whitespace(self):
        t_org = TargetConfig("   swack-tools   ")
        self.assertEqual(t_org.name, "swack-tools")
        self.assertEqual(t_org.api_path, "orgs/swack-tools")

        t_repo = TargetConfig("  xbmc4lyfe / newznab-tmux  ")
        self.assertEqual(t_repo.name, "xbmc4lyfe/newznab-tmux")
        self.assertEqual(t_repo.api_path, "repos/xbmc4lyfe/newznab-tmux")

    def test_target_rejects_leading_trailing_and_multiple_slashes(self):
        for invalid in ["foo/", "/bar", "/foo/bar/", "foo//bar", "foo/bar/baz", "/", "//"]:
            with self.assertRaises(ValueError):
                TargetConfig(invalid)

    def test_target_type_mismatches_rejected(self):
        with self.assertRaises(ValueError):
            TargetConfig("swack-tools", target_type="repo")
        with self.assertRaises(ValueError):
            TargetConfig("foo/bar", target_type="org")

    def test_runner_drain_is_repo_target_with_target_config_and_str(self):
        repo_tc = TargetConfig("xbmc4lyfe/newznab-tmux")
        org_tc = TargetConfig("swack-tools")
        self.assertTrue(runner_drain.is_repo_target(repo_tc))
        self.assertFalse(runner_drain.is_repo_target(org_tc))
        self.assertTrue(runner_drain.is_repo_target("xbmc4lyfe/newznab-tmux"))
        self.assertFalse(runner_drain.is_repo_target("swack-tools"))
        self.assertEqual(runner_drain.target_api_prefix(repo_tc), "repos/xbmc4lyfe/newznab-tmux")
        self.assertEqual(runner_drain.target_api_prefix(org_tc), "orgs/swack-tools")


class SizingFloorTests(unittest.TestCase):
    def _create_args(self, cpu: int, ram: int, runner_count: int = 1):
        args = manager.args_parser().parse_args([
            "--max-cost", "1.0",
            "--cpu", str(cpu),
            "--ram", str(ram),
            "--runner-count", str(runner_count),
        ])
        args.fixed_runner_sizing = False
        return args

    def test_minimum_sizing_2vcpu_4gib(self):
        args = self._create_args(cpu=2, ram=4)
        with patch.object(manager, "configure_storage"), patch.object(manager, "configure_cpu_platform"):
            manager.select_sizing(args)
        self.assertEqual(args.runner_count, 1)
        self.assertEqual(args.runner_cpu, 2)
        self.assertEqual(args.runner_memory_gb, 2)

    def test_sizing_2vcpu_8gib(self):
        args = self._create_args(cpu=2, ram=8)
        with patch.object(manager, "configure_storage"), patch.object(manager, "configure_cpu_platform"):
            manager.select_sizing(args)
        self.assertEqual(args.runner_count, 1)
        self.assertEqual(args.runner_cpu, 2)
        self.assertEqual(args.runner_memory_gb, 6)

    def test_sizing_4vcpu_16gib(self):
        args = self._create_args(cpu=4, ram=16)
        with patch.object(manager, "configure_storage"), patch.object(manager, "configure_cpu_platform"):
            manager.select_sizing(args)
        self.assertEqual(args.runner_count, 1)
        self.assertEqual(args.runner_cpu, 4)
        self.assertEqual(args.runner_memory_gb, 12)

    def test_sizing_8vcpu_32gib(self):
        args = self._create_args(cpu=8, ram=32)
        with patch.object(manager, "configure_storage"), patch.object(manager, "configure_cpu_platform"):
            manager.select_sizing(args)
        self.assertEqual(args.runner_count, 1)
        self.assertEqual(args.runner_cpu, 4)
        self.assertEqual(args.runner_memory_gb, 24)

    def test_legacy_sizing_32vcpu_128gib(self):
        args = manager.args_parser().parse_args([
            "--max-cost", "1.0",
            "--cpu", "32",
            "--ram", "128",
        ])
        with patch.object(manager, "configure_storage"), patch.object(manager, "configure_cpu_platform"):
            manager.select_sizing(args)
        self.assertEqual(args.runner_count, 8)
        self.assertEqual(args.runner_cpu, 4)
        self.assertEqual(args.runner_memory_gb, 15)

    def test_subminimum_cpu_rejected(self):
        args = self._create_args(cpu=1, ram=4)
        with patch.object(manager, "configure_storage"), patch.object(manager, "configure_cpu_platform"):
            with self.assertRaises(RuntimeError):
                manager.select_sizing(args)

    def test_subminimum_ram_rejected(self):
        args = self._create_args(cpu=2, ram=3)
        with patch.object(manager, "configure_storage"), patch.object(manager, "configure_cpu_platform"):
            with self.assertRaises(RuntimeError):
                manager.select_sizing(args)

    def test_sizing_4vcpu_8gib_host_overhead_2gib(self):
        """For 4 vCPU / 8 GiB RAM, overhead scales dynamically to 2 GiB leaving 6 GiB runner RAM."""
        args = manager.args_parser().parse_args([
            "--max-cost", "1.0",
            "--cpu", "4",
            "--ram", "8",
        ])
        args.fixed_runner_sizing = False
        with patch.object(manager, "configure_storage"), patch.object(manager, "configure_cpu_platform"):
            manager.select_sizing(args)
        self.assertEqual(args.runner_count, 1)
        self.assertEqual(args.runner_cpu, 4)
        self.assertEqual(args.runner_memory_gb, 6)

    def test_sizing_4vcpu_4gib_minimum_memory(self):
        """For 4 vCPU / 4 GiB RAM, runner receives 2 GiB RAM."""
        args = manager.args_parser().parse_args([
            "--max-cost", "1.0",
            "--cpu", "4",
            "--ram", "4",
        ])
        args.fixed_runner_sizing = False
        with patch.object(manager, "configure_storage"), patch.object(manager, "configure_cpu_platform"):
            manager.select_sizing(args)
        self.assertEqual(args.runner_count, 1)
        self.assertEqual(args.runner_cpu, 4)
        self.assertEqual(args.runner_memory_gb, 2)

    def test_sizing_8vcpu_4gib_scales_to_single_runner(self):
        """For 8 vCPU / 4 GiB RAM, runner count scales to 1 with 2 GiB RAM."""
        args = manager.args_parser().parse_args([
            "--max-cost", "1.0",
            "--cpu", "8",
            "--ram", "4",
        ])
        args.fixed_runner_sizing = False
        with patch.object(manager, "configure_storage"), patch.object(manager, "configure_cpu_platform"):
            manager.select_sizing(args)
        self.assertEqual(args.runner_count, 1)
        self.assertEqual(args.runner_cpu, 4)
        self.assertEqual(args.runner_memory_gb, 2)

    def test_sizing_16vcpu_8gib_allocates_positive_memory(self):
        """For 16 vCPU / 8 GiB RAM, runner count scales to 1 with 6 GiB RAM."""
        args = self._create_args(cpu=16, ram=8, runner_count=1)
        with patch.object(manager, "configure_storage"), patch.object(manager, "configure_cpu_platform"):
            manager.select_sizing(args)
        self.assertEqual(args.runner_count, 1)
        self.assertEqual(args.runner_cpu, 4)
        self.assertEqual(args.runner_memory_gb, 6)

    def test_sizing_subminimum_fixed_memory_rejected(self):
        """Fixed runner sizing with < 2 GiB memory is strictly rejected."""
        args = self._create_args(cpu=4, ram=8, runner_count=1)
        args.fixed_runner_sizing = True
        args.runner_memory_gb = 1
        with patch.object(manager, "configure_storage"), patch.object(manager, "configure_cpu_platform"):
            with self.assertRaises(RuntimeError):
                manager.select_sizing(args)


class RunnerTokenAndStartupTests(unittest.TestCase):
    def test_repo_registration_token_url(self):
        target = resolve_target("xbmc4lyfe/newznab-tmux")
        with patch("lib.runner_manager.run", return_value="synthetic-repo-token") as mock_run:
            token = manager.generate_registration_token(target)
            self.assertEqual(token, "synthetic-repo-token")
            mock_run.assert_called_once_with(
                ["gh", "api", "--method", "POST", "repos/xbmc4lyfe/newznab-tmux/actions/runners/registration-token", "--jq", ".token"],
                quiet=True,
            )

    def test_org_registration_token_url(self):
        target = resolve_target("swack-tools")
        with patch("lib.runner_manager.run", return_value="synthetic-org-token") as mock_run:
            token = manager.generate_registration_token(target)
            self.assertEqual(token, "synthetic-org-token")
            mock_run.assert_called_once_with(
                ["gh", "api", "--method", "POST", "orgs/swack-tools/actions/runners/registration-token", "--jq", ".token"],
                quiet=True,
            )

    def test_repo_startup_script_sets_target_url(self):
        args = manager.args_parser().parse_args([
            "--max-cost", "1.0",
            "--cpu", "2",
            "--ram", "8",
            "--target", "xbmc4lyfe/newznab-tmux",
        ])
        with patch.object(manager, "configure_storage"), patch.object(manager, "configure_cpu_platform"):
            manager.select_sizing(args)
        rendered = manager.render_startup(args, "test-token", "", "deploy-123")
        self.assertIn("ORG_URL=https://github.com/xbmc4lyfe/newznab-tmux", rendered)
        self.assertNotIn("{{ORG}}", rendered)

    def test_verify_runner_group_uses_target_org(self):
        args = SimpleNamespace(
            target="alternate-org",
            runner_group_id=123,
            heavy_runner_pool=None,
        )
        group_reply = json.dumps({"visibility": "selected", "name": "AlternateGroup"})
        repos_reply = json.dumps({"repositories": [{"full_name": "alternate-org/oxidex"}]})
        with patch("lib.runner_manager.run", side_effect=[group_reply, repos_reply]) as mock_run:
            group = manager.verify_runner_group(args)
            self.assertEqual(group["name"], "AlternateGroup")
            mock_run.assert_any_call(["gh", "api", "orgs/alternate-org/actions/runner-groups/123"], quiet=True)

    def test_verify_runner_group_rejects_personal_repository_target(self):
        args = SimpleNamespace(
            target="xbmc4lyfe/newznab-tmux",
            runner_group_id=123,
        )
        with self.assertRaises(RuntimeError) as ctx:
            manager.verify_runner_group(args)
        self.assertIn("Runner groups are not supported for personal repository targets", str(ctx.exception))


class AutoscalerFloorAndOverlapTests(unittest.TestCase):
    def test_idle_vm_not_removed_if_at_floor(self):
        vm = GceVm("oxidex-runners-vm1", "asia-east2-a", "RUNNING", "n2-standard-2", True, instance_id="vm-1")
        runner = GitHubRunner(1, "gha-oxidex-runners-vm1-1", "online", False, ("spot",))
        vms = [vm]
        runners = [runner]

        state = {
            "min_runner_floor": 1,
            "host_deployments": {
                "asia-east2-a/vm-1": {"expected_runner_count": 1},
            }
        }
        # Only 1 VM present: even if completely idle, removable_spot_vms must NOT remove it
        removals = removable_spot_vms(vms, runners, state=state)
        self.assertEqual(removals, [])

    def test_excess_idle_vm_removed_when_above_floor(self):
        vm1 = GceVm("oxidex-runners-vm1", "asia-east2-a", "RUNNING", "n2-standard-2", True, instance_id="vm-1")
        vm2 = GceVm("oxidex-runners-vm2", "asia-east2-a", "RUNNING", "n2-standard-2", True, instance_id="vm-2")
        runner1 = GitHubRunner(1, "gha-oxidex-runners-vm1-1", "online", False, ("spot",))
        runner2 = GitHubRunner(2, "gha-oxidex-runners-vm2-1", "online", False, ("spot",))
        vms = [vm1, vm2]
        runners = [runner1, runner2]

        state = {
            "min_runner_floor": 1,
            "host_deployments": {
                "asia-east2-a/vm-1": {"expected_runner_count": 1},
                "asia-east2-a/vm-2": {"expected_runner_count": 1},
            }
        }
        # 2 VMs present, floor is 1: exactly 1 VM may be removed
        removals = removable_spot_vms(vms, runners, state=state)
        self.assertEqual(len(removals), 1)

    def test_max_allowed_hosts_temporary_plus_one(self):
        args = SimpleNamespace(maximum_hosts=3)
        state_idle = {}
        state_rotating = {"price_rotation": {"phase": "provision"}}

        self.assertEqual(max_allowed_hosts(args, state_idle), 3)
        self.assertEqual(max_allowed_hosts(args, state_rotating), 4)

    def test_rotation_spend_allowance(self):
        state_idle = {}
        state_rotating = {
            "price_rotation": {
                "replacement_offer": {
                    "total_daily_usd": 3.75
                }
            }
        }
        self.assertEqual(rotation_spend_allowance(state_idle), Decimal("0"))
        self.assertEqual(rotation_spend_allowance(state_rotating), Decimal("3.75"))


class PriceRotationStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.outgoing_vm = GceVm(
            "oxidex-runners-old", "asia-east2-a", "RUNNING", "n4-highcpu-16", True,
            vcpu=16, ram_gib=32.0, instance_id="old-123",
            disks=({"diskType": "pd-standard", "diskSizeGb": 100},)
        )
        self.replacement_vm = GceVm(
            "oxidex-runners-new", "asia-east2-a", "RUNNING", "n4-highcpu-16", True,
            vcpu=16, ram_gib=32.0, instance_id="new-456",
            disks=({"diskType": "pd-standard", "diskSizeGb": 100},)
        )
        self.offer = SimpleNamespace(
            region="asia-east2",
            machine_type="n4-highcpu-16",
            vcpu=16,
            ram_gib=32.0,
            hourly_usd=0.15,
            total_daily_usd=Decimal("3.60"),
            zone="asia-east2-a",
            zones=("asia-east2-a",),
            cpu_platform="Intel Emerald Rapids",
        )
        self.args = SimpleNamespace(
            project="homelab-424523",
            org="swack-tools",
            target="swack-tools",
            max_daily_usd=Decimal("4.00"),
            maximum_hosts=5,
            disk_size_gb=100,
            dashboard_port=8080,
            make_before_break=True,
        )

    def test_overpriced_vm_initiates_rotation_without_deleting_old_vm(self):
        state = {"idle_loops": 0}
        launched = GceVm("oxidex-runners-new", "asia-east2-a", "PROVISIONING", "n4-highcpu-16", True)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.spot_runner_autoscaler.check_running_prices", return_value=[self.outgoing_vm]), \
                    patch("lib.spot_runner_autoscaler.offer_refresher", return_value=lambda: [self.offer]), \
                    patch("lib.spot_runner_autoscaler.delete_overpriced_vm") as mock_delete, \
                    patch("lib.spot_runner_autoscaler.launch_offer", return_value=launched) as mock_launch:
                result = reconcile_prices([self.outgoing_vm], [], self.args, state, path)
                self.assertTrue(result)
                mock_delete.assert_not_called()
                mock_launch.assert_called_once()
                recovered = read_scale_state(path)
                self.assertIn("price_rotation", recovered)
                self.assertEqual(recovered["price_rotation"]["phase"], "boot")
                self.assertEqual(recovered["price_rotation"]["outgoing_vm"]["name"], "oxidex-runners-old")
                self.assertEqual(recovered["price_rotation"]["replacement_vm"]["name"], "oxidex-runners-new")
                self.assertNotIn("dashboard_healthy", recovered["price_rotation"]["readiness"])
                self.assertNotIn("origin_switched", recovered["price_rotation"])

    def _readiness_state(self):
        return {
            "price_rotation": {
                "phase": "verify_readiness",
                "target": "swack-tools",
                "target_type": "org",
                "outgoing_vm": {"name": "oxidex-runners-old", "zone": "asia-east2-a", "instance_id": "old-123"},
                "replacement_vm": {"name": "oxidex-runners-new", "zone": "asia-east2-a", "instance_id": "new-456"},
                "expected_runner_count": 1,
                "readiness": {"runners_online": False},
                "phase_started_at": 2000000000.0,
            }
        }

    def test_runner_readiness_gate_needs_only_runners_and_never_touches_dashboard_or_edge(self):
        state = self._readiness_state()
        online = GitHubRunner(1, "gha-oxidex-runners-new-1", "online", False, ("spot",))
        offline = GitHubRunner(1, "gha-oxidex-runners-new-1", "offline", False, ("spot",))
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.edge_state.write_origin") as mock_origin, \
                    patch("lib.edge_state.CloudflareKv.put") as mock_put, \
                    patch("urllib.request.urlopen") as mock_urlopen, \
                    patch("lib.spot_runner_autoscaler.now", return_value=2000000001.0):
                step_price_rotation([self.outgoing_vm, self.replacement_vm], [offline], self.args, state, path)
                recovered = read_scale_state(path)
                self.assertEqual(recovered["price_rotation"]["phase"], "verify_readiness")
                self.assertFalse(recovered["price_rotation"]["readiness"]["runners_online"])

                step_price_rotation([self.outgoing_vm, self.replacement_vm], [online], self.args, state, path)
                recovered = read_scale_state(path)
                self.assertEqual(recovered["price_rotation"]["phase"], "cordon_drain")
                self.assertTrue(recovered["price_rotation"]["readiness"]["runners_online"])
                self.assertEqual(recovered["price_rotation"]["readiness"], {"runners_online": True})
                mock_origin.assert_not_called()
                mock_put.assert_not_called()
                mock_urlopen.assert_not_called()

    def test_readiness_ignores_runners_of_other_hosts_with_overlapping_names(self):
        state = self._readiness_state()
        unrelated = GitHubRunner(2, "gha-oxidex-runners-new2-1", "online", False, ("spot",))
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.spot_runner_autoscaler.now", return_value=2000000001.0):
                step_price_rotation([self.outgoing_vm, self.replacement_vm], [unrelated], self.args, state, path)
            self.assertEqual(read_scale_state(path)["price_rotation"]["phase"], "verify_readiness")

    def test_saved_origin_cutover_phase_resumes_as_cordon_without_edge_write(self):
        state = self._readiness_state()
        rotation = state["price_rotation"]
        rotation.update(phase="origin_cutover", origin_switched=False,
                        readiness={"runners_online": True, "dashboard_healthy": True, "state_hydrated": True})
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.edge_state.write_origin") as mock_origin, \
                    patch("urllib.request.urlopen") as mock_urlopen, \
                    patch("lib.runner_drain.isolated_group", return_value="orgs/swack-tools/actions/runner-groups/7"), \
                    patch("lib.runner_drain.api", return_value={}):
                step_price_rotation([self.outgoing_vm, self.replacement_vm], [], self.args, state, path)
            recovered = read_scale_state(path)["price_rotation"]
            self.assertEqual(recovered["phase"], "cordon_drain")
            self.assertEqual(recovered["readiness"], {"runners_online": True})
            self.assertNotIn("origin_switched", recovered)
            mock_origin.assert_not_called()
            mock_urlopen.assert_not_called()

    def test_saved_boot_hydrate_phase_resumes_as_runner_boot(self):
        state = self._readiness_state()
        state["price_rotation"]["phase"] = "boot_hydrate"
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("urllib.request.urlopen") as mock_urlopen, \
                    patch("lib.spot_runner_autoscaler.now", return_value=2000000001.0):
                step_price_rotation([self.outgoing_vm, self.replacement_vm], [], self.args, state, path)
            self.assertEqual(read_scale_state(path)["price_rotation"]["phase"], "verify_readiness")
            mock_urlopen.assert_not_called()

    def test_autoscaler_has_no_dashboard_cutover_or_hardcoded_cloudflare_settings(self):
        import lib.spot_runner_autoscaler as autoscaler
        source = Path(autoscaler.__file__).read_text(encoding="utf-8")
        for forbidden in ("1c34335d7e9216e17699777fccd5fab6", "wrangler", "switch_cloudflare_origin",
                          "check_dashboard_health", "origin:active", "CLOUDFLARE_API_TOKEN"):
            self.assertNotIn(forbidden, source)
        self.assertFalse(hasattr(autoscaler, "switch_cloudflare_origin"))
        self.assertFalse(hasattr(autoscaler, "check_dashboard_health"))

    def _provision_state(self, **extra):
        rotation = {
            "phase": "provision",
            "target": "swack-tools",
            "outgoing_vm": {"name": "oxidex-runners-old", "zone": "asia-east2-a", "instance_id": "old-123"},
            "replacement_offer": {
                "machine_type": "n4-highcpu-16", "zone": "asia-east2-a", "region": "asia-east2",
                "vcpu": 16, "ram_gib": 32.0, "hourly_usd": 0.15, "total_daily_usd": 3.60,
            },
            "pending_replacement": None,
            "replacement_vm": None,
            "phase_started_at": 1000.0,
            "attempt_count": 0,
        }
        rotation.update(extra)
        return {"price_rotation": rotation}

    def test_provision_never_adopts_an_unrelated_spot_vm(self):
        unrelated = GceVm("oxidex-runners-unrelated", "asia-east2-b", "RUNNING", "n4-highcpu-16", True,
                          instance_id="other-1")
        state = self._provision_state()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.spot_runner_autoscaler.guard_fleet_mutation"), \
                    patch("lib.spot_runner_autoscaler.launch_offer", return_value=None), \
                    patch("lib.spot_runner_autoscaler.now", return_value=1010.0):
                step_price_rotation([self.outgoing_vm, unrelated], [], self.args, state, path)
            recovered = read_scale_state(path)["price_rotation"]
            self.assertEqual(recovered["phase"], "provision")
            self.assertIsNone(recovered["replacement_vm"])
            self.assertEqual(recovered["attempt_count"], 1)

    def test_provision_records_the_name_its_own_launch_persisted(self):
        unrelated = GceVm("oxidex-runners-unrelated", "asia-east2-b", "RUNNING", "n4-highcpu-16", True,
                          instance_id="other-1")
        state = self._provision_state()

        def fake_launch(offer, args, launch_state, launch_path):
            launch_state["price_rotation"]["pending_replacement"] = {
                "name": "oxidex-runners-asiaeast2-a-1010", "zone": "asia-east2-a"}
            return GceVm("oxidex-runners-asiaeast2-a-1010", "asia-east2-a", "PROVISIONING", "n4-highcpu-16", True)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.spot_runner_autoscaler.guard_fleet_mutation"), \
                    patch("lib.spot_runner_autoscaler.launch_offer", side_effect=fake_launch), \
                    patch("lib.spot_runner_autoscaler.now", return_value=1010.0):
                step_price_rotation([self.outgoing_vm, unrelated], [], self.args, state, path)
            recovered = read_scale_state(path)["price_rotation"]
            self.assertEqual(recovered["phase"], "boot")
            self.assertEqual(recovered["replacement_vm"]["name"], "oxidex-runners-asiaeast2-a-1010")
            self.assertEqual(recovered["replacement_vm"]["zone"], "asia-east2-a")

    def test_provision_rejects_launch_result_that_differs_from_persisted_intent(self):
        state = self._provision_state()

        def fake_launch(offer, args, launch_state, launch_path):
            launch_state["price_rotation"]["pending_replacement"] = {"name": "oxidex-runners-mine", "zone": "asia-east2-a"}
            return GceVm("oxidex-runners-someone-else", "asia-east2-a", "RUNNING", "n4-highcpu-16", True)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.spot_runner_autoscaler.guard_fleet_mutation"), \
                    patch("lib.spot_runner_autoscaler.launch_offer", side_effect=fake_launch):
                step_price_rotation([self.outgoing_vm], [], self.args, state, path)
            recovered = read_scale_state(path)["price_rotation"]
            self.assertIsNone(recovered["replacement_vm"])
            self.assertEqual(recovered["phase"], "provision")

    def test_provision_resume_adopts_only_its_pending_launch(self):
        mine = GceVm("oxidex-runners-mine", "asia-east2-a", "RUNNING", "n4-highcpu-16", True, instance_id="m-1")
        unrelated = GceVm("oxidex-runners-unrelated", "asia-east2-a", "RUNNING", "n4-highcpu-16", True)
        state = self._provision_state(pending_replacement={"name": "oxidex-runners-mine", "zone": "asia-east2-a"})
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.spot_runner_autoscaler.launch_offer") as mock_launch:
                step_price_rotation([self.outgoing_vm, unrelated, mine], [], self.args, state, path)
                mock_launch.assert_not_called()
            recovered = read_scale_state(path)["price_rotation"]
            self.assertEqual(recovered["phase"], "boot")
            self.assertEqual(recovered["replacement_vm"]["name"], "oxidex-runners-mine")
            self.assertEqual(recovered["replacement_vm"]["instance_id"], "m-1")

    def test_provision_resume_waits_for_its_pending_host_operation(self):
        state = self._provision_state(pending_replacement={"name": "oxidex-runners-mine", "zone": "asia-east2-a"})
        state["host_operation"] = {"kind": "launch", "vm": {"name": "oxidex-runners-mine", "zone": "asia-east2-a"}}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.spot_runner_autoscaler.launch_offer") as mock_launch:
                self.assertTrue(step_price_rotation([self.outgoing_vm], [], self.args, state, path))
                mock_launch.assert_not_called()
            self.assertEqual(state["price_rotation"]["phase"], "provision")

    def test_launch_offer_persists_rotation_intent_with_host_operation_and_returns_vm(self):
        from lib.spot_runner_autoscaler import launch_offer
        offer = SimpleNamespace(machine_type="n2-standard-2", vcpu=2, ram_gib=8.0, hourly_usd=0.05,
                                total_daily_usd=Decimal("1.20"), region="asia-east2", zone="asia-east2-a",
                                cpu_platform="Intel Cascade Lake")
        args = SimpleNamespace(project="homelab-424523", org="swack-tools", target=None,
                               max_daily_usd=Decimal("4.00"), maximum_hosts=5, disk_size_gb=100, label="spot")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            args.state_file = path
            state = self._provision_state()
            writes = []
            real_write = __import__("lib.spot_runner_autoscaler", fromlist=["write_scale_state"]).write_scale_state

            def record_write(target, value):
                writes.append(json.loads(json.dumps(value)))
                real_write(target, value)

            with patch("lib.spot_runner_autoscaler.guard_fleet_mutation"), \
                    patch("lib.spot_runner_autoscaler.write_scale_state", side_effect=record_write), \
                    patch("lib.spot_runner_autoscaler.platform_preferences", return_value=[(1, "Intel Cascade Lake")]), \
                    patch("lib.spot_runner_autoscaler.machine_profile", return_value=SimpleNamespace(disk_type="pd-standard")), \
                    patch("lib.spot_runner_autoscaler.subprocess.run",
                          return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
                launched = launch_offer(offer, args, state=state, state_path=path)
            self.assertIsInstance(launched, GceVm)
            intent = next(w for w in writes if w.get("host_operation"))
            self.assertEqual(intent["price_rotation"]["pending_replacement"]["name"], intent["host_operation"]["vm"]["name"])
            self.assertEqual(launched.name, intent["host_operation"]["vm"]["name"])
            self.assertEqual(launched.zone, "asia-east2-a")

    def test_boot_timeout_never_deletes_a_same_named_vm_with_another_identity(self):
        from lib.spot_runner_autoscaler import _step_rotation_boot
        impostor = GceVm("oxidex-runners-new", "asia-east2-a", "PROVISIONING", "n4-highcpu-16", True,
                         instance_id="someone-else")
        rotation = {
            "phase": "boot",
            "target": "swack-tools",
            "outgoing_vm": {"name": "oxidex-runners-old", "zone": "asia-east2-a"},
            "replacement_vm": {"name": "oxidex-runners-new", "zone": "asia-east2-a", "instance_id": "new-456"},
            "phase_started_at": 1000.0,
        }
        state = {"price_rotation": rotation}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.spot_runner_autoscaler.delete_vm_and_disks") as mock_delete, \
                    patch("lib.spot_runner_autoscaler.now", return_value=1700.0):
                _step_rotation_boot([self.outgoing_vm, impostor], [], self.args, state, path, rotation)
            mock_delete.assert_not_called()
            self.assertNotIn("price_rotation", read_scale_state(path))

    def test_cordon_drain_target_aware_repo_vs_org(self):
        # Target repo: label removal only, runnergroup never touched
        target_repo = "xbmc4lyfe/newznab-tmux"
        runner_repo = GitHubRunner(10, "gha-oxidex-runners-old-1", "online", False, ("spot",))
        self.assertTrue(runner_drain.is_repo_target(target_repo))
        operation = {"drain_group_name": "spot-drain-test", "drain_group_id": None}
        with self.assertRaises(RuntimeError):
            runner_drain.isolated_group(operation, target_repo, lambda: None)

        # Target org: isolated group permitted
        target_org = "swack-tools"
        self.assertFalse(runner_drain.is_repo_target(target_org))

    def test_cordon_drain_holds_while_jobs_busy(self):
        busy_runner = GitHubRunner(99, "gha-oxidex-runners-old-1", "online", True, ("spot",))
        state = {
            "price_rotation": {
                "phase": "cordon_drain",
                "target": "swack-tools",
                "target_type": "org",
                "outgoing_vm": {"name": "oxidex-runners-old", "zone": "asia-east2-a", "instance_id": "old-123"},
                "replacement_vm": {"name": "oxidex-runners-new", "zone": "asia-east2-a", "instance_id": "new-456"},
                "cordon_complete": True,
                "drain_started_at": 2000000000.0,
                "phase_started_at": 2000000000.0,
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.runner_drain.jobs_finished", return_value=False), \
                    patch("lib.spot_runner_autoscaler.now", return_value=2000000010.0), \
                    patch("lib.spot_runner_autoscaler.delete_overpriced_vm") as mock_delete:
                step_price_rotation([self.outgoing_vm], [busy_runner], self.args, state, path)
                mock_delete.assert_not_called()
                recovered = read_scale_state(path)
                self.assertEqual(recovered["price_rotation"]["phase"], "cordon_drain")

    def test_complete_rotation_deregisters_and_deletes_vm_and_disks(self):
        idle_runner = GitHubRunner(99, "gha-oxidex-runners-old-1", "online", False, ("spot",))
        state = {
            "price_rotation": {
                "phase": "cordon_drain",
                "target": "xbmc4lyfe/newznab-tmux",
                "target_type": "repo",
                "outgoing_vm": {"name": "oxidex-runners-old", "zone": "asia-east2-a", "instance_id": "old-123"},
                "replacement_vm": {"name": "oxidex-runners-new", "zone": "asia-east2-a", "instance_id": "new-456"},
                "cordon_complete": True,
                "drain_started_at": 2000000000.0,
                "phase_started_at": 2000000000.0,
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.runner_drain.jobs_finished", return_value=True), \
                    patch("lib.spot_runner_autoscaler.now", return_value=2000000010.0), \
                    patch("lib.runner_drain.api") as mock_api, \
                    patch("lib.runner_drain.subprocess.run") as mock_delete_cmd:
                # Step 1: Drain finishes -> advances to deregister
                step_price_rotation([self.outgoing_vm], [idle_runner], self.args, state, path)
                self.assertEqual(read_scale_state(path)["price_rotation"]["phase"], "deregister")

                # Step 2: Deregister runner -> advances to delete
                step_price_rotation([self.outgoing_vm], [idle_runner], self.args, state, path)
                mock_api.assert_called_with("xbmc4lyfe/newznab-tmux", "DELETE", "repos/xbmc4lyfe/newznab-tmux/actions/runners/99")
                self.assertEqual(read_scale_state(path)["price_rotation"]["phase"], "delete")

                # Step 3: Deletes old VM and all attached disks
                step_price_rotation([self.outgoing_vm], [], self.args, state, path)
                mock_delete_cmd.assert_called_once_with([
                    "gcloud", "compute", "instances", "delete", "oxidex-runners-old",
                    "--zone=asia-east2-a", "--project=homelab-424523", "--delete-disks=all", "--quiet",
                ], check=True, text=True, capture_output=True, timeout=180)
                # Transaction completed and cleared
                self.assertNotIn("price_rotation", read_scale_state(path))

    def test_launch_offer_propagates_target_argument(self):
        from lib.spot_runner_autoscaler import launch_offer
        offer = SimpleNamespace(
            machine_type="n2-standard-2",
            vcpu=2,
            ram_gib=8.0,
            hourly_usd=0.05,
            total_daily_usd=Decimal("1.20"),
            region="asia-east2",
            zone="asia-east2-a",
            cpu_platform="Intel Cascade Lake",
        )
        args_with_target = SimpleNamespace(
            project="homelab-424523",
            org="swack-tools",
            target="xbmc4lyfe/newznab-tmux",
            max_daily_usd=Decimal("4.00"),
            maximum_hosts=5,
            disk_size_gb=100,
            dashboard_port=8080,
            make_before_break=True,
            label="spot",
            state_file=Path("/tmp/state.json"),
        )
        with patch("lib.spot_runner_autoscaler.guard_fleet_mutation"), \
             patch("lib.spot_runner_autoscaler.platform_preferences", return_value=[(1, "Intel Cascade Lake")]), \
             patch("lib.spot_runner_autoscaler.machine_profile", return_value=SimpleNamespace(disk_type="pd-standard")), \
             patch("lib.spot_runner_autoscaler.subprocess.run") as mock_sub:
            mock_sub.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            launch_offer(offer, args_with_target, state={}, state_path=Path("/tmp/state.json"))
            cmd = mock_sub.call_args[0][0]
            self.assertIn("--target", cmd)
            self.assertEqual(cmd[cmd.index("--target") + 1], "xbmc4lyfe/newznab-tmux")

    def test_provisioning_failure_falls_back_to_select_after_max_attempts(self):
        rotation = {
            "phase": "provision",
            "target": "swack-tools",
            "outgoing_vm": {"name": "oxidex-runners-old", "zone": "asia-east2-a"},
            "replacement_offer": {
                "machine_type": "n4-highcpu-16",
                "zone": "asia-east2-a",
                "region": "asia-east2",
                "vcpu": 16,
                "ram_gib": 32.0,
                "hourly_usd": 0.15,
                "total_daily_usd": 3.60,
            },
            "phase_started_at": 1000.0,
            "attempt_count": 0,
        }
        state = {"price_rotation": rotation}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.spot_runner_autoscaler.guard_fleet_mutation"), \
                 patch("lib.spot_runner_autoscaler.launch_offer", side_effect=RuntimeError("ZONE_RESOURCE_POOL_EXHAUSTED")), \
                 patch("lib.spot_runner_autoscaler.now", return_value=1010.0):
                # 3 consecutive provisioning failures
                for _ in range(3):
                    step_price_rotation([self.outgoing_vm], [], self.args, state, path)

            recovered = read_scale_state(path)
            self.assertEqual(recovered["price_rotation"]["phase"], "select")
            self.assertNotIn("replacement_offer", recovered["price_rotation"])

    def test_boot_hydrate_timeout_on_non_running_vm_aborts_cleanly(self):
        from lib.spot_runner_autoscaler import _step_rotation_boot_hydrate
        stuck_repl_vm = GceVm(
            "oxidex-runners-new", "asia-east2-a", "PROVISIONING", "n4-highcpu-16", True,
            instance_id="new-456"
        )
        rotation = {
            "phase": "boot",
            "target": "swack-tools",
            "outgoing_vm": {"name": "oxidex-runners-old", "zone": "asia-east2-a"},
            "replacement_vm": {"name": "oxidex-runners-new", "zone": "asia-east2-a"},
            "phase_started_at": 1000.0,
        }
        state = {"price_rotation": rotation}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.spot_runner_autoscaler.delete_vm_and_disks") as mock_delete, \
                 patch("lib.spot_runner_autoscaler.now", return_value=1700.0):
                result = _step_rotation_boot_hydrate(
                    [self.outgoing_vm, stuck_repl_vm], [], self.args, state, path, rotation
                )
                self.assertTrue(result)
                mock_delete.assert_called_once_with(stuck_repl_vm, self.args.project)
                self.assertNotIn("price_rotation", read_scale_state(path))

    def test_boot_hydrate_timeout_survives_gcloud_deletion_error(self):
        from lib.spot_runner_autoscaler import _step_rotation_boot_hydrate
        new_vm = GceVm("oxidex-runners-new", "asia-east2-a", "PROVISIONING", "n2-standard-2", True)
        state = {
            "price_rotation": {
                "phase": "boot_hydrate",
                "replacement_vm": {"name": "oxidex-runners-new", "zone": "asia-east2-a"},
                "phase_started_at": 1000.0,
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            with patch("lib.spot_runner_autoscaler.delete_vm_and_disks", side_effect=subprocess.CalledProcessError(1, ["gcloud"])), \
                 patch("lib.spot_runner_autoscaler.now", return_value=1700.0):
                res = _step_rotation_boot_hydrate([new_vm], [], self.args, state, path, state["price_rotation"])
                self.assertTrue(res)
                recovered = read_scale_state(path)
                self.assertNotIn("price_rotation", recovered)

    def test_verify_readiness_timeout_on_preempted_vm_aborts_cleanly(self):
        from lib.spot_runner_autoscaler import _step_rotation_verify_readiness
        preempted_repl_vm = GceVm(
            "oxidex-runners-new", "asia-east2-a", "TERMINATED", "n4-highcpu-16", True,
            instance_id="new-456"
        )
        rotation = {
            "phase": "verify_readiness",
            "target": "swack-tools",
            "outgoing_vm": {"name": "oxidex-runners-old", "zone": "asia-east2-a"},
            "replacement_vm": {"name": "oxidex-runners-new", "zone": "asia-east2-a", "ip": "10.0.0.2"},
            "phase_started_at": 1000.0,
        }
        state = {"price_rotation": rotation}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.spot_runner_autoscaler.delete_vm_and_disks") as mock_delete, \
                 patch("lib.spot_runner_autoscaler.now", return_value=2000.0):
                result = _step_rotation_verify_readiness(
                    [self.outgoing_vm, preempted_repl_vm], [], self.args, state, path, rotation
                )
                self.assertTrue(result)
                mock_delete.assert_called_once_with(preempted_repl_vm, self.args.project)
                self.assertNotIn("price_rotation", read_scale_state(path))

    def test_cordon_drain_does_not_mark_complete_on_api_error(self):
        runner = GitHubRunner(1, "gha-oxidex-runners-old-1", "online", False, ("spot", "linux"))
        state = {
            "price_rotation": {
                "phase": "cordon_drain",
                "target": "xbmc4lyfe/newznab-tmux",
                "target_type": "repo",
                "outgoing_vm": {"name": "oxidex-runners-old", "zone": "asia-east2-a", "instance_id": "old-123"},
                "replacement_vm": {"name": "oxidex-runners-new", "zone": "asia-east2-a", "instance_id": "new-456"},
                "cordon_complete": False,
                "drain_started_at": 1000.0,
                "phase_started_at": 1000.0,
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.runner_drain.api", side_effect=subprocess.CalledProcessError(1, ["gh", "api"], stderr="403 Forbidden")), \
                 patch("lib.spot_runner_autoscaler.now", return_value=1010.0):
                step_price_rotation([self.outgoing_vm], [runner], self.args, state, path)
                recovered = read_scale_state(path)
                self.assertFalse(recovered["price_rotation"].get("cordon_complete", False))
                self.assertEqual(recovered["price_rotation"]["phase"], "cordon_drain")

    def test_cordon_drain_org_fallback_on_group_error(self):
        out_vm = GceVm("vm-1", "asia-east2-a", "RUNNING", "n2-standard-2", True, instance_id="id-vm-1")
        runner = GitHubRunner(1, "gha-vm-1-1", "online", False, ("spot", "linux"))
        state = {
            "price_rotation": {
                "phase": "cordon_drain",
                "target": "swack-tools",
                "target_type": "org",
                "outgoing_vm": {"name": "vm-1", "zone": "asia-east2-a", "instance_id": "id-vm-1"},
                "replacement_vm": {"name": "vm-repl", "zone": "asia-east2-a", "instance_id": "id-repl"},
                "cordon_complete": False,
                "drain_started_at": 1000.0,
                "phase_started_at": 1000.0,
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            with patch("lib.runner_drain.isolated_group", side_effect=RuntimeError("Group quota reached")), \
                 patch("lib.runner_drain.api", return_value={}):
                res = step_price_rotation([out_vm], [runner], self.args, state, path)
                self.assertTrue(res)
                recovered = read_scale_state(path)
                self.assertTrue(recovered["price_rotation"]["cordon_complete"])
                self.assertEqual(recovered["price_rotation"]["cordon_mode"], "labels")


class EntrypointParityTests(unittest.TestCase):
    def test_entrypoint_character_for_character_parity(self):
        from lib.runner_assets import RUNNER_ENTRYPOINT
        entrypoint_path = Path(__file__).resolve().parent.parent / "runner" / "entrypoint.sh"
        self.assertTrue(entrypoint_path.exists())
        tracked_content = entrypoint_path.read_text(encoding="utf-8")
        self.assertEqual(tracked_content, RUNNER_ENTRYPOINT)

    def test_entrypoint_conditional_runnergroup_assembling(self):
        from lib.runner_assets import RUNNER_ENTRYPOINT
        self.assertIn('if [[ -n "${RUNNER_GROUP_NAME:-}" ]]; then', RUNNER_ENTRYPOINT)
        self.assertIn('config_args+=(--runnergroup "$RUNNER_GROUP_NAME")', RUNNER_ENTRYPOINT)
        self.assertIn('./config.sh "${config_args[@]}"', RUNNER_ENTRYPOINT)


class RunnerFleetListingTargetTests(unittest.TestCase):
    def _mock_runner_payload(self):
        page = {
            "total_count": 1,
            "runners": [
                {"id": 42, "name": "gha-test-1", "status": "online", "busy": False, "labels": [{"name": "spot"}]}
            ]
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps([page]), stderr="")

    def test_list_github_runners_org_target(self):
        from lib.runner_fleet import list_github_runners
        with patch("lib.runner_fleet.subprocess.run", return_value=self._mock_runner_payload()) as mock_sub:
            runners = list_github_runners("swack-tools")
            cmd = mock_sub.call_args[0][0]
            self.assertIn("orgs/swack-tools/actions/runners?per_page=100", cmd)
            self.assertEqual(len(runners), 1)
            self.assertEqual(runners[0].name, "gha-test-1")

    def test_list_github_runners_repo_target(self):
        from lib.runner_fleet import list_github_runners
        with patch("lib.runner_fleet.subprocess.run", return_value=self._mock_runner_payload()) as mock_sub:
            runners = list_github_runners("xbmc4lyfe/newznab-tmux")
            cmd = mock_sub.call_args[0][0]
            self.assertIn("repos/xbmc4lyfe/newznab-tmux/actions/runners?per_page=100", cmd)
            self.assertEqual(len(runners), 1)
            self.assertEqual(runners[0].name, "gha-test-1")

    def test_list_github_runners_target_config_repo(self):
        from lib.runner_fleet import list_github_runners
        target = TargetConfig("xbmc4lyfe/newznab-tmux")
        with patch("lib.runner_fleet.subprocess.run", return_value=self._mock_runner_payload()) as mock_sub:
            runners = list_github_runners(target)
            cmd = mock_sub.call_args[0][0]
            self.assertIn("repos/xbmc4lyfe/newznab-tmux/actions/runners?per_page=100", cmd)
            self.assertEqual(len(runners), 1)

    def test_list_github_runners_target_config_org(self):
        from lib.runner_fleet import list_github_runners
        target = TargetConfig("swack-tools")
        with patch("lib.runner_fleet.subprocess.run", return_value=self._mock_runner_payload()) as mock_sub:
            runners = list_github_runners(target)
            cmd = mock_sub.call_args[0][0]
            self.assertIn("orgs/swack-tools/actions/runners?per_page=100", cmd)
            self.assertEqual(len(runners), 1)

    def test_list_github_runners_keyword_target(self):
        from lib.runner_fleet import list_github_runners
        with patch("lib.runner_fleet.subprocess.run", return_value=self._mock_runner_payload()) as mock_sub:
            runners = list_github_runners(target="xbmc4lyfe/newznab-tmux")
            cmd = mock_sub.call_args[0][0]
            self.assertIn("repos/xbmc4lyfe/newznab-tmux/actions/runners?per_page=100", cmd)
            self.assertEqual(len(runners), 1)

    def test_count_active_github_runners_repo_target(self):
        from lib.runner_fleet import count_active_github_runners
        with patch("lib.runner_fleet.subprocess.run", return_value=self._mock_runner_payload()):
            count = count_active_github_runners(target="xbmc4lyfe/newznab-tmux")
            self.assertEqual(count, 1)

    def test_observe_fleet_passes_repo_target(self):
        from lib.runner_fleet import observe_fleet
        with patch("lib.runner_fleet.list_github_runners", return_value=()) as mock_list, \
             patch("lib.runner_fleet.list_gcloud_vms", return_value=()):
            observe_fleet(target="xbmc4lyfe/newznab-tmux")
            mock_list.assert_called_once_with("xbmc4lyfe/newznab-tmux", gh_binary="gh")


if __name__ == "__main__":
    unittest.main()

