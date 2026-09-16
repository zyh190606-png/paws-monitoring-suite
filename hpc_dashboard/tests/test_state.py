import tempfile
import unittest
from pathlib import Path

from state import AccountCapacityStore, AccountManager, AlertReadStore


MARKER = "OPENSSH PRIVATE KEY"
KEY = f"-----BEGIN {MARKER}-----\n" + "a" * 80 + f"\n-----END {MARKER}-----\n"


class StateTests(unittest.TestCase):
    def test_add_and_remove_managed_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accounts_root = root / "accounts"
            manager = AccountManager({"state_root": str(root / "state"), "accounts_root": str(accounts_root), "legacy_keys_root": str(root / "ssh")})
            added = manager.add("z999", KEY, "id_rsa")
            self.assertTrue(added["managed"])
            self.assertEqual([item["username"] for item in manager.describe()], ["z999"])
            removed = manager.remove("z999")
            self.assertTrue(removed["deleted_managed_copy"])
            self.assertEqual(manager.describe(), [])
            self.assertFalse((accounts_root / "z999").exists())

    def test_remove_discovered_account_preserves_original_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_dir = root / "accounts" / "z998"
            account_dir.mkdir(parents=True)
            original = account_dir / "z998_demo.key"
            original.write_text(KEY, encoding="utf-8")
            manager = AccountManager({"state_root": str(root / "state"), "accounts_root": str(root / "accounts"), "legacy_keys_root": str(root / "ssh")})
            result = manager.remove("z998")
            self.assertFalse(result["deleted_managed_copy"])
            self.assertTrue(original.exists())
            self.assertEqual(manager.describe(), [])

    def test_alerts_are_read_until_they_leave_active_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AlertReadStore({"state_root": tmp})
            alert = {"account": "z1", "job_id": "1", "task_id": "1", "case_id": "case", "code": "STDERR_NONEMPTY", "level": "critical"}
            decorated = store.decorate([alert])[0]
            store.mark_read([decorated["alert_id"]])
            self.assertTrue(store.decorate([alert])[0]["read"])
            self.assertEqual(store.decorate([]), [])
            self.assertFalse(store.decorate([alert])[0]["read"])

    def test_capacity_uses_unique_formal_cases_and_marks_legacy_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.csv"
            ledger.write_text(
                "account,formal_case_id\n"
                "z100,case_a\n"
                "z100,case_a\n"
                "z100,case_b\n",
                encoding="utf-8",
            )
            store = AccountCapacityStore({
                "account_ledger_path": str(ledger),
                "account_case_limit": 40,
                "account_pool_accounts": ["z100", "z101"],
            })
            values = store.describe(["z100", "z101", "z9"])
            self.assertEqual(values["z100"]["assigned_case_count"], 2)
            self.assertEqual(values["z100"]["capacity_remaining"], 38)
            self.assertEqual(values["z101"]["capacity_remaining"], 40)
            self.assertEqual(values["z9"]["capacity_status"], "read_only")

    def test_capacity_is_unknown_when_ledger_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountCapacityStore({
                "account_ledger_path": str(Path(tmp) / "missing.csv"),
                "account_case_limit": 40,
                "account_pool_accounts": ["z100"],
            })
            self.assertEqual(store.describe(["z100"])["z100"]["capacity_status"], "unknown")


if __name__ == "__main__":
    unittest.main()
