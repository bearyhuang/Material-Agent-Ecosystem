"""虚拟配方候选的生成与硬约束校验（方案 B 的"生成"环节）。

硬约束（不满足直接拒答，不是"警告后照做"）：
  1. 质量守恒 Σ=100%（DB 触发器 + Python 双重校验）
  2. 可行域（组分上下限来自历史数据包络 + 化学常识，允许小幅外推）
  3. 当量比机理约束（环氧体系 ee_ratio = Σ(pct/AHEW) / Σ(pct/EEW)）
  4. 法规约束（VOC / REACH / SVHC / GHS，由 fscompliance 规则引擎判定）

数值一律由确定性计算产生；LLM 不参与配方生成与合规判定。
"""
from __future__ import annotations

import json
import random

import numpy as np

from fscompliance import check_formulation
from fsdb import connect

# ---------------------------------------------------------------- 可行域
# 边界来源：历史数据包络 + 化学常识（外推幅度保守，避免生成荒谬配方）
SYSTEM_SPACE = {
    "epoxy": {
        "resin": (25.0, 80.0), "hardener": (4.0, 45.0), "toughener": (0.0, 18.0),
        "filler": (0.0, 45.0), "diluent": (0.0, 15.0), "additive": (0.0, 5.0),
        "ee_target": (0.85, 1.15),
    },
    "pu": {
        "polyol": (35.0, 65.0), "isocyanate": (25.0, 50.0), "filler": (0.0, 20.0),
        "additive": (0.0, 3.0), "nco_index": (0.95, 1.15),
    },
    "acrylic": {
        "toughener": (25.0, 75.0), "filler": (20.0, 60.0), "additive": (0.0, 12.0),
    },
    # 光刻胶（化学放大胶 CAR）：约束用"相对固体分"口径，与 gen_photoresist.py 同构
    "photoresist": {
        "polymer": (10.0, 32.0), "pag": (0.15, 2.6), "quencher": (0.0, 1.0),
        "solvent": (66.0, 90.0), "additive": (0.0, 1.5),
        "solid_content": (18.0, 32.0),     # 固体分（其余为溶剂）
        "pag_loading": (1.5, 6.0),         # PAG 占**固体分**的百分比
        "quench_ratio": (0.10, 0.75),      # 淬灭剂/PAG 摩尔比（化学窗口）
    },
}

# 工艺默认值（数据卡的工艺条件；也进入特征向量的工艺维）
PROCESS_DEFAULT = {
    "epoxy":   {"cure_temp_c": 120.0, "cure_time_min": 60.0, "pressure_mpa": 0.10,
                "post_cure": False, "test_temp_c": 23.0, "method": "heat"},
    "pu":      {"cure_temp_c": 80.0, "cure_time_min": 120.0, "pressure_mpa": 0.05,
                "post_cure": False, "test_temp_c": 23.0, "method": "heat"},
    "acrylic": {"cure_temp_c": 25.0, "cure_time_min": 1440.0, "pressure_mpa": 0.02,
                "post_cure": False, "test_temp_c": 23.0, "method": "room"},
    # 光刻胶：热步骤是曝光后烘烤（PEB），落在既有 cure_temp_c 语义列上保持跨体系可比
    "photoresist": {"cure_temp_c": 105.0, "cure_time_min": 1.5, "pressure_mpa": 0.0,
                    "post_cure": False, "test_temp_c": 23.0, "method": "peb"},
}

CAT_COLS = ["resin", "hardener", "toughener", "filler", "diluent",
            "additive", "polyol", "isocyanate",
            # 光刻胶类别。缺少会导致候选的 polymer_pct/pag_pct/… 恒为 0，
            # 与训练集口径不一致 → 适用域判定把这些候选全判成域外。
            "polymer", "pag", "quencher", "solvent"]
MECH_COLS = ["epoxy_eq", "amine_h_eq", "ee_ratio", "ee_deviation", "pct_total",
             # 光刻胶机理列（与 fsdata.MECH_COLS / mat.v_mechanistic_feature 同名）。
             # 不登记就会落进 comp_cols，被当成"原料代码"取 0。
             "solid_content", "pag_loading_pct", "quench_ratio", "acid_mol_per_100g"]
# has_ee / has_pr 是与 fsdata 一致的指示位。必须登记在这里，否则会被
# build_feature_vector 当成"原料代码列"（comp_col）→ 恒取 0。
PROC_COLS = ["cure_temp_c", "test_temp_c", "post_cure", "has_ee", "has_pr"]


