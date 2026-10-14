#!/usr/bin/env python3
"""自进化配方算法竞技场（方案A · 工具层 CLI，H1–3）

数值全部由 src/fsarena.py 的确定性内核产生；LLM 只做选择与论证，不产出任何指标。
蜂群编排版见 swarm.py（同一内核）。

用法：
  python arena.py --round 1 --budget 10
  python arena.py --round 2 --inherit --budget 3
  python arena.py --check                    # 供 node run_eval.js 调用，0=闸门通过
"""
import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from fsarena import (evaluate_trials, golden_evaluate, pick_finalists, prepare,
                     redteam_checks)
from fsdb import (append_event, connect, env_fingerprint, insert_capsule,
                  load_warm_capsules, upsert_gene)
from fsgep import signals_for
from fsmodels import CORE_FAMILIES, GENE_SIGNALS, SPACE
from fsselector import select

OOD_MAX_UNSEEN = 0.30          # 防自欺五律⑤：未见配方族占比阈值


def run(round_no=1, inherit=False, budget=4, seed=0, target="lap_shear_strength",
        folds=4, with_xgb=False, feature_mode="both", system=None,
        ood_max_unseen=OOD_MAX_UNSEEN, selector="auto", verbose=True):
    ctx = prepare(round_no, target, system, feature_mode)
    run_id = f"r{round_no}-{uuid.uuid4().hex[:6]}"
    families = CORE_FAMILIES + (["xgb"] if with_xgb else [])
    results = []

    with connect() as c, c.cursor() as cur:
        append_event(cur, run_id, "supervisor", "start",
                     {"round": round_no, "inherit": inherit, "target": target,
                      "system": system, "n_dev": ctx["dev_end"],
                      "n_gold": ctx["gold_end"] - ctx["dev_end"],
                      "dataset_hash": ctx["dhash"], "budget_per_family": budget,
                      "families": families})

        red = redteam_checks(ctx)
        append_event(cur, run_id, "red_team", "leak_check",
                     {"duplicates": red["duplicates"], "split": "time_forward",
                      "created_at_monotonic": red["created_at_monotonic"]})
        if red["duplicates"]:
            raise SystemExit(f"dev/golden 存在 {red['duplicates']} 条重复配方（泄漏），拒绝继续")
        append_event(cur, run_id, "red_team", "ood", red)

        warm, source = [], None
        if inherit:
            if selector in ("auto", "gep"):    # 走 EvoMap 资产：signals 匹配 + 历史 Capsule 排序
                warm = select(signals_for(ctx["dev_end"], target, system), limit=6)[1]
                source = "evomap-gep"
            if not warm:                        # 资产缺失 → 退回 PG（先同哈希，再任意历史轮次）
                warm = load_warm_capsules(cur, round_no, ctx["dhash"]) \
                    or load_warm_capsules(cur, round_no, None)
                source = "pg-fallback"
        if warm:
            append_event(cur, run_id, "selector", "inherit",
                         {"source": source,
                          "warm": [{"family": w["family"], "config": w["config"]} for w in warm]})

        def on_eval(fam, cfg, mae):
            append_event(cur, run_id, f"champion:{fam}", "evaluate",
                         {"config": cfg, "dev_mae": round(mae, 4)})

        trials = evaluate_trials(families, budget, warm, ctx, folds, seed, on_eval)
        finalists = pick_finalists(trials)
        n_trials = len(trials)
        append_event(cur, run_id, "red_team", "selection_bias",
                     {"trials_total": n_trials, "flag": n_trials > 20,
                      "finalists": len(finalists)})

        env = env_fingerprint()
        for f in finalists:
            res = golden_evaluate(f, ctx, red, n_trials,
                                  ood_max_unseen=ood_max_unseen)
            if res is None:
                continue
            results.append(res)
            append_event(cur, run_id, "integrator", "gate",
                         {k: (round(v, 4) if isinstance(v, float) else v)
                          for k, v in res.items() if k != "config"})
            gid = f"gene_{res['family']}_small_n"
            upsert_gene(cur, {"id": gid, "family": res["family"],
                              "signals_match": dict(GENE_SIGNALS[res["family"]], target=target),
                              "strategy": {"space": SPACE[res["family"]], "features": ctx["feats"]},
                              "avoid": ["random_split", "tune_on_golden"],
                              "validation": "node run_eval.js --check"})
            insert_capsule(cur, {"gene_id": gid, "run_id": run_id, "round": round_no,
                                 "config": res["config"], "dataset_hash": ctx["dhash"],
                                 "split": "time_forward", "dev_mae": res["dev_mae"],
                                 "holdout_mae": res["holdout_mae"], "coverage": res["coverage"],
                                 "trials_total": n_trials, "passed": res["passed"],
                                 "fail_reason": res["fail_reason"], "env_fingerprint": env})
            append_event(cur, run_id, "scribe", "promote" if res["passed"] else "reject",
                         {"gene_id": gid, "family": res["family"],
                          "holdout_mae": round(res["holdout_mae"], 4),
                          "fail_reason": res["fail_reason"]})

    if verbose:
        print(f"\n[{run_id}] target={target} system={system or 'ALL'} "
              f"features={feature_mode}({ctx['X'].shape[1]}) round={round_no} inherit={inherit} "
              f"n_dev={ctx['dev_end']} n_gold={ctx['gold_end'] - ctx['dev_end']} "
              f"试验总数={len(trials)}")
        print(f"{'Model':<8}{'devMAE':>8}{'goldMAE':>9}{'R2':>7}{'Stab':>7}{'UQ(cov)':>9}{'±CI':>7}{'OOD':>7}  Status")
        for r in sorted(results, key=lambda r: r["holdout_mae"]):
            status = "🏆 PASS" if r["passed"] else f"FAIL:{r['fail_reason']}"
            print(f"{r['family']:<8}{r['dev_mae']:>8.3f}{r['holdout_mae']:>9.3f}{r['r2']:>7.2f}"
                  f"{r['stability']:>7.2f}{r['coverage']:>9.2f}{r['interval_half_width']:>7.2f}"
                  f"{r['ood_ratio']:>7.2f}  {status}")
        if len(trials) > 20:
            print(f"⚠️ 选择性过拟合风险：本轮共试 {len(trials)} 次，胜者 dev_mae 被高估")
        if red["ood_ratio"] > 0.2:
            print(f"⚠️ 适用域外：{red['ood_ratio']:.0%} 的 golden 样本落在 dev 分布之外，结论不外推")
        if red["families_unseen_in_dev"]:
            print(f"⚠️ 配方族外推：golden 中的 {red['families_unseen_in_dev']} 在 dev 中从未出现，"
                  f"占 golden {red['unseen_share']:.0%}（阈值 {ood_max_unseen:.0%}）→ 超阈值不给结论")
        print("⚠️ 数据为合成/脱敏数据，不声称胶粘剂真实精度")
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--inherit", action="store_true", help="继承上一轮通过闸门的 Gene/Capsule")
    ap.add_argument("--budget", type=int, default=4, help="每个算法族的试验预算")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--target", default="lap_shear_strength")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--features", default="both", choices=["cat", "raw", "both"],
                    help="cat=类别+机理+工艺 / raw=原料级展开 / both=并用")
    ap.add_argument("--system", default=None, help="只建某一体系的域内模型（如 epoxy）")
    ap.add_argument("--ood-max-unseen", type=float, default=OOD_MAX_UNSEEN,
                    help="golden 中 dev 未见配方族占比超过此值 → 硬拒答")
    ap.add_argument("--with-xgb", action="store_true", help="加入 XGBoost 挑战者")
    ap.add_argument("--selector", default="auto", choices=["auto", "gep", "pg"],
                    help="继承来源：auto=优先 EvoMap GEP 资产 / gep=只用资产 / pg=只用 PG")
    ap.add_argument("--check", action="store_true", help="评测门模式：退出码 0=通过")
    a = ap.parse_args()
    res = run(a.round, a.inherit, a.budget, a.seed, a.target, a.folds,
              a.with_xgb, a.features, a.system, a.ood_max_unseen, a.selector)
    if a.check:
        sys.exit(0 if any(r["passed"] for r in res) else 1)
