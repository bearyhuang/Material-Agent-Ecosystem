#!/usr/bin/env python3
"""③ Agent 链上身份注册（ERC-8004）—— 调 BOT Chain Agent OS API，不自己写合约。

    POST /v1/wallets            → 创建 Agent 钱包（平台托管私钥 / 代付 Gas）
    POST /v1/agents/identities  → 注册身份，异步生成可验证的 agentId

    python scripts/chain_erc8004.py --dry-run   # 打印将要注册的 Agent 与等价 curl
    python scripts/chain_erc8004.py             # 真注册（需 BOT_AGENTOS_URL + TOKEN）
    python scripts/chain_erc8004.py --all       # 含铸造厂 9 个节点（默认只注册竞技场 10 个）

⚠️ ai4s01 §1 V4：接口路径未实测核验。若 404/401，脚本会原样打印响应，
   请以 dev-docs.botchain.ai 的 Agent OS / agent-identity 页面为准改路径后重试。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import fschain  # noqa: E402

# 竞技场蜂群（swarm.py: build_graph） —— 与 app/api.py 的 /api/agents registry 对齐
ARENA_AGENTS = [
    {"id": "supervisor", "role": "supervisor", "level": "L1",
     "capabilities": ["parse_task", "match_signals", "register_run"], "trust_scope": "db:read_only"},
    {"id": "champion_ridge", "role": "champion", "level": "L2", "family": "ridge",
     "capabilities": ["build_model", "fit", "predict"], "trust_scope": "db:read_only"},
    {"id": "champion_pls", "role": "champion", "level": "L2", "family": "pls",
     "capabilities": ["build_model", "fit", "predict"], "trust_scope": "db:read_only"},
    {"id": "champion_rf", "role": "champion", "level": "L2", "family": "rf",
     "capabilities": ["build_model", "fit", "predict"], "trust_scope": "db:read_only"},
    {"id": "champion_gbr", "role": "champion", "level": "L2", "family": "gbr",
     "capabilities": ["build_model", "fit", "predict"], "trust_scope": "db:read_only"},
    {"id": "champion_gp", "role": "champion", "level": "L2", "family": "gp",
     "capabilities": ["build_model", "fit", "predict", "uq"], "trust_scope": "db:read_only"},
    {"id": "arbiter", "role": "arbiter", "level": "L1",
     "capabilities": ["pugh_matrix", "verdict"], "trust_scope": "db:read_only"},
    {"id": "red_team", "role": "red_team", "level": "L2",
     "capabilities": ["leak_check", "ood_check", "family_audit"], "trust_scope": "db:read_only"},
    {"id": "integrator", "role": "integrator", "level": "L1",
     "capabilities": ["freeze_golden", "gate_evaluate"], "trust_scope": "evo.capsule:write"},
    {"id": "scribe", "role": "scribe", "level": "L0",
     "capabilities": ["append_event"], "trust_scope": "evo.evolution_event:append_only"},
]

# 虚拟配方铸造厂（fsfoundry.py: build_graph）
FOUNDRY_AGENTS = [
    {"id": "foundry_supervisor", "role": "supervisor", "level": "L1",
     "capabilities": ["plan_round", "inherit_strategy"], "trust_scope": "db:read_only"},
    {"id": "foundry_hypothesis", "role": "hypothesis", "level": "L2",
     "capabilities": ["propose_mechanism"], "trust_scope": "db:read_only"},
    {"id": "foundry_critic", "role": "critic", "level": "L2",
     "capabilities": ["attack", "verdict"], "trust_scope": "db:read_only"},
    {"id": "foundry_propose", "role": "propose", "level": "L2",
     "capabilities": ["sample_candidate", "bo_acquisition"], "trust_scope": "db:read_only"},
    {"id": "foundry_gate", "role": "gate", "level": "L1",
     "capabilities": ["human_approval_gate"], "trust_scope": "evo.experiment_plan:human_only"},
    {"id": "foundry_lab", "role": "lab", "level": "L2",
     "capabilities": ["virtual_lab_simulate"], "trust_scope": "db:read_only"},
    {"id": "foundry_ingest", "role": "ingest", "level": "L1",
     "capabilities": ["import_real_result"], "trust_scope": "evo.experiment_result:write"},
    {"id": "foundry_datacard", "role": "datacard", "level": "L1",
     "capabilities": ["build_datacard", "applicability", "compliance"],
     "trust_scope": "evo.datacard:write"},
    {"id": "foundry_evolve", "role": "evolve", "level": "L1",
     "capabilities": ["strategy_inheritance"], "trust_scope": "evo.formula_strategy:write"},
]


def main() -> int:
    ap = argparse.ArgumentParser(description="ERC-8004 Agent 链上身份注册")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true", help="含铸造厂 9 个节点")
    ap.add_argument("--wallet", default=None, help="覆盖 BOT_WALLET")
    ap.add_argument("--export", action="store_true",
                    help="把 Agent 能力卡导出为 artifacts/chain/agent_cards.json（不需要网络）")
    args = ap.parse_args()

    if args.wallet:
        import os
        os.environ["BOT_WALLET"] = args.wallet

    cards = ARENA_AGENTS + (FOUNDRY_AGENTS if args.all else [])
    c = fschain.cfg()
    print(f"[i] Agent 数量 {len(cards)}  钱包 {c['wallet']}  chain_id {c['chain_id']}")

    if args.export:
        out = fschain.ROOT / "artifacts" / "chain" / "agent_cards.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "protocol": "formulaswarm/1.0",
            "erc8004": True,
            "chain_id": c["chain_id"],
            "wallet_address": c["wallet"],
            "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
            "agents": [{"id": a["id"], "role": a["role"], "level": a["level"],
                        "family": a.get("family"),
                        "capabilities": a["capabilities"], "trust_scope": a["trust_scope"]}
                       for a in cards],
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[+] 已导出 {len(cards)} 张 Agent 能力卡 → {out}")
        return 0

    res = fschain.register_agents(cards, dry_run=args.dry_run)
    if res["mode"] == "dry-run":
        print(f"\n[DRY-RUN] 端点 {res['endpoint']}")
        print(f"  等价命令：\n    {res['curl']}\n")
        print("  将要注册的 Agent：")
        for p in res["payloads"]:
            m = p["metadata"]
            print(f"    - {m['name']:<20} role={m['role']:<12} level={m['level']} "
                  f"scope={m['trust_scope']}")
        print("\n  配置 BOT_AGENTOS_URL + BOT_AGENTOS_TOKEN 后去掉 --dry-run 即真注册。")
        return 0

    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
