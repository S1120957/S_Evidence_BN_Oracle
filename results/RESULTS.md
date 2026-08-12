# SELENE — Canonical Empirical Record (Definitive Path A Deployment)

All values below come from the final validated CSV artifacts and
`final_experiment_provenance.json`. Do not reconstruct numbers from older
manuscript text or archived CSVs.

## Definitive production deployment (Sepolia, chain ID 11155111)

| Contract | Address |
|---|---|
| CPTStore | `0x6a351aF33059B2a361F44d7E924AEA31BbC8dccc` |
| ClaimRegistry | `0xf16b44768983C68FE418764ec2b740EA7eA14635` |
| EvidenceRegistry | `0xa2e36d0bE698E54cD61d1b0be224fB81ee56d494` |
| OracleController | `0x71668f737d859d977DA13f952f3F0B03DC00ca01` |

Verified: non-empty bytecode; inter-contract wiring; operator authorization;
`SCALE = 1,000,000`; `UNOBSERVED = 2` in OracleController and EvidenceRegistry.

## Frozen asymmetric BN configuration

Priors: `P(PPH=1) = 0.30` (300000), `P(PPR=1) = 0.70` (700000).
CPTs `P(E_i=1 | PPH,PPR)` over parents `(1,0) (0,1) (1,1) (0,0)`:

| Evidence | (1,0) | (0,1) | (1,1) | (0,0) |
|---|---|---|---|---|
| GPS | 0.90 | 0.15 | 0.80 | 0.10 |
| PC  | 0.85 | 0.20 | 0.75 | 0.15 |
| PMD | 0.88 | 0.10 | 0.78 | 0.08 |
| PR  | 0.80 | 0.25 | 0.70 | 0.20 |

All 16 entries + both priors independently read back after initialization.

**bnInstanceId:**
`0x7497ecdac61fd9d1a089b81f068b50d5e136eb9b0d69d320c59b15cdb18772e5`
Initial verified snapshot block: 11468913.

## Production per-query gas

| Operation | n | Mean ± pop. std | Min | Max | Range |
|---|---|---|---|---|---|
| submitInference | 20 | 242,387.00 ± 0.00 | 242,387 | 242,387 | 0 |
| openClaim (reporting set) | 19 | 196,134.74 ± 3.68 | 196,124 | 196,136 | 12 |

The first openClaim observation is retained in the raw CSV but excluded from
the reported summary per the predefined protocol.

## Initialization scaling (auxiliary CPTStoreScaling instrumentation)

| N_e | Entries e | Gas |
|---|---|---|
| 4 | 16 | 895,687 |
| 8 | 32 | 1,751,559 |
| 32 | 128 | 6,886,791 |

Fit: `Gas(e) = 39,815 + 53,492·e`, R² = 1.000000000000.
This experiment measures **initialization** scaling only; it is not a
per-query measurement, and its auxiliary deployments are intentionally
separate from the production deployment above.

## Fixed-point fidelity (all 2^4 = 16 complete assignments; 32 marginals)

- Posterior range: [0.002109495, 0.996448974]
- δ_max = 4.968788028×10⁻⁷ (theoretical bound 5×10⁻⁷)
- δ_mean = 2.439419215×10⁻⁷
- Near-boundary marginals: 0 · Decision inconsistencies: 0 / 32
- All 16 evidence tuples unique; all provenance checks passed.

## Numerical boundary stress (19-value synthetic sweep, τ = 0.5, SCALE = 10⁶)

- ε = 5×10⁻⁷
- Outside ε: 11/11 decision-consistent (soundness)
- Inside/on ε: 4/8 inconsistent (tightness); all 4 below τ (one-sided)
- All tested SCALE soundness checks passed.

## Solidity boundary/invariant checks (B1–B9)

9/9 PASS: posterior 0 and SCALE accepted; posterior > SCALE reverts;
evidence 3 reverts; evidence 2 (UNOBSERVED) accepted; nonexistent claim,
finalized claim, unauthorized caller, and stale bnInstanceId all revert.

## Lifecycle (3 concurrent claims, partial ternary evidence)

Orderings: canonical GPS→PC→PMD→PR; reverse PR→PMD→PC→GPS;
gapped PC→PR→GPS→PMD. 21 CSV rows: 3 open + 12 staged submits +
3 finalize + 3 post-finalization revert checks.

- EvidenceRegistry read-back and recomputation: 12/12 exact
  (auditor delta PPH = 0, PPR = 0)
- Final posterior (all three orderings): P(PPH=1|e) ≈ 0.8700,
  P(PPR=1|e) ≈ 0.7596
- Gas: first posterior write 242,399; subsequent writes 208,187–208,199;
  mean 216,743, pop. std 14,812.50, range 34,212. At equivalent stages the
  order effect is ≤ ~12 gas. Gas is stage-dependent, order-invariant.
- finalizeClaim: n = 3, mean 42,396 gas. All post-finalization submits
  reverted with `ClaimRegistry: claim not open` (Finalized is absorbing).

## Authoritative artifacts

CSVs: `lifecycle_gas_logs.csv`, `posterior_fidelity_asymmetric.csv`,
`boundary_stress_partA.csv`, `boundary_stress_partB.csv`,
`sepolia_gas_logs.csv`, `scaling_gas_logs.csv`.
Provenance manifest: `final_experiment_provenance.json`.
