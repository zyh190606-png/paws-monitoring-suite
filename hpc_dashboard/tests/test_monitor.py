import json
import tempfile
import unittest
from pathlib import Path

from monitor import (
    _enrich_run,
    discover_accounts,
    parse_slurm_duration,
    parse_snapshot_output,
)


class MonitorTests(unittest.TestCase):
    def test_parse_slurm_duration(self):
        self.assertEqual(parse_slurm_duration("1-10:37:09"), 124629)
        self.assertEqual(parse_slurm_duration("13:22:51"), 48171)
        self.assertIsNone(parse_slurm_duration("UNLIMITED"))

    def test_parse_remote_snapshot(self):
        sample = "\n".join([
            "__NOW__\t1784470000\t2026-07-19 22:00:00 CST",
            "__SQUEUE__\t117386370_1|117386370|1|kshctest02|paws_waveA15|RUNNING|1-10:00:00|1-14:00:00|1|node01|2026-07-18T11:27:37",
            "__RUN__\t117386370\t1\tcase01\tbatch01\t/public/home/u/paws_hpc_cases/batch01\t/public/home/u/paws_hpc_cases/batch01/runs/117386370_1_case01\t\t1784345260\t\t0\t500000\t1784469990\t2004\t1\t2010\t120\t9",
        ])
        parsed = parse_snapshot_output(sample)
        self.assertEqual(parsed["runs"][0]["case_id"], "case01")
        self.assertEqual(parsed["scheduler"]["117386370_1"]["state"], "RUNNING")

    def test_completion_gate_and_running_walltime_warning(self):
        base = {
            "job_id": "1", "task_id": "1", "run_key": "1_1", "case_id": "case", "batch": "batch", "batch_root": "/b", "run_dir": "/b/r",
            "paws_rc": 0, "stderr_bytes": 0, "stdout_bytes": 100, "stdout_mtime": 2000, "latest_year": 2015, "latest_day": 1, "first_year": 2004,
            "first_day": 1, "core_file_count": 8,
        }
        completed = _enrich_run(base, {"state":"COMPLETED","exit_code":"0:0","elapsed":"2-00:00:00","time_left":None,"partition":"p","job_name":"j","node_or_reason":"n","start":"s","end":"e","timelimit":"3-00:00:00","source":"sacct"}, None, 2000, 30)
        self.assertTrue(completed["download_ready"])
        self.assertEqual(completed["completion_gate"]["coverage_evidence"], "normal_completion_and_final_stdout_date")

        running_base = dict(base, paws_rc=None, latest_year=2009, latest_day=200, stdout_mtime=2000)
        running = _enrich_run(
            running_base,
            {"state":"RUNNING","exit_code":"0:0","elapsed":"1-00:00:00","time_left":"2-00:00:00","partition":"p","job_name":"j","node_or_reason":"n","start":"s","end":None,"timelimit":"3-00:00:00","source":"squeue"},
            {"model_start":"2004-01-01 00:00:00","model_end":"2014-12-31 23:00:00","source":"runner.json"},
            2000,
            30,
        )
        self.assertIsNotNone(running["eta_seconds"])
        self.assertIsNotNone(running["expected_end_at"])

    def test_account_discovery_merges_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "accounts"; legacy = Path(tmp) / "ssh"; root.mkdir(); legacy.mkdir()
            folder = root / "z100"; folder.mkdir(); (folder / "z100_demo.key").write_text("key")
            (legacy / "paws_hpc_z101_key").write_text("key")
            accounts = discover_accounts({"accounts_root":str(root),"legacy_keys_root":str(legacy)})
            self.assertEqual([a.username for a in accounts], ["z100", "z101"])


if __name__ == "__main__":
    unittest.main()
