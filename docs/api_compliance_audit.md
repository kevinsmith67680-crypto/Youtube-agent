# API and data-source compliance audit (Phase 8)

> **Not legal advice, and not a compliance certification.** Automated tests passing does not
> establish compliance. The official policy pages could not be re-read during this audit:
> `developers.google.com` is not reachable from the build environment (DNS blocked, as in
> Phase 1). Every policy statement below therefore restates `docs/compliance.md` and **must be
> checked against the current official text by a person** before production use. Items that
> need a permission or review are disabled by default and listed in §4.

## 1. Sources in use and what is stored

| Source | Signal class | Access | Stored | Retention | Default |
|---|---|---|---|---|---|
| YouTube Data API v3 (`search.list`, `videos.list`, `channels.list`, `videoCategories.list`) | VIDEO_ACTIVITY | API key, public data only | titles, descriptions, channel names, IDs, publication times, search result positions | ≤ 30 days (configuration refuses > 30); refresh or delete | enabled only when a key is configured |
| YouTube statistics (views, likes, comments) | VIDEO_ACTIVITY | same | **not stored** by default; shown only | ≤ 30 days if enabled | `YOUTUBE_PERSIST_STATISTICS=false` |
| Google Trends API (official alpha) | EXTERNAL (web) / DIRECT (YouTube property) | operator admission + verified transport | nothing until a retention period is set | operator-defined | disabled |
| RSS/Atom feeds | OFFICIAL_EVENT_SIGNAL | public feeds | item title, link, date, summary excerpt | `discovery.yaml` per provider | enabled |
| GitHub Releases API | OFFICIAL_EVENT_SIGNAL | public API (optional token) | release name, tag, date, URL | per provider | enabled |
| Hacker News API | COMMUNITY_ACTIVITY | public API | item title, score, time, URL | per provider | enabled |
| Reddit Data API | COMMUNITY_ACTIVITY | OAuth app credentials | post title, score, time, permalink | per provider | disabled until credentials exist |
| Anthropic API (optional) | — (generation) | API key | prompts and outputs of generation runs (token counts, cost estimate) | with the recommendation | disabled unless configured |

Nothing is scraped. No undocumented endpoint is called. No rate limit is circumvented: there
is no proxy rotation, no CAPTCHA avoidance and no key pooling. Quotas are enforced locally
with a safety margin, and quota exhaustion stops requests. The bounded smoke test spends at
most one request per configured provider.

## 2. Derived metrics, aggregation and scoring

The system computes things from YouTube API data:

* coverage of a query's information needs;
* supply and freshness of existing videos;
* content-gap hypotheses;
* opportunity scores and recommendation ranks.

It also combines YouTube data with other sources (feeds, community APIs, the optional
Trends data). The system *labels* everything it derives, keeps each source separate, and
never presents a derived number as a YouTube metric. However:

> **Public visibility does not grant the right to store, aggregate or score.** Whether this
> project's derived metrics, cross-source combination and ranking are permitted for the
> operator's use case is a policy question that code cannot answer. The YouTube API
> Services Developer Policies contain rules on derived metrics, data aggregation and
> combination with other data. Some uses require a compliance review or audit by YouTube.

The software therefore treats a **completed compliance review as an external prerequisite**.
The readiness item `youtube_api_compliance_review` stays **BLOCKED** until the operator
records the outcome of that review:

```sql
INSERT INTO system_state (key, value, updated_at)
VALUES ('compliance_review', '{"approved": true, "by": "...", "scope": "..."}', now());
```

It is never inferred from tests.

## 3. Controls implemented

| Obligation (summary, to be confirmed) | Control | Where |
|---|---|---|
| Refresh or delete non-authorised API data within 30 days | expiry columns; `retention` stage runs daily in the scheduler; configuration refuses > 30 days | `ingestion/retention.py`; scheduler `retention` stage |
| Statistics not kept beyond the limit | not persisted by default; 30-day expiry when enabled | `config.py` |
| Show the data source / attribution | provider names on every series and recommendation; YouTube URLs link back | API views, dashboard |
| Do not present derived metrics as YouTube metrics | every derived value has a data class (OBSERVED / PROXY / FORECAST / SYNTHETIC / MISSING) and a definition; scores are labelled as research-priority indexes | `recommendations/views.py`, dashboard legend |
| Do not relabel web search as YouTube search | `measurement_type_for` maps only the YouTube property to DIRECT; the operating mode never unlocks direct claims from external data | `providers/google_trends.py`, `ops/modes.py` |
| Credentials kept confidential | `SecretStr`, redaction in logs and stored errors, never sent to the browser | `logging.py`, `ops/production.py`, E2E `s28` |
| Respect quotas | local accounting, margin, stop on exhaustion, `DAILY_API_BUDGET` across providers | `providers/quota.py`, `ops/budgets.py` |
| No automatic publishing | the system never uploads or publishes; outcomes are creator-entered and opt-in | `recommendation_outcomes` (consent required) |
| Owner analytics only with authorisation | the YouTube Analytics API is not called; outcome fields (views, search-traffic share) are entered by the creator, with `consent = true` enforced by the schema | migration 0009 |
| Personal data | single creator; no end-user accounts; no personal data collected beyond the creator's own entries | — |

## 4. Disabled by default until permission or review

1. `TREND_AGENT_YOUTUBE_PERSIST_STATISTICS=true`: needs a review of the statistics-storage rules.
2. Showing derived analytics to anyone other than the operator (multi-user or public
   dashboards): needs a YouTube API Services compliance review. The dashboard requires
   authentication and is a single-creator tool.
3. Google Trends: needs admission to the alpha programme, a verified transport for the
   contract Google supplies, and an operator-set retention period.
4. Reddit: needs OAuth app credentials and acceptance of the Reddit Data API terms.
   Commercial use requires a separate agreement with Reddit.
5. LLM generation: needs an Anthropic key. Prompts include public video metadata as
   delimited untrusted data; the operator should confirm this is consistent with the
   source terms.
6. YouTube quota above the default allocation: needs Google's audit and extension process.
   Never multiply keys or projects.

## 5. Verdict

* **Compliant by construction:** the technical controls above (retention, separation of
  sources, labelling, no scraping, quota enforcement, no auto-publishing, credential handling).
* **Unverified:** the policy texts themselves, which could not be re-read here, and whether
  the derived metrics and scoring are permitted for the operator's use case.
* **Status:** `youtube_api_compliance_review` = **BLOCKED** (external: human/legal review
  required).
