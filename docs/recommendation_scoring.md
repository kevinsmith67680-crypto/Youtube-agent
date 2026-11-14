# Recommendation scoring

The score orders ideas for research and production planning. It is a **0–100 ranking index
from versioned, experimental, unoptimised weights**, not the probability that a video will
succeed or go viral, and it is never presented as one.

## Composite

```
composite = (Σ weight_i × value_i over AVAILABLE components) / (Σ weight_i over AVAILABLE)
            × 100 × evidence_quality_factor
```

`ranking.version: ranking-1.0`. Every component keeps its raw inputs and a note; the API and
dashboard show them all (`/recommendations/{id}` → "Why this rank").

| Component | Weight | Value (0–1) | Missing when |
|---|---|---|---|
| demand_evidence | 0.25 | Phase 4 demand basis: confirmed direct YouTube Search surge 1.0, direct-search candidate 0.75, external search surge 0.55, proxy-only 0.35, cold start 0.30, watch 0.20; ×0.7 when stale/expired | no Phase 4 demand evidence |
| content_gap | 0.20 | Phase 5 gap type (no relevant results 1.0 … low relevance 0.6, no verifiable gap 0.1) × evidence strength (strong 1.0, moderate 0.75, weak 0.45) | no assessable gap |
| intent_alignment | 0.15 | 0.6 × format alignment (serves the sub-intent 1.0, the primary intent 0.7, otherwise 0.3) + 0.4 × share of the query's words in the title | never |
| forecast_opportunity | 0.15 | mean of: the **calibrated** breakout probability, the continuation probability, and the opportunity-window value for the idea's profile (URGENT 0.9, TIMELY 0.8, LONGER_WINDOW 0.7, LIKELY_TOO_LATE 0.1); × 0.8 for proxy-attention forecasts | no usable forecast, or none of the three parts is available |
| creator_relevance | 0.10 | category in the profile 1.0 else 0.3; blended with preferred formats (30%) and topic preferences (20%) when stated | never |
| production_feasibility | 0.10 | 1 − (expected hours − 3) / 30, clipped to [0.15, 1]; ×0.5 if it exceeds the stated weekly hours; ×0.4 if the window is likely too late | never |
| concept_originality | 0.05 | 1 − 0.8 × share of current, exact-entity inspected videos already in this format family | no inspected results |

## Missing data

* `ranking.missing_policy` is `exclude` for every component: a missing component is left
  out of both numerator and denominator and listed in the explanation and limitations
  ("forecast opportunity unavailable (excluded, not zero)"). It is **never** scored as zero
  (which would mean "no growth") or as one.
* A `neutral` policy (0.5) exists for experiments and is labelled when used.
* If less than `min_available_weight` (60%) of the weight is available, the idea is not
  ranked (shown as a research candidate with the reason).
* `require_demand_evidence: true` — no demand evidence, no rank.

## Evidence quality as a gate, not a bonus

`evidence_quality_factor = status factor × gap-evidence factor`:

* status: APPROVED 1.0, APPROVED_WITH_UNCERTAINTY 0.85; any other status → not ranked;
* Phase 5 evidence quality: strong 1.0, moderate 0.92, weak 0.80.

An attractive concept cannot compensate for weak or unverified evidence: the factor scales
the whole score down and research candidates are never ranked.

## Tie-breaks and ordering

Sort by composite (descending), then urgency (publish soon → likely too late), then ID.
Deduplication and diversity run after scoring (`recommendation_engine.md`); suppressed ideas
keep their score but get no rank.

## Limitations

* The weights are engineering choices, not fitted to outcomes. No outcome data (views,
  watch time) is used or inferred; feedback is recorded for later evaluation but changes
  nothing automatically in this phase.
* Phase 4 demand bases and Phase 5 gap types are mapped to values by hand.
* Title coverage is lexical (with a small synonym table); a good title can score lower if
  it paraphrases the query.
* Originality only sees the inspected search results, not the whole platform.
* Scores of real and synthetic records are never ranked together.
