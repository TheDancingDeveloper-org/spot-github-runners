"""The tdd-org Spot poller survives a Terraform template replace (WI-1914)."""

import importlib.util
import io
import os
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

POLLER = Path(__file__).resolve().parents[1] / "deploy" / "tdd-org-gcp" / "poller" / "gha_spot_poller.py"


def load_poller():
    env = {"GH_ORG": "Org", "REPOS": "vogt", "GCP_PROJECT": "p", "SMALL_TEMPLATE": "gha-small-1"}
    with mock.patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location("gha_spot_poller_templates_under_test", POLLER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


P = load_poller()
OLD = "gha-small-20261019094030056800000001"
NEW = "gha-small-20261019095236814000000001"


class TemplateResolutionTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.creates = []
        mock.patch.object(P, "log", self.logs.append).start()
        mock.patch.object(P, "gcp_token", lambda: "t").start()
        self.addCleanup(mock.patch.stopall)

    def fake_http(self, existing):
        def http(method, url, token, body=None):
            if "instanceTemplates?" in url:
                return {"items": [{"name": n, "creationTimestamp": ts} for n, ts in existing.items()]}
            name = url.rsplit("/", 1)[-1]
            if name not in existing:
                raise urllib.error.HTTPError(url, 404, "not found", {}, io.BytesIO(b""))
            return {"properties": {"metadata": {"items": [{"key": "tpl", "value": name}]}}}
        return mock.patch.object(P, "http", http)

    def test_wildcard_picks_the_newest_template(self):
        with self.fake_http({OLD: "2026-10-19T09:40:00", NEW: "2026-10-19T09:52:00"}):
            items, name = P.template_metadata_items("gha-small-*")
        self.assertEqual(name, NEW)
        self.assertEqual(items, [{"key": "tpl", "value": NEW}])

    def test_missing_fixed_name_falls_back_with_a_warning(self):
        with self.fake_http({NEW: "2026-10-19T09:52:00"}):
            _, name = P.template_metadata_items(OLD)
        self.assertEqual(name, NEW)
        self.assertTrue(any("WARNING" in m and OLD in m for m in self.logs))

    def test_existing_fixed_name_is_used_as_is(self):
        with self.fake_http({OLD: "2026-10-19T09:40:00", NEW: "2026-10-19T09:52:00"}):
            _, name = P.template_metadata_items(OLD)
        self.assertEqual(name, OLD)

    def test_no_template_left_keeps_the_job_queued_without_a_jit_runner(self):
        gh = mock.Mock()
        with self.fake_http({}), mock.patch.object(P, "gh", gh), \
                mock.patch.dict(P.CLASSES, {"small": {"prefix": "gha-rs-", "template": OLD, "labels": ["gcp-rust"],
                                                      "vcpus": 8, "max": 4}}):
            self.assertFalse(P.create_vm("repo#1", "small"))
        gh.assert_not_called()
        self.assertTrue(any("ERROR" in m for m in self.logs))

    def test_template_prefix_strips_the_terraform_suffix(self):
        self.assertEqual(P.template_prefix(NEW), "gha-small-")


if __name__ == "__main__":
    unittest.main()
