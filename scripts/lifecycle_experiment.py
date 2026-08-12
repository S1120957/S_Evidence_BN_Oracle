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

try:
    from web3.logs import DISCARD as LOG_DISCARD
except ImportError:
    LOG_DISCARD = "discard"

ROOT = Path.cwd()
OUT_CSV = ROOT / "results" / "lifecycle_gas_logs.csv"
RUN_DIR = Path(os.environ["SELENE_RUN_DIR"])
RUN_CSV = RUN_DIR / "lifecycle_gas_logs.csv"
MANIFEST = RUN_DIR / "asymmetric_bn_manifest.json"

RPC = os.environ["SEPOLIA_RPC_URL"]
PRIVATE_KEY = os.environ["PRIVATE_KEY"]

CPTSTORE_ADDR = Web3.to_checksum_address(os.environ["CPTSTORE_ADDR"])
CLAIMREGISTRY_ADDR = Web3.to_checksum_address(os.environ["CLAIMREGISTRY_ADDR"])
EVIDENCEREGISTRY_ADDR = Web3.to_checksum_address(os.environ["EVIDENCEREGISTRY_ADDR"])
ORACLECONTROLLER_ADDR = Web3.to_checksum_address(os.environ["ORACLECONTROLLER_ADDR"])

RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

ORDERINGS = {
    "canonical": ["GPS", "PC", "PMD", "PR"],
    "reverse":   ["PR", "PMD", "PC", "GPS"],
    "gapped":    ["PC", "PR", "GPS", "PMD"],
}

OBSERVED_VALUES = {
    "GPS": 1,
    "PC":  1,
    "PMD": 0,
    "PR":  1,
}

CPTS_ASYMMETRIC = {
    "GPS": { (0,0): 0.10, (0,1): 0.15, (1,0): 0.90, (1,1): 0.80 },
    "PC":  { (0,0): 0.15, (0,1): 0.20, (1,0): 0.85, (1,1): 0.75 },
    "PMD": { (0,0): 0.08, (0,1): 0.10, (1,0): 0.88, (1,1): 0.78 },
    "PR":  { (0,0): 0.20, (0,1): 0.25, (1,0): 0.80, (1,1): 0.70 },
}

from bn_oracle import BNOracle, canonicalize_evidence_for_chain, decode_chain_evidence, UNOBSERVED

def load_contract(w3, name, address):
    artifact = json.loads((ROOT / "deployment" / "build" / f"{name}.json").read_text(encoding="utf-8"))
    return w3.eth.contract(address=address, abi=artifact["abi"])

def send_tx(w3, fn, sender, private_key):
    nonce = w3.eth.get_transaction_count(sender, "pending")
    tx = fn.build_transaction({
        "from": sender,
        "nonce": nonce,
        "chainId": 11155111,
        "gas": 500000,
        "maxFeePerGas": w3.to_wei(50, "gwei"),
        "maxPriorityFeePerGas": w3.to_wei(3, "gwei"),
    })
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
    if receipt.status != 1:
        raise RuntimeError(f"Transaction reverted: {tx_hash.hex()}")
    return receipt

def read_state_synced(w3, claim_reg, claim_id, target_block):
    for _ in range(60):
        if w3.eth.block_number >= target_block:
            break
        time.sleep(2)
    return claim_reg.functions.getClaimState(claim_id).call(block_identifier=target_block)

