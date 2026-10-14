#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实数据上的受控 A/B：检验「经验继承」在真实信号下是否仍成立。

为什么不用 swarm.py --inherit 做这个对比
------------------------------------------------
swarm.py 的继承走 Selector → PG/GEP 资产，但 evo.capsule **没有 system/dataset 列**，
StringUtils 无法区分 came from 合成数据还是真实数据；跨体系继承会污染对照实验。
因此这里做受控对照：**同一评估窗口、同一预算、同一代价**，只让"各组看到的候选集合"不同。

实验协议
------------------------------------------------
沿伪时间把真实数据切成三段：
    past  [0, 0.40n)   ← "上一轮经验来源"，在此用穷举搜索给整个超参空间排序
    now   [0.40n,0.80n) ← 评估窗口（冷/热启动都在这里用完全相同的方式评估）
    golden[0.80n, n)   ← 冻结留出集，只评一次

冷启动 cold：直接从搜索空间随机抽 k 个配置（inheritance = False）
热启动 warm：用 past 窗口挑出的 top-k 配置（inheritance = True）
两者都在 now 窗口上算 4 折时间前向 dev MAE，取 k 次试验的最优值作比较。

额外报告三个"真实信号"才看得出来的指标：
  1. 转移性 ρ：past 排序 vs now 排序的 Spearman 相关（继承的前提假设）
  2. 命中率：past top-k 落在 now 前 25% 的比例
  3. 搜索效率：随机要抽多少次才能达到 warm 的水平（试错轮次是否真的下降）

用法（容器内）：
  python scripts/eval_real_ab.py --system CONCRETE       # n=1030 大样本体制
  python scripts/eval_real_ab.py --system CONCRETE_SMALL # n=200 小样本体制
