#!/usr/bin/env python3
"""部署 FormulaSwarmAttestor 到 BOT Chain（先测试网，后主网）。

    # 1) 编译
    node chain/compile.js

    # 2) 配置 chain/.env（BOT_RPC_URL / BOT_PRIVATE_KEY / BOT_CHAIN_ID）

    # 3) 先看会做什么（不发交易）
    python scripts/chain_deploy.py --dry-run

    # 4) 真部署
    python scripts/chain_deploy.py

部署后做一次读调用（VERSION / stats）验证合约可用 —— 即"1 写 1 读"。
地址写入 chain/deployments.json，并提示回填 chain/.env 的 BOT_CONTRACT。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import fschain  # noqa: E402

DEPLOY_FILE = fschain.DEPLOY_FILE


def main() -> int:
    ap = argparse.ArgumentParser(description="部署 FormulaSwarmAttestor")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不发交易")
    ap.add_argument("--network", default=os.environ.get("BOT_NETWORK", "testnet"),
                    help="网络标签，仅用于记录（默认 testnet）")
    ap.add_argument("--rpc", default="")
    ap.add_argument("--pk", default="")
    ap.add_argument("--chain-id", type=int, default=0)
    args = ap.parse_args()

    if args.rpc:
        os.environ["BOT_RPC_URL"] = args.rpc
    if args.pk:
        os.environ["BOT_PRIVATE_KEY"] = args.pk
    if args.chain_id:
        os.environ["BOT_CHAIN_ID"] = str(args.chain_id)

    c = fschain.cfg()
    abi, bytecode = fschain.abi_bytecode()
    print(f"[i] 合约 FormulaSwarmAttestor  bytecode {len(bytecode) // 2 - 1} bytes  abi {len(abi)} fns")
    ep = c["rpc_ip"] or c["rpc"]
    print(f"[i] 网络 {args.network}  rpc={ep or '(未配置)'}"
          f"{'（直连IP）' if c['rpc_ip'] else ''}  chain_id={c['chain_id']}  钱包={c['wallet']}")

    if args.dry_run or not fschain.has_endpoint() or not c["pk"]:
        print("\n[DRY-RUN] 将执行：")
        print(f"  1. 部署 FormulaSwarmAttestor（constructor 无参数，部署者自动成为 owner + scribe）")
        print(f"  2. 读 VERSION() / stats() 验证")
        print(f"  3. 写 chain/deployments.json")
        if not c["rpc"] or not c["pk"]:
            print("\n[!] 缺少 BOT_RPC_URL 或 BOT_PRIVATE_KEY —— 请在 chain/.env 中配置后再跑真实部署。")
        return 0

    web = fschain.w3()
    acct = fschain.account()
    if web.to_checksum_address(acct.address) != web.to_checksum_address(c["wallet"]):
        print(f"[!] 私钥地址 {acct.address} 与 BOT_WALLET {c['wallet']} 不一致，以私钥为准。")

    print(f"[i] deployer = {acct.address}  balance = "
          f"{web.from_wei(web.eth.get_balance(acct.address), 'ether')} (原生代币)")

    factory = web.eth.contract(abi=abi, bytecode=bytecode)
    cid = web.eth.chain_id if fschain._tester_mode() else c["chain_id"]
    tx = factory.constructor().build_transaction({
        "from": acct.address,
        "nonce": web.eth.get_transaction_count(acct.address),
        "chainId": cid,
        **{k: v for k, v in fschain._tx_params(web, acct).items() if k != "from" and k != "nonce"},
    })
    tx["gas"] = web.eth.estimate_gas(tx)
    signed = acct.sign_transaction(tx)
    txh = web.eth.send_raw_transaction(signed.raw_transaction)
    print(f"[>] deploy tx {txh.hex()}")
    rcpt = web.eth.wait_for_transaction_receipt(txh, timeout=240)
    addr = rcpt.get("contractAddress")
    print(f"[+] 合约地址 {addr}  block={rcpt.get('blockNumber')}  gas={rcpt.get('gasUsed')}  "
          f"status={rcpt.get('status')}")

    # 读验证（1 读）
    co = fschain.contract(addr)
    version = co.functions.VERSION().call()
    stats = co.functions.stats().call()
    owner = co.functions.owner().call()
    is_scribe = co.functions.scribes(acct.address).call()
    print(f"[+] 读验证 VERSION={version}  stats(approvals,predictions)={stats}  owner={owner}  scribe={is_scribe}")

    rec = {
        "network": args.network,
        "chain_id": c["chain_id"],
        "contract": "FormulaSwarmAttestor",
        "address": addr,
        "deployer": acct.address,
        "tx_hash": txh.hex(),
        "block": rcpt.get("blockNumber"),
        "gas_used": rcpt.get("gasUsed"),
        "explorer": f"{c['explorer']}/address/{addr}",
        "tx_explorer": f"{c['explorer']}/tx/{txh.hex()}",
        "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "abi": "chain/abi/FormulaSwarmAttestor.json",
    }
    DEPLOY_FILE.parent.mkdir(parents=True, exist_ok=True)
    history = []
    if DEPLOY_FILE.exists():
        try:
            data = json.loads(DEPLOY_FILE.read_text())
            history = data if isinstance(data, list) else [data]
        except json.JSONDecodeError:
            history = []
    history.append(rec)
    DEPLOY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n[✓] 已写入 chain/deployments.json")
    print(f"    浏览器：{rec['explorer']}")
    print(f"    请把下面这行填进 chain/.env：")
    print(f"    BOT_CONTRACT={addr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
