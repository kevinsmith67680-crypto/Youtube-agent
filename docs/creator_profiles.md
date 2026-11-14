# Creator profiles

The profile personalises which opportunities and formats are proposed and how feasible an
idea is for this creator. It contains **only what the creator states**. Nothing about the
channel's performance is inferred, and equipment or skills are never assumed: when the
profile does not state them, the answer shown is "unknown".

Edit it in the dashboard (Settings → Creator profile) or with `PUT /api/profile`. The
default profile covers AI, technology and gaming, regions GB and US, English, and the style
"practical demonstrations, tutorials and experiments". It is stored in `creator_profiles`
(one default profile per deployment; `profile_id` is part of every recommendation ID, so a
changed profile produces new ideas instead of overwriting old ones).

## Fields and how they are used

| Field | Validation | Used by |
|---|---|---|
| `preferred_categories` | subset of ai, technology, gaming | opportunities outside them are skipped (stated reason); creator-relevance component (1.0 vs 0.3) |
| `preferred_formats` | any of the 30 formats | format selection bonus (+0.5) and "your preferred format" label; creator relevance (30% blend) |
| `topic_preferences` | ≤ 30 entries of ≤ 80 chars | creator relevance (20% blend when a preference matches the query) |
| `content_exclusions` | ≤ 30 entries | an opportunity whose query contains an exclusion is skipped |
| `available_hours_per_week` | 0–168 | feasibility: "fits your capacity" yes/no (unknown if empty); production-feasibility component halved when it does not fit |
| `skills`, `equipment` | ≤ 30 entries | feasibility: "you have the requirements" yes/no against the format's software and hardware (unknown if both empty) |
| `production_style` | ≤ 200 chars | given to the optional LLM generator as context |
| `preferred_duration_minutes` | 1–240 | given to the optional LLM generator as context |
| `notification_preferences` | keys in_app, email, telegram | which alert channels deliver (email/Telegram also need server credentials) |
| `display_name`, `channel_focus`, `primary_regions`, `language`, `production_capacity_videos_per_week`, `channel_size`, `recommendation_quantity`, `llm_daily_token_budget` | validated | **stored and shown, not used by the engine in this phase**; the server-side `llm.daily_token_budget` is the budget that applies |

## Production profiles (planning assumptions)

| Profile | Hours | Typical formats |
|---|---|---|
| QUICK_RESPONSE | 2 | news-style explanations, short event guides |
| STANDARD_TUTORIAL | 8 | installation, migration and item-location guides |
| DEEP_TECHNICAL_VIDEO | 24 | benchmarks, experiments, builds, comparisons |

Hours are configured in `config/recommendations.yaml → feasibility.profiles` and are
planning assumptions, never guarantees. Each profile maps to the matching Phase 6
opportunity window (quick reaction, standard tutorial, deep dive) for urgency.

## Privacy

The profile is private data: the API returns it only with the token. No personal analytics
are collected or inferred; feedback and production status are recorded only when the
creator acts.
