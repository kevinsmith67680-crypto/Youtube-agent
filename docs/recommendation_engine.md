# Recommendation engine

`RecommendationEngine.recommend(report, forecasts, evidence, profile)` turns one verified
Phase 5 opportunity into at most `generation.max_concepts_per_opportunity` (5) video
concepts. It is pure: no database, no network unless an LLM generator is injected.

## 1. Input validation (`context_builder.validate_inputs`)

| Check | Result |
|---|---|
| policy flags on the intent (e.g. cheats, account compromise) | not eligible |
| status not in `generation.eligible_statuses` | not eligible, except `insufficient_evidence` → one research candidate |
| report older than `max_report_age_hours` (168) | research candidate only |
| forecast for another query, synthetic flag differs, older than `max_forecast_age_hours` (36) or dated in the future | forecast dropped with a stated limitation; **never read as zero growth** |
| category / exclusions in the creator profile | skipped with the reason |

## 2. Context (`build_context`)

Confirmed entities only (most specific first), a verified prior version as comparison
partner when the catalogue has one, information requirements with whether evidence
establishes them, the primary gap, credible fact sentences (official, code, curated, news
records that mention a confirmed entity), evidence references (third-party video metadata is
referenced by video ID only), demand and forecast summaries, the creator profile, and a
fingerprint of all of it.

## 3. Formats (`format_selector.select_formats`)

Each of the 30 formats declares its category, the primary and sub-intents it serves, the
gap types it answers, its planning hours and profile, required software/hardware, title
strategies, hook types and an outline plan. Only formats that serve the intent are
offered; a format that needs a second entity (`{other}`) is only offered when one is
verified.

## 4. Generation (`idea_generator`)

* **Template generator** (default, deterministic, no cost): titles, hooks and outlines are
  composed from confirmed names, requirements and credible facts by the format's plan.
* **LLM generator** (optional, `llm.enabled: true` + `TREND_AGENT_ANTHROPIC_API_KEY`):
  structured drafts (`ConceptSet` schema, unknown fields forbidden, no numeric fields),
  `llm.max_attempts` (2) bounded attempts with the rejection reason fed back, then the
  template fallback (recorded on the outcome and the run). Prompts are documented in
  `video_generation_prompts.md`.

## 5. Verification (`verification.verify_concept`)

Two layers; neither trusts the generator.

**Phase 7 rules (deterministic)**

* Titles: no clickbait, superlatives, promises of views, numbers that are not part of the
  query or a confirmed name (no invented statistics or versions), length limits.
* Hooks: 5–15 seconds (8–45 words), no untested results stated as fact, no artificial
  urgency, no promises, no invented numbers.
* Outline labels: a `VERIFIED_FACT` must cite evidence from this analysis' bundle, otherwise
  it becomes `UNKNOWN`; a planned demonstration or hypothesis written as a completed result
  is removed.
* Instruction-like text ("ignore previous instructions", "system prompt"), shell commands
  (`rm -rf`, `sudo`, `curl … | sh`, …) and credential words are removed from titles, hooks and
  outline items.
* Forecast and view claims are stripped from the premise and rationale; demand and
  forecasts are attached from Phase 4–6 outputs only.
* Evidence references outside the bundle are reported.

**The verification gate (existing anti-hallucination layer)** checks every declared
`VERIFIED_FACT` (entities, release wording, numbers, lexical support in the cited records)
and the factual elements of the free text (title, alternative titles, hooks, premise,
rationale). Failing statements are removed and listed. If no title passes, the gate does not
run, so no statement may keep the `VERIFIED_FACT` label.

**Status (application code)**

| Status | When |
|---|---|
| `REJECTED` | a core statement is contradicted by credible evidence |
| `RESEARCH_REQUIRED` | no supported title, research-only opportunity, gate state insufficient evidence / source unavailable, or no supportable hook / nothing to film |
| `APPROVED` | gate verified, nothing removed or relabelled, no out-of-bundle references |
| `APPROVED_WITH_UNCERTAINTY` | otherwise (statements removed or relabelled, partial verification, indirect demand) |

## 6. Feasibility (`feasibility.evaluate`)

Planning hours from the format, plus research time for requirements the evidence does not
establish; the profile is the nearest of QUICK_RESPONSE (2 h), STANDARD_TUTORIAL (8 h) and
DEEP_TECHNICAL_VIDEO (24 h). The breakdown (research, testing, recording, editing) sums to
the expected hours. Equipment, skills and capacity are judged **only** from the creator
profile; when it does not state them the answer is "unknown", never "yes". Urgency comes
from the Phase 6 opportunity window for that profile; without one it is `UNKNOWN`.

## 7. Ranking, deduplication and diversity

See `recommendation_scoring.md`. Duplicates (same opportunity objective and title word
overlap ≥ `diversity.title_similarity_merge`) are merged into the higher-ranked idea; at most
3 ideas per opportunity, 1 per topic × format family, and (when enough items exist) at most
60% per category. Every suppression is stored with its reason.

## 8. Identity, reuse and cost control

Recommendation IDs are UUIDv5 of (opportunity, format, profile, input fingerprint): the same
evidence regenerates the same IDs, so persistence is idempotent and unchanged evidence is
reused without generating. The service enforces `llm.daily_token_budget` (sum of input and
output tokens of today's runs) and records calls, tokens and the estimated cost at the
configured list prices on every run.
