from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from local_monitor.monitor import (
    SpinupMonitor,
    case_profile,
    list_running_wsl_distros,
    parse_wsl_distros,
    parse_progress_line,
    parse_wsl_process_rows,
    wsl_unc_path,
)


class ProgressParserTests(unittest.TestCase):
    def test_hourly_budget_clock(self) -> None:
        record = parse_progress_line(" 19800101    10000    1.0", "sp60.txt")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["datetime"].strftime("%Y-%m-%d %H:%M:%S"), "1980-01-01 01:00:00")

    def test_stdout_day_of_year(self) -> None:
        record = parse_progress_line(" 2020 60    22.1", "sp60.stdout.txt")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["date"], "2020-02-29")

    def test_invalid_header_is_ignored(self) -> None:
        self.assertIsNone(parse_progress_line(" day time id w.dt", "sp60.txt"))

    def test_invalid_day_of_year_is_ignored(self) -> None:
        self.assertIsNone(parse_progress_line(" 2021 366    22.1", "sp60.stdout.txt"))

    def test_wsl_process_row(self) -> None:
        rows = parse_wsl_process_rows(
            "51\tPAWS_CLM_instr\t38\t00:00:37\t3190428\t/home/administrator/runs/demo/work\t/home/a/PAWS_CLM_instrumented demo.in\n",
            "Ubuntu",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pid"], 51)
        self.assertEqual(rows[0]["working_set_bytes"], 3190428 * 1024)
        self.assertEqual(rows[0]["cwd_linux"], "/home/administrator/runs/demo/work")

    def test_wsl_unc_path(self) -> None:
        path = str(wsl_unc_path("Ubuntu", "/home/administrator/runs/demo/work"))
        self.assertEqual(path, r"\\wsl.localhost\Ubuntu\home\administrator\runs\demo\work")


class WslRootTests(unittest.TestCase):
    def test_parse_running_wsl_distros_deduplicates_names(self) -> None:
        self.assertEqual(parse_wsl_distros("Ubuntu\r\nDebian\r\nubuntu\r\n"), ["Ubuntu", "Debian"])

    def test_list_running_wsl_distros_uses_running_only_query(self) -> None:
        import subprocess
        from unittest.mock import MagicMock, patch

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Ubuntu\r\nDebian\r\n".encode("utf-16-le")
        mock_proc.stderr = b""
        with patch.object(subprocess, "run", return_value=mock_proc) as mock_run:
            self.assertEqual(list_running_wsl_distros(), ["Ubuntu", "Debian"])
            self.assertEqual(mock_run.call_args[0][0], ["wsl.exe", "--list", "--running", "--quiet"])

    def test_wsl_command_uses_root(self) -> None:
        import subprocess
        from unittest.mock import patch
        from local_monitor.monitor import _run_wsl
        with patch.object(subprocess, "run") as mock_run:
            _run_wsl("Ubuntu", "echo hello")
            args = mock_run.call_args[0][0]
            self.assertEqual(args[0], "wsl.exe")
            self.assertEqual(args[1], "-d")
            self.assertEqual(args[2], "Ubuntu")
            self.assertEqual(args[3], "-u")
            self.assertEqual(args[4], "root")
            self.assertEqual(args[5], "--")
            self.assertEqual(args[6], "bash")

    def test_auto_discovery_scans_every_running_distro(self) -> None:
        import subprocess
        from unittest.mock import patch
        from local_monitor import monitor

        with tempfile.TemporaryDirectory() as temp_dir:
            roots = {
                "Ubuntu": Path(temp_dir) / "ubuntu" / "work",
                "Debian": Path(temp_dir) / "debian" / "work",
            }
            for distro, directory in roots.items():
                directory.mkdir(parents=True)
                case_id = distro.lower()
                (directory / "muskegon.in").write_text(f"{case_id}\nprj.mat\n", encoding="ascii")
                (directory / f"{case_id}.txt").write_text(" 20040101 10000 1.0\n", encoding="ascii")

            pid_by_distro = {"Ubuntu": 101, "Debian": 202}

            def fake_run(args, **kwargs):
                if args == ["wsl.exe", "--list", "--running", "--quiet"]:
                    return subprocess.CompletedProcess(args, 0, b"Ubuntu\r\nDebian\r\n", b"")
                distro = args[args.index("-d") + 1]
                linux_dir = f"/home/{distro.lower()}/work"
                case_id = distro.lower()
                row = f"{pid_by_distro[distro]}\tPAWS_CLM\t10\t00:00:10\t100\t{linux_dir}\t/home/models/PAWS_CLM {case_id}.in\n"
                return subprocess.CompletedProcess(args, 0, row.encode(), b"")

            def fake_unc(distro, linux_path):
                return roots[distro]

            with patch.object(monitor.subprocess, "run", side_effect=fake_run), patch.object(monitor, "wsl_unc_path", side_effect=fake_unc):
                rows = monitor.discover_wsl_running_cases()

            self.assertEqual({row["wsl_distro"] for row in rows}, {"Ubuntu", "Debian"})
            self.assertEqual({row["pid"] for row in rows}, {101, 202})
            self.assertEqual(len(rows), 2)

    def test_discover_wsl_returns_empty_when_no_paws(self) -> None:
        import subprocess
        from unittest.mock import patch, MagicMock
        from local_monitor.monitor import discover_wsl_running_cases
        with patch.object(subprocess, "run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = b""
            mock_run.return_value = mock_proc
            
            result = discover_wsl_running_cases(["Ubuntu"])
            
            self.assertEqual(result, [])
            mock_run.assert_called_once()


class SnapshotTests(unittest.TestCase):
    def test_nested_work_ignores_template_and_reads_parent_logs(self) -> None:
        run = self.case / "tn1y"
        work = run / "work"
        work.mkdir(parents=True)
        for name in ("ct1y", "tn1y", "v00"):
            (work / f"{name}.in").write_text(f"{name}\nprj.mat\n", encoding="ascii")
        (work / "muskegon.in").write_text("tn1y\nprj.mat\n", encoding="ascii")
        (work / "tn1y.txt").write_text(" 20040104 230000 1.0\n", encoding="ascii")
        (run / "started.txt").write_text("2026-08-23T00:00:00+00:00", encoding="ascii")
        (run / "paws_stdout.log").write_text(
            "Model Start Time: 2004 1 1\nModel End Time: 2005 1 1\n", encoding="ascii")
        profile = case_profile(work)
        self.assertEqual(profile["runtime_case_id"], "tn1y")
        (run / "PID.txt").write_text("35\n", encoding="ascii")
        restarted = case_profile(work, process_backend="wsl2", pid_override=135)
        self.assertEqual(restarted["pid"], 35)
        self.assertEqual(restarted["pid_path"].resolve(), (run / "PID.txt").resolve())
        (run / "PID.txt").unlink()
        self.assertEqual(profile["stdout_path"].resolve(), (run / "paws_stdout.log").resolve())
        snapshot = SpinupMonitor(work).snapshot(datetime(2026, 8, 23, 1, tzinfo=timezone.utc))
        self.assertEqual(snapshot["progress"]["current_date"], "2004-01-04")
        self.assertIsNotNone(snapshot["progress"]["progress_pct"])
        self.assertEqual(snapshot["speed"]["wall_elapsed_seconds"], 3600)
        (run / "PAWS_RC.txt").write_text("24\n", encoding="ascii")
        self.assertEqual(SpinupMonitor(work).snapshot()["state"], "failed")

    def test_work_does_not_inherit_parent_case_manifest(self) -> None:
        work = self.case / "work"
        work.mkdir()
        (work / "muskegon.in").write_text("actual\nprj.mat\n", encoding="ascii")
        (work / "aaa_template.in").write_text("wrong\nprj.mat\n", encoding="ascii")
        self.assertEqual(case_profile(work)["runtime_case_id"], "actual")

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.case = Path(self.temp.name)
        (self.case / "continuous_spinup_manifest.json").write_text(
            json.dumps(
                {
                    "formal_case_id": "test-60y",
                    "runtime_case_id": "sp60",
                    "model_start": "1980-01-01 00:00:00",
                    "model_end": "2039-12-31 23:00:00",
                    "calendar_years": 60,
                    "target_days": 21915,
                }
            ),
            encoding="utf-8",
        )
        (self.case / "continuous_spinup_status.json").write_text(
            json.dumps({"started_at": "2026-08-23T00:00:00+00:00", "status": "running"}),
            encoding="utf-8",
        )
        (self.case / "sp60.txt").write_text(
            " day time id w.dt\n"
            " 19800101 10000 1.0\n"
            " 19800102 230000 1.0\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_snapshot_reads_tail_and_calculates_progress(self) -> None:
        monitor = SpinupMonitor(self.case)
        snapshot = monitor.snapshot(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(snapshot["state"], "running")
        self.assertEqual(snapshot["progress"]["current_date"], "1980-01-02")
        self.assertGreater(snapshot["progress"]["progress_pct"], 0)
        self.assertTrue(snapshot["read_only"])
        self.assertTrue(any(item["code"] == "PID_MISSING" for item in snapshot["warnings"]))

    def test_partial_last_line_does_not_hide_previous_complete_row(self) -> None:
        with (self.case / "sp60.txt").open("a", encoding="utf-8") as handle:
            handle.write("\n 19800103 10000")
        monitor = SpinupMonitor(self.case)
        snapshot = monitor.snapshot(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(snapshot["progress"]["current_date"], "1980-01-02")

    def test_recent_speed_appears_after_clock_advances(self) -> None:
        monitor = SpinupMonitor(self.case)
        t0 = datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)
        monitor.snapshot(t0)
        with (self.case / "sp60.txt").open("a", encoding="utf-8") as handle:
            handle.write("\n 19800103 230000 1.0\n")
        snapshot = monitor.snapshot(t0.replace(hour=2))
        self.assertIsNotNone(snapshot["speed"]["recent_sim_days_per_wall_hour"])
        self.assertGreater(snapshot["speed"]["recent_sim_days_per_wall_hour"], 0)

    def test_versioned_20y_manifest_and_status_names(self) -> None:
        (self.case / "continuous_spinup_manifest.json").unlink()
        (self.case / "continuous_spinup_status.json").unlink()
        (self.case / "sp60.txt").unlink()
        (self.case / "continuous_spinup_20y_manifest.json").write_text(
            json.dumps({"runtime_case_id": "ks10", "model_start": "1980-01-01 00:00:00", "model_end": "1999-12-31 23:00:00", "calendar_years": 20}),
            encoding="utf-8",
        )
        (self.case / "continuous_spinup_20y_status.json").write_text(
            json.dumps({"runtime_case_id": "ks10", "started_at": "2026-08-23T00:00:00+00:00", "status": "running"}),
            encoding="utf-8",
        )
        (self.case / "ks10.txt").write_text(" 19850101 10000 1.0\n", encoding="utf-8")
        snapshot = SpinupMonitor(self.case).snapshot(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(snapshot["runtime_case_id"], "ks10")
        self.assertAlmostEqual(snapshot["progress"]["progress_pct"], 25.0, delta=0.1)
        self.assertTrue(snapshot["metadata_sources"]["manifest"].endswith("continuous_spinup_20y_manifest.json"))

    def test_stdout_supplies_time_contract_without_manifest(self) -> None:
        (self.case / "continuous_spinup_manifest.json").unlink()
        (self.case / "continuous_spinup_status.json").unlink()
        (self.case / "sp60.txt").unlink()
        (self.case / "b0d1.in").write_text("b0d1\nprj.mat\n", encoding="ascii")
        (self.case / "b0d1.stdout.txt").write_text(
            "Model Start Time: 2001   1   1\nModel End Time: 2005  12  31\nEntering time loop\n",
            encoding="utf-8",
        )
        (self.case / "b0d1.txt").write_text(" 20030101 10000 1.0\n", encoding="utf-8")
        profile = case_profile(self.case)
        self.assertEqual(profile["runtime_case_id"], "b0d1")
        self.assertEqual(profile["model_start"].strftime("%Y-%m-%d"), "2001-01-01")
        self.assertEqual(profile["model_end"].strftime("%Y-%m-%d %H:%M"), "2005-12-31 23:00")
        snapshot = SpinupMonitor(self.case).snapshot(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc))
        self.assertIsNotNone(snapshot["progress"]["progress_pct"])
        self.assertFalse(any(item["code"] == "TIME_CONTRACT_MISSING" for item in snapshot["warnings"]))

    def test_controller_metadata_and_rc_supply_completion(self) -> None:
        (self.case / "continuous_spinup_status.json").unlink()
        (self.case / "run_metadata.log").write_text(
            "start=2026-08-23T00:00:00+00:00\nend=2026-08-23T01:00:00+00:00\nPAWS_RC=0\n",
            encoding="utf-8",
        )
        (self.case / "PAWS_RC.txt").write_text("0\n", encoding="ascii")
        snapshot = SpinupMonitor(self.case).snapshot(datetime(2026, 8, 23, 2, 0, tzinfo=timezone.utc))
        self.assertEqual(snapshot["state"], "completed")
        self.assertEqual(snapshot["result_file"]["return_code"], 0)
        self.assertEqual(snapshot["speed"]["wall_elapsed_seconds"], 3600.0)


if __name__ == "__main__":
    unittest.main()
