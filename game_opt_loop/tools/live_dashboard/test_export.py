import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("live_export", Path(__file__).with_name("export.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class InputBoundary(unittest.TestCase):
    def test_private_auth_rejected_without_open(self):
        for name in ["private-auth/key.json", "a/../private-auth/key.json", "/private-auth/key.json"]:
            with self.assertRaises(ValueError):
                m.safe_relative(name)
        with self.assertRaises(ValueError):
            m.Source("/nonexistent/private-auth")

    def test_symlink_and_oversize_and_hash_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d).resolve()
            (p/"data.json").write_bytes(b"{}")
            (p/"link.json").symlink_to(p/"data.json")
            with self.assertRaises(OSError):
                m.Source(p).read("link.json")
            with self.assertRaises(ValueError):
                m.Source(p).read("data.json", "0"*64)
            with (p/"big.json").open("wb") as f:
                f.truncate(m.JSON_CAP+1)
            with self.assertRaises(ValueError):
                m.Source(p).read("big.json")

    def test_missing_terminal_and_unknown_registry_rejected(self):
        with self.assertRaises(ValueError):
            m.terminal({"completed": "true"})
        with tempfile.TemporaryDirectory() as d:
            p = Path(d).resolve()
            (p/"registry.json").write_text('{"schema":"unknown","entries":[]}')
            with self.assertRaises(ValueError):
                m.eval_records(m.Source(p), "registry.json")

    def test_completed_eval_keeps_abort_and_null_without_identifiers(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d).resolve()
            command = {"finished_unix_s": 1, "exit_code": 1, "argv": ["credential-not-exported"]}
            report = {"verdict": {"status": "ABORT", "reason": "cold rejected /Users/person/session c531e0a 192.0.2.1"},
                      "qualification": {"eligible": False, "device": {"serial": "private-serial"}},
                      "metrics": {"frame_p95": None, "fps_mean": None}, "reverted": False}
            for name, value in [("command.json", command), ("report.json", report)]:
                (p/name).write_bytes(m.encode(value))
            entry = {"terminal": True, "id": "actual-eval", "command": "command.json",
                     "command_sha256": m.digest((p/"command.json").read_bytes()), "report": "report.json",
                     "report_sha256": m.digest((p/"report.json").read_bytes())}
            (p/"registry.json").write_bytes(m.encode({"schema": "hgamey/live_eval_input_registry@1", "entries": [entry]}))
            records = m.eval_records(m.Source(p), "registry.json")
            self.assertEqual(records[0]["status"], "ABORT")
            self.assertIsNone(records[0]["receiver_summary"]["metrics"]["fps_mean"])
            self.assertFalse(records[0]["optimized_effect_proven"])
            self.assertFalse(records[0]["restoration"])
            encoded = m.encode(records).decode()
            for secret in ("credential-not-exported", "private-serial", "c531e0a", "192.0.2.1", "/Users/person"):
                self.assertNotIn(secret, encoded)
            entry["terminal"] = False
            (p/"registry.json").write_bytes(m.encode({"schema": "hgamey/live_eval_input_registry@1", "entries": [entry]}))
            with self.assertRaises(ValueError):
                m.eval_records(m.Source(p), "registry.json")

    def test_root_alias_is_rejected_before_reading_owned_auth_fixture(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d).resolve()
            (p/"private-auth").mkdir()
            (p/"private-auth/safe.json").write_text("{}")
            (p/"ordinary-alias").symlink_to(p/"private-auth", target_is_directory=True)
            with self.assertRaises(OSError):
                m.Source(p/"ordinary-alias")
            (p/"ancestor-alias").symlink_to(p, target_is_directory=True)
            (p/"child").mkdir()
            with self.assertRaises(OSError):
                m.Source(p/"ancestor-alias/child")

    def test_latest_is_command_time_not_registry_order_and_prepare_error_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d).resolve()
            entries = []
            for name, finished, status in [("new", 20, "BLOCKED"), ("old", 10, "ABORT")]:
                command = m.encode({"finished_unix_s": finished, "exit_code": 1})
                report = m.encode({"status": status, "error": "thermal_snapshot exited 82: "})
                (p/(name+"-cmd.json")).write_bytes(command)
                (p/(name+"-report.json")).write_bytes(report)
                entries.append({"terminal": True, "id": name, "kind": "existing_prepare", "command": name+"-cmd.json",
                    "command_sha256": m.digest(command), "report": name+"-report.json", "report_sha256": m.digest(report)})
            (p/"registry.json").write_bytes(m.encode({"schema": "hgamey/live_eval_input_registry@1", "entries": entries}))
            records = m.eval_records(m.Source(p), "registry.json")
            self.assertEqual([r["id"] for r in records], ["old", "new"])
            self.assertEqual(records[-1]["receiver_summary"]["reason"], "thermal_snapshot exited 82: ")
            self.assertEqual(records[-1]["receiver_summary"]["metric_scope"], "NOT_RUN: prepare only")

    def test_restoration_and_checkpoint_do_not_promote_blocked_prepare(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d).resolve()
            values = {"cmd.json": {"finished_unix_s": 10, "exit_code": 1},
                "report.json": {"status": "BLOCKED", "error": "thermal_snapshot exited 82: "},
                "restore-cmd.json": {"finished_unix_s": 20, "exit_code": 0},
                "restore.json": {"status": "RESTORED_REQUIRES_NEW_SESSION", "old_samples_revalidated": False}}
            hashes = {}
            for name, value in values.items():
                b = m.encode(value)
                (p/name).write_bytes(b)
                hashes[name] = m.digest(b)
            review = {"schema": "phonefarm/live_existing_prepare_terminal_review@1",
                "status": "BLOCKED_BEFORE_SELECTED_CONTROL_AND_BASELINES", "completed_at_unix_s": 30,
                "prepare": {"status": "BLOCKED", "exit_code": 1, "performance_baselines_collected": 0,
                    "performance_baselines_requested": 3, "selected_control_write": "NOT_RUN",
                    "main_remote_tools_installed": True, "initial_snapshot_restore_graph_and_state_created": True},
                "warmup": {"sample_count": 7, "span_s": 31.71, "max_single_sensor_range_c": 2.0, "hal_statuses": [0]*7},
                "recovery": {"exit_code": 0, "status": "RESTORED_REQUIRES_NEW_SESSION", "full_initial_snapshot_equal": True,
                    "owned_groups_absent_verified_by_existing_receiver": True, "remote_session_permanently_revoked": True,
                    "normal_lease_after_recovery_exit_code": 0},
                "artifact_sha256": {str(p/name): h for name, h in hashes.items()}}
            rb = m.encode(review)
            (p/"review.json").write_bytes(rb)
            entry = {"terminal": True, "id": "prepare", "kind": "existing_prepare", "command": "cmd.json",
                "command_sha256": hashes["cmd.json"], "report": "report.json", "report_sha256": hashes["report.json"],
                "restoration_command": "restore-cmd.json", "restoration_command_sha256": hashes["restore-cmd.json"],
                "restoration_report": "restore.json", "restoration_report_sha256": hashes["restore.json"],
                "checkpoint_review": "review.json", "checkpoint_review_sha256": m.digest(rb)}
            registry = {"schema": "hgamey/live_eval_input_registry@1", "entries": [entry]}
            (p/"registry.json").write_bytes(m.encode(registry))
            record = m.eval_records(m.Source(p), "registry.json")[0]
            self.assertEqual(record["status"], "BLOCKED")
            self.assertEqual(record["metrics"][0]["value"], "0 / 3")
            self.assertIn("RESTORED_REQUIRES_NEW_SESSION", record["restoration"])
            self.assertFalse(record["promotion_allowed"])
            review["recovery"]["exit_code"] = 1
            rb = m.encode(review)
            (p/"review.json").write_bytes(rb)
            entry["checkpoint_review_sha256"] = m.digest(rb)
            (p/"registry.json").write_bytes(m.encode(registry))
            with self.assertRaises(ValueError):
                m.eval_records(m.Source(p), "registry.json")
            review["recovery"]["exit_code"] = 0
            review["artifact_sha256"][str(p/"report.json")] = "0"*64
            rb = m.encode(review)
            (p/"review.json").write_bytes(rb)
            entry["checkpoint_review_sha256"] = m.digest(rb)
            (p/"registry.json").write_bytes(m.encode(registry))
            with self.assertRaises(ValueError):
                m.eval_records(m.Source(p), "registry.json")


if __name__ == "__main__":
    unittest.main()
