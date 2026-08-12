# SELENE

Definitive SELENE Path A implementation and experimental artifacts.

## Reviewer entry points

- `contracts/` — definitive Path A Solidity contracts
- `scripts/` — Path A inference and experiment scripts
- `results/RESULTS.md` — final empirical results

## Definitive Sepolia deployment

- CPTStore: `0x6a351aF33059B2a361F44d7E924AEA31BbC8dccc`
- ClaimRegistry: `0xf16b44768983C68FE418764ec2b740EA7eA14635`
- EvidenceRegistry: `0xa2e36d0bE698E54cD61d1b0be224fB81ee56d494`
- OracleController: `0x71668f737d859d977DA13f952f3F0B03DC00ca01`

Frozen BN fingerprint:

`0x7497ecdac61fd9d1a089b81f068b50d5e136eb9b0d69d320c59b15cdb18772e5`

## Evidence encoding

- `0` = observed false
- `1` = observed true
- `2` = `UNOBSERVED`

## Setup

Python dependencies are listed in `requirements.txt`.
JavaScript/Truffle dependencies are listed in `package.json` and `package-lock.json`.

The published Sepolia deployment already exists; redeployment is not required.

## Historical provenance

The superseded binary/LUNA repository is preserved at:

`v0.1-luna-binary-baseline`

The development branch `feature/pathA-asymmetric-sepolia-production` is also retained.
