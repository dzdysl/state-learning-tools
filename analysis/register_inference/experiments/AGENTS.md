# Experimental ngKSI inference rules

These rules apply to experimental scripts and analyses in this directory. They
are deliberately weak, protocol-informed candidate priors for black-box NAS
inference, not confirmed claims about an AMF implementation.

## Candidate rules

1. For this initial candidate workflow, an AMF downlink that carries an
   ngKSI-related value is the updated-register observation for the inference
   region that ends at that edge: `r_after = downlink_ksi`. Do not infer a
   separate pre-update output interpretation in this workflow.
2. When a state-classified model has a non-self-loop transition whose target is
   a D-class state, include `r' = 7` as a candidate security-context reset for
   that edge. This candidate has higher priority than the no-observation
   default, but remains unconfirmed until a later trace field tests it.
3. When neither the input nor the output of a transition carries an
   ngKSI-related value, include `r' = r` as the default candidate. Absence of
   an observation is not proof that the register did not change, so retain this
   as a candidate and constrain it with later observations. If this rule and
   rule 2 both apply, rule 2's D-state reset candidate `r' = 7` has higher
   priority; do not treat the no-observation default as the preferred
   explanation for that transition.
4. Apply the repeated-cycle alignment and inference-region rules to every
   selected cycle-cover route, not only to C01. Anchor the fitted repetitions
   with the final observable downlink KSI of repetition 1.
5. Represent an inference region as `(r_before, ordered_observation_items,
   r_after)`.  Every item must retain its type, field path, logical input,
   occurrence index and trace position.  Do not replace a missing, duplicated,
   reordered or identity-mismatched slot by shifting another observation.
6. Configure transport-context signals through `mapping.signal_definitions`.
   Signal selection is data-driven: `match.input_symbols` accepts an arbitrary
   list or the sole wildcard `"*"`; inference code must not hard-code message
   names.  Within one event, emit signals in declaration order before numeric
   inputs.  The same field on different logical messages remains a different
   slot.
7. Build all configured, observed signal slots as outer `signal_guard` nodes,
   including constant-valued signals.  Use explicit `unknown` leaves for
   unobserved or insufficiently supported branches.  A tree with an unknown
   leaf is partial, not observationally exact.
8. Keep node semantics separate: `signal_guard` uses `s == 0/1`;
   `threshold_guard` models an observed wrap as `x < T` with a fixed constant
   zero else leaf; `derived_value_guard` is an input-slot equality introduced
   only after leaves and threshold trees fail.  A derived value has no
   protocol meaning by itself.
9. Manage depth independently: configured signal depth is outside the numeric
   depth budget; `max_numeric_depth` and `max_derived_signal_depth` are separate
   limits.  An automatically derived equality split must have non-empty sides,
   minimum consecutive support on both sides, and may enumerate only numeric
   input slots.
10. Retain every exact tie among `r'=c`, `r'=r+k`, and `r'=ij+k`, and every
    exact tied tree.  Index candidates by their ordered guard paths, complete
    update tree and candidate status, with the corresponding concrete DOT edge
    set.

## Deferred rules

- Do not currently add `r' = i` or `r' = r` merely because a UE uplink carries
  an ngKSI-related value. The candidate generator may still discover an exact
  data-driven leaf such as `r' = i + k`.
- Do not assign `ngKSI = 7` any NAS semantic meaning in this workflow. Treat
  every observed integer, including `7`, as a raw value; never use it to fill a
  missing field.
- Do not emit explicit modulo formulas. Express observed wrap or reset behavior
  through a tree guard `r < K` or `i < K`, where `K` is observed in the trace.
  For every such tree, the non-guard (`else`) leaf is fixed to the constant
  `r' = 0`; do not enumerate another fitted leaf for that branch.

## Evidence and reporting boundaries

- Keep UE-internal observations (for example `ue_sec_ctx_ngksi`) distinct from
  AMF-visible NAS fields such as `registration_ksi_value`,
  `auth_request_ksi_value`, and `smc_ksi_value`.
- Report candidate equations, counterexamples, alignment anomalies, and the
  ambiguity between output and internal update separately. A fitted equation
  describes observed behaviour; it does not establish an AMF source-level
  register name or implementation path.