class Refusal(Exception):
    """生成硬约束未满足：拒答，不产出配方。"""


# SYSTEM_SPACE 里属于"导出机理量"的键：它们不是某一类原料的占比，
# 而是由组成计算出来的量（与固化剂当量比 ee_target 同性质）。
DERIVED_KEYS = ("solid_content", "pag_loading", "quench_ratio")


def photoresist_mech(catalog, items):
    """光刻胶的三个机理量（口径与 mat.v_mechanistic_feature 一致）。

    solid_content  = 除溶剂外的一切（%）
    pag_loading    = PAG 占**固体分**的百分比（不是占全配方）
    quench_ratio   = 碱当量 / 酸摩尔（化学放大胶的核心权衡）
    """
    cat_of = {it["raw_material_code"]:
              (catalog.get(it["raw_material_code"]) or {}).get("category")
              for it in items}
    pct = {it["raw_material_code"]: float(it["pct"]) for it in items}
    solv = sum(p for c, p in pct.items() if cat_of.get(c) == "solvent")
    solid = sum(pct.values()) - solv

    pag_mol = 0.0
    base_mol = 0.0
    pag_pct = 0.0
    for code, p in pct.items():
        m = catalog.get(code) or {}
        pr = m.get("props") or {}
        c = cat_of.get(code)
        if c == "pag":
            pag_pct += p
            mw = float(pr.get("mw_pag") or 0.0)
            if mw:
                pag_mol += p / mw
        elif c == "quencher":
            mw = float(pr.get("mw_base") or 0.0)
            if mw:
                base_mol += p * float(pr.get("basicity") or 1.0) / mw
    return {
        "solid_content": round(solid, 4),
        "pag_pct": round(pag_pct, 4),
        "pag_loading": round(pag_pct / solid * 100.0, 4) if solid > 0 else None,
        "quench_ratio": round(base_mol / pag_mol, 4) if pag_mol > 0 else None,
        "acid_mol_per_100g": round(pag_mol, 6),
    }


# ---------------------------------------------------------------- 原料目录
def load_catalog():
    """{code: {id, name, category, eew, ahew, ...}}"""
    with connect() as c, c.cursor() as cur:
        cur.execute("""
            select id, code, name, category,
                   (props->>'eew')::numeric  as eew,
                   (props->>'ahew')::numeric as ahew,
                   (props->>'nco_pct')::numeric as nco_pct,
                   (props->>'oh_value_mgkoh')::numeric as oh_value,
                   props
            from mat.raw_material
        """)
        rows = cur.fetchall()
    return {r["code"]: dict(r) for r in rows}


def catalog_by_category(catalog, system):
    out = {}
    for code, m in catalog.items():
        if m["category"] in ("binder", "scm", "water", "admixture", "aggregate", "process"):
            continue                                  # 混凝土数据不属于胶粘剂体系
        out.setdefault(m["category"], []).append(code)
    return {k: sorted(v) for k, v in out.items()}


# ---------------------------------------------------------------- 机理计算
def _eq_weights(catalog, items):
    """返回 (epoxy_eq, amine_h_eq)：与 mat.v_mechanistic_feature 定义一致。

    epoxy_eq = Σ_{resin,diluent} pct/EEW ； amine_h_eq = Σ_{hardener} pct/AHEW
    """
    epoxy_eq = 0.0
    amine_h_eq = 0.0
    for it in items:
        m = catalog.get(it["raw_material_code"])
        if not m:
            continue
        pct = float(it["pct"])
        if m["category"] in ("resin", "diluent") and m.get("eew"):
            epoxy_eq += pct / float(m["eew"])
        elif m["category"] == "hardener" and m.get("ahew"):
            amine_h_eq += pct / float(m["ahew"])
    return epoxy_eq, amine_h_eq


def nco_index(catalog, items):
    """PU 体系的 NCO 指数 = (NCO 当量数) / (OH 当量数)。"""
    nco_eq = 0.0
    oh_eq = 0.0
    for it in items:
        m = catalog.get(it["raw_material_code"])
        if not m:
            continue
        pct = float(it["pct"])
        if m["category"] == "isocyanate" and m.get("nco_pct"):
            nco_eq += pct / (42.0 * 100.0 / float(m["nco_pct"]))
        elif m["category"] == "polyol" and m.get("oh_value"):
            oh_eq += pct / (56100.0 / float(m["oh_value"]))
    return (nco_eq / oh_eq) if oh_eq > 0 else None


