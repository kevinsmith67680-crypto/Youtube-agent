# Historical replay

Back-testing a detector on stored history is only honest if, at every simulated moment,
the detector sees exactly what was available then. Replay (`monitoring/replay.py`,
`trend-agent monitor replay`) enforces that.

## Availability rule

Each stored observation **revision** becomes available at:

| Mode | Availability time |
|---|---|
| `publication` (default) | `provider_published_at` when the provider stated it; otherwise the conservative bound `max(collected_at, ingested_at)` |
| `agent` | always `max(collected_at, ingested_at)` — what this system actually knew (backfilled history appears when it was backfilled) |

Earlier availability is never inferred without evidence. `first_available_at` is shown
for context but not used to bring availability forward.

**Example** (Scenario G): Monday's value is published Wednesday 12:00 UTC. A replay step
at Tuesday 12:00 does not contain it, and neither does a step at Wednesday 00:00. A step at
Wednesday 12:00 or later does. The step summary counts it as
`withheld_not_yet_available` until then.

## Revisions, conflicts and late data

* A revision replaces the value it supersedes **only from its own availability time**.
  Before that, the earlier revision is what was known (Scenario J: the 2026-03-03 value
  is 47.29 in a snapshot on 6 March and 54.79 from 7 March 06:00).
* Conflict rows (values contradicting an immutable source) never become visible values.
* Late-arriving data simply appears at its availability time; `late_arrivals` counts
  events available more than two days after their window ended.

## Determinism

Events are ordered by (availability time, series, window, revision, id). Each step has a
digest over the visible state (keyed by stable series identity, not database ids), so the
same stored data and parameters always produce the same steps and final digest, on any
database. The demo checks this.

## Interface for Phase 4

Phase 4 implements this interface in `detection/replay_adapter.py::DetectionReplayer`
(`trend-agent detect replay`): the full detection engine, alert policy and episode rules
run on each snapshot. See [detection_evaluation.md](detection_evaluation.md).


```python
from trend_agent.monitoring.replay import ReplayConsumer, ReplaySnapshot, replay

class MyDetector:                      # any custom consumer; Phase 4 ships DetectionReplayer
    def on_step(self, snapshot: ReplaySnapshot) -> None:
        for series_id, points in snapshot.series.items():   # one scale per series
            ...                                               # points sorted by event time

points = TimeSeriesRepository(session).points(target_id=..., current_only=False)
report = replay(points, start=..., end=..., step=timedelta(days=1),
                mode=AvailabilityMode.PUBLICATION, consumers=[MyDetector()])
```

`ReplaySnapshot` carries `as_of`, the visible series (each a single comparable scale; use
`comparable_groups`/`assess_pair` before comparing values across series),
`newly_available`, `revisions_applied` and `withheld_not_yet_available`. `ReplayReport`
(JSON via `as_dict()`) records mode, range, step, event and series counts, availability
basis counts, late arrivals, per-step summaries and the final digest. That is the
reproducible record of a back-test run.

## CLI

```bash
trend-agent monitor replay --query "AI coding agents" --provider mock
trend-agent monitor replay --query "AI coding agents" --start 2026-03-03T12:00:00 \
    --end 2026-03-08T12:00:00 --step-hours 24 --mode publication --json
```
