"""虚拟实验室（方案 B 的"实验"环节）—— 用竞技场冠军模型做 surrogate 预测。

循环论证防线（三条，缺一不可）：
  1. 返回值一律落 `evo.experiment_result(simulated=true)`，**永不写入 mat.performance**；
  2. 预测区间来自冠军模型 + split-conformal 校准（校准数据全部来自真实留出集）；
  3. 对外声称的精度只有 `holdout_mae`（留出集实测），**留出集不含任何模拟点**。

模拟器版本串进 `simulator_version`，任何一条数据卡都能反查到它是哪个冠军模型算出来的。
"""
from __future__ import annotations

import platform

import numpy as np

from fsmodels import build_model, predict

CHAMPION_SQL = """
select c.id::text as capsule_id, c.gene_id, g.family, c.config, c.dev_mae, c.holdout_mae,
       c.coverage, c.round, c.dataset_hash, c.system, c.split, c.created_at::text
from evo.capsule c join evo.gene g on g.id = c.gene_id
where c.passed and c.holdout_mae is not null
  and (%(system)s::text is null or c.system = %(system)s)
order by c.holdout_mae asc
limit 1
"""

SIM_VERSION = "fsim-1.0"


def champion_signature(champ):
    return (f"champ:{champ['family']}:{champ['gene_id']}"
            f":capsule={str(champ['capsule_id'])[:8]}:{SIM_VERSION}")


def load_champion(system=None, cur=None):
    """取通过闸门的最优 Capsule 作为 surrogate（没有冠军 → 不允许开虚拟实验室）。"""
    from fsdb import connect

    if cur is None:
        with connect() as c, c.cursor() as cur2:
            cur2.execute(CHAMPION_SQL, {"system": system})
            return cur2.fetchone()
    cur.execute(CHAMPION_SQL, {"system": system})
    return cur.fetchone()


def _env():
    import sklearn
    return {"python": platform.python_version(), "sklearn": sklearn.__version__}


class VirtualLab:
    """冠军模型 + conformal 校准 = 虚拟实验室。

    - fit：用 dev（真实观测）拟合冠军配置
    - predict：返回点预测 + 区间半宽（split-conformal，校准集为 dev 尾部真实数据）
    """

    def __init__(self, champion, ctx, nominal=0.90, cal_frac=0.8):
        self.champion = champion
        self.ctx = ctx
        self.nominal = nominal
        self.model = None
        self.q = None
        if champion is not None:
            self.fit(cal_frac)

    def fit(self, cal_frac=0.8):
        ctx = self.ctx
        Xd, yd = ctx["Xd"], ctx["yd"]
        n_cal = max(int(len(yd) * (1 - cal_frac)), 5)
        tr_end = len(yd) - n_cal
        n_feat = ctx["X"].shape[1]
        self.model = build_model(self.champion["family"], self.champion["config"], n_feat).fit(
            Xd[:tr_end], yd[:tr_end])
        res = np.abs(yd[tr_end:] - predict(self.model, Xd[tr_end:]))
        level = min(1.0, np.ceil((len(res) + 1) * self.nominal) / len(res))
        self.q = float(np.quantile(res, level))
        self.n_cal = int(len(res))
        return self

    def predict(self, X):
        X = np.atleast_2d(X)
        mu = predict(self.model, X)
        half = np.full_like(mu, self.q if self.q is not None else float("nan"))
        return mu, half

    @property
    def version(self):
        return champion_signature(self.champion) if self.champion else f"{SIM_VERSION}:no-champion"

    @property
    def holdout_mae(self):
        """真实精度口径：冠军模型在留出集上的 MAE（唯一可对外声称的精度）。"""
        return float(self.champion["holdout_mae"]) if self.champion else None


def simulate(cand, lab, feature_cols, system, catalog=None):
    """对单个候选做一次虚拟实验（返回带 simulated 标记的结果字典）。"""
    from fsrecipe import build_feature_vector, mech_features

    x = build_feature_vector(cand["items"], feature_cols, system, cand.get("process"), catalog)
    mu, half = lab.predict(x)
    mu = float(mu[0])
    half = float(half[0])
    return {
        "value": round(mu, 4),
        "ci_low": round(mu - half, 4),
        "ci_high": round(mu + half, 4),
        "interval_half_width": round(half, 4),
        "simulated": True,
        "source": "virtual_lab",
        "scale": "lab_500g",
        "model_version": lab.version,
        "simulator_version": lab.version,
        "mech": mech_features(catalog, cand["items"]) if catalog else {},
    }
