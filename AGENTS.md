# State-Learning Tools Repository

This repository is the single versioned source for cross-platform state-learning analysis, rendering, archival, and workspace-operation tools. Keep platform-specific source changes in their respective repositories and experimental evidence in `state-learning-experiments`.

## Terminology discipline

- Reader-facing reports, workflow documents, schemas and status values must use terminology already established by the user
  and by the nearest applicable `AGENTS.md`. Do not invent a synonym, classification or workflow name unless the user asks
  for one, or the existing term is genuinely ambiguous and the ambiguity is explained first.
- Compatibility fields, internal enums and temporary implementation names are not automatically reader-facing terms. When a
  domain has a dedicated `AGENTS.md`, that file is the single detailed terminology and algorithm contract; repository-level
  guidance should point to it instead of copying a second version.

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
  component-root `scripts.zip` deployment artifacts. If repository-root UERANSIM `src/` changed,
  the same tool must refresh and stage repository-root `src.zip`. After the commit, report whether
  `scripts/` and `src/` changed, both archive outcomes, and whether Linux deployment requires
  `chmod +x scripts/*.sh`.
