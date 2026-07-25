# Binary backtrace semantics

For every unordered pair of child classes produced by the same parent in
refinement round `k`, compare the complete signatures position by position.

## Classification and default entry policy

- `strict`: exactly one signature position differs.
- `convergent_unique`: at least two positions differ, but every difference maps
  to the same unordered previous-round target-class pair. Preserve all triggering
  inputs on one merged causal relation.
- `branching`: the differing positions map to more than one previous-round
  target-class pair. Preserve one causal branch for each distinct upstream pair.

The default CLI policy is `strict`: only `strict` pairs may start an independent
backtrace. `convergent_unique` and `branching` pairs are never independent
entries under this policy. They are expanded only when a later valid entry
reaches them. `--entry-policy unique-path` remains an explicit compatibility
option that additionally permits `convergent_unique` entries.

Scan candidate entries from the last effective refinement round down to round 1.
Use `(round, parent, unordered child pair)` as the identity. Once a pair has
already been reached from a later entry, mark it `covered` and do not start it
again.

## Classification versus traversal role

Classification and role are separate fields:

- `classification` explains the signature geometry: `strict`,
  `convergent_unique`, or `branching`.
- `role` explains how the pair participates in this run:
  `independent_entry`, `covered`, or `unvisited_non_entry`.

A multi-position pair can therefore be `branching` and `covered`. Reports and
figures must not collapse these two concepts or list the same pair in mutually
exclusive role rows.

At round 1, predecessor pairs are initial-category differences. At later rounds,
first locate the sibling-pair node in round `k-1`. If no such sibling pair
exists, record a partition-boundary terminal instead of inventing a split node.
The relation graph must decrease strictly by round and reuse common tails.

## Evidence and observable output

Schema version 2 treats every pair node as unordered. A pair stores a
deterministically ordered `children[2]` collection for serialization, but that
order does not define a path identity. Public JSON and Markdown must not expose
`left` or `right` fields.

Each independent entry seeds stable branch `A` from the first canonical child
and branch `B` from the second. For every causal path and input alternative,
follow each starting member state through the original DOT. Branch identity
never changes when a destination belongs to the other serialized child position
at a predecessor node. Shared predecessor nodes therefore have no global A/B
orientation; each entry path carries its own concrete A/B trajectories.

Each visited node records both child classes, original state members, full
signatures, every differing position, full input and abbreviation, every member
transition variant, and a representative transition. Terminal relations record
the immediate outputs for each named state. If the triggering input has equal
immediate outputs, compute the shortest observable suffix from the
successor-state pair and report each state's final differing output.

## Flowchart contract

Use `rankdir=BT`. Draw initial key differences at the top and effective
refinement rounds from round 1 down to the last split round; a convergence-only
round does not receive a fake split cluster. Each actual split parent is one HTML
table containing all child classes and members plus independent, covered, and
non-entry pair rows. A parent with only non-entry pairs and no valid relation is
grey and dashed.

Relations are labelled
`child pair: input abbreviations → previous-round pair`. Merge multiple
relations between the same parent nodes into a multiline edge label without
dropping any pair. Highlight every latest-round strict root and its complete
shared tail. Draw only actually reached initial key differences and include the
complete input-abbreviation legend.
