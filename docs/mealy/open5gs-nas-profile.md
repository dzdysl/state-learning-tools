# Open5GS NAS initial profile

The built-in profile classifies target states with the original experiment rules:

- `X`: every outgoing transition outputs `null_action`.
- `D`: `s0`, a non-X target of `deregistrationRequest`, a transition whose output contains `Reject`/`reject`, or a state-changing `securityModeReject` target.
- `N`: target of an `authenticationRequest` output.
- `NG`: target of `registrationRequestGUTI / identityRequest`.
- `A`: target of `authenticationResponse / securityModeCommand`.
- `S`: target of a `registrationAccept` output.
- `R`: target of `serviceAccept` or `configurationUpdateCommand`.

`X` takes precedence. All remaining evidence must place every state in exactly one class; conflicts and unclassified states are errors.

For refinement, choose one global input order from the first declared complete state and require every state to expose the same input set. For a state `s`, replace each transition target with its previous-round class label. The resulting tuple is the signature. Outputs are retained for explanation but are not signature dimensions.
