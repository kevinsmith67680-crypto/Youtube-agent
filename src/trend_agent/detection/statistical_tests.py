"""Small, vectorised statistical primitives used on hot paths.

They compute exactly the textbook quantities (checked against SciPy in the unit tests);
they exist because the generic SciPy/NumPy entry points carry per-call overhead that
dominates when called thousands of times on tiny arrays.

* ``nan_median_rows`` / ``nan_mad_rows`` - row-wise median and median absolute deviation
  ignoring NaN (sort once; NaN sorts last; pick the middle of the finite prefix).
* ``kruskal_wallis`` - Kruskal-Wallis H with the standard tie correction and its
  chi-square(k - 1) p-value (same as ``scipy.stats.kruskal``).
* ``logsumexp`` - numerically stable log(sum(exp(x))).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import numpy.typing as npt
from scipy import special
from scipy.stats import rankdata

Array = npt.NDArray[np.float64]


def _middle(sorted_rows: Array, counts: npt.NDArray[np.int64]) -> Array:
    rows = np.arange(sorted_rows.shape[0])
    lo = np.clip((counts - 1) // 2, 0, None)
    hi = np.clip(counts // 2, 0, None)
    hi = np.minimum(hi, sorted_rows.shape[1] - 1)
    out: Array = 0.5 * (sorted_rows[rows, lo] + sorted_rows[rows, hi])
    out[counts == 0] = np.nan
    return out


def nan_median_rows(windows: Array) -> tuple[Array, npt.NDArray[np.int64]]:
    """(median per row ignoring NaN, finite count per row)."""
    counts = np.count_nonzero(np.isfinite(windows), axis=1).astype(np.int64)
    if windows.shape[1] == 0:
        return np.full(windows.shape[0], np.nan), counts
    return _middle(np.sort(windows, axis=1), counts), counts


def nan_stats_rows(windows: Array) -> tuple[Array, Array, Array, Array]:
    """(count, median, MAD, mean absolute deviation) per row, NaN-aware."""
    med, counts = nan_median_rows(windows)
    dev = np.abs(windows - med[:, None])
    mad = _middle(np.sort(dev, axis=1), counts) if windows.shape[1] else med
    with np.errstate(invalid="ignore", divide="ignore"):
        mean_abs = np.where(counts > 0, np.nansum(dev, axis=1) / np.maximum(counts, 1), np.nan)
    return counts.astype(np.float64), med, mad, mean_abs


def kruskal_wallis(groups: Sequence[Array]) -> tuple[float, float]:
    """Kruskal-Wallis H statistic (tie-corrected) and p-value (chi-square, k-1 df)."""
    data = np.concatenate(groups)
    n = data.size
    ranks = rankdata(data)
    h = 0.0
    start = 0
    for g in groups:
        r = ranks[start : start + g.size]
        start += g.size
        h += g.size * (float(r.mean()) - (n + 1) / 2.0) ** 2
    h *= 12.0 / (n * (n + 1))
    _, ties = np.unique(data, return_counts=True)
    correction = 1.0 - float(np.sum(ties.astype(np.float64) ** 3 - ties)) / (n**3 - n)
    if correction <= 0:
        return 0.0, 1.0
    h /= correction
    p = float(special.chdtrc(len(groups) - 1, h))
    return float(h), p


def logsumexp(x: Array) -> float:
    m = float(np.max(x))
    if not np.isfinite(m):
        return m
    return m + float(np.log(np.sum(np.exp(x - m))))
