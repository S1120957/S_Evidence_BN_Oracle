// SPDX-License-Identifier: MIT
pragma solidity ^0.8.17;

/// @title  CPTStoreScaling
/// @notice Parameterized variant of CPTStore used solely to measure
///         initialization gas as a function of evidence-node count N_e.
///
/// @dev    This contract reproduces the EXACT storage-write cost of the
///         production CPTStore's setPriors() and setEvidenceCPT():
///           - identical SCALE and range checks
///           - identical per-entry SSTORE (one uint256 slot per CPT entry)
///           - identical lazy-dirty bnInstanceId bookkeeping
///         The only difference is that NUM_EVIDENCE is set at construction
///         time instead of being a hardcoded constant, allowing the same
///         init path to be exercised at N_e = 8 and N_e = 32.
///
///         submitInference / openClaim are intentionally absent: those costs
///         are BN-size-invariant by construction (fixed-width OracleController
///         interface) and are measured separately on the production contracts.
contract CPTStoreScaling {

    uint256 public constant SCALE = 1_000_000;

    /// @notice Number of binary evidence nodes, fixed at deployment.
    uint8 public immutable NUM_EVIDENCE;

    address public owner;
    uint256 public priorPPH;
    uint256 public priorPPR;
    bytes32 public bnInstanceId;

    /// @dev evidenceTrueCPT[i][pph][ppr] = P(E_i=1 | PPH=pph, PPR=ppr), scaled.
    mapping(uint8 =>
        mapping(uint8 =>
            mapping(uint8 => uint256))) private evidenceTrueCPT;

    bool private _dirty;

    event PriorsUpdated(
        uint256 priorPPHScaled,
        uint256 priorPPRScaled,
        bytes32 indexed bnInstanceId
    );

    event EvidenceCPTUpdated(
        uint8   indexed evidenceIndex,
        uint8   indexed pphState,
        uint8   indexed pprState,
        uint256 cptScaled,
        bytes32 bnInstanceId
    );

    modifier onlyOwner() {
        require(msg.sender == owner, "CPTStoreScaling: not owner");
        _;
    }

    /// @param numEvidence Number of evidence nodes (e.g. 8 or 32).
    constructor(uint8 numEvidence) {
        require(numEvidence > 0, "CPTStoreScaling: N must be > 0");
        owner        = msg.sender;
        NUM_EVIDENCE = numEvidence;
        // Neutral priors, matching production constructor default.
        priorPPH = SCALE / 2;
        priorPPR = SCALE / 2;
        _dirty   = true;
    }

    /// @notice Set both priors. Mirrors production setPriors() write cost.
    function setPriors(uint256 pphScaled, uint256 pprScaled)
        external
        onlyOwner
    {
        require(pphScaled <= SCALE, "CPTStoreScaling: PPH > SCALE");
        require(pprScaled <= SCALE, "CPTStoreScaling: PPR > SCALE");
        priorPPH = pphScaled;
        priorPPR = pprScaled;
        _dirty   = true;
        emit PriorsUpdated(pphScaled, pprScaled, bnInstanceId);
    }

    /// @notice Set one CPT entry. Mirrors production setEvidenceCPT() write
    ///         cost exactly: one range check + one SSTORE + one event.
    function setEvidenceCPT(
        uint8   evidenceIndex,
        uint8   pphState,
        uint8   pprState,
        uint256 cptScaled
    )
        external
        onlyOwner
    {
        require(evidenceIndex < NUM_EVIDENCE, "CPTStoreScaling: bad index");
        require(pphState < 2, "CPTStoreScaling: bad pph");
        require(pprState < 2, "CPTStoreScaling: bad ppr");
        require(cptScaled <= SCALE, "CPTStoreScaling: cpt > SCALE");
        evidenceTrueCPT[evidenceIndex][pphState][pprState] = cptScaled;
        _dirty = true;
        emit EvidenceCPTUpdated(
            evidenceIndex, pphState, pprState, cptScaled, bnInstanceId
        );
    }

    /// @notice Read one CPT entry (for verification).
    function getEvidenceCPT(uint8 i, uint8 pph, uint8 ppr)
        external
        view
        returns (uint256)
    {
        return evidenceTrueCPT[i][pph][ppr];
    }
}
