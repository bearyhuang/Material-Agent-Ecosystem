"""数据层 → 特征层：入口是语义视图 mat.v_arena_dataset（Agent 只读，不碰基表）。

- 原料级展开：composition（递归 CTE 已把中间体展开到原料）
- 机理特征：ee_ratio（当量比）/ ee_deviation / epoxy_eq / amine_h_eq（schema_ext 已算好）
- 工艺特征：cure_temp_c / post_cure / test_temp_c
- 分组维度：family（配方族，供 Red-team 做 group 质疑）

排序只按 created_at（时间前向，禁止随机打乱）。
"""
import hashlib

import numpy as np
import pandas as pd

from fsdb import connect

ARENA_SQL = """
select formulation_id, code, system, stage, family, project_code, app_code, industry, substrate,
       created_at,
       resin_pct, hardener_pct, toughener_pct, filler_pct, diluent_pct,
       additive_pct, polyol_pct, isocyanate_pct,
       polymer_pct, pag_pct, quencher_pct, solvent_pct,
       epoxy_eq, amine_h_eq, ee_ratio, ee_deviation, pct_total,
       solid_content, pag_loading_pct, quench_ratio, acid_mol_per_100g,
       target_value, unit, verdict,
       cure_temp_c, post_cure, test_temp_c, batch_anomaly, composition
from mat.v_arena_dataset
where test_item = %(target)s and target_value is not null {system_filter}
order by created_at, formulation_id
"""

CAT_COLS = ["resin_pct", "hardener_pct", "toughener_pct", "filler_pct",
            "diluent_pct", "additive_pct", "polyol_pct", "isocyanate_pct",
            "polymer_pct", "pag_pct", "quencher_pct", "solvent_pct"]
MECH_COLS = ["epoxy_eq", "amine_h_eq", "ee_ratio", "ee_deviation", "pct_total",
             # 光刻胶机理量：与环氧 ee_ratio 同一套路（化学先验 → 数值特征）。
             # 非光刻胶体系恒为 0，配合 has_pr 指示位让模型知道该不该用。
             "solid_content", "pag_loading_pct", "quench_ratio", "acid_mol_per_100g"]
META_COLS = ["formulation_id", "code", "system", "family", "created_at", "verdict", "stage"]


def load_arena_view(target="lap_shear_strength", system=None):
    sql = ARENA_SQL.format(system_filter="and system = %(system)s" if system else "")
    with connect() as c, c.cursor() as cur:
        cur.execute(sql, {"target": target, "system": system})
        rows = cur.fetchall()
    if not rows:
        raise SystemExit(f"mat.v_arena_dataset 中没有 test_item={target!r} 且 target_value 非空的数据")
    return pd.DataFrame(rows)


def build_features(df, mode="both", min_count=5):
    """mode: cat=类别+机理+工艺 / raw=原料级展开+工艺 / both=两者并用。"""
    df = df.sort_values("created_at", kind="mergesort").reset_index(drop=True)   # 时间前向

    comp = pd.DataFrame(list(df["composition"])).fillna(0.0).astype(float)
    comp = comp.reindex(sorted(comp.columns), axis=1)
    comp = comp[[c for c in comp.columns if (comp[c] > 0).sum() >= min_count]]    # 稀疏原料降维

    mech = df[MECH_COLS].astype(float).fillna(0.0)
    cat = df[CAT_COLS].astype(float).fillna(0.0)
    proc = pd.DataFrame({
        "cure_temp_c": df["cure_temp_c"].astype(float).fillna(0.0),
        "test_temp_c": df["test_temp_c"].astype(float).fillna(0.0),
        "post_cure": df["post_cure"].map({True: 1.0, False: 0.0}).fillna(0.0),
        "has_ee": df["ee_ratio"].notna().astype(float),          # PU/丙烯酸体系无量比 → 指示位
        "has_pr": df["quench_ratio"].notna().astype(float),      # 非光刻胶体系无淬灭比 → 指示位
    }, index=df.index)
    sys_oh = pd.get_dummies(df["system"].fillna("unknown"), prefix="sys").astype(float)

    blocks = {"cat": [cat, mech, proc, sys_oh],
              "raw": [comp, proc, sys_oh],
              "both": [comp, cat, mech, proc, sys_oh]}[mode]
    Xdf = pd.concat(blocks, axis=1)
    feature_cols = list(Xdf.columns)
    X = Xdf.to_numpy(dtype=float)
    y = df["target_value"].astype(float).to_numpy()
    return X, y, df[META_COLS], feature_cols


def load_dataset(target="lap_shear_strength", mode="both", min_count=5, system=None):
    return build_features(load_arena_view(target, system), mode, min_count)


def dataset_hash(X, y):
    return hashlib.sha256(np.ascontiguousarray(X).tobytes()
                          + np.ascontiguousarray(y).tobytes()).hexdigest()[:16]
