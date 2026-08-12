"""
risk_bounds_theory.py
=====================
Reproduces the information-theoretic results of SELENE
(Section "Information-Theoretic Limits on Oracle Reliability"):

    * Table VII  -- fundamental lower bounds on oracle Bayesian risk
                    as a function of the observed evidence set
    * Figure 5   -- (a) evidence sufficiency, (b) privacy frontier
    * the risk-gating thresholds quoted in Section V-F

Everything is computed analytically from the CPT parameters anchored in
CPTStore (Table IV) -- no chain interaction and no experimental data are
required, so any reader can verify the published numbers from ledger
state alone.

Theory
------
Casting oracle operation as Bayesian estimation with 0-1 loss and
applying the information-measure bounds of Esposito, Vandenbroucque and
Gastpar (JMLR 25(340):1-45, 2024):

  Theorem 3 (maximal leakage)
      R_B >= 1 - p_max * exp(L(W -> X_S))
      L(W -> X_S) = log sum_x max_w P(x | w)

  Theorem 4 (chi-squared / Hellinger p=2)
      R_B >= 1 - sqrt( p_max * (chi2(W, X_S) + 1) )

  Proposition 1 (privatised evidence, SDPI)
      Each evidence bit passed through BSC(lambda) before commitment.
      eta_p(K) = (1 - 2*lambda)^2  for 1 <= p <= 2, and tensorises, so
      R_B^priv >= 1 - sqrt( p_max * ((1-2*lambda)^2 * chi2(W,X_S) + 1) )
      BSC(lambda) is (eps, 0)-LDP with eps = log((1-lambda)/lambda).

Usage
-----
    python risk_bounds_theory.py                 # tables to stdout
    python risk_bounds_theory.py --figure out.pdf  # also write Figure 5

Requires: matplotlib + numpy only for --figure; core results are stdlib.
"""

from __future__ import annotations

import argparse
import itertools
import math
from typing import Dict, Iterable, List, Sequence, Tuple

# ---------------------------------------------------------------------------
# Model parameters -- Table IV (asymmetric CPTs) and the on-chain priors.
# Keys are the parent configuration (PPH, PPR); values are
# P(E_i = 1 | PPH, PPR).
# ---------------------------------------------------------------------------

CPT: Dict[str, Dict[Tuple[int, int], float]] = {
    "GPS": {(1, 0): 0.90, (0, 1): 0.15, (1, 1): 0.80, (0, 0): 0.10},
    "PC":  {(1, 0): 0.85, (0, 1): 0.20, (1, 1): 0.75, (0, 0): 0.15},
    "PMD": {(1, 0): 0.88, (0, 1): 0.10, (1, 1): 0.78, (0, 0): 0.08},
    "PR":  {(1, 0): 0.80, (0, 1): 0.25, (1, 1): 0.70, (0, 0): 0.20},
}

EVIDENCE_NAMES: Tuple[str, ...] = ("GPS", "PC", "PMD", "PR")

PRIOR_PPH = 0.30
PRIOR_PPR = 0.70


# ---------------------------------------------------------------------------
# Channel and prior
# ---------------------------------------------------------------------------

def prior_W() -> Dict[Tuple[int, int], float]:
    """Joint prior over W = (PPH, PPR); roots are marginally independent."""
    return {
        (h, r): (PRIOR_PPH if h else 1 - PRIOR_PPH)
                * (PRIOR_PPR if r else 1 - PRIOR_PPR)
        for h in (0, 1) for r in (0, 1)
    }


def px_given_w(
    x: Sequence[int],
    w: Tuple[int, int],
    subset: Sequence[str],
) -> float:
    """
    P(X_S = x | W = w) under the conditional-independence factorisation
    of Equation (1), restricted to the observed subset S.
    """
    p = 1.0
    for name, val in zip(subset, x):
        p1 = CPT[name][w]
        p *= p1 if val == 1 else (1.0 - p1)
    return p


def maximal_leakage(
    subset: Sequence[str],
    W_space: Iterable[Tuple[int, int]],
) -> Tuple[float, float]:
    """
    Maximal leakage L(W -> X_S) for discrete alphabets:
        L = log sum_x max_w P(x | w)
    Returns (L, exp(L)).
    """
    total = 0.0
    for x in itertools.product((0, 1), repeat=len(subset)):
        total += max(px_given_w(x, w, subset) for w in W_space)
    return math.log(total), total


