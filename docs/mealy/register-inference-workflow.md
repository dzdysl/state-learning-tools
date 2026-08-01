# Register-state-machine inference workflow

`analysis/register_inference/analyze_register_machine.py` turns explicitly
mapped integer observations into a reviewable, edge-local register-update
candidate fit.  It is a first-stage inference aid, not proof of an AMF's
internal member names or source-level control flow.

## Inputs and configuration

Keep the experiment-specific YAML configuration in the record's `inputs/`
directory.  It names an immutable Mealy DOT, a frozen JSONL trace and dotted
paths for source state, target state, logical input, integer register values
before/after each transition, and optional integer input variables.  Every
trace record must map to exactly one labelled DOT transition.

The tool deliberately retains all observations, including repeated cycle
iterations.  It neither deduplicates abstract input/output rows nor silently
repairs missing, reordered or unmappable records.  Correct cycle alignment
remains a prerequisite: for repeated-cycle tests, retain the access prefix and
first iteration as setup context and map iterations 2–10 explicitly.

Use the bundled generic example as a starting point:

```powershell
$tool = 'D:\state-learning-lab\projects\state-learning-tools\analysis\register_inference\analyze_register_machine.py'
& D:\anaconda3\python.exe $tool run `
  --config <record>\inputs\register-inference.yaml `
  --output-dir <record>\analysis\derived\register_inference
```

`run` creates `prepared.json`, `candidates.json` and `fit.json`.  The three
subcommands can instead be run independently when comparing a new cleaner,
candidate generator or fitter.

## Initial inference contract

The built-in `simple_scalar_v1` generator may produce only these integer
update forms: `r'=r`, `r'=c`, `r'=r+k`, and `r'=i`.  The
`z3_scalar_edge_v1` fitter tests each formula against all observations for an
edge/register pair and picks the first satisfiable candidate according to the
configured priority.  An unsatisfiable group reports the failing observations.

There is intentionally no KSI-specific field name, fixed initial register
value, special DOT edge, output-equals-register assumption or automatic
access-path reconstruction.

`ite`, threshold/periodic guards, modular arithmetic, multi-register formulas,
CEGIS and inferred-model DOT rendering are reserved extension points.  Do not
interpret their absence as a negative finding.  Preserve candidate equations
and diagnostics in the record, then correlate a promising candidate with the
exact AMF and UERANSIM/SUL source revisions before reporting a causal claim.
