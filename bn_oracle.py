from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from web3 import Web3

log = logging.getLogger(__name__)

SCALE: int = 1_000_000
EVIDENCE_NAMES: Tuple[str, ...] = ("GPS", "PC", "PMD", "PR")
EVIDENCE_INDEX: Dict[str, int] = {name: i for i, name in enumerate(EVIDENCE_NAMES)}
MAX_OBSERVED_MASK: int = 0x0F


@dataclass(frozen=True)
class BNSnapshot:
    prior_pph: float
    prior_ppr: float
    cpts: Dict[str, Dict[Tuple[int, int], float]]
    bn_instance_id: str
    snapshot_block: int


class BNOracle:
    def __init__(
        self,
        prior_pph: float,
        prior_ppr: float,
        cpts: Mapping[str, Mapping[Tuple[int, int], float]],
        bn_instance_id: Optional[str] = None,
        snapshot_block: Optional[int] = None,
    ) -> None:
        _validate_prob(prior_pph, "prior_pph")
        _validate_prob(prior_ppr, "prior_ppr")

        self.prior_pph = float(prior_pph)
        self.prior_ppr = float(prior_ppr)
        self.bn_instance_id = bn_instance_id
        self.snapshot_block = snapshot_block

        normalized: Dict[str, Dict[Tuple[int, int], float]] = {}
        for ev in EVIDENCE_NAMES:
            if ev not in cpts:
                raise ValueError(f"CPT missing for evidence node '{ev}'")
            normalized[ev] = {}
            for key in ((0, 0), (0, 1), (1, 0), (1, 1)):
                if key not in cpts[ev]:
                    raise ValueError(f"CPT entry missing: {ev}{key}")
                p_true = float(cpts[ev][key])
                _validate_prob(p_true, f"CPT {ev}{key}")
                normalized[ev][key] = p_true
        self.cpts = normalized

    def infer(
        self,
        evidence: Mapping[str, Optional[Any]],
    ) -> Dict[str, Any]:
        observed = _normalize_evidence(evidence)
        observed_mask = _encode_observed_mask(observed)

        unnorm: Dict[Tuple[int, int], float] = {}
        for pph in (0, 1):
            for ppr in (0, 1):
                mass = self._root_prior(pph, ppr)
                for ev_name, ev_val in observed.items():
                    p_true = self.cpts[ev_name][(pph, ppr)]
                    mass *= p_true if ev_val == 1 else (1.0 - p_true)
                unnorm[(pph, ppr)] = mass

        z = sum(unnorm.values())
        if z <= 0.0:
            raise ValueError("Normalization constant Z=0; check priors, CPTs, and evidence.")

        joint: Dict[Tuple[int, int], float] = {
            state: mass / z for state, mass in unnorm.items()
        }

        p_pph = joint[(1, 0)] + joint[(1, 1)]
        p_ppr = joint[(0, 1)] + joint[(1, 1)]

        return {
            "PPH": p_pph,
            "PPR": p_ppr,
            "joint": joint,
            "observed": observed,
            "observedMask": observed_mask,
        }

    @staticmethod
    def encode(p: float) -> int:
        _validate_prob(p, "p")
        return round(p * SCALE)

    @staticmethod
    def decode(p_hat: int) -> float:
        if p_hat < 0 or p_hat > SCALE:
            raise ValueError(f"Encoded value {p_hat} out of range [0, {SCALE}]")
        return float(p_hat) / float(SCALE)

    @staticmethod
    def rounding_error_bound() -> float:
        return 1.0 / (2.0 * SCALE)

    @classmethod
    def infer_from_chain(
        cls,
        w3: Web3,
        cpt_store_contract: Any,
        evidence: Mapping[str, Optional[Any]],
        block_identifier: str | int = "latest",
    ) -> Dict[str, Any]:
        snapshot = cls.load_snapshot_from_chain(
            w3=w3,
            cpt_store_contract=cpt_store_contract,
            block_identifier=block_identifier,
        )
        oracle = cls(
            prior_pph=snapshot.prior_pph,
            prior_ppr=snapshot.prior_ppr,
            cpts=snapshot.cpts,
            bn_instance_id=snapshot.bn_instance_id,
            snapshot_block=snapshot.snapshot_block,
        )
        result = oracle.infer(evidence)
        result["bn_instance_id"] = snapshot.bn_instance_id
        result["snapshot_block"] = snapshot.snapshot_block
        return result

    def submit_to_chain(
        self,
        oracle_controller_contract: Any,
        claim_id: int,
        evidence: Mapping[str, Optional[Any]],
        tx_params: Dict[str, Any],
    ) -> Dict[str, Any]:
        if self.bn_instance_id is None:
            raise RuntimeError(
                "submit_to_chain() requires bn_instance_id; construct via infer_from_chain() or load_snapshot_from_chain()."
            )
        if self.snapshot_block is None:
            raise RuntimeError(
                "submit_to_chain() requires snapshot_block; construct via infer_from_chain() or load_snapshot_from_chain()."
            )

        result = self.infer(evidence)
        p_pph = result["PPH"]
        p_ppr = result["PPR"]
        pph_enc = self.encode(p_pph)
        ppr_enc = self.encode(p_ppr)

        observed = result["observed"]
        observed_mask = result["observedMask"]

        gps = observed.get("GPS", 0)
        pc = observed.get("PC", 0)
        pmd = observed.get("PMD", 0)
        pr = observed.get("PR", 0)

        bn_id_bytes = _hex_to_bytes32(self.bn_instance_id)

        tx_hash = oracle_controller_contract.functions.submitInference(
            claim_id,
            gps,
            pc,
            pmd,
            pr,
            observed_mask,
            pph_enc,
            ppr_enc,
            bn_id_bytes,
            self.snapshot_block,
        ).transact(tx_params)

        log.info(
            "submitInference: claimId=%d PPH=%.6f(%d) PPR=%.6f(%d) mask=0x%X bnId=%s snapshotBlock=%s tx=%s",
            claim_id,
            p_pph,
            pph_enc,
            p_ppr,
            ppr_enc,
            observed_mask,
            self.bn_instance_id,
            self.snapshot_block,
            tx_hash.hex(),
        )

        return {
            "PPH": p_pph,
            "PPR": p_ppr,
            "pph_encoded": pph_enc,
            "ppr_encoded": ppr_enc,
            "observed": observed,
            "observedMask": observed_mask,
            "bn_instance_id": self.bn_instance_id,
            "snapshot_block": self.snapshot_block,
            "tx_hash": tx_hash.hex(),
        }

    @classmethod
    def load_snapshot_from_chain(
        cls,
        w3: Web3,
        cpt_store_contract: Any,
        block_identifier: str | int = "latest",
    ) -> BNSnapshot:
        if not w3.is_connected():
            raise RuntimeError("Web3 is not connected; cannot load BN snapshot from chain.")

        if block_identifier == "latest":
            snapshot_block = int(w3.eth.block_number)
        elif isinstance(block_identifier, int):
            snapshot_block = block_identifier
        else:
            snapshot_block = int(w3.eth.block_number)

        pph_scaled, ppr_scaled, flat_cpts, bn_instance_id_raw = (
            cpt_store_contract.functions.getCPTSnapshot().call(
                block_identifier=block_identifier
            )
        )

        prior_pph = _scaled_to_prob(pph_scaled)
        prior_ppr = _scaled_to_prob(ppr_scaled)

        cpts: Dict[str, Dict[Tuple[int, int], float]] = {
            ev: {} for ev in EVIDENCE_NAMES
        }
        for i, ev in enumerate(EVIDENCE_NAMES):
            base = i * 4
            cpts[ev][(0, 0)] = _scaled_to_prob(flat_cpts[base + 0])
            cpts[ev][(0, 1)] = _scaled_to_prob(flat_cpts[base + 1])
            cpts[ev][(1, 0)] = _scaled_to_prob(flat_cpts[base + 2])
            cpts[ev][(1, 1)] = _scaled_to_prob(flat_cpts[base + 3])

        if isinstance(bn_instance_id_raw, (bytes, bytearray)):
            bn_id_hex = "0x" + bytes(bn_instance_id_raw).hex()
        else:
            bn_id_hex = str(bn_instance_id_raw)

        log.debug(
            "Loaded BNSnapshot: block=%d bnId=%s pph=%.6f ppr=%.6f",
            snapshot_block,
            bn_id_hex,
            prior_pph,
            prior_ppr,
        )

        return BNSnapshot(
            prior_pph=prior_pph,
            prior_ppr=prior_ppr,
            cpts=cpts,
            bn_instance_id=bn_id_hex,
            snapshot_block=snapshot_block,
        )

    def _root_prior(self, pph_state: int, ppr_state: int) -> float:
        p_pph = self.prior_pph if pph_state == 1 else (1.0 - self.prior_pph)
        p_ppr = self.prior_ppr if ppr_state == 1 else (1.0 - self.prior_ppr)
        return p_pph * p_ppr


