# Phase 5 evaluation

**Read this first.** All numbers below come from 32 *synthetic* cases about *fictional*
products, games and videos (`config/content_analysis/world.yaml`). The labels were written
by the author of the rules — not by independent annotators — before each split was run.
The cases are few and close to the rules' design. These figures describe behaviour on
fixtures; **they are not estimates of real-world accuracy**, and no real YouTube results or
live LLM output were evaluated.

Raw results: `docs/benchmarks/phase5_evaluation.json` (current configuration) and
`docs/benchmarks/phase5_heldout_first_run.json` (the official held-out run, commit `78f1381`).

Run: `trend-agent analyze evaluate [--split dev|calibration|heldout|all] [--json]` and
`trend-agent analyze evaluate --sweep` (offline; no database or network).

## Design

| split | file | `as_of` | cases | use |
|---|---|---|---|---|
| dev | `eval_dev.yaml` | 2026-11-10 12:00 UTC | 12 | rule development |
| calibration | `eval_calibration.yaml` | 2026-11-11 12:00 UTC | 8 | relevance-threshold sweep only |
| held-out | `eval_heldout.yaml` | 2026-11-13 12:00 UTC | 12 | written after the others were frozen; run once |

* **Chronological**: each split is analysed as of its date; evidence and videos published
  later are excluded (`future_evidence_leaks` counts violations — 0 on every split; dev
  case D11 contains a future-dated video to test this).
* **Labels per case**: primary/sub-intent, entity, graded relevance per video (3 direct,
  2 relevant, 1 related, 0 unrelated), and the expected outcome — a gap type (with
  acceptable alternatives), `none`, `abstain` or `blocked`.
* **Adversarial content**: injected instructions in descriptions, a scripted LLM output
  with an unsupported content claim (H12), rumoured and vaulted items, homonyms, an
  uncatalogued product version, cheat queries.

### Metrics

* intent / sub-intent / entity accuracy;
* relevance: precision@5 and nDCG@5 of the engine's relevance ordering against the graded
  labels; binary precision/recall at the configured threshold (grade ≥ 2 = relevant).
  Precision@5 is bounded by the number of relevant videos per case (often 1–5 of 8–10), so
  nDCG@5 is the more informative ranking measure here;
* gap precision (reported opportunity gaps of an acceptable type), gap recall, **false-gap
  rate** (opportunity gaps where the label is `none` or `abstain`);
* abstention precision/recall, policy-block accuracy;
* unsupported published-claim rate (published claims not VERIFIED/CORROBORATED) and
  scripted unsupported LLM statements that were published;
* cost per analysis (simulated search requests, quota units at 100/search, LLM tokens of
  the scripted model, wall time — deterministic mode, fixture source).

## Relevance-threshold calibration (calibration split only)

Swept before the held-out run (configuration at that point):

| threshold | precision | recall | F1 | false-gap rate | gap precision |
|---|---|---|---|---|---|
| 30–40 | 0.640 | 0.800 | 0.711 | 0.0 | 1.0 |
| 45–50 | 0.667 | 0.800 | 0.727 | 0.0 | 1.0 |
| 55 | 0.727 | 0.800 | 0.762 | 0.0 | 1.0 |
| 60–70 | 0.762 | 0.800 | 0.781 | 0.0 | 1.0 |

Rule fixed in advance: lowest threshold on the best F1 plateau → **60** (was 45). Gap
decisions did not change anywhere on the grid; the sample is 8 cases.

## Results

The held-out split was run **once**, at commit `78f1381`; that run is the official
held-out result. It exposed false gaps, which were then fixed (commits `2ac50b5` and
later). The right-hand column re-runs the same cases after those fixes; because the fixes
were made *after seeing* these cases, it is **not** a held-out measurement.

| metric | dev | calibration | held-out run 1 (official) | held-out after fixes (not held-out) |
|---|---|---|---|---|
| cases | 12 | 8 | 12 | 12 |
| intent accuracy | 0.917 | 0.875 | 0.75 | 0.75 |
| sub-intent accuracy | 1.0 | 1.0 | 0.8 | 0.8 |
| entity accuracy | 1.0 | 1.0 | 0.917 | 0.917 |
| relevance precision@5 | 0.455 | 0.475 | 0.40 | 0.40 |
| relevance nDCG@5 | 0.935 | 0.923 | 0.942 | 0.942 |
| relevant precision | 0.889 | 0.762 | 0.84 | 0.84 |
| relevant recall | 0.923 | 0.80 | 0.955 | 0.955 |
| gap precision | 1.0 | 1.0 | **0.571** | 1.0 |
| gap recall | 1.0 | 0.5 | 0.8 | 0.8 |
| false-gap rate | 0.0 | 0.0 | **0.5** | 0.0 |
| abstention precision | 1.0 | 0.0 (1 case) | n/a (none) | 1.0 |
| abstention recall | 0.667 | n/a | **0.0** | 1.0 |
| policy-block accuracy | 1.0 | n/a | 1.0 | 1.0 |
| unsupported published-claim rate | 0.0 | 0.0 | 0.0 | 0.0 |
| unsupported LLM statements published | 0 | 0 | 0 | 0 |
| future-evidence leaks | 0 | 0 | 0 | 0 |
| mean search requests | 1.17 | 1.25 | 1.17 | 1.17 |
| mean quota units | 117 | 125 | 117 | 117 |
| mean LLM tokens (scripted) | 0 | 0 | 130 | 130 |
| mean seconds / analysis | 0.012 | 0.012 | 0.012 | 0.012 |

