# Keyword discovery — how candidates are found, labelled and ranked

> **Discovery finds candidates, not demand.** A prioritised keyword is one that is grounded
> in evidence and worth *monitoring*. Nothing in Phase 2 says how many people search for it.
> Search suggestions and related queries show that a query *exists*; video titles show
> *supply*; announcements show that an *entity* exists. None of these are stored as search
> volume (no `search_interest_observations` rows are written by discovery).

## 1. Sources and evidence types

Every stored item carries an `evidence_type`:

| Evidence type | Typical source | What it shows |
|---|---|---|
| `official_announcement` | vendor/publisher RSS or Atom feeds | an entity/event exists (trusted) |
| `code_release` | PyPI release feeds, GitHub releases | a software version was released (trusted) |
| `search_suggestion`, `related_query` | query-level providers (e.g. Trends related queries when access exists) | a query string is in use — **not** how often |
| `video_title`, `video_description` | YouTube Data API search/charts/channels | creators are publishing on a topic (supply) |
| `community_discussion` | Hacker News, Reddit | people discuss it (weak, often speculative) |
| `curated_seed` | `config/seeds/*.yaml` | an operator chose it as a starting point |
| `synthetic_test_data` | the bundled fixture only | nothing real; `simulated_evidence_type` records what it imitates |

