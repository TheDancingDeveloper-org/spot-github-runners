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
        # 2 VMs present, floor is 1: a removal is allowed and every idle host stays a candidate so
        # builder quiescence can choose; scale_down still removes only one host per step.
        removals = removable_spot_vms(vms, runners, state=state)
        self.assertEqual(removals, [vm1, vm2])

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
        # Fresh evidence that the replacement is RUNNING with its runner online.
        self.replacement_runner = GitHubRunner(500, "gha-oxidex-runners-new-1", "online", False, ("spot",))
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
                "expected_runner_count_source": "launch_sizing",
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
                step_price_rotation([self.outgoing_vm, self.replacement_vm], [self.replacement_runner],
                                    self.args, state, path)
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
            "expected_runner_count": 1, "expected_runner_count_source": "launch_sizing",
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
                "expected_runner_count": 1, "expected_runner_count_source": "launch_sizing",
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
                step_price_rotation([self.outgoing_vm, self.replacement_vm], [busy_runner, self.replacement_runner],
                                    self.args, state, path)
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
                "expected_runner_count": 1, "expected_runner_count_source": "launch_sizing",
                "cordon_complete": True,
                "drain_started_at": 2000000000.0,
                "phase_started_at": 2000000000.0,
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.runner_drain.jobs_finished", return_value=True), \
                    patch("lib.spot_runner_autoscaler.now", return_value=2000000010.0), \
                    patch("lib.runner_drain.api", side_effect=lambda target, method, path, body=None: (
                        {"name": "gha-oxidex-runners-old-1", "busy": False, "status": "online"}
                        if method == "GET" else {})) as mock_api, \
                    patch("lib.spot_runner_autoscaler.quiesce_remote_builder", return_value=True) as quiesce, \
                    patch("lib.runner_drain.subprocess.run") as mock_delete_cmd:
                # Step 1: Drain finishes -> advances to deregister
                live = [self.outgoing_vm, self.replacement_vm]
                step_price_rotation(live, [idle_runner, self.replacement_runner], self.args, state, path)
                self.assertEqual(read_scale_state(path)["price_rotation"]["phase"], "deregister")

                # Step 2: Deregister runner -> advances to delete
                step_price_rotation(live, [idle_runner, self.replacement_runner], self.args, state, path)
                mock_api.assert_called_with("xbmc4lyfe/newznab-tmux", "DELETE", "repos/xbmc4lyfe/newznab-tmux/actions/runners/99")
                self.assertEqual(read_scale_state(path)["price_rotation"]["phase"], "delete")

                # Step 3: Deletes old VM and all attached disks
                step_price_rotation(live, [self.replacement_runner], self.args, state, path)
                mock_delete_cmd.assert_called_once_with([
                    "gcloud", "compute", "instances", "delete", "oxidex-runners-old",
                    "--zone=asia-east2-a", "--project=homelab-424523", "--delete-disks=all", "--quiet",
                ], check=True, text=True, capture_output=True, timeout=180)
                # Transaction completed and cleared
                self.assertNotIn("price_rotation", read_scale_state(path))
                # Builder quiescence was confirmed before deregistration and again before deletion.
                self.assertEqual(quiesce.call_count, 2)

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
                "expected_runner_count": 1, "expected_runner_count_source": "launch_sizing",
                "cordon_complete": False,
                "drain_started_at": 1000.0,
                "phase_started_at": 1000.0,
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.runner_drain.api", side_effect=subprocess.CalledProcessError(1, ["gh", "api"], stderr="403 Forbidden")), \
                 patch("lib.spot_runner_autoscaler.now", return_value=1010.0):
                step_price_rotation([self.outgoing_vm, self.replacement_vm], [runner, self.replacement_runner],
                                    self.args, state, path)
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
                "replacement_vm": {"name": "oxidex-runners-new", "zone": "asia-east2-a", "instance_id": "new-456"},
                "expected_runner_count": 1, "expected_runner_count_source": "launch_sizing",
                "cordon_complete": False,
                "drain_started_at": 1000.0,
                "phase_started_at": 1000.0,
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            with patch("lib.runner_drain.isolated_group", side_effect=RuntimeError("Group quota reached")), \
                 patch("lib.runner_drain.api", return_value={}):
                res = step_price_rotation([out_vm, self.replacement_vm], [runner, self.replacement_runner],
                                          self.args, state, path)
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



class TargetOwnershipTests(unittest.TestCase):
    """Finding 1: a controller only scales, recovers, or rotates hosts owned by its target."""

    def setUp(self):
        from lib.target_config import target_label_value
        self.repo_label = target_label_value("xbmc4lyfe/newznab-tmux")
        self.org_label = target_label_value("swack-tools")
        self.org_host = GceVm("oxidex-runners-org", "asia-east2-a", "RUNNING", "n4-highcpu-16", True,
                              instance_id="org-1")
        self.labeled_org_host = GceVm("oxidex-runners-org2", "asia-east2-a", "RUNNING", "n4-highcpu-16", True,
                                      instance_id="org-2", target_label=self.org_label)
        self.repo_host = GceVm("oxidex-runners-repo", "asia-east2-a", "RUNNING", "n4-highcpu-16", True,
                               instance_id="repo-1", target_label=self.repo_label)
        self.repo_args = SimpleNamespace(org="swack-tools", target="xbmc4lyfe/newznab-tmux")
        self.org_args = SimpleNamespace(org="swack-tools", target=None)

    def test_label_value_is_gce_safe_case_insensitive_and_distinct(self):
        from lib.target_config import target_label_value
        values = {target_label_value(name) for name in
                  ("swack-tools", "xbmc4lyfe/newznab-tmux", "owner/re.po", "owner/re_po", "owner--re/po")}
        self.assertEqual(len(values), 5)
        for value in values | {target_label_value("a" * 39 + "/" + "b" * 100)}:
            self.assertLessEqual(len(value), 63)
            self.assertRegex(value, r"^[a-z0-9_-]+$")
        self.assertEqual(target_label_value("XBMC4lyfe/NewzNab-tmux"), self.repo_label)

    def test_unlabeled_hosts_belong_only_to_the_default_org_target(self):
        from lib.target_config import target_owns_vm_label
        self.assertTrue(target_owns_vm_label("", "swack-tools", "swack-tools"))
        self.assertFalse(target_owns_vm_label("", "xbmc4lyfe/newznab-tmux", "swack-tools"))
        self.assertFalse(target_owns_vm_label("", "other-org", "swack-tools"))
        self.assertTrue(target_owns_vm_label(self.repo_label, "xbmc4lyfe/newznab-tmux"))
        self.assertFalse(target_owns_vm_label(self.repo_label, "swack-tools"))
        self.assertFalse(target_owns_vm_label(self.org_label, "xbmc4lyfe/newznab-tmux"))

    def test_list_gcloud_vms_reads_target_label(self):
        from lib import runner_fleet
        rows = [{"name": "oxidex-runners-repo", "zone": "zones/asia-east2-a", "status": "RUNNING",
                 "machineType": "machineTypes/n4-highcpu-16", "id": "1",
                 "scheduling": {"provisioningModel": "SPOT"}, "labels": {"spot-target": self.repo_label}},
                {"name": "oxidex-runners-old", "zone": "zones/asia-east2-a", "status": "RUNNING",
                 "machineType": "machineTypes/n4-highcpu-16", "id": "2",
                 "scheduling": {"provisioningModel": "SPOT"}}]
        responses = {"instances": rows, "disks": [], "machine-types": {"guestCpus": 16, "memoryMb": 32768}}
        with patch("lib.runner_fleet._command_json", side_effect=lambda args: responses[args[2]]):
            vms = runner_fleet.list_gcloud_vms("p", name_prefix="oxidex-runners-")
        self.assertEqual([vm.target_label for vm in vms], [self.repo_label, ""])

    def test_repo_controller_scopes_out_org_hosts(self):
        from lib.spot_runner_autoscaler import scope_fleet_to_target
        vms = [self.org_host, self.labeled_org_host, self.repo_host]
        self.assertEqual(scope_fleet_to_target(vms, self.repo_args), (self.repo_host,))
        self.assertEqual(scope_fleet_to_target(vms, self.org_args), (self.org_host, self.labeled_org_host))
        explicit_org = SimpleNamespace(org="swack-tools", target="swack-tools")
        self.assertEqual(scope_fleet_to_target(vms, explicit_org), (self.org_host, self.labeled_org_host))

    def test_scoping_forgets_foreign_observations_so_they_are_never_recovered(self):
        from lib.fleet_logging import observe_vms
        from lib.spot_runner_autoscaler import scope_fleet_to_target
        state = {}
        observe_vms([self.org_host, self.repo_host], state)  # pre-upgrade unscoped observation
        self.assertIn("asia-east2-a/oxidex-runners-org", state["observed_vms"])
        owned = scope_fleet_to_target([self.org_host, self.repo_host], self.repo_args, state)
        observe_vms(owned, state)
        self.assertEqual(state["pending_vm_recovery"], {})
        self.assertEqual(list(state["observed_vms"]), ["asia-east2-a/oxidex-runners-repo"])

    def test_scoping_abandons_rotation_of_a_foreign_outgoing_host(self):
        from lib.spot_runner_autoscaler import scope_fleet_to_target
        state = {"price_rotation": {"phase": "cordon_drain", "target": "xbmc4lyfe/newznab-tmux",
                                    "outgoing_vm": {"name": "oxidex-runners-org", "zone": "asia-east2-a",
                                                    "instance_id": "org-1"}}}
        scope_fleet_to_target([self.org_host, self.repo_host], self.repo_args, state)
        self.assertNotIn("price_rotation", state)

    def test_repo_controller_loop_never_rotates_an_org_host(self):
        from contextlib import ExitStack
        from lib import spot_runner_autoscaler as controller
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            args = controller.args_parser().parse_args(
                ["--once", "--target", "xbmc4lyfe/newznab-tmux", "--make-before-break",
                 "--state-file", str(Path(directory) / "state.json")])
            for name, value in [("clear_catalog_cache", None), ("load_cpu_platform_map", {}),
                                ("list_github_runners", []),
                                ("list_gcloud_vms", (self.org_host, self.repo_host)),
                                ("log_hourly_spend", None), ("read_utilization", None)]:
                stack.enter_context(patch.object(controller, name, return_value=value))
            stack.enter_context(patch.object(controller, "JobRecovery"))
            prices = stack.enter_context(patch.object(controller, "check_running_prices", return_value=[]))
            controller._run_controller(args)
            checked = prices.call_args[0][0]
            self.assertEqual([vm.name for vm in checked], ["oxidex-runners-repo"])

    def test_runner_manager_labels_new_hosts_with_their_target(self):
        from lib.runner_manager import apply_startup, args_parser as manager_parser, select_sizing
        from lib.target_config import target_label_value
        for options, expected in ((["--target", "xbmc4lyfe/newznab-tmux"], self.repo_label),
                                  ([], target_label_value("swack-tools"))):
            with self.subTest(options=options):
                args = manager_parser().parse_args(["--max-cost", ".2", "--instance-type", "n2-standard-32",
                                                    "--ram", "128", "--instance-name", "test-vm", *options])
                select_sizing(args)
                calls = []

                def cloud(command, **kwargs):
                    calls.append(command)
                    return "[]" if command[:3] == ["compute", "disks", "list"] else ""
                missing = SimpleNamespace(returncode=1, stdout="")
                with patch("lib.runner_manager.subprocess.run", side_effect=[missing, missing]), \
                        patch("lib.runner_manager.gcloud", side_effect=cloud):
                    apply_startup(args, "zone-a", "safe-test-script")
                create = next(c for c in calls if c[:3] == ["compute", "instances", "create"])
                [labels] = [a for a in create if a.startswith("--labels=")]
                pairs = dict(p.split("=", 1) for p in labels[len("--labels="):].split(","))
                self.assertEqual(pairs["spot-target"], expected)
                self.assertEqual(set(pairs), {"spot-target", "spot-runner-patch"})  # plus the recorded-patch label