def main():
    w3 = Web3(Web3.HTTPProvider(RPC))
    if not w3.is_connected():
        raise RuntimeError("RPC connection failed")

    sender = Web3.to_checksum_address(Account.from_key(PRIVATE_KEY).address)
    cpt_store = load_contract(w3, "CPTStore", CPTSTORE_ADDR)
    claim_reg = load_contract(w3, "ClaimRegistry", CLAIMREGISTRY_ADDR)
    evidence_reg = load_contract(w3, "EvidenceRegistry", EVIDENCEREGISTRY_ADDR)
    oracle_ctrl = load_contract(w3, "OracleController", ORACLECONTROLLER_ADDR)

    if w3.eth.chain_id != 11155111:
        raise RuntimeError(f"Wrong chain id: {w3.eth.chain_id}")

    if not MANIFEST.exists():
        raise RuntimeError(f"Missing frozen BN manifest: {MANIFEST}")

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected_addresses = manifest["contracts"]
    actual_addresses = {
        "CPTStore": CPTSTORE_ADDR,
        "ClaimRegistry": CLAIMREGISTRY_ADDR,
        "EvidenceRegistry": EVIDENCEREGISTRY_ADDR,
        "OracleController": ORACLECONTROLLER_ADDR,
    }

    for name, actual in actual_addresses.items():
        if actual != Web3.to_checksum_address(expected_addresses[name]):
            raise RuntimeError(f"Deployment mismatch for {name}")

    frozen = cpt_store.functions.getCPTSnapshot().call()
    frozen_bn_id = "0x" + bytes(frozen[3]).hex()

    if int(frozen[0]) != int(manifest["priors_scaled"]["PPH"]):
        raise RuntimeError("PPH prior differs from frozen asymmetric manifest")
    if int(frozen[1]) != int(manifest["priors_scaled"]["PPR"]):
        raise RuntimeError("PPR differs from frozen asymmetric manifest")
    if [int(x) for x in frozen[2]] != [int(x) for x in manifest["cpts_flat_scaled"]]:
        raise RuntimeError("CPT entries differ from frozen asymmetric manifest")
    if frozen_bn_id.lower() != manifest["bn_instance_id"].lower():
        raise RuntimeError("bnInstanceId differs from frozen asymmetric manifest")

    print(f"Sender : {sender}")
    
    prior_pph = int(manifest["priors_scaled"]["PPH"]) / 1_000_000.0
    prior_ppr = int(manifest["priors_scaled"]["PPR"]) / 1_000_000.0
    oracle = BNOracle(prior_pph=prior_pph, prior_ppr=prior_ppr, cpts=CPTS_ASYMMETRIC)

    rows: List[Dict[str, Any]] = []
    claim_ids: Dict[str, int] = {}

    print("=== Opening all three claims before any inference ===")
    for claim_label, order in ORDERINGS.items():
        claim_id = int(claim_reg.functions.nextClaimId().call())
        key = Web3.keccak(text=f"SELENE_LIFECYCLE_{RUN_ID}_{claim_label}")
        r = send_tx(w3, oracle_ctrl.functions.openClaim(key), sender, PRIVATE_KEY)
        open_gas = int(r.gasUsed)
        open_block = int(r.blockNumber)

        state = read_state_synced(w3, claim_reg, claim_id, open_block)
        if state != 1:
            raise RuntimeError(f"claim {claim_id} state={state}, expected Open")

        claim_ids[claim_label] = claim_id
        print(f"  {claim_label:10s} claimId={claim_id} gas={open_gas:,}")

        rows.append({
            "run_id": RUN_ID, "claim_label": claim_label, "claim_id": claim_id,
            "stage": 0, "operation": "openClaim", "n_observed": 0,
            "evidence_order": "|".join(order), "gps_chain": "", "pc_chain": "",
            "pmd_chain": "", "pr_chain": "", "gas_used": open_gas, "block_number": open_block,
            "pph_offchain": prior_pph, "ppr_offchain": prior_ppr,
            "pph_onchain": "", "ppr_onchain": "", "auditor_pph": "", "auditor_delta_pph": "",
            "tx_hash": r.transactionHash.hex(), "notes": "stage0_prior",
        })

    concurrency_block = int(w3.eth.block_number)
    for claim_label, claim_id in claim_ids.items():
        state = read_state_synced(w3, claim_reg, claim_id, concurrency_block)
        if int(state) != 1:
            raise RuntimeError(f"Concurrency check failed: {claim_label} state={state}")

    print(f"Concurrency check: PASS at block {concurrency_block} (3/3 Open)\n")

    for claim_label, order in ORDERINGS.items():
        claim_id = claim_ids[claim_label]
        print(f"=== Claim '{claim_label}'  order={' -> '.join(order)} ===")

        current_evidence = {}
        for stage, var_name in enumerate(order, start=1):
            current_evidence[var_name] = OBSERVED_VALUES[var_name]
            result = oracle.infer(current_evidence)

            pph = float(result["PPH"])
            ppr = float(result["PPR"])
            pph_enc = int(round(pph * 1_000_000))
            ppr_enc = int(round(ppr * 1_000_000))

            chain_ev = canonicalize_evidence_for_chain(current_evidence)

            r = send_tx(
                w3,
                oracle_ctrl.functions.submitInference(
                    claim_id,
                    chain_ev["GPS"], chain_ev["PC"], chain_ev["PMD"], chain_ev["PR"],
                    pph_enc, ppr_enc,
                    bytes.fromhex(frozen_bn_id[2:])
                ),
                sender,
                PRIVATE_KEY
            )

            gas = int(r.gasUsed)
            blk = int(r.blockNumber)

            try:
                event_logs = oracle_ctrl.events.InferenceSubmitted().process_receipt(r, errors=LOG_DISCARD)
            except TypeError:
                event_logs = oracle_ctrl.events.InferenceSubmitted().process_receipt(r)

            if len(event_logs) != 1:
                raise RuntimeError("Expected exactly one InferenceSubmitted event")

            event_args = event_logs[0]["args"]
            evidence_id = int(event_args["evidenceId"])
            event_bn_id = "0x" + bytes(event_args["bnInstanceId"]).hex()

            if event_bn_id.lower() != frozen_bn_id.lower():
                raise RuntimeError("InferenceSubmitted bnInstanceId mismatch")

            rec = evidence_reg.functions.getEvidence(evidence_id).call(block_identifier=blk)
            rec_gps, rec_pc, rec_pmd, rec_pr = int(rec[1]), int(rec[2]), int(rec[3]), int(rec[4])

            record_match = (
                int(rec[0]) == claim_id and
                (rec_gps, rec_pc, rec_pmd, rec_pr) == (chain_ev["GPS"], chain_ev["PC"], chain_ev["PMD"], chain_ev["PR"]) and
                int(rec[5]) == pph_enc and int(rec[6]) == ppr_enc and
                Web3.to_checksum_address(rec[7]) == sender and int(rec[8]) == blk
            )

            if not record_match:
                raise RuntimeError(f"EvidenceRegistry read-back mismatch for claim {claim_id}, stage {stage}")

            audit_ev = decode_chain_evidence(rec_gps, rec_pc, rec_pmd, rec_pr)
            audit_res = oracle.infer(audit_ev)
            audit_pph = float(audit_res["PPH"])
            audit_ppr = float(audit_res["PPR"])
            audit_dpph = abs(audit_pph - pph)
            audit_dppr = abs(audit_ppr - ppr)

            pph_onchain = pph_enc / 1_000_000.0
            ppr_onchain = ppr_enc / 1_000_000.0

            print(f"  stage {stage} (+{var_name:3s})  gas={gas:,}  PPH={pph:.4f}  PPR={ppr:.4f}")

            rows.append({
                "run_id": RUN_ID, "claim_label": claim_label, "claim_id": claim_id,
                "stage": stage, "operation": "submitInference", "n_observed": stage,
                "evidence_order": "|".join(order),
                "gps_chain": chain_ev["GPS"], "pc_chain": chain_ev["PC"],
                "pmd_chain": chain_ev["PMD"], "pr_chain": chain_ev["PR"],
                "gas_used": gas, "block_number": blk,
                "pph_offchain": pph, "ppr_offchain": ppr,
                "pph_onchain": pph_onchain, "ppr_onchain": ppr_onchain,
                "auditor_pph": audit_pph, "auditor_delta_pph": audit_dpph,
                "auditor_ppr": audit_ppr, "auditor_delta_ppr": audit_dppr,
                "evidence_id": evidence_id, "snapshot_block": manifest["snapshot_block"],
                "record_match": record_match, "tx_hash": r.transactionHash.hex(), "notes": "",
            })

        r = send_tx(w3, oracle_ctrl.functions.finalizeClaim(claim_id), sender, PRIVATE_KEY)
        fin_gas = int(r.gasUsed)
        fin_block = int(r.blockNumber)
        fin_state = read_state_synced(w3, claim_reg, claim_id, fin_block)

        if int(fin_state) != 2:
            raise RuntimeError(f"claim {claim_id} final state={fin_state}, expected Finalized")

        print(f"  finalizeClaim gas={fin_gas:,} state={fin_state}")

        rows.append({
            "run_id": RUN_ID, "claim_label": claim_label, "claim_id": claim_id,
            "stage": 5, "operation": "finalizeClaim", "n_observed": 4,
            "evidence_order": "|".join(order),
            "gps_chain": "", "pc_chain": "", "pmd_chain": "", "pr_chain": "",
            "gas_used": fin_gas, "block_number": fin_block,
            "pph_offchain": "", "ppr_offchain": "", "pph_onchain": "", "ppr_onchain": "",
            "auditor_pph": "", "auditor_delta_pph": "",
            "tx_hash": r.transactionHash.hex(), "notes": "",
        })

        reason = None
        try:
            chain_ev = canonicalize_evidence_for_chain(OBSERVED_VALUES)
            oracle_ctrl.functions.submitInference(
                claim_id,
                chain_ev["GPS"], chain_ev["PC"], chain_ev["PMD"], chain_ev["PR"],
                500000, 500000,
                bytes.fromhex(frozen_bn_id[2:])
            ).call({"from": sender})
        except Exception as ex:
            reason = str(ex)

        reverted = reason is not None
        if not reverted:
            raise RuntimeError(f"post-finalization submit did NOT revert for {claim_label}")
        if "claim not open" not in reason.lower():
            raise RuntimeError(f"Unexpected post-finalization revert reason: {reason}")

        print(f"  post-finalization submit reverted: {reverted}")
        short = reason.split("revert")[-1].strip()[:70]
        print(f"    reason: {short}")

        rows.append({
            "run_id": RUN_ID, "claim_label": claim_label, "claim_id": claim_id,
            "stage": 6, "operation": "postFinalizeSubmit_expectRevert", "n_observed": 4,
            "evidence_order": "|".join(order),
            "gps_chain": "", "pc_chain": "", "pmd_chain": "", "pr_chain": "",
            "gas_used": 0, "block_number": int(w3.eth.block_number),
            "pph_offchain": "", "ppr_offchain": "", "pph_onchain": "", "ppr_onchain": "",
            "auditor_pph": "", "auditor_delta_pph": "",
            "reverted": reverted, "revert_reason": (reason or "")[:500],
            "tx_hash": "", "notes": f"reverted={reverted}; {(reason or 'NO REVERT')[:120]}",
        })

    if len(rows) != 21:
        raise RuntimeError(f"Expected 21 lifecycle rows, got {len(rows)}")

    for row in rows:
        row["deployment_id"] = ORACLECONTROLLER_ADDR
        row["cptstore_address"] = CPTSTORE_ADDR
        row["claimregistry_address"] = CLAIMREGISTRY_ADDR
        row["evidenceregistry_address"] = EVIDENCEREGISTRY_ADDR
        row["oraclecontroller_address"] = ORACLECONTROLLER_ADDR
        row["bn_instance_id"] = frozen_bn_id
        row["concurrency_verified"] = True
        row["concurrency_block"] = concurrency_block
        row.setdefault("auditor_ppr", "")
        row.setdefault("auditor_delta_ppr", "")
        row.setdefault("evidence_id", "")
        row.setdefault("snapshot_block", "")
        row.setdefault("record_match", "")
        row.setdefault("reverted", "")
        row.setdefault("revert_reason", "")

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys())

    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        wri = csv.DictWriter(f, fieldnames=fields)
        wri.writeheader()
        wri.writerows(rows)

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(OUT_CSV, RUN_CSV)

    subs = [r for r in rows if r["operation"] == "submitInference"]
    opens = [r for r in rows if r["operation"] == "openClaim"]
    fins  = [r for r in rows if r["operation"] == "finalizeClaim"]
    revs  = [r for r in rows if r["operation"] == "postFinalizeSubmit_expectRevert"]

    audit_max = max(float(r["auditor_delta_pph"]) for r in subs)
    audit_max_ppr = max(float(r["auditor_delta_ppr"]) for r in subs)
    record_ok = sum(1 for r in subs if bool(r["record_match"]))

    open_gases = [int(r["gas_used"]) for r in opens]
    sub_gases  = [int(r["gas_used"]) for r in subs]
    fin_gases  = [int(r["gas_used"]) for r in fins]
    n_rev = len(revs)

    print("\n============================================")
    print("LIFECYCLE SUMMARY")
    print("============================================")
    print(f"  openClaim       : n={len(opens)}  mean={statistics.mean(open_gases):.0f} gas")
    print(f"  submitInference : n={len(subs)}  mean={statistics.mean(sub_gases):.0f} gas")
    print(f"  finalizeClaim   : n={len(fins)}  mean={statistics.mean(fin_gases):.0f} gas")
    print(f"  EvidenceRegistry read-back: {record_ok}/{len(subs)} matched")
    print(f"  max auditor recomputation delta PPH: {audit_max:.2e}")
    print(f"  max auditor recomputation delta PPR: {audit_max_ppr:.2e}")

    final_posts = {}
    for r in subs:
        if r["stage"] == 4:
            final_posts[r["claim_label"]] = (r["pph_onchain"], r["ppr_onchain"])

    pairs = set(final_posts.values())
    print(f"  order-invariance: {'CONFIRMED' if len(pairs) == 1 else 'FAILED'} ({len(pairs)} distinct posterior pairs)")
    print(f"  absorbing Finalized state: {n_rev}/3 post-finalization submits reverted")
    print(f"\n  Wrote {len(rows)} rows -> {OUT_CSV}")
    print(f"  Run copy -> {RUN_CSV}")

    if record_ok != len(subs):
        raise RuntimeError("Not all EvidenceRegistry records matched")
    if audit_max > 1e-15 or audit_max_ppr > 1e-15:
        raise RuntimeError("Auditor recomputation delta exceeded 1e-15")
    if n_rev != 3:
        raise RuntimeError(f"Expected 3/3 post-finalization reverts, got {n_rev}/3")

    print("\nLIFECYCLE EXPERIMENT: PASS")

if __name__ == "__main__":
    main()

