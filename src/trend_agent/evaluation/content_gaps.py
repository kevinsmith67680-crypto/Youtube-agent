"""Content-gap accuracy in four outcome classes (labelled fixtures, Phase 5 analyzer).

Predicted class, from each analysis report:

* VERIFIED_GAP           CANDIDATE opportunity whose primary gap is ``verified`` (every
                         factual premise verified, observation reproducible).
* CANDIDATE_GAP          CANDIDATE opportunity whose gap is a hypothesis (e.g. inferred
                         from metadata only).
* NO_VERIFIABLE_GAP      the analysis found no supported gap.
* INSUFFICIENT_EVIDENCE  the analysis abstained.
(BLOCKED_POLICY cases are scored separately.)

Labels say whether a gap exists (and which), that none exists, or that the evidence is
insufficient. They do not say whether a gap is *verified*: that distinction is therefore
reported as a distribution, and any VERIFIED_GAP on a "no gap" or "abstain" case is a
false verified gap - the worst error, required to be zero.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from trend_agent.content_analysis.config import load_content_config
from trend_agent.content_analysis.evaluation import SPLITS, load_split, run_case, score_case
from trend_agent.schemas.content_gap import GapVerification
from trend_agent.schemas.content_opportunity import OpportunityStatus
from trend_agent.verification.config import load_verification_config

CLASSES = (
    "VERIFIED_GAP",
    "CANDIDATE_GAP",
    "NO_VERIFIABLE_GAP",
    "INSUFFICIENT_EVIDENCE",
    "BLOCKED_POLICY",
)


def predicted_class(report: Any) -> str:
    st = report.status
    if st is OpportunityStatus.BLOCKED_POLICY:
        return "BLOCKED_POLICY"
    if st is OpportunityStatus.INSUFFICIENT_EVIDENCE:
        return "INSUFFICIENT_EVIDENCE"
    if st is OpportunityStatus.NO_OPPORTUNITY or report.primary_gap is None:
        return "NO_VERIFIABLE_GAP"
    if report.primary_gap.verification_status is GapVerification.VERIFIED:
        return "VERIFIED_GAP"
    return "CANDIDATE_GAP"


def label_class(label_gap: str) -> str:
    return {"none": "NO_GAP", "abstain": "INSUFFICIENT_EVIDENCE", "blocked": "BLOCKED_POLICY"}.get(
        label_gap, "GAP"
    )


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def evaluate(root: Path, *, splits: tuple[str, ...] = SPLITS) -> dict[str, Any]:
    loaded, vloaded = load_content_config(root), load_verification_config(root)
    out: dict[str, Any] = {"origin": "synthetic", "splits": {}}
    for split in splits:
        as_of, cases = load_split(root / "content_analysis" / f"eval_{split}.yaml")
        confusion: Counter[tuple[str, str]] = Counter()
        rows = []
        for case in cases:
            outcome, _ = run_case(case, as_of=as_of, loaded=loaded, vloaded=vloaded, root=root)
            row = score_case(case, outcome, as_of)
            pred = predicted_class(outcome.report)
            lab = label_class(row["label_gap"])
            confusion[(lab, pred)] += 1
            rows.append(
                {
                    "case": row["case"],
                    "label": lab,
                    "predicted": pred,
                    "gap_correct": row["gap_correct"],
                }
            )
        gap_labels = [r for r in rows if r["label"] == "GAP"]
        non_gap = [r for r in rows if r["label"] in ("NO_GAP", "INSUFFICIENT_EVIDENCE")]
        reported = [r for r in rows if r["predicted"] in ("VERIFIED_GAP", "CANDIDATE_GAP")]
        verified = [r for r in rows if r["predicted"] == "VERIFIED_GAP"]
        out["splits"][split] = {
            "cases": len(rows),
            "confusion": {f"{a}->{b}": n for (a, b), n in sorted(confusion.items())},
            "predicted_distribution": dict(Counter(r["predicted"] for r in rows)),
            "gap_precision": _ratio(sum(r["gap_correct"] for r in reported), len(reported)),
            "gap_recall": _ratio(sum(r["gap_correct"] for r in gap_labels), len(gap_labels)),
            "false_gap_rate": _ratio(
                sum(r["predicted"] in ("VERIFIED_GAP", "CANDIDATE_GAP") for r in non_gap),
                len(non_gap),
            ),
            "false_verified_gaps": sum(r["label"] != "GAP" for r in verified),
            "verified_gap_precision": _ratio(
                sum(r["gap_correct"] for r in verified), len(verified)
            ),
            "abstention_recall": _ratio(
                sum(
                    r["predicted"] == "INSUFFICIENT_EVIDENCE"
                    for r in rows
                    if r["label"] == "INSUFFICIENT_EVIDENCE"
                ),
                sum(r["label"] == "INSUFFICIENT_EVIDENCE" for r in rows),
            ),
            "verified_vs_candidate_correctness": "UNKNOWN (labels do not encode verification)",
            "rows": rows,
        }
    out["notice"] = (
        "Fictional cases labelled by the rules' author; the heldout split was not used to "
        "tune thresholds. These numbers describe behaviour on fixtures, not real-world accuracy."
    )
    return out
