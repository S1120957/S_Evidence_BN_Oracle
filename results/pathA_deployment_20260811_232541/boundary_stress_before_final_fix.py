"""
SELENE Path A boundary-stress experiment.

Part A: analytic fixed-point boundary sweep for RQ3.
Part B: Sepolia contract invariant tests B1-B9 on the frozen Path A deployment.

This script NEVER changes CPTStore parameters.
"""

import argparse
import csv
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"

TAU = 0.5
SCALES = [10**2, 10**3, 10**4, 10**6]
DEFAULT_SCALE = 10**6
TARGET_OFFSETS = [
    1e-2, 1e-3, 1e-4, 1e-5, 1e-6,
    5e-7, 1e-7, 1e-8, 1e-9, 0.0,
]

BASE_CPTS: Dict[str, Dict[Tuple[int, int], float]] = {
    "GPS": {(1, 0): 0.90, (0, 1): 0.15, (1, 1): 0.80, (0, 0): 0.10},
    "PC":  {(1, 0): 0.85, (0, 1): 0.20, (1, 1): 0.75, (0, 0): 0.15},
    "PMD": {(1, 0): 0.88, (0, 1): 0.10, (1, 1): 0.78, (0, 0): 0.08},
    "PR":  {(1, 0): 0.80, (0, 1): 0.25, (1, 1): 0.70, (0, 0): 0.20},
}


def encode(p: float, scale: int) -> int:
    return int(round(p * scale))


def decode(p_hat: int, scale: int) -> float:
    return p_hat / scale


def decision(p: float, tau: float = TAU) -> bool:
    return p >= tau


def consistent(p: float, scale: int, tau: float = TAU) -> bool:
    return decision(p, tau) == decision(decode(encode(p, scale), scale), tau)


def posterior_pph(prior_pph: float, evidence: Dict[str, int]) -> float:
    """Closed-form P(PPH=1|e), with PPR prior fixed at 0.5 for boundary construction."""
    prior_ppr = 0.5
    weights: Dict[Tuple[int, int], float] = {}

    for pph in (0, 1):
        for ppr in (0, 1):
            w = (prior_pph if pph else 1.0 - prior_pph)
            w *= (prior_ppr if ppr else 1.0 - prior_ppr)

            for name, value in evidence.items():
                q = BASE_CPTS[name][(pph, ppr)]
                w *= q if value == 1 else (1.0 - q)

            weights[(pph, ppr)] = w

    z = sum(weights.values())
    if z <= 0.0:
        raise RuntimeError("Boundary construction produced zero normalizer")

    return sum(
        w for (pph, _ppr), w in weights.items() if pph == 1
    ) / z


def solve_prior_for_target(
    target: float,
    evidence: Dict[str, int],
    iters: int = 200,
) -> Optional[float]:
    lo, hi = 1e-12, 1.0 - 1e-12
    f_lo = posterior_pph(lo, evidence) - target
    f_hi = posterior_pph(hi, evidence) - target

    if f_lo * f_hi > 0:
        return None

    for _ in range(iters):
        mid = (lo + hi) / 2.0
        f_mid = posterior_pph(mid, evidence) - target

        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid

    return (lo + hi) / 2.0


