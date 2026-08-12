# Experimental Results (Sepolia public testnet)

All values are intrinsic execution gas (`gasUsed`), measured on the
Sepolia public testnet. Reproduce via the scripts in `scripts/`.

## Deployed contracts (current)

| Contract | Address |
|---|---|
| CPTStore | `0x24fcBbb11fb84dbA1E581794D36CA172eEC7EE49` |
| ClaimRegistry | `0x65348eD468c5Ed4F944510f93b5788D02e78fE75` |
| EvidenceRegistry | `0x3d2968b5154139952491E13a98c4CEB974be66E8` |
| OracleController | `0xe4353099Af771972405aC2d16E134029930a2c30` |

## Per-query gas (BN-size-invariant)

| Operation | Gas | n |
|---|---|---|
| `submitInference` | 242,329 ± 439 | 40 |
| `openClaim` | 196,135 ± 5 | 19 |

`submitInference` full range across 40 runs: 2,812 gas (1.2%).

## Initialization gas scaling (CPTStoreScaling contract)

| N_e | CPT entries | Init gas |
|---|---|---|
| 4 | 16 | 895,687 |
| 8 | 32 | 1,751,559 |
| 32 | 128 | 6,886,791 |

Linear fit: `gas = 39,815 + 53,492 × (CPT entries)`, R² = 1.00.

Production `CPTStore` (4-node, with fingerprint bookkeeping): 1,613,013 gas.

## Fixed-point fidelity (SCALE = 1e6, asymmetric CPTs)

| Metric | Value |
|---|---|
| δ_max | 3.80×10⁻⁷ |
| δ_mean | 1.80×10⁻⁷ |
| Near-boundary (τ=0.5) | 0 |
| Decision inconsistencies | 0 |

Posteriors tested span [0.022, 0.987] across 16 evidence assignments
(32 posterior marginals).

## Data files

- `sepolia_gas_logs.csv` — per-query and production init gas
- `posterior_fidelity_neutral.csv`, `posterior_fidelity_asymmetric.csv` — fidelity
- `scaling_gas_logs.csv` — CPTStoreScaling init at N=4,8,32 (add from local machine)