def replace_vm(vm, **changes):
    from dataclasses import replace
    return replace(vm, **changes)


class RotationHarness:
    """Shared fixtures for retirement-phase rotation tests."""

    def setUp(self):
        self.outgoing_vm = GceVm("oxidex-runners-old", "asia-east2-a", "RUNNING", "n4-highcpu-16", True,
                                 instance_id="old-123")
        self.replacement_vm = GceVm("oxidex-runners-new", "asia-east2-a", "RUNNING", "n4-highcpu-16", True,
                                    instance_id="new-456")
        self.old_runner = GitHubRunner(99, "gha-oxidex-runners-old-1", "online", False, ("self-hosted", "X64"))
        self.new_runner = GitHubRunner(500, "gha-oxidex-runners-new-1", "online", False, ("spot",))
        self.args = SimpleNamespace(project="homelab-424523", org="swack-tools", target="xbmc4lyfe/newznab-tmux",
                                    max_daily_usd=Decimal("4.00"), maximum_hosts=5)
        self.now = 1010.0

    def state(self, phase, target="xbmc4lyfe/newznab-tmux", target_type="repo", **extra):
        rotation = {
            "phase": phase, "target": target, "target_type": target_type,
            "outgoing_vm": {"name": "oxidex-runners-old", "zone": "asia-east2-a", "instance_id": "old-123"},
            "replacement_vm": {"name": "oxidex-runners-new", "zone": "asia-east2-a", "instance_id": "new-456"},
            "expected_runner_count_source": "launch_sizing",
            "expected_runner_count": 1, "readiness": {"runners_online": True},
            "cordon_complete": True, "drain_started_at": 1000.0, "phase_started_at": 1000.0,
            "cordoned_runners": {"99": {"name": "gha-oxidex-runners-old-1", "labels": ["spot", "x64-16"]}},
        }
        rotation.update(extra)
        return {"price_rotation": rotation}

    def step(self, state, vms, runners, api=None, builders_quiet=True, jobs_done=True):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            api_kwargs = {"side_effect": api} if api is not None else {"return_value": {}}
            with patch("lib.runner_drain.api", **api_kwargs) as mock_api, \
                    patch("lib.runner_drain.jobs_finished", return_value=jobs_done), \
                    patch("lib.spot_runner_autoscaler.delete_overpriced_vm") as delete_old, \
                    patch("lib.spot_runner_autoscaler.delete_vm_and_disks") as delete_new, \
                    patch("lib.spot_runner_autoscaler.quiesce_remote_builder", return_value=builders_quiet), \
                    patch("lib.spot_runner_autoscaler.now", return_value=self.now):
                step_price_rotation(vms, runners, self.args, state, path)
            saved = read_scale_state(path) if path.exists() else {}
        return mock_api, delete_old, delete_new, saved


