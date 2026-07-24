import importlib.util
import argparse
import json
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "protocol_events.py"
SPEC = importlib.util.spec_from_file_location("protocol_events", MODULE)
EVENTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVENTS)


class ProtocolEventClassificationTest(unittest.TestCase):
    def test_free5gc_transition(self):
        result = EVENTS.classify("free5gc", "Handle event[Gmm Message], transition from [Deregistered] to [Authentication]")
        self.assertEqual("state_change", result["kind"])
        self.assertEqual("Deregistered", result["state_before"])
        self.assertEqual("Authentication", result["state_after"])

    def test_oai_registration_accept_state(self):
        result = EVENTS.classify("oai", "[amf_n1] Set 5GMM state to _5GMM_REGISTERED")
        self.assertEqual("state_change", result["kind"])
        self.assertEqual("_5GMM_REGISTERED", result["state_after"])

    def test_open5gs_security_reject(self):
        result = EVENTS.classify("open5gs", "[gmm] WARNING: Security mode reject : Cause[24]")
        self.assertEqual("error", result["kind"])
        self.assertEqual("SecurityModeReject", result["message"])

    def test_finalize_reads_only_platform_core_logs_and_merges_sessions(self):
        fixtures = {
            "open5gs": (
                Path("raw/open5gs/core-session-001/core.log"),
                "[amf] InitialUEMessage\n",
            ),
            "free5gc": (
                Path("raw/free5gc/core-session-001/20260724_120000/free5gc.log"),
                "Handle InitialUEMessage\n",
            ),
            "oai": (
                Path("raw/oai/core-session-001/core.log"),
                "[ngap] Initial UE message\n",
            ),
        }
        for platform, (relative_log, content) in fixtures.items():
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                run_dir = Path(directory)
                core_log = run_dir / relative_log
                core_log.parent.mkdir(parents=True)
                core_log.write_text(content, encoding="utf-8")
                (run_dir / "raw/console.log").write_text(
                    "ERROR: this console line must not be parsed\n", encoding="utf-8")
                session_dir = next(
                    parent for parent in core_log.parents
                    if parent.name.startswith("core-session-"))
                (session_dir / "launcher.log").write_text(
                    "ERROR: launcher diagnostics are not protocol evidence\n", encoding="utf-8")
                (session_dir / "session.json").write_text(
                    json.dumps({
                        "platform": platform,
                        "run_id": "test-run",
                        "session_id": "core-session-001",
                        "status": "completed",
                    }),
                    encoding="utf-8")
                (run_dir / "run-manifest.json").write_text(
                    json.dumps({"status": "running", "warnings": ["kept"]}),
                    encoding="utf-8")

                EVENTS.finalize(argparse.Namespace(
                    platform=platform,
                    run_id="test-run",
                    run_dir=str(run_dir)))

                events = [
                    json.loads(line)
                    for line in (run_dir / "core-events.jsonl")
                    .read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(1, len(events))
                self.assertEqual(str(relative_log), events[0]["source"])
                manifest = json.loads(
                    (run_dir / "run-manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(["kept"], manifest["warnings"])
                self.assertEqual("core-session-001",
                                 manifest["core_sessions"][0]["session_id"])


if __name__ == "__main__":
    unittest.main()
