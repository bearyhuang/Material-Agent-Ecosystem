#!/usr/bin/env python3
"""部署结果独立校验（防中间人 / 防假回执）。

为什么必须有这一步：写链流量若经过不可信代理，对方可以返回一个"看起来成功"的假回执
（合约地址根本不是你的，或代码被替换）。因此部署后必须**从链上把代码读回来**，
与本地编译产物逐字节比对，并核对归属。

    .venv/bin/python scripts/chain_verify.py                 # 校验 BOT_CONTRACT
    .venv/bin/python scripts/chain_verify.py --address 0x…   # 指定地址
    .venv/bin/python scripts/chain_verify.py --json

检查项（任一项 critical 失败即判定部署不可用）：
  1. chainId == BOT_CHAIN_ID
  2. 链上代码非空
  3. 链上 runtime code == 本地编译的 deployedBytecode（逐字节）
  4. VERSION() == fs-attestor/1.0
  5. owner() == 期望的部署者钱包
  6. scribes(owner) == true（否则写不进去）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import fschain  # noqa: E402

EXPECT_VERSION = "fs-attestor/1.0"


def verify(address: str, expect_deployer: str) -> dict:
    c = fschain.cfg()
    web = fschain.w3()
    out = {"address": address, "rpc": c["rpc"], "items": [], "ok": False}

    def add(name, ok, detail, critical=True):
        out["items"].append({"name": name, "ok": ok, "detail": detail, "critical": critical})

    try:
        cid = web.eth.chain_id
        want = web.eth.chain_id if fschain._tester_mode() else c["chain_id"]
        add("chainId 匹配", cid == want, f"链上 {cid} / 期望 {want}")
    except Exception as e:
        add("chainId 匹配", False, f"RPC 不可达：{type(e).__name__}")
        return out

    addr = web.to_checksum_address(address)
    raw = web.eth.get_code(addr).hex()
    code = raw if raw.startswith("0x") else "0x" + raw
    add("链上有代码", len(code) > 2, f"{(len(code) - 2) // 2} bytes")

    abi, _ = fschain.abi_bytecode()
    meta = json.loads((fschain.ABI_DIR / "FormulaSwarmAttestor.json").read_text())
    local = meta.get("deployedBytecode", "")
    if not local:
        add("代码逐字节比对", False, "本地缺 deployedBytecode，请重跑 node chain/compile.js")
    else:
        same = code.lower() == local.lower()
        add("代码逐字节比对", same,
            "一致" if same else f"不一致（链上 {len(code)} vs 本地 {len(local)}）—— 可能被替换")

    co = web.eth.contract(address=addr, abi=abi)
    try:
        ver = co.functions.VERSION().call()
        add("VERSION", ver == EXPECT_VERSION, ver)
    except Exception as e:
        add("VERSION", False, f"{type(e).__name__}: {str(e)[:60]}")

    try:
        owner = co.functions.owner().call()
        add("owner 归属", owner.lower() == expect_deployer.lower(), f"owner={owner}")
        add("owner 可写", bool(co.functions.scribes(owner).call()), "scribes[owner]=true")
    except Exception as e:
        add("owner 归属", False, f"{type(e).__name__}: {str(e)[:60]}")

    try:
        a, p = co.functions.stats().call()
        add("计数器可读", True, f"approvals={a} predictions={p}", critical=False)
    except Exception as e:
        add("计数器可读", False, str(e)[:60], critical=False)

    out["ok"] = all(i["ok"] for i in out["items"] if i["critical"])
    out["explorer"] = f"{c['explorer']}/address/{address}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="部署结果独立校验")
    ap.add_argument("--address", default="", help="默认取 BOT_CONTRACT")
    ap.add_argument("--expect-deployer", default="", help="默认取 BOT_WALLET")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    c = fschain.cfg()
    addr = args.address or c["contract"]
    if not addr:
        print("[!] 未指定地址，也没有 BOT_CONTRACT")
        return 2
    who = args.expect_deployer or c["wallet"]

    rep = verify(addr, who)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["ok"] else 1

    print(f"[校验] {addr}")
    print(f"[校验] rpc={rep['rpc']}")
    for i in rep["items"]:
        mark = "[绿]" if i["ok"] else ("[红]" if i["critical"] else "[黄]")
        print(f"  {mark} {i['name']:<16} {i['detail']}")
    print()
    if rep["ok"]:
        print(f"  [绿] 部署可信 —— {rep['explorer']}")
        return 0
    print("  [红] 部署不可用：请勿把该地址写进提交材料")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
