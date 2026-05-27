// SPDX-License-Identifier: MIT
pragma solidity ^0.8.17;

/// @title  EvidenceRegistry
/// @notice Append-only evidence log for LUNA.
///
/// @dev    Paper-aligned design invariants (Section IV-A):
///
///         1. Append-only: records are never modified or deleted.
///            recordEvidence() is the only function that writes to
///            the records mapping; no delete or overwrite path exists.
///
///         2. Monotonic evidenceId allocation via nextEvidenceId.
///
///         3. Each record stores: claimId, four binary evidence bits
///            (gps, pc, pmd, pr), scaled posteriors (PPH, PPR),
///            submitter address, and block.number.
///
///         4. Only the configured controller (OracleController) may
///            append records.  A separate owner role (deployer) holds
///            the sole right to reassign the controller, preventing an
///            attacker who compromises the controller from permanently
///            locking or redirecting the registry.
///
///         5. Transcript reconstruction: the full inference history for
///            any claim is recoverable by filtering EvidenceRecorded
///            events by claimId.  On-chain storage via getEvidence()
///            provides direct access by evidenceId.  This satisfies the
///            paper's audit completeness claim without requiring an
///            on-chain claimId index (which would add SSTORE cost per
///            append and is unnecessary given event availability).
///
///         6. Evidence bits are validated in both OracleController and
///            here.  The double-check is intentional defense-in-depth:
///            EvidenceRegistry must not trust its caller unconditionally.
///            The marginal gas cost is ~4 × 100 gas per append.

