# Anti-hallucination and evidence verification layer

This layer sits between anything that *generates* a recommendation (a language model, a
template, a person's draft) and anything that *shows* one. Its job is to stop the system
from presenting fabricated trends, invented statistics, nonexistent entities,
unsupported claims or misleading video ideas. It does not replace any Phase 1–4
component: it reads their records and adds a registry, a verifier and a gate.

> Verification checks claims against stored evidence with deterministic rules. It
> reduces the risk of inaccurate statements; it does not eliminate it. See
> [Known failure modes](#known-failure-modes) and [Residual risks](#residual-risks).

## Audit summary (Phases 1–4)

| Area | What was there | Hallucination risk found | Addressed by |
|---|---|---|---|
| Phase 1 ingestion | provider adapters, observations with `MeasurementType`, retention | none generated; source text (YouTube titles/descriptions) stored verbatim | source text is untrusted data with injection flags |
| Phase 2 discovery | deterministic extraction, optional LLM entity extractor (grounding-checked), keywords as *hypotheses* | an LLM-proposed entity is grounded in text but not confirmed; video titles/community posts can carry rumours | entity verification (confirmed vs reported vs rumoured), trust levels |
| Phase 3 monitoring | measurement categories (direct / external / proxy / synthetic), comparability | proxy activity could be mistaken for search demand downstream | `MeasurementSemantics` on every record; proxy-as-search rule |
| Phase 4 detection | `SurgeAlert` contract with classification, stage ceiling, severity, readiness | any consumer could restate or recompute numbers | `AuthoritativeStatistics` copied from the contract; numbers in text must equal it |
| LLM use | only the optional Phase 2 extractor; **no recommendation generator existed** and no LLM SDK is a dependency | — | generator protocol + gate built before any LLM writes recommendations |

## Architecture

```
 Phase 2 items/entities   Phase 3 observations   Phase 4 surge episodes
            \                     |                     /
             v                    v                    v
   (1) read-only snapshot  ── verification/service.py ──  SET TRANSACTION READ ONLY
                                  |
   (2) register (application principal only)
                                  v
         evidence_records  (versioned, signed IDs, immutable content, retention)
                                  |
                         evidence_bundles (IDs this request may cite, HMAC token)
                                  |
   (3) generator ── system prompt (trusted) + request JSON + <untrusted_source_data nonce>
        (template | scripted | Claude adapter)      no tools, no DB, no credentials
                                  |
                              raw text
                                  v
   (4) verification gate (gate.py) — 10 steps, bounded retries, decides publish/withhold
                                  |
         verification_runs / verification_attempts / verification_claims
                                  v
                    VerifiedRecommendation (verified-recommendation/1.0)
```

| Module | Responsibility |
|---|---|
| `schemas/verification.py` | evidence, claim, draft, verdict, statistics and published-result models |
| `verification/registry.py` | signer, content hashing, versioned registration, bundles, reference resolution, freshness |
| `verification/sources.py` | Phase 1–4 rows → evidence drafts with deterministic facts |
| `verification/claims.py` | claim extraction: numbers (typed), dates, identifiers, trend/search wording, release, certainty, attribution |
| `verification/entities.py` | catalogue resolution, ambiguity, existence/confirmation, release dates, denials, conflicts |
| `verification/authority.py` | engine statistics block; checks every trend/search/statistic statement |
| `verification/verifier.py` | per-claim support, contradiction scan, freshness, attribution, action |
| `verification/untrusted.py` | sanitisation, data block encoding, injection signals, output checks |
| `verification/permissions.py` | principals, capabilities, tool authorisation, read-only transactions |
| `verification/generation.py` | generator protocol, prompts, safe parsing, template and scripted generators |
| `verification/anthropic_generator.py` | optional Claude adapter (lazy SDK import) |
| `verification/gate.py` | the gate: steps, retries, revision, abstention, publication |
| `verification/service.py` | database assembly, registration, gate wiring, read models |
| `verification/world.py`, `evaluation.py` | fixture worlds and the labelled evaluation |
| `db/verification_models.py`, migration `0005` | five tables + immutability trigger |
| `cli_verify.py` | `trend-agent verify …` |

## 1. Evidence registry

Every external factual assertion the system can make is traceable to an
`evidence_records` row:

| Field (spec) | Column / model field |
|---|---|
| Evidence ID | `evidence_id` (`ev_` + 26 base32 chars of HMAC-SHA256) |
| Source provider | `provider_name` |
| Canonical source URL/reference | `canonical_url`, `source_reference` |
| Source publication timestamp | `published_at` |
| Collection timestamp | `collected_at` (and `registered_at`) |
| Evidence type | `evidence_kind` (official announcement, code release, video metadata, community post, suggestion, measurement, surge detection, entity record, calculation, synthetic) |
| Measurement semantics | `measurement_semantics` (direct/external search interest, video activity, suggestion presence, source statement, entity registry, derived statistic, synthetic) |
| Relevant supported facts | `facts` (structured, extracted by code: mentions, release dates, measurements, surge fields) and `excerpt` (sanitised source text) |
| Verification status | `status` (recorded / superseded / source_removed), `trust_level`, `injection_flags`, integrity checked on every use |
| Data freshness | `fresh_until`; FRESH/AGING/STALE computed per kind at use (`config/verification.yaml`) |
| Retention restrictions | `expires_at` (copied from the source), `data_policy_id`, `retention_note` |

**Why the generator cannot fabricate or substitute IDs**

1. IDs are issued only by `EvidenceRegistry.register`, as an HMAC over
   (evidence key, version, content hash) with a server key
   (`TREND_AGENT_VERIFICATION_SIGNING_KEY`, required when `environment=production`). A
   made-up ID resolves to nothing → `unknown_evidence_id`.
2. A generation request receives a **bundle** (stored, HMAC-tokened). A real ID from
   another request or topic → `evidence_not_in_bundle`.
3. Every resolution recomputes the content hash and the HMAC → an edited row or a copied
   ID → `evidence_integrity`. The database also rejects content updates with a trigger
   (`evidence_records_immutable`).
4. Expired/removed sources → `evidence_unavailable`; synthetic records outside a synthetic
   run → `synthetic_evidence`; anything that is not `ev_…` → `malformed_evidence_id`.
5. Resolving is not support: the claim verifier still checks that a resolvable record
   states *this* claim.

**Versioning.** Registration is idempotent per content. A changed source (edited item,
revised observation — keyed by series and period —, an updated surge episode) registers
a new version and marks the old one `superseded` in the same transaction (advisory lock
per evidence key; a partial unique index keeps one current version).

## 2. Claim verification

Claims come from two places: the generator's declared `factual_claims` (with its
citations) and **every factual-looking sentence** of the title, rationale, audience-intent
text and uncertainty notes (cited against the draft's `supporting_evidence_ids`). A
generator cannot avoid verification by leaving a statement out of its claim list.

For each claim (`verifier.py`):

1. **Reference problems** of the cited IDs are recorded.
2. **Entities**: catalogue mentions (resolved by topic, category, evidence or context
   words), the declared subject, and product-like names not in the catalogue
   (identifiers such as `RTX 5090`, CamelCase, capitalised runs). Unknown names must
   appear verbatim in a supporting record.
3. **Support** (cited records first): every entity, every number (typed: currency,
   quantity, count; matched with tolerance), every date (at its granularity) and the
   release wording must be stated by a supporting record; the claim's remaining content
   words must be covered (`term_coverage`, default 60 %) and all salient (capitalised,
   numeric) words must be present. Measurements and surge records never support product,
   release or existence claims. If only uncited bundle records support the claim, the
   citation is replaced by code (`RECITE`, `citation_mismatch`).
4. **Contradictions** across the whole bundle: credible denials of a name ("There is no
   X", "X is fake", "no plans to release X"), release dates that differ from a credible
   date, releases still in the future ("is out now" vs "will be released on…"), prices or
   specifications that differ from a credible record, and conflicting credible release
   dates.
5. **Trend, search and statistics wording** is checked against the engine
   (see §4).
6. **Freshness and novelty**: time-sensitive claims need non-stale support; "new",
   "just announced", "latest" need support published within `novelty_window`.
7. **Attribution**: speculative or reported wording needs attribution; a claim supported
   by one lower-trust source can only be published as an attributed, labelled report.

| Status | Meaning |
|---|---|
| VERIFIED | supported, with at least one credible record (official source, curated entry, stored data) |
| CORROBORATED | supported only by lower-trust records from ≥ 2 independent sources (published with attribution and a label) |
| UNVERIFIED | anything else (missing, fabricated or irrelevant citations, single low-trust source, stale, unknown/ambiguous entity, unsupported number) |
| CONTRADICTED | a credible record conflicts with it (wins over any support) |

An optional `SemanticVerifier` (for example an NLI model or LLM judge) can be plugged in;
it can only **downgrade** a verdict, never upgrade one.

## 3. Structured outputs

`RecommendationDraft` (`recommendation-draft/1.0`, `extra="forbid"`) is the generator's
output: proposed video title, topic ID, search query ID, audience search intent (+
description), supporting evidence IDs, factual claims (text, type, subject, evidence IDs,
certainty, attribution), `verification_status` (must be `pending_verification`),
uncertainties, rationale, and `insufficient_evidence`. There is deliberately **no field**
for surge scores, growth, search volume, severity, p-values, confidence, freshness or
readiness. `trend-agent verify schema --which llm` prints the subset sent to structured
output APIs (`additionalProperties: false`, no length/range keywords).

`parse_draft` handles: malformed JSON, non-object output, schema violations, refusals,
truncated generations (`max_tokens` — never accepted, even if the JSON happens to parse),
empty output, fenced JSON. Unknown fields are dropped and recorded (`unsupported_field`;
statistic-like names additionally `statistic_field`); a claimed verification status is
reset (`self_verification`). **A schema-valid draft is still entirely unverified.**

`VerifiedRecommendation` (`verified-recommendation/1.0`) is the only published form. It
carries the verified title, the published claims with their status, evidence IDs and
labels, citations (provider, URL, publication time, kind, semantics, trust, freshness,
retention), the engine's statistics and trend statement, uncertainties, removed
statements, missing evidence, violations, versions and the abstention state.

## 4. Deterministic statistical authority

`AuthoritativeStatistics` is copied from the Phase 4 `SurgeAlert`s of the target:
classification, highest stage, evidence categories, severity, relative change (only for
direct/external search interest), observed/expected, scale, confidence status, baseline
readiness, freshness, event IDs and their evidence IDs. Nothing is recomputed.

| Statement in generated text | Rule |
|---|---|
| a number of searches ("2 million searches", "120,000 times a month") | always rejected: search data is a relative index |
| a growth percentage or multiplier | must equal the engine's relative change (±2 pp / 2 %); a different value is CONTRADICTED; none available → rejected |
| severity, score, p-value, confidence, CUSUM | must equal the engine's value; otherwise rejected (CONTRADICTED when the engine has a different value) |
| "statistically significant" | needs `STATISTICALLY_SUPPORTED` |
| "confirmed surge" | needs stage CONFIRMED |
| search/demand growth | needs DIRECT or EXTERNAL search interest at stage ≥ CANDIDATE |
| "YouTube search …" | needs DIRECT YouTube search interest (`external_as_youtube`, `proxy_as_search`) |
| any growth wording with proxy-only evidence | `proxy_as_search`: video activity, suggestions, announcements and community posts are never search growth |
| growth with a stale engine update | `stale_evidence` |

Accepted trend wording is not published as written: the gate publishes the engine's
deterministic `trend_statement` (e.g. "YouTube search interest for “q” is +140% against
its expected level - a relative index, not a number of searches. Detection stage:
CONFIRMED …"). Proxy-only targets get "… a potential YouTube demand opportunity …, not
measured YouTube search growth."

## 5. Entity verification

| State | Condition |
|---|---|
| CONFIRMED | a credible record mentions it without denying it, or a curated (gazetteer) entry |
| REPORTED | ≥ 2 independent lower-trust sources (derived records such as entity rows do not count as sources) |
| RUMORED | one lower-trust source, or a Phase 2 entity still `needs_verification` |
| UNKNOWN | not catalogued and not mentioned by any evidence (invented products, fake items) |
| AMBIGUOUS | several catalogued entities share the name and neither topic, category, evidence nor context words decide |
| REJECTED | rejected by Phase 2 grounding, or credibly denied |

Release dates come from credible records only (facts, dated sentences, and "available
today" posts dated by their publication); two different credible dates are a conflict,
surfaced in `missing_evidence` and as an uncertainty label. Missing product details are
never filled in: an attribute no record states remains unsupported.

## 6. Prompt-injection protection

Controls, in order of importance (none relies on a regex or an LLM judge alone):

1. **No capability to act.** Generators receive text and return text. The Claude
   adapter sends no tools. Any tool request in a response is authorised by
   `PermissionPolicy` with the principal fixed by the receiving code
   (`GENERATOR`), never by the request's content. `execute_code`, `read_credentials`,
   `modify_database`, `network_request`, `register_evidence`,
   `write_verification_record` and `publish_recommendation` can never be granted to a
   generator or to source content (the policy refuses such a configuration). The only
   tool a generator could be offered, `lookup_evidence`, is limited to its bundle.
2. **Least privilege in the database.** Evidence assembly runs in a
   `SET TRANSACTION READ ONLY` transaction; the gate's registry view is read-only; only
   the application principal registers evidence and writes verification records.
3. **Separation of instructions and data.** Trusted rules travel in the system prompt.
   Source content is JSON-encoded inside `<untrusted_source_data nonce="…">` with `<`, `>`
   and `&` escaped, so content cannot close the block; control, zero-width and bidi
   characters are removed; the delimiter name is neutralised; lengths are bounded.
4. **Output-side checks.** URLs not taken from evidence (`unlisted_url`), instruction- or
   credential-like text and self-certification ("verified by the system",
   `suspicious_output`) are removed.
5. **Verification of every fact.** Whatever injected text persuades a generator to
   write, it must still be supported by credible evidence; a record carrying injection
   flags cannot make a claim VERIFIED on its own.
6. **Signals.** `injection_signals` labels records (`override_instructions`,
   `role_injection`, `verification_tampering`, `statistic_injection`, `code_execution`,
   `database_modification`, `credential_access`, `exfiltration`, `hidden_characters`).

## 7. Abstention

| State | Published? | When |
|---|---|---|
| VERIFIED_RESULT | PUBLISH | every published factual statement is VERIFIED, nothing was removed, the title is the generator's, and demand is measured by direct YouTube search interest at CANDIDATE/CONFIRMED with a fresh engine update |
| PARTIALLY_VERIFIED | PUBLISH_WITH_LABELS | publishable, but something was removed, attributed, re-titled, or demand is indirect/external/absent |
| INSUFFICIENT_EVIDENCE | WITHHOLD | no evidence; topic entity unknown/ambiguous/denied; no supportable title; every factual statement unsupported; the generator itself reported insufficient evidence |
| CONTRADICTED | WITHHOLD | a core statement (the title, or an existence/release/event claim about the topic entity) is contradicted, including by conflicting credible sources |
| SOURCE_UNAVAILABLE | WITHHOLD | generator refused, failed, timed out or never produced a schema-valid draft; evidence sources removed/expired |

A withheld result carries no title, claims or citations — only the state, the trend
statement, `missing_evidence` and the removed statements (for audit). Synthetic contexts
are never plainly published (`PUBLISH_WITH_LABELS` at most, `[SYNTHETIC]` prefixes).

## 8. The verification gate

`VerificationGate.run(context, generator)`:

| Step | Implementation |
|---|---|
| 1 validate schema | `parse_draft`; malformed/incomplete → regenerate (bounded); refusal → abstain, no retry |
| 2 resolve evidence | `EvidenceRegistry.resolve` for bundle and every citation; query/topic IDs forced to the request's |
| 3 verify claims | `ClaimVerifier.verify` on declared and extracted claims |
| 4 numerical consistency | number support/contradiction + statistics authority |
| 5 freshness | per-kind freshness, novelty window, stale engine updates |
| 6 trend classification | `authority.check_trend` |
| 7 entity existence | `EntityVerifier` (unknown, ambiguous, rumoured, denied, conflicting) |
| 8 uncertainty labels | attribution labels, conflicts, indirect demand, synthetic |
| 9 reject or revise | remove unsupported statements, re-cite, re-render trend wording; deterministic fallback title; regenerate only for an unsupported title / contradicted core / nothing left, at most `retry.max_attempts` (3) generations, stopping as soon as a rejected claim is generated again |
| 10 persist | `verification_runs`, `verification_attempts`, `verification_claims` |

The state and the publication decision are computed by `_finish` from the verdicts —
never taken from the generator. Generator exceptions, refusals and tool requests cannot
take the gate down or change its decision.

## Generators

| Generator | Use |
|---|---|
| `TemplateGenerator` (default, `generator.default: template`) | deterministic, offline: quotes credible records with attribution, builds the title from the monitored query, never states statistics |
| `ScriptedGenerator` | replays prepared outputs (tests, adversarial evaluation, `verify check`) |
| `AnthropicGenerator` (optional) | Claude via the official SDK (`pip install anthropic`, `TREND_AGENT_ANTHROPIC_API_KEY`): model `claude-opus-5-5`, structured output via `output_config.format` (JSON schema), server-side refusal fallbacks (`fallbacks="default"`, beta `server-side-fallback-2026-07-01`; disable with `generator.server_side_fallbacks: false`), `stop_reason` checked before content (`refusal`, `max_tokens`). **Not exercised against the live API in this repository** (no key, no network in tests); tests use a fake client with the SDK's response shape. |

## CLI

```bash
trend-agent verify recommend --target 42 [--generator template|anthropic] [--dry-run] [--json]
trend-agent verify check --target 42 --draft draft.json      # verify an external draft
trend-agent verify runs [--target 42]                        # decisions
trend-agent verify explain <run-uuid>                        # steps + every claim verdict
trend-agent verify evidence [--prefix surge_event:] [--id ev_…]  # registry / integrity check
trend-agent verify stats
trend-agent verify evaluate [--split dev|heldout|all] [-o results.json]   # offline
trend-agent verify demo                                      # offline walk-through
trend-agent verify schema [--which draft|recommendation|llm]
```

## Known failure modes

These are properties of the deterministic, English-only, lexical approach; several were
observed in the evaluation (see `docs/verification_evaluation.md`).

1. **Paraphrase → false rejection.** Support is lexical (term coverage + exact numbers,
   dates, names). A correct claim worded very differently from its source is removed
   (held-out first run: 2 of 20 supported claims, e.g. a definitional sentence).
2. **Same words, different relation → false acceptance.** "X is not compatible with Y"
   against a source saying "X is compatible with Y" passes term coverage; negation is
   only modelled for release/denial wording. Relation errors with matching words can be
   verified.
3. **Contradiction needs explicit credible facts.** A wrong release date is CONTRADICTED
   only if a credible record dates the release; otherwise it is merely UNVERIFIED and
   removed (contradiction recall 0.6 → 0.8 on held-out after fixes). "Newest model"
   claims against older evidence are removed, not contradicted.
4. **Name detection heuristics.** Capitalised runs, identifiers and CamelCase find
   product names; lower-case or Title Case titles rely on grounding of every content
   word instead; unusual names can be missed, ordinary phrases can be flagged unknown.
5. **Trend vocabulary is finite.** Growth/search wording outside the lexicon ("more than
   ever" was caught via "searching", other idioms may not be) can slip through as a
   non-factual sentence.
6. **Non-factual sentences are kept.** Advice and opinion without factual markers are
   published unverified by design; a misleading meta-statement without markers could
   pass (self-certification is now caught; other forms may not be).
7. **Trust is per source configuration.** "Official" depends on `verified` flags in
   discovery source configuration; a compromised or mistaken official source is believed.
8. **Corroboration counts sources, not truth.** Two independent low-trust sources make a
   claim CORROBORATED (published with attribution and a label), even if both repeat the
   same rumour.
9. **Ambiguity resolution by context words** can choose the wrong entity when a claim
   mixes vocabulary of both candidates.

## Residual risks

* Real-world accuracy is **unknown**: all evaluation data is fictional and labelled by the
  same team that built the verifier; there is no independent annotation.
* The live Claude path has not been run; its refusal/fallback behaviour is implemented
  from the SDK documentation, not observed.
* The verifier checks what sources *say*, not what is true; false official
  announcements, edited pages and stale caches propagate.
* Development signing key is fixed and public (fine for tests, never for production);
  key rotation invalidates existing IDs (re-register).
* The application uses one database role; database-level least privilege (separate roles
  for read-only assembly vs writes) is recommended for deployment but not provisioned
  here. The read-only transaction is enforced per transaction, not per role.
* Non-English content is not supported by the extractors.
* The gate adds latency proportional to bundle size (≤ 40 records by default);
  pathological inputs are bounded by excerpt and field length limits.