def mech_features(catalog, items):
    """机理特征（与建模宽表完全同构，避免训练/推理两套口径）。"""
    pct_total = round(sum(float(i["pct"]) for i in items), 4)
    epoxy_eq, amine_h_eq = _eq_weights(catalog, items)
    ee = (amine_h_eq / epoxy_eq) if epoxy_eq > 0 else None
    return {
        "epoxy_eq": round(epoxy_eq, 6), "amine_h_eq": round(amine_h_eq, 6),
        "ee_ratio": None if ee is None else round(ee, 4),
        "ee_deviation": None if ee is None else round(abs(ee - 1.0), 4),
        "pct_total": pct_total,
        "nco_index": (lambda v: None if v is None else round(v, 4))(nco_index(catalog, items)),
    }


# ---------------------------------------------------------------- 可行域校验
def check_feasible(items, system, catalog=None, space=None):
    """返回 violations（空 = 可行）。"""
    catalog = catalog or load_catalog()
    space = space or SYSTEM_SPACE.get(system, {})
    violations = []

    total = sum(float(i["pct"]) for i in items)
    if abs(total - 100.0) > 0.5:
        violations.append(f"质量守恒失败：Σ={total:.3f}（须 100±0.5）")

    by_cat = {}
    for it in items:
        m = catalog.get(it["raw_material_code"])
        if m is None:
            violations.append(f"未知原料 {it['raw_material_code']}")
            continue
        by_cat[m["category"]] = by_cat.get(m["category"], 0.0) + float(it["pct"])

    for cat, pct_sum in by_cat.items():
        lo_hi = space.get(cat)
        if not lo_hi:
            if pct_sum > 0:
                violations.append(f"{system} 体系不允许 {cat} 类组分（实测 {pct_sum:.1f}%）")
            continue
        lo, hi = lo_hi
        if pct_sum < lo - 1e-6 or pct_sum > hi + 1e-6:
            violations.append(f"{cat} 合计 {pct_sum:.1f}% 超出可行域 [{lo}, {hi}]")

    # 只有"真实原料类别"才查必需性；solid_content / pag_loading / quench_ratio 是
    # **导出机理量**（不是某一类原料的占比），必须另行按计算值校验，否则会报
    # "缺少必需组分 solid_content" 这种永远无法满足的伪违规。
    derived = set(DERIVED_KEYS) | {"ee_target", "nco_index"}
    for cat, (lo, _hi) in space.items():
        if cat in derived:
            continue
        if cat not in by_cat and lo > 0:
            violations.append(f"缺少必需组分 {cat}（下限 {lo}%）")

    if system == "photoresist":
        m = photoresist_mech(catalog, items)
        for key in ("solid_content", "pag_loading", "quench_ratio"):
            lo, hi = space.get(key, (None, None))
            if lo is None:
                continue
            v = m.get(key)
            if v is None:
                violations.append(f"光刻胶体系必须能算出 {key}（缺原料分子量/碱度参数）")
            elif not (lo <= v <= hi):
                violations.append(f"{key} {float(v):.3f} 超出机理窗口 [{lo}, {hi}]")

    mech = mech_features(catalog, items)
    if system == "epoxy":
        lo, hi = space.get("ee_target", (0.85, 1.15))
        if mech["ee_ratio"] is None:
            violations.append("环氧体系必须能算出当量比（缺树脂 EEW 或固化剂 AHEW）")
        elif not (lo <= mech["ee_ratio"] <= hi):
            violations.append(f"当量比 {mech['ee_ratio']:.3f} 超出机理窗口 [{lo}, {hi}]")
    if system == "pu":
        lo, hi = space.get("nco_index", (0.95, 1.15))
        if mech["nco_index"] is None:
            violations.append("PU 体系必须能算出 NCO 指数")
        elif not (lo <= mech["nco_index"] <= hi):
            violations.append(f"NCO 指数 {mech['nco_index']:.3f} 超出窗口 [{lo}, {hi}]")
    return violations


