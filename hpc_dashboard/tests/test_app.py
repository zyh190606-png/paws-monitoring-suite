import json
import tempfile
import unittest
from pathlib import Path

from app import DashboardApp


class AppSnapshotTests(unittest.TestCase):
    def test_telemetry_accounts_are_not_overwritten_by_management_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {
                "accounts_root": str(root / "accounts"),
                "legacy_keys_root": str(root / "ssh"),
                "state_root": str(root / "state"),
                "account_ledger_path": str(root / "ledger.csv"),
                "account_case_limit": 40,
                "account_pool_accounts": ["z100"],
                "local_packages_root": str(root / "packages"),
                "download_root": str(root / "downloads"),
                "remote_cases_root": "paws_hpc_cases",
                "recent_days": 7,
                "ssh_timeout_seconds": 12,
                "remote_query_timeout_seconds": 60,
                "query_retry_count": 1,
                "stale_stdout_minutes": 30,
                "max_parallel_accounts": 1,
            }
            path = root / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            app = DashboardApp(path)
            app.monitor.last_snapshot = {
                "accounts": [{"username": "z100", "reachable": True, "source": "HPC_accounts"}],
                "runs": [], "jobs": [], "alerts": [], "summary": {"account_count": 1},
            }
            snapshot = app.snapshot()
            self.assertEqual(len(snapshot["accounts"]), 1)
            self.assertTrue(snapshot["accounts"][0]["reachable"])
            self.assertEqual(snapshot["managed_accounts"], [])


if __name__ == "__main__":
    unittest.main()
