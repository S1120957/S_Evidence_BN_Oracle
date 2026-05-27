import json
from pathlib import Path

names = ["CPTStore", "ClaimRegistry", "EvidenceRegistry", "OracleController"]
base = Path("deployment/build")

for n in names:
    p = base / f"{n}.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    nets = d.get("networks", {})
    print(
        n,
        "has_11155111=",
        "11155111" in nets,
        "address=",
        nets.get("11155111", {}).get("address"),
    )