class ReplacementEvidenceTests(RotationHarness, unittest.TestCase):
    """Finding 2: every retirement step needs fresh proof the replacement is live."""

    def test_missing_outgoing_never_claims_similarly_named_host(self):
        """Codex M4 round 6: 'oxidex-runners-old' must not match 'gha-oxidex-runners-old2-1'."""
        from lib.spot_runner_autoscaler import _missing_outgoing_runners
        other_vm = GceVm("oxidex-runners-old2", "asia-east2-a", "RUNNING", "n4-highcpu-16", True, instance_id="o2")
        other = GitHubRunner(42, "gha-oxidex-runners-old2-1", "online", False, ("spot",))
        stale = GitHubRunner(98, "gha-oxidex-runners-old-2", "offline", False, ("spot",))
        rotation = self.state("deregister")["price_rotation"]
        # Recorded identities win when present.
        self.assertEqual(_missing_outgoing_runners("oxidex-runners-old", [self.old_runner, stale, other],
                                                   [other_vm, self.replacement_vm], rotation), [self.old_runner])
        # Without records, only exact -<vm>-<n> names whose longest VM match is the outgoing host.
        rotation.pop("cordoned_runners")
        self.assertEqual(_missing_outgoing_runners("oxidex-runners-old", [self.old_runner, stale, other],
                                                   [other_vm, self.replacement_vm], rotation),
                         [self.old_runner, stale])

    def test_deregister_with_missing_outgoing_leaves_other_host_alone(self):
        other_vm = GceVm("oxidex-runners-old2", "asia-east2-a", "RUNNING", "n4-highcpu-16", True, instance_id="o2")
        other = GitHubRunner(42, "gha-oxidex-runners-old2-1", "offline", False, ("spot",))  # deletable if claimed
        state = self.state("deregister")
        state["price_rotation"].pop("cordoned_runners")  # force the name-based fallback
        names = {99: self.old_runner.name, 42: other.name}

        def github(target, method, path, body=None, **kwargs):
            if method == "GET":
                return {"name": names[int(path.rsplit("/", 1)[1])], "busy": False}
            return {}

        mock_api, _, _, _ = self.step(state, [self.replacement_vm, other_vm],
                                      [self.old_runner, other, self.new_runner], api=github)
        deletes = [c.args[2] for c in mock_api.call_args_list if c.args[1] == "DELETE"]
        self.assertIn("repos/xbmc4lyfe/newznab-tmux/actions/runners/99", deletes)
        self.assertNotIn("repos/xbmc4lyfe/newznab-tmux/actions/runners/42", deletes)

    def test_rollback_releases_rotation_owned_builder_drain(self):
        """Codex M4 round 4: rollback must not leave the outgoing host refusing builds."""
        terminated = replace_vm(self.replacement_vm, status="TERMINATED")
        state = self.state("cordon_drain", builder_drain_token="rotation-old-123")
        with patch("lib.spot_runner_autoscaler.release_remote_builder", return_value=True) as release:
            _, delete_old, _, saved = self.step(state, [self.outgoing_vm, terminated], [self.old_runner])
        release.assert_called_once_with(self.outgoing_vm, self.args, "rotation-old-123")
        delete_old.assert_not_called()
        self.assertNotIn("price_rotation", saved)

    def test_rollback_waits_until_builder_drain_is_released(self):
        terminated = replace_vm(self.replacement_vm, status="TERMINATED")
        state = self.state("cordon_drain", builder_drain_token="rotation-old-123")
        with patch("lib.spot_runner_autoscaler.release_remote_builder", return_value=False):
            _, _, delete_new, saved = self.step(state, [self.outgoing_vm, terminated], [self.old_runner])
        delete_new.assert_not_called()
        self.assertEqual(saved["price_rotation"]["builder_drain_token"], "rotation-old-123")  # retried next loop

    def test_cordon_drain_records_token_before_quiescing(self):
        state = self.state("cordon_drain")
        seen = {}

        def quiesce(vm, args, token=None):
            seen["token"] = token
            return True

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.runner_drain.api", return_value={}), \
                    patch("lib.runner_drain.jobs_finished", return_value=True), \
                    patch("lib.spot_runner_autoscaler.quiesce_remote_builder", side_effect=quiesce), \
                    patch("lib.spot_runner_autoscaler.now", return_value=self.now):
                step_price_rotation([self.outgoing_vm, self.replacement_vm], [self.old_runner, self.new_runner],
                                    self.args, state, path)
        self.assertEqual(seen.get("token"), "rotation-old-123")

    def test_drain_scripts_claim_and_release_only_their_own_marker(self):
        from lib import spot_runner_autoscaler as controller
        with patch.object(controller.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            controller.quiesce_remote_builder(self.outgoing_vm, self.args, token="rotation-old-123")
            claim = run.call_args.args[0][-1]
            controller.release_remote_builder(self.outgoing_vm, self.args, "rotation-old-123")
            release = run.call_args.args[0][-1]
        self.assertIn("test -e /run/oxidex-build-draining || printf %s rotation-old-123", claim)
        self.assertIn("flock -w 30 -E 75 /run/oxidex-build.lock", release)
        self.assertIn("old-123", release)  # instance identity is checked first
        self.assertIn("rotation-old-123", release)
        self.assertIn("rm -f /run/oxidex-build-draining", release)

    def test_preempted_replacement_stops_drain_uncordons_and_aborts(self):
        terminated = replace_vm(self.replacement_vm, status="TERMINATED")
        state = self.state("cordon_drain")
        mock_api, delete_old, delete_new, saved = self.step(
            state, [self.outgoing_vm, terminated], [self.old_runner])
        delete_old.assert_not_called()
        mock_api.assert_called_once_with("xbmc4lyfe/newznab-tmux", "PUT",
                                         "repos/xbmc4lyfe/newznab-tmux/actions/runners/99/labels",
                                         {"labels": ["spot", "x64-16"]})
        delete_new.assert_called_once_with(terminated, "homelab-424523")
        self.assertNotIn("price_rotation", saved)

    def test_deregister_requires_replacement_runners_still_online(self):
        offline = GitHubRunner(500, "gha-oxidex-runners-new-1", "offline", False, ("spot",))
        state = self.state("deregister")
        mock_api, delete_old, _, saved = self.step(
            state, [self.outgoing_vm, self.replacement_vm], [self.old_runner, offline])
        self.assertNotIn(("xbmc4lyfe/newznab-tmux", "DELETE", "repos/xbmc4lyfe/newznab-tmux/actions/runners/99"),
                         [c.args for c in mock_api.call_args_list])
        delete_old.assert_not_called()
        self.assertNotIn("price_rotation", saved)

    def test_delete_requires_replacement_with_recorded_instance_id(self):
        impostor = replace_vm(self.replacement_vm, instance_id="someone-else")
        state = self.state("delete")
        _, delete_old, delete_new, saved = self.step(state, [self.outgoing_vm, impostor], [self.new_runner])
        delete_old.assert_not_called()
        delete_new.assert_not_called()  # Never delete a VM that is not this rotation's replacement.
        # Outgoing listeners are already gone, so the rotation stays pending for fresh capacity.
        self.assertEqual(saved["price_rotation"]["phase"], "select")
        self.assertEqual(saved["price_rotation"]["failed_replacements"][0]["instance_id"], "new-456")

    def test_missing_replacement_blocks_delete(self):
        state = self.state("delete")
        _, delete_old, _, _ = self.step(state, [self.outgoing_vm], [self.new_runner])
        delete_old.assert_not_called()

    def test_failed_uncordon_holds_recovery_and_retries(self):
        def failing(*args, **kwargs):
            raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")
        state = self.state("cordon_drain")
        _, delete_old, delete_new, saved = self.step(state, [self.outgoing_vm], [self.old_runner], api=failing)
        delete_old.assert_not_called()
        delete_new.assert_not_called()
        self.assertIn("absent", saved["price_rotation"]["recovering"])
        # A recovered replacement does not resume retirement once recovery started.
        mock_api, delete_old, _, saved = self.step(
            saved, [self.outgoing_vm, self.replacement_vm], [self.old_runner, self.new_runner])
        delete_old.assert_not_called()
        self.assertEqual(mock_api.call_args[0][1], "PUT")
        self.assertNotIn("price_rotation", saved)

    def test_org_recovery_restores_original_runner_group(self):
        state = self.state("cordon_drain", target="swack-tools", target_type="org", drain_group_id=77,
                           cordoned_runners={"99": {"name": "gha-oxidex-runners-old-1", "labels": ["spot"],
                                                    "group_id": 10}})
        self.args.target = "swack-tools"
        mock_api, delete_old, _, _ = self.step(state, [self.outgoing_vm], [self.old_runner])
        calls = [c.args for c in mock_api.call_args_list]
        self.assertEqual(calls, [
            ("swack-tools", "PUT", "orgs/swack-tools/actions/runner-groups/10/runners/99"),
            ("swack-tools", "PUT", "orgs/swack-tools/actions/runners/99/labels", {"labels": ["spot"]}),
            ("swack-tools", "DELETE", "orgs/swack-tools/actions/runner-groups/77"),
        ])
        delete_old.assert_not_called()

    def test_busy_replacement_is_kept_when_rotation_aborts(self):
        busy_offline_peer = [GitHubRunner(500, "gha-oxidex-runners-new-1", "online", True, ("spot",)),
                             GitHubRunner(501, "gha-oxidex-runners-new-2", "offline", False, ("spot",))]
        state = self.state("cordon_drain", expected_runner_count=2)
        _, delete_old, delete_new, saved = self.step(
            state, [self.outgoing_vm, self.replacement_vm], [self.old_runner, *busy_offline_peer])
        delete_old.assert_not_called()
        delete_new.assert_not_called()
        self.assertNotIn("price_rotation", saved)

    def test_cordon_records_labels_before_stripping(self):
        labeled = GitHubRunner(99, "gha-oxidex-runners-old-1", "online", False, ("self-hosted", "X64", "spot"))
        state = self.state("cordon_drain", cordon_complete=False, cordoned_runners={})
        mock_api, _, _, saved = self.step(state, [self.outgoing_vm, self.replacement_vm], [labeled, self.new_runner])
        mock_api.assert_called_once_with("xbmc4lyfe/newznab-tmux", "DELETE",
                                         "repos/xbmc4lyfe/newznab-tmux/actions/runners/99/labels")
        self.assertEqual(saved["price_rotation"]["cordoned_runners"],
                         {"99": {"name": "gha-oxidex-runners-old-1", "labels": ["spot"]}})
        self.assertTrue(saved["price_rotation"]["cordon_complete"])

    def test_org_cordon_records_original_group_before_isolation(self):
        state = self.state("cordon_drain", target="swack-tools", target_type="org",
                           cordon_complete=False, cordoned_runners={})
        self.args.target = "swack-tools"
        labeled = GitHubRunner(99, "gha-oxidex-runners-old-1", "online", False, ("self-hosted", "spot"))

        def api(target, method, path, body=None, paginate=False):
            # The individual-runner endpoint has no runner_group_id; membership comes from groups.
            if path == "orgs/swack-tools/actions/runner-groups":
                return [{"runner_groups": [{"id": 1}, {"id": 23}, {"id": 77}]}]
            if path.endswith("/runner-groups/1/runners"):
                return [{"runners": [{"id": 500}]}]
            if path.endswith("/runner-groups/23/runners"):
                return [{"runners": [{"id": 99}]}]
            if path.endswith("/runner-groups/77/runners"):
                raise AssertionError("the drain group must not be consulted")
            return {}
        with patch("lib.runner_drain.isolated_group", return_value="orgs/swack-tools/actions/runner-groups/77"), \
                patch("lib.spot_runner_autoscaler.DEFAULT_RUNNER_GROUP_ID", 10):
            state["price_rotation"]["drain_group_id"] = 77
            mock_api, _, _, saved = self.step(state, [self.outgoing_vm, self.replacement_vm],
                                              [labeled, self.new_runner], api=api)
        self.assertEqual(saved["price_rotation"]["cordoned_runners"]["99"]["group_id"], 23)  # actual, not 10
        self.assertEqual(saved["price_rotation"]["cordon_mode"], "isolated_group")
        self.assertEqual([c.args[1] for c in mock_api.call_args_list][-2:], ["DELETE", "PUT"])

    def test_org_cordon_refuses_to_guess_unknown_group(self):
        state = self.state("cordon_drain", target="swack-tools", target_type="org",
                           cordon_complete=False, cordoned_runners={})
        self.args.target = "swack-tools"
        labeled = GitHubRunner(99, "gha-oxidex-runners-old-1", "online", False, ("self-hosted", "spot"))

        def api(target, method, path, body=None, paginate=False):
            if path == "orgs/swack-tools/actions/runner-groups":
                return [{"runner_groups": [{"id": 1}]}]
            if path.endswith("/runners") and method == "GET":
                return [{"runners": []}]
            return {}
        with patch("lib.runner_drain.isolated_group", return_value="orgs/swack-tools/actions/runner-groups/77"):
            mock_api, _, _, saved = self.step(state, [self.outgoing_vm, self.replacement_vm],
                                              [labeled, self.new_runner], api=api)
        self.assertNotIn("99", saved["price_rotation"].get("cordoned_runners", {}))
        self.assertNotIn("DELETE", [c.args[1] for c in mock_api.call_args_list])  # labels untouched

    def test_delete_never_targets_a_same_named_vm_with_another_identity(self):
        reused = replace_vm(self.outgoing_vm, instance_id="reused-999")
        state = self.state("delete")
        _, delete_old, _, saved = self.step(state, [reused, self.replacement_vm], [self.new_runner])
        delete_old.assert_not_called()
        self.assertNotIn("price_rotation", saved)


class LostCapacityRecoveryTests(RotationHarness, unittest.TestCase):
    """Round 2: once outgoing listeners are deregistered, rollback never strands both hosts."""

    def test_offline_replacement_after_deregistration_is_kept_and_old_host_stays_cordoned(self):
        offline = GitHubRunner(500, "gha-oxidex-runners-new-1", "offline", False, ("spot",))
        state = self.state("deregister", deregistered_runner_ids=[99])
        mock_api, delete_old, delete_new, saved = self.step(
            state, [self.outgoing_vm, self.replacement_vm], [offline])
        mock_api.assert_not_called()  # No pretend uncordon of deregistered listeners.
        delete_old.assert_not_called()
        delete_new.assert_not_called()
        rotation = saved["price_rotation"]
        self.assertTrue(rotation["capacity_lost"])
        self.assertTrue(rotation["recovering"])
        self.assertNotIn("restored", rotation["cordoned_runners"]["99"])
        # Listeners return: recovery clears and retirement resumes on the following check.
        _, _, delete_new, saved = self.step(saved, [self.outgoing_vm, self.replacement_vm], [self.new_runner])
        delete_new.assert_not_called()
        self.assertNotIn("recovering", saved["price_rotation"])
        self.assertEqual(saved["price_rotation"]["phase"], "deregister")
        mock_api, _, _, saved = self.step(saved, [self.outgoing_vm, self.replacement_vm], [self.new_runner])
        self.assertEqual(saved["price_rotation"]["phase"], "delete")

    def test_gone_replacement_after_deregistration_provisions_fresh_capacity(self):
        terminated = replace_vm(self.replacement_vm, status="TERMINATED")
        state = self.state("delete")
        mock_api, delete_old, delete_new, saved = self.step(state, [self.outgoing_vm, terminated], [])
        mock_api.assert_not_called()
        delete_old.assert_not_called()
        delete_new.assert_not_called()
        rotation = saved["price_rotation"]
        self.assertEqual(rotation["phase"], "select")
        self.assertIsNone(rotation["replacement_vm"])
        self.assertTrue(rotation["cordon_complete"])  # Old host stays out of service.

    def test_uncertain_deregistration_counts_as_lost_capacity(self):
        def failing_delete(target, method, path, body=None, paginate=False):
            if method == "GET":
                return {"name": "gha-oxidex-runners-old-1", "busy": False, "status": "online"}
            raise subprocess.CalledProcessError(1, ["gh"], stderr="timeout")
        state = self.state("deregister")
        mock_api, _, _, saved = self.step(state, [self.outgoing_vm, self.replacement_vm],
                                          [self.old_runner, self.new_runner], api=failing_delete)
        self.assertEqual(saved["price_rotation"]["deregistering_runner_id"], 99)
        offline = GitHubRunner(500, "gha-oxidex-runners-new-1", "offline", False, ("spot",))
        mock_api, _, delete_new, saved = self.step(saved, [self.outgoing_vm, self.replacement_vm],
                                                   [self.old_runner, offline])
        mock_api.assert_not_called()
        delete_new.assert_not_called()
        self.assertIn("price_rotation", saved)

    def test_rollback_with_no_online_outgoing_listener_keeps_replacement(self):
        offline_old = GitHubRunner(99, "gha-oxidex-runners-old-1", "offline", False, ("self-hosted",))
        offline_new = GitHubRunner(500, "gha-oxidex-runners-new-1", "offline", False, ("spot",))
        state = self.state("cordon_drain")
        _, _, delete_new, saved = self.step(state, [self.outgoing_vm, self.replacement_vm],
                                            [offline_old, offline_new])
        delete_new.assert_not_called()
        self.assertTrue(saved["price_rotation"]["capacity_lost"])

    def test_fresh_replacement_timeout_in_lost_mode_keeps_transaction(self):
        from lib.spot_runner_autoscaler import _step_rotation_boot
        stuck = replace_vm(self.replacement_vm, status="PROVISIONING")
        state = self.state("boot", capacity_lost=True)
        rotation = state["price_rotation"]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.spot_runner_autoscaler.delete_vm_and_disks") as delete_new, \
                    patch("lib.spot_runner_autoscaler.now", return_value=1000.0 + 10_000):
                _step_rotation_boot([self.outgoing_vm, stuck], [], self.args, state, path, rotation)
            saved = read_scale_state(path)
        delete_new.assert_called_once_with(stuck, "homelab-424523")  # Never had a listener online.
        self.assertEqual(saved["price_rotation"]["phase"], "select")


class RecreatedOutgoingHostTests(RotationHarness, unittest.TestCase):
    """Round 2: a same-named recreated outgoing VM is never cordoned or deregistered."""

    def test_cordon_never_touches_a_recreated_outgoing_instance(self):
        recreated = replace_vm(self.outgoing_vm, instance_id="new-instance")
        state = self.state("cordon_drain", cordon_complete=False, cordoned_runners={})
        mock_api, delete_old, _, saved = self.step(state, [recreated, self.replacement_vm],
                                                   [self.old_runner, self.new_runner])
        mock_api.assert_not_called()
        delete_old.assert_not_called()
        self.assertNotIn("price_rotation", saved)

    def test_deregister_never_touches_a_recreated_outgoing_instance(self):
        recreated = replace_vm(self.outgoing_vm, instance_id="new-instance")
        state = self.state("deregister")
        mock_api, delete_old, _, saved = self.step(state, [recreated, self.replacement_vm],
                                                   [self.old_runner, self.new_runner])
        mock_api.assert_not_called()
        delete_old.assert_not_called()
        self.assertNotIn("price_rotation", saved)


class RotationBuilderQuiescenceTests(RotationHarness, unittest.TestCase):
    """Round 4: rotation never retires a host while a remote build may be running."""

    def test_active_build_holds_deregistration_even_past_drain_deadline(self):
        from lib.spot_runner_autoscaler import DRAIN_DEADLINE_SECONDS
        self.now = 1000.0 + DRAIN_DEADLINE_SECONDS + 60
        state = self.state("cordon_drain")
        for _ in range(2):
            mock_api, delete_old, _, state = self.step(state, [self.outgoing_vm, self.replacement_vm],
                                                       [self.old_runner, self.new_runner], builders_quiet=False)
            mock_api.assert_not_called()
            delete_old.assert_not_called()
            self.assertEqual(state["price_rotation"]["phase"], "cordon_drain")
        _, _, _, state = self.step(state, [self.outgoing_vm, self.replacement_vm],
                                   [self.old_runner, self.new_runner])
        self.assertEqual(state["price_rotation"]["phase"], "deregister")

    def test_delete_requires_builder_quiescence(self):
        state = self.state("delete")
        _, delete_old, _, saved = self.step(state, [self.outgoing_vm, self.replacement_vm], [self.new_runner],
                                            builders_quiet=False)
        delete_old.assert_not_called()
        self.assertEqual(saved["price_rotation"]["phase"], "delete")
        self.assertNotIn("asia-east2-a/oxidex-runners-old", saved.get("intentional_deletions", []))
        _, delete_old, _, saved = self.step(saved, [self.outgoing_vm, self.replacement_vm], [self.new_runner])
        delete_old.assert_called_once()
        self.assertNotIn("price_rotation", saved)


class TrackedJobRepositoryTests(unittest.TestCase):
    """Round 4: job recovery and tier evidence follow a repository target by default."""

    def test_default_follows_target(self):
        from lib.spot_runner_autoscaler import args_parser, tracked_job_repositories
        cases = [([], ["swack-tools/oxidex"]),
                 (["--org", "acme"], ["acme/oxidex"]),
                 (["--target", "xbmc4lyfe/newznab-tmux"], ["xbmc4lyfe/newznab-tmux"]),
                 (["--target", "xbmc4lyfe/newznab-tmux", "--job-repository", "a/b", "--job-repository", "c/d"],
                  ["a/b", "c/d"])]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(tracked_job_repositories(args_parser().parse_args(argv)), expected)

    def test_controller_and_tier_evidence_use_the_same_resolution(self):
        import inspect
        from lib import fleet_migration, spot_runner_autoscaler, tier_controller
        for module in (spot_runner_autoscaler, tier_controller, fleet_migration):
            source = inspect.getsource(module)
            self.assertNotRegex(source, r"job_repository or \[[^\]]")  # No independent org-only fallback.
        self.assertIn("controller.tracked_job_repositories(args)", inspect.getsource(tier_controller.evidence_for))
        self.assertIn("controller.tracked_job_repositories(args)", inspect.getsource(tier_controller.dry_run))

    def test_controller_loop_tracks_repository_target_jobs(self):
        from contextlib import ExitStack
        from lib import spot_runner_autoscaler as controller
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            args = controller.args_parser().parse_args(
                ["--once", "--target", "xbmc4lyfe/newznab-tmux", "--state-file", str(Path(directory) / "s.json")])
            for name, value in [("clear_catalog_cache", None), ("load_cpu_platform_map", {}),
                                ("list_github_runners", []), ("list_gcloud_vms", ()),
                                ("log_hourly_spend", None), ("read_utilization", None),
                                ("check_running_prices", [])]:
                stack.enter_context(patch.object(controller, name, return_value=value))
            recovery = stack.enter_context(patch.object(controller, "JobRecovery"))
            controller._run_controller(args)
        self.assertEqual(recovery.call_args.args[2], ["xbmc4lyfe/newznab-tmux"])

    def test_launch_agent_restart_resolves_the_same_repositories(self):
        import plistlib
        from lib import spot_runner_autoscaler as controller
        for argv in (["--target", "xbmc4lyfe/newznab-tmux"], ["--target", "o/r", "--job-repository", "x/y"]):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as directory:
                args = controller.args_parser().parse_args(argv)
                with patch.object(Path, "home", return_value=Path(directory)), \
                        patch.object(controller.subprocess, "run"):
                    path = controller.install_launch_agent(args)
                program = plistlib.loads(path.read_bytes())["ProgramArguments"]
                restarted = controller.args_parser().parse_args(program[2:])
                self.assertEqual(controller.tracked_job_repositories(restarted),
                                 controller.tracked_job_repositories(args))


class AbortIntentionalDeletionTests(RotationHarness, unittest.TestCase):
    """Round 5: deleting a replacement during abort is never mistaken for a loss."""

    def run_abort(self, state, runners=()):
        from lib.fleet_logging import observe_vms
        from lib.spot_runner_autoscaler import _abort_runner_rotation
        stuck = replace_vm(self.replacement_vm, status="PROVISIONING")
        observe_vms([self.outgoing_vm, stuck], state)  # The replacement was already observed.
        seen = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"

            def deleting(vm, project):
                seen.update(read_scale_state(path))  # What was durable when the delete was sent.
            with patch("lib.spot_runner_autoscaler.delete_vm_and_disks", side_effect=deleting):
                _abort_runner_rotation(state["price_rotation"], [self.outgoing_vm, stuck], self.args,
                                       state, path, "Boot timed out", list(runners))
        key = "asia-east2-a/oxidex-runners-new"
        self.assertIn(key, seen["intentional_deletions"])
        self.assertEqual(seen["intentional_deletion_ids"][key], "new-456")
        observe_vms([self.outgoing_vm], state)  # Next poll: the replacement is gone.
        self.assertEqual(state["pending_vm_recovery"], {})
        return state

    def test_abort_records_intentional_deletion_before_deleting(self):
        state = self.run_abort(self.state("boot"))
        self.assertNotIn("price_rotation", state)

    def test_lost_capacity_restart_records_intentional_deletion(self):
        state = self.run_abort(self.state("boot", capacity_lost=True))
        self.assertEqual(state["price_rotation"]["phase"], "select")


class ExpectedListenerCountTests(unittest.TestCase):
    """Round 5: retirement waits for every listener the replacement launch provisions."""

    def setUp(self):
        self.outgoing_vm = GceVm("oxidex-runners-old", "asia-east2-a", "RUNNING", "n4-highcpu-16", True,
                                 vcpu=16, ram_gib=32.0, instance_id="old-123")
        self.replacement_vm = GceVm("oxidex-runners-new", "asia-east2-a", "RUNNING", "n4-highcpu-16", True,
                                    instance_id="new-456")
        self.offer = SimpleNamespace(region="asia-east2", machine_type="n4-highcpu-16", vcpu=16, ram_gib=32.0,
                                     hourly_usd=0.15, total_daily_usd=Decimal("3.60"), zone="asia-east2-a",
                                     cpu_platform="Intel Emerald Rapids")

    def runners(self, online):
        return [GitHubRunner(500 + i, f"gha-oxidex-runners-new-{i}", "online" if i <= online else "offline",
                             False, ("spot",)) for i in range(1, 9)]

    def test_production_cli_defaults_require_all_eight_listeners(self):
        from lib.spot_runner_autoscaler import (_step_rotation_verify_readiness, args_parser,
                                                launch_slot_count)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            args = args_parser().parse_args(["--make-before-break", "--state-file", str(path)])
            self.assertFalse(hasattr(args, "runner_count"))
            self.assertEqual(launch_slot_count(self.offer, args), 8)
            state = {}
            launched = GceVm("oxidex-runners-new", "asia-east2-a", "PROVISIONING", "n4-highcpu-16", True,
                             instance_id="new-456")
            with patch("lib.spot_runner_autoscaler.launch_offer", return_value=launched), \
                    patch("lib.spot_runner_autoscaler.guard_fleet_mutation"):
                begin_price_rotation(self.outgoing_vm, [self.outgoing_vm], [], args, state, path, offer=self.offer)
            rotation = state["price_rotation"]
            self.assertEqual(rotation["expected_runner_count"], 8)
            rotation.update(phase="verify_readiness", phase_started_at=2000000000.0)
            with patch("lib.spot_runner_autoscaler.now", return_value=2000000010.0):
                _step_rotation_verify_readiness([self.outgoing_vm, self.replacement_vm], self.runners(1),
                                                args, state, path, rotation)
                self.assertEqual(rotation["phase"], "verify_readiness")
                _step_rotation_verify_readiness([self.outgoing_vm, self.replacement_vm], self.runners(8),
                                                args, state, path, rotation)
            self.assertEqual(rotation["phase"], "cordon_drain")

    def test_launch_offer_records_its_own_slot_count_on_the_rotation(self):
        from lib.spot_runner_autoscaler import args_parser, launch_offer
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            args = args_parser().parse_args(["--state-file", str(path)])
            state = {"price_rotation": {"phase": "provision", "expected_runner_count": 1}}
            with patch("lib.spot_runner_autoscaler.guard_fleet_mutation"), \
                    patch("lib.spot_runner_autoscaler.platform_preferences",
                          return_value=[(1, "Intel Emerald Rapids")]), \
                    patch("lib.spot_runner_autoscaler.machine_profile",
                          return_value=SimpleNamespace(disk_type="pd-standard")), \
                    patch("lib.spot_runner_autoscaler.subprocess.run",
                          return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
                launch_offer(self.offer, args, state=state, state_path=path)
            rotation = read_scale_state(path)["price_rotation"]
            self.assertEqual(rotation["expected_runner_count"], state["host_operation"]["expected_runner_count"])
            self.assertEqual(rotation["pending_replacement"]["expected_runner_count"], 8)

    def test_legacy_saved_rotation_placeholder_is_recomputed(self):
        from lib.spot_runner_autoscaler import _replacement_live_failure
        rotation = {"replacement_vm": {"name": "oxidex-runners-new", "zone": "asia-east2-a", "instance_id": "new-456"},
                    "expected_runner_count": 1,
                    "replacement_offer": {"machine_type": "n4-highcpu-16", "vcpu": 16, "ram_gib": 32.0}}
        self.assertIn("1/8", _replacement_live_failure([self.replacement_vm], self.runners(1), rotation))
        self.assertIsNone(_replacement_live_failure([self.replacement_vm], self.runners(8), rotation))


class ReadinessTimeoutPreservesWorkTests(unittest.TestCase):
    """Round 6: a timed-out replacement is deleted only when idle and remote-builder quiesced."""

    def setUp(self):
        self.outgoing_vm = GceVm("oxidex-runners-old", "asia-east2-a", "RUNNING", "n4-highcpu-16", True,
                                 instance_id="old-123")
        self.replacement_vm = GceVm("oxidex-runners-new", "asia-east2-a", "RUNNING", "n4-highcpu-16", True,
                                    instance_id="new-456")
        self.args = SimpleNamespace(project="homelab-424523", org="swack-tools", target="swack-tools")

    def timeout(self, runners, builders_quiet=True):
        from lib.spot_runner_autoscaler import _step_rotation_verify_readiness
        rotation = {"phase": "verify_readiness", "target": "swack-tools", "target_type": "org",
                    "outgoing_vm": {"name": "oxidex-runners-old", "zone": "asia-east2-a", "instance_id": "old-123"},
                    "replacement_vm": {"name": "oxidex-runners-new", "zone": "asia-east2-a", "instance_id": "new-456"},
                    "expected_runner_count": 2, "expected_runner_count_source": "launch_sizing",
                    "phase_started_at": 1000.0}
        state = {"price_rotation": rotation}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.spot_runner_autoscaler.delete_vm_and_disks") as delete_new, \
                    patch("lib.spot_runner_autoscaler.quiesce_remote_builder", return_value=builders_quiet) as quiesce, \
                    patch("lib.spot_runner_autoscaler.now", return_value=1000.0 + 10_000):
                _step_rotation_verify_readiness([self.outgoing_vm, self.replacement_vm], runners,
                                                self.args, state, path, rotation)
            saved = read_scale_state(path)
        self.assertNotIn("price_rotation", saved)
        return delete_new, quiesce, saved

    def test_busy_listener_keeps_replacement_without_quiescing_it(self):
        runners = [GitHubRunner(1, "gha-oxidex-runners-new-1", "online", True, ("spot",)),
                   GitHubRunner(2, "gha-oxidex-runners-new-2", "offline", False, ("spot",))]
        delete_new, quiesce, saved = self.timeout(runners)
        delete_new.assert_not_called()
        quiesce.assert_not_called()  # Never mark a host draining while it is kept as capacity.
        self.assertNotIn("asia-east2-a/oxidex-runners-new", saved.get("intentional_deletions", []))

    def test_active_remote_build_keeps_replacement(self):
        runners = [GitHubRunner(1, "gha-oxidex-runners-new-1", "offline", False, ("spot",))]
        delete_new, quiesce, _ = self.timeout(runners, builders_quiet=False)
        quiesce.assert_called_once_with(self.replacement_vm, self.args)
        delete_new.assert_not_called()

    def test_online_idle_listener_keeps_replacement_for_normal_draining(self):
        """Codex M4 round 2: online listeners stay schedulable, so abort never deletes their host."""
        runners = [GitHubRunner(1, "gha-oxidex-runners-new-1", "online", False, ("spot",))]
        delete_new, quiesce, saved = self.timeout(runners)
        delete_new.assert_not_called()
        quiesce.assert_not_called()
        self.assertNotIn("asia-east2-a/oxidex-runners-new", saved.get("intentional_deletions", []))
        self.assertTrue(saved["replacement_cleanups"]["asia-east2-a/oxidex-runners-new"]["drain"])

    def test_idle_quiesced_replacement_is_deleted_intentionally(self):
        runners = [GitHubRunner(1, "gha-oxidex-runners-new-1", "offline", False, ("spot",))]
        delete_new, quiesce, saved = self.timeout(runners)
        quiesce.assert_called_once()
        delete_new.assert_called_once_with(self.replacement_vm, "homelab-424523")
        self.assertIn("asia-east2-a/oxidex-runners-new", saved["intentional_deletions"])


class RotationLossAccountingTests(unittest.TestCase):
    """Round 6: rotation overlap and replacement losses never inflate restoration targets."""

    def setUp(self):
        self.other = GceVm("oxidex-runners-other", "z", "RUNNING", "n4-highcpu-16", True, instance_id="o-1")
        self.outgoing = GceVm("oxidex-runners-old", "z", "RUNNING", "n4-highcpu-16", True, instance_id="old-1")
        self.replacement = GceVm("oxidex-runners-new", "z", "RUNNING", "n4-highcpu-16", True, instance_id="new-1")
        self.rotation = {"phase": "cordon_drain",
                         "outgoing_vm": {"name": "oxidex-runners-old", "zone": "z", "instance_id": "old-1"},
                         "replacement_vm": {"name": "oxidex-runners-new", "zone": "z", "instance_id": "new-1"}}

    def record(self, vms, lost, rotation=True):
        from dataclasses import asdict
        from lib.vm_recovery import record_loss_restoration_target
        state = {"pending_vm_recovery": {f"z/{lost.name}": asdict(lost)}}
        if rotation:
            state["price_rotation"] = self.rotation
        with tempfile.TemporaryDirectory() as tmpdir:
            record_loss_restoration_target(vms, state, Path(tmpdir) / "state.json")
        self.assertTrue(state["pending_vm_recovery"][f"z/{lost.name}"]["_target_recorded"])
        return state.get("price_replacement_target", 0)

    def test_preempted_rotation_replacement_requests_no_restoration(self):
        self.assertEqual(self.record([self.other, self.outgoing], self.replacement), 0)

    def test_outgoing_loss_during_rotation_ignores_overlap_host(self):
        self.assertEqual(self.record([self.other, self.replacement], self.outgoing), 2)

    def test_pending_launch_replacement_is_rotation_owned(self):
        self.rotation["replacement_vm"] = None
        self.rotation["pending_replacement"] = {"name": "oxidex-runners-new", "zone": "z"}
        self.assertEqual(self.record([self.other, self.outgoing], self.replacement), 0)

    def test_ordinary_losses_are_unchanged_without_rotation(self):
        self.assertEqual(self.record([self.other, self.replacement], self.outgoing, rotation=False), 3)



class RotationOwnedRecoveryRetirementTests(unittest.TestCase):
    """Round 7: a rotation-owned replacement loss never leaves an unclearable recovery record."""

    def test_full_sequence_retires_record_and_scaling_resumes(self):
        from contextlib import ExitStack
        from dataclasses import asdict
        from lib import spot_runner_autoscaler as controller
        from lib.fleet_logging import observe_vms
        from lib.vm_recovery import reconcile_lost_vms
        other = GceVm("oxidex-runners-a", "z", "RUNNING", "n4-standard-32", True, instance_id="1")
        outgoing = GceVm("oxidex-runners-b", "z", "RUNNING", "n4-standard-32", True, instance_id="2")
        replacement = GceVm("oxidex-runners-new", "z", "RUNNING", "n4-standard-32", True, instance_id="3")
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory) / "state.json"
            args = controller.args_parser().parse_args(["--once", "--state-file", str(path)])
            import time as clock
            state = {"runner_inventory_observed_at": clock.time(), "price_rotation": {
                "phase": "verify_readiness", "target": "swack-tools",
                "outgoing_vm": {"name": outgoing.name, "zone": "z", "instance_id": "2"},
                "replacement_vm": {"name": replacement.name, "zone": "z", "instance_id": "3"}}}
            observe_vms([other, outgoing, replacement], state)
            observe_vms([other, outgoing], state)  # The replacement is preempted mid-rotation.
            self.assertEqual(len(state["pending_vm_recovery"]), 1)
            # Ordinary recovery cleans up the lost replacement (disks and runners).
            stack.enter_context(patch("lib.vm_recovery.cleanup_lost_vm", return_value=True))
            stack.enter_context(patch("lib.spot_runner_autoscaler.step_price_rotation", return_value=True))
            self.assertTrue(reconcile_lost_vms([other, outgoing], [], args, state, path))
            self.assertEqual(state.get("price_replacement_target", 0), 0)
            key = state["host_operation"]["recovery_key"]
            state["pending_vm_recovery"][key]["_cleanup_complete"] = True
            state.pop("host_operation")
            state.pop("price_rotation")  # The rotation completed (or aborted) on its own.
            self.assertFalse(reconcile_lost_vms([other, outgoing], [], args, state, path))
            self.assertEqual(state["pending_vm_recovery"], {})
            # A full controller check now reaches the scaling decision.
            for name, value in [("clear_catalog_cache", None), ("load_cpu_platform_map", {}),
                                ("list_github_runners", []), ("list_gcloud_vms", (other, outgoing)),
                                ("log_hourly_spend", None), ("check_running_prices", [])]:
                stack.enter_context(patch.object(controller, name, return_value=value))
            stack.enter_context(patch.object(controller, "JobRecovery"))
            controller.write_scale_state(path, state)  # The controller restarts from durable state.
            metrics = stack.enter_context(patch.object(controller, "read_utilization", return_value=None))
            controller._run_controller(args)
            metrics.assert_called_once()
            self.assertEqual(read_scale_state(path)["pending_vm_recovery"], {})

    def test_legacy_completed_record_without_target_is_cleared_by_price_reconcile(self):
        state = {"pending_vm_recovery": {"z/gone": {"name": "gone", "zone": "z", "_cleanup_complete": True}},
                 "observed_vms": {"z/gone": {}}}
        args = SimpleNamespace(project="p", max_daily_usd=Decimal("4"))
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("lib.spot_runner_autoscaler.check_running_prices", return_value=[]):
            self.assertFalse(reconcile_prices([], [], args, state, Path(tmpdir) / "state.json"))
        self.assertEqual(state["pending_vm_recovery"], {})
        self.assertNotIn("z/gone", state["observed_vms"])


class RotationReselectTests(unittest.TestCase):
    """Round 7: reselect after repeated stockouts skips offers that already failed."""

    def offer(self, zone):
        return SimpleNamespace(region="asia-east2", zone=zone, machine_type="n4-highcpu-16", vcpu=16,
                               ram_gib=32.0, hourly_usd=0.15, total_daily_usd=Decimal("3.60"),
                               cpu_platform="Intel Emerald Rapids")

    def test_failed_offer_is_skipped_until_alternatives_are_exhausted(self):
        offers = [self.offer("asia-east2-a"), self.offer("asia-east2-b")]
        outgoing = GceVm("oxidex-runners-old", "asia-east2-a", "RUNNING", "n4-highcpu-16", True, instance_id="o")
        args = SimpleNamespace(project="p", max_daily_usd=Decimal("4"), maximum_hosts=5)
        state = {"price_rotation": {"phase": "select", "target": "swack-tools",
                                    "outgoing_vm": {"name": outgoing.name, "zone": "asia-east2-a"}}}
        chosen = []

        def launch(offer, *a, **k):
            chosen.append(offer.zone)
            raise RuntimeError("ZONE_RESOURCE_POOL_EXHAUSTED")
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("lib.spot_runner_autoscaler.offer_refresher", return_value=lambda: offers), \
                patch("lib.spot_runner_autoscaler.guard_fleet_mutation"), \
                patch("lib.spot_runner_autoscaler.launch_offer", side_effect=launch):
            path = Path(tmpdir) / "state.json"
            for _ in range(9):
                step_price_rotation([outgoing], [], args, state, path)
        # Three attempts per offer, then the second offer, then history resets to the first.
        self.assertEqual(chosen, ["asia-east2-a"] * 3 + ["asia-east2-b"] * 3 + ["asia-east2-a"] * 3)


class FloorQuiescenceSelectionTests(unittest.TestCase):
    """Round 7: a building first candidate does not block a quiescent second one."""

    def test_scale_down_drains_the_quiescent_host(self):
        from lib import spot_runner_autoscaler as controller
        vms = [GceVm(f"oxidex-runners-vm{i}", "z", "RUNNING", "n2-standard-2", True, instance_id=f"vm-{i}")
               for i in (1, 2, 3)]
        runners = [GitHubRunner(i, f"gha-oxidex-runners-vm{i}-1", "online", False, ("spot",)) for i in (1, 2, 3)]
        state = {"min_runner_floor": 2,
                 "host_deployments": {f"z/vm-{i}": {"expected_runner_count": 1} for i in (1, 2, 3)}}
        self.assertEqual(removable_spot_vms(vms, runners, state=state), vms)
        self.assertEqual(removable_spot_vms(vms[:2], runners[:2], state=state), [])
        args = SimpleNamespace(scaling_mode="legacy", state_file=Path("/nonexistent"))
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(controller, "quiesce_remote_builder",
                             side_effect=lambda vm, a: vm.name != "oxidex-runners-vm1"), \
                patch.object(controller, "reconcile_host_operation"):
            controller.scale_down(vms, runners, args, state=state, state_path=Path(tmpdir) / "s.json")
        self.assertEqual(state["host_operation"]["vm"]["name"], "oxidex-runners-vm2")

    def test_scale_down_honors_state_floor(self):
        from lib import spot_runner_autoscaler as controller
        vms = [GceVm(f"oxidex-runners-vm{i}", "z", "RUNNING", "n2-standard-2", True, instance_id=f"vm-{i}")
               for i in (1, 2, 3)]
        runners = [GitHubRunner(i, f"gha-oxidex-runners-vm{i}-1", "online", False, ("spot",)) for i in (1, 2, 3)]
        state = {"min_runner_floor": 3,
                 "host_deployments": {f"z/vm-{i}": {"expected_runner_count": 1} for i in (1, 2, 3)}}
        args = SimpleNamespace(scaling_mode="legacy", state_file=Path("/nonexistent"))
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(controller, "quiesce_remote_builder", return_value=True) as quiesce, \
                patch.object(controller, "reconcile_host_operation"):
            self.assertEqual(controller.scale_down(vms, runners, args, state=state,
                                                   state_path=Path(tmpdir) / "s.json"), 0)
        quiesce.assert_not_called()
        self.assertNotIn("host_operation", state)



class DeregisterIdleRecheckTests(RotationHarness, unittest.TestCase):
    """Round 8: never deregister or delete an outgoing host that picked up work after cordoning."""

    def idle_get(self, busy=False):
        def api(target, method, path, body=None, paginate=False):
            if method == "GET":
                return {"name": "gha-oxidex-runners-old-1", "busy": busy, "status": "online"}
            return {}
        return api

    def deletes(self, mock_api):
        return [c.args for c in mock_api.call_args_list if c.args[1] == "DELETE"]

    def test_busy_runner_in_fresh_inventory_returns_to_drain(self):
        busy = replace_vm(self.old_runner, busy=True)
        mock_api, _, _, saved = self.step(self.state("deregister"), [self.outgoing_vm, self.replacement_vm],
                                          [busy, self.new_runner], api=self.idle_get())
        self.assertEqual(self.deletes(mock_api), [])
        self.assertEqual(saved["price_rotation"]["phase"], "cordon_drain")
        self.assertTrue(saved["price_rotation"]["cordon_complete"])

    def test_runner_busy_on_fresh_get_is_not_deregistered(self):
        mock_api, _, _, saved = self.step(self.state("deregister"), [self.outgoing_vm, self.replacement_vm],
                                          [self.old_runner, self.new_runner], api=self.idle_get(busy=True))
        self.assertEqual(self.deletes(mock_api), [])
        self.assertEqual(saved["price_rotation"]["phase"], "cordon_drain")
        self.assertNotIn("deregistering_runner_id", saved["price_rotation"])

    def test_active_tracked_job_blocks_deregistration(self):
        mock_api, _, _, saved = self.step(self.state("deregister"), [self.outgoing_vm, self.replacement_vm],
                                          [self.old_runner, self.new_runner], api=self.idle_get(), jobs_done=False)
        mock_api.assert_not_called()
        self.assertEqual(saved["price_rotation"]["phase"], "cordon_drain")

    def test_idle_runner_is_deregistered_after_fresh_confirmation(self):
        mock_api, _, _, saved = self.step(self.state("deregister"), [self.outgoing_vm, self.replacement_vm],
                                          [self.old_runner, self.new_runner], api=self.idle_get())
        self.assertEqual(self.deletes(mock_api),
                         [("xbmc4lyfe/newznab-tmux", "DELETE", "repos/xbmc4lyfe/newznab-tmux/actions/runners/99")])
        self.assertEqual(saved["price_rotation"]["phase"], "delete")

    def test_delete_waits_for_active_job_on_host(self):
        state = self.state("delete")
        _, delete_old, _, saved = self.step(state, [self.outgoing_vm, self.replacement_vm], [self.new_runner],
                                            jobs_done=False)
        delete_old.assert_not_called()
        self.assertEqual(saved["price_rotation"]["phase"], "delete")
        self.assertNotIn("asia-east2-a/oxidex-runners-old", saved.get("intentional_deletions", []))

    def test_delete_waits_for_busy_leftover_runner(self):
        busy = replace_vm(self.old_runner, busy=True)
        _, delete_old, _, _ = self.step(self.state("delete"), [self.outgoing_vm, self.replacement_vm],
                                        [busy, self.new_runner])
        delete_old.assert_not_called()


class ReplacementCleanupRetryTests(unittest.TestCase):
    """Round 8: a failed abort-time replacement deletion is retried until confirmed."""

    def test_failed_deletion_persists_and_retries_until_absent(self):
        from lib.spot_runner_autoscaler import _abort_runner_rotation, reconcile_replacement_cleanups
        outgoing = GceVm("oxidex-runners-old", "z", "RUNNING", "n4-highcpu-16", True, instance_id="old")
        dead = GceVm("oxidex-runners-new", "z", "TERMINATED", "n4-highcpu-16", True, instance_id="new")
        args = SimpleNamespace(project="p")
        state = {"price_rotation": {"phase": "verify_readiness", "target": "swack-tools",
                                    "outgoing_vm": {"name": outgoing.name, "zone": "z", "instance_id": "old"},
                                    "replacement_vm": {"name": dead.name, "zone": "z", "instance_id": "new"}}}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.spot_runner_autoscaler.delete_vm_and_disks",
                       side_effect=subprocess.CalledProcessError(1, ["gcloud"])):
                _abort_runner_rotation(state["price_rotation"], [outgoing, dead], args, state, path, "timeout")
                self.assertNotIn("price_rotation", state)
                self.assertEqual(read_scale_state(path)["replacement_cleanups"]["z/oxidex-runners-new"],
                                 {"name": dead.name, "zone": "z", "instance_id": "new",
                                  "target": "swack-tools", "runners": {}})
                reconcile_replacement_cleanups([outgoing, dead], [], args, state, path)  # Still failing.
            self.assertIn("z/oxidex-runners-new", read_scale_state(path)["replacement_cleanups"])
            with patch("lib.spot_runner_autoscaler.delete_vm_and_disks") as retry:
                reconcile_replacement_cleanups([outgoing, dead], [], args, state, path)
                retry.assert_called_once_with(dead, "p")
                self.assertIn("z/oxidex-runners-new", state["replacement_cleanups"])  # Not yet confirmed.
                # A same-named VM with another identity is not this cleanup's VM: absence is confirmed.
                reconcile_replacement_cleanups([outgoing, replace_vm(dead, instance_id="other")], [], args, state, path)
                retry.assert_called_once()
            self.assertEqual(read_scale_state(path)["replacement_cleanups"], {})

    def _pending_cleanup(self, tmpdir, repl):
        from lib.spot_runner_autoscaler import write_scale_state
        key = f"{repl.zone}/{repl.name}"
        state = {"replacement_cleanups": {key: {"name": repl.name, "zone": repl.zone, "instance_id": repl.instance_id}},
                 "intentional_deletions": [key], "intentional_deletion_ids": {key: repl.instance_id},
                 "runner_inventory_observed_at": __import__("time").time()}
        path = Path(tmpdir) / "state.json"
        write_scale_state(path, state)
        return key, state, path

    def test_retry_drains_replacement_that_took_listeners_or_jobs(self):
        """Codex M4 rounds 1 and 9: never delete by identity alone, and never abandon the surplus host."""
        from lib.spot_runner_autoscaler import reconcile_replacement_cleanups
        repl = GceVm("oxidex-runners-new", "z", "RUNNING", "n4-highcpu-16", True, instance_id="new")
        args = SimpleNamespace(project="p")
        for runner in (GitHubRunner(9, "gha-oxidex-runners-new-1", "online", False, ("spot",)),
                       GitHubRunner(9, "gha-oxidex-runners-new-1", "online", True, ("spot",))):
            with self.subTest(busy=runner.busy), tempfile.TemporaryDirectory() as tmpdir:
                key, state, path = self._pending_cleanup(tmpdir, repl)
                state["replacement_cleanups"][key]["target"] = "swack-tools"

                def github(target, method, path_, body=None, **kwargs):
                    return {"name": runner.name, "busy": runner.busy} if method == "GET" else {}

                with patch("lib.spot_runner_autoscaler.delete_vm_and_disks") as delete, \
                        patch("lib.runner_drain.api", side_effect=github) as api, \
                        patch("lib.spot_runner_autoscaler.quiesce_remote_builder", return_value=True):
                    reconcile_replacement_cleanups([repl], [runner], args, state, path)
                delete.assert_not_called()  # listeners were online this check
                deletes = [c.args[2] for c in api.call_args_list if c.args[1] == "DELETE"]
                self.assertEqual(deletes, [] if runner.busy else ["orgs/swack-tools/actions/runners/9"])
                saved = read_scale_state(path)
                self.assertTrue(saved["replacement_cleanups"][key]["drain"])  # obligation kept

    def test_retry_keeps_replacement_with_active_remote_build(self):
        from lib.spot_runner_autoscaler import reconcile_replacement_cleanups
        repl = GceVm("oxidex-runners-new", "z", "RUNNING", "n4-highcpu-16", True, instance_id="new")
        with tempfile.TemporaryDirectory() as tmpdir:
            key, state, path = self._pending_cleanup(tmpdir, repl)
            with patch("lib.spot_runner_autoscaler.delete_vm_and_disks") as delete, \
                    patch("lib.spot_runner_autoscaler.quiesce_remote_builder", return_value=False):
                reconcile_replacement_cleanups([repl], [], SimpleNamespace(project="p"), state, path)
            delete.assert_not_called()
            self.assertIn(key, read_scale_state(path)["replacement_cleanups"])  # retried, not abandoned

    def test_kept_replacement_is_drained_and_deleted_across_checks(self):
        from lib.spot_runner_autoscaler import _abort_runner_rotation, reconcile_replacement_cleanups
        outgoing = GceVm("oxidex-runners-old", "z", "RUNNING", "n4-highcpu-16", True, instance_id="old")
        repl = GceVm("oxidex-runners-new", "z", "RUNNING", "n4-highcpu-16", True, instance_id="new")
        online = GitHubRunner(9, "gha-oxidex-runners-new-1", "online", False, ("spot",))
        args = SimpleNamespace(project="p")
        state = {"runner_inventory_observed_at": __import__("time").time(),
                 "price_rotation": {"phase": "verify_readiness", "target": "swack-tools",
                                    "outgoing_vm": {"name": outgoing.name, "zone": "z", "instance_id": "old"},
                                    "replacement_vm": {"name": repl.name, "zone": "z", "instance_id": "new"}}}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.spot_runner_autoscaler.delete_vm_and_disks") as delete, \
                    patch("lib.spot_runner_autoscaler.quiesce_remote_builder", return_value=True), \
                    patch("lib.runner_drain.api",
                          side_effect=lambda t, m, p_, b=None, **k: {"name": online.name, "busy": False}
                          if m == "GET" else {}):
                _abort_runner_rotation(state["price_rotation"], [outgoing, repl], args, state, path,
                                       "timeout", [online])
                delete.assert_not_called()
                self.assertTrue(state["replacement_cleanups"]["z/oxidex-runners-new"]["drain"])
                reconcile_replacement_cleanups([outgoing, repl], [online], args, state, path)  # deregisters
                delete.assert_not_called()
                reconcile_replacement_cleanups([outgoing, repl], [], args, state, path)  # listener gone
                delete.assert_called_once_with(repl, "p")
                reconcile_replacement_cleanups([outgoing], [], args, state, path)  # absence confirmed
            self.assertEqual(read_scale_state(path)["replacement_cleanups"], {})

    def test_retry_waits_for_fresh_runner_inventory(self):
        from lib.spot_runner_autoscaler import reconcile_replacement_cleanups
        repl = GceVm("oxidex-runners-new", "z", "RUNNING", "n4-highcpu-16", True, instance_id="new")
        with tempfile.TemporaryDirectory() as tmpdir:
            key, state, path = self._pending_cleanup(tmpdir, repl)
            state["runner_inventory_observed_at"] = 0.0  # stale
            with patch("lib.spot_runner_autoscaler.delete_vm_and_disks") as delete, \
                    patch("lib.spot_runner_autoscaler.quiesce_remote_builder", return_value=True):
                reconcile_replacement_cleanups([repl], [], SimpleNamespace(project="p"), state, path)
            delete.assert_not_called()
            self.assertIn(key, state["replacement_cleanups"])  # still pending, not released

    def test_retry_deletes_quiescent_replacement_without_listeners(self):
        from lib.spot_runner_autoscaler import reconcile_replacement_cleanups
        repl = GceVm("oxidex-runners-new", "z", "RUNNING", "n4-highcpu-16", True, instance_id="new")
        offline = GitHubRunner(9, "gha-oxidex-runners-new-1", "offline", False, ("spot",))
        with tempfile.TemporaryDirectory() as tmpdir:
            key, state, path = self._pending_cleanup(tmpdir, repl)
            with patch("lib.spot_runner_autoscaler.delete_vm_and_disks") as delete, \
                    patch("lib.spot_runner_autoscaler.quiesce_remote_builder", return_value=True):
                reconcile_replacement_cleanups([repl], [offline], SimpleNamespace(project="p"), state, path)
            delete.assert_called_once_with(repl, "p")
            self.assertIn(key, state["replacement_cleanups"])  # awaiting confirmed absence

    def test_deleted_replacement_runner_registrations_are_removed(self):
        """Codex M4 round 5: offline registrations of a deleted replacement must not be orphaned."""
        from lib.spot_runner_autoscaler import _abort_runner_rotation, reconcile_replacement_cleanups
        outgoing = GceVm("oxidex-runners-old", "z", "RUNNING", "n4-highcpu-16", True, instance_id="old")
        repl = GceVm("oxidex-runners-new", "z", "TERMINATED", "n4-highcpu-16", True, instance_id="new")
        offline = GitHubRunner(77, "gha-oxidex-runners-new-1", "offline", False, ("spot",))
        late = GitHubRunner(78, "gha-oxidex-runners-new-2", "offline", False, ("spot",))
        args = SimpleNamespace(project="p")
        state = {"price_rotation": {"phase": "verify_readiness", "target": "xbmc4lyfe/newznab-tmux",
                                    "outgoing_vm": {"name": outgoing.name, "zone": "z", "instance_id": "old"},
                                    "replacement_vm": {"name": repl.name, "zone": "z", "instance_id": "new"}}}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.spot_runner_autoscaler.delete_vm_and_disks"):
                _abort_runner_rotation(state["price_rotation"], [outgoing, repl], args, state, path, "timeout",
                                       [offline])
            record = state["replacement_cleanups"]["z/oxidex-runners-new"]
            self.assertEqual(record["runners"], {"77": "gha-oxidex-runners-new-1"})
            # VM gone, but GitHub deregistration fails: the obligation stays.
            with patch("lib.runner_drain.api", side_effect=subprocess.CalledProcessError(1, ["gh"])):
                reconcile_replacement_cleanups([outgoing], [offline, late], args, state, path)
            self.assertIn("z/oxidex-runners-new", read_scale_state(path)["replacement_cleanups"])
            with patch("lib.runner_drain.api", return_value={}) as api:
                reconcile_replacement_cleanups([outgoing], [offline, late], args, state, path)
            deleted = sorted(c.args[2] for c in api.call_args_list)
            self.assertEqual(deleted, ["repos/xbmc4lyfe/newznab-tmux/actions/runners/77",
                                       "repos/xbmc4lyfe/newznab-tmux/actions/runners/78"])
            self.assertEqual(read_scale_state(path)["replacement_cleanups"], {})

    def test_online_runner_of_deleted_replacement_is_not_deregistered(self):
        from lib.spot_runner_autoscaler import reconcile_replacement_cleanups
        online = GitHubRunner(77, "gha-oxidex-runners-new-1", "online", False, ("spot",))
        state = {"replacement_cleanups": {"z/oxidex-runners-new": {
            "name": "oxidex-runners-new", "zone": "z", "instance_id": "new", "target": "swack-tools",
            "runners": {"77": "gha-oxidex-runners-new-1"}}}}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.runner_drain.api") as api:
                reconcile_replacement_cleanups([], [online], SimpleNamespace(project="p"), state, path)
            api.assert_not_called()
            self.assertIn("z/oxidex-runners-new", state["replacement_cleanups"])

    def test_lost_capacity_abort_schedules_drain_for_partial_replacement(self):
        """Codex M4 round 11: a kept, partially ready replacement must not become an uncounted surplus."""
        from lib.spot_runner_autoscaler import _abort_runner_rotation
        outgoing = GceVm("oxidex-runners-old", "z", "RUNNING", "n4-highcpu-16", True, instance_id="old")
        repl = GceVm("oxidex-runners-new", "z", "RUNNING", "n4-highcpu-16", True, instance_id="new")
        online = GitHubRunner(9, "gha-oxidex-runners-new-1", "online", False, ("spot",))
        rotation = {"phase": "verify_readiness", "target": "swack-tools", "capacity_lost": True,
                    "deregistered_runner_ids": [99],
                    "outgoing_vm": {"name": outgoing.name, "zone": "z", "instance_id": "old"},
                    "replacement_vm": {"name": repl.name, "zone": "z", "instance_id": "new"}}
        state = {"price_rotation": rotation}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            with patch("lib.spot_runner_autoscaler.delete_vm_and_disks") as delete, \
                    patch("lib.spot_runner_autoscaler._outgoing_capacity_lost", return_value=True):
                _abort_runner_rotation(rotation, [outgoing, repl], SimpleNamespace(project="p"), state, path,
                                       "timeout", [online])
            delete.assert_not_called()
            saved = read_scale_state(path)
        self.assertTrue(saved["replacement_cleanups"]["z/oxidex-runners-new"]["drain"])
        self.assertEqual(saved["price_rotation"]["phase"], "select")  # still provisioning fresh capacity

    def _tracked_job_state(self, status="in_progress"):
        return {"job_tracking": {"jobs": {"j1": {"vm": {"instance_id": "new"}, "repository": "swack-tools/oxidex",
                                                   "job": {"id": 555, "status": status}}}}}

    def test_abort_keeps_replacement_with_tracked_job_in_progress(self):
        """Codex M4 round 12: offline/idle listeners do not prove the tracked workflow finished."""
        from lib.spot_runner_autoscaler import _abort_runner_rotation
        outgoing = GceVm("oxidex-runners-old", "z", "RUNNING", "n4-highcpu-16", True, instance_id="old")
        repl = GceVm("oxidex-runners-new", "z", "TERMINATED", "n4-highcpu-16", True, instance_id="new")
        state = self._tracked_job_state()
        state["price_rotation"] = {"phase": "verify_readiness", "target": "swack-tools",
                                   "outgoing_vm": {"name": outgoing.name, "zone": "z", "instance_id": "old"},
                                   "replacement_vm": {"name": repl.name, "zone": "z", "instance_id": "new"}}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            for api_result in ({"status": "in_progress"}, subprocess.CalledProcessError(1, ["gh"])):
                with self.subTest(api=type(api_result).__name__):
                    kwargs = {"side_effect": api_result} if isinstance(api_result, Exception) else {"return_value": api_result}
                    st = json.loads(json.dumps(state))
                    with patch("lib.spot_runner_autoscaler.delete_vm_and_disks") as delete, \
                            patch("lib.runner_drain.api", **kwargs):
                        _abort_runner_rotation(st["price_rotation"], [outgoing, repl], SimpleNamespace(project="p"),
                                               st, path, "timeout", [])
                    delete.assert_not_called()
                    self.assertTrue(st["replacement_cleanups"]["z/oxidex-runners-new"]["drain"])

    def test_cleanup_retry_waits_for_tracked_job_then_deletes(self):
        from lib.spot_runner_autoscaler import reconcile_replacement_cleanups
        repl = GceVm("oxidex-runners-new", "z", "TERMINATED", "n4-highcpu-16", True, instance_id="new")
        with tempfile.TemporaryDirectory() as tmpdir:
            key, state, path = self._pending_cleanup(tmpdir, repl)
            state["replacement_cleanups"][key]["target"] = "swack-tools"
            state.update(self._tracked_job_state())
            with patch("lib.spot_runner_autoscaler.delete_vm_and_disks") as delete, \
                    patch("lib.runner_drain.api", return_value={"status": "in_progress"}):
                reconcile_replacement_cleanups([repl], [], SimpleNamespace(project="p"), state, path)
            delete.assert_not_called()
            with patch("lib.spot_runner_autoscaler.delete_vm_and_disks") as delete, \
                    patch("lib.runner_drain.api", return_value={"status": "completed"}):
                reconcile_replacement_cleanups([repl], [], SimpleNamespace(project="p"), state, path)
            delete.assert_called_once_with(repl, "p")

    def test_controller_loop_runs_cleanup_each_check(self):
        import inspect
        from lib import spot_runner_autoscaler as controller
        self.assertIn("reconcile_replacement_cleanups(vms, runners, args, state, state_path)",
                      inspect.getsource(controller._run_controller))


