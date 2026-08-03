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

Analyze one `.seq` line at a time. Use the cycle-cover JSON to recover the
line's concrete cycle, prefix length, loop length and concrete loop inputs.
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
