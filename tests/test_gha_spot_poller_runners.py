"""The tdd-org Spot poller removes the runner a deleted throwaway VM registered (WI-1914)."""

import importlib.util
import os
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

POLLER = Path(__file__).resolve().parents[1] / "deploy" / "tdd-org-gcp" / "poller" / "gha_spot_poller.py"


def load_poller():
    env = {"GH_ORG": "Org", "REPOS": "vogt", "GCP_PROJECT": "p"}
    with mock.patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location("gha_spot_poller_runners_under_test", POLLER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


P = load_poller()


class RemoveRunnerTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        mock.patch.object(P, "log", lambda *_: None).start()
        mock.patch.object(P, "zone_api", lambda *a, **k: {}).start()
        self.addCleanup(mock.patch.stopall)

    def gh(self, runners):
        def fake(method, path, body=None):
            self.calls.append((method, path))
            return {"runners": runners} if method == "GET" else {}
        return mock.patch.object(P, "gh", fake)

    def test_deleting_a_vm_removes_its_offline_runner(self):
        with self.gh([{"id": 7, "name": "gha-rh-1", "status": "offline"}]):
            P.delete_vm("gha-rh-1", "finished")
        self.assertEqual(self.calls, [("GET", "orgs/Org/actions/runners?name=gha-rh-1"),
                                      ("DELETE", "orgs/Org/actions/runners/7")])

    def test_keeps_an_online_runner_and_other_names(self):
        with self.gh([{"id": 7, "name": "gha-rh-1", "status": "online"},
                      {"id": 8, "name": "gha-rh-10", "status": "offline"}]):
            P.remove_runner("gha-rh-1")
        self.assertEqual([c for c in self.calls if c[0] == "DELETE"], [])

    def test_a_github_failure_does_not_stop_vm_deletion(self):
        def boom(*_a, **_k):
            raise urllib.error.HTTPError("u", 500, "err", {}, None)
        with mock.patch.object(P, "gh", boom):
            P.delete_vm("gha-rh-1", "finished")
        with mock.patch.object(P, "gh", mock.Mock(side_effect=P.RateLimited(0))):
            P.remove_runner("gha-rh-1")


if __name__ == "__main__":
    unittest.main()
