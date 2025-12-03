"""
claim_lifecycle_demo.py

Demonstration of a single claim whose posterior is updated as evidence
arrives over time.

Pipeline:

1. Rebuild BN from CPTStore via bn_from_chain.build_bn_from_chain().
2. Open a single claim in ClaimRegistry with a fixed externalKey.
3. Simulate dynamic evidence arrival for that claim in 4 stages:

   Stage 0: GPS observed only
   Stage 1: GPS + PC
   Stage 2: GPS + PC + PMD
   Stage 3: GPS + PC + PMD + PR

4. For each stage:
   - Run BN inference off-chain with the currently available evidence.
   - Scale and commit posterior via OracleController.submitInference(claimId, scaledPPH, scaledPPR).
   - Read back the claim from ClaimRegistry.claims(claimId).
   - Log gasUsed, blockNumber, timestamp, and on-chain posterior.

5. Append all steps to experiments/claim_lifecycle_demo.csv.

NOTE:
This script assumes ClaimRegistry.storePosterior/submitInference can be called
multiple times for the same claimId (i.e., the claim is not permanently locked
after the first write).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
import sys
from typing import Dict, Any

from web3 import Web3

# ---------------------------------------------------------------------------
# Project root / imports
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.bn_from_chain import build_bn_from_chain  # noqa: E402

GANACHE_URL = "http://127.0.0.1:7545"
NETWORK_ID = "5777"   # Ganache network id
SCALE = 1_000_000     # must match CPTStore.SCALE


# ---------------------------------------------------------------------------
# Helpers to load Truffle artifacts / contracts
# ---------------------------------------------------------------------------

def _find_build_artifact(name: str) -> Dict[str, Any]:
    """Load a Truffle artifact JSON for the given contract name."""
    candidates = [
        ROOT / "deployment" / "build" / f"{name}.json",
        ROOT / "deployments" / "build" / f"{name}.json",
    ]
    for path in candidates:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
    raise FileNotFoundError(
        f"Could not find {name}.json in any known build directory. "
        f"Tried: {', '.join(str(c) for c in candidates)}"
    )


def _load_contract(w3: Web3, name: str):
    """Instantiate a web3 contract from its Truffle artifact."""
    artifact = _find_build_artifact(name)
    networks = artifact.get("networks", {})
    if NETWORK_ID not in networks:
        raise RuntimeError(f"{name} not deployed on network id {NETWORK_ID}")
    addr = networks[NETWORK_ID]["address"]
    abi = artifact["abi"]
    return w3.eth.contract(address=addr, abi=abi)


# ---------------------------------------------------------------------------
# Main demo
# ---------------------------------------------------------------------------

def main() -> None:
    # ----------------------------------------------------------------------
    # Web3 + accounts
    # ----------------------------------------------------------------------
    w3 = Web3(Web3.HTTPProvider(GANACHE_URL))
    if not w3.is_connected():
        raise RuntimeError(f"Web3 not connected to Ganache at {GANACHE_URL}")

    accounts = w3.eth.accounts
    if not accounts:
        raise RuntimeError("No accounts available in Ganache")
    sender = accounts[0]

    # ----------------------------------------------------------------------
    # Off-chain BN reconstructed from CPTStore
    # ----------------------------------------------------------------------
    bn, priors, cpts = build_bn_from_chain(w3)
    print("Priors from chain:", priors)
    print("Example GPS CPT (PPH=1,PPR=0):", cpts["GPS"][(1, 0)])

    # ----------------------------------------------------------------------
    # On-chain contracts
    # ----------------------------------------------------------------------
    controller = _load_contract(w3, "OracleController")
    claim_registry = _load_contract(w3, "ClaimRegistry")

    # ----------------------------------------------------------------------
    # Open a single claim to track across all evidence stages
    # ----------------------------------------------------------------------
    claim_key = Web3.keccak(text="CLAIM_DYNAMIC_DEMO")

    tx_open = claim_registry.functions.openClaim(claim_key).transact({"from": sender})
    receipt_open = w3.eth.wait_for_transaction_receipt(tx_open)

    next_id = claim_registry.functions.nextClaimId().call()
    claim_id = next_id - 1

    print(
        f"Opened dynamic demo claim: claim_id={claim_id}, "
        f"tx_open={receipt_open.transactionHash.hex()}"
    )

    raw_claim = claim_registry.functions.claims(claim_id).call()
    print("Initial claim state (raw struct):", raw_claim)

    # ----------------------------------------------------------------------
    # Define staged evidence arrival
    # ----------------------------------------------------------------------
    # We use -1 to mean "not observed yet" for logging.
    stages = [
        {
            "label": "stage_0_gps_only",
            "evidence": {"GPS": 1},
            "log_bits": {"GPS": 1, "PC": -1, "PMD": -1, "PR": -1},
        },
        {
            "label": "stage_1_add_pc",
            "evidence": {"GPS": 1, "PC": 0},
            "log_bits": {"GPS": 1, "PC": 0, "PMD": -1, "PR": -1},
        },
        {
            "label": "stage_2_add_pmd",
            "evidence": {"GPS": 1, "PC": 0, "PMD": 1},
            "log_bits": {"GPS": 1, "PC": 0, "PMD": 1, "PR": -1},
        },
        {
            "label": "stage_3_full",
            "evidence": {"GPS": 1, "PC": 0, "PMD": 1, "PR": 0},
            "log_bits": {"GPS": 1, "PC": 0, "PMD": 1, "PR": 0},
        },
    ]

    rows = []

    # ----------------------------------------------------------------------
    # For each stage, run BN inference and update the same claim
    # ----------------------------------------------------------------------
    for idx, stage in enumerate(stages):
        label = stage["label"]
        evidence = stage["evidence"]
        log_bits = stage["log_bits"]

        print(f"\n=== {label} (stage {idx}) ===")
        print("Evidence used for BN inference:", evidence)

        # Off-chain BN inference with partial evidence
        posterior = bn.infer(evidence)
        pph = float(posterior["PPH"])
        ppr = float(posterior["PPR"])

        scaled_pph = int(round(pph * SCALE))
        scaled_ppr = int(round(ppr * SCALE))

        # Commit posterior to the same claimId
        tx = controller.functions.submitInference(
            int(claim_id),
            int(scaled_pph),
            int(scaled_ppr),
        ).transact({"from": sender})
        receipt = w3.eth.wait_for_transaction_receipt(tx)

        gas_used = int(receipt.gasUsed)
        block = w3.eth.get_block(receipt.blockNumber)
        ts = int(block["timestamp"])

        # Read back the updated claim
        claim_tuple = claim_registry.functions.claims(claim_id).call()
        c_pph = claim_tuple[2]
        c_ppr = claim_tuple[3]

        onchain_pph = c_pph / SCALE
        onchain_ppr = c_ppr / SCALE

        print(
            f"stage={idx} label={label} claim_id={claim_id} "
            f"PPH_off={pph:.4f} PPR_off={ppr:.4f} "
            f"PPH_on={onchain_pph:.4f} PPR_on={onchain_ppr:.4f} "
            f"gas_used={gas_used} ts={ts}"
        )

        rows.append(
            {
                "stage_index": idx,
                "stage_label": label,
                "claim_id": int(claim_id),
                "gps": int(log_bits["GPS"]),
                "pc": int(log_bits["PC"]),
                "pmd": int(log_bits["PMD"]),
                "pr": int(log_bits["PR"]),
                "posterior_pph": pph,
                "posterior_ppr": ppr,
                "onchain_pph": onchain_pph,
                "onchain_ppr": onchain_ppr,
                "gas_used": gas_used,
                "block_number": int(receipt.blockNumber),
                "timestamp": ts,
            }
        )

    # ----------------------------------------------------------------------
    # Append to CSV (do not overwrite)
    # ----------------------------------------------------------------------
    out_dir = ROOT / "experiments"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "claim_lifecycle_demo.csv"

    fieldnames = [
        "stage_index",
        "stage_label",
        "claim_id",
        "gps",
        "pc",
        "pmd",
        "pr",
        "posterior_pph",
        "posterior_ppr",
        "onchain_pph",
        "onchain_ppr",
        "gas_used",
        "block_number",
        "timestamp",
    ]

    file_exists = out_path.exists()

    with out_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)

    print(f"\nAppended {len(rows)} rows to {out_path.resolve()}")


if __name__ == "__main__":
    main()
