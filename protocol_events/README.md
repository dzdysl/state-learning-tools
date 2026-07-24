# Protocol event tools

`finalize` reads only platform-owned protocol logs:

- `raw/open5gs/core-session-*/core.log`
- `raw/free5gc/core-session-*/*/free5gc.log`
- `raw/oai/core-session-*/core.log`

It intentionally excludes `raw/console.log` and every session `launcher.log`, then merges valid
`session.json` records into `run-manifest.json` before returning.

`protocol_events.py` converts platform-native core logs and UERANSIM traces into a
versioned JSONL evidence stream.  It deliberately emits only facts present in the
source line; `raw` and `raw_ref` always preserve the provenance of each event.
The current `protocol-events/v2` contract is documented in `protocol-events-v2.md`;
use `level` for source severity and `kind` for behavioral classification.

Examples:

```bash
python3 protocol_events.py parse --platform free5gc --run-id r1 --session core-session-001 \
  --input raw/free5gc/core-session-001/free5gc.log --output core-events.jsonl
python3 protocol_events.py normalize-ue --run-id r1 --input statelearner_trace.jsonl --output ue-events.jsonl
python3 protocol_events.py timeline --run-dir runs/r1
```
