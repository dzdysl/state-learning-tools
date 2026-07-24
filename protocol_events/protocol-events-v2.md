# Protocol events v2

Every JSONL event has `schema_version`, `event_id`, `platform`, `run_id`,
`session_id`, `source`, `timestamp`, `level`, `kind`, `layer`, `direction`,
`message`, `action`, `state_before`, `state_after`, `identifiers`, `raw_ref`,
and `raw`.

`level` preserves the source log severity and is normalized to `trace`, `debug`,
`info`, `notice`, `warning`, or `error`. Source `warn` becomes `warning`;
`critical`, `fatal`, and `panic` become `error`. UERANSIM observations use
`info`.

`kind` describes event semantics independently of severity. It is one of
`ngap_rx`, `ngap_tx`, `nas_ul`, `nas_dl`, `core_action`, `state_change`,
`context`, `warning`, `error`, or `ue_observation`. A line may therefore have
`kind=error` and `level=info` when its source logger labels an error-handling
action as informational. Use `level` for severity counts and `kind` for
behavioral classification.

Absent facts are represented by `null`; no parser infers them. `timestamp` is
an ISO-8601 value when supplied by the source, otherwise `null`. `raw_ref` is
the one-based source-line reference, so every normalized item is independently
auditable. `protocol-events.jsonl` is a deterministic timestamp/order merge of
`ue-events.jsonl` and `core-events.jsonl`.
