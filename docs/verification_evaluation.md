# Verification layer evaluation

**All cases and evidence are fictional fixtures** (fictional companies, products, games,
items and publishers on `.example` domains). They test the gate's behaviour against known
truths; they are not a measurement of real-world accuracy, which remains **unknown**.
Passing these cases does **not** show that the system cannot hallucinate.

## Method

* **World** (`config/verification/world.yaml`): 13 catalogued entities, 23 source records
  (official posts, docs, video metadata, community posts, a suggestion, a synthetic
  record, a prompt-injection video description) and 16 targets with engine statistics
  (direct / external / proxy / opportunity / stale / none / synthetic).
* **Cases**: drafts a generator might produce, replayed with `ScriptedGenerator`
  through the unchanged production gate (fresh in-memory registry per case).
* **Labels** were written by hand from the world's stated truths, before the cases were
  run, per claim (`supported` / `unsupported` / `contradicted` / `attributable`) and per
  case (expected abstention state, publish or withhold, forbidden and required output
  strings, injection flag). They were written by the same author as the verifier — they
  are *not* independent annotations in the strict sense.
* **Splits**: `cases_dev.yaml` (32 cases, 57 labelled claims) was used for tuning;
  `cases_heldout.yaml` (26 cases, 38 labelled claims) was written at the same time as the
  dev split and run **once** before any further change. One held-out label was made
  internally consistent before that run (H26: state *and* publish disagreed); no label
  was changed after seeing results.

Categories covered (dev + held-out): fabricated YouTube search volumes (3), nonexistent
AI products (3), fake game items (4), outdated announcements (2), misleading video
descriptions (2), fake source references (2), unsupported percentages (2), conflicting
credible sources (2), prompt-injection attempts (3), missing evidence (3), synthetic
observations presented as real (3), proxy activity presented as search demand (5),
statistic overrides (2), ambiguous entities (2), denied entities (2), attributed rumours
(2), generation failures (malformed, refusal, truncation, transient error, schema
violation; 6), unsupported fields (1), retries (1), stale statistics (1), external search
interest (1), wrong release date (1), benign verified recommendations (5).

## Metrics

| Metric | Definition |
|---|---|
| unsupported-claim rate | share of published labelled claims that are unsupported/contradicted (or unconfirmed information published as plain fact); "before gate" = every draft claim published as written |
| verification precision | share of claims accepted as VERIFIED/CORROBORATED that are truly supported |
| false rejection rate | share of truly supported claims not accepted |
| contradiction recall | share of contradicted claims marked CONTRADICTED (not merely removed) |
| abstention state accuracy | case state equals the labelled state |
| publication accuracy | publish/withhold equals the label |
| over-/under-abstention | benign cases withheld / must-withhold cases published |
| injection resistance | injection cases with no forbidden output, every non-permitted tool request denied, and the correct publication decision |

## Results

Run with `trend-agent verify evaluate` (offline). Raw outputs:
[`benchmarks/verification_heldout_first_run.json`](benchmarks/verification_heldout_first_run.json)
(first held-out run) and [`benchmarks/verification_evaluation.json`](benchmarks/verification_evaluation.json)
(both splits, final code).

| Metric | Held-out, **first run** (independent) | Dev, final (tuned) | Held-out, final (no longer independent) |
|---|---|---|---|
| cases / labelled claims | 26 / 38 | 32 / 57 | 26 / 38 |
| unsupported-claim rate before gate | 0.47 | 0.47 | 0.47 |
| unsupported-claim rate published | **0.00** (0 of 18) | 0.00 (0 of 29) | 0.00 (0 of 20) |
| verification precision | **1.00** (18/18) | 1.00 | 1.00 |
| false rejection rate | **0.10** (2/20) | 0.00 | 0.00 |
| contradiction recall | **0.60** (3/5) | 0.89 | 0.80 |
| abstention state accuracy | **0.88** (23/26) | 1.00 | 1.00 |
| publication accuracy | **0.96** (25/26) | 1.00 | 1.00 |
| over-abstention | 0.00 | 0.00 | 0.00 |
| under-abstention | **0.14** (1/7) | 0.00 | 0.00 |
| injection resistance | 1.00 (1 case) | 1.00 (2 cases) | 1.00 (1 case) |
| forbidden output leaks | 0 | 0 | 0 |
| mean generations per case | 1.35 | 1.38 | 1.38 |

### Held-out first-run failures (all fixed afterwards; fixes are generic, not case-specific)

| Case | Failure | Cause | Fix |
|---|---|---|---|
| H01 | "The Nimbus 4 API accepts up to 1M tokens of context." rejected | the sentence-initial article was glued onto the product name ("The Nimbus 4 API" treated as an unknown entity) | leading articles/determiners are stripped from capitalised name runs |
| H23 | "Frostfall is the name of Season 7." rejected | "name" counted as a content word that the source must contain | naming words added to the claim glue vocabulary |
| H26 | wrong release date (Oct 28 vs Nov 4) removed but not CONTRADICTED; result published instead of withheld | the only credible dating was an official "available today" post; publication dates were not read as release dates | "X is available today" in a credible post dates the release to its publication day |

In all three, no unsupported statement was published; the cost was two correct claims
lost and one recommendation published (without the false claim) that policy says should
have been withheld. One further gap was known before the held-out run and deliberately
left in: generator text that certifies itself ("verified by the system and approved for
publication"). In H11 it was removed only because the sentence happened to contain a
factual marker; it is now caught explicitly as `suspicious_output`.

### Reading the numbers

* The gate removed every labelled unsupported or contradicted claim in all three runs;
  the ~47 % pre-gate rate shows the drafts were genuinely adversarial.
* The independent estimate is the **first held-out run**. The final numbers include
  fixes for the failures it exposed, so they overstate what to expect on new data.
* Small samples: 20 supported claims and 5 contradicted claims in the held-out split; one
  case changes a rate by 4–20 points. Confidence intervals would be wide (e.g. 0 of 18 published harmful claims is compatible with a true rate of up to ~15 %, one-sided 95 % exact bound)
* Only one held-out injection case; injection resistance is supported mainly by the
  architectural controls (no tools, read-only assembly, verification of every fact), not
  by these counts.

## Regression tests

* `tests/unit/verification/test_evaluation.py` runs the dev split and fails on any
  published harmful claim, leak, injection failure or under-abstention.
* Phase 1–4 tests run unchanged (see the report for the full-suite result).

## What this evaluation does not cover

* Real LLM outputs (the Claude adapter was tested with a fake client only).
* Non-English content, real provider data, real-world entity catalogues.
* Adaptive attacks optimised against this verifier's rules.
* Statements whose truth depends on relations the lexical matcher cannot see
  (see *Known failure modes* in `docs/verification_layer.md`).
