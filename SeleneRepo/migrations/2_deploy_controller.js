const ClaimRegistry = artifacts.require("ClaimRegistry");
const CPTStore = artifacts.require("CPTStore");
const EvidenceRegistry = artifacts.require("EvidenceRegistry");
const OracleController = artifacts.require("OracleController");

module.exports = async function (deployer, network, accounts) {
  const claimRegistry = await ClaimRegistry.deployed();
  const cptStore = await CPTStore.deployed();
  const evidenceRegistry = await EvidenceRegistry.deployed();

  const oracleOperator = accounts[0];

  await deployer.deploy(
    OracleController,
    claimRegistry.address,
    evidenceRegistry.address,
    cptStore.address,
    oracleOperator
  );

  const oracleController = await OracleController.deployed();

  await claimRegistry.setOracleController(oracleController.address);
  await evidenceRegistry.setController(oracleController.address);
};