Providers and their availability in this repository are listed in
[phase2_report.md](phase2_report.md#c-working-discovery-sources).

## 2. Entities

`DeterministicExtractor` works without any network access or API key:

* **Gazetteer** — curated names/aliases per category (`config/seeds/*.yaml`), with
  case-sensitive aliases (e.g. `GTA`) and `context_terms` that disambiguate same-name things.
* **Known entities** — everything already stored is matched too, so a new mention of a
  previously discovered entity is recognised.
* **Patterns** — versioned names ("Nimbus Coder 2", "Lattice 5.0.1"), release-feed titles
  (with the source's `entity_hint`), introduction cues ("today released/announced/introducing"),
  and game structure (events, seasons, updates, items introduced by an event).
* **Families** — a versioned entity gets a family parent ("Nimbus Coder 2" → "Nimbus Coder");
  versions are separate entities and are never merged (`Lattice 5.0` ≠ `Lattice 5.0.1`;
  trailing `.0` is folded for matching only: `5.0.0` = `5.0`).
* **Attributes** (e.g. `local`, `api`, `pricing`, `breaking`, `challenges`, `rewards`) are
  extracted only from sentences about that entity (subject carry-over for "it", "the model",
  "the festival"), so a product mentioned in passing does not inherit another's features.
* **Status** — `verified` with trusted evidence (official/code release/query-level) or ≥ 2
  independent origins; otherwise `needs_verification`. Game items need trusted evidence:
  repeated community claims are not enough. Rumours (`leak`, `rumor`, `reportedly`, …) are flagged `speculative`, cost 0.2
  confidence and are never expanded; an item named only in a rumour does not become an entity.
* **Release dates** come only from (a) a date stated next to a release cue in a sentence
  naming the entity — official sources win and conflicts > 1 day are recorded — or (b) the
  first official/code-release publication of a new, verified entity. Curated entities,
  families and unverified entities have no release date. The basis is stored
  (`release_date_basis`).
* **Optional LLM** — plug in via the `EntityExtractor` interface. Proposals whose name is
  not in the item text are stored as `rejected` and never expanded.

## 3. Candidate keywords

| Method | Origin label | Rule |
|---|---|---|
| `observed_query` | `observed_query` | the exact query string returned by a suggestion/related-query provider |
| `entity_template` | `generated_hypothesis` | intent template applied to a **verified** entity; every template's requirements must be grounded (e.g. "pricing" needs a pricing mention) |
| `entity_comparison` | `generated_hypothesis` | "A vs B" only for B = the nearest older sibling version or an entity compared with A in the evidence (never its own family, never the reverse pair) |
| `title_phrase` | `generated_hypothesis` | a query-like segment of a video/community title that names a verified entity ("How to Install X Locally") |
| `seed_term` | `generated_hypothesis` | a curated seed with `track_as_keyword: true` |

`verified_interest` is reserved for a later phase with real interest data; the lifecycle
module refuses to let discovery set it, and an observed query is never downgraded to a
hypothesis. Each candidate stores its `grounding` (e.g. `entity:game_item:Glacier Lance`,
`attribute:local`, `release_level:major`, `competitor:Lattice 4.0`).

**Expansion limits** (configurable): depth 0/1/2 (none / templates / + comparisons), max
templates and comparisons per entity, max keywords per run (observed queries are kept first
when capping), min entity confidence, max query words. Not expanded: evergreen curated
entities (only their *new* releases/events are), unverified entities, pre-releases, patch
releases of software, superseded code releases (a newer minor supersedes a minor; only a
newer major supersedes a major), outdated items (> `max_item_age_days`).

## 4. Classification

* **Intent** — 13 intents. Weighted regex rules produce a score per intent, normalised to
  sum to 1; several intents are kept (e.g. "how to fix game crashing" → troubleshooting +
  how-to). These are rule scores, **not calibrated probabilities**
  (`intent_scores_uncalibrated` in `discover show`).
* **Category** — vocabulary + gazetteer names; a linked entity's category dominates;
  secondary categories are retained (e.g. "gaming AI npc mod" → gaming, with AI score).

## 5. Clustering (two levels)

* **Topic** — the linked entity (`entity:<key>`), otherwise a lexical topic that never
  merges strings containing different numbers (versions, seasons, years).
* **Intent cluster** within a topic — intent family + *facet* (the query words left after
  removing the entity/parent names and filler, with light plural folding), merged when facet
  Jaccard ≥ `intent_similarity_threshold`. So "how to install X", "X install" and
  "install X guide" share `instructional:install`, while "X benchmark" and "X pricing" stay
  separate.
* Every assignment stores a decision record (`method` new/exact/lexical, matched query,
  score, threshold, backend, facet) — visible with `discover show` and `discover clusters`.

## 6. Scoring

All components are 0–100, stored with the keyword, and recomputable.

**Novelty** depends only on stored timestamps, so re-processing the same evidence at the
same time gives the same result: entity novelty (decays from first seen; 10 for evergreen),
query novelty (decays from first discovery), intent novelty (cluster founded within the
novelty window; higher if its topic already existed), recency of the release date (30 when
unknown). Kinds: `new_entity`, `new_version` (an ancestor was known earlier),
`new_event` (game event/update/item of a known game), `new_intent`, `recurring`, `evergreen`.

**Priority (`priority-v1`)** = 0.25 novelty + 0.20 freshness + 0.20 corroboration +
0.15 relevance + 0.10 intent actionability + 0.10 evidence quality.

* *freshness* — decay from the newest evidence (half-life 7 days).
* *corroboration* — `100 × (1 − 0.7^n)` for n **independent origins**: items sharing a
  canonical URL, publisher, (provider, author), or near-identical text (containment ≥ 0.6 for
  substantial texts, Jaccard ≥ 0.8 for short ones) are one origin, so syndicated copies and
  reposts do not add corroboration. Speculative items are excluded.
* *relevance* — category confidence and seed/gazetteer priority.
* *intent actionability* — fixed table (how-to 100 … news 40, other 20).
* *evidence quality* — base quality per evidence type × source reliability, minus penalties
  for missing/conflicting/future dates, outdated, speculative and low-quality claims.

The weights are initial heuristics, not experimentally validated, and the score contains no
search-volume input. The stored `score_components.note` says so.

## 7. Lifecycle

`discovered → validated → prioritized → monitoring → dormant`, `rejected` from any
non-terminal state. Every transition is validated and logged in `keyword_status_history`.

* *validated*: coherent (2..max words), in a target category, grounded (observed, or
  generated from a verified entity).
* *prioritized*: priority ≥ `prioritize_threshold` (demoted to validated when it falls below).
* *monitoring*: only the `monitoring` actor (Phase 3) may set it; discovery cannot.
* *dormant*: no new evidence for `dormant_after_days`; new evidence re-activates it.

## 8. Budget and scheduling

Per run: fetch slots are split across categories by `category_budgets` (largest remainder;
a single-category run gets only its own share). Within a category, `exploration_share` of the
slots is reserved for exploration sources (new entities' queries, never-fetched sources);
unused slots flow to the other pool. Sources are skipped (with a recorded reason) when
disabled, the provider is not configured/unsupported, the minimum poll interval (doubled per
consecutive failure) has not elapsed, or local quota is insufficient. Remaining sources are
ranked by reliability × expected yield × staleness. Unfillable slots stay unused — nothing is
invented to meet a share. No bandits/RL.

## 9. Data-quality behaviour (covered by the synthetic fixture and tests)

| Case | Behaviour |
|---|---|
| duplicate delivery of an item | stored once (unique provider + reference); counted as `dup` |
| syndicated/reposted articles | one independent origin |
| conflicting release dates | most authoritative date chosen, conflict recorded, conflicting items penalised |
| outdated items | flagged `outdated`, not expanded |
| missing timestamps | flagged, penalised, neutral recency |
| ambiguous names / same name, different product | resolved by context terms, version tails and category; otherwise separate entities ("Nimbus 2" thermostat ≠ "Nimbus Coder 2") |
| provider outage, rate limit, quota exhaustion | source marked failed with typed error; run `partial`; backoff |
| low-quality / speculative claims | penalised; never create game items; never expanded |
| source with no registered/licensed provider | skipped as `unsupported`; never scraped |
