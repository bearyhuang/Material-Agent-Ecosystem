"""竞技场确定性内核：CLI（arena.py）与编排层（swarm.py）共用。

唯一数值来源。Agent 只能读这里返回的数字，不能自己产生指标。
"""
import random

import numpy as np

from fsdata import dataset_hash, load_dataset
from fsmodels import (GENE_SIGNALS, SPACE, build_model, ood_ratio, predict, r2,
                      rolling_origin, stability)

NOMINAL = 0.90                 # 名义置信水平
COVER_BAND = (0.80, 0.97)      # conformal 覆盖率合格带
BASELINE_GAIN = 0.90           # golden MAE 须 ≤ 基线 90%
MAX_FINALISTS = 3
CAL_FRAC = 0.35                # dev 尾部用作 conformal 校准集的比例


def prepare(round_no, target="lap_shear_strength", system=None, feature_mode="both"):
    """取数 + 时间前向切分（golden 逐轮向后滚动）。"""
    X, y, meta, feats = load_dataset(target, feature_mode, system=system)
    n = len(y)
    dev_end = int(n * (0.6 if round_no == 1 else 0.8))
    gold_end = int(n * (0.8 if round_no == 1 else 1.0))
    if gold_end <= dev_end:
        raise SystemExit(f"样本量 {n} 不足以支撑第 {round_no} 轮切分")
    return {
        "X": X, "y": y, "meta": meta, "feats": feats,
        "Xd": X[:dev_end], "yd": y[:dev_end],
        "Xg": X[dev_end:gold_end], "yg": y[dev_end:gold_end],
        "dev_end": dev_end, "gold_end": gold_end,
        "dhash": dataset_hash(X[:gold_end], y[:gold_end]),
        "round": round_no, "target": target, "system": system,
    }


def propose_configs(family, budget, warm, rng):
    """Champion 提案：继承来的通过配置优先，其余从搜索空间随机取。"""
    pool = [dict(c) for c in SPACE[family]]
    rng.shuffle(pool)
    out = [w["config"] for w in warm if w["family"] == family]
    out += [c for c in pool if c not in out]
    return out[:budget]


def evaluate_trials(families, budget, warm, ctx, folds=4, seed=0, on_eval=None):
    """Arbiter 用：时间前向滚动评估（禁止随机 CV）。"""
    rng = random.Random(seed)
    trials = []
    for fam in families:
        for cfg in propose_configs(fam, budget, warm, rng):
            mae, oof, fold_maes = rolling_origin(fam, cfg, ctx["Xd"], ctx["yd"], folds)
            t = {"family": fam, "config": cfg, "dev_mae": mae,
                 "oof_abs_res": oof, "fold_maes": fold_maes}
            trials.append(t)
            if on_eval:
                on_eval(fam, cfg, mae)
    return trials


def pick_finalists(trials, k=MAX_FINALISTS):
    """每族最多 1 个入围，保多样性（Pugh 矩阵的硬约束部分）。"""
    out = []
    for t in sorted(trials, key=lambda t: t["dev_mae"]):
        if t["family"] not in [f["family"] for f in out] and len(out) < k:
            out.append(t)
    return out


def _fit(family, cfg, n_features, Xtr, ytr):
    from fsmodels import build_model
    return build_model(family, cfg, n_features).fit(Xtr, ytr)


def ensemble_evaluate(finalists, ctx, red, n_trials, exclude_unseen=False, ood_max_unseen=0.30):
    """蜂群集成：把入围者的预测做等权平均 —— 涌现性的直接证据（1+1>2 是否成立由数值说话）。

    与单模型同样遵守防自欺：golden 只评一次，且集成本身计入试验总次数（+1）。
    """
    from fsmodels import predict

    if len(finalists) < 2:
        return None
    meta, dev_end, gold_end = ctx["meta"], ctx["dev_end"], ctx["gold_end"]
    mask = np.ones(gold_end - dev_end, dtype=bool)
    if exclude_unseen:
        fam_dev = set(meta["family"].iloc[:dev_end])
        mask = meta["family"].iloc[dev_end:gold_end].isin(fam_dev).to_numpy()
    Xg, yg = ctx["Xg"][mask], ctx["yg"][mask]
    if len(yg) == 0:
        return None

    Xd, yd = ctx["Xd"], ctx["yd"]
    cal_start = int(len(yd) * 0.8)
    n_feat = ctx["X"].shape[1]
    models = [_fit(f["family"], f["config"], n_feat, Xd[:cal_start], yd[:cal_start])
              for f in finalists]

    def ens(X):
        return np.mean([predict(m, X) for m in models], axis=0)

    res_cal = np.abs(yd[cal_start:] - ens(Xd[cal_start:]))
    q_level = min(1.0, np.ceil((len(res_cal) + 1) * NOMINAL) / len(res_cal))
    q = float(np.quantile(res_cal, q_level))
    pred = ens(Xg)
    mae_g = float(np.mean(np.abs(yg - pred)))
    cov = float(np.mean(np.abs(yg - pred) <= q))
    base_mae = float(np.mean(np.abs(yg - yd.mean())))
    unseen_share = red["unseen_share"] if not exclude_unseen else 0.0
    ok_gain = mae_g <= BASELINE_GAIN * base_mae
    ok_cov = COVER_BAND[0] <= cov <= COVER_BAND[1]
    ok_ood = unseen_share <= ood_max_unseen
    reasons = [r for r, bad in (("gain<baseline", not ok_gain),
                                ("coverage_out_of_band", not ok_cov),
                                ("ood_unseen_family", not ok_ood)) if bad]
    return {
        "family": "ensemble", "config": {"members": [f["family"] for f in finalists]},
        "dev_mae": float(np.mean([f["dev_mae"] for f in finalists])),
        "holdout_mae": mae_g, "r2": r2(yg, pred),
        "stability": float(np.mean([stability(f["fold_maes"]) for f in finalists])),
        "coverage": cov, "interval_half_width": q, "baseline_mae": base_mae,
        "n_gold_eval": int(len(yg)), "excluded_ood": int((~mask).sum()),
        "trials_total": n_trials + 1, "ood_ratio": red["ood_ratio"],
        "passed": ok_gain and ok_cov and ok_ood,
        "fail_reason": ";".join(reasons) or None, "is_ensemble": True,
    }


