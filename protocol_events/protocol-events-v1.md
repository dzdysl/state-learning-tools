# Protocol events v1

Every JSONL event has `schema_version`, `event_id`, `platform`, `run_id`,
`session_id`, `source`, `timestamp`, `kind`, `layer`, `direction`, `message`,
`action`, `state_before`, `state_after`, `identifiers`, `raw_ref`, and `raw`.
Absent facts are represented by `null`; no parser infers them. `timestamp` is an
ISO-8601 value when supplied by the source, otherwise `null`.

`kind` is one of `ngap_rx`, `ngap_tx`, `nas_ul`, `nas_dl`, `core_action`,
`state_change`, `context`, `error`, or `ue_observation`.  `raw_ref` is the
one-based source-line reference, so every normalized item is independently
auditable. `protocol-events.jsonl` is a deterministic timestamp/order merge of
`ue-events.jsonl` and `core-events.jsonl`.
