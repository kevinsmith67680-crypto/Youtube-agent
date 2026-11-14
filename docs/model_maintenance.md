# Model maintenance

Forecast models (Phase 6) are versioned, checksummed artifacts with an explicit lifecycle.
Nothing is promoted automatically.

## 1. Statuses

| Status | Meaning | Served? |
|---|---|---|
| EXPERIMENTAL | trained, not validated | no |
| VALIDATED | passed the acceptance checks on its own scope | no (eligible for promotion) |
| CHALLENGER | validated and under comparison with the champion | no |
| CHAMPION | the one model serving its scope (category-specific first, then pooled) | **yes** |
| RETIRED | replaced, or taken out of service without fault | no |
| REJECTED | **new in Phase 8**: failed validation, or rolled back for a fault; never promoted again | no |

Every change is an event in `forecast_model_events` (from, to, reason, evidence, time).

## 2. Retraining and promotion

* **Training never promotes.** `trend-agent forecast train` trains and registers models
  only.
* **Promotion is explicit.** `trend-agent forecast promote` makes a model champion only if
  there is no champion yet and it passed the acceptance criteria, or it beats the current
  champion on the *same* held-out rows under the comparison rules (`forecast compare`).
* **No automatic promotion of retrained models.** The scheduler runs predictions only; it
  never trains or promotes.
* **LightGBM only with a meaningful gain.** LightGBM is promoted only when it passes the same
  acceptance checks and beats the simpler baselines (`forecast_validation.md` §1).

## 3. Drift

`trend-agent forecast drift` compares recent feature and outcome distributions with the
training period (Phase 6 `forecasting/drift.py`). Drift is reported, not acted on
automatically. The response is a retraining run, followed by an explicit comparison and
promotion decision.

## 4. Rollback

```
trend-agent forecast rollback MODEL_ID                       # retire; scope -> baseline-only
trend-agent forecast rollback MODEL_ID --reject              # mark REJECTED (a fault)
trend-agent forecast rollback MODEL_ID --reject --restore-previous --reason "drift since 2026-11-01"
trend-agent forecast reject MODEL_ID --reason "failed held-out validation"
```

`--restore-previous` reinstates the champion that `MODEL_ID` replaced. It runs only after
that model's artifact passes its checksum check; if the artifact is missing or altered,
nothing changes. Without a previous champion, the scope falls back to baseline-only
forecasts.

Tests in `tests/unit/forecasting/test_registry.py` cover:

* rejected models can never be promoted;
* a champion cannot be "rejected" without a rollback;
* restoration works;
* the scope falls back when there is no previous champion;
* a tampered artifact blocks the rollback, and the current champion stays in service.

## 5. Experiments

Evaluations write to `experiments` with:

* the hypothesis;
* the configuration;
* the dataset identity (version and hash);
* the data origin;
* the split;
* the metrics and baseline metrics;
* the decision (`adopted | rejected | inconclusive | recorded`) and its reason;
* the code version.

The detection tuning loop only *proposes*; applying a change is a configuration edit with a
new `threshold_version`. No LLM changes statistical thresholds.

## 6. Real-data models

Every model in this deployment has data origin `synthetic`. A real model needs real
observations: in this deployment those would be proxy observations, so the model would
forecast proxy attention, not search demand. It also needs its own validation. Synthetic
and real models are never mixed: `champion()` is scoped by data origin.
