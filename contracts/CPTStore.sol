// SPDX-License-Identifier: MIT
pragma solidity ^0.8.17;

/// @title  CPTStore
/// @notice On-chain repository for LUNA Bayesian Network parameters.
///         Stores priors P(PPH=true) and P(PPR=true), and Conditional
///         Probability Table (CPT) entries for four binary evidence nodes
///         (GPS, PC, PMD, PR).  All probabilities are scaled integers
///         in [0, SCALE].
///
/// @dev    Design invariants (referenced in paper Section IV-A):
///         1. Every parameter write is range-checked: 0 <= p <= SCALE.
///         2. Every parameter write emits an indexed event, enabling full
///            reconstruction of BN parameter history from the ledger.
///         3. Write access is restricted to the contract owner (deployer).
///         4. A bnInstanceId fingerprint is recomputed lazily (on the first
///            read after any write), keeping per-write gas O(1) in storage
///            writes and making initialization cost O(N_e) in SSTORE ops.
///         5. getCPTSnapshot() returns all 18 parameters + bnInstanceId in
///            a single call, reducing oracle BN reconstruction to 1 RPC.
///         6. block.number is available in every transaction receipt;
///            the oracle records it externally to bind each posterior
///            submission to the parameter state at reconstruction time.

contract CPTStore {

    // ----------------------------------------------------------------
    // Constants
    // ----------------------------------------------------------------

    /// @notice Global scale factor.  All probabilities p in [0,1] are
    ///         stored as round(p * SCALE).  Absolute rounding error is
    ///         bounded by 1 / (2 * SCALE) = 5e-7.
    uint256 public constant SCALE = 1_000_000;

    /// @notice Number of binary evidence nodes supported.
    ///         0 = GPS, 1 = PC, 2 = PMD, 3 = PR.
    uint8 public constant NUM_EVIDENCE = 4;

    // ----------------------------------------------------------------
    // State
    // ----------------------------------------------------------------

    /// @notice Contract owner; the only address permitted to write
    ///         parameters.  Set to msg.sender at deployment.
    address public owner;

    /// @notice Scaled prior P(PPH = true).
    ///         PPH and PPR are modeled as marginally independent root
    ///         nodes; they need not sum to SCALE.  Mutual exclusivity
    ///         of claim types is enforced at the application layer.
    uint256 public priorPPH;

    /// @notice Scaled prior P(PPR = true).
    uint256 public priorPPR;

    /// @notice keccak256 fingerprint of the current parameter state.
    ///         Recomputed lazily after any write.  The oracle records
    ///         this value alongside each posterior submission so that
    ///         auditors can verify which parameter version was active.
    bytes32 public bnInstanceId;

    /// @dev    CPT storage: evidenceTrueCPT[i][pph][ppr] =
    ///         P(evidence_i = true | PPH = pph, PPR = ppr), scaled.
    ///         Indices: i in [0, NUM_EVIDENCE), pph/ppr in {0, 1}.
    mapping(uint8 =>
        mapping(uint8 =>
            mapping(uint8 => uint256))) private evidenceTrueCPT;

    /// @dev    Dirty flag for lazy bnInstanceId recomputation.
    ///         Set to true on every parameter write; cleared when
    ///         bnInstanceId is recomputed in _refreshIfDirty().
    bool private _dirty;

    // ----------------------------------------------------------------
    // Events
    // ----------------------------------------------------------------

    /// @notice Emitted when contract ownership is transferred.
    event OwnershipTransferred(
        address indexed previousOwner,
        address indexed newOwner
    );

    /// @notice Emitted on every prior update.
    ///         Indexed on bnInstanceId so callers can filter parameter
    ///         history by snapshot.
    /// @param  priorPPHScaled   New scaled P(PPH = true).
    /// @param  priorPPRScaled   New scaled P(PPR = true).
    /// @param  bnInstanceId     Updated parameter fingerprint.
    event PriorsUpdated(
        uint256 priorPPHScaled,
        uint256 priorPPRScaled,
        bytes32 indexed bnInstanceId
    );

    /// @notice Emitted on every CPT entry update.
    ///         Three indexed fields (EVM topic limit) allow filtering
    ///         by evidence node and parent configuration.
    ///         bnInstanceId is stored in event data (not indexed) and
    ///         is readable via log data scan or getCPTSnapshot().
    /// @param  evidenceIndex    Evidence node (0=GPS,1=PC,2=PMD,3=PR).
    /// @param  pphState         Parent PPH state (0 or 1).
    /// @param  pprState         Parent PPR state (0 or 1).
    /// @param  pTrueScaled      New scaled P(evidence=true|parents).
    /// @param  bnInstanceId     Updated parameter fingerprint (data).
    event EvidenceCPTUpdated(
        uint8  indexed evidenceIndex,
        uint8  indexed pphState,
        uint8  indexed pprState,
        uint256        pTrueScaled,
        bytes32        bnInstanceId
    );

    // ----------------------------------------------------------------
    // Modifiers
    // ----------------------------------------------------------------

    modifier onlyOwner() {
        require(msg.sender == owner, "CPTStore: caller is not owner");
        _;
    }

    // ----------------------------------------------------------------
    // Constructor
    // ----------------------------------------------------------------

    /// @notice Deploys CPTStore.
    ///         Priors are initialised to neutral values (0.5 / 0.5).
    ///         Call setPriors() and setEvidenceCPT() after deployment
    ///         to configure the desired parameter set before any
    ///         inference submissions are accepted.
    constructor() {
        owner = msg.sender;
        // Neutral priors: P(PPH) = P(PPR) = 0.5
        // Asymmetric priors are set via setPriors() in the deploy script.
        priorPPH = 500_000;
        priorPPR = 500_000;
        // All CPT entries default to 0 (uninitialised).
        // setEvidenceCPT() must be called for each entry before use.
        _dirty = true;
        _refreshIfDirty();
    }

    // ----------------------------------------------------------------
    // Owner administration
    // ----------------------------------------------------------------

    /// @notice Transfers contract ownership to newOwner.
    /// @param  newOwner  Address of the new owner.  Must be non-zero.
    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "CPTStore: zero address");
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }

    // ----------------------------------------------------------------
    // Parameter writes
    // ----------------------------------------------------------------

    /// @notice Updates both root-node priors atomically.
    /// @dev    Each value is independently range-checked.  The two
    ///         priors need not sum to SCALE; see contract-level note
    ///         on marginal independence.
    /// @param  newPriorPPH  Scaled P(PPH = true) in [0, SCALE].
    /// @param  newPriorPPR  Scaled P(PPR = true) in [0, SCALE].
    function setPriors(
        uint256 newPriorPPH,
        uint256 newPriorPPR
    ) external onlyOwner {
        require(newPriorPPH <= SCALE, "CPTStore: PPH prior > 1");
        require(newPriorPPR <= SCALE, "CPTStore: PPR prior > 1");
        priorPPH = newPriorPPH;
        priorPPR = newPriorPPR;
        _dirty = true;
        _refreshIfDirty();
        emit PriorsUpdated(priorPPH, priorPPR, bnInstanceId);
    }

    /// @notice Sets one CPT entry:
    ///         P(evidence_evidenceIndex = true | PPH=pphState, PPR=pprState).
    /// @dev    Gas cost per call: O(1) SSTOREs (one data slot + dirty flag).
    ///         bnInstanceId is recomputed once on the subsequent read,
    ///         keeping initialization cost O(N_e) in SSTORE operations.
    /// @param  evidenceIndex  Evidence node index in [0, NUM_EVIDENCE).
    /// @param  pphState       PPH parent state in {0, 1}.
    /// @param  pprState       PPR parent state in {0, 1}.
    /// @param  pTrueScaled    Scaled probability in [0, SCALE].
    function setEvidenceCPT(
        uint8   evidenceIndex,
        uint8   pphState,
        uint8   pprState,
        uint256 pTrueScaled
    ) external onlyOwner {
        require(evidenceIndex < NUM_EVIDENCE, "CPTStore: bad evidence index");
        require(pphState  < 2,               "CPTStore: bad PPH state");
        require(pprState  < 2,               "CPTStore: bad PPR state");
        require(pTrueScaled <= SCALE,        "CPTStore: probability > 1");

        evidenceTrueCPT[evidenceIndex][pphState][pprState] = pTrueScaled;
        _dirty = true;
        _refreshIfDirty();

        emit EvidenceCPTUpdated(
            evidenceIndex,
            pphState,
            pprState,
            pTrueScaled,
            bnInstanceId
        );
    }

    // ----------------------------------------------------------------
    // Parameter reads
    // ----------------------------------------------------------------

    /// @notice Returns a single CPT entry.
    /// @param  evidenceIndex  Evidence node index in [0, NUM_EVIDENCE).
    /// @param  pphState       PPH parent state in {0, 1}.
    /// @param  pprState       PPR parent state in {0, 1}.
    /// @return Scaled P(evidence = true | PPH=pphState, PPR=pprState).
    function getEvidenceTrueCPT(
        uint8 evidenceIndex,
        uint8 pphState,
        uint8 pprState
    ) external view returns (uint256) {
        require(evidenceIndex < NUM_EVIDENCE, "CPTStore: bad evidence index");
        require(pphState  < 2,               "CPTStore: bad PPH state");
        require(pprState  < 2,               "CPTStore: bad PPR state");
        return evidenceTrueCPT[evidenceIndex][pphState][pprState];
    }

    /// @notice Returns all 18 BN parameters and the current bnInstanceId
    ///         in a single call, reducing off-chain BN reconstruction
    ///         to one RPC round-trip.
    ///
    /// @dev    cpts layout (length 16):
    ///         cpts[ i*4 + pphState*2 + pprState ]
    ///           = P(evidence_i = true | PPH=pphState, PPR=pprState)
    ///         for i in [0, NUM_EVIDENCE), pphState/pprState in {0,1}.
    ///
    /// @return pph   Scaled prior P(PPH = true).
    /// @return ppr   Scaled prior P(PPR = true).
    /// @return cpts  Flattened CPT array, length NUM_EVIDENCE * 4 = 16.
    /// @return id    Current bnInstanceId fingerprint.
    function getCPTSnapshot()
        external
        view
        returns (
            uint256          pph,
            uint256          ppr,
            uint256[16] memory cpts,
            bytes32          id
        )
    {
        pph = priorPPH;
        ppr = priorPPR;
        for (uint8 i = 0; i < NUM_EVIDENCE; i++) {
            cpts[uint256(i) * 4 + 0] = evidenceTrueCPT[i][0][0];
            cpts[uint256(i) * 4 + 1] = evidenceTrueCPT[i][0][1];
            cpts[uint256(i) * 4 + 2] = evidenceTrueCPT[i][1][0];
            cpts[uint256(i) * 4 + 3] = evidenceTrueCPT[i][1][1];
        }
        id = bnInstanceId;
    }

    // ----------------------------------------------------------------
    // Internal
    // ----------------------------------------------------------------

    /// @dev Recomputes bnInstanceId from the full current parameter
    ///      state and clears the dirty flag.  Called immediately after
    ///      every write so that bnInstanceId is always current when
    ///      read by external callers or emitted in events.
    function _refreshIfDirty() internal {
        if (!_dirty) return;
        bnInstanceId = keccak256(abi.encodePacked(
            "LUNA/CPTStore/v1",
            priorPPH,
            priorPPR,
            evidenceTrueCPT[0][0][0], evidenceTrueCPT[0][0][1],
            evidenceTrueCPT[0][1][0], evidenceTrueCPT[0][1][1],
            evidenceTrueCPT[1][0][0], evidenceTrueCPT[1][0][1],
            evidenceTrueCPT[1][1][0], evidenceTrueCPT[1][1][1],
            evidenceTrueCPT[2][0][0], evidenceTrueCPT[2][0][1],
            evidenceTrueCPT[2][1][0], evidenceTrueCPT[2][1][1],
            evidenceTrueCPT[3][0][0], evidenceTrueCPT[3][0][1],
            evidenceTrueCPT[3][1][0], evidenceTrueCPT[3][1][1]
        ));
        _dirty = false;
    }
}
