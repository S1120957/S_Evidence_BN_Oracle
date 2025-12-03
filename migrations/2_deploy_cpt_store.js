const CPTStore = artifacts.require("CPTStore");
const EvidenceRegistry = artifacts.require("EvidenceRegistry");
const ClaimRegistry = artifacts.require("ClaimRegistry");
const OracleController = artifacts.require("OracleController");

module.exports = async function (deployer, network, accounts) {
  // 1) Deploy EvidenceRegistry
  await deployer.deploy(EvidenceRegistry);
  const evidenceRegistry = await EvidenceRegistry.deployed();

  // 2) Deploy ClaimRegistry
  await deployer.deploy(ClaimRegistry);
  const claimRegistry = await ClaimRegistry.deployed();

  // 3) Get already-deployed CPTStore from 1_deploy_cpt_store.js
  const cptStore = await CPTStore.deployed();

  // 4) Deploy OracleController with 3 constructor args:
  //    (address _cptStore, address _evidenceRegistry, address _claimRegistry)
  await deployer.deploy(
    OracleController,
    cptStore.address,
    evidenceRegistry.address,
    claimRegistry.address
  );
};
