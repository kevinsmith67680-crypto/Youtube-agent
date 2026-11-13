# Opportunity scoring

The opportunity score is a **0–100 ranking index for prioritising research**. It is not a
probability of success, views or revenue, and it is not comparable across score versions.

## Components (`score_version: opportunity-score-1.0`)

| | component | weight | value (0–1) from |
|---|---|---|---|
| A | `demand_evidence` | 0.25 | the Phase 4 demand basis (confirmed direct 1.0, direct candidate 0.75, external 0.5, potential/proxy 0.35, cold start 0.3, watch 0.2, insufficient 0.1), scaled by `0.5 + 0.5 × severity/100` (severity is Phase 4's ranking index); manual analyses have **no value** |
| B | `intent_clarity` | 0.10 | confidence label, specificity, ambiguity |
| C | `content_gap_strength` | 0.25 | the primary gap's type and strength (no verifiable gap = 0) |
| D | `supply_relevance` | 0.15 | how few adequate results exist among the relevant ones |
| E | `freshness_opportunity` | 0.10 | share of relevant results that are potentially or verifiably outdated |
| F | `evidence_quality` | 0.15 | 0.35 × inspected volume (vs. 16) + 0.25 × best tier (metadata 0.5, captions 0.8, full 1.0) + 0.25 × entity state + 0.15 × share of verified claims |

**Missing is not zero**: a component without data has `value: null` and is excluded; the
remaining weights are renormalised. If less than 60 % of the weight is available, or the
analysis abstained, the score is `null` (unavailable).

## Caps by demand basis

Content findings can never manufacture demand. The score is capped by what Phase 4
established:

| demand basis | cap |
|---|---|
| CONFIRMED_DIRECT_YOUTUBE_SEARCH_SURGE | 100 |
| DIRECT_SEARCH_CANDIDATE | 85 |
| EXTERNAL_SEARCH_SURGE | 70 |
| POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY (incl. proxy attention) | 55 |
| COLD_START_GROWTH_CANDIDATE | 50 |
| MANUAL_NO_DEMAND_EVIDENCE | 50 |
| WATCH | 45 |
| INSUFFICIENT_EVIDENCE | 35 |

## Status

* `candidate` — an opportunity gap (any type other than no-verifiable-gap / insufficient)
  with a score;
* `no_opportunity` — adequate coverage or no verifiable gap;
* `insufficient_evidence` — abstained (score `null`);
* `blocked_policy` — cheats, exploits, account compromise or anti-cheat evasion.

## Report

`OpportunityReport` (`schema_version: content-opportunity/1.0`) carries: opportunity and
run IDs, query/category/topic/entity, intent, the full Phase 4 demand evidence, region,
language, inspected count, coverage limitations, requirements and their coverage,
relevant videos (URL, channel, date, relevance, match, format, freshness, tier, evidence
ID), all gaps and the primary gap, supply, score with components, evidence quality,
status, verification state, main uncertainty, verified claims, source references, an
explanation (`audience_wants`, `existing_content`, `possibly_missing`, `why_investigate`,
`evidence_needed`), an optional one-sentence `suggested_content_angle` (the detected need —
not a title, script or production plan), versions, the LLM model if used, and the
synthetic flag.

## Interpreting scores

The weights are a documented operating point, not fitted to outcomes: there is no ground
truth for "this video idea succeeded" in this project. Use the score to order manual
review; read the primary gap, its strength and verification status, and the main
uncertainty before acting on any report.
