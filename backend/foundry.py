#!/usr/bin/env python3
"""方案 B · 虚拟配方铸造厂-lite（命令行入口）

假说 →（批判）→ BO 选点 → 人工批准 → 虚拟实验 → 回灌 → 数据卡 → 策略继承

用法：
  python foundry.py --rounds 2 --budget 5                   # 只出建议清单（待人批准）
  python foundry.py --rounds 2 --budget 5 --approver 张工    # 批准后才做虚拟实验
  python foundry.py --scale pilot_50kg --approver 张工       # 带放大修正
  python foundry.py --check                                 # 退出码 0 = 本轮有可用数据卡
  python foundry.py --list                                  # 打印最近的建议实验清单

L4 红线：不加 --approver 就没有任何实验被执行（计划停在 proposed）。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from fsdb import connect                      # noqa: E402
from fsfoundry import import_real_result, run_foundry  # noqa: E402

PLAN_LIST_SQL = """
select p.id, p.run_id, p.round, p.status, p.approved_by, p.target, p.predicted,
       p.ci_low, p.ci_high, p.acquisition, p.rationale, c.code, c.system, c.ee_ratio,
       p.created_at::text
from evo.experiment_plan p
left join mat.formulation_candidate c on c.id = p.candidate_id
order by p.id desc limit %(limit)s
"""


def list_plans(limit=20):
    with connect() as c, c.cursor() as cur:
        cur.execute(PLAN_LIST_SQL, {"limit": limit})
        rows = cur.fetchall()
    print(f"{'PLAN':<10}{'状态':<10}{'体系':<8}{'预测':>8}{'区间':>20}{'ee':>7}  批准人")
    for r in rows:
        ci = f"[{r['ci_low']}, {r['ci_high']}]" if r["ci_low"] is not None else "-"
        print(f"PLAN-{r['id']:<5}{r['status']:<10}{r['system'] or '-':<8}"
              f"{r['predicted'] if r['predicted'] is not None else 0:>8.2f}{ci:>20}"
              f"{(r['ee_ratio'] if r['ee_ratio'] is not None else 0):>7.2f}  {r['approved_by'] or '—'}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--budget", type=int, default=5, help="每轮建议的实验数（batch BO 的批大小）")
    ap.add_argument("--pool", type=int, default=400, help="候选池大小（须远大于 budget）")
    ap.add_argument("--system", default="epoxy")
    ap.add_argument("--target", default="lap_shear_strength")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--approver", default=None, help="批准人；不传 = 只出建议清单")
    ap.add_argument("--scale", default="lab_500g", choices=["lab_500g", "pilot_50kg", "prod_5t"])
    ap.add_argument("--features", default="both", choices=["cat", "raw", "both"])
    ap.add_argument("--no-feed-simulated", action="store_true",
                    help="不让模拟点进入下一轮搜索集（更保守）")
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--import-real", type=int, metavar="PLAN_ID",
                    help="回灌真实实验结果（人做完实验后调用）")
    ap.add_argument("--value", type=float, help="实测值（配合 --import-real）")
    ap.add_argument("--tester", default=None, help="实验人")
    ap.add_argument("--unit", default="MPa")
    ap.add_argument("--no-promote", action="store_true", help="实测后不要把候选配方正式入库")
    a = ap.parse_args()

    if not a.llm:
        import os
        os.environ["FS_NO_LLM"] = "1"

    if a.list:
        list_plans()
        return 0

    if a.import_real:
        if a.value is None:
            print("❌ --import-real 必须同时给出 --value（实测值）")
            return 2
        out = import_real_result(a.import_real, a.value, tester=a.tester, unit=a.unit,
                                 promote=not a.no_promote)
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        if out["in_interval"] is not None:
            print(("✅ 实测值落在预测区间内" if out["in_interval"]
                   else "⚠️ 实测值落在预测区间外 —— 记入负例，用于校准区间"))
        return 0

    out = run_foundry(rounds=a.rounds, target=a.target, system=a.system, budget=a.budget,
                      pool=a.pool, seed=a.seed, approver=a.approver, scale=a.scale,
                      feature_mode=a.features, feed_simulated=not a.no_feed_simulated,
                      verbose=not a.json)
    if a.json:
        payload = {
            "run_id": out["run_id"],
            "rounds": [{
                "round": o["round"], "pool": len(o["pool"]), "plans": o["plans"],
                "rejected_ood": o["rejected"], "results": o["results"],
                "datacards": [{"code": d["code"], "predicted": d["predicted"],
                               "ci": [d["ci_low"], d["ci_high"]],
                               "applicability": d["applicability"]["decision"],
                               "artifacts": d.get("artifacts")}
                              for d in o["datacards"]],
                "hypotheses": [{"code": h["code"], "status": h["status"],
                                "posterior": h.get("posterior")} for h in o["hypotheses"]],
            } for o in out["rounds"]],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if a.check:
        ok = any(o["datacards"] for o in out["rounds"])
        print(f"[check] 退出码 {0 if ok else 1}（{'有可用数据卡' if ok else '无数据卡'}）")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
