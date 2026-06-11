"""
posterior_fidelity_logs.py — PROVEN-METHOD VERSION
---------------------------------------------------
Uses the EXACT claimId method that sepolia_gas_logs.py proved works
20/20 times in the same deployment:

    claim_id = nextClaimId()   # read BEFORE openClaim
    send openClaim
    send submitInference(claim_id, ...)

This is sequential single-sender execution: each transaction fully
confirms (wait_for_transaction_receipt) before the next is sent, so
nextClaimId() read before openClaim is exactly the id that openClaim
will allocate. No resolveKey, no events, no races.

Produces: results/posterior_fidelity_{neutral|asymmetric}.csv
"""

import csv, itertools, json, os, time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3
from web3.contract import Contract

from bn_oracle import BNOracle, canonicalize_evidence_for_chain

load_dotenv()

ROOT      = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "deployment" / "build"

SEPOLIA_RPC_URL       = os.environ["SEPOLIA_RPC_URL"]
PRIVATE_KEY           = os.environ["PRIVATE_KEY"]
CPTSTORE_ADDR         = Web3.to_checksum_address(os.environ["CPTSTORE_ADDR"])
CLAIMREGISTRY_ADDR    = Web3.to_checksum_address(os.environ["CLAIMREGISTRY_ADDR"])
ORACLECONTROLLER_ADDR = Web3.to_checksum_address(os.environ["ORACLECONTROLLER_ADDR"])

PROFILE_NAME  = os.environ.get("PROFILE_NAME", "neutral")
DECISION_TAU  = 0.5
OUTPUT_CSV    = ROOT / "results" / f"posterior_fidelity_{PROFILE_NAME}.csv"
TX_TIMEOUT    = 300
MAX_FEE_GWEI  = 50
PRIORITY_GWEI = 3
RUN_ID        = int(time.time())


def load_contract(w3, name, address):
    abi = json.load(open(BUILD_DIR / f"{name}.json", encoding="utf-8"))["abi"]
    return w3.eth.contract(address=address, abi=abi)


def send_tx(w3, fn, sender, private_key):
    """Sign, send, wait for confirmed receipt. Raises on revert."""
    nonce  = w3.eth.get_transaction_count(sender, "pending")
    tx     = fn.build_transaction({
        "from": sender, "nonce": nonce, "chainId": w3.eth.chain_id,
        "gas": 800_000,
        "maxFeePerGas":         w3.to_wei(str(MAX_FEE_GWEI),  "gwei"),
        "maxPriorityFeePerGas": w3.to_wei(str(PRIORITY_GWEI), "gwei"),
    })
    signed  = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=TX_TIMEOUT)
    if receipt.status != 1:
        raise RuntimeError(f"TX REVERTED: {tx_hash.hex()}")
    return receipt


def read_state_synced(w3, claim_reg, claim_id, target_block, retries=15, delay=2.0):
    """Read getClaimState pinned to target_block, waiting for node sync."""
    import time as _time
    for _ in range(retries):
        if w3.eth.block_number >= target_block:
            try:
                return claim_reg.functions.getClaimState(claim_id).call(
                    block_identifier=target_block
                )
            except Exception:
                pass
        _time.sleep(delay)
    return claim_reg.functions.getClaimState(claim_id).call()


def read_claim_synced(w3, claim_reg, claim_id, submit_block, retries=10, delay=2.0):
    """
    Read getClaim() but wait until the RPC node has synced to at least
    submit_block. Free-tier RPC nodes serve reads from slightly-behind
    state, so an immediate read after submitInference can return stale
    (pre-write) values. This polls until the node's view includes the
    submit transaction's block, then reads.
    """
    import time as _time
    for attempt in range(retries):
        node_block = w3.eth.block_number
        if node_block >= submit_block:
            tup = claim_reg.functions.getClaim(claim_id).call(
                block_identifier=submit_block
            )
            return tup
        _time.sleep(delay)
    # Final attempt at latest
    return claim_reg.functions.getClaim(claim_id).call()


def is_consistent(p_float, p_onchain, tau):
    return (p_float >= tau) == (p_onchain >= tau)