def chi2_plus_one(
    subset: Sequence[str],
    W_space: Sequence[Tuple[int, int]],
    PW: Dict[Tuple[int, int], float],
) -> float:
    """
    chi^2(W, X_S) + 1  =  E_{P_W P_X}[ (dP_WX / dP_W P_X)^2 ]
                       =  sum_x sum_w P(w) P(x) (P(x|w)/P(x))^2
    """
    total = 0.0
    for x in itertools.product((0, 1), repeat=len(subset)):
        px = sum(PW[w] * px_given_w(x, w, subset) for w in W_space)
        if px <= 0.0:
            continue
        for w in W_space:
            total += PW[w] * px * (px_given_w(x, w, subset) / px) ** 2
    return total


# ---------------------------------------------------------------------------
# Risk bounds
# ---------------------------------------------------------------------------

def bound_maximal_leakage(expL: float, p_max: float) -> float:
    """Theorem 3, evaluated at rho = 1 (0-1 loss)."""
    return max(0.0, 1.0 - p_max * expL)


def bound_chi2(c2_plus_1: float, p_max: float) -> float:
    """Theorem 4, evaluated at rho = 1 (0-1 loss)."""
    return max(0.0, 1.0 - math.sqrt(p_max * c2_plus_1))


def bound_privatised(chi2: float, p_max: float, lam: float) -> float:
    """Proposition 1: BSC(lambda) privatisation, SDPI contraction."""
    eta = (1.0 - 2.0 * lam) ** 2
    return max(0.0, 1.0 - math.sqrt(p_max * (eta * chi2 + 1.0)))


def ldp_epsilon(lam: float) -> float:
    """BSC(lambda) is (eps, 0)-LDP with eps = log((1-lambda)/lambda)."""
    if lam <= 0.0:
        return float("inf")
    if lam >= 0.5:
        return 0.0
    return math.log((1.0 - lam) / lam)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def most_informative_by_size(
    W_space: Sequence[Tuple[int, int]],
    PW: Dict[Tuple[int, int], float],
    p_max: float,
) -> List[dict]:
    """
    For each |S| = 0..4, find the subset giving the SMALLEST chi^2 bound,
    i.e. the most informative evidence set of that size. This is the set a
    risk-gated oracle would target (Corollary 1).
    """
    rows: List[dict] = []
    for k in range(len(EVIDENCE_NAMES) + 1):
        best = None
        for subset in itertools.combinations(EVIDENCE_NAMES, k):
            c2 = chi2_plus_one(subset, W_space, PW)
            b_chi2 = bound_chi2(c2, p_max)
            if best is None or b_chi2 < best["chi2_bound"]:
                L, expL = maximal_leakage(subset, W_space)
                best = {
                    "k": k, "subset": subset, "L": L,
                    "ml_bound": bound_maximal_leakage(expL, p_max),
                    "chi2_bound": b_chi2, "chi2": c2 - 1.0,
                }
        rows.append(best)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--figure", metavar="PATH",
                    help="also write Figure 5 to PATH (PDF)")
    args = ap.parse_args()

    PW = prior_W()
    W_space = list(PW.keys())
    p_max = max(PW.values())

    print("=" * 72)
    print("SELENE -- fundamental limits on oracle reliability")
    print("=" * 72)
    print(f"  prior over W=(PPH,PPR): "
          f"{ {w: round(p, 4) for w, p in PW.items()} }")
    print(f"  p_max = {p_max:.4f}  (attained at "
          f"{max(PW, key=PW.get)})")
    print(f"  no-evidence baseline 1 - sqrt(p_max) = "
          f"{1 - math.sqrt(p_max):.4f}")
    print()

    rows = most_informative_by_size(W_space, PW, p_max)

    # ---- Table VII ----
    print("TABLE VII -- lower bounds on oracle Bayesian risk")
    print(f"  {'|S|':>3}  {'most informative subset':<24} "
          f"{'L(W->X)':>9} {'ML bd.':>9} {'chi2 bd.':>9}")
    print("  " + "-" * 60)
    for r in rows:
        subset = ",".join(r["subset"]) if r["subset"] else "(none)"
        ml = "---" if r["ml_bound"] <= 0.0 else f"{r['ml_bound']:.3f}"
        print(f"  {r['k']:>3}  {subset:<24} {r['L']:>9.3f} "
              f"{ml:>9} {r['chi2_bound']:>9.3f}")
    print()

    # ---- risk gating (Corollary 1) ----
    print("RISK-GATED EXECUTION (Corollary 1)")
    print("  minimum |S| such that the certified bound <= tau_risk")
    for tau in (0.30, 0.20, 0.15, 0.10, 0.075, 0.06, 0.05):
        need = next((r for r in rows if r["chi2_bound"] <= tau), None)
        if need is None:
            print(f"    tau_risk = {tau:<6} -> UNATTAINABLE "
                  f"with these four evidence nodes")
        else:
            print(f"    tau_risk = {tau:<6} -> |S| >= {need['k']}  "
                  f"(bound {need['chi2_bound']:.4f}, "
                  f"{','.join(need['subset']) or 'none'})")
    print()

    # ---- privacy frontier (Proposition 1) ----
    chi2_full = rows[-1]["chi2"]
    print("PRIVACY-RELIABILITY FRONTIER (Proposition 1)")
    print(f"  chi2(W, X) on the full evidence set = {chi2_full:.6f}")
    print(f"  {'lambda':>8} {'LDP eps':>10} {'eta':>8} "
          f"{'risk bound':>11} {'max accuracy':>13}")
    print("  " + "-" * 54)
    for lam in (0.0, 0.05, 0.10, 0.15, 0.20, 0.25,
                0.30, 0.35, 0.40, 0.45, 0.50):
        rb = bound_privatised(chi2_full, p_max, lam)
        eps = ldp_epsilon(lam)
        eps_s = "inf" if math.isinf(eps) else f"{eps:.3f}"
        print(f"  {lam:>8.2f} {eps_s:>10} {(1-2*lam)**2:>8.4f} "
              f"{rb:>11.4f} {1-rb:>13.4f}")

    if args.figure:
        _write_figure(args.figure, W_space, PW, p_max, chi2_full)
        print(f"\n  Figure written to {args.figure}")


