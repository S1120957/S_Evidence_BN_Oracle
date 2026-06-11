import csv
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
EVIDENCEREGISTRY_ADDR = Web3.to_checksum_address(os.environ["EVIDENCEREGISTRY_ADDR"])
ORACLECONTROLLER_ADDR = Web3.to_checksum_address(os.environ["ORACLECONTROLLER_ADDR"])

OUTPUT_CSV = ROOT / "sepolia_gas_logs.csv"
NETWORK_NAME = "Sepolia"
BN_SIZE_CONFIG = 4


def load_artifact(name: str) -> Dict[str, Any]:
    with open(BUILD_DIR / f"{name}.json", encoding="utf-8") as f:
        return json.load(f)


def load_contract(w3: Web3, name: str, address: str) -> Contract:
    artifact = load_artifact(name)
    return w3.eth.contract(address=address, abi=artifact["abi"])


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


def write_rows(rows: list) -> None:
    file_exists = OUTPUT_CSV.exists()
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "network",
                "bn_size_config",
                "transaction_type",
                "transaction_hash",
                "gas_used",
                "block_number",
                "claim_id",
                "profile",
                "notes",
            ],
        )
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def neutral_profile():
    priors = (500_000, 500_000)
    cpts = [
        (ev_idx, pph, ppr, 500_000)
        for ev_idx in range(4)
        for pph in (0, 1)
        for ppr in (0, 1)
    ]
    return priors, cpts


def asymmetric_profile():
    priors = (300_000, 700_000)
    cpts = [
        (0, 0, 0, 100_000), (0, 0, 1, 150_000),
        (0, 1, 0, 900_000), (0, 1, 1, 800_000),
        (1, 0, 0, 150_000), (1, 0, 1, 200_000),
        (1, 1, 0, 850_000), (1, 1, 1, 750_000),
        (2, 0, 0, 80_000),  (2, 0, 1, 100_000),
        (2, 1, 0, 880_000), (2, 1, 1, 780_000),
        (3, 0, 0, 200_000), (3, 0, 1, 250_000),
        (3, 1, 0, 800_000), (3, 1, 1, 700_000),
    ]
    return priors, cpts


def main() -> None:
    w3 = Web3(Web3.HTTPProvider(SEPOLIA_RPC_URL))
    if not w3.is_connected():
        raise RuntimeError("Web3 not connected to Sepolia")

    sender = Account.from_key(PRIVATE_KEY).address
    cpt_store = load_contract(w3, "CPTStore", CPTSTORE_ADDR)
    claim_reg = load_contract(w3, "ClaimRegistry", CLAIMREGISTRY_ADDR)
    oracle_ctrl = load_contract(w3, "OracleController", ORACLECONTROLLER_ADDR)

    rows = []

    for profile_name, profile_fn in [
        ("neutral", neutral_profile),
        ("asymmetric", asymmetric_profile),
    ]:
        priors, cpts = profile_fn()

        tx_hash, gas_used, block_number = send_tx(
            w3,
            cpt_store.functions.setPriors(*priors),
            sender,
            PRIVATE_KEY,
        )
        rows.append(
            {
                "network": NETWORK_NAME,
                "bn_size_config": BN_SIZE_CONFIG,
                "transaction_type": "CPTStore_init",
                "transaction_hash": tx_hash,
                "gas_used": gas_used,
                "block_number": block_number,
                "claim_id": "",
                "profile": profile_name,
                "notes": "setPriors",
            }
        )

        for ev_idx, pph, ppr, p_true in cpts:
            tx_hash, gas_used, block_number = send_tx(
                w3,
                cpt_store.functions.setEvidenceCPT(ev_idx, pph, ppr, p_true),
                sender,
                PRIVATE_KEY,
            )
            rows.append(
                {
                    "network": NETWORK_NAME,
                    "bn_size_config": BN_SIZE_CONFIG,
                    "transaction_type": "CPTStore_init",
                    "transaction_hash": tx_hash,
                    "gas_used": gas_used,
                    "block_number": block_number,
                    "claim_id": "",
                    "profile": profile_name,
                    "notes": f"setEvidenceCPT({ev_idx},{pph},{ppr})",
                }
            )

    priors_n, cpts_n = neutral_profile()
    send_tx(w3, cpt_store.functions.setPriors(*priors_n), sender, PRIVATE_KEY)
    for ev_idx, pph, ppr, p_true in cpts_n:
        send_tx(
            w3,
            cpt_store.functions.setEvidenceCPT(ev_idx, pph, ppr, p_true),
            sender,
            PRIVATE_KEY,
        )

    evidence = {"GPS": 1, "PC": 0, "PMD": 1, "PR": 0}

    for i in range(20):
        external_key = Web3.keccak(text=f"LUNA_SEP_CLAIM_{i}")
        claim_id = int(claim_reg.functions.nextClaimId().call())

        tx_hash, gas_used, block_number = send_tx(
            w3,
            oracle_ctrl.functions.openClaim(external_key),
            sender,
            PRIVATE_KEY,
        )
        rows.append(
            {
                "network": NETWORK_NAME,
                "bn_size_config": BN_SIZE_CONFIG,
                "transaction_type": "openClaim",
                "transaction_hash": tx_hash,
                "gas_used": gas_used,
                "block_number": block_number,
                "claim_id": claim_id,
                "profile": "neutral",
                "notes": f"run_{i}",
            }
        )

        result = BNOracle.infer_from_chain(w3, cpt_store, evidence)
        pph_enc = BNOracle.encode(result["PPH"])
        ppr_enc = BNOracle.encode(result["PPR"])

        values, observed_mask = canonicalize_evidence_for_chain(evidence)
        bn_id_bytes = bytes.fromhex(result["bn_instance_id"][2:])

        tx_hash, gas_used, block_number = send_tx(
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
        rows.append(
            {
                "network": NETWORK_NAME,
                "bn_size_config": BN_SIZE_CONFIG,
                "transaction_type": "submitInference",
                "transaction_hash": tx_hash,
                "gas_used": gas_used,
                "block_number": block_number,
                "claim_id": claim_id,
                "profile": "neutral",
                "notes": f"run_{i}_snapshotBlock_{result['snapshot_block']}_mask_{observed_mask}",
            }
        )

    write_rows(rows)
    print(f"Wrote {len(rows)} rows to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()