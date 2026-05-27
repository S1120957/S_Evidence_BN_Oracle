# LUNA: Scalable Evidence-Based Oracles Using Bayesian Networks

**Target journal:** IEEE Journal of Biomedical and Health Informatics (JBHI)

LUNA is a Bayesian network (BN)-based oracle architecture for blockchain
smart contracts. It anchors BN parameters on-chain for auditability while
executing inference off-chain for gas efficiency. Applied to healthcare
physician-visit claim verification on Ethereum (Sepolia testnet).

---

## Repository Structure

```
contracts/                  Solidity smart contracts (Solidity 0.8.17)
  CPTStore.sol              BN parameter store (priors + CPTs)
  ClaimRegistry.sol         Claim lifecycle state machine
  EvidenceRegistry.sol      Append-only evidence audit log
  OracleController.sol      Single entry point for all claim operations

deployment/
  build/                    Compiled ABI + bytecode artifacts (Truffle)
  deployment_addresses_log.csv  Deployed contract addresses on Sepolia

migrations/
  1_deploy_core.js          Deploys CPTStore, ClaimRegistry, EvidenceRegistry
  2_deploy_controller.js    Deploys OracleController and binds contracts

scripts/
  bn_oracle.py              Off-chain BN inference engine (Python)
  sepolia_gas_logs.py       Gas measurement experiment (n=20 per tx type)
  posterior_fidelity_logs.py  Fixed-point fidelity experiment (16 assignments)

paper/
  main.tex                  Full paper (IEEE JBHI format, single file)
  references.bib            Bibliography
  figures/                  Component diagram and sequence diagram

truffle-config.js           Truffle network configuration (Sepolia)
.env.example                Environment variable template (copy to .env)
```

---

## Deployed Contracts (Sepolia Testnet)

| Contract | Address |
|---|---|
| CPTStore | `0x19E2f1C1Abe30a25F7B2f52f14b6363A64C80026` |
| ClaimRegistry | `0x7d8253184Ee4685Ebf0f802FD2559d499CF56756` |
| EvidenceRegistry | `0x37C60182079fe0a4B1185cC70f7Ee13C23E8327f` |
| OracleController | `0x53484B8e002A945D4E46c8040434bc8B3390085A` |

All transactions are publicly verifiable on
[Sepolia Etherscan](https://sepolia.etherscan.io).

---

## Setup

### Prerequisites

- Node.js >= 16, npm
- Python >= 3.10
- Truffle (`npm install -g truffle`)

### Install dependencies

```bash
# Solidity toolchain
npm install

# Python oracle and experiment scripts
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install web3 eth-account python-dotenv
```

### Configure environment

```bash
cp .env.example .env
# Edit .env: add your SEPOLIA_RPC_URL and PRIVATE_KEY
```

---

## Compile and Deploy

```bash
# Compile contracts
npx truffle compile

# Deploy to Sepolia
npx truffle migrate --network sepolia --reset
```

After deployment, update the `*_ADDR` values in `.env` with the
addresses printed by Truffle (also saved to
`deployment/deployment_addresses_log.csv`).

---

## Run Experiments

Activate your Python virtual environment first, then:

```bash
cd scripts

# Gas scaling experiment (produces sepolia_gas_logs.csv)
python sepolia_gas_logs.py

# Fixed-point fidelity — neutral CPTs
python posterior_fidelity_logs.py

# Fixed-point fidelity — asymmetric CPTs
PROFILE_NAME=asymmetric python posterior_fidelity_logs.py
# Windows: set PROFILE_NAME=asymmetric && python posterior_fidelity_logs.py
```

Results are written to `scripts/sepolia_gas_logs.csv` and
`scripts/posterior_fidelity_{neutral,asymmetric}.csv`.

---

## BN Model

LUNA uses a discrete Naive Bayes structure for physician-visit verification:

- **Root nodes:** PPH (in-person), PPR (remote) — marginally independent
- **Evidence nodes:** GPS, PC (patient confirmation), PMD (device log), PR (prescription)
- **Inference:** closed-form enumeration over 4 root configurations
- **Fixed-point encoding:** SCALE = 10^6, max rounding error = 5×10^-7

All BN parameters are anchored on-chain in `CPTStore`. Inference runs
off-chain in `bn_oracle.py` and results are committed through
`OracleController.submitInference()`.

---

## Paper

The full manuscript is in `paper/main.tex` (IEEE JBHI format).
Compile with:

```
pdflatex paper/main.tex
bibtex paper/main
pdflatex paper/main.tex
pdflatex paper/main.tex
```

Or open directly in [Overleaf](https://overleaf.com) by uploading
the `paper/` directory contents.

---

## License

MIT — see contract source files.
