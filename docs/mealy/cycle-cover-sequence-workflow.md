# Cycle-cover sequence workflow

Use this workflow when a Mealy SMP cycle/closed-walk cover must be converted
into directly executable `multiSeq` input sequences.

## Stages

1. Generate or confirm the SMP derivative without modifying the original
   hypothesis DOT. Before publishing each Graphviz SVG, parse it as UTF-8 XML
   and require an `<svg>` root element. Do not accept a non-empty file as a
   successful render: a file whose line breaks were escaped as literal `\\n`
   is malformed XML and Inkscape cannot open it. Render to a temporary path and
   publish it only after this check passes.
2. With `--sequence-output`, run the layered signal-constrained route analysis.
   The SMP edges are concrete coverage targets; the original DOT supplies
   closure transitions. Parallel SMP state-pair edges are retained as separate
   targets and reported as an input warning instead of aborting the analysis.
   The base group first selects simple cycles for every simple-coverable target,
   then uses composite closed walks only for the residual targets. Concrete KSI
   self-loops are appended to the base group as standalone tests.
3. When `--extra-output-dir` and `--extra-sequence-output` are supplied, write
   a separate additional group containing every signal-valid length-3/4/5
   simple route that contains an SMP target edge, plus one variant for every
   eligible concrete self-loop on that route. A standalone self-loop repeats
   10 times; an embedded self-loop executes 3 times in each iteration of its
   enclosing route.
4. For every selected closed route, choose the numerically smallest `sN`
   state. If that state occurs more than once in a composite closed walk, use
   its first occurrence in the canonical selected route and rotate the route
   there.
5. On the filtered original DOT, compute one shortest access sequence from
   `s0` to that state. Breadth-first search minimizes the number of inputs;
   equal-length paths use the original DOT transition appearance order.
6. Materialize merged SMP inputs according to the requested policy. `first`
   selects the first DOT input on every merged edge. `expand` emits the
   Cartesian product of all edge inputs.
7. For each concrete route variant, append the same complete route the
   requested number of times. Do not choose a different merged input in each
   repetition.
8. Simulate every generated line against the filtered original DOT. Require
   every transition to exist and every repeated route to finish at its chosen
   start state.
9. Store the final `.seq` under the experiment record's `inputs/` directory.
   Record the command, source hashes, line count, expansion policy, repetition
   count and output SHA-256 in the experiment record.

The closure graph must be deterministic for every `(state,input)` pair.
Excluded and synthetic-start states must not occur in access paths or repeated
routes. Sequence files contain one input sequence per line, use one ASCII
space between inputs, contain no comments or blank lines, and end with a
newline.

## CLI

```powershell
& D:\anaconda3\python.exe D:\state-learning-lab\projects\state-learning-tools\analysis\cycle_cover\analyze_cycle_cover.py `
  --dot <record>\analysis\derived\hypothesis_N_smp.dot `
  --closure-dot <record>\evidence\hypotheses\hypothesis_N.dot `
  --exclude-state s2 `
  --required-output authenticationRequest `
  --required-output securityModeCommand `
  --signal-mode output-only `
  --output-dir <record>\analysis\derived\cycle_cover `
  --basename hypothesis_N `
  --formats svg `
  --sequence-output <record>\inputs\hypothesis_N_cycle_cover_repeat10.seq `
  --sequence-start-state s0 `
  --sequence-repeat-count 10 `
  --sequence-merged-input-policy expand `
  --extra-output-dir <record>\analysis\derived\cycle_cover\extra `
  --extra-basename hypothesis_N_extra `
  --extra-sequence-output <record>\inputs\hypothesis_N_extra_cycle_cover_repeat10.seq `
  --overwrite
```

Omit `--sequence-output` when only the cover and per-route SVGs are required.
The other sequence options have no effect unless sequence output is enabled.
Supply both extra output options to enable the additional group; its JSON,
report, SVG directory and `.seq` are independent from the base group.

## Manual repeated-cycle trace analysis

### Authoritative trace input

Use `statelearner_trace.jsonl` as the only authoritative trace for model
comparison, executed-sequence coverage, repeated-cycle alignment, and register
inference. Read every valid JSONL record and preserve the original file hash.

Do **not** use `statelearner_trace.cleaned.jsonl` as an analysis input. It is a
derived filtering view and may omit whole sequences or individual observations.
It can be retained for audit or human browsing only. The runner console can
confirm that logical sequences completed, but it cannot replace the complete
trace for step-level output comparison.

### Mandatory raw-data materialization

Before any alignment or register inference, freeze one explicit raw run window.
For a run larger than the record's Git boundary, first create and hash an
external snapshot under `D:\state-learning-lab\run-data\<platform>\<run-id>`
and record it in `artifacts.yaml`. Never point analysis at a live launcher
directory, a wildcard collection, or an append-only root log.

The only permitted conversion from the runner trace to an inference input is a
**lossless materialization**:

1. Take the raw `statelearner_trace.jsonl` from the frozen snapshot.
2. Validate its UTF-8 JSONL records and the inference adapter's grouping
   contract against the exact `.seq`, selected `sequence_export.cycles`, and
   register-inference YAML.
3. Copy its bytes unchanged to `evidence/statelearner_trace.jsonl` and require
   equal byte size and SHA-256 before using it.
4. Write a derived trace-materialization JSON manifest that records both paths,
   hashes, record/group counts, selected-cycle-to-trace matches, and the fact
   that payload transformation was `none`.

Use the versioned preparation tool; the output path must be exactly the
configuration's `inputs.trace` path, so the subsequent inference cannot point
at a different file:

```powershell
D:\anaconda3\python.exe D:\state-learning-lab\projects\state-learning-tools\analysis\register_inference\experiments\prepare_register_inference_trace.py `
  --config <record>\analysis\derived\register_inference\<name>-inference.yaml `
  --source-trace D:\state-learning-lab\run-data\<platform>\<run-id>\statelearner_trace.jsonl `
  --evidence-trace <record>\evidence\statelearner_trace.jsonl `
  --manifest <record>\analysis\derived\register_inference\<name>-trace-materialization.json
