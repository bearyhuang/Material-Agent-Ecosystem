"""贝叶斯优化选点（方案 B 的"选实验"环节）—— 数值全部确定性计算。

- surrogate：高斯过程（复用 fsmodels.build_model('gp', ...)），与竞技场同源；
- 采集函数：EI / UCB / PI（解析式，numpy 实现，无额外依赖，可复现）；
- batch：constant-liar（CL-mean）串行化，避免一批点挤在同一处；
- 退化保护：候选池不足 / GP 训练失败 → 退回随机 + 最优邻域扰动，并如实标注策略。

LLM 不参与选点：它只能解释"为什么选这些点"，不能决定选哪些点。
"""
from __future__ import annotations

import random

import numpy as np
from scipy.stats import norm

from fsmodels import build_model

# ---------------------------------------------------------------- 采集函数
def _safe_sigma(sigma, floor=1e-6):
    return np.maximum(np.asarray(sigma, dtype=float), floor)


def expected_improvement(mu, sigma, best, xi=0.01):
    mu, s = np.asarray(mu, float), _safe_sigma(sigma)
    imp = mu - best - xi
    z = imp / s
    return imp * norm.cdf(z) + s * norm.pdf(z)


def probability_of_improvement(mu, sigma, best, xi=0.01):
    mu, s = np.asarray(mu, float), _safe_sigma(sigma)
    return norm.cdf((mu - best - xi) / s)


def upper_confidence_bound(mu, sigma, beta=1.0):
    return np.asarray(mu, float) + beta * _safe_sigma(sigma)


ACQ = {"ei": expected_improvement, "pi": probability_of_improvement}


def acquisition(mu, sigma, best, kind="ei", **kw):
    if kind == "ucb":
        return upper_confidence_bound(mu, sigma, beta=kw.get("beta", 1.0))
    fn = ACQ.get(kind, expected_improvement)
    return fn(mu, sigma, best, xi=kw.get("xi", 0.01))


# ---------------------------------------------------------------- surrogate
class GPSurrogate:
    """用竞技场同款 GP 做 surrogate；训练失败即降级为常数预测（并标注）。"""

    def __init__(self, cfg=None, ls=None):
        self.cfg = cfg or {"ls": 1.0}
        if ls is not None:
            self.cfg = {"ls": float(ls)}
        self.model = None
        self.degraded = False
        self.y_mean = 0.0

    def fit(self, X, y):
        X = np.asarray(X, float)
        y = np.asarray(y, float).ravel()
        self.X = X
        self.y = y
        self.y_mean = float(np.mean(y)) if len(y) else 0.0
        try:
            if len(y) < 4:
                raise ValueError("样本太少，GP 不可靠")
            self.model = build_model("gp", self.cfg, X.shape[1]).fit(X, y)
            self.degraded = False
        except Exception:
            self.model = None
            self.degraded = True
        return self

    def predict(self, X):
        X = np.atleast_2d(np.asarray(X, float))
        if self.model is None:
            return np.full(len(X), self.y_mean), np.full(len(X), 1.0)
        from fsmodels import predict as _pred

        mu = _pred(self.model, X)
        try:
            _, std = self.model.predict(X, return_std=True)
            std = np.asarray(std).ravel()
        except Exception:
            std = np.full(len(X), 1.0)
        # GP 的 std 在无噪声核下可能退化为 0，给一个下界防止 EI 塌缩
        return np.asarray(mu).ravel(), np.maximum(std, 1e-3)


# ---------------------------------------------------------------- batch BO
def propose_batch(X_cand, surrogate, y_obs, k=5, kind="ei", xi=0.01, beta=1.0,
                  liar="mean", rng=None, exclude=None):
    """constant-liar batch BO：选一个点 → 用预测均值当"假观测"喂回去 → 再选下一个。

    exclude：已被强制入选的索引（继承起点 / 冠军贪心点），不重复选。
    返回 (选中索引列表, 每步的采集值列表)。
    """
    rng = rng or random.Random(0)
    X_cand = np.asarray(X_cand, float)
    k = int(min(k, len(X_cand)))
    if k <= 0:
        return [], []
    blocked = set(int(i) for i in (exclude or []))

    X = np.asarray(getattr(surrogate, "X", None), float) if getattr(surrogate, "X", None) is not None else None
    y = np.asarray(y_obs, float).ravel()
    best = float(np.max(y)) if len(y) else 0.0
    liar_value = float(np.mean(y)) if liar == "mean" else (
        float(np.min(y)) if liar == "min" else best)

    chosen, scores = [], []
    X_work, y_work = (None, None)
    if X is not None:
        X_work, y_work = X.copy(), y.copy()

    cur = surrogate
    for step in range(k):
        mu, sigma = cur.predict(X_cand)
        acq = acquisition(mu, sigma, best, kind=kind, xi=xi, beta=beta)
        # 已选点 / 强制入选点不再重复
        for idx in set(chosen) | blocked:
            acq[idx] = -np.inf
        if not np.any(np.isfinite(acq)):
            break
        order = np.argsort(-acq)
        pick = int(order[0])
        chosen.append(pick)
        scores.append(round(float(acq[pick]), 6))
        if step == k - 1:
            break
        # constant liar：把选中点以预测均值（或指定 liar 值）当作假观测喂回
        if X_work is not None:
            nxt = GPSurrogate(cfg=cur.cfg)
            nxt.fit(np.vstack([X_work, X_cand[pick:pick + 1]]),
                    np.append(y_work, liar_value))
            cur = nxt
        else:
            break
    return chosen, scores


def diverse_topk(scores, k, X_cand, min_dist=0.10):
    """在采集值 top 里做多样性过滤：余弦距离 < min_dist 的近重复点被剔除。

    batch BO 最常见的问题是一批点挤在同一个峰上，等于只做了一个实验。
    """
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    X = np.atleast_2d(np.asarray(X_cand, float))
    norms = np.linalg.norm(X, axis=1) + 1e-12
    picked = []
    for i in order:
        ok = True
        for j in picked:
            cos = float(np.dot(X[i], X[j]) / (norms[i] * norms[j]))
            if 1.0 - cos < min_dist:
                ok = False
                break
        if ok:
            picked.append(i)
        if len(picked) >= k:
            break
    if len(picked) < k:                                  # 空间太小，按分数补齐
        for i in order:
            if i not in picked:
                picked.append(i)
            if len(picked) >= k:
                break
    return picked