def _normalize_evidence(
    evidence: Mapping[str, Optional[Any]],
) -> Dict[str, int]:
    observed: Dict[str, int] = {}
    for ev_name in EVIDENCE_NAMES:
        if ev_name not in evidence:
            continue
        value = evidence[ev_name]
        if value is None or value == "⊥":
            continue
        if value not in (0, 1, False, True):
            raise ValueError(
                f"Evidence '{ev_name}' must be 0, 1, None, or '⊥'; got {value!r}"
            )
        observed[ev_name] = int(value)
    return observed


def _encode_observed_mask(observed: Mapping[str, int]) -> int:
    mask = 0
    if "GPS" in observed:
        mask |= 0x01
    if "PC" in observed:
        mask |= 0x02
    if "PMD" in observed:
        mask |= 0x04
    if "PR" in observed:
        mask |= 0x08

    if mask < 0 or mask > MAX_OBSERVED_MASK:
        raise ValueError(f"Observed mask out of range: {mask}")
    return mask


def decode_observed_mask(mask: int) -> Dict[str, bool]:
    if mask < 0 or mask > MAX_OBSERVED_MASK:
        raise ValueError(f"Observed mask out of range: {mask}")
    return {
        "GPS": bool(mask & 0x01),
        "PC": bool(mask & 0x02),
        "PMD": bool(mask & 0x04),
        "PR": bool(mask & 0x08),
    }


def canonicalize_evidence_for_chain(
    evidence: Mapping[str, Optional[Any]],
) -> Tuple[Dict[str, int], int]:
    observed = _normalize_evidence(evidence)
    mask = _encode_observed_mask(observed)
    values = {
        "GPS": observed.get("GPS", 0),
        "PC": observed.get("PC", 0),
        "PMD": observed.get("PMD", 0),
        "PR": observed.get("PR", 0),
    }
    return values, mask


def _hex_to_bytes32(hex_str: str) -> bytes:
    s = hex_str[2:] if hex_str.startswith("0x") else hex_str
    raw = bytes.fromhex(s)
    if len(raw) != 32:
        raise ValueError(f"bn_instance_id must be 32 bytes; got {len(raw)}")
    return raw


def _scaled_to_prob(x: int) -> float:
    if x < 0 or x > SCALE:
        raise ValueError(f"Scaled value {x} outside [0, {SCALE}]")
    return float(x) / float(SCALE)


def _validate_prob(p: float, name: str) -> None:
    if not (0.0 <= float(p) <= 1.0):
        raise ValueError(f"Probability '{name}' must be in [0, 1]; got {p}")