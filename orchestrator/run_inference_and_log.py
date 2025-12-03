"""Single-shot BN oracle inference + on-chain logging."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from web3 import Web3

import sys

# Ensure project root (s_evidence_bn_oracle) is on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from offChain.bn_oracle.bn_oracle import BNOracle
from orchestrator.bn_from_chain import build_bn_from_chain

GANACHE_URL = "http://127.0.0.1:7545"
NETWORK_ID = "5777"
SCALE = 1_000_000  # must match CPTStore.SCALE


def _find_build_artifact(name: str) -> dict:
    candidates = [
        Path("deployment") / "build" / f"{name}.json",
        Path("deployments") / "build" / f"{name}.json",
    ]
    for path in candidates:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
    raise FileNotFoundError(
        f"Could not find {name}.json in any known build directory. "
        f"Tried: {', '.join(str(c) for c in candidates)}"
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run one BN inference and log it on-chain.")
    p.add_argument("--gps", type=int, choices=[0, 1], default=0)
    p.add_argument("--pc", type=int, choices=[0, 1], default=0)
    p.add_argument("--pmd", type=int, choices=[0, 1], default=0)
    p.add_argument("--pr", type=int, choices=[0, 1], default=0)
    return p.parse_args()


def main() -> None:
    args = parse_args()

    w3 = Web3(Web3.HTTPProvider(GANACHE_URL))
    if not w3.is_connected():
        raise RuntimeError(f"Web3 not connected to Ganache at {GANACHE_URL}")

    accounts = w3.eth.accounts
    if not accounts:
        raise RuntimeError("No accounts available in Ganache")
    sender = accounts[0]

    # BN from on-chain CPTs
    bn, priors, cpts = build_bn_from_chain(w3)
    print("Priors:", priors)
    print("GPS CPT (PPH=1,PPR=0):", cpts["GPS"][(1, 0)])

    evidence = {"GPS": args.gps, "PC": args.pc, "PMD": args.pmd, "PR": args.pr}
    posterior = bn.infer(evidence)
    print("Posterior (off-chain BN):", posterior)

    # Scale posteriors to integer fixed-point
    scaled_pph = int(round(posterior["PPH"] * SCALE))
    scaled_ppr = int(round(posterior["PPR"] * SCALE))

    # OracleController
    oc_artifact = _find_build_artifact("OracleController")
    oc_addr = oc_artifact["networks"][NETWORK_ID]["address"]
    oc_abi = oc_artifact["abi"]
    controller = w3.eth.contract(address=oc_addr, abi=oc_abi)

    # EvidenceRegistry
    er_artifact = _find_build_artifact("EvidenceRegistry")
    er_addr = er_artifact["networks"][NETWORK_ID]["address"]
    er_abi = er_artifact["abi"]
    registry = w3.eth.contract(address=er_addr, abi=er_abi)

    # Submit inference
    tx_hash = controller.functions.submitInference(
        int(args.gps),
        int(args.pc),
        int(args.pmd),
        int(args.pr),
        scaled_pph,
        scaled_ppr,
    ).transact({"from": sender})
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    print("submitInference tx status:", receipt.status)

    evidence_id = controller.functions.lastEvidenceId().call()

    # Read back from registry to confirm
    gps, pc, pmd, pr, stored_pph, stored_ppr = registry.functions.getEvidence(
        evidence_id
    ).call()

    print(
        "On-chain stored posteriors:",
        {
            "PPH": stored_pph / SCALE,
            "PPR": stored_ppr / SCALE,
        },
    )
    print("Last evidence ID:", evidence_id)


if __name__ == "__main__":
    main()