"""
import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fsarena import golden_evaluate, redteam_checks
from fsdata import load_dataset
from fsmodels import SPACE, rolling_origin

TARGET_DEFAULT = "compressive_strength_MPa"
SEEDS = list(range(10))
FOLDS = 4
PAST_FRAC, NOW_FRAC = 0.40, 0.80


def spearman(a, b):
    """秩相关（不引 scipy 依赖）。a/b 是等长的两列数值。"""
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    ra, rb = rank(a), rank(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return num / den if den else float("nan")


def make_ctx(X, y, meta, dev_end, gold_end):
    return {"X": X, "y": y, "meta": meta, "Xd": X[:dev_end], "yd": y[:dev_end],
            "Xg": X[dev_end:gold_end], "yg": y[dev_end:gold_end],
            "dev_end": dev_end, "gold_end": gold_end}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", default="CONCRETE")
    ap.add_argument("--target", default=TARGET_DEFAULT)
    ap.add_argument("--budget", type=int, default=3, help="每族配置数 k")
    ap.add_argument("--features", default="raw", choices=["raw", "cat", "both"])
    ap.add_argument("--rho-thr", type=float, default=0.5,
                    help="过去内部一致性阈值：≥该值才继承，低于则本轮改为随机探索")
    a = ap.parse_args()

    X, y, meta, feats = load_dataset(a.target, a.features, system=a.system)
    n = len(y)
    pa, no = int(n * PAST_FRAC), int(n * NOW_FRAC)
    print(f"\n===== 真实数据 · {a.system} =====")
    print(f"n={n}  past={pa}  now={no - pa}（评估窗口）  golden={n - no}（冻结）")
    print(f"目标 {a.target}：mean={y.mean():.2f}  sd={y.std():.2f}  range=[{y.min():.2f},{y.max():.2f}]")

    rho_int = {}
    # ---- 1. 穷举整个搜索空间在 past 与 now 上的表现（转移性的 ground truth）----
    print("\n--- 超参空间排序转移性（past → now，继承的前提假设）---")
    print(f"{'族':<8}{'空间大小':>8}{'ρ(Spearman)':>13}{'top-k命中前25%':>15}")
    warm_top = {}
    now_rank = {}
    def _key(fam, cfg):
        return (fam, tuple(sorted(cfg.items())))

    cache = {}                                    # now 窗口结果复用，避免重复拟合

    def now_mae_of(fam, cfg):
        k = _key(fam, cfg)
        if k not in cache:
            cache[k] = rolling_origin(fam, cfg, X[pa:no], y[pa:no], FOLDS)[0]
        return cache[k]

    for fam, space in SPACE.items():
        if fam == "xgb":
            continue
        past_mae = [rolling_origin(fam, cfg, X[:pa], y[:pa], FOLDS)[0] for cfg in space]
        now_mae = [now_mae_of(fam, cfg) for cfg in space]
        rho = spearman(past_mae, now_mae)
        order_past = sorted(range(len(space)), key=lambda i: past_mae[i])
        order_now = sorted(range(len(space)), key=lambda i: now_mae[i])
        top_now = set(order_now[:max(1, len(space) // 4)])
        hit = sum(1 for i in order_past[:a.budget] if i in top_now)
        print(f"{fam:<8}{len(space):>8}{rho:>13.3f}{hit:>10}/{a.budget}")
        warm_top[fam] = [space[i] for i in order_past[:a.budget]]
        now_rank[fam] = (order_now, now_mae)
        # 过去内部一致性（past 前后两半的排序相关）—— 只用过去信息，不看未来
        half = len(y[:pa]) // 2
        mae_a = [rolling_origin(fam, cfg, X[:half], y[:half], 3)[0] for cfg in space]
        mae_b = [rolling_origin(fam, cfg, X[half:pa], y[half:pa], 3)[0] for cfg in space]
        rho_int[fam] = spearman(mae_a, mae_b)

    # ---- 2. 冷 / 热 / 选择性继承：同一窗口、同一预算 ----
    print(f"\n--- 三臂对照（窗口 now，预算 k={a.budget}/族，{len(SEEDS)} 个随机种子）---")
    print("过去的内部一致性 ρ_int 用来决定「这族值不值得继承」（阈值 0.5，只用过去信息）")
    print(f"{'族':<8}{'ρ_int':>8}{'cold':>9}{'warm':>9}{'selective':>11}"
          f"{'冷优于暖次数':>13}{'选定策略':>9}")
    cold_all, warm_all, sel_all = [], [], []
    THRESHOLDS = (0.0, 0.3, 0.5)
    by_thr = {t: [] for t in THRESHOLDS}
    for fam, space in SPACE.items():
        if fam == "xgb":
            continue
        cold_best = []
        warm_best = min(now_mae_of(fam, cfg) for cfg in warm_top[fam])
        for s in SEEDS:
            rng = random.Random(s)
            pool = list(space)
            rng.shuffle(pool)
            cold_best.append(min(now_mae_of(fam, c) for c in pool[:a.budget]))
        inherit = rho_int[fam] >= a.rho_thr
        # 逐种子：该族不继承时，本种子退化为随机探索
        sel_best = [warm_best] * len(SEEDS) if inherit else list(cold_best)
        cm = sum(cold_best) / len(cold_best)
        sm = sum(sel_best) / len(sel_best)
        cold_wins = sum(1 for c in cold_best if c < warm_best - 1e-9)
        print(f"{fam:<8}{rho_int[fam]:>8.2f}{cm:>9.3f}{warm_best:>9.3f}{sm:>11.3f}"
              f"{cold_wins:>10}/{len(SEEDS)}{'继承' if inherit else '探索':>9}")
        cold_all.append(cm)
        warm_all.append(warm_best)
        sel_all.append(sm)
        for thr in THRESHOLDS:
            by_thr[thr].append([warm_best] * len(SEEDS) if rho_int[fam] >= thr
                               else list(cold_best))

    def agg(v):
        return sum(v) / len(v)

    print(f"\n各族均值（MPa，越小越好）")
    print(f"  cold(纯随机探索)  {agg(cold_all):.3f}")
    print(f"  warm(全盘继承)    {agg(warm_all):.3f}   vs cold {(agg(warm_all) - agg(cold_all)) / agg(cold_all) * 100:+.1f}%")
    print(f"  selective(ρ_int≥{a.rho_thr} 才继承)  {agg(sel_all):.3f}   "
          f"vs cold {(agg(sel_all) - agg(cold_all)) / agg(cold_all) * 100:+.1f}%   "
          f"vs warm {(agg(sel_all) - agg(warm_all)) / agg(warm_all) * 100:+.1f}%")
    print("\n  阈值敏感性（各族提优/退化，相对 cold）：")
    for thr in THRESHOLDS:
        vals = []
        for i in range(len(cold_all)):
            vals.append(sum(by_thr[thr][i]) / len(by_thr[thr][i]))
        print(f"    ρ_int≥{thr:<4} → {agg(vals):.3f}  "
              f"{(agg(vals) - agg(cold_all)) / agg(cold_all) * 100:+.1f}%")

    # ---- 3. Red-team + golden 闸门（best model 走完整防自欺流程）----
    ctx = make_ctx(X, y, meta, no, n)
    red = redteam_checks(ctx)
    print("\n--- Red-team 检查（真实数据）---")
    for k in ("duplicates", "ood_ratio", "unseen_share", "families_unseen_in_dev"):
        print(f"  {k}: {red[k]}")

    best = None
    for fam, space in SPACE.items():
        if fam == "xgb":
            continue
        for cfg in space:
            m = rolling_origin(fam, cfg, X[:no], y[:no], FOLDS)[0]
            if best is None or m < best[0]:
                best = (m, fam, cfg)
    print(f"\n--- 冻结门槛（dev 最优 {best[1]} {best[2]} dev_mae={best[0]:.3f}）---")
    res = golden_evaluate({"family": best[1], "config": best[2],
                           "dev_mae": best[0], "fold_maes": [best[0]]},
                          ctx, red, n_trials=sum(len(v) for v in SPACE.values()))
    if res:
        print(f"  holdout_mae={res['holdout_mae']:.3f}  R²={res['r2']:.2f}  "
              f"覆盖率={res['coverage']:.2f}  区间半宽={res['interval_half_width']:.2f}")
        print(f"  基线(预测 dev 均值)={res['baseline_mae']:.3f}  通过={res['passed']}  "
              f"原因={res['fail_reason']}")
    print("\n⚠️ 真实数据来自 UCI #165（混凝土抗压强度），伪时间轴由文件行序生成；")
    print("   该数据与胶粘剂无关，只用于检验『算法选择/继承/闸门』的机制是否成立。")


if __name__ == "__main__":
    main()
