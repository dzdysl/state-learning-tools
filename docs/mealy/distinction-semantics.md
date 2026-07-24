# Distinction semantics

A refinement signature contains previous-round target classes and intentionally omits edge outputs. Therefore a signature backtrace explains why a partition was refined, while a shortest distinguishing suffix explains the earliest observable output difference. These paths can differ.

For two Mealy states, search the product automaton. At each pair, test every input in the global input order. If the outputs differ, the current prefix plus that input is a distinguishing suffix. Otherwise enqueue the successor-state pair. Exhaustion without an output difference proves behavioral equivalence only within the supplied deterministic model.

For two classes, every cross-pair must be behaviorally equivalent before declaring the classes mergeable. One counterexample is sufficient to reject merging, but the report should retain all checked pairs when the user requests a full audit.

SQLite membership-query tables in this workspace contain space-separated `command` and `result` columns. Compare output tails after each state's access prefix; do not compare prefix outputs belonging to different access sequences.
