# Content-gap accuracy (Phase 8)

Command: `trend-agent evaluate content-gaps`. Code:
`src/trend_agent/evaluation/content_gaps.py`. It runs the Phase 5 analyzer on the
author-labelled fixture splits (`config/content_analysis/eval_{dev,calibration,heldout}.yaml`).

## 1. Four outcome classes

| Class | Assigned when |
|---|---|
| VERIFIED_GAP | the opportunity is a candidate **and** its primary gap's verification status is `verified` (every factual premise verified, observation reproducible) |
| CANDIDATE_GAP | the opportunity is a candidate whose gap is a hypothesis (e.g. inferred from metadata only) |
| NO_VERIFIABLE_GAP | no supported gap was found |
| INSUFFICIENT_EVIDENCE | the analysis abstained |
| (BLOCKED_POLICY) | prohibited topic; scored separately |

The fixture labels state whether a gap exists, that none exists, or that evidence is
insufficient. They do **not** state whether a gap is verified. So "verified versus
candidate" correctness is reported as **UNKNOWN**. Any VERIFIED_GAP on a "no gap" or
"abstain" case is a *false verified gap*, the worst error, and its target is zero.

## 2. Results (fixtures; the held-out split was not used for any threshold)

| Split | Cases | Predicted distribution | Gap precision | Gap recall | False gap rate | False VERIFIED_GAP |
|---|---|---|---|---|---|---|
| dev | 12 | verified 3, candidate 1, no gap 5, insufficient 2, blocked 1 | 1.00 | 1.00 | 0.00 | 0 |
| calibration | 8 | verified 1, candidate 1, no gap 5, insufficient 1 | 1.00 | 0.50 | 0.00 | 0 |
| **heldout** | 12 | verified 3, candidate 1, no gap 5, insufficient 2, blocked 1 | **1.00** | **0.80** | **0.00** | **0** |

The Phase 5 held-out first run, before the fixes made at that time, had a 50 % false-gap
rate. The current figures are after those fixes, and the held-out cases were not edited to
achieve them.

With 12 cases, a 0/7 false-gap result has a 95 % upper bound of about 35 % (Wilson). These
are behaviour checks, not accuracy estimates.

## 3. Real-world accuracy

**UNKNOWN.** There are 0 real `content_gap` labels, against a minimum of 50.

* **Limits of real gap analysis:** it needs YouTube search results (VIDEO_ACTIVITY), which
  require an API key; it is metadata-only (titles, descriptions, publication dates), so
  most real gaps will be CANDIDATE_GAP; and the 30-day storage limit bounds how far back
  inspected results can be kept.
* **How to label:** have a person watch the top results for a sample of analysed queries
  and record whether the claimed gap holds.