class LaunchAgentArgumentTests(unittest.TestCase):
    """Finding 3: the LaunchAgent restarts the controller with the same target and rotation mode."""

    def program_arguments(self, argv):
        import plistlib
        from lib import spot_runner_autoscaler as controller
        with tempfile.TemporaryDirectory() as directory:
            args = controller.args_parser().parse_args(argv)
            with patch.object(Path, "home", return_value=Path(directory)), \
                    patch.object(controller.subprocess, "run") as run:
                path = controller.install_launch_agent(args)
                run.assert_called_once()  # launchctl is mocked, never executed.
            return plistlib.loads(path.read_bytes())["ProgramArguments"]

    def test_target_and_make_before_break_are_serialized(self):
        arguments = self.program_arguments(["--target", "xbmc4lyfe/newznab-tmux", "--make-before-break",
                                            "--manage-control-vm"])
        self.assertEqual(arguments[arguments.index("--target") + 1], "xbmc4lyfe/newznab-tmux")
        self.assertIn("--make-before-break", arguments)
        self.assertIn("--manage-control-vm", arguments)

    def test_defaults_omit_target_and_rotation_flags(self):
        arguments = self.program_arguments([])
        self.assertNotIn("--target", arguments)
        self.assertNotIn("--make-before-break", arguments)