def run_part_a(run_dir: Path) -> List[Dict[str, Any]]:
    print("=" * 68)
    print("BOUNDARY STRESS PART A -- ANALYTIC FIXED-POINT SWEEP")
    print("=" * 68)

    evidence = {"GPS": 1, "PC": 0, "PMD": 1, "PR": 0}
    rows: List[Dict[str, Any]] = []

    for offset in TARGET_OFFSETS:
        for side, sign in (("above", +1.0), ("below", -1.0)):
            if offset == 0.0 and side == "below":
                continue

            target = TAU + sign * offset
            prior = solve_prior_for_target(target, evidence)
            if prior is None:
                continue

            p = posterior_pph(prior, evidence)
            dist = abs(p - TAU)

            row: Dict[str, Any] = {
                "target_offset": offset,
                "target_side": side,
                "actual_side": "above" if p >= TAU else "below",
                "prior_pph": prior,
                "posterior_pph": p,
                "dist_from_tau": dist,
                "tau": TAU,
                "evidence": "GPS=1|PC=0|PMD=1|PR=0",
            }

            for scale in SCALES:
                eps = 1.0 / (2.0 * scale)
                enc = encode(p, scale)
                dec = decode(enc, scale)
                row[f"encoded_scale_{scale}"] = enc
                row[f"decoded_scale_{scale}"] = dec
                row[f"consistent_scale_{scale}"] = consistent(p, scale)
                row[f"within_eps_scale_{scale}"] = dist <= eps

            rows.append(row)

    if len(rows) != 19:
        raise RuntimeError(f"Expected 19 Part-A rows, got {len(rows)}")

    # Validate soundness for every tested SCALE.
    for scale in SCALES:
        eps = 1.0 / (2.0 * scale)
        outside = [r for r in rows if r["dist_from_tau"] > eps]
        bad = [r for r in outside if not r[f"consistent_scale_{scale}"]]
        if bad:
            raise RuntimeError(
                f"Soundness failure at SCALE={scale}: {len(bad)} outside-epsilon cases inconsistent"
            )

    eps = 1.0 / (2.0 * DEFAULT_SCALE)
    outside = [r for r in rows if r["dist_from_tau"] > eps]
    inside = [r for r in rows if r["dist_from_tau"] <= eps]
    outside_ok = sum(
        bool(r[f"consistent_scale_{DEFAULT_SCALE}"]) for r in outside
    )
    inside_bad = sum(
        not bool(r[f"consistent_scale_{DEFAULT_SCALE}"]) for r in inside
    )

    inconsistent_rows = [
        r for r in rows if not r[f"consistent_scale_{DEFAULT_SCALE}"]
    ]

    if outside_ok != len(outside):
        raise RuntimeError("SCALE=1e6 soundness check failed")
    if inside_bad == 0:
        raise RuntimeError("Tightness not demonstrated inside epsilon")
    if not all(r["posterior_pph"] < TAU for r in inconsistent_rows):
        raise RuntimeError("Observed a fixed-point failure from above tau; expected one-sided failures from below")

    for row in rows:
        row["soundness_verified_scale_1e6"] = outside_ok == len(outside)
        row["tightness_demonstrated_scale_1e6"] = inside_bad > 0
        row["failure_one_sided_below_tau"] = True

    out = RESULTS / "boundary_stress_partA.csv"
    run_out = run_dir / "boundary_stress_partA.csv"
    RESULTS.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    keys: List[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)

    for path in (out, run_out):
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)

    print(f"Rows                         : {len(rows)} / 19")
    print(f"SCALE=1e6 epsilon            : {eps:.9e}")
    print(f"Outside epsilon consistent   : {outside_ok}/{len(outside)}")
    print(f"Inside epsilon inconsistent  : {inside_bad}/{len(inside)}")
    print(f"Inconsistent cases below tau : {len(inconsistent_rows)}/{len(inconsistent_rows)}")
    print(f"Output                       : {out}")
    print(f"Run copy                     : {run_out}")
    print("BOUNDARY PART A: PASS")

    return rows


