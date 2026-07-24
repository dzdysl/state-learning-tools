# Binary backtrace semantics

For two child classes produced by the same parent in round `k`, compare their complete signatures.

- `strict`: exactly one differing signature position.
- `convergent_unique`: two or more positions differ, but after converting every difference to an unordered previous-round target-class pair, only one pair remains. Keep every input label on one causal edge. This is a valid entry under the default policy.
- `branching`: differences point to multiple previous-round class pairs. It is not a single-cause entry, but if another path reaches it, expand each predecessor branch.

Entry eligibility and traversal role are independent concepts in the data, but the user-facing role must be unique. A pair can be an independent entry, already covered by a later entry, or an unvisited non-entry. Do not print the same pair in both “参与” and “排除” rows.

At round 1, predecessor class pairs are initial-category differences. At later rounds, first try to locate a sibling-pair node in round `k-1`; if the two labels did not share a parent there, record a partition-boundary terminal instead of inventing a split node.
