"`n==================== DEPLOY RUN $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====================" | Tee-Object -FilePath deployment_logs.txt -Append
npx truffle migrate --network sepolia --reset 2>&1 | Tee-Object -FilePath deployment_logs.txt -Append

python -c "import json, pathlib, datetime; names=['CPTStore','ClaimRegistry','EvidenceRegistry','OracleController']; base=pathlib.Path('deployment/build'); out=pathlib.Path('deployment_addresses_log.csv'); now=datetime.datetime.now().isoformat(); rows=[]; 
for n in names:
    d=json.loads((base/f'{n}.json').read_text(encoding='utf-8'))
    net=d.get('networks', {}).get('11155111', {})
    rows.append((now, n, net.get('address')))
exists=out.exists()
with out.open('a', encoding='utf-8', newline='') as f:
    if not exists:
        f.write('timestamp,contract,address\n')
    for r in rows:
        f.write(','.join('' if x is None else str(x) for x in r) + '\n')
print('Appended addresses to', out)"