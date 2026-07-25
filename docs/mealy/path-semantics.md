# Path semantics

An access sequence starts at a chosen initial state and reaches a target state. A distinguishing suffix starts from two already-selected states and makes their outputs differ. This skill computes access sequences only; use `$explain-mealy-state-distinction` for distinguishing suffixes.

Breadth-first search minimizes the number of input symbols. When several shortest paths exist, traverse transitions in their first global appearance order in the DOT file and return the first path. Preserve all output actions in the reported trace even though outputs do not affect reachability.

For every reachable result, keep the normal `input_sequence` array and complete transition/output trace, and also emit `input_sequence_text`: the same input symbols joined by single ASCII spaces. This is the directly executable signaling sequence; the zero-length path is displayed as `(空序列)`.
