// SPDX-License-Identifier: MIT
pragma solidity ^0.8.17;

/// -----------------------------------------------------------------------
/// Interfaces
/// -----------------------------------------------------------------------

/// @notice Subset of CPTStore surface used by OracleController.
///         The oracle calls getCPTSnapshot() directly via web3 off-chain;
///         OracleController only needs SCALE() and bnInstanceId() on-chain
///         to enforce snapshot consistency at submission time.
interface ICPTStore {
    function SCALE()        external view returns (uint256);
    function bnInstanceId() external view returns (bytes32);
    /// @notice Returns all 18 BN parameters in one call.
    ///         Used by the off-chain oracle for BN reconstruction.
    ///         Declared here for interface completeness; not called
    ///         on-chain (would add gas proportional to BN size).
    function getCPTSnapshot()
        external
        view
        returns (
            uint256          pph,
            uint256          ppr,
            uint256[16] memory cpts,
            bytes32          id
        );
}

interface IClaimRegistry {
    function SCALE()       external view returns (uint256);
    function openClaim(bytes32 externalKey)
        external returns (uint256 claimId);
    function updatePosterior(
        uint256 claimId,
        uint256 posteriorPPH,
        uint256 posteriorPPR
    ) external;
    function finalizeClaim(uint256 claimId) external;
    function resolveKey(bytes32 externalKey)
        external view returns (uint256 claimId, bool found);
}

interface IEvidenceRegistry {
    function recordEvidence(
        uint256 claimId,
        uint8   gps,
        uint8   pc,
        uint8   pmd,
        uint8   pr,
        uint256 posteriorPPH,
        uint256 posteriorPPR,
        address submitter
    ) external returns (uint256 evidenceId);
}

/// -----------------------------------------------------------------------
/// OracleController
/// -----------------------------------------------------------------------

/// @title  OracleController
/// @notice Sole on-chain entry point for all LUNA claim and inference
///         operations.  ClaimRegistry and EvidenceRegistry accept writes
///         only from this contract.
///
/// @dev    Paper-aligned design invariants (Sections III-E and IV-A):
///
///         1. Single authorised entry point.
///            openClaim(), submitInference(), and finalizeClaim() are
///            the only public paths that mutate ClaimRegistry or
///            EvidenceRegistry.
///
///         2. submitInference() is restricted to onlyOracleOperator.
///            openClaim() and finalizeClaim() are intentionally open
///            to any caller for the current single-operator prototype,
///            matching the paper's description of application-initiated
///            claim management.  In a production deployment these
///            should be gated by an authorizedApp role.
///
///         3. BN snapshot consistency.
///            submitInference() requires the caller to supply the
///            expectedBnInstanceId it observed when reconstructing the
///            BN from CPTStore.  The contract verifies this matches
///            the current CPTStore.bnInstanceId(), rejecting stale
///            submissions and providing the stale-parameter mitigation
///            described in the paper's threat model (Section III-B).
///
///         4. Gas independence from BN size.
///            No on-chain function traverses CPT entries or performs
///            probabilistic arithmetic.  submitInference() performs a
///            fixed number of SSTORE and LOG operations regardless of
///            BN size, satisfying the O(1) per-query gas claim.
///
///         5. Trusted-contract reentrancy.
///            submitInference() calls two external contracts
///            (claimRegistry then evidenceRegistry).  Both are
///            deployed by the same owner with no ETH transfers and no
///            callbacks into OracleController; the Checks-Effects-
///            Interactions pattern is satisfied (all checks precede
///            both calls, and neither callee can re-enter a state-
///            modifying path).  A ReentrancyGuard is therefore not
///            required, but this is explicitly documented here.
///
///         6. Immutable contract references.
///            claimRegistry, evidenceRegistry, and cptStore are set
///            in the constructor and declared immutable, preventing
///            post-deployment substitution attacks.

