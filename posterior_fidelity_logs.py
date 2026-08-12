import csv
import itertools
import json
import os
from pathlib import Path
from typing import Any, Dict, Tuple

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3
from web3.contract import Contract

from bn_oracle import BNOracle, canonicalize_evidence_for_chain

load_dotenv()

ROOT = Path(__file__).resolve().parent
BUILD_DIR = ROOT / "deployment" / "build"

SEPOLIA_RPC_URL = os.environ["SEPOLIA_RPC_URL"]
PRIVATE_KEY = os.environ["PRIVATE_KEY"]
CPTSTORE_ADDR = Web3.to_checksum_address(os.environ["CPTSTORE_ADDR"])
CLAIMREGISTRY_ADDR = Web3.to_checksum_address(os.environ["CLAIMREGISTRY_ADDR"])
ORACLECONTROLLER_ADDR = Web3.to_checksum_address(os.environ["ORACLECONTROLLER_ADDR"])

PROFILE_NAME = os.environ.get("PROFILE_NAME", "neutral")
DECISION_TAU = 0.5
OUTPUT_CSV = ROOT / f"posterior_fidelity_{PROFILE_NAME}.csv"


def load_artifact(name: str) -> Dict[str, Any]:
    with open(BUILD_DIR / f"{name}.json", encoding="utf-8") as f:
        return json.load(f)


def load_contract(w3: Web3, name: str, address: str) -> Contract:
    return w3.eth.contract(address=address, abi=load_artifact(name)["abi"])


def send_tx(
    w3: Web3,
    fn,
    sender: str,
    private_key: str,
) -> Tuple[str, int, int]:
    nonce = w3.eth.get_transaction_count(sender, "pending")
    tx = fn.build_transaction(
        {
            "from": sender,
            "nonce": nonce,
            "chainId": w3.eth.chain_id,
            "gas": 800_000,
            "maxFeePerGas": w3.to_wei("30", "gwei"),
            "maxPriorityFeePerGas": w3.to_wei("2", "gwei"),
        }
    )
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    return tx_hash.hex(), int(receipt.gasUsed), int(receipt.blockNumber)


def is_decision_consistent(p_float: float, p_onchain: float, tau: float) -> bool:
    return (p_float >= tau) == (p_onchain >= tau)


def main() -> None:
    w3 = Web3(Web3.HTTPProvider(SEPOLIA_RPC_URL))
    if not w3.is_connected():
        raise RuntimeError("Web3 not connected to Sepolia")

    sender = Account.from_key(PRIVATE_KEY).address
    cpt_store = load_contract(w3, "CPTStore", CPTSTORE_ADDR)
    claim_reg = load_contract(w3, "ClaimRegistry", CLAIMREGISTRY_ADDR)
    oracle_ctrl = load_contract(w3, "OracleController", ORACLECONTROLLER_ADDR)

    rows = []

    for bits in itertools.product((0, 1), repeat=4):
        gps, pc, pmd, pr = bits
        evidence = {"GPS": gps, "PC": pc, "PMD": pmd, "PR": pr}

        claim_id = int(claim_reg.functions.nextClaimId().call())
        external_key = Web3.keccak(
            text=f"LUNA_FIDELITY_{PROFILE_NAME}_{gps}{pc}{pmd}{pr}_{claim_id}"
        )

        send_tx(
            w3,
            oracle_ctrl.functions.openClaim(external_key),
            sender,
            PRIVATE_KEY,
        )

        result = BNOracle.infer_from_chain(w3, cpt_store, evidence)
        pph_float = float(result["PPH"])
        ppr_float = float(result["PPR"])

        pph_enc = BNOracle.encode(pph_float)
        ppr_enc = BNOracle.encode(ppr_float)

        values, observed_mask = canonicalize_evidence_for_chain(evidence)
        bn_id_bytes = bytes.fromhex(result["bn_instance_id"][2:])

        send_tx(
            w3,
            oracle_ctrl.functions.submitInference(
                claim_id,
                values["GPS"],
                values["PC"],
                values["PMD"],
                values["PR"],
                pph_enc,
                ppr_enc,
                bn_id_bytes,
),
            sender,
            PRIVATE_KEY,
        )

        claim_tuple = claim_reg.functions.getClaim(claim_id).call()
        onchain_pph_enc = int(claim_tuple[3])
        onchain_ppr_enc = int(claim_tuple[4])

        onchain_pph = BNOracle.decode(onchain_pph_enc)
        onchain_ppr = BNOracle.decode(onchain_ppr_enc)

        delta_pph = abs(pph_float - onchain_pph)
        delta_ppr = abs(ppr_float - onchain_ppr)

        dc_pph = is_decision_consistent(pph_float, onchain_pph, DECISION_TAU)
        dc_ppr = is_decision_consistent(ppr_float, onchain_ppr, DECISION_TAU)

        rows.append(
            {
                "profile": PROFILE_NAME,
                "gps": gps,
                "pc": pc,
                "pmd": pmd,
                "pr": pr,
                "pph_float": pph_float,
                "ppr_float": ppr_float,
                "pph_encoded": pph_enc,
                "ppr_encoded": ppr_enc,
                "pph_onchain": onchain_pph,
                "ppr_onchain": onchain_ppr,
                "delta_pph": delta_pph,
                "delta_ppr": delta_ppr,
                "decision_consistent_pph": dc_pph,
                "decision_consistent_ppr": dc_ppr,
                "within_error_bound_pph": delta_pph <= BNOracle.rounding_error_bound(),
                "within_error_bound_ppr": delta_ppr <= BNOracle.rounding_error_bound(),
                "claim_id": claim_id,
                "bn_instance_id": result["bn_instance_id"],
                "snapshot_block": result["snapshot_block"],
                "observed_mask": observed_mask,
            }
        )

        print(
            f"[{gps}{pc}{pmd}{pr}] "
            f"PPH={pph_float:.6f}({pph_enc}) "
            f"PPR={ppr_float:.6f}({ppr_enc}) "
            f"delta_pph={delta_pph:.2e} "
            f"delta_ppr={delta_ppr:.2e}"
        )

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    max_delta = max(max(r["delta_pph"], r["delta_ppr"]) for r in rows)
    mean_delta = sum(r["delta_pph"] + r["delta_ppr"] for r in rows) / (2 * len(rows))
    inconsistent = sum(
        1
        for r in rows
        if not r["decision_consistent_pph"] or not r["decision_consistent_ppr"]
    )
    bound = BNOracle.rounding_error_bound()

    print(f"\n=== FIDELITY SUMMARY (profile={PROFILE_NAME}) ===")
    print(f"Posteriors evaluated : {2 * len(rows)}")
    print(f"Max |delta|          : {max_delta:.2e}  (bound: {bound:.2e})")
    print(f"Mean |delta|         : {mean_delta:.2e}")
    print(f"Decision inconsistent: {inconsistent}")
    print(f"Wrote {len(rows)} rows to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()