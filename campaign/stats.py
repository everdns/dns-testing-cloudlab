"""Statistics for the campaign: noise, replicate policy, effects, designs.

Kept free of I/O so the phases, the local analysis (analysis/importance.py)
and the tests share one implementation.

  noise_sigma         pooled within-group standard deviation of replicates
  needs_replicates    the adaptive replicate rule (a result near the best
                      must be repeated before it can become the best)
  effect              difference of means with a bootstrap confidence interval
  fractional_factorial  two-level designs for up to 8 factors in 16 runs
  main_effects        contrasts of a two-level design
"""
import itertools
import math
import random
import statistics


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def noise_sigma(groups):
    """Pooled standard deviation over replicate groups ({key: [values]}).
    Groups with one value carry no information about noise. None if no group
    has two values."""
    num, dof = 0.0, 0
    for values in groups.values():
        values = [v for v in values if v is not None]
        if len(values) >= 2:
            num += statistics.variance(values) * (len(values) - 1)
            dof += len(values) - 1
    return math.sqrt(num / dof) if dof else None


def needs_replicates(value, best, sigma, have, want=3, k=2.0):
    """True if a result of ``value`` (with ``have`` replicates so far) should
    be measured again: it is within ``k`` sigma of ``best`` (or better) and
    has fewer than ``want`` replicates. Without a noise estimate, anything
    within 5% of the best is repeated."""
    if value is None or have >= want:
        return False
    if best is None:
        return True
    margin = k * sigma if sigma else 0.05 * best
    return value >= best - margin


def bootstrap_ci(a, b, n=2000, level=0.95, seed=0):
    """Bootstrap confidence interval for mean(a) - mean(b)."""
    a = [v for v in a if v is not None]
    b = [v for v in b if v is not None]
    if not a or not b:
        return None, None
    rng = random.Random(seed)
    diffs = sorted(mean(rng.choices(a, k=len(a))) - mean(rng.choices(b, k=len(b)))
                   for _ in range(n))
    lo = diffs[int((1 - level) / 2 * n)]
    hi = diffs[min(n - 1, int((1 + level) / 2 * n))]
    return lo, hi


def effect(with_values, without_values, reference=None, seed=0):
    """Effect of a change: mean(with) - mean(without), its 95% bootstrap CI,
    and the same as a percentage of ``reference`` (default mean(without))."""
    m_with, m_without = mean(with_values), mean(without_values)
    if m_with is None or m_without is None:
        return None
    lo, hi = bootstrap_ci(with_values, without_values, seed=seed)
    ref = reference or m_without
    pct = (lambda x: None if x is None or not ref else 100.0 * x / ref)
    diff = m_with - m_without
    return {"effect": diff, "ci": [lo, hi], "effect_pct": pct(diff),
            "ci_pct": [pct(lo), pct(hi)],
            "n_with": len([v for v in with_values if v is not None]),
            "n_without": len([v for v in without_values if v is not None]),
            "significant": lo is not None and (lo > 0 or hi < 0)}


# Generators of the 2^(k-p) resolution IV designs in 16 runs (base factors
# A..D as a full 2^4 factorial).
GENERATORS_16 = {"E": "ABC", "F": "BCD", "G": "ACD", "H": "ABD"}


def fractional_factorial(factors):
    """Two-level design for ``factors`` (names). Up to 4 factors: full
    factorial (2^k runs). 5 to 8 factors: 16 runs, resolution IV (main effects
    are not aliased with two-factor interactions). Returns a list of
    {factor: +1/-1} rows in standard order."""
    k = len(factors)
    if not 1 <= k <= 8:
        raise ValueError("fractional_factorial supports 1 to 8 factors")
    base = min(k, 4)
    letters = "ABCDEFGH"
    rows = []
    for levels in itertools.product((-1, 1), repeat=base):
        # Standard (Yates) order: the first factor alternates fastest.
        coded = dict(zip(letters[:base], reversed(levels)))
        for extra in letters[base:k]:
            value = 1
            for factor in GENERATORS_16[extra]:
                value *= coded[factor]
            coded[extra] = value
        rows.append({factors[i]: coded[letters[i]] for i in range(k)})
    return rows


def main_effects(design, responses):
    """{factor: mean(y | +1) - mean(y | -1)} for a two-level design and its
    responses (one list of values per run; replicates averaged)."""
    ys = [mean(r) for r in responses]
    out = {}
    for factor in design[0]:
        hi = [y for row, y in zip(design, ys) if row[factor] == 1 and y is not None]
        lo = [y for row, y in zip(design, ys) if row[factor] == -1 and y is not None]
        out[factor] = (mean(hi) - mean(lo)) if hi and lo else None
    return out


def interaction_effects(design, responses):
    """Two-factor interaction contrasts. In a resolution IV design they are
    aliased in pairs; the key lists every interaction sharing the contrast."""
    ys = [mean(r) for r in responses]
    factors = list(design[0])
    contrasts = {}
    for a, b in itertools.combinations(factors, 2):
        column = tuple(row[a] * row[b] for row in design)
        contrasts.setdefault(column, []).append(f"{a}*{b}")
    out = {}
    for column, names in contrasts.items():
        hi = [y for c, y in zip(column, ys) if c == 1 and y is not None]
        lo = [y for c, y in zip(column, ys) if c == -1 and y is not None]
        if hi and lo:
            out[" = ".join(names)] = mean(hi) - mean(lo)
    return out
