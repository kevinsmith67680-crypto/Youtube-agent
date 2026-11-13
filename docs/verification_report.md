# Verification layer report — anti-hallucination and evidence verification

Scope: audit Phases 1–4 and add safeguards that prevent fabricated trends, invented
statistics, nonexistent entities, unsupported claims and misleading recommendations —
without replacing working functionality. Phase 5 was not started.

## 1. Architecture and implementation summary

The layer adds four things around the existing pipeline (design:
[verification_layer.md](verification_layer.md)):

1. **Evidence registry** — every Phase 2 item/entity, Phase 3 observation and Phase 4
   surge episode used in a recommendation becomes a versioned, signed, immutable record
   with provenance, semantics, trust level, freshness and retention.
2. **Claim verifier** — deterministic, per-claim support and contradiction checks against
   the cited records and the whole request bundle, with entity verification and the
   engine as the sole authority for statistics.
3. **Generators** — a protocol with a deterministic template generator (default), a
   scripted generator (tests/evaluation/external drafts) and an optional Claude adapter.
   Generators get text in and give text out: no tools, no database, no credentials.
4. **Verification gate** — 10 steps; bounded regeneration; deterministic revision;
   abstention and publish/withhold decided by application code; every decision persisted.

Audit result: the only existing LLM touchpoint was Phase 2's optional, grounding-checked
entity extractor; no recommendation generator existed and no LLM SDK is a dependency.
Phase 1–4 code paths are unchanged except for additive wiring (retention, container, CLI,
settings).

## 2. Repository changes

| Kind | Files |
|---|---|
| New package | `src/trend_agent/verification/` — `config.py`, `registry.py`, `sources.py`, `claims.py`, `text.py`, `entities.py`, `authority.py`, `verifier.py`, `untrusted.py`, `permissions.py`, `generation.py`, `anthropic_generator.py`, `gate.py`, `service.py`, `world.py`, `evaluation.py` |
| Schemas | `src/trend_agent/schemas/verification.py` |
| Database | `src/trend_agent/db/verification_models.py`, `db/repositories/verification.py`, migration `20261113_0005_verification_layer.py` (5 tables, immutability trigger) |
| CLI | `src/trend_agent/cli_verify.py` (`trend-agent verify …`), registered in `cli.py` |
| Wiring (additive) | `container.py` (`verification_config()`, `verification()`), `config.py` (`verification_signing_key`, `anthropic_api_key` as `SecretStr`), `db/models.py` (model registration), `ingestion/retention.py` (verification rows purge with their sources) |
| Configuration | `config/verification.yaml`; fixtures `config/verification/world.yaml`, `cases_dev.yaml`, `cases_heldout.yaml` (fictional) |
| Tests | `tests/unit/verification/` (55 tests), `tests/integration/test_verification.py` (6), `tests/conftest.py` + `tests/integration/test_database.py` updated for revision 0005 |
| Docs | `docs/verification_layer.md`, `docs/verification_evaluation.md`, this report, README and `docs/compliance.md` sections, `docs/benchmarks/verification_*.json` |

## 3. Evidence and verification schemas

* `EvidenceDraft` / `EvidenceRecord` — evidence ID, key, version, status, source kind and
  row, provider, source reference, canonical URL, publisher, author, publication and
  collection times, evidence kind, measurement semantics, trust level, structured facts,
  sanitised excerpt, content hash, signer key id, freshness horizon, data policy,
  retention note, expiry, synthetic flag, injection flags.
* `EvidenceBundle` — the IDs one generation request may cite, with an HMAC token.
* `RecommendationDraft` (`recommendation-draft/1.0`) — generator output; no statistics
  fields; `verification_status` must be `pending_verification`.
* `ClaimVerdict` — per claim: origin, field, type, certainty, status, action, cited /
  supporting / contradicting IDs, reasons, violation codes, label, published text, core.
* `AuthoritativeStatistics` — copied from Phase 4 `SurgeAlert`s.
* `VerifiedRecommendation` (`verified-recommendation/1.0`) — published or withheld result
  with abstention state and publication decision.
* Tables: `evidence_records`, `evidence_bundles`, `verification_runs`,
  `verification_attempts`, `verification_claims`. JSON schemas: `trend-agent verify
  schema --which draft|recommendation|llm`.

## 4. Claim-verification workflow

```
draft ──> candidate claims: declared factual_claims + every factual sentence of the
          title, rationale, audience intent and uncertainty notes
     ──> per claim: reference problems -> entities (catalogue, subject, unknown names)
          -> support in CITED records (entities, numbers, dates, release wording,
             content coverage, salient words; measurements never support world claims)
          -> else support in other bundle records => re-cite (citation_mismatch)
          -> contradiction scan over the bundle (credible denials, dates, prices/specs,
             release state, conflicting credible sources)
          -> trend/search/statistics wording vs the engine (authority)
          -> entity states (unknown, ambiguous, rumoured, denied)
          -> freshness and novelty -> optional semantic check (downgrade only)
          -> attribution rules
     ──> status (VERIFIED | CORROBORATED | UNVERIFIED | CONTRADICTED)
         and action (KEEP | RECITE | KEEP_ATTRIBUTED | REPLACE_AUTHORITATIVE | REMOVE |
         NOT_FACTUAL)
```