def redteam_checks(ctx):
    """Red-team：泄漏 / 适用域 / 配方族聚集（全部由代码判定，不由 LLM 断言）。"""
    Xd, Xg = ctx["Xd"], ctx["Xg"]
    meta = ctx["meta"]
    dev_end, gold_end = ctx["dev_end"], ctx["gold_end"]
    dup = len({tuple(r) for r in np.round(Xd, 6)} & {tuple(r) for r in np.round(Xg, 6)})
    ood, thr = ood_ratio(Xd, Xg)
    fam_all = meta["family"]

    def _labels(col):
        """配方族标签集合。缺 family 的行（pandas NaN）不属于任何族，直接丢弃——
        否则 NaN 会混进 unseen 列表并被 json.dumps 写成 `NaN`，Postgres JSONB 拒收。"""
        return {x for x in col.tolist() if isinstance(x, str) and x}

    fam_dev = _labels(fam_all.iloc[:dev_end])
    gold_fam = fam_all.iloc[dev_end:gold_end]
    gold_labels = _labels(gold_fam)
    unseen = sorted(gold_labels - fam_dev)
    unseen_share = float(gold_fam.isin(unseen).mean()) if len(gold_fam) else 0.0
    return {
        "duplicates": dup,
        "split": "time_forward",
        "created_at_monotonic": bool(meta["created_at"].is_monotonic_increasing),
        "ood_ratio": round(float(ood), 3),
        "nn_dist_p95": round(float(thr), 3),
        "families_unseen_in_dev": unseen,
        "unseen_share": round(unseen_share, 3),
        "families_only_in_dev": sorted(fam_dev - gold_labels),
    }


def golden_evaluate(trial, ctx, red, n_trials, exclude_unseen=False, ood_max_unseen=0.30):
    """Integrator：golden 冻结，只评一次 + split-conformal + 三门。"""
    meta, dev_end, gold_end = ctx["meta"], ctx["dev_end"], ctx["gold_end"]
    mask = np.ones(gold_end - dev_end, dtype=bool)
    if exclude_unseen:
        fam_dev = set(meta["family"].iloc[:dev_end])
        mask = meta["family"].iloc[dev_end:gold_end].isin(fam_dev).to_numpy()
    Xg, yg = ctx["Xg"][mask], ctx["yg"][mask]
    if len(yg) == 0:
        return None

    Xd, yd = ctx["Xd"], ctx["yd"]
    cal_start = int(len(yd) * 0.8)                 # 尾部留校准集，不参与拟合
    m = build_model(trial["family"], trial["config"], ctx["X"].shape[1]).fit(
        Xd[:cal_start], yd[:cal_start])
    res_cal = np.abs(yd[cal_start:] - predict(m, Xd[cal_start:]))
    q_level = min(1.0, np.ceil((len(res_cal) + 1) * NOMINAL) / len(res_cal))
    q = float(np.quantile(res_cal, q_level))

    pred = predict(m, Xg)
    mae_g = float(np.mean(np.abs(yg - pred)))
    cov = float(np.mean(np.abs(yg - pred) <= q))
    base_mae = float(np.mean(np.abs(yg - yd.mean())))

    unseen_share = red["unseen_share"] if not exclude_unseen else 0.0
    ok_gain = mae_g <= BASELINE_GAIN * base_mae
    ok_cov = COVER_BAND[0] <= cov <= COVER_BAND[1]
    ok_ood = unseen_share <= ood_max_unseen
    reasons = [r for r, bad in (("gain<baseline", not ok_gain),
                                ("coverage_out_of_band", not ok_cov),
                                ("ood_unseen_family", not ok_ood)) if bad]
    return {
        "family": trial["family"], "config": trial["config"],
        "dev_mae": trial["dev_mae"], "holdout_mae": mae_g,
        "r2": r2(yg, pred), "stability": stability(trial["fold_maes"]),
        "coverage": cov, "interval_half_width": q,
        "baseline_mae": base_mae, "n_gold_eval": int(len(yg)),
        "excluded_ood": int((~mask).sum()),
        "trials_total": n_trials, "ood_ratio": red["ood_ratio"],
        "passed": ok_gain and ok_cov and ok_ood,
        "fail_reason": ";".join(reasons) or None,
    }
