// SPDX-License-Identifier: MIT
pragma solidity ^0.8.17;

/// @title  ClaimRegistry
/// @notice Manages the lifecycle of LUNA claims and stores the latest
///         committed posterior for each claim.
///
/// @dev    Paper-aligned design invariants (Section IV-A):
///
///         1. Three-state machine: None -> Open -> Finalized.
///            The Finalized state is absorbing; updatePosterior() and
///            finalizeClaim() both revert if the claim is not Open.
///
///         2. Only OracleController may transition claim state or write
///            posteriors.  Applications open and finalize claims by
///            calling the corresponding OracleController functions,
///            which in turn call this registry.  This keeps a single
///            authorised entry point for all claim mutations and matches
///            the paper's workflow (Section III-E).
///
///         3. Every state transition and posterior update records
///            block.number.  The oracle also records the CPTStore block
///            height at BN reconstruction time; together these two
///            values allow auditors to detect parameter-submission races.
///
///         4. posteriorPPH and posteriorPPR are range-checked
///            independently against SCALE.  They are NOT required to
///            sum to SCALE because PPH and PPR are marginally independent
///            root nodes; mutual exclusivity of claim types
///            is enforced at the application layer, not in the BN.
///
///         5. A reverse mapping externalKey -> claimId allows the oracle
///            to resolve visit-metadata keys to on-chain identifiers in
///            O(1) without scanning event logs.  A duplicate-key guard
///            prevents two claims from being opened for the same visit.