class DrainTargetFallbackTests(unittest.TestCase):
    """Finding 4: drain helpers fall back to the caller's effective target, not 'swack-tools'."""

    def test_resolve_target_uses_caller_default(self):
        self.assertEqual(runner_drain.resolve_target({}, None, "other-org"), "other-org")
        self.assertEqual(runner_drain.resolve_target({"target": "a/b"}, None, "other-org"), "a/b")
        self.assertEqual(runner_drain.resolve_target({}, None), "swack-tools")

    def test_cleanup_group_without_stored_target_uses_caller_org(self):
        operation = {"drain_group_id": 7, "drain_group_name": "spot-drain-x"}
        responses = [{"name": "spot-drain-x"}, [{"runners": []}], {}]
        with patch("lib.runner_drain.api", side_effect=responses) as mock_api:
            runner_drain.cleanup_group(operation, "other-org")
        self.assertEqual([c.args[2] for c in mock_api.call_args_list],
                         ["orgs/other-org/actions/runner-groups/7",
                          "orgs/other-org/actions/runner-groups/7/runners",
                          "orgs/other-org/actions/runner-groups/7"])

    def test_cleanup_group_skips_repo_fallback_target(self):
        with patch("lib.runner_drain.api") as mock_api:
            runner_drain.cleanup_group({"drain_group_id": 7, "drain_group_name": "g"}, "xbmc4lyfe/newznab-tmux")
        mock_api.assert_not_called()

    def test_terminate_deregisters_through_the_effective_target(self):
        from lib import spot_runner_autoscaler as controller
        vm = GceVm("oxidex-runners-a", "z", "RUNNING", "n4-highcpu-16", True, instance_id="1")
        runner = GitHubRunner(5, "gha-oxidex-runners-a-1", "online", False, ("spot",))
        with patch.object(controller.subprocess, "run") as run:
            controller.terminate_spot_vm(vm, "p", "xbmc4lyfe/newznab-tmux", [runner], [vm])
        self.assertEqual(run.call_args_list[-1].args[0],
                         ["gh", "api", "-X", "DELETE", "repos/xbmc4lyfe/newznab-tmux/actions/runners/5"])

    def test_controller_passes_effective_target_to_group_cleanup(self):
        from lib import spot_runner_autoscaler as controller
        args = SimpleNamespace(org="swack-tools", target="other-org", project="p", scaling_mode="legacy",
                               max_daily_usd=Decimal("4"))
        state = {"host_operation": {"kind": "delete", "vm": {"name": "oxidex-runners-gone", "zone": "z",
                                                             "status": "RUNNING", "machine_type": "n4-highcpu-16",
                                                             "spot": True, "instance_id": "9"},
                                    "runner_ids": [], "drain_group_id": 3,
                                    "drain_group_name": "spot-drain-oxidex-runners-gone-9"}}
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("lib.spot_runner_autoscaler.runner_inventory_stale", return_value=False), \
                patch("lib.spot_runner_autoscaler.import_deployment_receipt"), \
                patch("lib.runner_drain.cleanup_group") as cleanup, \
                patch("lib.fleet_migration.confirm_migration_source_drives", return_value=True), \
                patch("lib.fleet_migration.record_retirement_completion"):
            controller.reconcile_host_operation([], [], args, state, Path(tmpdir) / "state.json")
        self.assertEqual(cleanup.call_args.args[1], "other-org")


