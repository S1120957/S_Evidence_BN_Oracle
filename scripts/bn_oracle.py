"""
bn_oracle.py — LUNA off-chain Bayesian Network inference engine.

Paper reference: LUNA: Scalable Evidence-Based Oracles Using Bayesian Networks
                 Sections III-C (BN formulation), III-D (fixed-point encoding),
                 III-E (claim lifecycle), IV-B (off-chain oracle).

BN model
--------
Roots    : PPH, PPR  in {0, 1}   (marginally independent)
Evidence : GPS, PC, PMD, PR  in {0, 1}   (conditionally independent given roots)

Factorization (Equation 1 in paper):
    P(PPH, PPR, E) = P(PPH) P(PPR) * prod_i P(E_i | PPH, PPR)

Posterior (Equation 2 in paper):
    P(PPH, PPR | e) ∝ P(PPH) P(PPR) * prod_{E_i observed} P(E_i=e_i | PPH, PPR)

Missing evidence (⊥ variables) is marginalized out by omitting the
corresponding factor from the product — not by substituting a default value.

Fixed-point encoding (Section III-D):
    p_hat = round(p * SCALE),   p_hat in {0, …, SCALE}
    |p - p_hat/SCALE| ≤ 1/(2*SCALE) = 5e-7  for SCALE=1_000_000

CPT array layout (matches CPTStore.getCPTSnapshot()):
    flat_cpts[i*4 + pph_state*2 + ppr_state]
        = P(evidence_i=true | PPH=pph_state, PPR=ppr_state)
    i.e.
        base+0: (pph=0, ppr=0)
        base+1: (pph=0, ppr=1)
        base+2: (pph=1, ppr=0)
        base+3: (pph=1, ppr=1)
    This layout is consistent with CPTStore.setEvidenceCPT(i, pphState, pprState, ...)
    and evidenceTrueCPT[i][pphState][pprState].
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from web3 import Web3

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — must match CPTStore.SCALE
# ---------------------------------------------------------------------------

SCALE: int = 1_000_000

#: Canonical evidence node order; matches CPTStore evidenceIndex 0..3.
EVIDENCE_NAMES: Tuple[str, ...] = ("GPS", "PC", "PMD", "PR")
EVIDENCE_INDEX: Dict[str, int] = {name: i for i, name in enumerate(EVIDENCE_NAMES)}


# ---------------------------------------------------------------------------
# Immutable snapshot of on-chain BN parameters
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BNSnapshot:
    """
    Immutable record of BN parameters read from CPTStore at a specific block.

    Attributes
    ----------
    prior_pph:       P(PPH=true), float in [0, 1].
    prior_ppr:       P(PPR=true), float in [0, 1].
    cpts:            {evidence_name: {(pph_state, ppr_state): p_true}}.
    bn_instance_id:  hex string of CPTStore.bnInstanceId() at snapshot_block.
                     Passed to OracleController.submitInference() as
                     expectedBnInstanceId to detect stale-parameter races.
    snapshot_block:  Block number at which parameters were read.
                     Recorded alongside each posterior submission so auditors
                     can verify which parameter version was in effect.
    """
    prior_pph:      float
    prior_ppr:      float
    cpts:           Dict[str, Dict[Tuple[int, int], float]]
    bn_instance_id: str
    snapshot_block: int


# ---------------------------------------------------------------------------
# Inference engine
# ---------------------------------------------------------------------------

class BNOracle:
    """
    Closed-form Bayesian Network inference engine for the LUNA healthcare
    claim-verification model.

    The BN is a Naive Bayes structure:
      - Two binary root nodes PPH and PPR (marginally independent).
      - Four binary evidence nodes GPS, PC, PMD, PR, each conditioned
        on both roots independently (conditional independence assumption).

    This structure reduces exact inference to an O(2^2 * N_e) enumeration
    over 4 root configurations, linear in the number of observed evidence
    variables.  For fixed N_h=2 roots, inference is constant-time.

    Parameters
    ----------
    prior_pph:       P(PPH=true) in [0, 1].
    prior_ppr:       P(PPR=true) in [0, 1].
    cpts:            {evidence_name: {(pph_state, ppr_state): p_true}}
                     All four evidence nodes and all four parent combinations
                     must be present.
    bn_instance_id:  CPTStore.bnInstanceId() at construction time (optional).
    snapshot_block:  Block at which parameters were loaded (optional).
    """

    def __init__(
        self,
        prior_pph:       float,
        prior_ppr:       float,
        cpts:            Mapping[str, Mapping[Tuple[int, int], float]],
        bn_instance_id:  Optional[str] = None,
        snapshot_block:  Optional[int] = None,
    ) -> None:
        _validate_prob(prior_pph, "prior_pph")
        _validate_prob(prior_ppr, "prior_ppr")

        self.prior_pph      = float(prior_pph)
        self.prior_ppr      = float(prior_ppr)
        self.bn_instance_id = bn_instance_id
        self.snapshot_block = snapshot_block

        # Validate and normalise CPTs
        normalised: Dict[str, Dict[Tuple[int, int], float]] = {}
        for ev in EVIDENCE_NAMES:
            if ev not in cpts:
                raise ValueError(f"CPT missing for evidence node '{ev}'")
            normalised[ev] = {}
            for key in ((0, 0), (0, 1), (1, 0), (1, 1)):
                if key not in cpts[ev]:
                    raise ValueError(
                        f"CPT entry missing: {ev}{key}"
                    )
                p_true = float(cpts[ev][key])
                _validate_prob(p_true, f"CPT {ev}{key}")
                normalised[ev][key] = p_true
        self.cpts = normalised

    # ------------------------------------------------------------------
    # Core inference
    # ------------------------------------------------------------------

    def infer(
        self,
        evidence: Mapping[str, Optional[Any]],
    ) -> Dict[str, Any]:
        """
        Exact closed-form inference from the loaded BN parameters.

        Implements Equation (2) from the paper:
            P(PPH, PPR | e) ∝ P(PPH) P(PPR) ∏_{E_i observed} P(E_i=e_i | PPH, PPR)

        Missing / None / '⊥' evidence variables are marginalized out by
        omitting their factor from the product.  This is equivalent to
        summing over their possible values, exploiting conditional independence.

        Parameters
        ----------
        evidence : mapping of evidence name -> observed value.
            Observed values: 0, 1, False, True.
            Missing / unobserved: absent, None, or '⊥'.
            Example: {"GPS": 1, "PC": 0, "PMD": None, "PR": "⊥"}

        Returns
        -------
        dict with keys:
            "PPH"   : P(PPH=1 | e), float in [0, 1].
            "PPR"   : P(PPR=1 | e), float in [0, 1].
            "joint" : {(pph, ppr): posterior mass}, diagnostic only.
                      Not committed on-chain; only the marginals are.
        """
        observed = _normalize_evidence(evidence)

        # Enumerate all 4 root configurations
        unnorm: Dict[Tuple[int, int], float] = {}
        for pph in (0, 1):
            for ppr in (0, 1):
                mass = self._root_prior(pph, ppr)
                for ev_name, ev_val in observed.items():
                    p_true = self.cpts[ev_name][(pph, ppr)]
                    mass  *= p_true if ev_val == 1 else (1.0 - p_true)
                unnorm[(pph, ppr)] = mass

        z = sum(unnorm.values())
        if z <= 0.0:
            raise ValueError(
                "Normalization constant Z=0; check priors, CPTs, and evidence."
            )

        joint: Dict[Tuple[int, int], float] = {
            state: mass / z for state, mass in unnorm.items()
        }

        # Marginals by summing over the complementary root
        p_pph = joint[(1, 0)] + joint[(1, 1)]
        p_ppr = joint[(0, 1)] + joint[(1, 1)]

        return {"PPH": p_pph, "PPR": p_ppr, "joint": joint}

    # ------------------------------------------------------------------
    # Fixed-point encoding (Section III-D)
    # ------------------------------------------------------------------

    @staticmethod
    def encode(p: float) -> int:
        """
        Encode a probability p in [0,1] to a scaled integer.

        p_hat = round(p * SCALE)

        Absolute rounding error: |p - p_hat/SCALE| ≤ 1/(2*SCALE) = 5e-7.

        The encoded value is passed to OracleController.submitInference()
        as posteriorPPH or posteriorPPR.
        """
        _validate_prob(p, "p")
        return round(p * SCALE)

    @staticmethod
    def decode(p_hat: int) -> float:
        """
        Decode a scaled integer back to a probability.

        p = p_hat / SCALE

        Used to verify on-chain values against off-chain posteriors.
        """
        if p_hat < 0 or p_hat > SCALE:
            raise ValueError(
                f"Encoded value {p_hat} out of range [0, {SCALE}]"
            )
        return float(p_hat) / float(SCALE)

    @staticmethod
    def rounding_error_bound() -> float:
        """
        Returns the theoretical upper bound on absolute rounding error
        introduced by fixed-point encoding: 1 / (2 * SCALE).
        """
        return 1.0 / (2.0 * SCALE)

    # ------------------------------------------------------------------
    # Chain-integrated inference
    # ------------------------------------------------------------------

    @classmethod
    def infer_from_chain(
        cls,
        w3:                   Web3,
        cpt_store_contract:   Any,
        evidence:             Mapping[str, Optional[Any]],
        block_identifier:     str | int = "latest",
    ) -> Dict[str, Any]:
        """
        Reconstruct BN from CPTStore, run inference, return posteriors
        and snapshot metadata.

        This is the primary entry point for the off-chain oracle pipeline:
            1. Load snapshot from CPTStore.getCPTSnapshot() at block_identifier.
            2. Run closed-form inference.
            3. Return posteriors + bn_instance_id + snapshot_block.

        The caller must pass result["bn_instance_id"] to
        OracleController.submitInference() as expectedBnInstanceId.
        This enforces the stale-parameter mitigation described in the
        paper's threat model: if CPTStore parameters change between
        reconstruction and submission, the contract reverts.

        Returns
        -------
        dict with keys:
            "PPH"               : P(PPH=1 | e).
            "PPR"               : P(PPR=1 | e).
            "joint"             : {(pph, ppr): posterior mass} (diagnostic).
            "bn_instance_id"    : hex bnInstanceId from CPTStore snapshot.
            "snapshot_block"    : block number at which BN was reconstructed.
        """
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
        claim_id:                   int,
        evidence:                   Mapping[str, Optional[Any]],
        tx_params:                  Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Run inference and submit the result to OracleController.submitInference().

        This method closes the gap between infer_from_chain() and the
        contract call: it ensures bn_instance_id is always taken from
        the same snapshot used for inference, never supplied separately
        by the caller (which could introduce a stale-id bug).

        Parameters
        ----------
        oracle_controller_contract : web3.contract instance for OracleController.
        claim_id                   : on-chain claim identifier.
        evidence                   : evidence assignment (see infer()).
        tx_params                  : web3 transaction parameters
                                     (e.g. {"from": operator_address}).

        Returns
        -------
        dict with keys:
            "PPH"            : float posterior.
            "PPR"            : float posterior.
            "pph_encoded"    : int scaled posterior committed on-chain.
            "ppr_encoded"    : int scaled posterior committed on-chain.
            "bn_instance_id" : hex snapshot id used in submission.
            "snapshot_block" : block at which BN was reconstructed.
            "tx_hash"        : hex transaction hash.
        """
        if self.bn_instance_id is None:
            raise RuntimeError(
                "submit_to_chain() requires bn_instance_id; "
                "construct via infer_from_chain() or load_snapshot_from_chain()."
            )

        result   = self.infer(evidence)
        p_pph    = result["PPH"]
        p_ppr    = result["PPR"]
        pph_enc  = self.encode(p_pph)
        ppr_enc  = self.encode(p_ppr)
        obs      = _normalize_evidence(evidence)

        gps = obs.get("GPS", 0)
        pc  = obs.get("PC",  0)
        pmd = obs.get("PMD", 0)
        pr  = obs.get("PR",  0)

        bn_id_bytes = bytes.fromhex(
            self.bn_instance_id[2:]
            if self.bn_instance_id.startswith("0x")
            else self.bn_instance_id
        )

        tx_hash = oracle_controller_contract.functions.submitInference(
            claim_id,
            gps, pc, pmd, pr,
            pph_enc,
            ppr_enc,
            bn_id_bytes,
        ).transact(tx_params)

        log.info(
            "submitInference: claimId=%d PPH=%.6f(%d) PPR=%.6f(%d) "
            "bnId=%s block=%s tx=%s",
            claim_id, p_pph, pph_enc, p_ppr, ppr_enc,
            self.bn_instance_id, self.snapshot_block,
            tx_hash.hex(),
        )

        return {
            "PPH":            p_pph,
            "PPR":            p_ppr,
            "pph_encoded":    pph_enc,
            "ppr_encoded":    ppr_enc,
            "bn_instance_id": self.bn_instance_id,
            "snapshot_block": self.snapshot_block,
            "tx_hash":        tx_hash.hex(),
        }

    # ------------------------------------------------------------------
    # Chain reconstruction
    # ------------------------------------------------------------------

    @classmethod
    def load_snapshot_from_chain(
        cls,
        w3:                   Web3,
        cpt_store_contract:   Any,
        block_identifier:     str | int = "latest",
    ) -> BNSnapshot:
        """
        Load BN parameters from CPTStore.getCPTSnapshot() in a single RPC call.

        CPT array layout (Solidity):
            flat_cpts[i*4 + pph_state*2 + ppr_state]
                = P(evidence_i=true | PPH=pph_state, PPR=ppr_state)

        This matches evidenceTrueCPT[i][pphState][pprState] in CPTStore.sol:
            base+0: evidenceTrueCPT[i][0][0] => (pph=0, ppr=0)
            base+1: evidenceTrueCPT[i][0][1] => (pph=0, ppr=1)
            base+2: evidenceTrueCPT[i][1][0] => (pph=1, ppr=0)
            base+3: evidenceTrueCPT[i][1][1] => (pph=1, ppr=1)
        Mapping verified consistent between CPTStore.sol and this decoder.

        Parameters
        ----------
        w3                  : connected Web3 instance.
        cpt_store_contract  : web3.contract instance for CPTStore.
        block_identifier    : block tag ("latest") or integer block number.
                              The block number is stored in BNSnapshot.snapshot_block
                              and returned with every inference result.

        Returns
        -------
        BNSnapshot (immutable).
        """
        if not w3.is_connected():
            raise RuntimeError(
                "Web3 is not connected; cannot load BN snapshot from chain."
            )

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
            base             = i * 4
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
            snapshot_block, bn_id_hex, prior_pph, prior_ppr,
        )

        return BNSnapshot(
            prior_pph=prior_pph,
            prior_ppr=prior_ppr,
            cpts=cpts,
            bn_instance_id=bn_id_hex,
            snapshot_block=snapshot_block,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _root_prior(self, pph_state: int, ppr_state: int) -> float:
        """
        P(PPH=pph_state) * P(PPR=ppr_state).

        PPH and PPR are marginally independent root nodes; their joint
        prior factorizes as the product of their individual priors.
        """
        p_pph = self.prior_pph if pph_state == 1 else (1.0 - self.prior_pph)
        p_ppr = self.prior_ppr if ppr_state == 1 else (1.0 - self.prior_ppr)
        return p_pph * p_ppr


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _normalize_evidence(
    evidence: Mapping[str, Optional[Any]],
) -> Dict[str, int]:
    """
    Filter evidence dict to observed variables only.

    Missing keys, None, and '⊥' values are treated as unobserved and
    excluded from the result; they are marginalized out in infer().

    Returns
    -------
    Dict mapping evidence name -> 0 or 1 for observed variables only.
    """
    observed: Dict[str, int] = {}
    for ev_name in EVIDENCE_NAMES:
        if ev_name not in evidence:
            continue
        value = evidence[ev_name]
        if value is None or value == "⊥":
            continue
        if value not in (0, 1, False, True):
            raise ValueError(
                f"Evidence '{ev_name}' must be 0, 1, None, or '⊥'; "
                f"got {value!r}"
            )
        observed[ev_name] = int(value)
    return observed


def _scaled_to_prob(x: int) -> float:
    """
    Convert a SCALE-encoded integer to a float probability.
    Raises ValueError if x is outside [0, SCALE].
    """
    if x < 0 or x > SCALE:
        raise ValueError(
            f"Scaled value {x} outside [0, {SCALE}]"
        )
    return float(x) / float(SCALE)


def _validate_prob(p: float, name: str) -> None:
    """Raise ValueError if p is not in [0, 1]."""
    if not (0.0 <= float(p) <= 1.0):
        raise ValueError(
            f"Probability '{name}' must be in [0, 1]; got {p}"
        )