```

This preparation tool intentionally rejects the legacy
`sequence_export.routes` interface. It requires `sequence_export.cycles`, each
selected variant's `line_number`, exactly one `sequence_id` group whose final
`sequence_inputs` equals that `.seq` line, equal record/input counts, and a
step-by-step match between `abstract_io.input` and the expected logical input.
Sparse numeric `sequence_id` values are allowed; uniqueness of the matched
group is required.

No CSV export, `cleaned` trace, log grep, packet re-decode, field backfill,
deduplication, sorting, null-field deletion, or cross-session concatenation is
an inference input. The adapter may only perform three in-memory conversions:
add the physical JSONL line number, parse a **configured** numeric field as an
integer, and parse a **configured** signal field as a Boolean. It must never
write those converted values back into evidence. If the complete raw trace
lacks a required field or fails the grouping contract, record the run as
unsuitable for that inference configuration rather than repairing the data.

Analyze one `.seq` line at a time. Use the cycle-cover JSON to recover the
line's concrete cycle, prefix length, loop length and concrete loop inputs.
The JSON interface is `sequence_export.cycles`; use `cycle_id` and each
variant's `loop_inputs`, never a legacy route key.
Split the trace into prefix `l` and repetitions 1 through 10. Keep the prefix
and repetition 1 as setup context, but fit repeated behavior only from
repetitions 2 through 10.

Align the nine analyzed repetitions by the same cycle edge, logical input,
message direction, message type and field path. A missing, extra, duplicate,
retransmitted or reordered message is an anomaly; do not hide it by shifting
the later alignment.

For each aligned numeric, Boolean or identifier subfield, write the observed
series `r2 ... r10` and heuristically try these simple logical-register
candidates in order:

1. `r' = r` — unchanged.
2. `r' = 0` — cleared or reset.
3. `r' = c` — assigned a fixed observed constant.
4. `r' = r + 1`.
5. `r' = r - 1`.
6. `r' = r + k` for one fixed `k`.
7. `r' = (r + k) mod 2^w` when the protocol field width `w` is known.
8. `r' = 1 - r` for a Boolean/two-state toggle.
9. `r' = input_field` when the value is copied from the corresponding request.

Here `r` is the candidate AMF logical state at one occurrence of the edge and
`r'` is its candidate state at the next occurrence. A matching series is an
observational candidate, not proof that an AMF variable with that name exists.
If several candidates fit, report all of them from simplest to most specific.
If no candidate explains all samples, report the first breaking repetition,
the closest candidate and the exact observed values as an anomaly or
`no simple candidate`.

After finding a candidate, inspect the experiment's exact AMF source snapshot
and exact UERANSIM/SUL revision. Look for a matching context member and
unchanged, clear, assignment, increment, decrement, wrap or copy operation on
the corresponding edge. Report:

- line, cycle, edge, direction, message and field path;
- values from repetitions 2 through 10;
- candidate equation(s);
- first anomaly, if any;
- possible AMF context member/update site with revision, file, function and
  line range;
- confidence: `high` for a direct source match, `medium` for a consistent but
  indirect match, or `low` for a trace-only candidate.

Do this exploration manually first. Do not add a fixed extraction or
inference script until real trace results show that the edge boundaries,
message names and field paths are stable.

### Required inference record

The register-inference configuration, materialization manifest, machine-readable
candidate JSON, reader-facing Markdown summary and complete Excel audit workbook
are one inseparable derived set. Schema v3 inference requires both `--report` and
`--workbook`; a generic model-consistency report or a launcher log is not a
substitute. The Markdown summary and workbook metadata must link the complete
evidence trace, exact `.seq`, original DOT, cycle-cover JSON, configuration and
result JSON by SHA-256. They must state the fitted repetitions, configured fields,
unknown branches, alignment anomalies, candidate grade, and the distinction
between `partition_divergent` and `confirmed_observational_conflict`.

The required Markdown is the H13-style reader summary: one fixed-layout HTML
table with every concrete DOT edge group and the four columns “cycle, edge and
nodes / edge candidate / input register / candidate grade”. It preserves tied
global candidates but refers detailed material to the workbook. The required
Excel workbook contains separate filtered sheets for edge coordination, every
`cycle_id` edge use, deterministic `V01…` variants and complete `loop_inputs`,
candidate records, and reconciliation evidence. A repeated edge appears once in
every cycle-use row. The workbook must keep all tied global, intersection,
non-consensus and cycle-local candidates, expose candidate grade as its own
column, and distinguish an empty intersection from a recorded
`confirmed_observational_conflict`. Message pairs break after `/`; the Markdown
uses HTML `colgroup` fixed widths, while the workbook freezes its header row,
enables filters, wraps text and uses readable column widths.

Workbook render previews belong in a system temporary directory and are inspected there. The
artifact-tool `<workbook>.inspect.ndjson` sidecar is an intermediate verification file, not evidence
or a deliverable: remove it after successful validation and never place it under an experiment record.
