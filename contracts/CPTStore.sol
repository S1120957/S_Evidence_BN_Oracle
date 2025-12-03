// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.17;

/// @title CPTStore
/// @notice Stores priors for PPH/PPR and CPTs for evidence nodes (GPS,PC,PMD,PR)
contract CPTStore {
    uint256 public constant SCALE = 1_000_000;

    // How many evidence nodes are supported (0..NUM_EVIDENCE-1)
    uint8 public constant NUM_EVIDENCE = 4;

    // Priors P(PPH=true), P(PPR=true), scaled
    uint256 public priorPPH;
    uint256 public priorPPR;

    // Identifier for this BN instance (structure + priors/CPTs version)
    bytes32 public bnInstanceId;

    // evidenceIndex: 0=GPS,1=PC,2=PMD,3=PR
    // pphState: 0/1, pprState: 0/1
    mapping(uint8 => mapping(uint8 => mapping(uint8 => uint256))) private evidenceTrueCPT;

    event EvidenceCPTUpdated(
        uint8 indexed evidenceIndex,
        uint8 indexed pphState,
        uint8 indexed pprState,
        uint256 pTrueScaled
    );

    constructor() {
        // Example priors: P(PPH) = 0.3, P(PPR) = 0.7
        priorPPH = 300_000;
        priorPPR = 700_000;

        // Simple BN instance id: you can later make this depend on CPT hashes
        bnInstanceId = keccak256(
            abi.encodePacked(
                "PPH/PPR v1",
                priorPPH,
                priorPPR
            )
        );
    }

    function setEvidenceCPT(
        uint8 evidenceIndex,
        uint8 pphState,
        uint8 pprState,
        uint256 pTrueScaled
    ) external {
        require(evidenceIndex < NUM_EVIDENCE, "bad evidence index");
        require(pphState < 2, "bad PPH state");
        require(pprState < 2, "bad PPR state");
        require(pTrueScaled <= SCALE, "probability > 1");

        evidenceTrueCPT[evidenceIndex][pphState][pprState] = pTrueScaled;
        emit EvidenceCPTUpdated(evidenceIndex, pphState, pprState, pTrueScaled);
    }

    function getEvidenceTrueCPT(
        uint8 evidenceIndex,
        uint8 pphState,
        uint8 pprState
    ) external view returns (uint256) {
        return evidenceTrueCPT[evidenceIndex][pphState][pprState];
    }
}