# ---------------------------------------------------------------- 生成
def _fixed_point_resin(catalog, resin_block, aux_items, hardener_code, target_ee,
                       resin_bounds, hard_bounds):
    """固定点迭代：调整树脂用量使固化剂用量落在可行域，且当量比 = target_ee。"""
    hm = catalog.get(hardener_code)
    if not hm or not hm.get("ahew"):
        return None
    ahew = float(hm["ahew"])
    aux_total = sum(float(i["pct"]) for i in aux_items)
    lo_r, hi_r = resin_bounds
    lo_h, hi_h = hard_bounds
    resin_shares = {c: float(p) for c, p in resin_block}
    share_sum = sum(resin_shares.values()) or 1.0

    # 初始猜测：树脂取可行域中值
    resin_total = min(max((lo_r + hi_r) / 2.0, lo_r), hi_r)
    for _ in range(12):
        epoxy_eq = 0.0
        for code, sh in resin_shares.items():
            m = catalog.get(code)
            if m and m.get("eew"):
                epoxy_eq += (resin_total * sh / share_sum) / float(m["eew"])
        for it in aux_items:                                  # 稀释剂也贡献环氧当量
            m = catalog.get(it["raw_material_code"])
            if m and m["category"] == "diluent" and m.get("eew"):
                epoxy_eq += float(it["pct"]) / float(m["eew"])
        hard_pct = target_ee * ahew * epoxy_eq
        new_resin = 100.0 - aux_total - hard_pct
        if new_resin <= 0:
            return None
        resin_total = 0.5 * resin_total + 0.5 * new_resin      # 阻尼迭代
        resin_total = min(max(resin_total, lo_r), hi_r)
        if not (lo_h <= hard_pct <= hi_h) or not (lo_r <= resin_total <= hi_r):
            # 夹紧后再验一次；仍超界即判不可行
            hard_pct = min(max(hard_pct, lo_h), hi_h)
            resin_total = 100.0 - aux_total - hard_pct
            if not (lo_r <= resin_total <= hi_r):
                return None
    items = [{"raw_material_code": c, "pct": round(resin_total * sh / share_sum, 3)}
             for c, sh in resin_shares.items()]
    items += [dict(a) for a in aux_items]
    items.append({"raw_material_code": hardener_code, "pct": round(hard_pct, 3)})
    return _trim_to_100(items)


def _trim_to_100(items, tol=0.001):
    """四舍五入误差收敛到 Σ=100（把差值补给占比最大的组分）。"""
    items = [dict(i) for i in items]
    total = sum(float(i["pct"]) for i in items)
    diff = round(100.0 - total, 6)
    if abs(diff) < tol:
        return items
    big = max(items, key=lambda i: float(i["pct"]))
    big["pct"] = round(float(big["pct"]) + diff, 4)
    if float(big["pct"]) < 0:
        return None
    return items


def _sample_epoxy(rng, cat, space, catalog):
    resin_codes = cat.get("resin", [])
    hard_codes = cat.get("hardener", [])
    if not resin_codes or not hard_codes:
        return None
    n_resin = 1 if rng.random() < 0.6 else 2
    chosen = rng.sample(resin_codes, min(n_resin, len(resin_codes)))
    if len(chosen) == 1:
        resin_block = [(chosen[0], 100.0)]
    else:
        s = rng.uniform(0.2, 0.8)
        resin_block = [(chosen[0], s * 100), (chosen[1], (1 - s) * 100)]

    aux = []
    for c in ("toughener", "filler", "diluent", "additive"):
        lo, hi = space.get(c, (0.0, 0.0))
        if hi <= 0 or not cat.get(c):
            continue
        if rng.random() < (0.35 if c in ("diluent", "additive") else 0.6):
            pct = round(rng.uniform(lo, min(hi, lo + (hi - lo) * 0.8)), 3)
            if pct > 0:
                aux.append({"raw_material_code": rng.choice(cat[c]), "pct": pct})
    lo, hi = space.get("ee_target", (0.9, 1.1))
    target_ee = round(rng.uniform(max(0.9, lo), min(1.1, hi)), 3)
    return _fixed_point_resin(catalog, resin_block, aux, rng.choice(hard_codes),
                              target_ee, space["resin"], space["hardener"])


