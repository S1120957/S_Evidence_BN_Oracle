import csv
import os
import json
import shutil
import statistics
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List

warnings.filterwarnings("ignore")

from eth_account import Account
from web3 import Web3

ROOT = Path.cwd()
OUT_CSV = ROOT / "results" / "sepolia_gas_logs.csv"
RUN_DIR = Path(os.environ["SELENE_RUN_DIR"])
RUN_CSV = RUN_DIR / "sepolia_gas_logs.csv"
MANIFEST = RUN_DIR / "asymmetric_bn_manifest.json"

RPC = os.environ["SEPOLIA_RPC_URL"]
PRIVATE_KEY = os.environ["PRIVATE_KEY"]

CPTSTORE_ADDR = Web3.to_checksum_address(os.environ["CPTSTORE_ADDR"])
CLAIMREGISTRY_ADDR = Web3.to_checksum_address(os.environ["CLAIMREGISTRY_ADDR"])
EVIDENCEREGISTRY_ADDR = Web3.to_checksum_address(os.environ["EVIDENCEREGISTRY_ADDR"])
ORACLECONTROLLER_ADDR = Web3.to_checksum_address(os.environ["ORACLECONTROLLER_ADDR"])

RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

NEUTRAL_PRIORS = {"PPH": 0.5, "PPR": 0.5}
NEUTRAL_CPTS = {
    "GPS": {(0,0): 0.5, (0,1): 0.5, (1,0): 0.5, (1,1): 0.5},
    "PC":  {(0,0): 0.5, (0,1): 0.5, (1,0): 0.5, (1,1): 0.5},
    "PMD": {(0,0): 0.5, (0,1): 0.5, (1,0): 0.5, (1,1): 0.5},
    "PR":  {(0,0): 0.5, (0,1): 0.5, (1,0): 0.5, (1,1): 0.5},
}

ASYM_PRIORS = {"PPH": 0.3, "PPR": 0.7}
ASYM_CPTS = {
    "GPS": {(0,0): 0.10, (0,1): 0.15, (1,0): 0.90, (1,1): 0.80},
    "PC":  {(0,0): 0.15, (0,1): 0.20, (1,0): 0.85, (1,1): 0.75},
    "PMD": {(0,0): 0.08, (0,1): 0.10, (1,0): 0.88, (1,1): 0.78},
    "PR":  {(0,0): 0.20, (0,1): 0.25, (1,0): 0.80, (1,1): 0.70},
}

from bn_oracle import BNOracle, canonicalize_evidence_for_chain

VAR_IDS = {"GPS": 0, "PC": 1, "PMD": 2, "PR": 3}

def load_contract(w3, name, address):
    artifact = json.loads((ROOT / "deployment" / "build" / f"{name}.json").read_text(encoding="utf-8"))
    return w3.eth.contract(address=address, abi=artifact["abi"])

def send_tx(w3, fn, sender, private_key):
    nonce = w3.eth.get_transaction_count(sender, "pending")
    tx = fn.build_transaction({
        "from": sender,
        "nonce": nonce,
        "chainId": 11155111,
        "gas": 800000,
        "maxFeePerGas": w3.to_wei(50, "gwei"),
        "maxPriorityFeePerGas": w3.to_wei(3, "gwei"),
    })
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
    if receipt.status != 1:
        raise RuntimeError(f"Transaction reverted: {tx_hash.hex()}")
    return receipt

def set_profile_on_chain(w3, cpt_store, sender, private_key, profile_name, priors, cpts):
    logs = []
    pph_s = int(round(priors["PPH"] * 1_000_000))
    ppr_s = int(round(priors["PPR"] * 1_000_000))
    
    r = send_tx(w3, cpt_store.functions.setPriors(pph_s, ppr_s), sender, private_key)
    logs.append({
        "transaction_type": "CPTStore_init", "op_sub_type": "setPriors",
        "profile": profile_name, "bn_size_config": "4",
        "gas_used": int(r.gasUsed), "block_number": int(r.blockNumber),
        "transaction_hash": r.transactionHash.hex(),
    })

    var_order = ["GPS", "PC", "PMD", "PR"]
    parent_states = [(0,0), (0,1), (1,0), (1,1)]

    for var in var_order:
        var_id = VAR_IDS[var]
        cpt = cpts[var]
        for pph_state, ppr_state in parent_states:
            prob_scaled = int(round(cpt[(pph_state, ppr_state)] * 1_000_000))
            r = send_tx(
                w3,
                cpt_store.functions.setEvidenceCPT(var_id, pph_state, ppr_state, prob_scaled),
                sender,
                private_key
            )
            logs.append({
                "transaction_type": "CPTStore_init", "op_sub_type": f"setCPT_{var}_{pph_state}{ppr_state}",
                "profile": profile_name, "bn_size_config": "4",
                "gas_used": int(r.gasUsed), "block_number": int(r.blockNumber),
                "transaction_hash": r.transactionHash.hex(),
            })

    return logs