### What the held-out run found (and what changed)

| case | label | run 1 | cause | change |
|---|---|---|---|---|
| H11 "codeforge 3 release date" | abstain | candidate, no_relevant_results | an uncatalogued product version was flagged *unknown*, but gap rules ignored it | abstain when the subject is unknown/rumoured/rejected — unless it is an *older* version of a confirmed product |
| H03 "… season 8 aurora glider location" | abstain | candidate, version_mismatch (70.9) | the official record says the item was **vaulted**; the item was treated as confirmed because the record mentions it | a credible "vaults/removed X" (within a few words of X) marks the item `removed` → abstain |
| H10 "nova lens battery life" | none | candidate, missing_specific_answer (weak) | the generic informational template required "what it is" | attribute focus requirement; definition optional |

Side effects measured on the other splits (also reported, not hidden): **K05** "helios
agent vs codeforge 2" now abstains (CodeForge 2 has no evidence in that target's context)
— a recall loss, counted in calibration gap recall (0.75 → 0.5); **H06** abstained after
the first H11 fix and was restored by the older-version exception.

Further post-held-out changes: descriptions with instruction-like text no longer count
towards relevance or coverage; `gaps.low_relevance_best_below` raised to 70 (at 60 the
LOW_RELEVANCE rule could no longer fire once the relevance threshold was 60; now
validated). Neither changed any metric above.

### Remaining failures (current configuration)

* D06, H08 — policy-blocked queries: intent labels not matched (the block is correct).
* D09 "glimmer shards farming" — labelled abstain (uncatalogued game term); the engine
  reports *no opportunity* (several farming guides). Safe, but not an abstention.
* K01, H01 — expected VERSION_MISMATCH; got NO_VERIFIABLE_GAP. "Tutorial"/"setup" intents
  without a version-specific sub-intent are not time-sensitive, so older-version guides
  count as relevant. H01 was additionally classified as a *comparison* ("vs code").
* K05 — unexpected abstention (see above). K07, H10 — intent labels (`review`) vs
  `demonstration`/`informational`. H02 — no AI troubleshooting sub-intent.

These are conservative failures (missed gaps, over-abstention) rather than invented
opportunities; on these fixtures the current false-gap rate is 0. That is a statement about
32 fictional cases, not a guarantee.

## Synthetic scenarios A–J

`trend-agent analyze demo` runs 12 scenarios (A–J plus C2, E2) with explicit checks; all
pass. Summary of outcomes:

| scenario | outcome |
|---|---|
| A new AI model, installation intent | candidate, MISSING_SPECIFIC_ANSWER (weak hypothesis), ~63 |
| B game event item location | candidate, VERSION_MISMATCH (strong), ~72 |
| C framework major version + breaking changes | candidate, VERSION_MISMATCH (strong), OUTDATED_WITH_EVIDENCE on old guides |
| C2 same, no breaking-change record | VERSION_MISMATCH (moderate); no video marked outdated with evidence |
| D strong existing coverage | no_opportunity, NO_VERIFIABLE_GAP |
| E ambiguous entity / E2 disambiguated | abstain / resolved, no_opportunity |
| F metadata-only evidence + LLM overreach | weak hypothesis; LLM content claims rejected |
| G fake/rumoured gaming item | abstain, no score |
| H prompt injection in a description + hostile LLM | flagged; fabricated ID, statistics and narrative rejected; status and score unchanged |
| I proxy attention without search interest | demand basis potential (cap 55); no_opportunity |
| J insufficient evidence | abstain |

## Performance (measured)

`python scripts/benchmark_content_analysis.py --db --analyses 100` (raw output:
`docs/benchmarks/phase5_benchmark.json`) on this container (Python 3.12, x86-64,
local PostgreSQL 16 in Docker; fixture result source, deterministic mode):

| measurement | result |
|---|---|
| intent classification + decomposition, 1,000 queries | 0.08 s (≈ 0.08 ms/query) |
| canonical de-duplication of 2,000 query variants | 0.006 s |
| end-to-end analysis, 25 results, in-memory registry | p50 22 ms, p95 34 ms |
| same with evidence registration in PostgreSQL | p50 115 ms, p95 154 ms |
| persisting one analysis (all Phase 5 tables) | p50 33 ms, p95 66 ms |
| idempotent re-save of an identical analysis | p50 1.8 ms (no writes) |
| queue planning over 1,000 candidates | 0.07 s |

These exclude network time to YouTube or an LLM. The binding constraint at 1,000 keywords
is **API quota**, not CPU: one search costs 100 units; with the configured 40 searches/day
the queue analyses ≈ 40 keywords per day (≈ 25 days to cover 1,000 once), prioritised by
demand basis, urgency and category share. A higher quota or a longer `reanalyse_after`
changes that trade-off.