def _sample_pu(rng, cat, space, catalog):
    polyols, isos = cat.get("polyol", []), cat.get("isocyanate", [])
    if not polyols or not isos:
        return None
    lo, hi = space.get("nco_index", (0.95, 1.15))
    index = round(rng.uniform(lo, hi), 3)
    aux = []
    for c in ("filler", "additive"):
        lo2, hi2 = space.get(c, (0.0, 0.0))
        if hi2 <= 0 or not cat.get(c):
            continue
        if rng.random() < 0.5:
            aux.append({"raw_material_code": rng.choice(cat[c]),
                        "pct": round(rng.uniform(lo2, hi2), 3)})
    poly_code, iso_code = rng.choice(polyols), rng.choice(isos)
    pm, im = catalog[poly_code], catalog[iso_code]
    if not (pm.get("oh_value") and im.get("nco_pct")):
        return None
    oh_eq_w = 56100.0 / float(pm["oh_value"])
    nco_eq_w = 42.0 * 100.0 / float(im["nco_pct"])
    aux_total = sum(float(i["pct"]) for i in aux)
    # polyol + iso = 100 - aux；index = (iso/nco_eq_w)/(poly/oh_eq_w)
    # → iso = index * nco_eq_w * poly / oh_eq_w
    poly = (100.0 - aux_total) / (1.0 + index * nco_eq_w / oh_eq_w)
    iso = 100.0 - aux_total - poly
    items = [{"raw_material_code": poly_code, "pct": round(poly, 3)},
             {"raw_material_code": iso_code, "pct": round(iso, 3)}] + aux
    return _trim_to_100(items)


def _sample_acrylic(rng, cat, space, catalog):
    parts = []
    for c in ("toughener", "filler", "additive"):
        lo, hi = space.get(c, (0.0, 0.0))
        if hi <= 0 or not cat.get(c):
            continue
        parts.append({"raw_material_code": rng.choice(cat[c]),
                      "pct": round(rng.uniform(lo, hi), 3)})
    if not parts:
        return None
    total = sum(float(i["pct"]) for i in parts)
    for p in parts:
        p["pct"] = round(float(p["pct"]) / total * 100.0, 3)
    return _trim_to_100(parts)


def _sample_photoresist(rng, cat, space, catalog):
    """光刻胶（化学放大胶）：先在固体分内按机理配平，再用溶剂补足 100%。

    机理约束（与 gen_photoresist.py 的种植真值同口径）：
      - 固体分 = 树脂 + PAG + 淬灭剂 + 添加剂
      - 淬灭剂用量由 **淬灭剂/PAG 摩尔比** 反推（不是独立随机）→ 保证落在化学合理区间
      - 溶剂补足至 100%
    """
    poly_codes = cat.get("polymer") or []
    pag_codes = cat.get("pag") or []
    q_codes = cat.get("quencher") or []
    if not (poly_codes and pag_codes and q_codes):
        return None
    lo_s, hi_s = space.get("solid_content", (18.0, 32.0))
    lo_q, hi_q = space.get("quench_ratio", (0.10, 0.75))
    lo_pag, hi_pag = space.get("pag_loading", (1.5, 6.0))
    lo_add, hi_add = space.get("additive", (0.0, 1.2))

    solid = rng.uniform(lo_s, hi_s)
    pag_rel = rng.uniform(lo_pag, hi_pag)
    qr = rng.uniform(lo_q, hi_q)
    add_pct = rng.uniform(lo_add, hi_add)

    poly_code, pag_code, q_code = rng.choice(poly_codes), rng.choice(pag_codes), rng.choice(q_codes)
    mp, mq = catalog.get(pag_code), catalog.get(q_code)
    mw_pag = float((mp or {}).get("props", {}).get("mw_pag") or 450.0)
    mw_base = float((mq or {}).get("props", {}).get("mw_base") or 150.0)
    basicity = float((mq or {}).get("props", {}).get("basicity") or 1.0)

    pag_pct = solid * pag_rel / 100.0
    q_pct = qr * (pag_pct / mw_pag) * mw_base / basicity
    poly_pct = solid - pag_pct - q_pct - add_pct
    if poly_pct <= 2.0:
        return None

    items = [{"raw_material_code": poly_code, "pct": round(poly_pct, 3)},
             {"raw_material_code": pag_code, "pct": round(pag_pct, 3)},
             {"raw_material_code": q_code, "pct": round(q_pct, 3)}]
    add_codes = cat.get("additive") or []
    if add_codes and add_pct > 0:
        items.append({"raw_material_code": rng.choice(add_codes), "pct": round(add_pct, 3)})
    solv_codes = cat.get("solvent") or []
    if not solv_codes:
        return None
    items.append({"raw_material_code": rng.choice(solv_codes),
                  "pct": round(100.0 - sum(i["pct"] for i in items), 3)})
    return _trim_to_100(items)