def main():
    w3 = Web3(Web3.HTTPProvider(RPC))
    if not w3.is_connected():
        raise RuntimeError("RPC connection failed")

    sender = Web3.to_checksum_address(Account.from_key(PRIVATE_KEY).address)
    cpt_store = load_contract(w3, "CPTStore", CPTSTORE_ADDR)
    claim_reg = load_contract(w3, "ClaimRegistry", CLAIMREGISTRY_ADDR)
    evidence_reg = load_contract(w3, "EvidenceRegistry", EVIDENCEREGISTRY_ADDR)
    oracle_ctrl = load_contract(w3, "OracleController", ORACLECONTROLLER_ADDR)

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected_bn_id = manifest["bn_instance_id"]

    print(f"Sender: {sender}")
    rows = []

    # 1. Neutral Init (17 tx: 1 setPriors + 16 setEvidenceCPT)
    print("=== Initializing Neutral Profile (17 tx) ===")
    rows.extend(set_profile_on_chain(w3, cpt_store, sender, PRIVATE_KEY, "neutral", NEUTRAL_PRIORS, NEUTRAL_CPTS))

    # 2. Asymmetric Init (17 tx: 1 setPriors + 16 setEvidenceCPT)
    print("=== Initializing Asymmetric Profile (17 tx) ===")
    rows.extend(set_profile_on_chain(w3, cpt_store, sender, PRIVATE_KEY, "asymmetric", ASYM_PRIORS, ASYM_CPTS))

    # 3. Per-Query Experiment (40 tx: 20 openClaim + 20 submitInference)
    print("=== Executing 20 Per-Query Runs under Asymmetric Profile (40 tx) ===")
    asym_oracle = BNOracle(prior_pph=0.3, prior_ppr=0.7, cpts=ASYM_CPTS)
    evidence_sample = {"GPS": 1, "PC": 1, "PMD": 0, "PR": 1}
    chain_ev = canonicalize_evidence_for_chain(evidence_sample)
    inf_res = asym_oracle.infer(evidence_sample)
    pph_enc = int(round(float(inf_res["PPH"]) * 1_000_000))
    ppr_enc = int(round(float(inf_res["PPR"]) * 1_000_000))

    snap = cpt_store.functions.getCPTSnapshot().call()
    bn_bytes = bytes(snap[3])
    bn_id_str = "0x" + bn_bytes.hex()

    if bn_id_str.lower() != expected_bn_id.lower():
        raise RuntimeError(f"BN ID mismatch after asymmetric set: {bn_id_str} != {expected_bn_id}")

    for i in range(1, 21):
        claim_id = int(claim_reg.functions.nextClaimId().call())
        key = Web3.keccak(text=f"SELENE_GAS_{RUN_ID}_{i}")
        
        # openClaim
        r_open = send_tx(w3, oracle_ctrl.functions.openClaim(key), sender, PRIVATE_KEY)
        print(f"  [{i:02d}/20] openClaim claimId={claim_id} gas={int(r_open.gasUsed):,}")
        rows.append({
            "transaction_type": "openClaim", "op_sub_type": "openClaim",
            "profile": "asymmetric", "bn_size_config": "4",
            "gas_used": int(r_open.gasUsed), "block_number": int(r_open.blockNumber),
            "transaction_hash": r_open.transactionHash.hex(),
        })

        # submitInference
        r_sub = send_tx(w3, oracle_ctrl.functions.submitInference(
            claim_id, chain_ev["GPS"], chain_ev["PC"], chain_ev["PMD"], chain_ev["PR"],
            pph_enc, ppr_enc, bn_bytes
        ), sender, PRIVATE_KEY)
        print(f"        submitInference gas={int(r_sub.gasUsed):,}")
        rows.append({
            "transaction_type": "submitInference", "op_sub_type": "submitInference",
            "profile": "asymmetric", "bn_size_config": "4",
            "gas_used": int(r_sub.gasUsed), "block_number": int(r_sub.blockNumber),
            "transaction_hash": r_sub.transactionHash.hex(),
        })

    # Add provenance fields
    for row in rows:
        row["deployment_id"] = ORACLECONTROLLER_ADDR
        row["cptstore_address"] = CPTSTORE_ADDR
        row["claimregistry_address"] = CLAIMREGISTRY_ADDR
        row["evidenceregistry_address"] = EVIDENCEREGISTRY_ADDR
        row["oraclecontroller_address"] = ORACLECONTROLLER_ADDR
        row["bn_instance_id"] = bn_id_str
        row["run_id"] = RUN_ID

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys())

    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        wri = csv.DictWriter(f, fieldnames=fields)
        wri.writeheader()
        wri.writerows(rows)

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(OUT_CSV, RUN_CSV)

    # 4. Final Asymmetric Reset (17 tx) — Not recorded in CSV per measurement design
    print("=== Executing Final Asymmetric Reset (17 tx - unrecorded) ===")
    set_profile_on_chain(w3, cpt_store, sender, PRIVATE_KEY, "asymmetric", ASYM_PRIORS, ASYM_CPTS)

    print(f"\nSEPOLIA GAS EXPERIMENT COMPLETE: 91 total transactions sent.")
    print(f"Wrote {len(rows)} recorded rows -> {OUT_CSV}")
    print(f"Run copy -> {RUN_CSV}")

if __name__ == "__main__":
    main()

