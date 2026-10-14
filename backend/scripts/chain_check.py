#!/usr/bin/env python3
"""链层一键自检（对应 ai4s03 §13.4 的"设备测试"环节）：30 秒内出三色结果。

    .venv/bin/python scripts/chain_check.py
    .venv/bin/python scripts/chain_check.py --json

绿=可演示 / 黄=降级可演示（链不可用但不影响主流程）/ 红=该项砍掉。
同时打印主网区块浏览器链接，供提交材料直接复制。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import fschain  # noqa: E402

GREEN, YELLOW, RED = "[绿]", "[黄]", "[红]"


def main() -> int:
    as_json = "--json" in sys.argv
    c = fschain.cfg()
    ep = c["rpc_ip"] or c["rpc"]
    mode = "直连IP" if c["rpc_ip"] else ("URL" if c["rpc"] else "无")
    rep: dict = {"rpc": ep, "mode": mode, "chain_id": c["chain_id"], "contract": c["contract"],
                 "wallet": c["wallet"], "fs_chain": c["enabled"], "items": []}

    def add(name: str, ok: bool, detail: str, level: str = RED) -> None:
        icon = GREEN if ok else level
        rep["items"].append({"name": name, "ok": ok, "detail": detail})
        if not as_json:
            print(f"  {icon} {name:<12} {detail}")

    if not as_json:
        print(f"[链层自检] rpc={ep or '(未配置)'}  模式={mode}  chain_id={c['chain_id']}  钱包={c['wallet']}")

    add("配置完整性", fschain.has_endpoint(), f"端点已配（{mode}）" if fschain.has_endpoint() else "缺 RPC 端点", YELLOW)

    if not fschain.has_endpoint():
        for n in ("RPC 连通", "合约可读", "钱包余额"):
            add(n, False, "未配置，跳过", YELLOW)
        _tail(rep, c, as_json)
        return 0

    try:
        web = fschain.w3()
        cid = web.eth.chain_id
        add("RPC 连通", True, f"chain_id={cid}"
            + ("" if cid == c["chain_id"] else f"（与配置 {c['chain_id']} 不一致）"))
    except Exception as e:
        add("RPC 连通", False, f"{type(e).__name__}: {str(e)[:80]}", YELLOW)
        for n in ("合约可读", "钱包余额"):
            add(n, False, "RPC 不可达", YELLOW)
        _tail(rep, c, as_json)
        return 0

    try:
        bal = web.from_wei(web.eth.get_balance(web.to_checksum_address(c["wallet"])), "ether")
        add("钱包余额", float(bal) > 0, f"{bal} 原生代币")
    except Exception as e:
        add("钱包余额", False, str(e)[:60], YELLOW)

    if not c["contract"]:
        add("合约可读", False, "BOT_CONTRACT 未配置（先部署）", YELLOW)
    else:
        try:
            co = fschain.contract()
            v = co.functions.VERSION().call()
            a, p = co.functions.stats().call()
            owner = co.functions.owner().call()
            add("合约可读", True, f"{v}  approvals={a} predictions={p}")
            add("合约归属", owner.lower() == c["wallet"].lower(), f"owner={owner}", YELLOW)
            rep["address_link"] = f"{c['explorer']}/address/{c['contract']}"
        except Exception as e:
            add("合约可读", False, f"{type(e).__name__}: {str(e)[:80]}", YELLOW)

    _tail(rep, c, as_json)
    return 0


def _tail(rep: dict, c: dict, as_json: bool) -> None:
    ok = all(i["ok"] for i in rep["items"] if i["name"] in ("RPC 连通", "合约可读"))
    rep["verdict"] = "可演示" if ok else "降级（页面照常运行，徽标显示未上链）"
    if as_json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return
    if rep.get("address_link"):
        print(f"\n  主网浏览器：{rep['address_link']}")
    print(f"  结论：{GREEN if ok else YELLOW} {rep['verdict']}")


if __name__ == "__main__":
    raise SystemExit(main())
