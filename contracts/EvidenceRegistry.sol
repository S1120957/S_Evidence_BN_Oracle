// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.17;

/// @title EvidenceRegistry
/// @notice Stores individual evidence pieces linked to claims.
contract EvidenceRegistry {
    uint256 public nextEvidenceId;

    struct Evidence {
        uint256 id;
        uint256 claimId;
        uint8 evidenceIndex; // 0=GPS,1=PC,2=PMD,3=PR,...
        uint8 value;         // 0/1 (for your current BN)
        uint256 timestamp;
        address reporter;
    }

    mapping(uint256 => Evidence) public evidences;
    mapping(uint256 => uint256[]) public evidenceIdsByClaim;

    event EvidenceAdded(
        uint256 indexed evidenceId,
        uint256 indexed claimId,
        uint8 indexed evidenceIndex,
        uint8 value,
        address reporter
    );

    /// @notice Add a single evidence piece for a claim.
    function addEvidence(
        uint256 claimId,
        uint8 evidenceIndex,
        uint8 value
    ) external returns (uint256 evidenceId) {
        evidenceId = nextEvidenceId++;

        evidences[evidenceId] = Evidence({
            id: evidenceId,
            claimId: claimId,
            evidenceIndex: evidenceIndex,
            value: value,
            timestamp: block.timestamp,
            reporter: msg.sender
        });

        evidenceIdsByClaim[claimId].push(evidenceId);

        emit EvidenceAdded(evidenceId, claimId, evidenceIndex, value, msg.sender);
    }

    /// @notice Get all evidence ids associated with a given claim.
    function getEvidenceIdsForClaim(uint256 claimId)
        external
        view
        returns (uint256[] memory)
    {
        return evidenceIdsByClaim[claimId];
    }
}
