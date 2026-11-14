"""Twitch attention detector (versioned, interpretable): ranking and observed-viewer change.

Inputs are stored observations only:

* rank points from Get Top Games polls (a game absent from a poll's top-N is *outside the
  top-N*, never "zero viewers": the endpoint returns positions, not viewer numbers);
* viewer points from Get Streams polls: the summed viewers of the live streams returned.

Rules
-----
* Growth needs two observations at least ``min_interval_minutes`` apart; rates use the
  actual elapsed time.
* The service passes only a game's own stream-list samples here: a game's share of the
  global top-streams sample changes as *other* games' streams enter and leave it.
* Viewer points are compared only when they were sampled the same way: same basis
  (a game's own stream list vs the global top-streams sample), same languages, same page cap
  and the same completeness (complete with complete; partial with partial, which compares
  "the top N streams" with "the top N streams"). Unknown samples are never compared.
* Sustained increases use the Phase 4 one-sided CUSUM primitive on standardised hourly
  log-changes, with Twitch-specific parameters and Twitch-specific labels. Nothing here
  produces a search-surge classification or an alert stage.

The attention score (0-100) is a weighted sum of bounded components, each stored with its
value, weight, contribution and basis. Weights are heuristic and configurable.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from trend_agent.detection import cusum
from trend_agent.detection.config import CusumConfig
from trend_agent.twitch.config import AttentionSettings
from trend_agent.twitch.models import SIGNAL_CLASS, SIGNAL_LABEL

VERSION = "twitch-attention-v1"
CAVEATS = (
    "Twitch viewers are concurrent viewers of live streams on another platform: external "
    "gaming attention, not YouTube search interest and not a number of searches.",
    "Viewer totals cover only the streams the API returned; a partial sample is not the "
    "game's total audience.",
    "Changes are measured only between comparable stored observations, over their actual "
    "interval. One observation is never growth.",
)


@dataclass(frozen=True)
class RankPoint:
    observed_at: datetime
    rank: int


@dataclass(frozen=True)
class ViewerPoint:
    observed_at: datetime
    viewers: int
    streams: int
    status: str  # complete | partial | unknown
    basis: str  # game_streams | top_streams
    cap: int  # page_size x pages_requested
    languages: tuple[str, ...] = ()


def comparable(a: ViewerPoint, b: ViewerPoint) -> tuple[bool, str]:
    """Can these two viewer observations be compared, and on what terms?"""
    if a.basis != b.basis:
        return False, "different sampling basis"
    if tuple(sorted(a.languages)) != tuple(sorted(b.languages)):
        return False, "different language filters"
    if a.cap != b.cap:
        return False, "different page caps"
    if "unknown" in (a.status, b.status):
        return False, "sample completeness unknown"
    if a.status != b.status:
        return False, "one complete and one partial sample"
    if a.status == "partial":
        return True, "both partial with the same cap (top-N streams vs top-N streams)"
    return True, "both complete samples"


@dataclass
class Attention:
    version: str
    state: str
    score: float
    components: dict[str, dict[str, Any]]
    rank: dict[str, Any]
    viewers: dict[str, Any]
    labels: list[str]
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "signal_class": SIGNAL_CLASS,
            "signal_label": SIGNAL_LABEL,
            "state": self.state,
            "score": self.score,
            "components": self.components,
            "rank": self.rank,
            "viewers": self.viewers,
            "labels": self.labels,
            "notes": self.notes,
            "caveats": list(CAVEATS),
        }


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def _hours(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3600


def _spaced[T: (RankPoint, ViewerPoint)](points: Sequence[T], min_gap: timedelta) -> list[T]:
    """Chronological points at least ``min_gap`` apart (latest kept; duplicates dropped)."""
    ordered = sorted(points, key=lambda p: p.observed_at)
    out: list[T] = []
    for p in reversed(ordered):
        if not out or out[-1].observed_at - p.observed_at >= min_gap:
            out.append(p)
    return list(reversed(out))


def _rank_features(
    ranks: Sequence[RankPoint],
    top_snapshots: Sequence[datetime],
    s: AttentionSettings,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "latest_rank": None,
        "in_latest_top": False,
        "positions_gained": None,
        "positions_gained_per_hour": None,
        "window_positions_gained": None,
        "interval_hours": None,
        "new_in_top": False,
        "status": "insufficient_observations",
    }
    snaps = sorted(set(top_snapshots))
    present = {p.observed_at: p.rank for p in ranks}
    if not snaps:
        return out
    latest = snaps[-1]
    out["in_latest_top"] = latest in present
    if latest not in present:
        out["status"] = "outside_top_n_in_latest_poll"
        return out
    out["latest_rank"] = present[latest]
    earlier = [
        t
        for t in snaps[:-1]
        if t in present and latest - t >= timedelta(minutes=s.min_interval_minutes)
    ]
    absent_before = [t for t in snaps[:-1] if t not in present]
    first_present = min(present)
    # newly in the top-N: present now, never present before in the window, and absent from
    # enough earlier polls that its absence is observed, not assumed
    if first_present == latest and len(absent_before) >= s.new_in_top_min_prior_snapshots:
        out["new_in_top"] = True
        out["status"] = "new_in_top_n"
    if earlier:
        base = earlier[-1]
        gained = present[base] - present[latest]  # positive = climbed
        hours = _hours(base, latest)
        out.update(
            positions_gained=gained,
            interval_hours=round(hours, 3),
            positions_gained_per_hour=round(gained / hours, 3) if hours > 0 else None,
            window_positions_gained=present[min(present)] - present[latest],
            status="ok",
        )
    return out


def _viewer_features(points: Sequence[ViewerPoint], s: AttentionSettings) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "insufficient_observations",
        "observations": len(points),
        "comparison": None,
        "latest_viewers": None,
        "latest_streams": None,
        "latest_sample_status": None,
        "sample_basis": None,
        "viewer_delta": None,
        "pct_change": None,
        "pct_per_hour": None,
        "stream_delta": None,
        "stream_pct_per_hour": None,
        "interval_hours": None,
        "acceleration": None,
        "volatility": None,
        "sustained": {"status": "insufficient_observations"},
        "window_pct_change": None,
    }
    if not points:
        return out
    ordered = sorted(points, key=lambda p: p.observed_at)
    latest = ordered[-1]
    out.update(
        latest_viewers=latest.viewers,
        latest_streams=latest.streams,
        latest_sample_status=latest.status,
        sample_basis=latest.basis,
    )
    same = [p for p in ordered if comparable(p, latest)[0]]
    if len(ordered) >= 2 and len(same) < 2:
        out["status"] = "incomparable_samples"
        out["comparison"] = comparable(ordered[-2], latest)[1]
        return out
    chain = _spaced(same, timedelta(minutes=s.min_interval_minutes))
    out["observations"] = len(chain)
    if len(chain) < 2:
        if len(same) >= 2:
            out["status"] = "interval_too_short"
        return out
    out["comparison"] = comparable(chain[-2], chain[-1])[1]
    rates: list[float] = []
    for a, b in itertools.pairwise(chain):
        h = _hours(a.observed_at, b.observed_at)
        if a.viewers > 0 and b.viewers > 0 and h > 0:
            rates.append(math.log(b.viewers / a.viewers) / h)
    a, b = chain[-2], chain[-1]
    h = _hours(a.observed_at, b.observed_at)
    out["interval_hours"] = round(h, 3)
    out["viewer_delta"] = b.viewers - a.viewers
    out["stream_delta"] = b.streams - a.streams
    if a.viewers > 0:
        pct = 100 * (b.viewers - a.viewers) / a.viewers
        out["pct_change"] = round(pct, 2)
        out["pct_per_hour"] = round(pct / h, 2)
    if a.streams > 0:
        out["stream_pct_per_hour"] = round(100 * (b.streams - a.streams) / a.streams / h, 2)
    first = chain[0]
    if first.viewers > 0:
        out["window_pct_change"] = round(100 * (b.viewers - first.viewers) / first.viewers, 2)
    if len(rates) >= 2:
        out["acceleration"] = round(rates[-1] - rates[-2], 4)  # change of hourly log-rate
    if len(rates) >= 3:
        out["volatility"] = round(float(np.std(rates, ddof=1)), 4)
    out["sustained"] = _sustained(rates, s)
    out["status"] = "ok"
    return out


def _sustained(rates: Sequence[float], s: AttentionSettings) -> dict[str, Any]:
    """Phase 4 CUSUM on hourly log-rates standardised by their robust spread (null: no
    growth). Needs ``min_intervals_for_sustained`` intervals."""
    if len(rates) < s.min_intervals_for_sustained:
        return {"status": "insufficient_observations", "intervals": len(rates)}
    arr = np.asarray(rates, dtype=float)
    mad = float(np.median(np.abs(arr - np.median(arr))))
    scale = max(1.4826 * mad, 0.05)  # floor: 5 %/h spread, so flat series do not alarm
    cfg = CusumConfig(
        k=s.cusum_k,
        h=s.cusum_h,
        z_clip=s.cusum_z_clip,
        max_gap_periods=0,
        refractory_periods=0,
    )
    state = cusum.CusumState()
    alarm = watch = False
    last = None
    for slot, r in enumerate(arr):
        last = cusum.update(state, slot, float(r) / scale, cfg)
        alarm = alarm or last.alarm
        watch = last.status.value == "watch"
    return {
        "status": "alarm" if alarm else "watch" if watch else "not_triggered",
        "intervals": len(rates),
        "statistic": None if last is None or last.statistic is None else round(last.statistic, 3),
        "threshold": cfg.h,
        "method": cusum.VERSION,
    }


def assess(
    ranks: Sequence[RankPoint],
    top_snapshots: Sequence[datetime],
    viewers: Sequence[ViewerPoint],
    settings: AttentionSettings,
) -> Attention:
    s = settings
    rk = _rank_features(ranks, top_snapshots, s)
    vw = _viewer_features(viewers, s)
    rising_pph = s.rising_pct_per_hour
    pph = vw["pct_per_hour"]
    gained = rk["window_positions_gained"]
    sustained = vw["sustained"].get("status")
    notes: list[str] = []
    if vw["status"] == "incomparable_samples":
        notes.append(f"viewer samples not comparable: {vw['comparison']}")
    if rk["status"] == "outside_top_n_in_latest_poll":
        notes.append("outside the polled top-N in the latest ranking (position unknown)")

    if sustained == "alarm" and pph is not None and pph > 0:
        state = "sustained_rise"
    elif (pph is not None and pph >= rising_pph) or (
        gained is not None and gained >= s.rank_climb_positions
    ):
        state = "rising"
    elif (pph is not None and pph <= s.falling_pct_per_hour) or (
        gained is not None and gained <= -s.rank_climb_positions
    ):
        state = "falling"
    elif vw["status"] == "ok" or rk["status"] == "ok":
        state = "steady"
    elif vw["status"] == "incomparable_samples":
        state = "incomparable_samples"
    else:
        state = "insufficient_observations"
    if rk["new_in_top"] and state in ("steady", "insufficient_observations"):
        state = "rising"
        notes.append("newly in the polled top-N ranking")

    w = s.weights
    accel = vw["acceleration"]
    vol = vw["volatility"]
    sp = vw["stream_pct_per_hour"]
    values: dict[str, tuple[float, float, str]] = {
        "rank_improvement": (
            w.rank_improvement,
            _clip((gained or 0) / (2 * s.rank_climb_positions)),
            f"{gained} positions over the window" if gained is not None else "no rank pair",
        ),
        "viewer_growth": (
            w.viewer_growth,
            _clip((pph or 0) / (2 * rising_pph)),
            f"{pph} %/h ({vw['comparison']})" if pph is not None else "not measured",
        ),
        "stream_growth": (
            w.stream_growth,
            _clip((sp or 0) / (2 * rising_pph)),
            f"{sp} %/h live streams" if sp is not None else "not measured",
        ),
        "acceleration": (
            w.acceleration,
            _clip((accel or 0) / math.log(1 + rising_pph / 100)),
            f"hourly log-rate change {accel}" if accel is not None else "needs 3 observations",
        ),
        "sustained": (
            w.sustained,
            {"alarm": 1.0, "watch": 0.5}.get(sustained or "", 0.0),
            f"CUSUM {sustained}",
        ),
        "new_in_top": (
            w.new_in_top,
            1.0 if rk["new_in_top"] else 0.0,
            "newly in the polled top-N" if rk["new_in_top"] else "not newly ranked",
        ),
    }
    total_w = sum(weight for weight, _, _ in values.values()) or 1.0
    components: dict[str, dict[str, Any]] = {
        name: {
            "value": round(x, 3),
            "weight": weight,
            "contribution": round(100 * weight * x / total_w, 2),
            "basis": basis,
        }
        for name, (weight, x, basis) in values.items()
    }
    penalty = _clip((vol or 0) / 1.0)
    components["volatility_penalty"] = {
        "value": round(penalty, 3),
        "weight": w.volatility_penalty,
        "contribution": -round(100 * w.volatility_penalty * penalty / total_w, 2),
        "basis": f"std of hourly log-rates {vol}" if vol is not None else "needs 3 observations",
    }
    score: float = sum(float(c["contribution"]) for c in components.values())
    if state in ("insufficient_observations", "incomparable_samples"):
        score = 0.0  # nothing measured: no score, rather than a guess
    labels = ["TWITCH_ACTIVITY"]
    if rk["status"] in ("ok", "new_in_top_n"):
        labels.append("RANK_CHANGE")
    if vw["latest_viewers"] is not None:
        labels.append("OBSERVED_VIEWERS")
    if vw["latest_sample_status"] == "partial":
        labels.append("PARTIAL_SAMPLE")
    return Attention(
        version=VERSION,
        state=state,
        score=round(max(0.0, min(100.0, score)), 2),
        components=components,
        rank=rk,
        viewers=vw,
        labels=labels,
        notes=notes,
    )
