#!/usr/bin/env python3
"""Normalize Open5GS, free5GC, OAI and UERANSIM evidence into JSONL."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "protocol-events/v2"
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OPEN_TS = re.compile(r"(?P<date>\d{2}/\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2}\.\d{3})")
ISO_TS = re.compile(r"(?P<time>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)")
OAI_TS = re.compile(r"\[(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2}\.\d{3})\]")

def clean(line: str) -> str:
    return ANSI.sub("", line).rstrip("\n")

def timestamp(line: str):
    m = ISO_TS.search(line)
    if m:
        return m.group("time")
    m = OAI_TS.search(line)
    if m:
        return f"{m.group('date')}T{m.group('time')}Z"
    # Open5GS omits year/zone. Keep source clock unambiguous rather than inventing one.
    m = OPEN_TS.search(line)
    return f"source-clock:{m.group('date')}T{m.group('time')}" if m else None

def log_level(line: str, semantic_kind: str | None = None) -> str:
    structured = re.search(
        r'\blevel\s*=\s*["\']?(trace|debug|info|notice|warn|warning|error|critical|fatal|panic)\b',
        line,
        re.I,
    )
    if structured:
        value = structured.group(1).lower()
    else:
        prefixed = re.search(
            r'(?:^|[\s\[])'
            r'(trace|debug|info|notice|warn|warning|error|critical|fatal|panic)'
            r'(?=[:\]"\s])',
            line,
            re.I,
        )
        value = prefixed.group(1).lower() if prefixed else ""
    if value in {"warn", "warning"}:
        return "warning"
    if value in {"critical", "fatal", "panic"}:
        return "error"
    if value:
        return value
    return "error" if semantic_kind == "error" else "info"

def identifiers(line: str):
    values = {}
    for key, pat in {
        "ran_ue_ngap_id": r"(?:RAN[_ ]UE[_ ]NGAP[_ ]ID|RanUeNgapID)[:\[]\s*([0-9]+)",
        "amf_ue_ngap_id": r"(?:AMF[_ ]UE(?:[_ ]NGAP)?[_ ]ID|AmfUeNgapID)[:\[]\s*([0-9]+)",
        "imsi": r"\b(imsi-[0-9]+|IMSI\s*([0-9]+))",
        "suci": r"\b(suci-[A-Za-z0-9-]+|SUCI\[([^]]+)\])",
        "guti": r"\b(?:GUTI|guti)[:\[]?([A-Za-z0-9-]+)",
    }.items():
        match = re.search(pat, line, re.I)
        if match:
            values[key] = next((part for part in match.groups() if part), match.group(0))
    return values

def classify(platform: str, line: str):
    lower = line.lower()
    result = {"kind": None, "layer": None, "direction": None,
              "message": None, "action": None, "state_before": None, "state_after": None}
    def set_(kind, layer, direction=None, message=None, action=None):
        result.update(kind=kind, layer=layer, direction=direction, message=message, action=action)
    if platform == "free5gc":
        if "handle initialuemessage" in lower: set_("ngap_rx", "ngap", "uplink", "InitialUEMessage")
        elif "handle uplinknastransport" in lower: set_("nas_ul", "nas", "uplink", "UplinkNASTransport")
        elif "send downlink nas transport" in lower: set_("ngap_tx", "ngap", "downlink", "DownlinkNASTransport")
        elif "transition from [" in lower:
            m = re.search(r"transition from \[([^]]+)] to \[([^]]+)]", line, re.I)
            set_("state_change", "gmm", None, action="gmm_transition")
            if m: result.update(state_before=m.group(1), state_after=m.group(2))
        elif re.search(r"(?:handle|send) (registration|identity|authentication|security mode|deregistration)", line, re.I):
            m = re.search(r"(?:handle|send) (.+)$", line, re.I); set_("core_action", "gmm", action=m.group(1) if m else line)
    elif platform == "oai":
        if "initial ue message" in lower: set_("ngap_rx", "ngap", "uplink", "InitialUEMessage")
        elif "ul_nas_data_ind" in lower or "received uplink nas message" in lower: set_("nas_ul", "nas", "uplink", "UplinkNAS")
        elif "set 5gmm state to" in lower:
            m = re.search(r"set 5gmm state to\s+([^\s]+)", line, re.I); set_("state_change", "gmm", action="set_5gmm_state")
            if m: result["state_after"] = m.group(1)
        elif "update ue state" in lower:
            m = re.search(r"state\s+([^)]*)", line, re.I); set_("state_change", "gmm", action="update_ue_state")
            if m: result["state_after"] = m.group(1)
        elif re.search(r"(?:sending|encoded) (authentication|securitymode|registration|identity).*message", lower):
            m = re.search(r"(?:sending|encoded) ([A-Za-z -]+?)(?: message| with| len|$)", line, re.I); set_("nas_dl", "nas", "downlink", m.group(1).strip() if m else None)
        elif "itti" in lower: set_("core_action", "itti", action=line)
    elif platform == "open5gs":
        if "initialuemessage" in lower: set_("ngap_rx", "ngap", "uplink", "InitialUEMessage")
        elif "registration request" in lower: set_("nas_ul", "nas", "uplink", "RegistrationRequest")
        elif "deregistration request" in lower: set_("nas_ul", "nas", "uplink", "DeregistrationRequest")
        elif "security mode reject" in lower: set_("error", "nas", "uplink", "SecurityModeReject")
        elif "nas mac verification failed" in lower: set_("error", "nas", None, "NasMacVerificationFailed")
        elif "ue context release" in lower: set_("context", "ngap", "downlink", action="ue_context_release")
        elif "number of amf-ues" in lower or "number of gnb-ues" in lower: set_("context", "ngap", action="ue_context_count")
    if result["kind"] is None:
        severity = log_level(line)
        if severity == "warning":
            set_("warning", "core", action=line)
        elif severity == "error" or "error" in lower or "failed" in lower:
            set_("error", "core", action=line)
    return result if result["kind"] else None

def event(platform, run_id, session, source, line_no, raw, kind):
    return {"schema_version": SCHEMA, "event_id": f"{source}:{line_no}", "platform": platform,
            "run_id": run_id, "session_id": session, "source": source, "timestamp": timestamp(raw),
            "level": log_level(raw, kind.get("kind")), **kind, "identifiers": identifiers(raw),
            "raw_ref": {"source": source, "line": line_no}, "raw": raw}

def parse_core(args):
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    with Path(args.input).open("r", encoding="utf-8", errors="replace") as src, output.open("w", encoding="utf-8") as dst:
        for number, line in enumerate(src, 1):
            raw = clean(line); kind = classify(args.platform, raw)
            if kind:
                dst.write(json.dumps(event(args.platform, args.run_id, args.session, Path(args.input).name, number, raw, kind), ensure_ascii=False) + "\n")

def normalize_ue(args):
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    with Path(args.input).open("r", encoding="utf-8", errors="replace") as src, output.open("w", encoding="utf-8") as dst:
        for number, line in enumerate(src, 1):
            try: record = json.loads(line)
            except json.JSONDecodeError: continue
            observed = record.get("observed_at_utc")
            item = {"schema_version": SCHEMA, "event_id": f"ue:{number}", "platform": args.platform,
                    "run_id": args.run_id, "session_id": str(record.get("socket_session_id", "unknown")),
                    "source": Path(args.input).name, "timestamp": observed, "level": "info",
                    "kind": "ue_observation", "layer": "nas",
                    "direction": "bidirectional", "message": record.get("abstract_io", {}).get("input"),
                    "action": record.get("abstract_io", {}).get("output"), "state_before": None, "state_after": None,
                    "identifiers": {}, "raw_ref": {"source": Path(args.input).name, "line": number}, "raw": record}
            dst.write(json.dumps(item, ensure_ascii=False) + "\n")

def timeline(args):
    run_dir = Path(args.run_dir); records = []
    for name in ("ue-events.jsonl", "core-events.jsonl"):
        path = run_dir / name
        if path.exists():
            for order, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
                try: records.append((str(json.loads(line).get("timestamp") or "~"), name, order, json.loads(line)))
                except json.JSONDecodeError: pass
    records.sort(key=lambda item: item[:3])
    temp = run_dir / "protocol-events.jsonl.tmp"; final = run_dir / "protocol-events.jsonl"
    with temp.open("w", encoding="utf-8") as out:
        for _, _, _, record in records: out.write(json.dumps(record, ensure_ascii=False) + "\n")
    temp.replace(final)

def core_log_paths(run_dir: Path, platform: str):
    platform_root = run_dir / "raw" / platform
    if not platform_root.is_dir():
        return []
    if platform == "free5gc":
        return sorted(
            path for path in platform_root.glob("core-session-*/*/free5gc.log")
            if path.is_file()
        )
    return sorted(
        path for path in platform_root.glob("core-session-*/core.log")
        if path.is_file()
    )

def merge_sessions_into_manifest(run_dir: Path, platform: str):
    sessions = []
    platform_root = run_dir / "raw" / platform
    if platform_root.is_dir():
        for path in sorted(platform_root.glob("core-session-*/session.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            record["metadata_path"] = str(path.relative_to(run_dir))
            sessions.append(record)
    manifest_path = run_dir / "run-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {
            "schema_version": "1",
            "platform": platform,
            "status": "finalizing",
        }
    manifest["core_sessions"] = sessions
    temp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(manifest_path)

def finalize(args):
    run_dir = Path(args.run_dir)
    output = run_dir / "core-events.jsonl"
    raw_logs = core_log_paths(run_dir, args.platform)
    with output.open("w", encoding="utf-8") as dst:
        for path in raw_logs:
            session = next((part for part in path.parts if part.startswith("core-session-")), "core-session-unknown")
            with path.open("r", encoding="utf-8", errors="replace") as src:
                for number, line in enumerate(src, 1):
                    raw = clean(line); kind = classify(args.platform, raw)
                    if kind:
                        dst.write(json.dumps(event(args.platform, args.run_id, session, str(path.relative_to(run_dir)), number, raw, kind), ensure_ascii=False) + "\n")
    trace = run_dir / "statelearner_trace.jsonl"
    if trace.exists():
        normalize_ue(argparse.Namespace(platform=args.platform, run_id=args.run_id, input=str(trace), output=str(run_dir / "ue-events.jsonl")))
    timeline(argparse.Namespace(run_dir=str(run_dir)))
    merge_sessions_into_manifest(run_dir, args.platform)

def main():
    parser = argparse.ArgumentParser(); commands = parser.add_subparsers(dest="command", required=True)
    core = commands.add_parser("parse"); core.add_argument("--platform", choices=("open5gs", "free5gc", "oai"), required=True); core.add_argument("--run-id", required=True); core.add_argument("--session", required=True); core.add_argument("--input", required=True); core.add_argument("--output", required=True)
    ue = commands.add_parser("normalize-ue"); ue.add_argument("--platform", choices=("open5gs", "free5gc", "oai"), required=True); ue.add_argument("--run-id", required=True); ue.add_argument("--input", required=True); ue.add_argument("--output", required=True)
    merge = commands.add_parser("timeline"); merge.add_argument("--run-dir", required=True)
    finish = commands.add_parser("finalize"); finish.add_argument("--platform", choices=("open5gs", "free5gc", "oai"), required=True); finish.add_argument("--run-id", required=True); finish.add_argument("--run-dir", required=True)
    args = parser.parse_args(); {"parse": parse_core, "normalize-ue": normalize_ue, "timeline": timeline, "finalize": finalize}[args.command](args)
if __name__ == "__main__": main()
