# Live-data readiness and operating modes

This document answers one question honestly: **what can this system measure with real data
today, and what may it therefore claim?**

## 1. Signal classes

Every provider is classified by what it measures (`ops/signals.py`), not by its name:

| Class | Meaning | Can support |
|---|---|---|
| DIRECT_YOUTUBE_SEARCH_INTEREST | relative interest in YouTube Search itself | confirmed YouTube Search surges, direct search forecasts |
| EXTERNAL_SEARCH_INTEREST | search interest on another property (for example Google Web) | "external search surge"; never YouTube demand |
| SEARCH_SUGGESTION_PRESENCE | the phrase appears in a suggestion system | presence only |
| VIDEO_ACTIVITY | video publication and attention (YouTube Data API) | supply analysis, content gaps, proxy attention |
| COMMUNITY_ACTIVITY | community discussion (Hacker News, Reddit) | proxy attention |
| OFFICIAL_EVENT_SIGNAL | releases, announcements, patches (feeds, GitHub) | event timing, proxy attention |
| SYNTHETIC_TEST_DATA | fictional data | demonstrations and tests only |

## 2. Provider readiness

`trend-agent system providers` combines three independent facts. None is inferred from
another.

1. **Configuration**: the provider's own status, computed without I/O (enabled, credentials
   present, access not restricted).
2. **Live verification**: the latest bounded smoke test (`trend-agent system smoke-test`, at
   most one request per available provider). A configured provider that has never answered a
   real request is `CONFIGURED_UNVERIFIED`, not `READY`. A failed check is `DEGRADED`.
3. **Approvals**: what the operator must hold (terms, admissions, reviews). These are listed,
   never assumed.

### Measured in the build environment (2026-11-14)

| Provider | Class | Readiness | Live check |
|---|---|---|---|
| youtube_data_api, youtube_discovery | VIDEO_ACTIVITY | NOT_CONFIGURED (no key) | not attempted |
| google_trends | EXTERNAL (DIRECT only with a verified YouTube-property transport) | DISABLED | not attempted |
| google_trends_discovery | SEARCH_SUGGESTION_PRESENCE | NOT_CONFIGURED | not attempted |
| feeds | OFFICIAL_EVENT_SIGNAL | live check **succeeded** (1 request, 231 ms) | ok |
| github_releases | OFFICIAL_EVENT_SIGNAL | live check failed: 403 from the sandbox proxy | DEGRADED here (a normal deployment reaches it) |
| hackernews | COMMUNITY_ACTIVITY | live check failed: network policy denied | DEGRADED here |
| reddit | COMMUNITY_ACTIVITY | NOT_CONFIGURED (OAuth credentials) | not attempted |
| mock | SYNTHETIC_TEST_DATA | SYNTHETIC_ONLY | — |

**No provider in this code base can supply DIRECT_YOUTUBE_SEARCH_INTEREST with real data.**
The only path is the Google Trends API alpha with the YouTube property. That requires
admission by Google, plus a transport implemented and verified against the contract Google
supplies. None of these exists here, and none can be created by code.

## 3. Operating modes

The mode is computed by `ops/modes.py`; it cannot be configured upward.

| Mode | Requires | Allowed (in addition to the synthetic demo) | Never allowed |
|---|---|---|---|
| FULL_SEARCH_INTELLIGENCE | a **live-verified** DIRECT provider **and** detection validated on real labelled surges (≥ 30 real labels and an adopted real-data experiment with precision ≥ 0.7 and recall ≥ 0.5 on the direct class) | everything, including confirmed-surge claims and direct-search forecasts | — |
| LIMITED_SEARCH_INTELLIGENCE | a usable EXTERNAL provider, or a DIRECT provider not yet validated | external search signals, proxy detection and forecasts, gap analysis (with VIDEO_ACTIVITY), recommendations, LLM, alerts | confirmed YouTube Search surge claims, direct-search forecasts |
| PROXY_OPPORTUNITY_RESEARCH | at least one usable proxy provider | proxy detection and forecasts, gap analysis (only with VIDEO_ACTIVITY), recommendations as research hypotheses, LLM, alerts | any search-demand claim |
| DEMONSTRATION | demo mode, or no usable real provider | synthetic demo, synthetic recommendations | any real-data capability |
| DEGRADED | one of the above plus a degradation | the base mode minus what the degradation withdraws | — |

Degradations and what they withdraw:

* **Database unreachable, or schema outdated:** everything except the demo.
* **LLM budget exhausted:** LLM generation; templates are used instead.
* **API budget exhausted:** real-data collection and gap analysis.
* **No live scheduler, or stale data:** creator alerts.
* **No live workers, or a failing provider:** reported, with no capability withdrawn.

Every mode change is recorded (`system_state.operating_mode`, with history) and raises an ops
alert. A lost direct provider is reported as "direct YouTube Search surge claims are
WITHDRAWN" (E2E `s07`). The dashboard shows the mode on every page.

### The mode of this deployment

`DEGRADED (base PROXY_OPPORTUNITY_RESEARCH)`:

* **Proxy signals usable:** OFFICIAL_EVENT_SIGNAL and COMMUNITY_ACTIVITY.
* **Degraded because:** no scheduler or worker is running and no real observation has been
  collected.
* **Direct claims:** disabled.

With a YouTube API key and a successful smoke test it would be
`PROXY_OPPORTUNITY_RESEARCH` with content-gap analysis enabled. It can never be FULL
without a direct provider and real validation.

## 4. What the original objective needs, and what exists

| Need | Status |
|---|---|
| Measure YouTube Search interest for a query over time | **Not available**: no legitimate provider. |
| Detect surges in YouTube Search demand | Detection works on any time series. On real data it can only run on proxy series, which are labelled as such. |
| Real-world detection accuracy | **UNKNOWN**: zero labelled real surges. |
| Proxy research (what is being released, discussed and published) | Available with a YouTube key plus feeds; one live feed source verified here. |

**The original objective (YouTube Search surge intelligence) is therefore not achieved**, and
the system is built to say so rather than to substitute proxy signals silently.

## 5. Proxy-intelligence mode, used honestly

In PROXY_OPPORTUNITY_RESEARCH:

* opportunities and recommendations carry the demand label "Potential demand: proxy signals
  only";
* scores apply a proxy discount;
* creator alerts state the proxy basis;
* nothing is phrased as "people are searching for X".

The data path is the same as in the higher modes, so a direct provider can be added later
without changing the stages; only the claims change.

## 6. Checklist to reach a higher mode

1. `TREND_AGENT_YOUTUBE_API_KEY`, then `trend-agent system smoke-test` → PROXY with gap
   analysis.
2. Google Trends alpha admission, plus a verified transport for the web property →
   LIMITED.
3. A verified transport for the YouTube property → LIMITED (direct, unvalidated).
4. At least 30 independently labelled real surges (`trend-agent evaluate labels import --origin
   real`), an evaluation on them meeting the floors, and the experiment recorded as adopted
   → FULL.