SAMPLERS = {"epoxy": _sample_epoxy, "pu": _sample_pu, "acrylic": _sample_acrylic,
            "photoresist": _sample_photoresist}


def sample_candidate(system, rng, catalog=None, space=None):
    catalog = catalog or load_catalog()
    space = space or SYSTEM_SPACE.get(system)
    if not space:
        raise Refusal(f"体系 {system} 未定义可行域，拒答")
    cat = catalog_by_category(catalog, system)
    fn = SAMPLERS.get(system)
    if not fn:
        raise Refusal(f"体系 {system} 暂无配方生成器，拒答")
    return fn(rng, cat, space, catalog)


def mutate_candidate(items, rng, catalog=None, space=None, system="epoxy", scale=0.15):
    """在已知配方附近做局部扰动（BO 的 exploitation 侧）。"""
    catalog = catalog or load_catalog()
    space = space or SYSTEM_SPACE.get(system, {})
    out = []
    for it in items:
        m = catalog.get(it["raw_material_code"])
        lo, hi = space.get(m["category"], (0.0, 100.0)) if m else (0.0, 100.0)
        pct = float(it["pct"]) * (1.0 + rng.uniform(-scale, scale))
        out.append({"raw_material_code": it["raw_material_code"], "pct": round(pct, 3)})
    # 扰动后重新按机理配平：环氧体系重解固化剂，其余直接归一化
    if system == "epoxy":
        hard = [o for o in out if (catalog.get(o["raw_material_code"]) or {}).get("category") == "hardener"]
        if hard:
            mech = mech_features(catalog, out)
            target = mech["ee_ratio"] or 1.0
            hm = catalog[hard[0]["raw_material_code"]]
            resin = [o for o in out if (catalog.get(o["raw_material_code"]) or {}).get("category") in ("resin", "diluent")]
            aux = [o for o in out if o not in hard and o not in resin]
            fixed = _fixed_point_resin(catalog, [(o["raw_material_code"], o["pct"]) for o in resin],
                                       aux, hard[0]["raw_material_code"], target,
                                       space.get("resin", (25, 80)), space.get("hardener", (4, 45)))
            if fixed:
                return fixed
    total = sum(float(i["pct"]) for i in out)
    for o in out:
        o["pct"] = round(float(o["pct"]) / total * 100.0, 3) if total else 0.0
    return _trim_to_100(out)


# ---------------------------------------------------------------- 候选装配
def build_candidate(code, system, items, origin="bo", catalog=None, notes=None,
                    enforce=True):
    """装配候选：可行域 + 质量守恒 + 合规三重校验；任一不满足 → Refusal（拒答）。"""
    catalog = catalog or load_catalog()
    violations = check_feasible(items, system, catalog)
    comp = check_formulation(items)
    if enforce and (violations or comp["decision"] == "reject"):
        raise Refusal("；".join(violations + comp["blocking"]) or "硬约束未满足")
    mech = mech_features(catalog, items)
    return {
        "code": code, "system": system, "origin": origin,
        "items": items, "pct_total": mech["pct_total"], "ee_ratio": mech["ee_ratio"],
        "nco_index": mech["nco_index"],
        "feasibility": {"violations": violations, "space": SYSTEM_SPACE.get(system)},
        "compliance": comp, "mech": mech, "notes": notes,
        "process": dict(PROCESS_DEFAULT.get(system, PROCESS_DEFAULT["epoxy"])),
    }


CAND_INSERT = """
insert into mat.formulation_candidate(code, system, origin, items, feasibility, compliance, notes)
values (%(code)s, %(system)s, %(origin)s, %(items)s::jsonb, %(feasibility)s::jsonb,
        %(compliance)s::jsonb, %(notes)s)
on conflict (code) do update set items = excluded.items
returning id, pct_total, ee_ratio
"""


def save_candidate(cand, cur):
    cur.execute(CAND_INSERT, {
        "code": cand["code"], "system": cand["system"], "origin": cand["origin"],
        "items": json.dumps(cand["items"], ensure_ascii=False),
        "feasibility": json.dumps(cand["feasibility"], ensure_ascii=False),
        "compliance": json.dumps(cand["compliance"], ensure_ascii=False),
        "notes": cand.get("notes"),
    })
    row = cur.fetchone()
    cand["id"] = row["id"]
    cand["db_pct_total"] = row["pct_total"]
    cand["db_ee_ratio"] = row["ee_ratio"]
    return cand