def _write_figure(path, W_space, PW, p_max, chi2_full) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    ks, lo, hi, ml = [], [], [], []
    for k in range(len(EVIDENCE_NAMES) + 1):
        c = [bound_chi2(chi2_plus_one(s, W_space, PW), p_max)
             for s in itertools.combinations(EVIDENCE_NAMES, k)]
        m = [bound_maximal_leakage(maximal_leakage(s, W_space)[1], p_max)
             for s in itertools.combinations(EVIDENCE_NAMES, k)]
        ks.append(k); lo.append(min(c)); hi.append(max(c)); ml.append(min(m))

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(9.0, 3.3))
    a1.fill_between(ks, lo, hi, alpha=0.18, color="#1f4e79",
                    label="range over subsets")
    a1.plot(ks, lo, "o-", color="#1f4e79", lw=2, ms=7,
            label=r"$\chi^2$ bound (most informative)")
    a1.plot(ks, ml, "s--", color="#c0392b", lw=1.8, ms=6,
            label="maximal-leakage bound")
    a1.axhline(0.10, color="gray", ls=":", lw=1.2)
    a1.annotate(r"$\tau_{\mathrm{risk}}=0.10$", (0.05, 0.115),
                fontsize=7.5, color="gray")
    a1.set_xlabel(r"observed evidence variables $|S|$", fontsize=10)
    a1.set_ylabel(r"lower bound on Bayesian risk $R_B$", fontsize=10)
    a1.set_xticks(ks); a1.grid(alpha=0.25); a1.set_ylim(-0.02, 0.55)
    a1.legend(fontsize=7.5, loc="upper right")
    a1.set_title("(a) Evidence sufficiency", fontsize=10)

    lams = np.linspace(0, 0.5, 200)
    rb = [bound_privatised(chi2_full, p_max, l) for l in lams]
    a2.plot(lams, rb, "-", color="#1f4e79", lw=2.2)
    a2.fill_between(lams, rb, 0.32, alpha=0.12, color="#c0392b")
    for l, t in ((0.0, r"$\lambda{=}0$"),
                 (0.25, r"$\varepsilon{\approx}1.10$"),
                 (0.5, "no information")):
        v = bound_privatised(chi2_full, p_max, l)
        a2.plot([l], [v], "o", color="#c0392b", ms=6)
        a2.annotate(t, (l, v), textcoords="offset points",
                    xytext=(-52 if l == 0.5 else 6, -11), fontsize=7.5)
    a2.set_xlabel(r"BSC privatisation parameter $\lambda$", fontsize=10)
    a2.set_ylabel(r"lower bound on $R_B$", fontsize=10)
    a2.grid(alpha=0.25); a2.set_ylim(0, 0.32)
    a2.set_title("(b) Privacy--accuracy trade-off", fontsize=10)

    plt.tight_layout()
    plt.savefig(path, dpi=200, bbox_inches="tight")


if __name__ == "__main__":
    main()