contract ClaimRegistry {

    // ----------------------------------------------------------------
    // Constants
    // ----------------------------------------------------------------

    /// @notice Scale factor shared across LUNA contracts.
    ///         All posteriors p in [0,1] are stored as round(p * SCALE).
    uint256 public constant SCALE = 1_000_000;

    // ----------------------------------------------------------------
    // Roles
    // ----------------------------------------------------------------

    /// @notice Deployer; permitted only to bind the OracleController.
    address public owner;

    /// @notice The single authorised writer for all claim mutations.
    ///         Set once after deployment via setOracleController().
    address public oracleController;

    // ----------------------------------------------------------------
    // Claim lifecycle
    // ----------------------------------------------------------------

    /// @notice Claim lifecycle states.
    ///         None      – claim does not exist (mapping default).
    ///         Open      – accepting posterior updates.
    ///         Finalized – absorbing; no further writes accepted.
    enum ClaimState { None, Open, Finalized }

    /// @notice Full claim record.
    /// @param  id                Internal monotonic identifier.
    /// @param  externalKey       keccak256 of visit metadata; unique per claim.
    /// @param  state             Current lifecycle state.
    /// @param  posteriorPPH      Latest scaled P(PPH=true | evidence).
    /// @param  posteriorPPR      Latest scaled P(PPR=true | evidence).
    /// @param  lastUpdatedBlock  block.number of the most recent write.
    struct Claim {
        uint256    id;
        bytes32    externalKey;
        ClaimState state;
        uint256    posteriorPPH;
        uint256    posteriorPPR;
        uint256    lastUpdatedBlock;
    }

    /// @notice claimId => Claim record.
    mapping(uint256 => Claim) public claims;

    /// @notice externalKey => claimId reverse lookup.
    ///         Enables O(1) key-to-id resolution by the off-chain oracle.
    ///         Value 0 means no claim exists for that key (claimId 0 is
    ///         distinguished by checking claims[0].state != None).
    mapping(bytes32 => uint256) public externalKeyToClaimId;

    /// @dev    Tracks whether a given externalKey has been registered.
    ///         Needed because claimId 0 is a valid identifier.
    mapping(bytes32 => bool) private _keyRegistered;

    /// @notice Next claimId; incremented atomically on each openClaim().
    uint256 public nextClaimId;

    // ----------------------------------------------------------------
    // Events
    // ----------------------------------------------------------------

    /// @notice Emitted when the OracleController binding changes.
    event OracleControllerUpdated(
        address indexed previousController,
        address indexed newController
    );

    /// @notice Emitted when a new claim is opened.
    ///         Both claimId and externalKey are indexed for efficient
    ///         lookup by either identifier.
    event ClaimOpened(
        uint256 indexed claimId,
        bytes32 indexed externalKey,
        uint256         blockNumber
    );

    /// @notice Emitted on every posterior update.
    ///         blockNumber enables auditors to correlate the update with
    ///         the CPTStore parameter snapshot used by the oracle.
    event ClaimPosteriorUpdated(
        uint256 indexed claimId,
        uint256         posteriorPPH,
        uint256         posteriorPPR,
        uint256         blockNumber
    );

    /// @notice Emitted when a claim is finalized.
    event ClaimFinalized(
        uint256 indexed claimId,
        uint256         blockNumber
    );

    // ----------------------------------------------------------------
    // Modifiers
    // ----------------------------------------------------------------

    modifier onlyOwner() {
        require(
            msg.sender == owner,
            "ClaimRegistry: caller is not owner"
        );
        _;
    }

    modifier onlyOracleController() {
        require(
            msg.sender == oracleController,
            "ClaimRegistry: caller is not OracleController"
        );
        _;
    }

    // ----------------------------------------------------------------
    // Constructor
    // ----------------------------------------------------------------

    constructor() {
        owner = msg.sender;
    }

    // ----------------------------------------------------------------
    // Owner administration
    // ----------------------------------------------------------------

    /// @notice Binds the OracleController address.
    ///         Must be called once after deployment, before any claims
    ///         are opened.  Can be updated by the owner if the
    ///         controller contract is redeployed.
    /// @param  newController  Address of the deployed OracleController.
    function setOracleController(address newController)
        external
        onlyOwner
    {
        require(
            newController != address(0),
            "ClaimRegistry: zero address"
        );
        emit OracleControllerUpdated(oracleController, newController);
        oracleController = newController;
    }

    // ----------------------------------------------------------------
    // Claim lifecycle writes  (onlyOracleController)
    // ----------------------------------------------------------------

    /// @notice Opens a new claim.  Transition: None -> Open.
    /// @dev    Called by OracleController on behalf of an application.
    ///         externalKey must be unique; duplicate keys are rejected
    ///         to prevent two claims from being created for the same
    ///         physician visit.
    /// @param  externalKey  keccak256(visit metadata); must be unique.
    /// @return claimId      Internal on-chain identifier for this claim.
    function openClaim(bytes32 externalKey)
        external
        onlyOracleController
        returns (uint256 claimId)
    {
        require(
            !_keyRegistered[externalKey],
            "ClaimRegistry: externalKey already registered"
        );

        claimId = nextClaimId++;

        claims[claimId] = Claim({
            id:               claimId,
            externalKey:      externalKey,
            state:            ClaimState.Open,
            posteriorPPH:     0,
            posteriorPPR:     0,
            lastUpdatedBlock: block.number
        });

        externalKeyToClaimId[externalKey] = claimId;
        _keyRegistered[externalKey]       = true;

        emit ClaimOpened(claimId, externalKey, block.number);
    }

    /// @notice Updates the committed posterior for an open claim.
    /// @dev    Accepts repeated updates; each replaces the previous
    ///         posterior and appends a new audit event.
    ///         Reverts if the claim is Finalized (absorbing state).
    ///         posteriorPPH and posteriorPPR are checked independently;
    ///         they need not sum to SCALE (see contract-level note on
    ///         marginal independence of the PPH and PPR root nodes).
    /// @param  claimId       Internal claim identifier.
    /// @param  posteriorPPH  Scaled P(PPH=true | evidence) in [0,SCALE].
    /// @param  posteriorPPR  Scaled P(PPR=true | evidence) in [0,SCALE].
    function updatePosterior(
        uint256 claimId,
        uint256 posteriorPPH,
        uint256 posteriorPPR
    ) external onlyOracleController {
        require(
            posteriorPPH <= SCALE,
            "ClaimRegistry: posteriorPPH exceeds SCALE"
        );
        require(
            posteriorPPR <= SCALE,
            "ClaimRegistry: posteriorPPR exceeds SCALE"
        );

        Claim storage c = claims[claimId];
        require(
            c.state == ClaimState.Open,
            "ClaimRegistry: claim not open"
        );

        c.posteriorPPH     = posteriorPPH;
        c.posteriorPPR     = posteriorPPR;
        c.lastUpdatedBlock = block.number;

        emit ClaimPosteriorUpdated(
            claimId,
            posteriorPPH,
            posteriorPPR,
            block.number
        );
    }

    /// @notice Finalizes an open claim.  Transition: Open -> Finalized.
    /// @dev    Finalized is absorbing: this function and updatePosterior()
    ///         both revert if called again on a Finalized claim.
    ///         Called by OracleController when the application signals
    ///         that its completion condition has been met.
    /// @param  claimId  Internal claim identifier.
    function finalizeClaim(uint256 claimId)
        external
        onlyOracleController
    {
        Claim storage c = claims[claimId];
        require(
            c.state == ClaimState.Open,
            "ClaimRegistry: claim not open"
        );

        c.state            = ClaimState.Finalized;
        c.lastUpdatedBlock = block.number;

        emit ClaimFinalized(claimId, block.number);
    }

    // ----------------------------------------------------------------
    // View helpers
    // ----------------------------------------------------------------

    /// @notice Returns the lifecycle state of a claim.
    /// @param  claimId  Internal claim identifier.
    /// @return Current ClaimState (None / Open / Finalized).
    function getClaimState(uint256 claimId)
        external
        view
        returns (ClaimState)
    {
        return claims[claimId].state;
    }

    /// @notice Returns the full claim record.
    /// @param  claimId  Internal claim identifier.
    
    function getClaim(uint256 claimId)
        external
        view
        returns (
            uint256    id,
            bytes32    externalKey,
            ClaimState state,
            uint256    posteriorPPH,
            uint256    posteriorPPR,
            uint256    lastUpdatedBlock
        )
    {
        Claim storage c = claims[claimId];
        return (
            c.id,
            c.externalKey,
            c.state,
            c.posteriorPPH,
            c.posteriorPPR,
            c.lastUpdatedBlock
        );
    }

    /// @notice Resolves a visit-metadata key to an on-chain claimId.
    /// @dev    Returns (claimId, true) if registered,
    ///         (0, false) if not found.
    /// @param  externalKey  keccak256(visit metadata).
    /// @return claimId      On-chain claim identifier.
    /// @return found        True iff a claim is registered for this key.
    function resolveKey(bytes32 externalKey)
        external
        view
        returns (uint256 claimId, bool found)
    {
        found   = _keyRegistered[externalKey];
        claimId = found ? externalKeyToClaimId[externalKey] : 0;
    }
}
