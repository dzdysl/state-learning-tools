# Workspace contract

## Repository ownership

| Project | Repository directory | Main learner directory | Sequence runner directory |
| --- | --- | --- | --- |
| Open5GS | `open5gs-state-learning` | `Corelearner_open5gs` | `Corelearner_seqTest_pack` |
| free5GC | `free5gc-state-learning` | `Corelearner_free5gc` | `Corelearner_seqTest_pack` |
| OAI | `oai-state-learning` | `Corelearner_OAI` | `Corelearner_seqTest_pack` |

The repositories remain independent. Cross-project reuse is a shared contract and coordinated patch, not a shared source-code repository.

## Terminology

- Component: `Corelearner_sequence_runner`
- Mode: `multiSeq`
- Java runner: `MultiSequenceRunner`
- Input boundary: `SequenceInputAdapter`
- MDF adapter: `MdfInputAdapter`
- Published sequence-runner JAR: `Corelearner_SeqTest.jar`
- Required `run_mode`: `interactive` or `multi_sequence` (case-insensitive)
- Removed `sequence_testing*` configuration keys are rejected with a migration error.

## MDF completion rule

MDF support has two halves:

1. Learner: convert a logical input such as `serviceRequest_mdf` into a platform wire symbol after validating its parameter.
2. Platform SUL/UERANSIM: recognize and execute that wire symbol.

Mark a platform supported only when both halves and a smoke test exist. free5GC's existing implementation is evidence for behavior, not a file template that can overwrite the other platforms.

## Artifact policy

- Source and configuration belong in Git.
- `target`, duplicate JAR/ZIP files, logs, traces, databases, and generated diagrams are disposable or experiment artifacts.
- Publish the verified runnable fat JAR and deterministic `scripts.zip` at each component root.
- When repository-root UERANSIM `src/` changes, refresh the repository-root Git-LFS `src.zip` in
  the same commit.
- Default build runtime: JDK 17. Maven source/target compatibility: Java 11.
