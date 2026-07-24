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
        raw = "[gmm] WARNING: Security mode reject : Cause[24]"
        result = EVENTS.classify("open5gs", raw)
        self.assertEqual("error", result["kind"])
        self.assertEqual("SecurityModeReject", result["message"])
        event = EVENTS.event(
            "open5gs", "run", "core-session-001", "core.log", 1, raw, result)
        self.assertEqual("warning", event["level"])
        self.assertEqual("protocol-events/v2", event["schema_version"])

    def test_free5gc_preserves_warning_and_error_levels(self):
        warning_raw = (
            'time="2026-07-24T15:30:20+08:00" level="warning" '
            'msg="Login to Webconsole FTP fail" CAT="CGF" NF="CHF"')
        warning = EVENTS.classify("free5gc", warning_raw)
        self.assertEqual("warning", warning["kind"])
        warning_event = EVENTS.event(
            "free5gc", "run", "core-session-001", "free5gc.log", 1,
            warning_raw, warning)
        self.assertEqual("warning", warning_event["level"])

        error_raw = (
            'time="2026-07-24T15:30:30+08:00" level="error" '
            'msg="UE state mismatch" CAT="Gmm" NF="AMF"')
        error = EVENTS.classify("free5gc", error_raw)
        self.assertEqual("error", error["kind"])
        error_event = EVENTS.event(
            "free5gc", "run", "core-session-001", "free5gc.log", 2,
            error_raw, error)
        self.assertEqual("error", error_event["level"])

    def test_normalized_ue_event_has_info_level(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "statelearner_trace.jsonl"
            output = root / "ue-events.jsonl"
            source.write_text(
                json.dumps({
                    "observed_at_utc": "2026-07-24T07:30:30Z",
                    "socket_session_id": 1,
                    "abstract_io": {
                        "input": "registrationRequest",
                        "output": "identityRequest",
                    },
                }) + "\n",
                encoding="utf-8",
            )
            EVENTS.normalize_ue(argparse.Namespace(
                platform="free5gc",
                run_id="run",
                input=str(source),
                output=str(output),
            ))
            record = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("info", record["level"])
            self.assertEqual("protocol-events/v2", record["schema_version"])

    def test_info_level_error_text_keeps_source_level(self):
        raw = (
            'time="2026-07-24T15:31:13+08:00" level="info" '
            'msg="Handle SCTP Connection Error" CAT="Ngap" NF="AMF"')
        result = EVENTS.classify("free5gc", raw)
        self.assertEqual("error", result["kind"])
        record = EVENTS.event(
            "free5gc", "run", "core-session-001", "free5gc.log", 1,
            raw, result)
        self.assertEqual("info", record["level"])

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
