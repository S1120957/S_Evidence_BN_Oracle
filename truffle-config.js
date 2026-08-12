const HDWalletProvider = require("@truffle/hdwallet-provider");
require("dotenv").config();

module.exports = {
  networks: {
    sepolia: {
      provider: () =>
        new HDWalletProvider({
          privateKeys: [process.env.PRIVATE_KEY],
          providerOrUrl: process.env.SEPOLIA_RPC_URL,
          pollingInterval: 15000,
        }),
      network_id: 11155111,
      gas: 5500000,
      gasPrice: 10000000000,
      confirmations: 1,
      timeoutBlocks: 200,
      skipDryRun: true,
      networkCheckTimeout: 1200000,
    },
  },

  compilers: {
    solc: {
      version: "0.8.17",
      settings: {
        optimizer: {
          enabled: true,
          runs: 200,
        },
        viaIR: true,
      },
    },
  },

  contracts_directory: "./contracts",
  contracts_build_directory: "./deployment/build",
  migrations_directory: "./migrations",
};