def main():
    w3 = Web3(Web3.HTTPProvider(SEPOLIA_RPC_URL))
    if not w3.is_connected():
        raise RuntimeError("Web3 not connected.")

    sender      = Account.from_key(PRIVATE_KEY).address
    cpt_store   = load_contract(w3, "CPTStore",         CPTSTORE_ADDR)
    claim_reg   = load_contract(w3, "ClaimRegistry",    CLAIMREGISTRY_ADDR)
    oracle_ctrl = load_contract(w3, "OracleController", ORACLECONTROLLER_ADDR)

    print(f"Profile  : {PROFILE_NAME}")
    print(f"Sender   : {sender}")
    print(f"Run ID   : {RUN_ID}")
    print(f"Running 16 evidence assignments on Sepolia...\n")

    rows  = []
    bound = BNOracle.rounding_error_bound()

    for bits in itertools.product((0, 1), repeat=4):
        gps, pc, pmd, pr = bits
        evidence = {"GPS": gps, "PC": pc, "PMD": pmd, "PR": pr}

        # ----------------------------------------------------------
        # 1. Read claimId BEFORE openClaim — the proven method.
        #    Sequential single-sender: this id is exactly what
        #    openClaim will allocate (gas_logs.py: 20/20 success).
        # ----------------------------------------------------------
        claim_id = int(claim_reg.functions.nextClaimId().call())

        external_key = Web3.keccak(
            text=f"LUNA_FID_{RUN_ID}_{PROFILE_NAME}_{gps}{pc}{pmd}{pr}"
        )
        open_receipt = send_tx(
            w3,
            oracle_ctrl.functions.openClaim(external_key),
            sender, PRIVATE_KEY,
        )
        open_block = int(open_receipt.blockNumber)
        print(f"  [{gps}{pc}{pmd}{pr}] claimId={claim_id} ", end="", flush=True)

        # Verify the claim is Open, reading pinned to the openClaim block
        # so we don't race a slightly-behind RPC node.
        state = read_state_synced(w3, claim_reg, claim_id, open_block)
        if state != 1:
            raise RuntimeError(
                f"claimId={claim_id} state={state} (expected 1=Open) "
                f"even after block-sync to {open_block}. "
                f"Inspect on Etherscan."
            )

        # ----------------------------------------------------------
        # 2. Reconstruct BN and infer
        # ----------------------------------------------------------
        result    = BNOracle.infer_from_chain(w3, cpt_store, evidence)
        pph_float = float(result["PPH"])
        ppr_float = float(result["PPR"])
        pph_enc   = BNOracle.encode(pph_float)
        ppr_enc   = BNOracle.encode(ppr_float)

        # ----------------------------------------------------------
        # 3. Submit inference (8 ABI params)
        # ----------------------------------------------------------
        values = canonicalize_evidence_for_chain(evidence)
        bn_id  = bytes.fromhex(result["bn_instance_id"][2:])
        submit_receipt = send_tx(
            w3,
            oracle_ctrl.functions.submitInference(
                claim_id,
                values["GPS"], values["PC"],
                values["PMD"], values["PR"],
                pph_enc, ppr_enc, bn_id,
            ),
            sender, PRIVATE_KEY,
        )
        submit_block = int(submit_receipt.blockNumber)

        # ----------------------------------------------------------
        # 4. Read back posteriors
        # ----------------------------------------------------------
        tup             = read_claim_synced(
            w3, claim_reg, claim_id, submit_block
        )
        onchain_pph_enc = int(tup[3])
        onchain_ppr_enc = int(tup[4])

        if onchain_pph_enc == 0 and pph_enc != 0:
            raise RuntimeError(
                f"posteriorPPH=0 on claimId={claim_id}. Expected {pph_enc}. "
                f"This should not happen with the proven method — "
                f"check Etherscan."
            )

        onchain_pph = BNOracle.decode(onchain_pph_enc)
        onchain_ppr = BNOracle.decode(onchain_ppr_enc)
        delta_pph   = abs(pph_float - onchain_pph)
        delta_ppr   = abs(ppr_float - onchain_ppr)
        dc_pph      = is_consistent(pph_float, onchain_pph, DECISION_TAU)
        dc_ppr      = is_consistent(ppr_float, onchain_ppr, DECISION_TAU)

        rows.append({
            "profile": PROFILE_NAME,
            "gps": gps, "pc": pc, "pmd": pmd, "pr": pr,
            "pph_float": pph_float,   "ppr_float": ppr_float,
            "pph_encoded": pph_enc,   "ppr_encoded": ppr_enc,
            "pph_onchain": onchain_pph, "ppr_onchain": onchain_ppr,
            "delta_pph": delta_pph,   "delta_ppr": delta_ppr,
            "decision_consistent_pph": dc_pph,
            "decision_consistent_ppr": dc_ppr,
            "within_bound_pph": delta_pph <= bound,
            "within_bound_ppr": delta_ppr <= bound,
            "claim_id": claim_id,
            "bn_instance_id": result["bn_instance_id"],
            "snapshot_block": result["snapshot_block"],
            "run_id": RUN_ID,
        })

        print(
            f"PPH={pph_float:.4f}({pph_enc}) "
            f"PPR={ppr_float:.4f}({ppr_enc}) "
            f"d={delta_pph:.2e} "
            f"dc={'YY' if dc_pph and dc_ppr else 'N!'}"
        )

    # Write CSV
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    # Summary
    max_delta  = max(max(r["delta_pph"], r["delta_ppr"]) for r in rows)
    mean_delta = sum(r["delta_pph"]+r["delta_ppr"] for r in rows)/(2*len(rows))
    near_bdry  = sum(1 for r in rows
                     if abs(r["pph_float"]-DECISION_TAU) < bound
                     or abs(r["ppr_float"]-DECISION_TAU) < bound)
    inconsist  = sum(1 for r in rows
                     if not r["decision_consistent_pph"]
                     or not r["decision_consistent_ppr"])

    print(f"\n=== FIDELITY SUMMARY (profile={PROFILE_NAME}) ===")
    print(f"  Posteriors evaluated  : {2*len(rows)}")
    print(f"  Max |delta|           : {max_delta:.2e}  (bound: {bound:.2e})")
    print(f"  Mean |delta|          : {mean_delta:.2e}")
    print(f"  Near decision boundary: {near_bdry}")
    print(f"  Decision inconsistent : {inconsist}")
    print(f"  Output                : {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
