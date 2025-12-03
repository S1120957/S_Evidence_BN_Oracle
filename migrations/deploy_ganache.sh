# s-evidence-bn-oracle

Scalable Evidence-based Oracle Using Bayesian Networks

This repository contains the reference implementation of our evidence-based oracle for blockchain smart contracts, built on Bayesian networks (BNs). It extends the prototype introduced in our conference paper:

> Evidence-Based Oracles Using Bayesian Network, in *DCOSS-IoT 2025* (IEEE).

The goal is to provide a modular, attack-testable oracle that reasons over verifiable evidences (GPS, patient confirmation, physician report, medical device data) for home healthcare scenarios, and anchors both parameters and outcomes on an EVM-compatible blockchain.

---

## Repository layout

- `contracts/`  
  Solidity smart contracts:
  - CPT storage and management (e.g., `CPTStore.sol`),
  - evidence registration and logging (`EvidenceRegistry.sol`),
  - oracle control logic (`OracleController.sol`),
  - optional governance / security contracts (`SecurityGovernance.sol`).
  Each contract is designed to compile and deploy independently and can be reused in other BN-based oracle deployments.

- `offChain/bn_oracle/`  
  Off-chain BN engine:
  - BN structure definition and parameterisation,
  - CPT export utilities,
  - inference routines that consume evidences and produce posterior beliefs for PPH/PPR.

- `orchestrator/`  
  Scripts that glue together the off-chain BN engine and on-chain contracts:
  - submit evidences,
  - trigger BN inference,
  - commit results or proofs on-chain,
  - collect logs for experiments.

- `attacks/`  
  Attack generators and harnesses:
  - colluding patient–physician scenarios,
  - falsified GPS traces,
  - manipulated CPTs,
  - other bribery / incentive attacks used in the paper.

- `experiments/`  
  End-to-end experiment scripts corresponding to the figures and tables in the paper, for both PPH and PPR:
  - `exp_baseline_pph.py`, `exp_baseline_ppr.py` – clean scenarios,
  - `exp_attacks_pph_ppr.py` – security evaluation under attacks,
  - `exp_ablation_*.py` – ablation studies (evidence nodes, architecture variants, security features).

- `deployment/`  
  Truffle / Ganache / Sepolia configuration and migration scripts for compiling and deploying the contracts on:
  - a local test chain (Ganache) for deterministic experiments,
  - Sepolia as a public EVM testnet.

- `docs/`  
  Additional documentation:
  - architecture and threat model notes,
  - instructions for reproducing the experiments,
  - any generated figures used in the manuscript.

---

## Toolchain

The implementation currently targets EVM-compatible blockchains and uses:

- **Solidity** smart contracts, edited in **Visual Studio Code** with the Solidity extension.
- **Truffle** and **Ganache** for local compilation, deployment, and debugging.
- **Sepolia testnet** for validating behaviour on a public network.
- **Python 3.x** for the BN oracle, attack harnesses, and experiment scripts.
- **Web3 libraries** (e.g., `web3.py` or `web3.js`) for off-chain/on-chain interaction.

Once the Python and Node.js dependencies are finalised, add them to a `requirements.txt` and/or `package.json` so users can install them via:

```bash
pip install -r requirements.txt
# and/or
npm install