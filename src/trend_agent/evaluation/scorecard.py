"""The quality scorecard: offline (synthetic / fixture) results against targets, and the
real-world status of each component - UNKNOWN unless enough real labels exist."""

from __future__ import annotations

from typing import Any

# Offline targets. They describe acceptable behaviour on synthetic data and fixtures; they
# are NOT real-world accuracy targets and are never reported as such.
OFFLINE_TARGETS: dict[str, dict[str, tuple[str, float]]] = {
    "detection": {
        "precision": (">=", 0.6),
        "recall": (">=", 0.6),
        "false_alerts_per_1000_series_day": ("<=", 5.0),
    },
    "forecasting": {"beats_best_baseline_share": (">=", 0.5)},
    "content_gaps": {"false_gap_rate": ("<=", 0.25), "false_verified_gaps": ("<=", 0.0)},
    "recommendations": {
        "grounding_violation_rate": ("<=", 0.0),
        "duplicate_f1": (">=", 0.8),
        "false_approvals": ("<=", 0.0),
    },
}


def _check(value: Any, op: str, target: float) -> str:
    if value is None:
        return "NOT_MEASURED"
    ok = value >= target if op == ">=" else value <= target
    return "MEETS_TARGET" if ok else "BELOW_TARGET"


def offline_metrics(results: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    d = results.get("detection")
    if d:
        t = d["current"]["test"]
        out["detection"] = {k: t.get(k) for k in OFFLINE_TARGETS["detection"]}
    f = results.get("forecasting")
    if f:
        out["forecasting"] = {
            "beats_best_baseline_share": f["summary"]["beats_best_baseline_share"]
        }
    g = results.get("content_gaps")
    if g:
        h = g["splits"].get("heldout", {})
        out["content_gaps"] = {
            "false_gap_rate": h.get("false_gap_rate"),
            "false_verified_gaps": h.get("false_verified_gaps"),
        }
    r = results.get("recommendations")
    if r:
        out["recommendations"] = {
            "grounding_violation_rate": r.get("grounding_violation_rate"),
            "duplicate_f1": r.get("duplicate_f1"),
            "false_approvals": r["heldout"].get("false_approvals"),
        }
    return out


def build(results: dict[str, Any], real: dict[str, Any]) -> dict[str, Any]:
    offline = offline_metrics(results)
    components: dict[str, dict[str, Any]] = {}
    for comp, targets in OFFLINE_TARGETS.items():
        vals = offline.get(comp)
        if vals is None:
            components[comp] = {"offline": "NOT_RUN", "checks": {}}
        else:
            checks = {
                k: {
                    "value": vals.get(k),
                    "target": f"{op} {t}",
                    "result": _check(vals.get(k), op, t),
                }
                for k, (op, t) in targets.items()
            }
            results_set = {c["result"] for c in checks.values()}
            status = (
                "BELOW_TARGET"
                if "BELOW_TARGET" in results_set
                else "NOT_MEASURED"
                if "NOT_MEASURED" in results_set
                else "MEETS_TARGET"
            )
            components[comp] = {"offline": status, "checks": checks}
        components[comp]["real_world"] = real.get(
            comp, {"status": "UNKNOWN", "reason": "no real labels"}
        )
    integrity = results.get("integrity")
    return {
        "components": components,
        "pipeline_integrity": integrity["status"] if integrity else "NOT_RUN",
        "real_world_accuracy": (
            "UNKNOWN"
            if all(c["real_world"].get("status") != "MEASURED" for c in components.values())
            else "PARTIAL"
        ),
        "notice": "Offline results are measured on synthetic data and author-labelled "
        "fixtures. They show that the pipeline behaves as designed; they are not evidence of "
        "real-world accuracy, which stays UNKNOWN until enough real labels are collected.",
    }
