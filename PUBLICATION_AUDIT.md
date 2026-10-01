# Publication audit

This document records the checks made before the portfolio version was published.

## Claims verified from saved project artefacts

- The semantic catalog contains **56 governed metric definitions** in `config/metrics.yaml`.
- The development benchmark contains **137 questions**. Its saved offline result is **100% overall pass rate**. This is a development-set result and is **not** presented as an estimate of performance on unseen questions.
- The held-out benchmark contains **30 questions** written after development. Its saved one-pass offline result is **83.3% overall**, with **90% metric-result accuracy** and **1 of 4 planted root-cause cases recovered**.
- The saved development evaluation reports **99.1%** grounded narratives; the held-out evaluation reports **96.0%**.
- The benchmark documentation states that metric gold answers use independently hand-written SQL rather than the Copilot's semantic compiler.
- The project explicitly labels its company and business events as synthetic and distinguishes contribution analysis from causal proof.

## Publication framing

The **83.3% held-out result is the primary generalisation result**. The 100% development result is retained for transparency but should not be used as the headline accuracy claim.

The high development score reflects a system that was iterated against that benchmark. The held-out misses are also retained in `eval/results/holdout_results.md`; in particular, unfamiliar causal phrasing reduces root-cause recovery to 1/4 on the held-out set.

No LLM benchmark result is claimed. The saved results are from `semantic_offline` mode. LLM modes require a configured provider and must be measured before their performance is reported.

## Reproducibility and safety

- Synthetic generation is deterministic for a fixed seed and scale.
- Generated DuckDB files are intentionally git-ignored because the full warehouse is reproducible and can exceed GitHub's file-size limit.
- SQL execution is read-only and guarded by parsing, single-statement enforcement, forbidden-operation/function checks, an allow-list, schema validation and row limits.
- The repository includes a CI workflow that installs the project, runs the automated tests, and runs lightweight publication-integrity checks.

## Environment limitation during this audit

The review environment had no package-network access and did not already contain `duckdb` or `sqlglot`, so the dependency-based pytest suite and full warehouse regeneration could not be rerun here. Saved benchmark artefacts, source code, configuration, benchmark construction, documentation and static publication checks were inspected. The included GitHub Actions workflow is intended to rerun the dependency-based suite in a clean supported environment after publication.
