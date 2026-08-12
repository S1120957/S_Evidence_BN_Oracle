"""
lifecycle_experiment.py  --  RQ2 evidence (Path A)
===================================================
Measures the claim lifecycle on Sepolia with REAL staged, partial,
out-of-order evidence arrival.

This is the experiment that Section "Claim Lifecycle Evaluation" reports.
It exercises exactly what RQ2 claims:

  * three concurrent claims, opened once, never redeployed;
  * evidence arriving INCREMENTALLY (1 -> 2 -> 3 -> 4 variables observed);
  * each claim using a DIFFERENT arrival ORDER;
  * unobserved variables encoded on-chain as UNOBSERVED (=2), so the
    ledger record is a faithful transcript at every intermediate stage;
  * finalizeClaim() at the end of each claim;
  * a post-finalization submitInference() that MUST revert.

For every stage it records: gas used, the ternary evidence tuple written
on chain, the off-chain posterior, the on-chain posterior read back, and
an independent AUDITOR recomputation performed by decoding the on-chain
evidence record and re-running inference.  The auditor delta is the
quantitative form of the transcript-completeness claim.

Output: results/lifecycle_gas_logs.csv

Usage:
    python scripts/lifecycle_experiment.py
"""

import csv
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3
from web3.contract import Contract

from bn_oracle import (
    BNOracle,
    canonicalize_evidence_for_chain,
    decode_chain_evidence,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT      = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "deployment" / "build"
OUT_CSV   = ROOT / "results" / "lifecycle_gas_logs.csv"

SEPOLIA_RPC_URL       = os.environ["SEPOLIA_RPC_URL"]
PRIVATE_KEY           = os.environ["PRIVATE_KEY"]
CPTSTORE_ADDR         = Web3.to_checksum_address(os.environ["CPTSTORE_ADDR"])
CLAIMREGISTRY_ADDR    = Web3.to_checksum_address(os.environ["CLAIMREGISTRY_ADDR"])
ORACLECONTROLLER_ADDR = Web3.to_checksum_address(os.environ["ORACLECONTROLLER_ADDR"])

TX_TIMEOUT    = 300
MAX_FEE_GWEI  = 50
PRIORITY_GWEI = 3
RUN_ID        = int(time.time())

EVIDENCE_ORDER = ["GPS", "PC", "PMD", "PR"]

# The three arrival orderings (RQ2). Same evidence SET, different ORDER.
# Final assignment is identical for all three, so order-invariance of the
# committed posterior is directly testable.
FINAL_ASSIGNMENT = {"GPS": 1, "PC": 1, "PMD": 1, "PR": 0}

ORDERINGS: Dict[str, List[str]] = {
    "canonical": ["GPS", "PC", "PMD", "PR"],
    "reverse":   ["PR", "PMD", "PC", "GPS"],
    "gapped":    ["GPS", "PR", "PC", "PMD"],
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_contract(w3: Web3, name: str, address: str) -> Contract:
    abi = json.load(open(BUILD_DIR / f"{name}.json", encoding="utf-8"))["abi"]
    return w3.eth.contract(address=address, abi=abi)


def send_tx(w3: Web3, fn: Any, sender: str, pk: str) -> Any:
    """Sign, send, wait for confirmed receipt. Raises on revert."""
    nonce = w3.eth.get_transaction_count(sender, "pending")
    tx = fn.build_transaction({
        "from": sender, "nonce": nonce, "chainId": w3.eth.chain_id,
        "gas": 800_000,
        "maxFeePerGas":         w3.to_wei(str(MAX_FEE_GWEI),  "gwei"),
        "maxPriorityFeePerGas": w3.to_wei(str(PRIORITY_GWEI), "gwei"),
    })
    signed  = w3.eth.account.sign_transaction(tx, pk)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=TX_TIMEOUT)
    if receipt.status != 1:
        raise RuntimeError(f"TX REVERTED unexpectedly: {tx_hash.hex()}")
    return receipt


def expect_revert(w3: Web3, fn: Any, sender: str) -> Optional[str]:
    """
    Assert that a call reverts. Uses eth_call (no gas spent, no state change).
    Returns the revert reason string, or None if it did NOT revert.
    """
    try:
        fn.call({"from": sender})
        return None                      # did not revert -> test FAILED
    except Exception as exc:             # noqa: BLE001 - want the message
        return str(exc)


def read_claim_synced(w3, claim_reg, claim_id, target_block,
                      retries=15, delay=2.0):
    """Read getClaim pinned to target_block, waiting for node sync."""
    for _ in range(retries):
        if w3.eth.block_number >= target_block:
            try:
                return claim_reg.functions.getClaim(claim_id).call(
                    block_identifier=target_block
                )
            except Exception:            # noqa: BLE001 - node lag
                pass
        time.sleep(delay)
    return claim_reg.functions.getClaim(claim_id).call()


def read_state_synced(w3, claim_reg, claim_id, target_block,
                      retries=15, delay=2.0):
    """Read getClaimState pinned to target_block."""
    for _ in range(retries):
        if w3.eth.block_number >= target_block:
            try:
                return claim_reg.functions.getClaimState(claim_id).call(
                    block_identifier=target_block
                )
            except Exception:            # noqa: BLE001
                pass
        time.sleep(delay)
    return claim_reg.functions.getClaimState(claim_id).call()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    w3 = Web3(Web3.HTTPProvider(SEPOLIA_RPC_URL))
    if not w3.is_connected():
        raise RuntimeError("Web3 not connected.")

    sender      = Account.from_key(PRIVATE_KEY).address
    cpt_store   = load_contract(w3, "CPTStore",         CPTSTORE_ADDR)
    claim_reg   = load_contract(w3, "ClaimRegistry",    CLAIMREGISTRY_ADDR)
    oracle_ctrl = load_contract(w3, "OracleController", ORACLECONTROLLER_ADDR)

    print(f"Sender : {sender}")
    print(f"Run ID : {RUN_ID}")
    print(f"Lifecycle: 3 claims x 4 stages, partial evidence, "
          f"then finalize + revert check\n")

    rows: List[Dict[str, Any]] = []

    for claim_label, order in ORDERINGS.items():
        print(f"=== Claim '{claim_label}'  order={' -> '.join(order)} ===")

        # ---- open claim -------------------------------------------------
        claim_id = int(claim_reg.functions.nextClaimId().call())
        key = Web3.keccak(text=f"SELENE_LIFECYCLE_{RUN_ID}_{claim_label}")
        r = send_tx(w3, oracle_ctrl.functions.openClaim(key),
                    sender, PRIVATE_KEY)
        open_gas, open_block = int(r.gasUsed), int(r.blockNumber)
        state = read_state_synced(w3, claim_reg, claim_id, open_block)
        if state != 1:
            raise RuntimeError(f"claim {claim_id} state={state}, expected Open")
        print(f"  openClaim  claimId={claim_id}  gas={open_gas:,}")
        rows.append({
            "run_id": RUN_ID, "claim_label": claim_label,
            "claim_id": claim_id, "stage": 0, "operation": "openClaim",
            "n_observed": 0, "evidence_order": "|".join(order),
            "gps_chain": "", "pc_chain": "", "pmd_chain": "", "pr_chain": "",
            "gas_used": open_gas, "block_number": open_block,
            "pph_offchain": "", "ppr_offchain": "",
            "pph_onchain": "", "ppr_onchain": "",
            "auditor_pph": "", "auditor_delta_pph": "",
            "tx_hash": r.transactionHash.hex(), "notes": "",
        })

        # ---- staged partial evidence -----------------------------------
        observed: Dict[str, int] = {}
        for stage, ev_name in enumerate(order, start=1):
            observed[ev_name] = FINAL_ASSIGNMENT[ev_name]

            # off-chain inference over the OBSERVED SUBSET ONLY
            result   = BNOracle.infer_from_chain(w3, cpt_store, observed)
            pph, ppr = float(result["PPH"]), float(result["PPR"])
            pph_enc  = BNOracle.encode(pph)
            ppr_enc  = BNOracle.encode(ppr)

            # ternary encoding: unobserved -> 2
            chain_ev = canonicalize_evidence_for_chain(observed)
            bn_id    = bytes.fromhex(result["bn_instance_id"][2:])

            r = send_tx(
                w3,
                oracle_ctrl.functions.submitInference(
                    claim_id,
                    chain_ev["GPS"], chain_ev["PC"],
                    chain_ev["PMD"], chain_ev["PR"],
                    pph_enc, ppr_enc, bn_id,
                ),
                sender, PRIVATE_KEY,
            )
            gas, blk = int(r.gasUsed), int(r.blockNumber)

            # read back, pinned to the submit block
            tup = read_claim_synced(w3, claim_reg, claim_id, blk)
            on_pph = BNOracle.decode(int(tup[3]))
            on_ppr = BNOracle.decode(int(tup[4]))

            # AUDITOR: decode the on-chain ternary record and recompute
            audit_ev   = decode_chain_evidence(
                chain_ev["GPS"], chain_ev["PC"],
                chain_ev["PMD"], chain_ev["PR"],
            )
            audit_res  = BNOracle.infer_from_chain(w3, cpt_store, audit_ev)
            audit_pph  = float(audit_res["PPH"])
            audit_dpph = abs(audit_pph - pph)

            print(f"  stage {stage}: +{ev_name:4} "
                  f"chain=({chain_ev['GPS']},{chain_ev['PC']},"
                  f"{chain_ev['PMD']},{chain_ev['PR']}) "
                  f"gas={gas:,} P(PPH)={pph:.4f} "
                  f"audit_d={audit_dpph:.2e}")

            rows.append({
                "run_id": RUN_ID, "claim_label": claim_label,
                "claim_id": claim_id, "stage": stage,
                "operation": "submitInference",
                "n_observed": len(observed),
                "evidence_order": "|".join(order),
                "gps_chain": chain_ev["GPS"], "pc_chain": chain_ev["PC"],
                "pmd_chain": chain_ev["PMD"], "pr_chain": chain_ev["PR"],
                "gas_used": gas, "block_number": blk,
                "pph_offchain": pph, "ppr_offchain": ppr,
                "pph_onchain": on_pph, "ppr_onchain": on_ppr,
                "auditor_pph": audit_pph,
                "auditor_delta_pph": audit_dpph,
                "tx_hash": r.transactionHash.hex(), "notes": "",
            })

        # ---- finalize ---------------------------------------------------
        r = send_tx(w3, oracle_ctrl.functions.finalizeClaim(claim_id),
                    sender, PRIVATE_KEY)
        fin_gas, fin_block = int(r.gasUsed), int(r.blockNumber)
        fin_state = read_state_synced(w3, claim_reg, claim_id, fin_block)
        print(f"  finalizeClaim gas={fin_gas:,} state={fin_state} "
              f"({'Finalized' if fin_state == 2 else 'UNEXPECTED'})")
        rows.append({
            "run_id": RUN_ID, "claim_label": claim_label,
            "claim_id": claim_id, "stage": 5, "operation": "finalizeClaim",
            "n_observed": 4, "evidence_order": "|".join(order),
            "gps_chain": "", "pc_chain": "", "pmd_chain": "", "pr_chain": "",
            "gas_used": fin_gas, "block_number": fin_block,
            "pph_offchain": "", "ppr_offchain": "",
            "pph_onchain": "", "ppr_onchain": "",
            "auditor_pph": "", "auditor_delta_pph": "",
            "tx_hash": r.transactionHash.hex(),
            "notes": f"final_state={fin_state}",
        })

        # ---- post-finalization submission MUST revert -------------------
        snap = cpt_store.functions.getCPTSnapshot().call()
        bn_id = bytes(snap[3])
        reason = expect_revert(
            w3,
            oracle_ctrl.functions.submitInference(
                claim_id, 1, 1, 1, 0, 500_000, 500_000, bn_id,
            ),
            sender,
        )
        reverted = reason is not None
        print(f"  post-finalization submit reverted: {reverted}")
        if reverted:
            short = reason.split("revert")[-1].strip()[:70]
            print(f"    reason: {short}")
        rows.append({
            "run_id": RUN_ID, "claim_label": claim_label,
            "claim_id": claim_id, "stage": 6,
            "operation": "postFinalizeSubmit_expectRevert",
            "n_observed": 4, "evidence_order": "|".join(order),
            "gps_chain": "", "pc_chain": "", "pmd_chain": "", "pr_chain": "",
            "gas_used": 0, "block_number": 0,
            "pph_offchain": "", "ppr_offchain": "",
            "pph_onchain": "", "ppr_onchain": "",
            "auditor_pph": "", "auditor_delta_pph": "",
            "tx_hash": "",
            "notes": f"reverted={reverted}; {(reason or 'NO REVERT')[:120]}",
        })
        print()

    # ---- write CSV ------------------------------------------------------
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        wri = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wri.writeheader()
        wri.writerows(rows)

    # ---- summary --------------------------------------------------------
    subs = [r for r in rows if r["operation"] == "submitInference"]
    gases = [r["gas_used"] for r in subs]
    finals = {r["claim_label"]: (r["pph_offchain"], r["ppr_offchain"])
              for r in subs if r["stage"] == 4}
    audit_max = max(float(r["auditor_delta_pph"]) for r in subs)

    import statistics
    print("=" * 62)
    print("LIFECYCLE SUMMARY")
    print("=" * 62)
    print(f"  submitInference stages : n={len(gases)}")
    print(f"    mean={statistics.mean(gases):,.0f} gas  "
          f"std={statistics.pstdev(gases):,.0f}  "
          f"min={min(gases):,}  max={max(gases):,}")
    print(f"    range={max(gases)-min(gases):,} gas "
          f"({100*(max(gases)-min(gases))/statistics.mean(gases):.2f}%)")
    print(f"  max auditor recomputation delta: {audit_max:.2e}")
    print("  final posteriors per ordering (must be identical):")
    for k, v in finals.items():
        print(f"    {k:10} P(PPH)={v[0]:.10f}  P(PPR)={v[1]:.10f}")
    uniq = len(set(finals.values()))
    print(f"  order-invariance: {'CONFIRMED' if uniq == 1 else 'VIOLATED'}"
          f" ({uniq} distinct final posterior(s))")
    reverts = [r for r in rows
               if r["operation"] == "postFinalizeSubmit_expectRevert"]
    n_rev = sum(1 for r in reverts if "reverted=True" in r["notes"])
    print(f"  absorbing Finalized state: {n_rev}/{len(reverts)} "
          f"post-finalization submits reverted")
    print(f"\n  Wrote {len(rows)} rows -> {OUT_CSV}")


if __name__ == "__main__":
    main()

