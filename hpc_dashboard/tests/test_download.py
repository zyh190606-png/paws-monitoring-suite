import unittest

from download import _archive_aliases, _file_role, _infer_output_prefix, _standard_audit, _standard_selected


class DownloadContractTests(unittest.TestCase):
    def test_long_case_recfile_collision_is_not_treated_as_recoverable(self):
        case_id = "canshuwa01_ctrl_a0_k095_t100_o1700"
        truncated = f"{case_id}.txt"[:32]
        files = [
            {
                "path": f"work/{truncated}",
                "size_bytes": 127_418_376,
                "role": _file_role(f"work/{truncated}", case_id),
                "standard": True,
            },
            {
                "path": "work/cRec.txt",
                "size_bytes": 216_447_264,
                "role": _file_role("work/cRec.txt", case_id),
                "standard": True,
            },
        ]

        audit = _standard_audit(files, case_id)

        self.assertEqual(files[0]["role"], "collided_output")
        self.assertFalse(_standard_selected(files[0]["path"], case_id))
        self.assertEqual(files[1]["role"], "channel_recorder")
        self.assertTrue(audit["filename_collision"])
        self.assertFalse(audit["standard_complete"])
        self.assertEqual(audit["missing_roles"], ["main_output", "recorder_output"])
        self.assertEqual(audit["collided_output_paths"], [f"work/{truncated}"])
        self.assertEqual(_archive_aliases(files[0], case_id), [])

    def test_short_case_has_distinct_main_and_recorder(self):
        case_id = "short_case"
        files = [
            {"path": f"work/{case_id}.txt", "role": "main_output"},
            {"path": f"work/{case_id}_Rec.txt", "role": "recorder_output"},
        ]

        audit = _standard_audit(files, case_id)

        self.assertTrue(audit["standard_complete"])
        self.assertFalse(audit["filename_collision"])
        self.assertEqual(
            _archive_aliases(files[1], case_id),
            ["work/prj_Rec.txt", f"work/{case_id}_Rec.txt"],
        )

    def test_changepar_alias_uses_formal_case_id(self):
        case_id = "case01"
        entry = {"path": "work/changepar.dat", "role": "changepar"}
        self.assertEqual(
            _archive_aliases(entry, case_id),
            ["work/changeparcase01.dat"],
        )

    def test_driver_output_prefix_can_differ_from_display_case(self):
        files = [
            {"path": "work/m06.txt", "size_bytes": 100},
            {"path": "work/m06_Rec.txt", "size_bytes": 0},
            {"path": "work/cRec.txt", "size_bytes": 200},
        ]
        self.assertEqual(_infer_output_prefix([f["path"] for f in files], "m649"), "m06")
        classified = [
            {**f, "role": _file_role(f["path"], "m06"), "standard": True}
            for f in files
        ]
        audit = _standard_audit(classified, "m649")
        self.assertTrue(audit["standard_complete"])
        self.assertEqual(audit["main_output_paths"], ["work/m06.txt"])
        self.assertEqual(audit["recorder_output_paths"], ["work/m06_Rec.txt"])


if __name__ == "__main__":
    unittest.main()
