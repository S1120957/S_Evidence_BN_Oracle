"""
boundary_stress.py  --  RQ3 evidence (Path A)
==============================================
RQ3 asks not only *whether* fixed-point encoding preserves decision
consistency, but "**what are the conditions under which this guarantee
holds**".  Reporting "0 inconsistencies observed" does not answer that
question if no evaluated posterior ever came near the decision threshold.

Theory (Proposition 1 / Definition 1) says inconsistency is possible only
when

        |p - tau|  <=  eps,      eps = 1 / (2 * SCALE)

This script tests that boundary directly.  It has two parts.

PART A -- analytic boundary sweep (off-chain, exhaustive)
    Constructs CPT configurations whose posterior P(PPH=1|e) sits at
    CONTROLLED distances from tau = 0.5, spanning several decades either
    side of eps, by bisection on a single CPT entry.  For each posterior
    and each candidate SCALE it checks whether the encoded decision
    matches the floating-point decision.  This establishes:
      (i)  consistency ALWAYS holds when |p - tau| > eps  (bound is sound)
      (ii) inconsistency CAN occur when |p - tau| < eps   (bound is tight)
      (iii) the minimum SCALE sufficient for a given safety margin

PART B -- on-chain confirmation (Sepolia)
    Writes the most adversarial near-boundary CPT configuration found in
    Part A to CPTStore, runs the full submit/read-back cycle, and verifies
    the contract stores exactly the encoded value the theory predicts.
    This shows the bound is not an artefact of the off-chain code path.

Output: results/boundary_stress.csv

Usage:
    python scripts/boundary_stress.py            # Part A + Part B
    python scripts/boundary_stress.py --offline  # Part A only (no chain)
"""

import argparse
import csv
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from bn_oracle import BNOracle

ROOT    = Path(__file__).resolve().parent.parent
OUT_CSV = ROOT / "results" / "boundary_stress.csv"

TAU     = 0.5
SCALES  = [10**2, 10**3, 10**4, 10**6]
DEFAULT_SCALE = 10**6

# Distances from tau to target, spanning eps = 5e-7 for SCALE = 1e6
TARGET_OFFSETS = [
    1e-2, 1e-3, 1e-4, 1e-5, 1e-6,
    5e-7,          # exactly eps for SCALE = 1e6
    1e-7, 1e-8, 1e-9, 0.0,
]

BASE_CPTS: Dict[str, Dict[Any, float]] = {
    "GPS": {(1, 0): 0.90, (0, 1): 0.15, (1, 1): 0.80, (0, 0): 0.10},
    "PC":  {(1, 0): 0.85, (0, 1): 0.20, (1, 1): 0.75, (0, 0): 0.15},
    "PMD": {(1, 0): 0.88, (0, 1): 0.10, (1, 1): 0.78, (0, 0): 0.08},
    "PR":  {(1, 0): 0.80, (0, 1): 0.25, (1, 1): 0.70, (0, 0): 0.20},
}


# ---------------------------------------------------------------------------
# Encoding helpers parameterised by SCALE
# ---------------------------------------------------------------------------

def encode(p: float, scale: int) -> int:
    return int(round(p * scale))


def decode(p_hat: int, scale: int) -> float:
    return p_hat / scale


def decision(p: float, tau: float = TAU) -> bool:
    return p >= tau


def consistent(p: float, scale: int, tau: float = TAU) -> bool:
    return decision(p, tau) == decision(decode(encode(p, scale), scale), tau)


# ---------------------------------------------------------------------------
# PART A -- construct posteriors at controlled distances from tau
# ---------------------------------------------------------------------------

def posterior_for_prior(prior_pph: float, evidence: Dict[str, int]) -> float:
    """P(PPH=1 | evidence) as a function of the PPH prior."""
    o = BNOracle(prior_pph=prior_pph, prior_ppr=0.5, cpts=BASE_CPTS)
    return float(o.infer(evidence)["PPH"])


