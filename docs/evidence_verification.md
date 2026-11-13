# Evidence verification in Phase 5

Phase 5 reuses the anti-hallucination layer (`docs/verification_layer.md`): the evidence
registry (signed, versioned, immutable records; bundles), entity verification, the claim
verifier and the untrusted-content handling. This page describes how opportunity reports
use it.

## Report claims

Every sentence in an opportunity report is a `ReportClaim` with a kind and cited evidence:

| kind | produced by | check |
|---|---|---|
| `FACT` | application (entity/release facts) | full claim verifier: entities, dates, release wording, contradictions, staleness |
| `OBSERVATION` | application (counts) | every number must equal a fact of a cited `STATISTICAL_CALCULATION` record |
| `HYPOTHESIS` | application (gaps) | as observations; labelled as a hypothesis |
| `NARRATIVE` | optional LLM | claim verifier + overreach checks (below) |

Statuses: `VERIFIED`, `CORROBORATED`, `UNVERIFIED`, `CONTRADICTED`,
`INSUFFICIENT_EVIDENCE` (nothing cited). Only application code sets them; only `VERIFIED`
and `CORROBORATED` claims are published. Rejected claims stay in the report for audit with
their reasons.

## The gate (`content_analysis/verification.py`)

1. **Reference resolution** — IDs must be well formed, exist, belong to this analysis'
   bundle, pass signature and content-hash checks, not be expired or removed, and not be
   synthetic unless the analysis is. A fabricated or substituted ID fails here.
2. **Exact assertion** — numbers in observations/hypotheses must be reproduced from the
   cited calculations (the LLM never supplies statistics).
3. **Narrative checks** — rejected if it claims to have *watched* a video, or (at tier A)
   states what a video explains/omits without metadata hedging ("titles suggest …").
4. **Claim verifier** — entity state, release dates, denials, contradictions.
5. **Status and publication**, then the report's `verification_state`:
   `verified_result` (every published claim verified, none rejected, primary gap verified,
   confirmed direct YouTube search demand), `partially_verified`, `contradicted`,
   `insufficient_evidence` or `source_unavailable`.

`trend-agent analyze verify <opportunity>` repeats step 1 for a stored report *now*:
claims whose evidence has expired (e.g. the 30-day YouTube limit) or been removed are
reported as no longer valid.

## Prompt-injection defences

Retrieved text (titles, descriptions, news, catalog items) is untrusted data:

* sanitised and scanned for instruction-like signals (`verification.untrusted`); flagged
  records are stored with `injection_flags`;
* a flagged description is **not used** for relevance or coverage (title only), so
  instruction text cannot buy relevance;
* in an LLM prompt, records are encoded inside a nonce-delimited data block after the
  trusted instructions; the system prompt states that the block is data;
* the LLM output is parsed into a closed schema with no numeric, score, date or ranking
  fields; unknown fields are dropped and logged as violations;
* nothing the LLM or a source says can call tools, fetch URLs, read credentials or write to
  the database: the analysis has no such capability (`permissions.PermissionPolicy` keeps
  these capabilities away from `GENERATOR` and `SOURCE_CONTENT`);
* the status, score and gap decision are computed before and independently of the LLM;
  scenario H and `test_llm_and_injection.py` assert they do not change.

Regression tests: `tests/unit/content_analysis/test_llm_and_injection.py`,
`tests/integration/test_content_analysis.py::test_injected_description_cannot_change_the_outcome`,
scenario H (`config/content_analysis/scenarios.yaml`), held-out case H12.

These are layered controls, not a proof: regex signals miss paraphrased instructions, and
a determined source can still influence the lexical relevance of its own *title*.

## Controlled LLM layer

* Off by default (`llm.enabled: false`). Without a key or with it disabled, analyses run in
  **reduced-capability mode** — deterministic only, stated in every run's notes and in
  `analyze status`.
* One assist call per analysis (`max_attempts` 2 for malformed or transient failures;
  refusals and unavailability are final), structured output (`output_config.format`
  JSON schema), Pydantic re-validation, token and cost accounting per run.
* The model may suggest secondary intents, generic requirements, gap hypotheses and a
  summary. It may not invent search volumes, products, video contents, dates or citations,
  claim to have watched videos, or override verification — each of these is checked.
* The adapter has not been exercised against the live API in this environment (no key);
  it is tested with a fake client (ok, refusal, max_tokens, transient error).
