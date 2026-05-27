const CPTStore = artifacts.require("CPTStore");
const ClaimRegistry = artifacts.require("ClaimRegistry");
const EvidenceRegistry = artifacts.require("EvidenceRegistry");

module.exports = async function (deployer) {
  await deployer.deploy(CPTStore);
  await deployer.deploy(ClaimRegistry);
  await deployer.deploy(EvidenceRegistry);
};