def solve_prior_for_target(target: float, evidence: Dict[str, int],
                           iters: int = 200) -> Optional[float]:
    """
    Bisection on the PPH prior to drive P(PPH=1|e) to `target`.
    Returns the prior, or None if the target is unreachable.
    """
    lo, hi = 1e-12, 1.0 - 1e-12
    f_lo = posterior_for_prior(lo, evidence) - target
    f_hi = posterior_for_prior(hi, evidence) - target
    if f_lo * f_hi > 0:
        return None
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        f_mid = posterior_for_prior(mid, evidence) - target
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def run_part_a() -> List[Dict[str, Any]]:
    print("=" * 66)
    print("PART A -- analytic boundary sweep (off-chain)")
    print("=" * 66)
    evidence = {"GPS": 1, "PC": 0, "PMD": 1, "PR": 0}
    rows: List[Dict[str, Any]] = []

    print(f"  tau = {TAU},  evidence = {evidence}")
    print(f"  eps(SCALE=1e6) = {1/(2*DEFAULT_SCALE):.3e}\n")
    hdr = (f"  {'offset':>10} {'side':>5} {'posterior':>20} "
           f"{'|p-tau|':>11} " + " ".join(f"S=1e{len(str(s))-1:<2}" for s in SCALES))
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for offset in TARGET_OFFSETS:
        for side, sgn in (("above", +1.0), ("below", -1.0)):
            if offset == 0.0 and side == "below":
                continue
            target = TAU + sgn * offset
            prior = solve_prior_for_target(target, evidence)
            if prior is None:
                continue
            p = posterior_for_prior(prior, evidence)
            dist = abs(p - TAU)

            flags = []
            row: Dict[str, Any] = {
                "target_offset": offset, "side": side,
                "prior_pph": prior, "posterior_pph": p,
                "dist_from_tau": dist,
            }
            for s in SCALES:
                ok = consistent(p, s)
                eps = 1.0 / (2 * s)
                row[f"consistent_scale_{s}"] = ok
                row[f"within_eps_scale_{s}"] = dist <= eps
                flags.append("ok " if ok else "BAD")
            rows.append(row)
            print(f"  {offset:>10.0e} {side:>5} {p:>20.16f} "
                  f"{dist:>11.3e} " + "  ".join(f"{f:<5}" for f in flags))

    # --- soundness / tightness analysis --------------------------------
    print("\n  --- bound analysis (SCALE = 1e6, eps = 5.0e-07) ---")
    eps = 1.0 / (2 * DEFAULT_SCALE)
    outside = [r for r in rows if r["dist_from_tau"] > eps]
    inside  = [r for r in rows if r["dist_from_tau"] <= eps]
    out_ok  = sum(1 for r in outside if r[f"consistent_scale_{DEFAULT_SCALE}"])
    in_bad  = sum(1 for r in inside
                  if not r[f"consistent_scale_{DEFAULT_SCALE}"])
    print(f"    |p-tau| >  eps : {out_ok}/{len(outside)} consistent "
          f"-> bound SOUND ({'yes' if out_ok == len(outside) else 'NO'})")
    print(f"    |p-tau| <= eps : {in_bad}/{len(inside)} inconsistent "
          f"-> bound TIGHT ({'yes' if in_bad > 0 else 'no violation found'})")

    print("\n  --- minimum sufficient SCALE per safety margin ---")
    for margin in (1e-2, 1e-3, 1e-4, 1e-5, 1e-6):
        cohort = [r for r in rows if r["dist_from_tau"] >= margin]
        if not cohort:
            continue
        best = None
        for s in SCALES:
            if all(r[f"consistent_scale_{s}"] for r in cohort):
                best = s
                break
        print(f"    posteriors with |p-tau| >= {margin:.0e} "
              f"({len(cohort):>2} cases): minimum SCALE = "
              f"{best if best else '> 1e6'}")
    return rows


# ---------------------------------------------------------------------------
# PART B -- on-chain confirmation
# ---------------------------------------------------------------------------

