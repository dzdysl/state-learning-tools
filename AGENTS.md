# State-Learning Tools Repository

This repository is the single versioned source for cross-platform state-learning analysis, rendering, archival, and workspace-operation tools. Keep platform-specific source changes in their respective repositories and experimental evidence in `state-learning-experiments`.

## Generated file lifecycle

- When generating files from a user command, retain only requested final deliverables plus required source evidence, provenance, and versioned tool inputs.
- Unless explicitly requested otherwise, verify the final output and promptly remove task-created disposable intermediates, staging copies, temporary conversion results, and temporary build output.
- Never delete user-existing files, source evidence, requested deliverables, tracked files, or anything outside the task scope. If whether to retain a generated file is unclear, ask before deleting it.

## Build uncertainty

- In ordinary mode, inspect discoverable build/generation targets, environment, artifact paths, and verification scope first. If an unresolved point could change a result, overwrite/damage an existing artifact, or affect reproducibility, ask the user before proceeding; do not wait for Plan mode.

## Git safety

- Do not use `git add .`; review the selected source files and generated artifacts first.
- Do not commit, tag, or push unless the user explicitly requests it.
- When the user explicitly requests a commit, immediately create an annotated, immutable tag after the commit succeeds. Use a purpose-and-date tag name. Never overwrite, delete, or move an existing tag—stop and report a name collision.
- Before committing any platform source repository, run
  `operations/workspace/New-StateLearningScriptArchives.ps1` for that repository and stage both
  component-root `scripts.zip` deployment artifacts. After the commit, report whether `scripts/`
  changed and whether Linux deployment requires `chmod +x scripts/*.sh`.
