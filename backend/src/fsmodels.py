"""工具层：算法原子（Gene 族）+ 时间前向评估 + conformal + OOD 检查。

数值唯一来源：这里。LLM 只做选择与论证，不产出任何性能指标。
"""
import numpy as np
from sklearn.cross_decomposition import PLSRegression
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import ConstantKernel as C
from sklearn.gaussian_process.kernels import RBF, WhiteKernel
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

CORE_FAMILIES = ["ridge", "pls", "rf", "gbr", "gp"]

# 搜索空间应远大于单轮预算，继承才有意义（doc §9 局限）
SPACE = {
    "ridge": [{"alpha": a} for a in (0.01, 0.03, 0.1, 0.3, 1, 3, 10, 30, 100, 300)],
    "pls":   [{"k": k} for k in (1, 2, 3, 4, 5, 6, 7)],
    "rf":    [{"n": n, "d": d} for n in (50, 100, 200, 400) for d in (2, 3, 5, 8, None)],
    "gbr":   [{"n": n, "lr": lr, "d": d} for n in (50, 100, 200, 400)
              for lr in (0.02, 0.05, 0.1, 0.2) for d in (2, 3, 4)],
    "gp":    [{"ls": ls} for ls in (0.1, 0.3, 0.6, 1, 2, 4, 8, 16, 32)],
    # 钩子：TabPFN / PySR / 层次贝叶斯 在此加族即可
    "xgb":   [{"n": n, "lr": lr, "d": d} for n in (100, 300) for lr in (0.05, 0.1) for d in (2, 3, 4)],
}

GENE_SIGNALS = {
    "ridge": {"n_min": 20, "n_max": 10**6, "needs_uq": False},
    "pls":   {"n_min": 20, "n_max": 10**6, "needs_uq": False},
    "rf":    {"n_min": 50, "n_max": 10**6, "needs_uq": False},
    "gbr":   {"n_min": 80, "n_max": 10**6, "needs_uq": False},
    "gp":    {"n_min": 20, "n_max": 500, "needs_uq": True},
    "xgb":   {"n_min": 80, "n_max": 10**6, "needs_uq": False},
}


def build_model(family, cfg, n_features):
    if family == "ridge":
        return make_pipeline(StandardScaler(), Ridge(alpha=cfg["alpha"]))
    if family == "pls":
        return make_pipeline(StandardScaler(), PLSRegression(n_components=cfg["k"]))
    if family == "rf":
        return RandomForestRegressor(n_estimators=cfg["n"], max_depth=cfg["d"], random_state=0)
    if family == "gbr":
        return GradientBoostingRegressor(n_estimators=cfg["n"], learning_rate=cfg["lr"],
                                         max_depth=cfg["d"], random_state=0)
    if family == "gp":
        # 固定核（optimizer=None）：核超参不再被自动优化，ls 才是真正被搜索的量
        return make_pipeline(StandardScaler(), GaussianProcessRegressor(
            C(1.0) * RBF(np.ones(n_features) * cfg["ls"]) + WhiteKernel(0.1),
            normalize_y=True, optimizer=None, random_state=0))
    if family == "xgb":
        from xgboost import XGBRegressor
        return XGBRegressor(n_estimators=cfg["n"], learning_rate=cfg["lr"], max_depth=cfg["d"],
                            random_state=0, verbosity=0)
    raise ValueError(f"未知算法族: {family}")


def predict(m, X):
    return np.asarray(m.predict(X)).ravel()


# ---------------------------------------------------------------- 时间前向滚动（禁止随机 CV）
def rolling_origin(family, cfg, X, y, folds=4):
    n = len(y)
    start = n // 2
    step = max((n - start) // folds, 1)
    maes, oof = [], []
    for k in range(folds):
        tr_end = start + k * step
        te_end = min(tr_end + step, n)
        if te_end <= tr_end:
            break
        m = build_model(family, cfg, X.shape[1]).fit(X[:tr_end], y[:tr_end])
        r = np.abs(y[tr_end:te_end] - predict(m, X[tr_end:te_end]))
        maes.append(float(r.mean()))
        oof.extend(r.tolist())
    return float(np.mean(maes)), np.asarray(oof), maes


def stability(fold_maes):
    """折间稳定性：1/(1+CV)，越接近 1 越稳。"""
    a = np.asarray(fold_maes, dtype=float)
    if len(a) < 2 or a.mean() == 0:
        return 1.0
    return float(1.0 / (1.0 + a.std() / a.mean()))


def r2(y_true, y_pred):
    sse = float(np.sum((y_true - y_pred) ** 2))
    sst = float(np.sum((y_true - y_true.mean()) ** 2))
    return 1.0 - sse / sst if sst > 0 else float("nan")


# ---------------------------------------------------------------- Red-team：适用域检查
def ood_ratio(X_dev, X_gold):
    """golden 样本中有多少落在 dev 分布之外（最近邻距离 > dev 内部 p95）。"""
    mu, sd = X_dev.mean(0), X_dev.std(0) + 1e-9
    A, B = (X_dev - mu) / sd, (X_gold - mu) / sd
    d_gold = np.sqrt(((B[:, None, :] - A[None, :, :]) ** 2).sum(-1).min(1))
    d_dev = np.sqrt(((A[:, None, :] - A[None, :, :]) ** 2).sum(-1)
                    + np.eye(len(A)) * 1e9).min(1)
    thr = float(np.quantile(d_dev, 0.95))
    return float(np.mean(d_gold > thr)), thr