# ---------------------------------------------------------------- 特征对齐
def build_feature_vector(items, feature_cols, system, process=None, catalog=None):
    """把候选配方投影到竞技场训练用的特征空间（必须与 fsdata.build_features 同构）。

    feature_cols 顺序：原料级组成（code 升序）→ 类别占比 → 机理 → 工艺 → 体系 one-hot。
    """
    catalog = catalog or load_catalog()
    process = process or PROCESS_DEFAULT.get(system, PROCESS_DEFAULT["epoxy"])
    # 同时登记裸类别名与 "<类别>_pct" 两种写法：fsdata 的特征列带 _pct，
    # 若不登记，这些列会被判成 comp_col（原料代码）而恒取 0。
    known = (set(CAT_COLS) | {c + "_pct" for c in CAT_COLS}
             | set(MECH_COLS) | set(PROC_COLS))
    sys_cols = [c for c in feature_cols if c.startswith("sys_")]
    comp_cols = [c for c in feature_cols if c not in known and c not in sys_cols]

    comp = {}
    cat = {c: 0.0 for c in CAT_COLS}
    for it in items:
        code, pct = it["raw_material_code"], float(it["pct"])
        comp[code] = comp.get(code, 0.0) + pct
        m = catalog.get(code)
        if m and m["category"] in cat:
            cat[m["category"]] += pct

    epoxy_eq, amine_h_eq = _eq_weights(catalog, items)
    ee = (amine_h_eq / epoxy_eq) if epoxy_eq > 0 else None
    mech = {
        "epoxy_eq": epoxy_eq, "amine_h_eq": amine_h_eq,
        "ee_ratio": 0.0 if ee is None else ee,
        "ee_deviation": 0.0 if ee is None else abs(ee - 1.0),
        "pct_total": sum(float(i["pct"]) for i in items),
    }
    # 光刻胶机理量：必须与 mat.v_mechanistic_feature / fsdata 完全同口径。
    # 漏掉会造成"训练时有、候选时恒为 0"，而这些列在单体系训练集里 std=0，
    # 差 1 就会被标准化放大成 1e9 → 全部候选被判域外（实测踩过）。
    if system == "photoresist":
        pr = photoresist_mech(catalog, items)
        mech["solid_content"] = pr["solid_content"]
        mech["pag_loading_pct"] = pr["pag_loading"] or 0.0
        mech["quench_ratio"] = pr["quench_ratio"] or 0.0
        mech["acid_mol_per_100g"] = pr["acid_mol_per_100g"]
    proc = {
        "cure_temp_c": float(process.get("cure_temp_c") or 0.0),
        "test_temp_c": float(process.get("test_temp_c") or 0.0),
        "post_cure": 1.0 if process.get("post_cure") else 0.0,
        "has_ee": 1.0 if ee is not None else 0.0,
        # 指示位同口径：fsdata 里用 quench_ratio.notna()，这里必须一致
        "has_pr": 1.0 if system == "photoresist" and mech.get("quench_ratio") else 0.0,
    }
    vec = []
    for c in feature_cols:
        if c in comp_cols:
            vec.append(comp.get(c, 0.0))
        elif c in cat:
            vec.append(cat[c])
        elif c.endswith("_pct") and c[:-4] in cat:
            # fsdata 的特征列是 "<类别>_pct"，而这里的 cat 按裸类别名累计 ——
            # 不处理这个后缀，候选侧所有类别占比特征恒为 0，与训练侧不同口径。
            vec.append(cat[c[:-4]])
        elif c in mech:
            vec.append(mech[c])
        elif c in proc:
            vec.append(proc[c])
        elif c.startswith("sys_"):
            vec.append(1.0 if c == f"sys_{system}" else 0.0)
        else:
            vec.append(0.0)
    return np.asarray(vec, dtype=float)


def build_feature_matrix(cands, feature_cols, system, catalog=None):
    return np.vstack([build_feature_vector(c["items"], feature_cols, system,
                                           c.get("process"), catalog) for c in cands])


def rng_for(seed):
    return random.Random(seed)