def run_part_b(run_dir: Path) -> List[Dict[str, Any]]:
    from dotenv import load_dotenv
    from eth_account import Account
    from web3 import Web3

    load_dotenv(ROOT / ".env")

    print("\n" + "=" * 68)
    print("BOUNDARY STRESS PART B -- SEPOLIA CONTRACT INVARIANTS")
    print("=" * 68)

    manifest_path = run_dir / "asymmetric_bn_manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"Missing frozen manifest: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    w3 = Web3(Web3.HTTPProvider(os.environ["SEPOLIA_RPC_URL"]))
    if not w3.is_connected():
        raise RuntimeError("Sepolia RPC connection failed")
    if w3.eth.chain_id != 11155111:
        raise RuntimeError(f"Wrong chain id: {w3.eth.chain_id}")

    private_key = os.environ["PRIVATE_KEY"]
    sender = Web3.to_checksum_address(Account.from_key(private_key).address)

    build = ROOT / "deployment" / "build"

    def load_contract(name: str, env_name: str):
        artifact = json.loads((build / f"{name}.json").read_text(encoding="utf-8"))
        address = Web3.to_checksum_address(os.environ[env_name])
        expected = Web3.to_checksum_address(manifest["contracts"][name])
        if address != expected:
            raise RuntimeError(f"{name} address differs from frozen manifest")
        return w3.eth.contract(address=address, abi=artifact["abi"])

    cpt = load_contract("CPTStore", "CPTSTORE_ADDR")
    claims = load_contract("ClaimRegistry", "CLAIMREGISTRY_ADDR")
    evidence = load_contract("EvidenceRegistry", "EVIDENCEREGISTRY_ADDR")
    controller = load_contract("OracleController", "ORACLECONTROLLER_ADDR")

    deployment_id = controller.address
    scale = int(cpt.functions.SCALE().call())
    if scale != DEFAULT_SCALE:
        raise RuntimeError(f"Unexpected SCALE: {scale}")
    if int(controller.functions.UNOBSERVED().call()) != 2:
        raise RuntimeError("OracleController UNOBSERVED != 2")
    if int(evidence.functions.UNOBSERVED().call()) != 2:
        raise RuntimeError("EvidenceRegistry UNOBSERVED != 2")

    pre_snapshot = cpt.functions.getCPTSnapshot().call()
    pre_bn_id = "0x" + bytes(pre_snapshot[3]).hex()
    expected_bn_id = manifest["bn_instance_id"]

    if pre_bn_id.lower() != expected_bn_id.lower():
        raise RuntimeError("Current bnInstanceId differs from frozen manifest")
    if int(pre_snapshot[0]) != int(manifest["priors_scaled"]["PPH"]):
        raise RuntimeError("Current PPH prior differs from frozen manifest")
    if int(pre_snapshot[1]) != int(manifest["priors_scaled"]["PPR"]):
        raise RuntimeError("Current PPR prior differs from frozen manifest")
    if [int(x) for x in pre_snapshot[2]] != [int(x) for x in manifest["cpts_flat_scaled"]]:
        raise RuntimeError("Current CPT entries differ from frozen manifest")

    bn_bytes = bytes(pre_snapshot[3])
    run_id = int(time.time())
    rows: List[Dict[str, Any]] = []

    def send(fn, gas: int = 800_000):
        nonce = w3.eth.get_transaction_count(sender, "pending")
        tx = fn.build_transaction({
            "from": sender,
            "nonce": nonce,
            "chainId": w3.eth.chain_id,
            "gas": gas,
            "maxFeePerGas": w3.to_wei(50, "gwei"),
            "maxPriorityFeePerGas": w3.to_wei(3, "gwei"),
        })
        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
        if int(receipt.status) != 1:
            raise RuntimeError(f"Transaction reverted: {tx_hash.hex()}")
        return receipt

    def open_claim(label: str, key=None):
        claim_id = int(claims.functions.nextClaimId().call())
        if key is None:
            key = Web3.keccak(text=f"SELENE_BOUNDARY_{run_id}_{label}_{claim_id}")
        receipt = send(controller.functions.openClaim(key))
        state = int(claims.functions.getClaimState(claim_id).call(block_identifier=int(receipt.blockNumber)))
        if state != 1:
            raise RuntimeError(f"Claim {claim_id} did not open")
        return claim_id, key, receipt

    def success_submit(
        claim_id: int,
        gps: int,
        pc: int,
        pmd: int,
        pr: int,
        pph: int,
        ppr: int,
    ):
        receipt = send(
            controller.functions.submitInference(
                claim_id, gps, pc, pmd, pr, pph, ppr, bn_bytes
            )
        )
        logs = controller.events.InferenceSubmitted().process_receipt(receipt)
        if len(logs) != 1:
            raise RuntimeError(f"Expected one InferenceSubmitted event, got {len(logs)}")
        evidence_id = int(logs[0]["args"]["evidenceId"])
        rec = evidence.functions.getEvidence(evidence_id).call(
            block_identifier=int(receipt.blockNumber)
        )
        claim = claims.functions.getClaim(claim_id).call(
            block_identifier=int(receipt.blockNumber)
        )

        expected_ev = (gps, pc, pmd, pr)
        actual_ev = tuple(int(rec[i]) for i in (1, 2, 3, 4))
        if int(rec[0]) != claim_id or actual_ev != expected_ev:
            raise RuntimeError("EvidenceRegistry read-back mismatch")
        if int(rec[5]) != pph or int(rec[6]) != ppr:
            raise RuntimeError("EvidenceRegistry posterior read-back mismatch")
        if Web3.to_checksum_address(rec[7]) != sender:
            raise RuntimeError("EvidenceRegistry submitter mismatch")
        if int(claim[3]) != pph or int(claim[4]) != ppr:
            raise RuntimeError("ClaimRegistry posterior read-back mismatch")

        return receipt, evidence_id

    def call_expect_revert(fn, expected_substring: str, from_addr: Optional[str] = None):
        caller = sender if from_addr is None else Web3.to_checksum_address(from_addr)
        try:
            fn.call({"from": caller})
        except Exception as exc:  # web3 exception types differ by provider/version
            reason = str(exc)
            passed = expected_substring.lower() in reason.lower()
            return True, reason, passed
        return False, "NO REVERT", False

    def add_row(
        case_id: str,
        description: str,
        input_summary: str,
        expected: str,
        observed: str,
        reverted: bool,
        revert_reason: str,
        gas_used: int,
        tx_hash: str,
        block_number: Any,
        claim_id: Any,
        passed: bool,
        setup_tx_hashes: str = "",
        notes: str = "",
    ):
        rows.append({
            "run_id": run_id,
            "case_id": case_id,
            "description": description,
            "input_summary": input_summary,
            "expected": expected,
            "observed": observed,
            "reverted": reverted,
            "revert_reason": revert_reason,
            "gas_used": gas_used,
            "tx_hash": tx_hash,
            "block_number": block_number,
            "claim_id": claim_id,
            "passed": passed,
            "deployment_id": deployment_id,
            "bn_instance_id": pre_bn_id,
            "cptstore_address": cpt.address,
            "claimregistry_address": claims.address,
            "evidenceregistry_address": evidence.address,
            "oraclecontroller_address": controller.address,
            "setup_tx_hashes": setup_tx_hashes,
            "notes": notes,
        })
        status = "PASS" if passed else "FAIL"
        print(f"  {case_id:<3} {status:<4}  {description}")

    # Shared open claim for B1-B6, B8-B9.
    shared_claim, _, shared_open = open_claim("shared")
    shared_setup = shared_open.transactionHash.hex()

    # B1: lower posterior edge.
    r, _ = success_submit(shared_claim, 1, 0, 1, 0, 0, 0)
    add_row(
        "B1", "posterior = 0", "PPH=0,PPR=0", "success", "success",
        False, "", int(r.gasUsed), r.transactionHash.hex(), int(r.blockNumber),
        shared_claim, True, shared_setup,
    )

    # B2: upper posterior edge.
    r, _ = success_submit(shared_claim, 1, 0, 1, 0, scale, scale)
    add_row(
        "B2", "posterior = SCALE", f"PPH={scale},PPR={scale}", "success", "success",
        False, "", int(r.gasUsed), r.transactionHash.hex(), int(r.blockNumber),
        shared_claim, True, shared_setup,
    )

    # B3: above posterior bound.
    reverted, reason, passed = call_expect_revert(
        controller.functions.submitInference(
            shared_claim, 1, 0, 1, 0, scale + 1, 0, bn_bytes
        ),
        "posteriorPPH exceeds SCALE",
    )
    add_row(
        "B3", "posterior > SCALE", f"PPH={scale+1}", "revert", "revert" if reverted else "success",
        reverted, reason, 0, "", "", shared_claim, passed, shared_setup,
    )

    # B4: invalid ternary value.
    reverted, reason, passed = call_expect_revert(
        controller.functions.submitInference(
            shared_claim, 3, 0, 1, 0, 500_000, 500_000, bn_bytes
        ),
        "bad gps",
    )
    add_row(
        "B4", "evidence outside {0,1,2}", "gps=3", "revert", "revert" if reverted else "success",
        reverted, reason, 0, "", "", shared_claim, passed, shared_setup,
    )

    # B5: explicit UNOBSERVED is representable; all-unobserved posterior = priors.
    prior_pph = int(pre_snapshot[0])
    prior_ppr = int(pre_snapshot[1])
    r, evidence_id = success_submit(
        shared_claim, 2, 2, 2, 2, prior_pph, prior_ppr
    )
    add_row(
        "B5", "UNOBSERVED evidence accepted", "gps=pc=pmd=pr=2", "success", "success",
        False, "", int(r.gasUsed), r.transactionHash.hex(), int(r.blockNumber),
        shared_claim, True, shared_setup, notes=f"evidence_id={evidence_id}",
    )

    # B6: nonexistent claim.
    nonexistent = 2**32
    reverted, reason, passed = call_expect_revert(
        controller.functions.submitInference(
            nonexistent, 1, 0, 1, 0, 500_000, 500_000, bn_bytes
        ),
        "claim not open",
    )
    add_row(
        "B6", "submit to nonexistent claim", f"claimId={nonexistent}", "revert", "revert" if reverted else "success",
        reverted, reason, 0, "", "", nonexistent, passed,
    )

    # B7 setup: open then finalize one claim.
    finalized_claim, _, fin_open = open_claim("finalized")
    fin_receipt = send(controller.functions.finalizeClaim(finalized_claim))
    if int(claims.functions.getClaimState(finalized_claim).call()) != 2:
        raise RuntimeError("B7 setup claim did not finalize")
    fin_setup = ";".join([
        fin_open.transactionHash.hex(),
        fin_receipt.transactionHash.hex(),
    ])

    reverted, reason, passed = call_expect_revert(
        controller.functions.submitInference(
            finalized_claim, 1, 0, 1, 0, 500_000, 500_000, bn_bytes
        ),
        "claim not open",
    )
    add_row(
        "B7", "submit to finalized claim", f"claimId={finalized_claim}", "revert", "revert" if reverted else "success",
        reverted, reason, 0, "", "", finalized_claim, passed, fin_setup,
    )

    # B8: unauthorised caller; eth_call needs no funded second wallet.
    unauthorized = Web3.to_checksum_address("0x000000000000000000000000000000000000bEEF")
    if unauthorized == sender:
        raise RuntimeError("Unexpected unauthorized test address collision")
    reverted, reason, passed = call_expect_revert(
        controller.functions.submitInference(
            shared_claim, 1, 0, 1, 0, 500_000, 500_000, bn_bytes
        ),
        "caller is not oracle operator",
        from_addr=unauthorized,
    )
    add_row(
        "B8", "unauthorized submitInference caller", f"from={unauthorized}", "revert", "revert" if reverted else "success",
        reverted, reason, 0, "", "", shared_claim, passed, shared_setup,
    )

    # B9: stale/wrong BN fingerprint.
    wrong_bn = bytes(32)
    if wrong_bn == bn_bytes:
        wrong_bn = bytes.fromhex("01" + "00" * 31)
    reverted, reason, passed = call_expect_revert(
        controller.functions.submitInference(
            shared_claim, 1, 0, 1, 0, 500_000, 500_000, wrong_bn
        ),
        "stale BN snapshot",
    )
    add_row(
        "B9", "stale BN snapshot rejected", "expectedBnInstanceId=wrong", "revert", "revert" if reverted else "success",
        reverted, reason, 0, "", "", shared_claim, passed, shared_setup,
    )

    # B10-B12 from the handoff are optional robustness extras and are
    # intentionally not part of the required P1.5 run. B1-B9 are the
    # necessary contract invariants that substantiate the manuscript.

    if len(rows) != 9:
        raise RuntimeError(f"Expected 9 Part-B rows, got {len(rows)}")
    failed = [r for r in rows if not r["passed"]]
    if failed:
        raise RuntimeError(
            "Boundary Part B failures: " + ", ".join(r["case_id"] for r in failed)
        )

    # Critical invariant: the boundary suite must not mutate CPTStore.
    post_snapshot = cpt.functions.getCPTSnapshot().call()
    post_bn_id = "0x" + bytes(post_snapshot[3]).hex()
    if post_bn_id.lower() != pre_bn_id.lower():
        raise RuntimeError("Boundary suite unexpectedly changed bnInstanceId")
    if int(post_snapshot[0]) != int(pre_snapshot[0]) or int(post_snapshot[1]) != int(pre_snapshot[1]):
        raise RuntimeError("Boundary suite unexpectedly changed priors")
    if [int(x) for x in post_snapshot[2]] != [int(x) for x in pre_snapshot[2]]:
        raise RuntimeError("Boundary suite unexpectedly changed CPT entries")

    out = RESULTS / "boundary_stress_partB.csv"
    run_out = run_dir / "boundary_stress_partB.csv"
    keys = list(rows[0].keys())

    for path in (out, run_out):
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)

    successful = [r for r in rows if not r["reverted"]]
    reverted_rows = [r for r in rows if r["reverted"]]

    print()
    print(f"Rows                         : {len(rows)} / 9")
    print(f"Passed                       : {len(rows) - len(failed)}/{len(rows)}")
    print(f"Successful state-changing    : {len(successful)}")
    print(f"Expected reverts via eth_call: {len(reverted_rows)}")
    print(f"Frozen bnInstanceId unchanged: YES ({post_bn_id})")
    print(f"Output                       : {out}")
    print(f"Run copy                     : {run_out}")
    print("BOUNDARY PART B: PASS")

    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--offline",
        action="store_true",
        help="run Part A only; do not access Sepolia",
    )
    args = parser.parse_args()

    run_dir_env = os.environ.get("SELENE_RUN_DIR", "").strip()
    if args.offline:
        run_dir = Path(run_dir_env) if run_dir_env else RESULTS / "offline_boundary"
    else:
        if not run_dir_env:
            raise RuntimeError("SELENE_RUN_DIR is required for the definitive Path A run")
        run_dir = Path(run_dir_env)

    run_part_a(run_dir)

    if not args.offline:
        run_part_b(run_dir)
        print("\nBOUNDARY STRESS EXPERIMENT: PASS")


if __name__ == "__main__":
    main()
