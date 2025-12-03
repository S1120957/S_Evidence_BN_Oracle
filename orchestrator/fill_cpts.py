"""Fill CPTStore on-chain with dummy CPTs.

By default all P(evidence=True | PPH, PPR) = 0.5.

To plug in real domain CPTs later, edit the gps/pc/pmd/pr dicts only.
"""

from __future__ import annotations

import json
from pathlib import Path

from web3 import Web3

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


def main() -> None:
    w3 = Web3(Web3.HTTPProvider(GANACHE_URL))
    if not w3.is_connected():
        raise RuntimeError(f"Web3 not connected to Ganache at {GANACHE_URL}")

    artifact = _find_build_artifact("CPTStore")
    networks = artifact.get("networks", {})
    if NETWORK_ID not in networks:
        raise RuntimeError(f"CPTStore not deployed on network id {NETWORK_ID}")

    address = networks[NETWORK_ID]["address"]
    abi = artifact["abi"]
    cpt_store = w3.eth.contract(address=address, abi=abi)

    accounts = w3.eth.accounts
    if not accounts:
        raise RuntimeError("No accounts available in Ganache")
    sender = accounts[0]

    # CPTs – safe to modify later. For now: neutral 0.5 everywhere.
    gps = {(0, 0): 0.5, (0, 1): 0.5, (1, 0): 0.5, (1, 1): 0.5}
    pc = {(0, 0): 0.5, (0, 1): 0.5, (1, 0): 0.5, (1, 1): 0.5}
    pmd = {(0, 0): 0.5, (0, 1): 0.5, (1, 0): 0.5, (1, 1): 0.5}
    pr = {(0, 0): 0.5, (0, 1): 0.5, (1, 0): 0.5, (1, 1): 0.5}

    cpt_maps = [gps, pc, pmd, pr]
    tx_count = 0

    for ev_index, mapping in enumerate(cpt_maps):
        for pph in (0, 1):
            for ppr in (0, 1):
                p_true = mapping[(pph, ppr)]
                scaled = int(round(p_true * SCALE))
                tx_hash = cpt_store.functions.setEvidenceCPT(
                    ev_index, pph, ppr, scaled
                ).transact({"from": sender})
                w3.eth.wait_for_transaction_receipt(tx_hash)
                tx_count += 1

    print(
        f"CPTs written on-chain with neutral 0.5 probabilities for all entries "
        f"({tx_count} transactions)."
    )
    print("To use real CPTs, edit gps/pc/pmd/pr dicts only.")


if __name__ == "__main__":
    main()