contract EvidenceRegistry {

    // ----------------------------------------------------------------
    // Constants
    // ----------------------------------------------------------------

    /// @notice Scale factor shared across LUNA contracts.
    uint256 public constant SCALE = 1_000_000;

    // ----------------------------------------------------------------
    // Roles
    // ----------------------------------------------------------------

    /// @notice Deployer; sole address permitted to reassign controller.
    ///         Separated from controller to prevent controller-key
    ///         compromise from permanently redirecting the registry.
    address public owner;

    /// @notice Address permitted to append evidence records.
    ///         Set to OracleController after deployment.
    address public controller;

    // ----------------------------------------------------------------
    // Evidence log
    // ----------------------------------------------------------------

    /// @notice Monotonically increasing evidence identifier.
    ///         Incremented atomically on each recordEvidence() call.
    uint256 public nextEvidenceId;

    /// @notice Full evidence record.
    /// @param  claimId       Internal LUNA claim identifier.
    /// @param  gps           GPS consistency evidence bit (0 or 1).
    /// @param  pc            Patient confirmation evidence bit (0 or 1).
    /// @param  pmd           Physician medical device log bit (0 or 1).
    /// @param  pr            Physician prescription evidence bit (0 or 1).
    /// @param  posteriorPPH  Scaled P(PPH=true | evidence) at append time.
    /// @param  posteriorPPR  Scaled P(PPR=true | evidence) at append time.
    /// @param  submitter     Address of the submitting oracle operator.
    /// @param  blockNumber   Block at which this record was appended.
    struct EvidenceRecord {
        uint256 claimId;
        uint8   gps;
        uint8   pc;
        uint8   pmd;
        uint8   pr;
        uint256 posteriorPPH;
        uint256 posteriorPPR;
        address submitter;
        uint256 blockNumber;
    }

    /// @dev    evidenceId => EvidenceRecord.  Private; access via
    ///         getEvidence() or by filtering EvidenceRecorded events.
    mapping(uint256 => EvidenceRecord) private records;

    // ----------------------------------------------------------------
    // Events
    // ----------------------------------------------------------------

    /// @notice Emitted when ownership is transferred.
    event OwnershipTransferred(
        address indexed previousOwner,
        address indexed newOwner
    );

    /// @notice Emitted when the controller is reassigned.
    event ControllerUpdated(
        address indexed previousController,
        address indexed newController
    );

    /// @notice Emitted on every evidence append.
    ///         evidenceId, claimId, and submitter are indexed (EVM
    ///         3-topic limit).  All other fields are in event data and
    ///         recoverable via log scanning or getEvidence().
    ///         Filtering by claimId reconstructs the full inference
    ///         transcript for any claim without on-chain enumeration.
    event EvidenceRecorded(
        uint256 indexed evidenceId,
        uint256 indexed claimId,
        address indexed submitter,
        uint8           gps,
        uint8           pc,
        uint8           pmd,
        uint8           pr,
        uint256         posteriorPPH,
        uint256         posteriorPPR,
        uint256         blockNumber
    );

    // ----------------------------------------------------------------
    // Modifiers
    // ----------------------------------------------------------------

    modifier onlyOwner() {
        require(
            msg.sender == owner,
            "EvidenceRegistry: caller is not owner"
        );
        _;
    }

    modifier onlyController() {
        require(
            msg.sender == controller,
            "EvidenceRegistry: caller is not controller"
        );
        _;
    }

    // ----------------------------------------------------------------
    // Constructor
    // ----------------------------------------------------------------

    /// @notice Deploys EvidenceRegistry.
    ///         controller is set separately via setController() after
    ///         OracleController is deployed, to avoid circular
    ///         deployment dependency.
    constructor() {
        owner = msg.sender;
    }

    // ----------------------------------------------------------------
    // Owner administration
    // ----------------------------------------------------------------

    /// @notice Transfers ownership to newOwner.
    /// @param  newOwner  Must be non-zero.
    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "EvidenceRegistry: zero address");
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }

    /// @notice Assigns or reassigns the controller.
    /// @dev    Only the owner (deployer) may call this, preventing a
    ///         compromised controller from redirecting the registry.
    /// @param  newController  Address of the OracleController contract.
    function setController(address newController) external onlyOwner {
        require(
            newController != address(0),
            "EvidenceRegistry: zero controller"
        );
        emit ControllerUpdated(controller, newController);
        controller = newController;
    }

    // ----------------------------------------------------------------
    // Append
    // ----------------------------------------------------------------

    /// @notice Appends an immutable evidence record.
    /// @dev    Defense-in-depth: evidence bits and posteriors are
    ///         validated here even though OracleController validates
    ///         them first.  EvidenceRegistry must not blindly trust
    ///         its caller.
    ///
    ///         posteriorPPH and posteriorPPR are checked independently
    ///         against SCALE.  They need not sum to SCALE because PPH
    ///         and PPR are marginally independent root nodes in the BN.
    ///
    /// @param  claimId       Internal LUNA claim identifier.
    /// @param  gps           GPS evidence bit in {0, 1}.
    /// @param  pc            Patient confirmation bit in {0, 1}.
    /// @param  pmd           Physician device log bit in {0, 1}.
    /// @param  pr            Physician prescription bit in {0, 1}.
    /// @param  posteriorPPH  Scaled P(PPH=true|e) in [0, SCALE].
    /// @param  posteriorPPR  Scaled P(PPR=true|e) in [0, SCALE].
    /// @param  submitter     Oracle operator address; must be non-zero.
    /// @return evidenceId    Monotonically allocated record identifier.
    function recordEvidence(
        uint256 claimId,
        uint8   gps,
        uint8   pc,
        uint8   pmd,
        uint8   pr,
        uint256 posteriorPPH,
        uint256 posteriorPPR,
        address submitter
    ) external onlyController returns (uint256 evidenceId) {
        require(gps < 2,   "EvidenceRegistry: bad gps");
        require(pc  < 2,   "EvidenceRegistry: bad pc");
        require(pmd < 2,   "EvidenceRegistry: bad pmd");
        require(pr  < 2,   "EvidenceRegistry: bad pr");
        require(posteriorPPH <= SCALE, "EvidenceRegistry: PPH > SCALE");
        require(posteriorPPR <= SCALE, "EvidenceRegistry: PPR > SCALE");
        require(submitter != address(0), "EvidenceRegistry: zero submitter");

        evidenceId      = nextEvidenceId;
        nextEvidenceId += 1;

        records[evidenceId] = EvidenceRecord({
            claimId:      claimId,
            gps:          gps,
            pc:           pc,
            pmd:          pmd,
            pr:           pr,
            posteriorPPH: posteriorPPH,
            posteriorPPR: posteriorPPR,
            submitter:    submitter,
            blockNumber:  block.number
        });

        emit EvidenceRecorded(
            evidenceId,
            claimId,
            submitter,
            gps, pc, pmd, pr,
            posteriorPPH,
            posteriorPPR,
            block.number
        );
    }

    // ----------------------------------------------------------------
    // View helpers
    // ----------------------------------------------------------------

    /// @notice Returns the full evidence record for a given evidenceId.
    /// @param  evidenceId  Monotonic record identifier.
    /// @return Full EvidenceRecord struct.
    function getEvidence(uint256 evidenceId)
        external
        view
        returns (EvidenceRecord memory)
    {
        return records[evidenceId];
    }
}
