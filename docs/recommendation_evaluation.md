# Recommendation evaluation

`trend-agent recommend evaluate [--split calibration|heldout|all] [--human-labels FILE]
[--out FILE]` — offline, no database, no LLM. Results of the last run:
`docs/benchmarks/phase7_evaluation.json`.

> **What these numbers are not.** The fixtures are synthetic (fictional products and
> games), the labels were written by the author of the rules (not by independent
> annotators), and the cases are few. The results show how the rules behave on these
> cases. They are **not** evidence of real-world recommendation accuracy or usefulness.
> No LLM grades any output.

## Datasets

`config/recommendations_eval/eval_calibration.yaml` and `eval_heldout.yaml` (8 labelled
drafts each, across the AI, technology and gaming scenarios of Phase 5, plus 12 and 10
labelled duplicate-title pairs).

Each draft is a complete concept (titles, hooks, labelled outline statements, optional
premise and rationale) with gold labels:

* each statement: `supported` (stated by a cited record), `unsupported` (fabricated or
  unverifiable), `result_as_fact` (an untested result written as done), `unsafe`
  (instruction, shell command, credential), or `plan`;
* each title and hook: `ok` or `reject`;
* the statuses a careful reviewer would accept for the whole concept.

Failure types covered: invented percentages, an unconfirmed version (CodeForge 3), a
fabricated unlock condition, fabricated event dates, a fake evidence citation, an untested
benchmark result written as a fact, clickbait and superlative titles, result and urgency
hooks, forecast and view claims in the premise and rationale, prompt injection and
destructive shell commands, and a weak-evidence gaming opportunity.

**Protocol.** The calibration split was used to inspect behaviour and to sweep the
duplicate-title threshold. The held-out split was written separately, with different
texts and failure types, and evaluated with the rules and threshold frozen. Four engine
fixes came out of writing the unit tests (facts surviving when no title passes, a fake
citation still approved, unsafe planned demonstrations, one-word hooks) before either split
was run; no rule was changed after looking at held-out results.

## Metrics

| Metric | Definition |
|---|---|
| status accuracy | share of drafts whose final status is one of the acceptable statuses |
| false approvals | drafts that ended `APPROVED` although the label does not accept it |
| unsupported published as fact | `unsupported` / `result_as_fact` statements still labelled `VERIFIED_FACT`, or `unsafe` statements present at all (target 0) |
| bad-statement removal | precision / recall of "no longer presented as a fact" against the gold labels |
| supported-fact retention | share of `supported` statements kept as `VERIFIED_FACT` |
| title / hook rejection | precision, recall, F1 of rejection against the gold labels |
| duplicate merge | precision, recall, F1 of "title word overlap ≥ threshold" against labelled pairs |

On **generated** recommendations (deterministic template generator over scenarios A, B, C,
G, J), measured by checks independent of the verification gate:

| Measure | Definition |
|---|---|
| search-intent alignment | format alignment (1.0 sub-intent, 0.7 primary intent, 0.3 none) and query-word coverage of the title |
| factual grounding / unsupported-claim rate | share of `VERIFIED_FACT` statements whose content words (≥ 80%) appear in the text of the records they cite; an "According to X:" attribution must name a cited record's publisher |
| content-gap relevance | share of ideas whose format family answers the opportunity's gap type |
| originality | mean originality component (share of current exact-entity results not already in this format) |
| production feasibility | share within the deepest profile's bounds (36 h) |
| diversity | mean / max pairwise title similarity in the top 10, distinct format families, categories |
| human usefulness | from `--human-labels` (recommendation ID → true/false) when given; otherwise **not measured**. In production, USEFUL / NOT_RELEVANT feedback is recorded for this purpose (`recommend stats`) |

## Results (synthetic fixtures)

| | Calibration | Held-out |
|---|---|---|
| drafts | 8 | 8 |
| status accuracy | 1.00 | 1.00 |
| false approvals | 0 | 0 |
| unsupported statements published as fact | 0 | 0 |
| bad-statement removal precision / recall | 1.00 / 1.00 (4 bad, 14 good) | 1.00 / 1.00 (4 bad, 12 good) |
| supported-fact retention | 1.00 | 1.00 |
| title rejection F1 | 1.00 (1 positive) | 1.00 (2 positives) |
| hook rejection F1 | 1.00 (1 positive) | 1.00 (1 positive) |
| duplicate merge F1 at 0.6 | 1.00 (sweep optimum 0.45–0.60) | 0.89 (precision 1.00, recall 0.80) |

The held-out duplicate miss is a paraphrase ("Pyxis 4 vs Pyxis 5: What Actually Changed" /
"Pyxis 4 vs Pyxis 5 Compared", word overlap 0.50): lexical similarity cannot see that both
are the same comparison. It was not tuned away.

Generated ideas: 17 (15 eligible, 2 research candidates); format alignment 0.88 and 100%
serving the intent; title query coverage 0.56 (titles for, e.g., an "explained" format
legitimately reuse fewer query words); **24 of 24 verified facts grounded (unsupported-claim
rate 0.0)**; gap relevance 0.67; originality 0.73; all within feasibility bounds; top-9 list
spans 8 format families and all three categories with a maximum pairwise title similarity
of 0.50. Human usefulness: not measured.

## Limitations

* Small, author-labelled, synthetic sets; perfect scores here mean the rules handle these
  failure types, not that they generalise. Real creator judgements and independent
  annotation are needed before any claim of usefulness.
* The grounding check is lexical: it can miss a paraphrased unsupported claim that reuses
  the record's words, and it cannot judge relevance or tone.
* The LLM generator is evaluated only through scripted outputs (no live model).