if __name__ == "__main__":
    unittest.main()


class ControllerTargetBindingTests(unittest.TestCase):
    """Codex M4 round 13: controller state is bound to its target."""

    def args(self, target=None, org="swack-tools"):
        return SimpleNamespace(target=target, org=org)

    def test_fresh_state_binds_to_active_target(self):
        from lib.spot_runner_autoscaler import bind_state_to_target
        state = {}
        self.assertTrue(bind_state_to_target(state, self.args("xbmc4lyfe/newznab-tmux")))
        self.assertEqual(state["controller_target"], "xbmc4lyfe/newznab-tmux")
        self.assertFalse(bind_state_to_target(state, self.args("XBMC4LYFE/newznab-tmux")))

    def test_state_of_another_target_is_refused(self):
        from lib.spot_runner_autoscaler import bind_state_to_target
        state = {"controller_target": "swack-tools",
                 "observed_vms": {"z/oxidex-runners-a": {"name": "oxidex-runners-a", "zone": "z"}}}
        with self.assertRaisesRegex(RuntimeError, "separate --state-file"):
            bind_state_to_target(state, self.args("xbmc4lyfe/newznab-tmux"))
        self.assertIn("z/oxidex-runners-a", state["observed_vms"])  # untouched

    def test_legacy_state_belongs_to_default_org(self):
        from lib.spot_runner_autoscaler import bind_state_to_target
        legacy = {"observed_vms": {"z/oxidex-runners-a": {"name": "oxidex-runners-a", "zone": "z"}}}
        with self.assertRaisesRegex(RuntimeError, "predates target binding"):
            bind_state_to_target(dict(legacy), self.args("xbmc4lyfe/newznab-tmux"))
        state = dict(legacy)
        self.assertTrue(bind_state_to_target(state, self.args()))
        self.assertEqual(state["controller_target"], "swack-tools")

    def test_controller_refuses_foreign_state_before_any_action(self):
        from lib import spot_runner_autoscaler as controller
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            controller.write_scale_state(path, {"controller_target": "swack-tools"})
            args = SimpleNamespace(target="xbmc4lyfe/newznab-tmux", org="swack-tools", state_file=path,
                                   dry_run=False)
            with patch.object(controller, "list_github_runners") as runners:
                with self.assertRaisesRegex(RuntimeError, "separate --state-file"):
                    controller._run_controller(args)
                runners.assert_not_called()
