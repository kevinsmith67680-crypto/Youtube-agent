# Content-gap detection

A content gap is a **hypothesis about the inspected results**: within a stated scope
(query, region, language, N retrieved results) the titles and descriptions do not appear
to provide something searchers appear to need. It is never proof that existing videos fail
viewers, and it says nothing about YouTube's ranking.

## 1. Retrieval (bounded)

* Canonical query first (`search.list`, 25 results, region/language from the target or
  defaults, optional `publishedAfter`); one variant (e.g. "tutorial", "fix", "guide") only
  if fewer than 3 results look relevant. At most 2 search calls per analysis.
* Through the Phase 1 ingestion service: quota accounting, the 30-day metadata limit,
  stored `video_search_results`. Results stored within `cache_ttl` (24 h) are reused.
* The **API position** is the order one request returned, not a YouTube search ranking.
* Without an API key: stored results only; with none, the analysis abstains and the
  report says why.
* Results published after the analysis time are dropped (chronological replay).

## 2. Evidence tiers

| tier | what was inspected | allowed conclusions |
|---|---|---|
| A — metadata | title, description, dates, duration | "titles/descriptions do not mention X" (a weak hypothesis); never "the video omits X" |
| B — captions | authorised transcripts only (disabled by default) | coverage of a requirement in the transcript |
| C — full content | not implemented | — |
| D — insufficient | nothing usable | abstain |

Every video is registered as `VIDEO_METADATA` evidence (user-generated trust, YouTube
retention note, expiry = metadata expiry). Deterministic observations (supply counts,
coverage counts, gap statistics) are registered as `STATISTICAL_CALCULATION` evidence so
report sentences can cite them.

## 3. Relevance (`relevance.assess`)

A 0–100 lexical **relevance index** — not a probability that a viewer is satisfied:
entity match (0.35), intent cues/format (0.25), title (0.20), description (0.10), keywords
(0.10). Entity match is one of EXACT, FAMILY, VERSION_MISMATCH, OTHER_ENTITY (homonym),
NONE, NOT_APPLICABLE. A result is *relevant* when the index ≥ 60 (set from the calibration
sweep), the intent component is positive and the match is not OTHER_ENTITY/NONE; an
older-version result is not relevant for time-sensitive versioned intents. Item queries
require the item to be named. A description containing instruction-like text is ignored
(title only). Each assessment carries an explanation, limitations and algorithm version.

## 4. Freshness (`freshness.assess`)

* `OUTDATED_WITH_EVIDENCE` — the video is about an older version/season **and** a credible
  record states a breaking change, removal, replacement or vaulting;
* `POTENTIALLY_OUTDATED` — about an older version without such a record, or published
  before a verified release/change (whether it reflects the change is not established);
* `CURRENT` — consistent with the verified state, evergreen intent, or recent with no change;
* `UNKNOWN` — no date, or old without any verified change. **Age alone never makes a video
  outdated or inaccurate.**

## 5. Requirement coverage (`coverage.cell` / `summarise`)

Per requirement × video: `COVERED` (cue in title or ≥ 2 in description), `PARTIALLY_COVERED`
(one description cue), `NOT_EVIDENCED` (no cue — *content not inspected*), `CONTRADICTED`
(refers to a denied entity, or a mismatched version for an entity-specific requirement),
`UNKNOWN` (not about the entity). Summaries are scoped: "No title or description among 6
relevant inspected results mentions …; whether the videos cover it is not established."

## 6. Supply (`supply.summarise`)

`OBSERVED` counts within the inspected set (relevant, highly relevant, recent relevant,
exact entity, older version, channels, formats); `ESTIMATED`/`UNKNOWN` otherwise. Always
phrased with its scope ("Of the 25 retrieved results inspected, …"). **Disabled**: view
counts as demand or supply, `totalResults` as a count of competing videos.

## 7. Gap rules (`content_gaps.detect`, method `content-gap-rules-1.0`)

Rules run in order; the first block decides.

**Abstention → `INSUFFICIENT_EVIDENCE`** (nothing else is reported) when:
retrieval failed; fewer than 8 results; an entity is ambiguous; the query's subject is
`unknown`, `rumored` or `rejected` (e.g. an uncatalogued product version); an item is
`removed`/vaulted per a credible record; a blocking requirement depends on an unconfirmed
entity; or the intent is LOW confidence *and* broad.

**Adequate coverage → `NO_VERIFIABLE_GAP` only**: ≥ 3 relevant, current, intent-matching
results about the exact entity that address the essential requirements.

**Otherwise**, in precedence order:

| gap | condition (summary) | strength |
|---|---|---|
| `NO_RELEVANT_RESULTS` | none of the inspected results is relevant | moderate; strong with ≥ 16 inspected and a confirmed entity |
| `VERSION_MISMATCH` | confirmed versioned entity; ≥ 60 % of results about its family concern other versions and ≤ 1 matches the current one and the intent | strong with a verified breaking change, else moderate |
| `POTENTIALLY_OUTDATED` | time-sensitive intent, within the recent window after a verified change/release, ≥ 60 % of relevant results predate it | strong when at least half of those are outdated with evidence, else moderate |
| `INSUFFICIENT_PRACTICAL_DEMONSTRATION` | tutorial/demonstration/gaming-guide/problem-solving intent, ≥ 2 relevant exact-entity results, none in a tutorial/demonstration/guide format | moderate |
| `MISSING_SPECIFIC_ANSWER` | an essential requirement is not evidenced in any relevant exact-entity result | **weak hypothesis** at tier A (moderate with captions) |
| `LOW_RELEVANCE` | relevant results exist, none highly relevant, best index < 70 | moderate |
| `LANGUAGE_OR_REGION_GAP` | ≥ 3 relevant results with a known language and ≥ 70 % in another language | moderate |
| `FORMAT_GAP` | demonstration-type intent, ≥ 3 relevant results, ≥ 70 % short-form (≤ 75 s) | moderate |
| `NO_VERIFIABLE_GAP` | nothing above applies | weak with fewer than 3 relevant results, else moderate |

Every gap records: type, target query, intent, requirement keys, statement, supporting
evidence IDs (videos + observations + entity records), strength, limitations, method,
detection time, verification status (`verified`, `hypothesis`, `unverified`, `rejected`)
and its numeric observations.

## Worked examples (synthetic scenarios)

* **A** new AI model, installation intent: install videos are for version 1, the rest is
  news → `MISSING_SPECIFIC_ANSWER` (weak hypothesis).
* **B** game item location: six Season 7 guides, one Season 8 guide →
  `VERSION_MISMATCH` (strong; the season record vaults items).
* **C / C2** framework major version with / without a breaking-change record →
  `OUTDATED_WITH_EVIDENCE` only in C.
* **D** strong coverage → `NO_VERIFIABLE_GAP`. **E** ambiguous "Orbit" → abstain; **E2**
  with a category → resolved. **G** rumoured item → abstain. **J** too few results →
  abstain.

`trend-agent analyze demo -v` prints all scenarios with their checks.