## 5. Verification-gate integration

* `VerificationService.recommend(target_id, generator)`: read-only snapshot of Phase 2–4
  records → registration (application principal) → bundle → gate → persisted decision.
  `check(target_id, draft)` verifies an externally written draft in one attempt.
* The gate is the only producer of `VerifiedRecommendation`; Phase 5 (not started) should
  consume only that contract (or `verification_runs.output`) and must treat
  `publication = withhold` as "do not show".
* Retries: at most 3 generations; regeneration only for an unsupported title, a
  contradicted core claim or nothing left; stops at once when a rejected claim is
  generated again; refusals are never retried; malformed/truncated output is retried.
* Monitoring and detection are unchanged; verification reads their stored outputs.

## 6. Automated test results

| Check | Result |
|---|---|
| Full suite, Python 3.12, PostgreSQL test DB required (`TREND_AGENT_REQUIRE_DB_TESTS=1`) | **557 passed**, 3 deselected (opt-in live tests), 0 failed, 7 min 19 s; line+branch coverage 91 % (Phase 1–4 suite unchanged and passing) |
| Full suite, Python 3.13 | **557 passed**, 3 deselected, 0 failed (3 min 40 s) |
| Verification unit tests (offline) | 55 passed |
| Verification integration tests (database) | 6 passed |
| ruff check / ruff format --check / mypy --strict | clean |
| `alembic upgrade head` → `downgrade 0004` → `upgrade head`; `alembic check` | no drift |
| Labelled evaluation — held-out **first run** (independent) | 0 of 18 published labelled claims harmful; precision 1.00; false rejection 0.10; contradiction recall 0.60; state accuracy 0.88; publication accuracy 0.96; under-abstention 0.14; injection resistance 1.00 (1 case) |
| Labelled evaluation — final code, dev (tuned) / held-out (no longer independent) | 0 harmful published; precision 1.00 / 1.00; false rejection 0.00 / 0.00; contradiction recall 0.89 / 0.80; state and publication accuracy 1.00 / 1.00 |
| Unsupported-claim rate of the drafts before the gate | 0.47 (both splits) |
| Gate latency (8-record bundle, in-memory registry, 4 vCPU Xeon 2.1 GHz, under load) | ≈ 12 ms per recommendation (p50) |

Details and the per-case failures of the first held-out run:
[verification_evaluation.md](verification_evaluation.md). The fixes for those failures
are generic (name extraction, glue vocabulary, release dating, self-certification); after
them the held-out split is no longer an independent test.

We do **not** claim zero hallucinations: the cases are finite, fictional, English-only and
written by the same author as the verifier; the live LLM path was not exercised.

## 7. Known failure modes

Summarised from [verification_layer.md](verification_layer.md#known-failure-modes):
paraphrased correct claims can be rejected; claims with the right words in the wrong
relation (e.g. a missed negation) can be accepted; contradictions are only found when a
credible record states the conflicting fact explicitly (otherwise the claim is merely
removed); name detection is heuristic; trend vocabulary is finite; non-factual advice is
published unverified by design; trust depends on source configuration; corroboration
counts sources, not truth; context-word disambiguation can choose the wrong entity.

## 8. Residual risks

* Real-world accuracy unknown (fictional fixtures, no independent annotation).
* Live Claude behaviour (refusals, fallbacks, schema compliance) not observed; adapter
  tested against a fake client with the documented response shape only.
* Sources can be wrong: a false official announcement is "credible".
* Prompt injection is contained architecturally (no tools, read-only assembly, every fact
  verified), but an injected *true-looking* statement that matches credible evidence
  lexically could still be published; adaptive attacks were not evaluated.
* Single database role: least privilege is enforced per transaction and in application
  code, not by database grants.
* Sending evidence excerpts to an LLM provider may be restricted by source terms (see
  [compliance.md](compliance.md#verification-layer)); the default generator sends nothing.
* Development signing key is public; production requires a secret key; rotation
  invalidates IDs.
* The stray empty file `/smoke.py` at the container root (outside the repository) was
  created by a mistyped shell command during development; the removal was blocked by a
  safety check and left to the operator (it is not part of the repository).

## 9. Readiness for Phase 5

Ready to start Phase 5 on these terms:

* Phase 5 should consume `VerifiedRecommendation` / `verification_runs.output` (or call
  `VerificationService`) and show only `publish` / `publish_with_labels` results with
  their labels, citations and trend statement; it must never compute or restate
  statistics itself.
* Any new generator (e.g. content-gap ideas) must implement `RecommendationGenerator`
  and go through the gate; new factual fields need matching verification rules before
  they can be published.
* Open items worth doing early in Phase 5: a real (non-fictional) labelled evaluation set
  with independent annotators; a live, budgeted run of the Claude adapter on that set;
  separate database roles for read-only assembly; non-English support if needed.
