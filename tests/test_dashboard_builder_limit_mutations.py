"""Fixed dedicated-builder limits and qualification contracts."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import dashboard_models as models
from lib.dashboard_server import DashboardStore
from lib.edge_state import build_snapshot


class DashboardBuilderLimitMutationTests(unittest.TestCase):
    def profile(self, **changes):
        return {"role": "rust_builder", "runner_vcpu": 16, "runner_ram_gib": 28,
                "host_overhead_gib": 4, "min_benchmark_score": 0,
                "budget": {"max_hourly_usd": 100, "max_daily_usd": 2400,
                           "max_monthly_usd": 73000}, **changes}

    def qualify(self, **changes):
        return models.qualify_instances(self.profile(**changes), {}, live_pricing={})

    def test_every_limit_alias_rejects_over_capacity(self):
        for key, value in (("runner_vcpu", 17), ("container_vcpu", 17), ("builder_vcpu", 17),
                           ("runner_ram_gib", 28.01), ("container_ram_gib", 28.01),
                           ("builder_ram_gib", 28.01)):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    models.validate_builder_container_limits({key: value})

    def test_limits_are_checked_before_lossy_coercion(self):
        for value in (16.1, True, None, "NaN", "Infinity", 0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    models.validate_builder_container_limits({"runner_vcpu": value})

    def test_valid_limits_and_budget_only_patch_are_accepted(self):
        for patch in ({"runner_vcpu": 16, "runner_ram_gib": 28},
                      {"container_vcpu": 8, "container_ram_gib": 16.5},
                      {"builder_vcpu": "16", "builder_ram_gib": "28"},
                      {"budget": {"max_daily_usd": 10}}):
            with self.subTest(patch=patch):
                models.validate_builder_container_limits(patch)

    def test_conflicting_aliases_are_rejected(self):
        for patch in ({"container_vcpu": 16, "builder_vcpu": 8},
                      {"container_ram_gib": 28, "builder_ram_gib": 16}):
            with self.subTest(patch=patch):
                with self.assertRaises(ValueError):
                    models.validate_builder_container_limits(patch)

    def test_builder_qualification_only_returns_fixed_shape(self):
        result = self.qualify()
        self.assertGreater(result["hardware_qualifying_count"], 0)
        self.assertGreater(result["spot_price_qualifying_count"], 0)
        for key in ("hardware_qualifying", "spot_price_qualifying"):
            for row in result[key]:
                self.assertEqual((row["machine_type"], row["vcpu"], row["ram_gib"]),
                                 ("n4-highcpu-16", 16, 32))

    def test_oversized_legacy_profiles_have_no_offers_without_raising(self):
        for changes in ({"runner_vcpu": 32}, {"runner_ram_gib": 64}):
            with self.subTest(changes=changes):
                result = self.qualify(**changes)
                self.assertEqual(result["hardware_qualifying_count"], 0)
                self.assertEqual(result["spot_price_qualifying_count"], 0)
                self.assertTrue(result["validation_error"])

    def test_legacy_smaller_limits_and_template_reference_remain_valid(self):
        result = self.qualify(runner_vcpu=4, runner_ram_gib=16,
                              vm_template_id="legacy-c4d", use_vm_template=True)
        self.assertGreater(result["hardware_qualifying_count"], 0)
        self.assertTrue(all(row["machine_type"] == "n4-highcpu-16"
                            for row in result["hardware_qualifying"]))

    def test_fixed_shape_cannot_satisfy_larger_host_requirements(self):
        for changes in ({"min_instance_vcpu": 32}, {"min_instance_ram_gib": 64},
                        {"host_overhead_gib": 8}):
            with self.subTest(changes=changes):
                self.assertEqual(self.qualify(**changes)["hardware_qualifying_count"], 0)

    def test_runner_qualification_still_allows_other_shapes(self):
        result = self.qualify(role="cicd_runner", runner_vcpu=32, runner_ram_gib=64)
        self.assertGreater(result["hardware_qualifying_count"], 0)
        self.assertTrue(any(row["machine_type"] != "n4-highcpu-16"
                            for row in result["hardware_qualifying"]))

    def test_builder_render_rejects_oversized_limits(self):
        for changes in ({"runner_vcpu": 17}, {"runner_ram_gib": 28.1}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    models.render_cloud_init_for_profile(
                        '{"cpus": 16, "memory_gib": 28}', role="rust_builder", **changes)

    def test_builder_render_accepts_cap_and_runner_render_remains_unrestricted(self):
        self.assertEqual(models.render_cloud_init_for_profile(
            '{"cpus": 4, "memory_gib": 16}', role="rust_builder",
            runner_vcpu=16, runner_ram_gib=28), '{"cpus": 16, "memory_gib": 28}')
        models.render_cloud_init_for_profile("", role="cicd_runner", runner_vcpu=32, runner_ram_gib=64)


class DashboardBuilderLimitApiTests(unittest.TestCase):
    now = 1800000000.0

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state_path = Path(temp.name) / "state.json"
        for name in ("lib.dashboard_models.fetch_live_spot_pricing",
                     "lib.dashboard_server.fetch_live_spot_pricing"):
            patcher = patch(name, return_value={})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = self.new_store()
        self.store.hydrate()
        self.store.refresh_fleet_inventory()

    def new_store(self):
        store = DashboardStore(state_path=self.state_path, clock=lambda: self.now, fleet_source=lambda: [])
        self.addCleanup(store.close)
        return store

    def assert_rejected_atomically(self, call):
        before = copy.deepcopy(self.store.state)
        before_disk = self.state_path.read_bytes()
        version = self.store.state_version()
        with self.assertRaises(ValueError):
            call()
        self.assertEqual(self.store.state, before)
        self.assertEqual(self.store.state_version(), version)
        self.assertEqual(self.state_path.read_bytes(), before_disk)

    def legacy_state(self):
        legacy = copy.deepcopy(self.store.state)
        legacy["profiles"]["rust_builder"].update(runner_vcpu=32, runner_ram_gib=64)
        legacy["scaling"]["rust_builder"].update(
            container_vcpu=32, builder_vcpu=32, container_ram_gib=64, builder_ram_gib=64)
        return legacy

    def load_legacy(self):
        self.store._apply_loaded_state(self.legacy_state())
        self.store._persist()

    def test_profile_api_rejects_caps_and_lossy_cpu_coercion_atomically(self):
        for key, value in (("runner_vcpu", 17), ("runner_vcpu", 16.1),
                           ("runner_vcpu", "16.1"), ("runner_vcpu", True),
                           ("runner_ram_gib", 28.01), ("runner_ram_gib", "28.01")):
            with self.subTest(key=key, value=value):
                self.assert_rejected_atomically(
                    lambda: self.store.update_profile("rust_builder", {key: value, "budget": {"max_daily_usd": 19}}))

    def test_scaling_api_rejects_every_alias_cap_before_any_state_changes(self):
        for key, value in (("container_vcpu", 17), ("builder_vcpu", 17),
                           ("container_ram_gib", 28.01), ("builder_ram_gib", 28.01)):
            with self.subTest(key=key):
                self.assert_rejected_atomically(lambda: self.store.update_scaling({
                    "role": "rust_builder", "policy": {key: value, "max_scale_up_daily_usd": 19}}))

    def test_scaling_api_rejects_fractional_and_boolean_cpu_before_coercion(self):
        for key in ("container_vcpu", "builder_vcpu"):
            for value in (16.1, "16.1", True):
                with self.subTest(key=key, value=value):
                    self.assert_rejected_atomically(lambda: self.store.update_scaling({
                        "role": "rust_builder", "policy": {key: value}}))

    def test_scaling_api_rejects_conflicting_supported_aliases_atomically(self):
        for policy in ({"container_vcpu": 16, "builder_vcpu": 8},
                       {"container_ram_gib": 28, "builder_ram_gib": 16}):
            with self.subTest(policy=policy):
                self.assert_rejected_atomically(lambda: self.store.update_scaling({
                    "role": "rust_builder", "policy": policy}))

    def test_scaling_api_rejects_profile_cpu_alias_atomically(self):
        self.assert_rejected_atomically(lambda: self.store.update_scaling({
            "role": "rust_builder", "policy": {"runner_vcpu": 8}}))

    def test_scaling_api_rejects_profile_ram_alias_atomically(self):
        self.assert_rejected_atomically(lambda: self.store.update_scaling({
            "role": "rust_builder", "policy": {"runner_ram_gib": 16}}))

    def test_accepted_scaling_updates_keep_supported_aliases_and_profile_consistent(self):
        for cpu_key, ram_key in (("container_vcpu", "container_ram_gib"),
                                 ("builder_vcpu", "builder_ram_gib")):
            for cpu, ram, expected_cpu, expected_ram in (("16", "28", 16, 28), (8, 16.1, 8, 17)):
                with self.subTest(cpu_key=cpu_key, cpu=cpu, ram=ram):
                    self.store.update_scaling({"role": "rust_builder", "policy": {cpu_key: cpu, ram_key: ram}})
                    policy = self.store.state["scaling"]["rust_builder"]
                    profile = self.store.state["profiles"]["rust_builder"]
                    self.assertEqual(float(policy["container_vcpu"]), expected_cpu)
                    self.assertEqual(float(policy["builder_vcpu"]), expected_cpu)
                    self.assertEqual(policy["container_ram_gib"], expected_ram)
                    self.assertEqual(policy["builder_ram_gib"], expected_ram)
                    self.assertEqual(profile["runner_vcpu"], expected_cpu)
                    self.assertEqual(profile["runner_ram_gib"], expected_ram)
                    models.validate_builder_container_limits(policy)

    def test_profile_budget_only_edit_preserves_legacy_limits_and_template(self):
        self.load_legacy()
        profile_before = copy.deepcopy(self.store.state["profiles"]["rust_builder"])
        builder_templates = [copy.deepcopy(t) for t in self.store.state["cloud_init_templates"]
                             if t.get("role") == "rust_builder"]
        result = self.store.update_profile("rust_builder", {"budget": {"max_daily_usd": 15}})
        profile = self.store.state["profiles"]["rust_builder"]
        self.assertEqual(profile["budget"]["max_daily_usd"], 15)
        self.assertEqual({k: v for k, v in profile.items() if k != "budget"},
                         {k: v for k, v in profile_before.items() if k != "budget"})
        self.assertEqual([t for t in self.store.state["cloud_init_templates"]
                          if t.get("role") == "rust_builder"], builder_templates)
        self.assertEqual(result["qualification"]["hardware_qualifying_count"], 0)
        self.assertEqual(json.loads(self.state_path.read_text())["profiles"]["rust_builder"]["runner_vcpu"], 32)

    def test_scaling_budget_only_edit_preserves_legacy_profile_and_policy_limits(self):
        self.load_legacy()
        profile_before = copy.deepcopy(self.store.state["profiles"]["rust_builder"])
        self.store.update_scaling({"role": "rust_builder", "policy": {"max_scale_up_daily_usd": 16}})
        profile = self.store.state["profiles"]["rust_builder"]
        policy = self.store.state["scaling"]["rust_builder"]
        self.assertEqual(policy["max_scale_up_daily_usd"], 16)
        self.assertEqual(profile["budget"]["max_daily_usd"], 16)
        self.assertEqual(profile["runner_vcpu"], profile_before["runner_vcpu"])
        self.assertEqual(profile["runner_ram_gib"], profile_before["runner_ram_gib"])
        self.assertEqual((policy["container_vcpu"], policy["builder_vcpu"],
                          policy["container_ram_gib"], policy["builder_ram_gib"]), (32, 32, 64, 64))

    def assert_legacy_qualification_held(self, store):
        result = models.qualify_instances(store.state["profiles"]["rust_builder"], {}, live_pricing={})
        self.assertEqual(result["hardware_qualifying"], [])
        self.assertEqual(result["spot_price_qualifying"], [])
        self.assertIn("runner_vcpu", result["validation_error"])
        held = store._fail_closed_evaluation({"direction": "scale_up"}, "rust_builder")
        self.assertEqual(held["direction"], "hold")
        self.assertIn("runner_vcpu", held["hold_reason"])
        self.assertEqual(store.state["profiles"]["rust_builder"]["runner_vcpu"], 32)

    def test_local_load_and_hydration_retain_legacy_but_hold_qualification(self):
        self.state_path.write_text(json.dumps(self.legacy_state()))
        loaded = self.new_store()
        loaded.hydrate()
        self.assertTrue(loaded.hydrated)
        self.assert_legacy_qualification_held(loaded)

    def test_snapshot_import_retains_legacy_but_holds_qualification(self):
        legacy = self.legacy_state()
        snapshot = build_snapshot(legacy, self.store.state_version() + 1, "a", now=self.now)
        result = self.store.import_snapshot(snapshot)
        self.assertTrue(result["imported"])
        self.assertTrue(self.store.hydrated)
        self.assert_legacy_qualification_held(self.store)


if __name__ == "__main__":
    unittest.main()
