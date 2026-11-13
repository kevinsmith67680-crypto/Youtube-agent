# Search intent analysis

`intent_classifier.classify(query, entities, config, category)` returns an
`IntentClassification` deterministically; an optional LLM can only add suggestions.

## Taxonomy

Primary intents (10): `informational`, `tutorial`, `problem_solving`, `navigational`,
`comparison`, `review`, `demonstration`, `news`, `entertainment`, `gaming_guide`.

Sub-intents by category:

* **AI** — model_installation, ai_agent_tutorial, model_comparison, benchmark,
  api_integration, ai_automation, local_model_setup, tool_demonstration, model_pricing,
  release_explanation;
* **technology** — software_installation, product_review, hardware_benchmark,
  troubleshooting, programming_tutorial, version_migration, security_explanation,
  product_comparison, feature_demonstration;
* **gaming** — item_location, challenge_completion, event_guide, weapon_guide,
  game_update, game_settings, game_troubleshooting, strategy_guide,
  gameplay_demonstration, reward_unlock.

## Output

`primary_intent`, `sub_intent`, `secondary_intents` (+ sub), `entities` (name, catalog key,
type, version, verification state, evidence IDs, ambiguity candidates), `specificity`
(broad / moderate / specific), `ambiguity` notes, `confidence` (**HIGH / MEDIUM / LOW —
rule-based labels, not probabilities**), `supporting_evidence` (which rules fired on which
text), `method` (`deterministic_rules` or `llm_assisted`), `time_sensitive`,
`policy_flags`, `algorithm_version`.

## Method

1. **Category** — given, or inferred from resolved entities.
2. **Rules** — weighted regex rules per intent and sub-intent (`RULES`); sub-intents map to
   their primary; a fixed precedence breaks ties. No cue → `informational`, LOW confidence,
   with an ambiguity note.
3. **Entities** (`entity_verification.QueryEntityResolver`) — catalog names and aliases,
   versions (`CodeForge 2` vs family `CodeForge`), states from evidence:
   `confirmed` (credible record), `reported`, `rumored`, `unknown` (not in the catalog or
   evidence), `ambiguous` (homonyms, e.g. *Orbit* the AI model vs *Orbit* the glasses —
   resolved only when the category or context disambiguates), `rejected`, and `removed`
   (a credible record says the item was vaulted/removed).
4. **Item queries** — for item-location, reward and weapon guides the item phrase is
   extracted and verified separately; requirements that depend on it are *blocking*.
5. **Policy** — queries matching `policy.prohibited_patterns` (aimbots, "undetected" cheats,
   anti-cheat bypass, currency generators, account hacking, dupe exploits, exploit scripts)
   are flagged and never become opportunities (`blocked_policy`).

## Query decomposition

`query_decomposition.decompose(intent, entities)` lists *candidate* information
requirements from templates per sub-intent (installation, local setup, agent tutorial, API
integration, automation, pricing, release explanation, version migration, item location,
event guide, challenge, reward, weapon, game update, settings, strategy, gameplay) or per
primary intent. Each has a priority (essential / important / optional), coverage cues and
flags (`requires_verified_entity`, `blocking`).

* Requirements never contain product facts: "Where the item is found (Ember Lantern)" — not
  a location. Item existence and event identification are blocking for item guides.
* **Attribute focus** — informational/review queries naming an attribute
  ("nova lens battery life") get an essential "Information about battery life" requirement;
  the generic definition becomes optional.
* **LLM suggestions** (≤ 3) are accepted only if generic: no digits, no capitalised names
  outside the verified entities, at most *important* priority.

## LLM-assisted classification (optional)

`merge_llm_suggestions` may add secondary intents. It can choose a primary intent only when
the rules are LOW confidence *and* the suggestion is among the rule-plausible primaries. The
LLM's self-assessment is never used as confidence or ground truth.

## Known limitations

* Lexical rules miss paraphrases ("how do I get X working"), non-English queries and
  intents without cue words; such queries fall back to `informational` with LOW confidence.
* Held-out misses (see `phase5_evaluation.md`): "how to use CodeForge 2 in **vs** code"
  was read as a comparison ("vs" = versus); "CodeForge 2 not working fix" got
  `problem_solving` without the `troubleshooting` sub-intent (the AI category has no
  troubleshooting sub-intent); "Nova Lens battery life" was `informational`, labelled
  `review`; policy-blocked queries get whatever intent the rules produce (the block is what
  matters).
