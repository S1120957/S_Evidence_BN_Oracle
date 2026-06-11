"""
measure_scaling.py
------------------
Path A: measure REAL CPTStore initialization gas at N_e = 8 and N_e = 32
evidence nodes by deploying the parameterized CPTStoreScaling contract and
running the full init sequence (setPriors + N_e*4 setEvidenceCPT).

This produces genuine 3-point init-scaling data:
    N_e = 4   -> from existing sepolia_gas_logs.csv (1,613,013 gas)
    N_e = 8   -> measured here (32 CPT writes)
    N_e = 32  -> measured here (128 CPT writes)

submitInference / openClaim are NOT re-measured: they are BN-size-invariant
by construction (fixed-width OracleController interface) and already measured
on the production contracts.

Output: results/scaling_gas_logs.csv

Prereqs:
  - CPTStoreScaling.sol compiled into deployment/build/CPTStoreScaling.json
    (run: npx truffle compile  after placing the .sol in contracts/)
  - Same env vars as other scripts (SEPOLIA_RPC_URL, PRIVATE_KEY)

Usage:
  python scripts/measure_scaling.py
"""

import csv
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3

load_dotenv()

ROOT      = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "deployment" / "build"
OUT_CSV   = ROOT / "results" / "scaling_gas_logs.csv"

SEPOLIA_RPC_URL = os.environ["SEPOLIA_RPC_URL"]
PRIVATE_KEY     = os.environ["PRIVATE_KEY"]

TX_TIMEOUT    = 300
MAX_FEE_GWEI  = 50
PRIORITY_GWEI = 3

# BN sizes to measure (4 already measured on production CPTStore)
SIZES = [8, 32]

# Asymmetric CPT values reused for every node (value is irrelevant to gas;
# what matters is that each write is a non-zero SSTORE to a fresh slot,
# matching the production init path).
CPT_VALUES = [900000, 150000, 800000, 100000]  # (1,0),(0,1),(1,1),(0,0)
PARENT_CFG = [(1, 0), (0, 1), (1, 1), (0, 0)]


def load_artifact(name):
    return json.load(open(BUILD_DIR / f"{name}.json", encoding="utf-8"))


def send_tx(w3, fn, sender, pk):
    nonce = w3.eth.get_transaction_count(sender, "pending")
    tx = fn.build_transaction({
        "from": sender, "nonce": nonce, "chainId": w3.eth.chain_id,
        "gas": 1_500_000,
        "maxFeePerGas":         w3.to_wei(str(MAX_FEE_GWEI),  "gwei"),
        "maxPriorityFeePerGas": w3.to_wei(str(PRIORITY_GWEI), "gwei"),
    })
    signed  = w3.eth.account.sign_transaction(tx, pk)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=TX_TIMEOUT)
    if receipt.status != 1:
        raise RuntimeError(f"TX REVERTED: {tx_hash.hex()}")
    return tx_hash.hex(), int(receipt.gasUsed), int(receipt.blockNumber)


def deploy(w3, artifact, sender, pk, num_evidence):
    contract = w3.eth.contract(
        abi=artifact["abi"], bytecode=artifact["bytecode"]
    )
    nonce = w3.eth.get_transaction_count(sender, "pending")
    tx = contract.constructor(num_evidence).build_transaction({
        "from": sender, "nonce": nonce, "chainId": w3.eth.chain_id,
        "gas": 1_500_000,
        "maxFeePerGas":         w3.to_wei(str(MAX_FEE_GWEI),  "gwei"),
        "maxPriorityFeePerGas": w3.to_wei(str(PRIORITY_GWEI), "gwei"),
    })
    signed  = w3.eth.account.sign_transaction(tx, pk)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=TX_TIMEOUT)
    if receipt.status != 1:
        raise RuntimeError(f"Deploy REVERTED at N={num_evidence}")
    return receipt.contractAddress, int(receipt.gasUsed)


def main():
    w3 = Web3(Web3.HTTPProvider(SEPOLIA_RPC_URL))
    if not w3.is_connected():
        raise RuntimeError("Web3 not connected.")
    sender = Account.from_key(PRIVATE_KEY).address

    try:
        artifact = load_artifact("CPTStoreScaling")
    except FileNotFoundError:
        raise SystemExit(
            "CPTStoreScaling.json not found in deployment/build/.\n"
            "Place CPTStoreScaling.sol in contracts/ and run:\n"
            "  npx truffle compile"
        )

    rows = []
    print(f"Sender: {sender}\n")

    for n in SIZES:
        print(f"=== N_e = {n} nodes ({n*4} CPT entries) ===")
        addr, deploy_gas = deploy(w3, artifact, sender, PRIVATE_KEY, n)
        print(f"  deployed at {addr}  (deploy gas={deploy_gas:,})")

        c = w3.eth.contract(address=addr, abi=artifact["abi"])

        # setPriors (asymmetric: 0.3 / 0.7 like the paper)
        _, g_priors, blk = send_tx(
            w3, c.functions.setPriors(300000, 700000), sender, PRIVATE_KEY
        )
        print(f"  setPriors gas={g_priors:,}")
        rows.append({
            "network": "Sepolia", "num_evidence": n,
            "tx_type": "setPriors", "gas_used": g_priors,
            "block_number": blk, "contract": addr,
        })

        init_total = g_priors
        # N_e * 4 setEvidenceCPT writes
        for i in range(n):
            for k, (pph, ppr) in enumerate(PARENT_CFG):
                _, g, blk = send_tx(
                    w3,
                    c.functions.setEvidenceCPT(i, pph, ppr, CPT_VALUES[k]),
                    sender, PRIVATE_KEY,
                )
                init_total += g
                rows.append({
                    "network": "Sepolia", "num_evidence": n,
                    "tx_type": "setEvidenceCPT", "gas_used": g,
                    "block_number": blk, "contract": addr,
                })
            print(f"    node {i+1}/{n} written", end="\r", flush=True)

        print(f"\n  >>> CPTStore init TOTAL (N={n}): {init_total:,} gas\n")
        rows.append({
            "network": "Sepolia", "num_evidence": n,
            "tx_type": "CPTStore_init_TOTAL", "gas_used": init_total,
            "block_number": blk, "contract": addr,
        })

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        wri = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wri.writeheader()
        wri.writerows(rows)

    print("=" * 55)
    print("SCALING MEASUREMENT COMPLETE")
    for n in SIZES:
        tot = next(r["gas_used"] for r in rows
                   if r["num_evidence"] == n
                   and r["tx_type"] == "CPTStore_init_TOTAL")
        print(f"  N_e={n:>2}: CPTStore init = {tot:,} gas")
    print(f"  (N_e= 4: 1,613,013 gas from existing measurement)")
    print(f"\n  Wrote {len(rows)} rows -> {OUT_CSV}")


if __name__ == "__main__":
    main()
