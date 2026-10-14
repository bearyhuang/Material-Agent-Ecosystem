#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FormulaAgents · 四级库 mock 种子数据生成器

数据性质（务必如实声明）：
  * 原料/牌号/物性（EEW、AHEW、Hansen 参数、粘度、粒径）取自公开资料与行业通用规格，
    数值落在真实规格区间内，但**不是任何一家公司的实测台账**；
  * 配方与性能是**按机理方程合成的**（planted ground truth），用于验证算法选择机制，
    **不得对外声称胶粘剂真实精度**。

产出：sql/seed_data.sql（30-seed.sql 挂载进 initdb）

用法：python3 scripts/gen_seed.py [--out sql/seed_data.sql] [--seed 20260923]
"""
import argparse, json, math, random
from datetime import date, timedelta

# ----------------------------------------------------------------- SQL 字面量
def s(v):
    return "null" if v is None else "'" + str(v).replace("'", "''") + "'"

def j(o):
    return "null" if o is None else s(json.dumps(o, ensure_ascii=False))

def num(v, nd=4):
    return "null" if v is None else f"{round(float(v), nd):g}"

def boolsql(b):
    return "null" if b is None else ("true" if b else "false")

def vec(v):
    return "null" if v is None else "'[" + ",".join(f"{x:.6f}" for x in v) + "]'::vector"

# ----------------------------------------------------------------- 原料目录
# props 关键字段：eew(环氧当量 g/eq) / ahew(活泼氢当量 g/eq) / hsp(Hansen 三参数) /
#                 visc_25c_mpas / d50_um / oil_abs / water_pct / oh_value / nco_pct
# 物性来源（公开资料，2026-09 检索）：
#   E51  EEW 185–195 g/eq（部分规格 185–210），粘度 11000–15000 mPa·s @25℃
#   E44  EEW 210–240 g/eq；D.E.R.331 EEW 189；D.E.R.337 EEW 240；BGE 活性稀释剂 EEW 130
#   DETA 分子量 103.2 / 活泼氢 5 → AHEW 20.6；TETA AHEW 24.4；IPDA AHEW 42.6；DDS AHEW 62.1
#   Epoxy HSP ≈ [17.4, 10.5, 9]（Abbott, Practical Adhesion）
CAT = {
    "resin": "树脂", "hardener": "固化剂", "toughener": "增韧剂", "filler": "填料",
    "diluent": "稀释剂", "additive": "助剂", "polyol": "多元醇", "isocyanate": "异氰酸酯",
}

RAW = [
    # code, name, cas, category, props
    ("E51", "双酚A型环氧树脂 E51（618）", "25068-38-6", "resin",
     {"eew": 190, "epoxy_value": 0.51, "visc_25c_mpas": 12000, "hsp": [17.4, 10.5, 9],
      "water_pct": 0.08, "functionality": 2.0, "form": "liquid"}),
    ("E44", "双酚A型环氧树脂 E44（6101）", "25068-38-6", "resin",
     {"eew": 225, "epoxy_value": 0.44, "visc_25c_mpas": 13000, "hsp": [17.4, 10.5, 9],
      "water_pct": 0.10, "functionality": 2.0, "form": "liquid"}),
    ("DER331", "双酚A型环氧树脂 D.E.R.331", "25068-38-6", "resin",
     {"eew": 189, "visc_25c_mpas": 11500, "hsp": [17.4, 10.5, 9], "water_pct": 0.05,
      "functionality": 2.0, "form": "liquid"}),
    ("F51", "酚醛环氧树脂 F-51", "61788-97-4", "resin",
     {"eew": 180, "visc_25c_mpas": 30000, "hsp": [17.8, 10.8, 9.2], "water_pct": 0.12,
      "functionality": 2.6, "form": "semi_solid"}),
    ("TGDDM", "四缩水甘油胺型环氧 TGDDM", "28768-32-3", "resin",
     {"eew": 118, "visc_25c_mpas": 2500, "hsp": [17.9, 11.0, 9.5], "water_pct": 0.15,
      "functionality": 4.0, "form": "liquid"}),

    ("DETA", "二乙烯三胺 DETA", "111-40-0", "hardener",
     {"ahew": 20.6, "mw": 103.2, "active_h": 5, "amine_value_mgkoh": 1360, "form": "liquid"}),
    ("TETA", "三乙烯四胺 TETA", "112-24-3", "hardener",
     {"ahew": 24.4, "mw": 146.2, "active_h": 6, "amine_value_mgkoh": 1150, "form": "liquid"}),
    ("IPDA", "异佛尔酮二胺 IPDA", "2855-13-2", "hardener",
     {"ahew": 42.6, "mw": 170.3, "active_h": 4, "amine_value_mgkoh": 660, "form": "liquid"}),
    ("DDS", "4,4'-二氨基二苯砜 DDS", "80-08-0", "hardener",
     {"ahew": 62.1, "mw": 248.3, "active_h": 4, "form": "solid", "cure_temp_min_c": 130}),
    ("PA650", "聚酰胺固化剂 650（胺值 200）", "68410-23-1", "hardener",
     {"ahew": 200, "amine_value_mgkoh": 200, "visc_25c_mpas": 8000, "form": "liquid"}),
    ("MTHPA", "甲基四氢苯酐 MTHPA", "19438-64-3", "hardener",
     {"ahew": 166, "anhydride_equiv": 166, "form": "liquid", "cure_temp_min_c": 100}),
    ("DICY", "双氰胺 DICY（潜伏性）", "461-58-5", "hardener",
     {"ahew": 21, "form": "solid", "cure_temp_min_c": 150, "latent": True}),

    ("CTBN", "端羧基液体丁腈橡胶 CTBN-1300X13", "25265-19-4", "toughener",
     {"hsp": [17.6, 6.5, 4.2], "acrylonitrile_pct": 26, "cooh_equiv": 1900,
      "visc_25c_mpas": 55000, "form": "liquid"}),
    ("ATBN", "端胺基液体丁腈橡胶 ATBN-1300X16", "25265-19-4", "toughener",
     {"hsp": [17.6, 6.8, 4.6], "ahew": 900, "acrylonitrile_pct": 16, "form": "liquid"}),
    ("CSR", "核壳橡胶增韧剂 CSR（粉体）", "9010-94-0", "toughener",
     {"hsp": [17.8, 5.0, 3.5], "d50_um": 0.2, "core_shell": True, "form": "powder"}),

    ("SILICA", "活性硅微粉（硅烷处理）", "60676-86-0", "filler",
     {"d50_um": 5.0, "oil_abs_g100g": 25, "density": 2.65, "form": "powder"}),
    ("CACO3", "重质碳酸钙", "471-34-1", "filler",
     {"d50_um": 10.0, "oil_abs_g100g": 18, "density": 2.71, "form": "powder"}),
    ("ALOH3", "氢氧化铝（阻燃填料）", "21645-51-2", "filler",
     {"d50_um": 15.0, "oil_abs_g100g": 22, "density": 2.42, "form": "powder"}),
    ("AL2O3", "氧化铝（导热填料）", "1344-28-1", "filler",
     {"d50_um": 8.0, "oil_abs_g100g": 20, "density": 3.95, "form": "powder"}),
    ("FUMED", "气相二氧化硅（触变剂）", "112945-52-5", "filler",
     {"d50_um": 0.012, "surface_area_m2g": 200, "form": "powder", "thixotrope": True}),

    ("BGE", "丁基缩水甘油醚（活性稀释剂）", "2426-08-6", "diluent",
     {"eew": 130, "visc_25c_mpas": 3, "form": "liquid"}),
    ("AGE", "C12-14 烷基缩水甘油醚", "68609-97-2", "diluent",
     {"eew": 290, "visc_25c_mpas": 8, "form": "liquid"}),

    ("KH560", "硅烷偶联剂 KH-560", "2530-83-8", "additive", {"form": "liquid", "coupling": True}),
    ("DMP30", "促进剂 DMP-30", "90-72-2", "additive", {"form": "liquid", "accelerator": True}),
    ("DEFOAM", "有机硅消泡剂", "63148-62-9", "additive", {"form": "liquid"}),
    ("CB", "炭黑颜料", "1333-86-4", "additive", {"d50_um": 0.05, "form": "powder"}),

    ("PPG2000", "聚醚多元醇 PPG-2000", "25322-69-4", "polyol",
     {"oh_value_mgkoh": 56, "mw": 2000, "hsp": [16.5, 9.0, 7.0], "form": "liquid"}),
    ("MDI", "二苯基甲烷二异氰酸酯 MDI", "101-68-8", "isocyanate",
     {"nco_pct": 33.6, "hsp": [19.0, 8.5, 8.0], "form": "liquid"}),
]
RAW_IDX = {c: i + 1 for i, (c, *_ ) in enumerate(RAW)}
RAW_BY_CODE = {r[0]: r for r in RAW}

# ----------------------------------------------------------------- 中间体（BOM）
# (code, name, process, [(kind, code, pct)])  kind: raw / sub
INTERMEDIATES = [
    ("IM-CTBN-MB", "CTBN 增韧母料（CTBN 40%）", "预反应加成：130℃/2h 氮气保护",
     [("raw", "CTBN", 40), ("raw", "E51", 60)]),
    ("IM-FILL-MB", "填料母料（硅微粉 50%）", "三辊研磨分散至细度≤30μm",
     [("raw", "SILICA", 50), ("raw", "E51", 50)]),
    ("IM-TOUGH-FILL-MB", "增韧填充复合母料（二级）", "母料复配：两级 BOM 验证血缘递归",
     [("sub", "IM-CTBN-MB", 65), ("sub", "IM-FILL-MB", 35)]),
    ("IM-AMINE-ADDUCT", "胺加成物固化剂（DETA/E51）", "胺过量加成：60℃/1h，降低胺析出发白",
     [("raw", "DETA", 25), ("raw", "E51", 75)]),
    ("IM-PU-PREPOLY", "PU 预聚体（NCO≈8%）", "80℃/3h 预聚，MDI 与 PPG-2000 反应",
     [("raw", "PPG2000", 70), ("raw", "MDI", 30)]),
]

# ----------------------------------------------------------------- 旁挂维度
SUPPLIERS = [
    ("SUP-BALING", "巴陵石化环氧树脂事业部", "CN", "A", {"scope": "液态双酚A环氧 E51/E44"}),
    ("SUP-HONGCHANG", "宏昌电子材料", "CN", "A", {"scope": "电子级环氧、酚醛环氧"}),
    ("SUP-HUNTSMAN", "Huntsman Advanced Materials", "US", "A", {"scope": "胺类固化剂、增韧剂"}),
    ("SUP-EVONIK", "Evonik Industries", "DE", "A", {"scope": "气相二氧化硅、硅烷偶联剂"}),
    ("SUP-CABOT", "Cabot Corporation", "US", "B", {"scope": "炭黑、导热填料"}),
    ("SUP-LOCAL", "华东地区填料代工厂", "CN", "C", {"scope": "碳酸钙、氢氧化铝"}),
]
SUP_IDX = {c: i + 1 for i, (c, *_) in enumerate(SUPPLIERS)}
RAW_SUPPLIER = {  # 原料 → 供应商
    "E51": "SUP-BALING", "E44": "SUP-BALING", "DER331": "SUP-HUNTSMAN",
    "F51": "SUP-HONGCHANG", "TGDDM": "SUP-HUNTSMAN",
    "DETA": "SUP-HUNTSMAN", "TETA": "SUP-HUNTSMAN", "IPDA": "SUP-EVONIK",
    "DDS": "SUP-HUNTSMAN", "PA650": "SUP-HUNTSMAN", "MTHPA": "SUP-HUNTSMAN",
    "DICY": "SUP-EVONIK",
    "CTBN": "SUP-HUNTSMAN", "ATBN": "SUP-HUNTSMAN", "CSR": "SUP-EVONIK",
    "SILICA": "SUP-LOCAL", "CACO3": "SUP-LOCAL", "ALOH3": "SUP-LOCAL",
    "AL2O3": "SUP-LOCAL", "FUMED": "SUP-EVONIK",
    "BGE": "SUP-HONGCHANG", "AGE": "SUP-HONGCHANG",
    "KH560": "SUP-EVONIK", "DMP30": "SUP-HUNTSMAN", "DEFOAM": "SUP-EVONIK",
    "CB": "SUP-CABOT", "PPG2000": "SUP-HUNTSMAN", "MDI": "SUP-HUNTSMAN",
}

APPLICATIONS = [
    ("APP-EV", "新能源汽车", "铝合金/碳纤维复材", 120,
     {"lap_shear_min_mpa": 25, "tg_min_c": 110, "impact_required": True}),
    ("APP-WIND", "风电叶片", "玻纤/环氧层压板", 80,
     {"lap_shear_min_mpa": 22, "tg_min_c": 85, "fatigue_required": True}),
    ("APP-ELEC", "电子封装", "铜/PCB FR-4", 150,
     {"lap_shear_min_mpa": 15, "tg_min_c": 140, "low_stress": True}),
    ("APP-CIVIL", "建筑加固", "混凝土/钢", 60,
     {"lap_shear_min_mpa": 18, "tg_min_c": 65, "thixotropy_required": True}),
    ("APP-AERO", "航空复材修补", "铝/铝、CFRP", 130,
     {"lap_shear_min_mpa": 30, "tg_min_c": 120, "hot_wet_required": True}),
]
APP_IDX = {c: i + 1 for i, (c, *_) in enumerate(APPLICATIONS)}

PROJECTS = [
    ("PRJ-2601", "电池包结构胶开发", "宁德时代（示例）", date(2024, 1, 15), "APP-EV"),
    ("PRJ-2602", "风电叶片主梁结构胶", "某整机厂（示例）", date(2024, 3, 4), "APP-WIND"),
    ("PRJ-2603", "低应力电子封装胶", "某封测厂（示例）", date(2024, 6, 10), "APP-ELEC"),
    ("PRJ-2604", "建筑植筋加固胶", "某加固工程（示例）", date(2024, 9, 2), "APP-CIVIL"),
    ("PRJ-2605", "航空复材快速修补胶", "某 MRO（示例）", date(2025, 2, 17), "APP-AERO"),
]
PRJ_IDX = {c: i + 1 for i, (c, *_) in enumerate(PROJECTS)}
PRJ_APP = {c: a for (c, _, _, _, a) in PROJECTS}

# ----------------------------------------------------------------- 配方族（时间聚集，供 Red-team 做 group split）
FAMILIES = [
    # code, 描述, 引入区间(配索引起止), 体系
    # 区间彼此重叠、且随时间推进 → 天然形成"配方族 × 时间"聚集，Red-team 的 group-split 才有的放矢
    ("F1-BPA-AMINE", "双酚A环氧/脂肪胺通用体系", (0, 22), "epoxy"),
    ("F2-TOUGH-HT", "CTBN 增韧耐高温体系", (13, 41), "epoxy"),
    ("F3-FILLED-LOWCOST", "高填充低成本体系", (32, 60), "epoxy"),
    ("F4-LATENT-1K", "潜伏性单组分体系", (50, 71), "epoxy"),
    ("F5-PU-MOD", "PU 改性环氧体系", (65, 86), "epoxy"),
]


def hsp_distance(a, b):
    return math.sqrt(4 * (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def expand(items, depth=0):
    """把配方项（原料或中间体）递归展开到原料级，返回 {code: pct}（与 mat.v_bom_explode 同构）"""
    out = {}
    for kind, code, pct in items:
        if kind == "raw":
            out[code] = out.get(code, 0.0) + pct
        else:
            sub = next(x for x in INTERMEDIATES if x[0] == code)
            if depth >= 5:
                continue
            for c, p in expand(sub[3], depth + 1).items():
                out[c] = out.get(c, 0.0) + p * pct / 100.0
    return out


def mech_features(comp):
    """与 mat.v_mechanistic_feature 完全一致的机理特征计算"""
    epoxy_eq = amine_h_eq = 0.0
    resin = hard = tough = fill = dil = add = poly = iso = 0.0
    for code, pct in comp.items():
        _, _, _, cat, props = RAW_BY_CODE[code]
        if cat in ("resin", "diluent") and props.get("eew"):
            epoxy_eq += pct / props["eew"]
        if cat == "hardener" and props.get("ahew"):
            amine_h_eq += pct / props["ahew"]
        {"resin": lambda: None}
        if cat == "resin":      resin += pct
        if cat == "hardener":   hard += pct
        if cat == "toughener":  tough += pct
        if cat == "filler":     fill += pct
        if cat == "diluent":    dil += pct
        if cat == "additive":   add += pct
        if cat == "polyol":     poly += pct
        if cat == "isocyanate": iso += pct
    r = amine_h_eq / epoxy_eq if epoxy_eq > 0 else 0.0
    return dict(epoxy_eq=epoxy_eq, amine_h_eq=amine_h_eq, ee_ratio=r, resin_pct=resin,
                hardener_pct=hard, toughener_pct=tough, filler_pct=fill, diluent_pct=dil,
                additive_pct=add, polyol_pct=poly, isocyanate_pct=iso)


def embedding(comp, dim=32):
    v = [0.0] * dim
    for code, pct in comp.items():
        v[RAW_IDX[code] % dim] += pct
    nrm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / nrm for x in v]


def weighted_hsp(comp, cats):
    num_ = [0.0, 0.0, 0.0]
    tot = 0.0
    for code, pct in comp.items():
        _, _, _, cat, props = RAW_BY_CODE[code]
        if cat in cats and props.get("hsp"):
            for i in range(3):
                num_[i] += props["hsp"][i] * pct
            tot += pct
    return [x / tot for x in num_] if tot else None


# ----------------------------------------------------------------- 性能机理性成（planted ground truth）
def lap_shear(system, comp, m, fmeta, rng):
    """按机理方程合成剪切强度（MPa）。不是实测值，仅用于验证算法选择机制。
    项：当量比高斯峰 / 增韧剂量（含饱和与过量惩罚）/ 填料（先增后降）/ 稀释剂 /
        固化温度 / 增韧剂×温度交互（非线性）/ HSP 相分离 / 放大损失 / 时间漂移 / 噪声
    """
    r = m["ee_ratio"]
    tough, fill, dil = m["toughener_pct"], m["filler_pct"], m["diluent_pct"]
    cure_T = fmeta["cure_temp_c"]
    post = fmeta["post_cure"]
    t = fmeta["t"]

    if system == "epoxy":
        base = 12.0
        term_ratio = 20.0 * math.exp(-((r - 1.0) / 0.25) ** 2)
        term_tough = 10.0 * (1 - math.exp(-tough / 7.0)) - 0.35 * max(0.0, tough - 16)
        term_fill = 5.5 * (1 - math.exp(-fill / 14.0)) - 0.09 * max(0.0, fill - 35)
        term_dil = -0.30 * dil
        term_cure = 0.10 * (cure_T - 110) * (1.0 if post else 0.35)
        term_inter = 0.055 * tough * (cure_T - 110) / 30.0          # 非线性交互项
        hsp_r = weighted_hsp(comp, ("resin",))
        hsp_t = weighted_hsp(comp, ("toughener",))
        term_hsp = 0.0
        if hsp_r and hsp_t:
            ra = hsp_distance(hsp_r, hsp_t)
            term_hsp = -0.5 * abs(ra - 7.5) / 7.5                    # 适度相分离最优
        term_stage = {"lab": 0.0, "pilot": -1.1, "prod": -1.9}[fmeta["stage"]]
        term_t = 2.0 * t
        noise = rng.gauss(0, 1.3)
        y = (base + term_ratio + term_tough + term_fill + term_dil + term_cure
             + term_inter + term_hsp + term_stage + term_t + noise)
        return max(3.5, min(48.0, y))

    if system == "pu":
        idx = m["isocyanate_pct"] / max(m["polyol_pct"], 1e-6) * 3.0     # 近似 NCO/OH 指数
        base = 9.0
        term_idx = 14.0 * math.exp(-((idx - 1.05) / 0.30) ** 2)
        term_fill = 4.0 * (1 - math.exp(-fill / 12.0)) - 0.07 * max(0.0, fill - 30)
        term_cure = 0.06 * (cure_T - 80) * (1.0 if post else 0.5)
        term_stage = {"lab": 0.0, "pilot": -0.8, "prod": -1.4}[fmeta["stage"]]
        y = base + term_idx + term_fill + term_cure + term_stage + 1.2 * t + rng.gauss(0, 1.1)
        return max(2.0, min(32.0, y))

    # acrylic（第二代丙烯酸酯 SGA）：机理更弱，主要由增韧与基材决定
    y = 14.0 + 8.0 * math.exp(-((m["toughener_pct"] - 12) / 9.0) ** 2) \
        + 2.0 * (1 - math.exp(-fill / 15.0)) + 1.0 * t + rng.gauss(0, 1.5)
    return max(3.0, min(30.0, y))


def tg_c(comp, m, fmeta, rng):
    """玻璃化转变温度（℃）：随当量比接近 1、后固化、树脂官能度升高而升高"""
    base = 62.0
    term_ratio = 42.0 * math.exp(-((m["ee_ratio"] - 1.0) / 0.30) ** 2)
    term_tgddm = 0.35 * comp.get("TGDDM", 0.0) + 0.22 * comp.get("F51", 0.0)
    term_dil = -0.55 * m["diluent_pct"]
    term_filler = 0.12 * m["filler_pct"]
    term_post = 12.0 if fmeta["post_cure"] else 0.0
    y = base + term_ratio + term_tgddm + term_dil + term_filler + term_post + rng.gauss(0, 3.0)
    return max(35.0, min(210.0, y))


def viscosity(comp, m, rng):
    """25℃ 混合粘度（mPa·s）：填料与气相二氧化硅显著增稠，稀释剂降粘"""
    base = 9000.0 * (1 + 0.06 * m["filler_pct"])
    base *= 1 + 0.55 * comp.get("FUMED", 0.0)
    base /= 1 + 0.035 * m["diluent_pct"]
    base *= 1 + 0.9 * comp.get("CTBN", 0.0) / 100.0
    return max(300.0, base * (1 + rng.gauss(0, 0.12)))


def peel_nmm(comp, m, rng):
    """剥离强度（N/mm）：对增韧剂最敏感（这正是 Red-team 常说的“剪切高≠剥离高”）"""
    y = 1.2 + 4.6 * (1 - math.exp(-m["toughener_pct"] / 6.0)) - 0.02 * m["filler_pct"]
    return max(0.1, y * (1 + rng.gauss(0, 0.10)))


# ----------------------------------------------------------------- 配方生成
def build_formulation(i, n_total, rng, family, system):
    """生成一个配方：items = [(kind, code, pct), ...]，pct 归一到 100"""
    fm = {"stage": "lab", "cure_temp_c": 120, "post_cure": True, "t": i / max(n_total - 1, 1)}
    items = []

    if system == "epoxy":
        # 树脂主体（可含中间体母料）
        resin_pool = ["E51", "E44", "DER331"]
        resin_code = rng.choice(resin_pool)
        resin_pct = rng.uniform(38, 58)
        items.append(("raw", resin_code, resin_pct))
        if family == "F2-TOUGH-HT":
            resin_code = rng.choice(["TGDDM", "F51", "E51"])
            items = [("raw", resin_code, rng.uniform(34, 50))]
            items.append(("sub", "IM-CTBN-MB", rng.uniform(8, 22)))
        elif family == "F3-FILLED-LOWCOST":
            items.append(("raw", rng.choice(["CACO3", "ALOH3"]), rng.uniform(12, 26)))
            items.append(("sub", "IM-FILL-MB", rng.uniform(6, 18)))
        elif family == "F4-LATENT-1K":
            items.append(("sub", "IM-TOUGH-FILL-MB", rng.uniform(10, 24)))
        elif family == "F5-PU-MOD":
            items.append(("sub", "IM-PU-PREPOLY", rng.uniform(5, 18)))
        else:  # F1
            if rng.random() < 0.45:
                items.append(("sub", "IM-CTBN-MB", rng.uniform(4, 14)))

        # 填料 / 助剂
        if rng.random() < 0.75:
            items.append(("raw", rng.choice(["SILICA", "CACO3", "AL2O3", "ALOH3"]), rng.uniform(4, 26)))
        if rng.random() < 0.35:
            items.append(("raw", "FUMED", rng.uniform(0.5, 3.0)))
        if rng.random() < 0.40:
            items.append(("raw", rng.choice(["BGE", "AGE"]), rng.uniform(1, 9)))
        if rng.random() < 0.55:
            items.append(("raw", "KH560", rng.uniform(0.3, 1.6)))
        if rng.random() < 0.18:
            items.append(("raw", "CB", rng.uniform(0.1, 0.8)))

        comp0 = expand(items)
        m0 = mech_features(comp0)

        # 固化剂：按“目标当量比”反解用量（业务上就是老师傅算 phr）
        target_r = rng.gauss(1.0, 0.10)
        if family == "F4-LATENT-1K":
            hard_code = "DICY"
        elif family == "F2-TOUGH-HT":
            hard_code = rng.choice(["DDS", "IPDA", "MTHPA"])
        elif family == "F3-FILLED-LOWCOST":
            hard_code = rng.choice(["PA650", "TETA"])
        else:
            hard_code = rng.choice(["DETA", "TETA", "PA650", "IPDA"])
        ahew = RAW_BY_CODE[hard_code][4]["ahew"]
        hard_pct = target_r * m0["epoxy_eq"] * ahew
        hard_pct = min(max(hard_pct, 1.5), 40.0)
        items.append(("raw", hard_code, hard_pct))
        if hard_code in ("DICY", "MTHPA") and rng.random() < 0.7:
            items.append(("raw", "DMP30", rng.uniform(0.3, 1.5)))

        fm["cure_temp_c"] = {
            "DDS": rng.uniform(150, 180), "DICY": rng.uniform(150, 175),
            "MTHPA": rng.uniform(120, 150), "PA650": rng.uniform(25, 80),
        }.get(hard_code, rng.uniform(25, 120))
        fm["post_cure"] = fm["cure_temp_c"] > 100 or rng.random() < 0.35

    elif system == "pu":
        items.append(("sub", "IM-PU-PREPOLY", rng.uniform(45, 70)))
        items.append(("raw", "PPG2000", rng.uniform(8, 25)))
        if rng.random() < 0.6:
            items.append(("raw", rng.choice(["CACO3", "SILICA"]), rng.uniform(5, 22)))
        if rng.random() < 0.4:
            items.append(("raw", "KH560", rng.uniform(0.3, 1.2)))
        comp0 = expand(items)
        m0 = mech_features(comp0)
        items.append(("raw", "MDI", max(2.0, min(30.0, m0["polyol_pct"] * rng.uniform(0.30, 0.55)))))
        fm["cure_temp_c"] = rng.uniform(60, 110)
        fm["post_cure"] = rng.random() < 0.5

    else:  # acrylic
        items.append(("raw", "CTBN", rng.uniform(6, 20)))
        items.append(("raw", rng.choice(["CACO3", "SILICA"]), rng.uniform(4, 18)))
        items.append(("raw", "DMP30", rng.uniform(0.2, 1.0)))
        items.append(("raw", "KH560", rng.uniform(0.2, 1.0)))
        fm["cure_temp_c"] = rng.uniform(23, 80)
        fm["post_cure"] = False

    # 归一到 100
    tot = sum(p for _, _, p in items)
    items = [(k, c, round(p * 100.0 / tot, 3)) for k, c, p in items]
    # 修正舍入误差
    diff = round(100.0 - sum(p for _, _, p in items), 3)
    if items:
        k, c, p = items[0]
        items[0] = (k, c, round(p + diff, 3))

    fm["stage"] = rng.choices(["lab", "pilot", "prod"], weights=[0.62, 0.25, 0.13])[0]
    fm["hard_code"] = locals().get("hard_code")
    return items, fm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="sql/seed_data.sql")
    ap.add_argument("--seed", type=int, default=20260923)
    ap.add_argument("--no-vector", action="store_true", help="pgvector 不可用时跳过 comp_embedding")
    a = ap.parse_args()
    global _HAS_VECTOR
    _HAS_VECTOR = not a.no_vector
    rng = random.Random(a.seed)

    L = []
    w = L.append
    w("-- ============================================================================")
    w("-- FormulaAgents · 四级库种子数据（30-seed.sql）")
    w(f"-- 生成：scripts/gen_seed.py  seed={a.seed}")
    w("-- ⚠️ 声明：物性取自公开规格区间，配方/性能为**机理合成数据**（非实测台账），")
    w("--        仅用于验证算法选择机制，**不得对外声称胶粘剂真实精度**。")
    w("-- ============================================================================")
    w("begin;")

    # ---------------- 供应商
    w("\n-- 供应商")
    for code, name, country, tier, props in SUPPLIERS:
        w(f"insert into mat.supplier(code,name,country,tier,props) values("
          f"{s(code)},{s(name)},{s(country)},{s(tier)},{j(props)}) on conflict (code) do nothing;")

    # ---------------- 客户应用
    w("\n-- 客户应用（旁挂维度）")
    for code, industry, substrate, temp, req in APPLICATIONS:
        w(f"insert into mat.customer_application(code,industry,substrate,service_temp_c,requirement)"
          f" values({s(code)},{s(industry)},{s(substrate)},{num(temp,1)},{j(req)})"
          f" on conflict (code) do nothing;")

    # ---------------- 项目
    w("\n-- 研发项目")
    for code, name, cust, started, app in PROJECTS:
        w(f"insert into mat.project(code,name,customer,started_on) values("
          f"{s(code)},{s(name)},{s(cust)},{s(started.isoformat())}) on conflict (code) do nothing;")

    # ---------------- 原料
    w("\n-- 原料（四级库第一级）")
    for code, name, cas, cat, props in RAW:
        w(f"insert into mat.raw_material(code,name,cas,category,props,supplier_id) values("
          f"{s(code)},{s(name)},{s(cas)},{s(cat)},{j(props)},"
          f"{num(SUP_IDX.get(RAW_SUPPLIER.get(code)), 0)}) on conflict (code) do nothing;")

    # ---------------- 原料批次（实测物性漂移）
    w("\n-- 原料批次（实测物性漂移，批次归因的数据基础）")
    for code, name, cas, cat, props in RAW:
        if cat not in ("resin", "hardener"):
            continue
        for k in range(2):
            lot = f"L{rng.randint(2401, 2612)}-{k+1:02d}"
            meas = {}
            if props.get("eew"):
                meas["eew"] = round(props["eew"] * (1 + rng.gauss(0, 0.015)), 1)
            if props.get("ahew"):
                meas["ahew"] = round(props["ahew"] * (1 + rng.gauss(0, 0.02)), 2)
            meas["water_pct"] = round(abs(rng.gauss(0.09, 0.04)), 3)
            day = date(2024, 1, 1) + timedelta(days=rng.randint(0, 700))
            w(f"insert into mat.material_lot(raw_material_id,lot_no,produced_at,props) select id,{s(lot)},"
              f"{s(day.isoformat())},{j(meas)} from mat.raw_material where code={s(code)}"
              f" on conflict do nothing;")

    # ---------------- 中间体
    w("\n-- 中间体（四级库第二级）")
    for code, name, proc, _ in INTERMEDIATES:
        w(f"insert into mat.intermediate(code,name,process) values({s(code)},{s(name)},{s(proc)})"
          f" on conflict (code) do nothing;")

    w("\n-- 中间体投入（中间体可由原料或其他中间体合成 → 递归展开）")
    for code, *_rest, inputs in INTERMEDIATES:
        for kind, c, pct in inputs:
            if kind == "raw":
                w(f"insert into mat.intermediate_input(intermediate_id,raw_material_id,mass_pct)"
                  f" select i.id,r.id,{num(pct,3)} from mat.intermediate i, mat.raw_material r"
                  f" where i.code={s(code)} and r.code={s(c)} on conflict do nothing;")
            else:
                w(f"insert into mat.intermediate_input(intermediate_id,sub_intermediate_id,mass_pct)"
                  f" select i.id,s.id,{num(pct,3)} from mat.intermediate i, mat.intermediate s"
                  f" where i.code={s(code)} and s.code={s(c)} on conflict do nothing;")

    # ---------------- 配方
    # 环氧 87 条 —— 与演示剧本 "数据集 n=87；体系=环氧" 对齐
    N_EPOXY, N_PU, N_ACRYLIC = 87, 14, 10
    N = N_EPOXY + N_PU + N_ACRYLIC
    base_dt = date(2024, 1, 15)
    created, perf_rows = [], []
    fam_of = {}
    for i in range(N):
        # 体系
        if i < N_EPOXY:
            system = "epoxy"
        elif i < N_EPOXY + N_PU:
            system = "pu"
        else:
            system = "acrylic"

        # 配方族（时间聚集）
        cands = [f for f in FAMILIES if f[2][0] <= i <= f[2][1]] if system == "epoxy" else []
        if cands:
            family = rng.choice(cands)[0]
        else:
            family = f"F0-{system.upper()}-GEN"
        fam_of[i] = family

        # 项目 / 应用
        if i < N_EPOXY:
            prj_code = PROJECTS[min(i // 18, len(PROJECTS) - 1)][0]
        elif system == "pu":
            prj_code = "PRJ-2604"
        else:
            prj_code = "PRJ-2603"
        app_code = PRJ_APP[prj_code]

        # 时间：递增，平均 6 天一条 → 约 2 年跨度，构成时间序
        created.append(base_dt + timedelta(days=int(i * 6 + rng.gauss(0, 2))))

        items, fm = build_formulation(i, N, rng, family, system)
        comp = expand(items)
        m = mech_features(comp)

        # 失败注入（约 12%）：配料失误 / 过量填充 → 负例是一等公民数据
        anomaly = None
        if rng.random() < 0.12:
            anomaly = rng.choice(["stoich_error", "over_fill", "under_cure"])
            if anomaly == "stoich_error":
                for k, (kind, c, p) in enumerate(items):
                    if kind == "raw" and RAW_BY_CODE[c][3] == "hardener":
                        items[k] = (kind, c, round(p * rng.choice([0.55, 1.75]), 3))
                tot = sum(x[2] for x in items)
                items = [(k2, c2, round(p2 * 100.0 / tot, 3)) for k2, c2, p2 in items]
                comp = expand(items); m = mech_features(comp)
            elif anomaly == "over_fill":
                items.append(("raw", rng.choice(["CACO3", "ALOH3"]), rng.uniform(18, 30)))
                tot = sum(x[2] for x in items)
                items = [(k2, c2, round(p2 * 100.0 / tot, 3)) for k2, c2, p2 in items]
                comp = expand(items); m = mech_features(comp)
            else:
                fm["cure_temp_c"] = min(fm["cure_temp_c"], 60)
                fm["post_cure"] = False

        fm["t"] = i / (N - 1)
        fm["anomaly"] = anomaly
        fm["family"] = family
        fm["system"] = system
        fm["prj_code"] = prj_code
        fm["app_code"] = app_code
        fm["created"] = created[-1]
        fm["comp"] = comp
        fm["m"] = m
        fm["items"] = items
        fm["emb"] = embedding(comp)
        perf_rows.append(fm)

    # 写入配方
    w("\n-- 配方（四级库第三级）")
    for i, fm in enumerate(perf_rows):
        code = f"FS-{fm['system'][:2].upper()}-{i+1:04d}"
        fm["code"] = code
        target = round(APPLICATIONS[APP_IDX[fm["app_code"]] - 1][4]["lap_shear_min_mpa"], 1)
        fm["target"] = target
        stage = fm["stage"]
        w(f"insert into mat.formulation(code,version,system,stage,project_id,application_id,"
          f"family,target_lap_shear_mpa,created_at,comp_embedding) values("
          f"{s(code)},1,{s(fm['system'])},{s(stage)},"
          f"(select id from mat.project where code={s(fm['prj_code'])}),"
          f"(select id from mat.customer_application where code={s(fm['app_code'])}),"
          f"{s(fm['family'])},{num(target,1)},"
          f"{s(fm['created'].isoformat() + ' 09:00:00+08')},"
          f"{vec(fm['emb']) if _HAS_VECTOR else 'null'}) on conflict do nothing;")

    # 配方项
    w("\n-- 配方组成项（可直投原料，也可投中间体 → 需递归展开）")
    for fm in perf_rows:
        for kind, c, pct in fm["items"]:
            if kind == "raw":
                w(f"insert into mat.formulation_item(formulation_id,raw_material_id,mass_pct)"
                  f" select f.id,r.id,{num(pct,3)} from mat.formulation f, mat.raw_material r"
                  f" where f.code={s(fm['code'])} and r.code={s(c)} on conflict do nothing;")
            else:
                w(f"insert into mat.formulation_item(formulation_id,intermediate_id,mass_pct)"
                  f" select f.id,i.id,{num(pct,3)} from mat.formulation f, mat.intermediate i"
                  f" where f.code={s(fm['code'])} and i.code={s(c)} on conflict do nothing;")

    # 性能
    w("\n-- 性能（四级库第四级；verdict∈hit/partial/miss，失败是一等公民数据）")
    for i, fm in enumerate(perf_rows):
        tested = fm["created"] + timedelta(days=int(rng.uniform(8, 30)))
        cond = {
            "cure_temp_c": round(fm["cure_temp_c"], 1),
            "post_cure": bool(fm["post_cure"]),
            "test_temp_c": 23,
            "substrate": APPLICATIONS[APP_IDX[fm["app_code"]] - 1][2],
            "test_speed_mm_min": 5,
            "batch_anomaly": fm["anomaly"],
        }
        shear = lap_shear(fm["system"], fm["comp"], fm["m"], fm, rng)
        verdict = "hit" if shear >= fm["target"] else ("partial" if shear >= 0.85 * fm["target"] else "miss")
        fm["shear"] = shear
        w(f"insert into mat.performance(formulation_id,test_item,value,unit,condition,method_std,"
          f"tested_at,verdict) select f.id,'lap_shear_strength',{num(shear,3)},'MPa',{j(cond)},"
          f"'GB/T 7124-2008',{s(tested.isoformat() + ' 15:00:00+08')},{s(verdict)}"
          f" from mat.formulation f where f.code={s(fm['code'])};")

    # 附加性能（部分配方）：Tg / 粘度 / 剥离强度
    tg_set = rng.sample(range(N), 60)
    vis_set = rng.sample(range(N), 45)
    peel_set = rng.sample(range(N), 36)
    for i in sorted(tg_set):
        fm = perf_rows[i]
        if fm["system"] != "epoxy":
            continue
        tested = fm["created"] + timedelta(days=int(rng.uniform(10, 40)))
        w(f"insert into mat.performance(formulation_id,test_item,value,unit,condition,method_std,tested_at,verdict)"
          f" select f.id,'glass_transition_tg',{num(tg_c(fm['comp'], fm['m'], fm, rng),2)},'℃',"
          f"{j({'method':'DSC','heating_rate_k_min':10,'batch_anomaly':fm['anomaly']})},"
          f"'GB/T 19466.2-2004',{s(tested.isoformat() + ' 11:00:00+08')},null"
          f" from mat.formulation f where f.code={s(fm['code'])};")
    for i in sorted(vis_set):
        fm = perf_rows[i]
        tested = fm["created"] + timedelta(days=int(rng.uniform(5, 20)))
        w(f"insert into mat.performance(formulation_id,test_item,value,unit,condition,method_std,tested_at,verdict)"
          f" select f.id,'mixed_viscosity_25c',{num(viscosity(fm['comp'], fm['m'], rng),0)},'mPa·s',"
          f"{j({'temp_c':25,'spindle':'RV-07','rpm':20})},'GB/T 22314-2008',"
          f"{s(tested.isoformat() + ' 10:00:00+08')},null from mat.formulation f where f.code={s(fm['code'])};")
    for i in sorted(peel_set):
        fm = perf_rows[i]
        tested = fm["created"] + timedelta(days=int(rng.uniform(10, 35)))
        w(f"insert into mat.performance(formulation_id,test_item,value,unit,condition,method_std,tested_at,verdict)"
          f" select f.id,'peel_strength',{num(peel_nmm(fm['comp'], fm['m'], rng),3)},'N/mm',"
          f"{j({'substrate':'Al/Al','test_speed_mm_min':100,'batch_anomaly':fm['anomaly']})},"
          f"'GB/T 2790-1995',{s(tested.isoformat() + ' 14:00:00+08')},null"
          f" from mat.formulation f where f.code={s(fm['code'])};")

    # ---------------- evo：种子 Gene（策略资产，不含任何“成绩”）
    w("\n-- evo.gene：5 个算法族种子策略（只有策略，不含分数；分数只能由评测门写入）")
    GENES = [
        ("gene_ridge_small_n", "ridge", {"n_min": 20, "n_max": 100000, "needs_uq": False},
         {"estimator": "Ridge", "space": {"alpha": [0.01, 0.03, 0.1, 0.3, 1, 3, 10, 30, 100, 300]},
          "features": ["composition", "mechanistic"]},
         ["random_split", "tune_on_golden", "extrapolate_outside_family"]),
        ("gene_pls_collinear", "pls", {"n_min": 20, "n_max": 100000, "needs_uq": False,
                                       "signals": ["high_collinearity", "composition_sum_100"]},
         {"estimator": "PLSRegression", "space": {"n_components": [1, 2, 3, 4, 5, 6, 7]},
          "features": ["composition", "mechanistic"]},
         ["random_split", "tune_on_golden"]),
        ("gene_rf_robust", "rf", {"n_min": 50, "n_max": 100000, "needs_uq": False},
         {"estimator": "RandomForestRegressor",
          "space": {"n_estimators": [50, 100, 200, 400], "max_depth": [2, 3, 5, 8, None]},
          "features": ["composition", "mechanistic"]},
         ["random_split", "tune_on_golden", "extrapolate_outside_family"]),
        ("gene_gbr_nonlinear", "gbr", {"n_min": 80, "n_max": 100000, "needs_uq": False,
                                       "signals": ["nonlinear_interaction"]},
         {"estimator": "GradientBoostingRegressor",
          "space": {"n_estimators": [50, 100, 200, 400], "learning_rate": [0.02, 0.05, 0.1, 0.2],
                    "max_depth": [2, 3, 4]},
          "features": ["composition", "mechanistic"]},
         ["random_split", "tune_on_golden", "extrapolate_outside_family"]),
        ("gene_gp_small_n_uq", "gp", {"n_min": 20, "n_max": 500, "needs_uq": True,
                                      "signals": ["small_data", "uncertainty_required"]},
         {"estimator": "GaussianProcessRegressor", "kernel": "C*RBF+White(fixed)",
          "space": {"length_scale": [0.1, 0.3, 0.6, 1, 2, 4, 8, 16, 32]},
          "features": ["composition", "mechanistic"], "uq": "conformal_split"},
         ["random_split", "tune_on_golden", "extrapolate_outside_family"]),
    ]
    for gid, fam, sig, strat, avoid in GENES:
        w(f"insert into evo.gene(id,family,signals_match,strategy,avoid,validation) values("
          f"{s(gid)},{s(fam)},{j(sig)},{j(strat)},"
          f"array[{','.join(s(x) for x in avoid)}],'node run_eval.js --check')"
          f" on conflict (id) do nothing;")

    # ---------------- 预测先行登记表
    w("\n-- evo.prediction_register：预测先行登记（先登记预测，后做实验；已回灌 actual 的用于校准）")
    reg_n = 0
    for i in range(N):
        fm = perf_rows[i]
        if fm["system"] != "epoxy" or reg_n >= 12:
            continue
        reg_n += 1
        pred = round(fm["shear"] * (1 + rng.gauss(0, 0.09)), 2)
        half = round(abs(pred) * rng.uniform(0.10, 0.20), 2)
        actual = round(fm["shear"], 2) if rng.random() < 0.6 else None
        w(f"insert into evo.prediction_register(experiment_no,model_version,formulation_features,"
          f"predicted,ci_low,ci_high,rationale,registered_at,actual,aligned_at) values("
          f"{s('EXP-' + fm['code'] + '-SH')},{s('champion-round0-baseline')},"
          f"{j({'ee_ratio': round(fm['m']['ee_ratio'], 4), 'toughener_pct': round(fm['m']['toughener_pct'], 2), 'filler_pct': round(fm['m']['filler_pct'], 2)})},"
          f"{num(pred,2)},{num(pred-half,2)},{num(pred+half,2)},"
          f"{s('基线模型预测，待实验回灌校准')},"
          f"{s(fm['created'].isoformat() + ' 08:30:00+08')},{num(actual,2)},"
          f"{(s((fm['created'] + timedelta(days=25)).isoformat() + ' 16:00:00+08')) if actual else 'null'});")

    # ---------------- 引导事件（追加式审计日志的第一条）
    w("\n-- evo.evolution_event：引导事件（本表只追加，禁 UPDATE/DELETE）")
    boot_note = ("四级库种子数据装载完成；种子 Gene 5 条；"
                 "Capsule 不预置——按防自欺律第2条，Capsule 只能由真实评估产生")
    boot_payload = {"note": boot_note, "seed": a.seed, "formulations": N,
                    "families": sorted({fm["family"] for fm in perf_rows})}
    w("insert into evo.evolution_event(run_id,actor,kind,payload) values("
      f"{s('bootstrap')},'scribe','bootstrap',{j(boot_payload)});")

    w("\ncommit;")

    out = a.out
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")

    # 统计
    hits = sum(1 for fm in perf_rows if fm.get("shear", 0) >= fm["target"])
    print(f"✅ 已生成 {out}")
    print(f"   配方 {N} 个（环氧 {N_EPOXY} / PU {N_PU} / 丙烯酸 {N_ACRYLIC}），"
          f"性能行 {N + len([i for i in tg_set if perf_rows[i]['system']=='epoxy']) + len(vis_set) + len(peel_set)}")
    print(f"   达标(hit) {hits}/{N} = {hits/N:.0%}；注入负例 {sum(1 for fm in perf_rows if fm['anomaly'])} 个")
    print(f"   当量比 ee_ratio 均值 {sum(fm['m']['ee_ratio'] for fm in perf_rows)/N:.3f}")


_HAS_VECTOR = True   # 由 --no-vector 关闭；默认假设 20-schema-ext.sql 已建 comp_embedding

if __name__ == "__main__":
    main()
