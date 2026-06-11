from dotenv import load_dotenv
load_dotenv()
import os
from web3 import Web3
from eth_account import Account

w3 = Web3(Web3.HTTPProvider(os.environ["SEPOLIA_RPC_URL"]))
sender = Account.from_key(os.environ["PRIVATE_KEY"]).address

pending   = w3.eth.get_transaction_count(sender, "pending")
confirmed = w3.eth.get_transaction_count(sender, "latest")
base_fee  = w3.eth.get_block("latest")["baseFeePerGas"]
balance   = w3.eth.get_balance(sender)

print(f"Address:             {sender}")
print(f"Balance:             {w3.from_wei(balance, 'ether'):.4f} ETH")
print(f"Confirmed nonce:     {confirmed}")
print(f"Pending nonce:       {pending}")
print(f"Stuck transactions:  {pending - confirmed}")
print(f"Current base fee:    {w3.from_wei(base_fee, 'gwei'):.2f} gwei")

if pending - confirmed > 0:
    print("\nACTION: Stuck transactions found.")
    print("Wait 2-3 minutes and run again, or increase gas price.")
else:
    print("\nOK: No stuck transactions. Safe to run experiments.")
