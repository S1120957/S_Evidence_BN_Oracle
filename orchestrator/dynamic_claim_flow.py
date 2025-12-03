# orchestrator/dynamic_claim_flow.py
"""
Dynamic claim + evidence arrival demo.

Pipeline:
1) Physician opens a claim via OracleController.openClaim().
2) Physician (or sensors) add evidence pieces over time via addEvidence().
3) Off-chain oracle:
   - Reads BN CPTs from CPTStore via build_bn_from_chain().
   - Reads all evidence for a given claim from EvidenceRegistry.
   - Runs BN inference to get P(PPH|evidence), P(PPR|evidence).
   - Calls OracleController.submitInference(claimId, posteriorPPH, posteriorPPR).

This shows how claims, evidence pieces, and BN inference are linked over time.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Dict

from web3 import Web3

# Ensure project root is on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.bn_from_chain import build_bn_from_chain  # type: ignore

GANACHE_URL = "http://127.0.0.1:7545"
NETWORK_ID = "5777"
SCALE = 1_000_000  # must match CPTStore.SCALE

# Consistent mapping between evidenceIndex and BN variable names
EVIDENCE_NAMES = ["GPS", "PC", "PMD", "PR"]


def _find_build_artifact(name: str) -> dict:
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
    artifact = _find_build_artifact(name)
    networks = artifact.get("networks", {})
    if NETWORK_ID not in networks:
        raise RuntimeError(f"{name} not deployed on network id {NETWORK_ID}")
    addr = networks[NETWORK_ID]["address"]
    abi = artifact["abi"]
    return w3.eth.contract(address=addr, abi=abi)


def _fetch_evidence_for_claim(evidence_registry, claim_id: int) -> Dict[str, int]:
    """
    Reconstruct the evidence dict { 'GPS':0/1, 'PC':0/1, 'PMD':0/1, 'PR':0/1 }
    for the given claimId from piece-wise EvidenceRegistry rows.
    """
    ids = evidence_registry.functions.getEvidenceIdsForClaim(claim_id).call()
    evidence_values: Dict[str, int] = {}

    for eid in ids:
        ev = evidence_registry.functions.evidences(eid).call()
        # ev tuple: (id, claimId, evidenceIndex, value, timestamp, reporter)
        _, _, evidence_index, value, _, _ = ev
        idx = int(evidence_index)
        if idx < len(EVIDENCE_NAMES):
            name = EVIDENCE_NAMES[idx]
            evidence_values[name] = int(value)

    return evidence_values


def _compute_and_commit_posterior(
    w3: Web3,
    bn,
    controller,
    claim_registry,
    evidence_registry,
    claim_id: int,
    label: str,
) -> None:
    """
    Pull evidence for claimId, run BN inference, and commit posterior on-chain.
    """
    evidence = _fetch_evidence_for_claim(evidence_registry, claim_id)
    print(f"\n[{label}] Evidence for claim {claim_id}: {evidence}")

    if not evidence:
        print(f"[{label}] No evidence yet for claim {claim_id}, skipping inference.")
        return

    posterior = bn.infer(evidence)
    print(f"[{label}] Off-chain BN posterior: {posterior}")

    scaled_pph = int(round(float(posterior["PPH"]) * SCALE))
    scaled_ppr = int(round(float(posterior["PPR"]) * SCALE))

    oracle = w3.eth.accounts[0]  # treat accounts[0] as the oracle for now

    tx_hash = controller.functions.submitInference(
        int(claim_id),
        scaled_pph,
        scaled_ppr,
    ).transact({"from": oracle})
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"[{label}] submitInference tx status: {receipt.status}")

    # Read back the claim row
    claim_row = claim_registry.functions.claims(claim_id).call()
    # claim tuple: (id, physician, openedAt, lastUpdatedAt, bnInstanceId, posteriorPPH, posteriorPPR, closed)
    (
        cid,
        physician,
        opened_at,
        updated_at,
        bn_id,
        posteriorPPH,
        posteriorPPR,
        closed,
    ) = claim_row

    print(
        f"[{label}] On-chain Claim[{cid}] physician={physician} "
        f"PPH={posteriorPPH / SCALE:.4f} PPR={posteriorPPR / SCALE:.4f} "
        f"closed={closed}"
    )


def main() -> None:
    w3 = Web3(Web3.HTTPProvider(GANACHE_URL))
    if not w3.is_connected():
        raise RuntimeError(f"Web3 not connected to {GANACHE_URL}")

    accounts = w3.eth.accounts
    if len(accounts) < 2:
        raise RuntimeError("Need at least 2 accounts (oracle, physician) in Ganache")

    oracle = accounts[0]
    physician = accounts[1]

    print(f"Using oracle account:    {oracle}")
    print(f"Using physician account: {physician}")

    # Contracts
    cpt_store = _load_contract(w3, "CPTStore")
    claim_registry = _load_contract(w3, "ClaimRegistry")
    evidence_registry = _load_contract(w3, "EvidenceRegistry")
    controller = _load_contract(w3, "OracleController")

    # Build BN from CPTStore (same as your previous pipeline)
    bn, priors, cpts = build_bn_from_chain(w3)
    print("BN priors from CPTStore:", priors)

    # ---------------------------------------------------------------------
    # 1) Physician opens a claim
    # ---------------------------------------------------------------------
    next_claim_before = claim_registry.functions.nextClaimId().call()
    print(f"nextClaimId before openClaim: {next_claim_before}")

    tx_hash = controller.functions.openClaim().transact({"from": physician})
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    print("openClaim tx status:", receipt.status)

    # New claim id is the previous nextClaimId
    claim_id = int(next_claim_before)
    print(f"Opened claimId={claim_id} for physician={physician}")

    # ---------------------------------------------------------------------
    # 2) Add evidence pieces in two phases (simulate dynamic arrival)
    # ---------------------------------------------------------------------
    # Phase 1: GPS=1, PC=0
    print("\nPhase 1: adding GPS=1, PC=0")
    controller.functions.addEvidence(
        claim_id,
        0,  # GPS index
        1,  # value=1
    ).transact({"from": physician})

    controller.functions.addEvidence(
        claim_id,
        1,  # PC index
        0,  # value=0
    ).transact({"from": physician})

    _compute_and_commit_posterior(
        w3,
        bn,
        controller,
        claim_registry,
        evidence_registry,
        claim_id,
        label="Phase 1",
    )

    # Phase 2: later, PMD=1, PR=0 arrive
    print("\nPhase 2: adding PMD=1, PR=0")
    controller.functions.addEvidence(
        claim_id,
        2,  # PMD index
        1,
    ).transact({"from": physician})

    controller.functions.addEvidence(
        claim_id,
        3,  # PR index
        0,
    ).transact({"from": physician})

    _compute_and_commit_posterior(
        w3,
        bn,
        controller,
        claim_registry,
        evidence_registry,
        claim_id,
        label="Phase 2",
    )

    print("\nDynamic claim flow completed.")


if __name__ == "__main__":
    main()
