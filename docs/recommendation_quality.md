# Recommendation quality (Phase 8)

Command: `trend-agent evaluate recommendations`. Code:
`src/trend_agent/evaluation/recommendations.py`, which wraps the Phase 7 evaluation.

## 1. Offline quality (fixtures, deterministic generator)

Ideas are judged by **rules and author labels**, never by the model that wrote them. There
is no LLM self-judgement.

| Measure (held-out split) | Result |
|---|---|
| labelled drafts | 8 |
| status accuracy (APPROVED / APPROVED_WITH_UNCERTAINTY / RESEARCH_REQUIRED / REJECTED) | 1.00 |
| false approvals | **0** |
| unsupported statements published as fact | **0** |
| bad-statement removal | precision 1.0, recall 1.0 (4/4, no false removals in 12) |
| supported-fact retention | 1.00 |
| duplicate detection F1 | **0.889** (threshold chosen on the calibration split) |
| generated ideas over the demo opportunities | 17; unsupported-claim rate 0.0; top-10 max pairwise title Jaccard 0.5 |

## 2. Outcomes (real, opt-in, descriptive)

Creators may record what happened after publishing in `recommendation_outcomes`:

* the days since publication;
* views, impressions, click-through rate and average view duration;
* optionally, the search-traffic share **they read from YouTube Studio**.

The schema requires `consent = true`. The system never fetches owner analytics without
authorisation and never publishes anything.

`trend-agent evaluate recommendations` summarises outcomes **descriptively** (medians,
counts). It makes **no causal claim**:

* there is no control group;
* recommendations are not randomised;
* creators choose which ideas to make;
* higher search interest does not guarantee more views.

A causal estimate would need an experiment, for example randomised assignment of
comparable ideas, and enough of them. That experiment is not designed into this release.

Current outcomes recorded: **0** (status UNKNOWN).

## 3. Budget and safety controls in generation

* `MAX_RECOMMENDATIONS_PER_RUN` (default 20) caps each scheduled run (E2E `s14`).
* The daily and monthly LLM budgets fall back to the deterministic template generator
  when exhausted (`s11`).
* Generation through the API is private, rate-limited and refused when the operating mode
  withdraws it.
* Creator alerts are sent only when the mode allows them. In proxy mode, alerts carry the
  proxy basis.