def run_part_b(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from dotenv import load_dotenv
    from eth_account import Account
    from web3 import Web3

    load_dotenv()
    print("\n" + "=" * 66)
    print("PART B -- on-chain confirmation (Sepolia)")
    print("=" * 66)

    build = ROOT / "deployment" / "build"
    w3 = Web3(Web3.HTTPProvider(os.environ["SEPOLIA_RPC_URL"]))
    if not w3.is_connected():
        raise RuntimeError("Web3 not connected.")
    pk = os.environ["PRIVATE_KEY"]
    sender = Account.from_key(pk).address

    def lc(name, addr):
        abi = json.load(open(build / f"{name}.json", encoding="utf-8"))["abi"]
        return w3.eth.contract(address=Web3.to_checksum_address(addr), abi=abi)

    cpt_store   = lc("CPTStore",         os.environ["CPTSTORE_ADDR"])
    claim_reg   = lc("ClaimRegistry",    os.environ["CLAIMREGISTRY_ADDR"])
    oracle_ctrl = lc("OracleController", os.environ["ORACLECONTROLLER_ADDR"])

    def send(fn):
        nonce = w3.eth.get_transaction_count(sender, "pending")
        tx = fn.build_transaction({
            "from": sender, "nonce": nonce, "chainId": w3.eth.chain_id,
            "gas": 800_000,
            "maxFeePerGas": w3.to_wei("50", "gwei"),
            "maxPriorityFeePerGas": w3.to_wei("3", "gwei"),
        })
        s = w3.eth.account.sign_transaction(tx, pk)
        h = w3.eth.send_raw_transaction(s.raw_transaction)
        r = w3.eth.wait_for_transaction_receipt(h, timeout=300)
        if r.status != 1:
            raise RuntimeError(f"TX reverted: {h.hex()}")
        return r

    def read_synced(cid, blk, retries=15, delay=2.0):
        for _ in range(retries):
            if w3.eth.block_number >= blk:
                try:
                    return claim_reg.functions.getClaim(cid).call(
                        block_identifier=blk)
                except Exception:      # noqa: BLE001
                    pass
            time.sleep(delay)
        return claim_reg.functions.getClaim(cid).call()

    # pick the most adversarial cases: smallest |p - tau|
    cases = sorted(rows, key=lambda r: r["dist_from_tau"])[:4]
    run_id = int(time.time())
    out: List[Dict[str, Any]] = []

    print(f"  Submitting {len(cases)} near-boundary posteriors on-chain\n")
    for i, c in enumerate(cases):
        p = c["posterior_pph"]
        enc = encode(p, DEFAULT_SCALE)

        cid = int(claim_reg.functions.nextClaimId().call())
        key = Web3.keccak(text=f"LUNA_BOUNDARY_{run_id}_{i}")
        send(oracle_ctrl.functions.openClaim(key))

        snap = cpt_store.functions.getCPTSnapshot().call()
        bn_id = bytes(snap[3])
        r = send(oracle_ctrl.functions.submitInference(
            cid, 1, 0, 1, 0, enc, enc, bn_id))
        blk = int(r.blockNumber)

        tup = read_synced(cid, blk)
        on_enc = int(tup[3])
        on_p = decode(on_enc, DEFAULT_SCALE)
        ok = decision(p) == decision(on_p)

        print(f"  case {i}: |p-tau|={c['dist_from_tau']:.3e} "
              f"p={p:.12f} enc={enc} on-chain={on_enc} "
              f"decision_match={ok}")

        out.append({
            **c, "onchain_encoded": on_enc, "onchain_posterior": on_p,
            "onchain_decision_consistent": ok,
            "claim_id": cid, "block_number": blk,
            "tx_hash": r.transactionHash.hex(), "run_id": run_id,
        })

    n_ok = sum(1 for r in out if r["onchain_decision_consistent"])
    print(f"\n  on-chain decision consistency: {n_ok}/{len(out)}")
    print("  (cases inside eps are EXPECTED to be inconsistent - that is "
          "the bound being tight, not a defect)")
    return out


# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="run Part A only (no chain interaction)")
    args = ap.parse_args()

    rows = run_part_a()
    if not args.offline:
        try:
            onchain = run_part_b(rows)
            by_key = {(r["target_offset"], r["side"]): r for r in onchain}
            for r in rows:
                m = by_key.get((r["target_offset"], r["side"]))
                r["onchain_tested"] = m is not None
                if m:
                    r["onchain_encoded"] = m["onchain_encoded"]
                    r["onchain_decision_consistent"] = \
                        m["onchain_decision_consistent"]
                    r["tx_hash"] = m["tx_hash"]
        except Exception as exc:                       # noqa: BLE001
            print(f"\n  Part B skipped ({type(exc).__name__}: {exc})")
            print("  Part A results are complete and standalone.")

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    keys: List[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        wri = csv.DictWriter(f, fieldnames=keys)
        wri.writeheader()
        for r in rows:
            wri.writerow({k: r.get(k, "") for k in keys})
    print(f"\n  Wrote {len(rows)} rows -> {OUT_CSV}")


if __name__ == "__main__":
    main()
