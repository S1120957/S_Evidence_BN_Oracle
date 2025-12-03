"""Utilities to rebuild the BNOracle from on-chain CPTStore."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Tuple

from web3 import Web3

from offChain.bn_oracle.bn_oracle import BNOracle

GANACHE_URL = "http://127.0.0.1:7545"
NETWORK_ID = "5777"
SCALE = 1_000_000  # must match CPTStore.SCALE


def _find_build_artifact(name: str) -> dict:
    """Load a Truffle artifact for `name` from deployment/build or deployments/build."""
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


def load_priors_and_cpts_from_chain(
    w3: Web3,
) -> Tuple[Dict[str, float], Dict[str, Dict[Tuple[int, int], float]]]:
    artifact = _find_build_artifact("CPTStore")
    networks = artifact.get("networks", {})
    if NETWORK_ID not in networks:
        raise RuntimeError(f"CPTStore not deployed on network id {NETWORK_ID}")

    address = networks[NETWORK_ID]["address"]
    abi = artifact["abi"]

    cpt_store = w3.eth.contract(address=address, abi=abi)

    prior_pph = cpt_store.functions.priorPPH().call() / SCALE
    prior_ppr = cpt_store.functions.priorPPR().call() / SCALE
    priors = {"PPH": prior_pph, "PPR": prior_ppr}

    idx_to_name = {0: "GPS", 1: "PC", 2: "PMD", 3: "PR"}
    cpts: Dict[str, Dict[Tuple[int, int], float]] = {name: {} for name in idx_to_name.values()}

    for ev_idx, name in idx_to_name.items():
        table = cpts[name]
        for pph in (0, 1):
            for ppr in (0, 1):
                scaled = cpt_store.functions.getEvidenceTrueCPT(ev_idx, pph, ppr).call()
                table[(pph, ppr)] = scaled / SCALE

    return priors, cpts


def build_bn_from_chain(
    w3: Web3,
) -> Tuple[BNOracle, Dict[str, float], Dict[str, Dict[Tuple[int, int], float]]]:
    priors, cpts = load_priors_and_cpts_from_chain(w3)
    bn = BNOracle(priors, cpts)
    return bn, priors, cpts


def main() -> None:
    w3 = Web3(Web3.HTTPProvider(GANACHE_URL))
    if not w3.is_connected():
        raise RuntimeError(f"Web3 not connected to Ganache at {GANACHE_URL}")

    bn, priors, cpts = build_bn_from_chain(w3)
    print("Priors from chain:", priors)
    print("Sample CPT entry (GPS, PPH=1, PPR=0):", cpts["GPS"][(1, 0)])

    posterior = bn.infer({"GPS": 1, "PC": 0, "PMD": 1, "PR": 0})
    print("Posterior given evidence:", posterior)


if __name__ == "__main__":
    main()
