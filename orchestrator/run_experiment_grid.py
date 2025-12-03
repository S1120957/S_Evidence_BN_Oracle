"""Grid experiment over all (GPS, PC, PMD, PR) evidence combinations.

New claim-based pipeline:

1. Read priors + CPTs from CPTStore and rebuild the BN off-chain.
2. For each of the 16 binary evidence patterns (GPS, PC, PMD, PR):

   a) Create a synthetic claim key (bytes32) and open a claim in ClaimRegistry.
   b) Compute posterior P(PPH | evidence), P(PPR | evidence) off-chain.
   c) Scale posteriors and call OracleController.submitInference(claimId, scaledPPH, scaledPPR).
   d) Read back the stored posterior from ClaimRegistry.claims(claimId).
   e) Log gasUsed, timestamp, etc.

3. Write all results to experiments/batch_inference_grid.csv.
"""

from __future__ import annotations

import csv
import json
from itertools import product
from pathlib import Path
import sys

from web3 import Web3

# Project root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.bn_from_chain import build_bn_from_chain  # noqa: E402

GANACHE_URL = "http://127.0.0.1:7545"
NETWORK_ID = "5777"  # Ganache network id
SCALE = 1_000_000    # must match CPTStore.SCALE


def _find_build_artifact(name: str) -> dict:
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


def main() -> None:
    # ------------------------------------------------------------------
    # Web3 + accounts
    # ------------------------------------------------------------------
    w3 = Web3(Web3.HTTPProvider(GANACHE_URL))
    if not w3.is_connected():
        raise RuntimeError(f"Web3 not connected to Ganache at {GANACHE_URL}")

    accounts = w3.eth.accounts
    if not accounts:
        raise RuntimeError("No accounts available in Ganache")
    # Use the first account as the actor in this experiment
    sender = accounts[0]

    # ------------------------------------------------------------------
    # Off-chain BN from CPTStore
    # ------------------------------------------------------------------
    bn, priors, cpts = build_bn_from_chain(w3)
    print("Using priors:", priors)

    # ------------------------------------------------------------------
    # On-chain contracts: OracleController + ClaimRegistry
    # ------------------------------------------------------------------
    controller = _load_contract(w3, "OracleController")
    claim_registry = _load_contract(w3, "ClaimRegistry")

    # ------------------------------------------------------------------
    # Prepare CSV output
    # ------------------------------------------------------------------
    out_dir = ROOT / "experiments"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "batch_inference_grid.csv"

    rows = []
    evidence_id = 0  # local index [0..15]

    # ------------------------------------------------------------------
    # Main 2x2x2x2 grid over (GPS, PC, PMD, PR)
    # ------------------------------------------------------------------
    for gps, pc, pmd, pr in product((0, 1), repeat=4):
        # 1) Ask the contract what claimId it will assign for this openClaim
        claim_key = Web3.keccak(text=f"CLAIM_{evidence_id}")
        claim_id = claim_registry.functions.openClaim(claim_key).call({"from": sender})

        # 2) Actually open the claim on-chain
        tx_open = claim_registry.functions.openClaim(claim_key).transact({"from": sender})
        w3.eth.wait_for_transaction_receipt(tx_open)

        # Optional debug: confirm the claim exists and is in the expected state
        try:
            _claim_tuple = claim_registry.functions.claims(claim_id).call()
            # print("Opened claim:", claim_id, "raw:", _claim_tuple)
        except Exception as e:
            print(f"Warning: failed to read claim {claim_id} after openClaim:", e)

        # 3) Off-chain BN inference
        evidence = {"GPS": gps, "PC": pc, "PMD": pmd, "PR": pr}
        posterior = bn.infer(evidence)
        pph = float(posterior["PPH"])
        ppr = float(posterior["PPR"])

        scaled_pph = int(round(pph * SCALE))
        scaled_ppr = int(round(ppr * SCALE))

        # 4) Commit posterior on-chain for this claim
        tx = controller.functions.submitInference(
            int(claim_id),
            int(scaled_pph),
            int(scaled_ppr),
        ).transact({"from": sender})
        receipt = w3.eth.wait_for_transaction_receipt(tx)

        gas_used = int(receipt.gasUsed)
        block = w3.eth.get_block(receipt.blockNumber)
        ts = int(block["timestamp"])

        # 5) Read back stored posterior from ClaimRegistry.claims(claimId)
        #    Assuming struct: (bytes32 externalKey, uint8 state,
        #                      uint256 posteriorPPH, uint256 posteriorPPR, ...)
        claim_tuple = claim_registry.functions.claims(claim_id).call()
        c_pph = claim_tuple[2]  # adjust index if your struct layout differs
        c_ppr = claim_tuple[3]

        onchain_pph = c_pph / SCALE
        onchain_ppr = c_ppr / SCALE

        print(
            f"evidence_id={evidence_id} claim_id={claim_id} "
            f"(GPS={gps},PC={pc},PMD={pmd},PR={pr}) "
            f"posterior_pph={pph:.4f} posterior_ppr={ppr:.4f} "
            f"onchain_pph={onchain_pph:.4f} onchain_ppr={onchain_ppr:.4f} "
            f"gas_used={gas_used} ts={ts}"
        )

        rows.append(
            {
                "evidence_id": evidence_id,
                "claim_id": int(claim_id),
                "gps": int(gps),
                "pc": int(pc),
                "pmd": int(pmd),
                "pr": int(pr),
                "posterior_pph": pph,
                "posterior_ppr": ppr,
                "onchain_pph": onchain_pph,
                "onchain_ppr": onchain_ppr,
                "gas_used": gas_used,
                "block_number": int(receipt.blockNumber),
                "timestamp": ts,
            }
        )

        evidence_id += 1

    # ------------------------------------------------------------------
    # Write CSV
    # ------------------------------------------------------------------
        # ------------------------------------------------------------------
    # Append to CSV instead of overwriting
    # ------------------------------------------------------------------
    out_dir = ROOT / "experiments"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "batch_inference_grid.csv"

    fieldnames = [
        "evidence_id",
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
