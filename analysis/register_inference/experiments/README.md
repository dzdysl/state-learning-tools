# Experimental repeated-cycle ngKSI inference

`infer_cycle_ngksi_regions.py` builds typed, temporally ordered inference
regions between configured AMF downlink KSI fields, then emits signal-gated
leaf, wrap-threshold, or counterexample-derived candidates for each concrete
DOT edge. It is a candidate-discovery aid, not proof of an AMF implementation
variable or update site.

```powershell
& D:\anaconda3\python.exe .\infer_cycle_ngksi_regions.py `
  --config <region-inference.yaml> --output <result.json>
```

Minimal configuration:

```yaml
schema_version: 2
inputs:
  dot: hypothesis.dot
  trace: statelearner_trace.jsonl
  cycle_cover: cycle_cover.json
  sequence_file: cycle_repeat.seq
mapping:
  downlink_ksi_by_output:
    authenticationRequest: downlink_side.fields.auth_request_ksi_value
    securityModeCommand: downlink_side.fields.smc_ksi_value
  uplink_ksi_by_input:
    registrationRequest: ue_side.fields.registration_ksi_value
    registrationRequestGUTI: ue_side.fields.registration_ksi_value
  signal_definitions:
    - id: initial_uplink_context
      path: ue_side.fields.isInitMsg
      value_type: boolean
      match:
        input_symbols:
          - registrationRequest
          - registrationRequestGUTI
      phase: before_numeric_inputs
  d_states: []
analysis:
  repetitions: [2, 10]
  min_consecutive_support: 3
  max_numeric_depth: 1
  max_derived_signal_depth: 1
```

The last KSI downlink in repetition 1 anchors the first region in repetition 2.
Every subsequent KSI downlink closes one region; its terminal DOT edge is the
edge being fitted. A region is `(r_before, ordered_observation_items, r_after)`.
Each item preserves type, message, field path, occurrence and trace position.
Signal selectors accept any input-symbol list or the sole wildcard `"*"`;
message names are not hard-coded in the inference engine.  Each
`uplink_ksi_by_input` value may likewise be one path or an ordered path list,
so multiple numeric fields on one event retain configuration order.

Configured signal slots form outer `signal_guard` layers even when a signal is
constant in the selected trace. Unobserved and insufficiently supported
branches remain explicit `unknown` nodes. Leaf candidates are `r'=c`,
`r'=r+k`, and `r'=i_j+k`. Only when leaves fail does the adapter try
`threshold_guard` nodes of the fixed wrap form `ite(x<T,f,0)`; only when both
leaves and wrap trees fail may it enumerate one supported input equality as a
`derived_value_guard`. The three node kinds and their depth budgets remain
separate, and an observed value such as `7` receives no automatic protocol
semantics.