contract OracleController {

    // ----------------------------------------------------------------
    // Roles
    // ----------------------------------------------------------------

    /// @notice Deployer; may update oracleOperator and nothing else.
    address public owner;

    /// @notice Address of the off-chain oracle operator.
    ///         Only this address may call submitInference().
    address public oracleOperator;

    // ----------------------------------------------------------------
    // Linked contracts (immutable)
    // ----------------------------------------------------------------

    /// @notice ClaimRegistry contract reference.
    IClaimRegistry    public immutable claimRegistry;

    /// @notice EvidenceRegistry contract reference.
    IEvidenceRegistry public immutable evidenceRegistry;

    /// @notice CPTStore contract reference.
    ICPTStore         public immutable cptStore;

    // ----------------------------------------------------------------
    // Local state
    // ----------------------------------------------------------------

    /// @notice claimId => address that opened the claim.
    ///         Used in finalizeClaim() to allow openers to finalize
    ///         their own claims without oracle involvement.
    mapping(uint256 => address) public claimOpener;

    // ----------------------------------------------------------------
    // Events
    // ----------------------------------------------------------------

    /// @notice Emitted when the oracle operator is updated.
    event OracleOperatorUpdated(
        address indexed previousOperator,
        address indexed newOperator
    );

    /// @notice Emitted when a claim is opened through this controller.
    ///         opener is the msg.sender; for auditing which application
    ///         or user initiated the claim.
    event ClaimOpenedThroughController(
        uint256 indexed claimId,
        bytes32 indexed externalKey,
        address indexed opener,
        uint256         blockNumber
    );

    /// @notice Emitted on every successful inference submission.
    ///         claimId, evidenceId, operator are indexed (EVM 3-topic
    ///         limit); all other fields are in event data.
    ///         bnInstanceId links the posterior to the exact CPTStore
    ///         parameter snapshot used for off-chain inference.
    event InferenceSubmitted(
        uint256 indexed claimId,
        uint256 indexed evidenceId,
        address indexed operator,
        uint8           gps,
        uint8           pc,
        uint8           pmd,
        uint8           pr,
        uint256         posteriorPPH,
        uint256         posteriorPPR,
        bytes32         bnInstanceId,
        uint256         blockNumber
    );

    /// @notice Emitted when a claim is finalized through this controller.
    event ClaimFinalizedThroughController(
        uint256 indexed claimId,
        address indexed caller,
        uint256         blockNumber
    );

    // ----------------------------------------------------------------
    // Modifiers
    // ----------------------------------------------------------------

    modifier onlyOwner() {
        require(
            msg.sender == owner,
            "OracleController: caller is not owner"
        );
        _;
    }

    modifier onlyOracleOperator() {
        require(
            msg.sender == oracleOperator,
            "OracleController: caller is not oracle operator"
        );
        _;
    }

    // ----------------------------------------------------------------
    // Constructor
    // ----------------------------------------------------------------

    /// @notice Deploys OracleController and binds all peer contracts.
    /// @dev    A SCALE consistency check ensures CPTStore and
    ///         ClaimRegistry share the same fixed-point base, preventing
    ///         silent precision mismatches between contracts.
    /// @param  claimRegistry_    Address of the deployed ClaimRegistry.
    /// @param  evidenceRegistry_ Address of the deployed EvidenceRegistry.
    /// @param  cptStore_         Address of the deployed CPTStore.
    /// @param  oracleOperator_   Initial oracle operator address.
    constructor(
        address claimRegistry_,
        address evidenceRegistry_,
        address cptStore_,
        address oracleOperator_
    ) {
        require(
            claimRegistry_ != address(0),
            "OracleController: zero ClaimRegistry"
        );
        require(
            evidenceRegistry_ != address(0),
            "OracleController: zero EvidenceRegistry"
        );
        require(
            cptStore_ != address(0),
            "OracleController: zero CPTStore"
        );
        require(
            oracleOperator_ != address(0),
            "OracleController: zero oracle operator"
        );

        owner          = msg.sender;
        oracleOperator = oracleOperator_;

        claimRegistry    = IClaimRegistry(claimRegistry_);
        evidenceRegistry = IEvidenceRegistry(evidenceRegistry_);
        cptStore         = ICPTStore(cptStore_);

        require(
            claimRegistry.SCALE() == cptStore.SCALE(),
            "OracleController: SCALE mismatch between ClaimRegistry and CPTStore"
        );
    }

    // ----------------------------------------------------------------
    // Administration
    // ----------------------------------------------------------------

    /// @notice Updates the oracle operator address.
    /// @param  newOperator  Must be non-zero.
    function setOracleOperator(address newOperator) external onlyOwner {
        require(
            newOperator != address(0),
            "OracleController: zero operator"
        );
        emit OracleOperatorUpdated(oracleOperator, newOperator);
        oracleOperator = newOperator;
    }

    // ----------------------------------------------------------------
    // Claim lifecycle entry points
    // ----------------------------------------------------------------

    /// @notice Opens a new claim on behalf of an application.
    /// @dev    Any address may call this in the current single-operator
    ///         prototype.  The caller's address is recorded in
    ///         claimOpener so they can later finalize their own claim.
    ///         externalKey uniqueness is enforced by ClaimRegistry.
    /// @param  externalKey  keccak256(visit metadata).  Must be unique.
    /// @return claimId      Newly allocated on-chain claim identifier.
    function openClaim(bytes32 externalKey)
        external
        returns (uint256 claimId)
    {
        claimId = claimRegistry.openClaim(externalKey);
        claimOpener[claimId] = msg.sender;

        emit ClaimOpenedThroughController(
            claimId,
            externalKey,
            msg.sender,
            block.number
        );
    }

    /// @notice Opens a claim only if no claim exists for externalKey;
    ///         otherwise returns the existing claimId.
    /// @dev    Idempotent helper that prevents duplicate-open errors
    ///         in workflows where open and submit may be called in the
    ///         same pipeline step.
    /// @param  externalKey  keccak256(visit metadata).
    /// @return claimId      Existing or newly allocated claim identifier.
    /// @return created      True iff a new claim was created.
    function openOrResolveClaim(bytes32 externalKey)
        external
        returns (uint256 claimId, bool created)
    {
        (uint256 existingId, bool found) = claimRegistry.resolveKey(externalKey);
        if (found) {
            return (existingId, false);
        }

        claimId = claimRegistry.openClaim(externalKey);
        claimOpener[claimId] = msg.sender;

        emit ClaimOpenedThroughController(
            claimId,
            externalKey,
            msg.sender,
            block.number
        );

        return (claimId, true);
    }

    /// @notice Finalizes a claim, preventing further posterior updates.
    /// @dev    Permitted callers:
    ///           - the address that originally opened the claim,
    ///           - the oracle operator,
    ///           - the contract owner.
    ///         This matches the paper's description of an
    ///         'application-defined completion condition' while also
    ///         allowing the oracle to finalize if the application
    ///         does not.
    /// @param  claimId  Internal claim identifier.
    function finalizeClaim(uint256 claimId) external {
        require(
            msg.sender == claimOpener[claimId] ||
            msg.sender == oracleOperator        ||
            msg.sender == owner,
            "OracleController: caller cannot finalize this claim"
        );

        claimRegistry.finalizeClaim(claimId);

        emit ClaimFinalizedThroughController(
            claimId,
            msg.sender,
            block.number
        );
    }

    // ----------------------------------------------------------------
    // Inference submission
    // ----------------------------------------------------------------

    /// @notice Submits a BN inference result for an open claim.
    ///
    /// @dev    Execution path (all O(1) in BN size):
    ///           1. Validate evidence bits and posterior bounds.
    ///           2. Verify BN snapshot has not changed since off-chain
    ///              reconstruction (stale-parameter mitigation).
    ///           3. Update latest posterior in ClaimRegistry (SSTORE).
    ///           4. Append immutable record to EvidenceRegistry (SSTORE + LOG).
    ///           5. Emit InferenceSubmitted event.
    ///
    ///         No CPT entries are read or traversed on-chain.
    ///         Per-query gas is therefore independent of BN size.
    ///
    ///         Reentrancy: claimRegistry and evidenceRegistry are
    ///         owner-deployed contracts with no ETH transfers and no
    ///         callbacks into OracleController.  CEI is satisfied;
    ///         a ReentrancyGuard is not required (see contract NatDoc).
    ///
    /// @param  claimId               Internal claim identifier.
    /// @param  gps                   GPS evidence bit in {0, 1}.
    /// @param  pc                    Patient confirmation bit in {0, 1}.
    /// @param  pmd                   Physician device log bit in {0, 1}.
    /// @param  pr                    Prescription evidence bit in {0, 1}.
    /// @param  posteriorPPH          Scaled P(PPH=true|e) in [0, SCALE].
    /// @param  posteriorPPR          Scaled P(PPR=true|e) in [0, SCALE].
    /// @param  expectedBnInstanceId  bnInstanceId read from CPTStore at
    ///                               off-chain BN reconstruction time.
    ///                               Submission reverts if CPTStore
    ///                               parameters changed since then.
    /// @return evidenceId            Monotonic record id in EvidenceRegistry.
    function submitInference(
        uint256 claimId,
        uint8   gps,
        uint8   pc,
        uint8   pmd,
        uint8   pr,
        uint256 posteriorPPH,
        uint256 posteriorPPR,
        bytes32 expectedBnInstanceId
    ) external onlyOracleOperator returns (uint256 evidenceId) {

        // --- Checks ---
        uint256 scale = cptStore.SCALE();

        require(gps < 2, "OracleController: bad gps");
        require(pc  < 2, "OracleController: bad pc");
        require(pmd < 2, "OracleController: bad pmd");
        require(pr  < 2, "OracleController: bad pr");
        require(
            posteriorPPH <= scale,
            "OracleController: posteriorPPH exceeds SCALE"
        );
        require(
            posteriorPPR <= scale,
            "OracleController: posteriorPPR exceeds SCALE"
        );

        bytes32 currentBnInstanceId = cptStore.bnInstanceId();
        require(
            currentBnInstanceId == expectedBnInstanceId,
            "OracleController: stale BN snapshot — CPTStore parameters changed"
        );

        // --- Interactions (trusted contracts, no ETH, no callbacks) ---

        claimRegistry.updatePosterior(
            claimId,
            posteriorPPH,
            posteriorPPR
        );

        evidenceId = evidenceRegistry.recordEvidence(
            claimId,
            gps, pc, pmd, pr,
            posteriorPPH,
            posteriorPPR,
            msg.sender
        );

        // --- Log ---

        emit InferenceSubmitted(
            claimId,
            evidenceId,
            msg.sender,
            gps, pc, pmd, pr,
            posteriorPPH,
            posteriorPPR,
            currentBnInstanceId,
            block.number
        );
    }
}
