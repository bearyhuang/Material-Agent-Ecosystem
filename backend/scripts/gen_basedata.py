#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FormulaAgents · 四级库基础数据全面扩充生成器（2026-10-13 检索轮）

链路：供应商 ──→ 原材料 ──→ 中间体 ──→ 配方 ──→ 实验 ──→ 性能 ←── 客户应用
                    (BOM 递归)          (时间序)      (旁挂)

数据性质（务必如实声明，与 gen_seed.py 一致）：
  * 原料/牌号/物性取自公开资料与行业通用规格（2026-10-13 全网检索复核：环氧当量、
    胺当量、填料粒径/吸油值、多元醇羟值、异氰酸酯 NCO%），数值落在真实规格区间内，
    但**不是任何一家公司的实测台账**；
  * 配方与性能是**按机理方程合成的**（planted ground truth，与 gen_seed.py 同一套方程），
    用于验证算法选择机制，**不得对外声称胶粘剂真实精度**；
  * 大模型钩子（--llm）只生成**定性业务描述**（供应商画像、客户应用需求说明），
    禁止产出任何数值指标；无 OPENAI_API_KEY 时自动回退确定性文案。

用法：
  python3 scripts/gen_basedata.py                 # 直接加载进 PG（DATABASE_URL）
  python3 scripts/gen_basedata.py --dry-run       # 只在内存生成并打印统计，不写库
  python3 scripts/gen_basedata.py --scale 0.5 --seed 42
  python3 scripts/gen_basedata.py --llm           # 启用大模型定性描述 enrichment
"""
import argparse, json, math, random, sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent                  # backend/scripts
BACKEND = HERE.parent                                   # backend/
sys.path.insert(0, str(BACKEND / "src"))
from fsdb import connect  # noqa: E402

RAW_BY_CODE = {}                                        # main() 里赋全量目录（模块级函数引用）

# ----------------------------------------------------------------- SQL 字面量
def s(v):
    return "null" if v is None else "'" + str(v).replace("'", "''") + "'"


def j(o):
    return "null" if o is None else s(json.dumps(o, ensure_ascii=False))


def num(v, nd=4):
    return "null" if v is None else f"{round(float(v), nd):g}"


def vec(v):
    return "null" if v is None else "'[" + ",".join(f"{x:.6f}" for x in v) + "]'::vector"


# ================================================================= 新增原料目录
# 物性来源（公开资料，2026-10-13 检索）：
#   固体环氧 E20/E12：环氧值 0.20/0.12 eq/100g → EEW 500/833（=100/环氧值）
#   AG-80（四官能氨基环氧）：EEW 105–125，取 117；DER 383：EEW 182–190，取 186
#   双酚F环氧：EEW ≈ 165；MHHPA/HHPA 酸酐当量 168/154
#   1,3-BAC 脂环胺 AHEW = 170.3/4 ≈ 42.6；Jeffamine D-230 AHEW ≈ 60（MW 230/4），T-403 ≈ 81
#   ETBN 环氧当量 ≈ 2800；沉淀/球形填料 d50/吸油值取公开规格区间中值
#   聚醚多元醇羟值 28–56、聚酯多元醇 56–112 mgKOH/g；NCO/OH 摩尔比 1.05–1.2
#   MDI NCO% = 2×42.02/250.25 ≈ 33.6；TDI-100 ≈ 48.2；HDI 三聚体 ≈ 22；IPDI ≈ 37.8
RAW_BD = [
    # ---- 树脂（环氧）----
    ("E20", "双酚A型固体环氧 E20", "25068-38-6", "resin",
     {"epoxy_value": 0.20, "eew": 500, "form": "solid", "functionality": 2.0, "hsp": [17.6, 10.6, 9.1]}),
    ("E12", "双酚A型固体环氧 E12", "25068-38-6", "resin",
     {"epoxy_value": 0.12, "eew": 833, "form": "solid", "functionality": 2.0, "hsp": [17.7, 10.7, 9.2]}),
    ("AG80", "四官能氨基环氧 AG-80", "71248-24-9", "resin",
     {"eew": 117, "visc_25c_mpas": 4500, "functionality": 4.0, "form": "liquid",
      "hsp": [17.9, 11.0, 9.4], "water_pct": 0.18}),
    ("DER383", "双酚A型环氧 D.E.R.383", "25068-38-6", "resin",
     {"eew": 186, "visc_25c_mpas": 9000, "functionality": 2.0, "form": "liquid",
      "hsp": [17.4, 10.5, 9.0], "water_pct": 0.05}),
    ("BF210", "双酚F型环氧树脂", "2095-03-6", "resin",
     {"eew": 165, "visc_25c_mpas": 3000, "functionality": 2.0, "form": "liquid",
      "hsp": [17.2, 10.1, 8.7], "water_pct": 0.10}),
    # ---- 固化剂 ----
    ("MHHPA", "甲基六氢苯酐 MHHPA", "19438-64-3", "hardener",
     {"ahew": 168, "anhydride_equiv": 168, "form": "liquid", "cure_temp_min_c": 100}),
    ("HHPA", "六氢苯酐 HHPA", "85-60-9", "hardener",
     {"ahew": 154, "anhydride_equiv": 154, "form": "solid", "cure_temp_min_c": 110}),
    ("BAC", "1,3-环己二甲胺（脂环胺）", "2579-20-6", "hardener",
     {"ahew": 42.6, "mw": 170.3, "active_h": 4, "visc_25c_mpas": 9, "form": "liquid"}),
    ("D230", "聚醚胺固化剂 Jeffamine D-230", "9046-10-0", "hardener",
     {"ahew": 60, "mw": 230, "visc_25c_mpas": 9, "form": "liquid"}),
    ("T403", "聚醚胺固化剂 Jeffamine T-403", "39423-51-3", "hardener",
     {"ahew": 81, "mw": 440, "visc_25c_mpas": 70, "form": "liquid"}),
    # ---- 增韧剂 ----
    ("ETBN", "端环氧基液体丁腈橡胶 ETBN", "25265-19-4", "toughener",
     {"hsp": [17.5, 6.4, 4.1], "epoxy_equiv": 2800, "acrylonitrile_pct": 18, "form": "liquid"}),
    ("MBS", "MBS 核壳增韧剂（粉体）", "25053-09-2", "toughener",
     {"hsp": [17.9, 5.2, 3.6], "d50_um": 1.2, "core_shell": True, "form": "powder"}),
    ("TPU", "热塑性聚氨酯增韧粒子", "58305-83-6", "toughener",
     {"hsp": [18.5, 8.0, 6.0], "d50_um": 5.0, "form": "powder"}),
    # ---- 填料 ----
    ("SILICA-F", "活性硅微粉（细，硅烷处理）", "60676-86-0", "filler",
     {"d50_um": 2.0, "oil_abs_g100g": 28, "density": 2.65, "form": "powder"}),
    ("SILICA-C", "活性硅微粉（粗）", "60676-86-0", "filler",
     {"d50_um": 12.0, "oil_abs_g100g": 22, "density": 2.65, "form": "powder"}),
    ("AL2O3-S5", "球形氧化铝（导热 5μm）", "1344-28-1", "filler",
     {"d50_um": 5.0, "oil_abs_g100g": 32, "density": 3.95, "form": "powder", "spherical": True}),
    ("AL2O3-S20", "球形氧化铝（导热 20μm）", "1344-28-1", "filler",
     {"d50_um": 20.0, "oil_abs_g100g": 28, "density": 3.95, "form": "powder", "spherical": True}),
    ("ATH-F", "细氢氧化铝（阻燃）", "21645-51-2", "filler",
     {"d50_um": 1.0, "oil_abs_g100g": 35, "density": 2.42, "form": "powder"}),
    ("CACO3-N", "纳米活性碳酸钙", "471-34-1", "filler",
     {"d50_um": 0.08, "oil_abs_g100g": 40, "density": 2.71, "form": "powder", "surface_treated": True}),
    ("TALC", "滑石粉", "14807-96-6", "filler",
     {"d50_um": 8.0, "oil_abs_g100g": 32, "density": 2.78, "form": "powder"}),
    ("WOLL", "硅灰石粉", "13983-17-0", "filler",
     {"d50_um": 10.0, "oil_abs_g100g": 26, "density": 2.90, "form": "powder"}),
    # ---- 稀释剂 ----
    ("NPGDGE", "新戊二醇二缩水甘油醚（692）", "17557-23-2", "diluent",
     {"eew": 140, "visc_25c_mpas": 15, "form": "liquid"}),
    ("PGDGE", "聚丙二醇二缩水甘油醚（630）", "26142-30-3", "diluent",
     {"eew": 250, "visc_25c_mpas": 60, "form": "liquid"}),
    # ---- 助剂 ----
    ("KH550", "硅烷偶联剂 KH-550", "919-30-2", "additive", {"form": "liquid", "coupling": True}),
    ("KH792", "硅烷偶联剂 KH-792", "1760-24-3", "additive", {"form": "liquid", "coupling": True}),
    ("E4MZ", "2-乙基-4-甲基咪唑（促进剂）", "931-36-2", "additive", {"form": "solid", "accelerator": True}),
    ("PHENOL", "苯酚（潜伏促进剂）", "108-95-2", "additive", {"form": "solid", "latent": True}),
    ("AO1010", "抗氧剂 1010", "6683-19-8", "additive", {"form": "powder"}),
    ("LEVEL", "聚硅氧烷流平消泡剂", "63148-62-9", "additive", {"form": "liquid"}),
    # ---- 聚氨酯体系 ----
    ("PPG3000", "聚醚多元醇 PPG-3000", "25322-69-4", "polyol",
     {"oh_value_mgkoh": 37, "mw": 3000, "hsp": [16.5, 9.0, 7.0], "form": "liquid"}),
    ("PPG1000", "聚醚多元醇 PPG-1000", "25322-69-4", "polyol",
     {"oh_value_mgkoh": 112, "mw": 1000, "hsp": [16.6, 9.1, 7.1], "form": "liquid"}),
    ("PTMEG1000", "聚四氢呋喃多元醇 PTMEG-1000", "25190-06-1", "polyol",
     {"oh_value_mgkoh": 112, "mw": 1000, "hsp": [17.0, 9.4, 7.4], "form": "liquid"}),
    ("PEA", "聚酯多元醇（己二酸系）", "25214-14-8", "polyol",
     {"oh_value_mgkoh": 75, "mw": 1500, "hsp": [17.8, 10.2, 8.0], "form": "liquid"}),
    ("PMDI", "聚合 MDI（多苯基多亚甲基多异氰酸酯）", "9016-87-9", "isocyanate",
     {"nco_pct": 31.0, "functionality": 2.7, "hsp": [19.2, 8.6, 8.1], "form": "liquid"}),
    ("TDI8020", "甲苯二异氰酸酯 TDI-80/20", "584-84-9", "isocyanate",
     {"nco_pct": 48.2, "hsp": [18.8, 8.2, 7.6], "form": "liquid"}),
    ("HDIT", "HDI 三聚体（脂肪族）", "28182-81-2", "isocyanate",
     {"nco_pct": 22.0, "functionality": 3.0, "hsp": [17.6, 8.0, 7.2], "form": "liquid"}),
    ("IPDI", "异佛尔酮二异氰酸酯 IPDI", "4098-71-9", "isocyanate",
     {"nco_pct": 37.8, "hsp": [18.4, 8.4, 7.8], "form": "liquid"}),
]

# ================================================================= 新增中间体（BOM）
# (code, name, process, [(kind, code, pct)])  kind: raw / sub
INTERMEDIATES_BD = [
    ("IM-ATBN-MB", "ATBN 增韧母料（ATBN 35%）", "预反应加成：120℃/2h 氮气保护",
     [("raw", "ATBN", 35), ("raw", "DER383", 65)]),
    ("IM-FILL-F-MB", "细填料母料（硅微粉 45%）", "三辊研磨分散至细度≤20μm",
     [("raw", "SILICA-F", 45), ("raw", "DER383", 55)]),
    ("IM-FILL-C-MB", "粗填料母料（硅微粉 50%）", "高速分散至细度≤45μm",
     [("raw", "SILICA-C", 50), ("raw", "E44", 50)]),
    ("IM-AL2O3-MB", "导热填料母料（球铝 55%）", "行星搅拌分散，真空脱泡",
     [("raw", "AL2O3-S20", 55), ("raw", "E51", 45)]),
    ("IM-TOUGH-ATH-MB", "增韧阻燃复合母料（二级）", "母料复配：两级 BOM 验证血缘递归",
     [("sub", "IM-CTBN-MB", 55), ("raw", "ATH-F", 45)]),
    ("IM-TOUGH-FILL2-MB", "细粒径增韧填充母料（二级）", "母料复配：两级 BOM",
     [("sub", "IM-ATBN-MB", 60), ("sub", "IM-FILL-F-MB", 40)]),
    ("IM-AMINE-MHHPA", "酸酐加成物固化剂（MHHPA/E51）", "酸酐预加成：110℃/2h",
     [("raw", "MHHPA", 60), ("raw", "E51", 40)]),
    ("IM-AMINE-D230", "聚醚胺加成物固化剂（D-230/DER383）", "胺过量加成：70℃/1.5h",
     [("raw", "D230", 30), ("raw", "DER383", 70)]),
    ("IM-EPOXY-DIL", "环氧稀释液（E51/692）", "常温搅拌复配",
     [("raw", "E51", 80), ("raw", "NPGDGE", 20)]),
    ("IM-PU-PREPOLY2", "PU 预聚体（PPG-3000/PMDI，NCO≈9%）", "85℃/3h 预聚",
     [("raw", "PPG3000", 72), ("raw", "PMDI", 28)]),
    ("IM-PU-PREPOLY3", "PU 预聚体（聚酯多元醇/TDI，NCO≈7%）", "80℃/2.5h 预聚",
     [("raw", "PEA", 70), ("raw", "TDI8020", 30)]),
]

# ================================================================= 新增供应商
SUPPLIERS_BD = [
    ("SUP-BD-OLIN", "Olin Epoxy（示例）", "US", "A", {"scope": "液体双酚A环氧、特种环氧"}),
    ("SUP-BD-KUKDO", "国都化工（示例）", "CN", "A", {"scope": "电子级环氧、水性环氧"}),
    ("SUP-BD-ADK", "ADEKA（示例）", "JP", "A", {"scope": "酸酐/脂环胺固化剂"}),
    ("SUP-BD-BASF", "BASF Polyurethanes（示例）", "DE", "A", {"scope": "多元醇、异氰酸酯"}),
    ("SUP-BD-COVESTRO", "Covestro（示例）", "DE", "A", {"scope": "MDI/TDI 体系"}),
    ("SUP-BD-WACKER", "Wacker Chemie（示例）", "DE", "B", {"scope": "硅烷、消泡剂"}),
    ("SUP-BD-BYK", "BYK Additives（示例）", "DE", "B", {"scope": "流平剂、分散剂"}),
    ("SUP-BD-SHANDONG", "华东阻燃填料厂（示例）", "CN", "C", {"scope": "氢氧化铝、滑石粉"}),
    ("SUP-BD-HENAN", "中原球形铝氧化厂（示例）", "CN", "B", {"scope": "球形氧化铝导热填料"}),
    ("SUP-BD-ZHEJIANG", "浙东沉淀法白炭黑厂（示例）", "CN", "C", {"scope": "沉淀硅微粉"}),
    ("SUP-BD-JIANGSU", "苏南纳米碳酸钙厂（示例）", "CN", "C", {"scope": "纳米活性碳酸钙"}),
    ("SUP-BD-LIAONING", "辽西硅灰石粉体厂（示例）", "CN", "C", {"scope": "硅灰石、硅微粉"}),
]

RAW_SUPPLIER_BD = {
    "E20": "SUP-BD-KUKDO", "E12": "SUP-BD-KUKDO", "AG80": "SUP-BD-OLIN",
    "DER383": "SUP-BD-OLIN", "BF210": "SUP-BD-KUKDO",
    "MHHPA": "SUP-BD-ADK", "HHPA": "SUP-BD-ADK", "BAC": "SUP-BD-ADK",
    "D230": "SUP-HUNTSMAN", "T403": "SUP-HUNTSMAN",
    "ETBN": "SUP-HUNTSMAN", "MBS": "SUP-EVONIK", "TPU": "SUP-BD-BASF",
    "SILICA-F": "SUP-BD-ZHEJIANG", "SILICA-C": "SUP-BD-ZHEJIANG",
    "AL2O3-S5": "SUP-BD-HENAN", "AL2O3-S20": "SUP-BD-HENAN",
    "ATH-F": "SUP-BD-SHANDONG", "CACO3-N": "SUP-BD-JIANGSU",
    "TALC": "SUP-BD-SHANDONG", "WOLL": "SUP-BD-LIAONING",
    "NPGDGE": "SUP-BD-OLIN", "PGDGE": "SUP-BD-OLIN",
    "KH550": "SUP-BD-WACKER", "KH792": "SUP-BD-WACKER",
    "E4MZ": "SUP-BD-ADK", "PHENOL": "SUP-BD-OLIN",
    "AO1010": "SUP-BD-BASF", "LEVEL": "SUP-BD-BYK",
    "PPG3000": "SUP-BD-BASF", "PPG1000": "SUP-BD-BASF",
    "PTMEG1000": "SUP-BD-BASF", "PEA": "SUP-BD-BASF",
    "PMDI": "SUP-BD-COVESTRO", "TDI8020": "SUP-BD-COVESTRO",
    "HDIT": "SUP-BD-COVESTRO", "IPDI": "SUP-BD-COVESTRO",
}

# ================================================================= 新增客户应用
APPLICATIONS_BD = [
    ("APP-BD-DISP", "显示模组组装", "铝/钢", 80, {"lap_shear_min_mpa": 12, "tg_min_c": 70, "low_stress": True}),
    ("APP-BD-PKG", "锂电池包装", "铝塑膜/PP", 85, {"lap_shear_min_mpa": 8, "peel_min_nmm": 3, "uv_resist": True}),
    ("APP-BD-CONS", "消费电子结构件", "PC/ABS", 70, {"lap_shear_min_mpa": 10, "tg_min_c": 60, "low_voc": True}),
    ("APP-BD-RAIL", "轨道交通内饰", "钢/铝", 100, {"lap_shear_min_mpa": 20, "tg_min_c": 90, "vibration": True}),
    ("APP-BD-MED", "医疗器械组装", "PC/PVC", 60, {"lap_shear_min_mpa": 6, "tg_min_c": 50, "biocompat": True}),
    ("APP-BD-LED", "LED 封装与固晶", "铝/陶瓷", 120, {"lap_shear_min_mpa": 12, "tg_min_c": 100, "low_stress": True}),
    ("APP-BD-PV", "光伏组件边框", "玻璃/铝", 85, {"lap_shear_min_mpa": 10, "tg_min_c": 70, "uv_resist": True}),
    ("APP-BD-AUTO2", "汽车车身结构", "钢/铝", 90, {"lap_shear_min_mpa": 18, "tg_min_c": 80, "crash_required": True}),
    ("APP-BD-5G", "5G 基站滤波器", "铝合金/镀锌板", 110, {"lap_shear_min_mpa": 15, "tg_min_c": 95, "emc": True}),
    ("APP-BD-SEMI", "功率半导体封装", "铜/DBC", 150, {"lap_shear_min_mpa": 14, "tg_min_c": 130, "thermal_cycling": True}),
    ("APP-BD-WEAR", "可穿戴设备", "不锈钢/TPU", 65, {"lap_shear_min_mpa": 7, "tg_min_c": 55, "flex_fatigue": True}),
    ("APP-BD-MOLD", "模具与工装粘接", "钢/花岗岩", 120, {"lap_shear_min_mpa": 25, "tg_min_c": 110, "machinable": True}),
    ("APP-BD-SPORT", "运动器材（球拍/头盔）", "碳纤维/铝合金", 80, {"lap_shear_min_mpa": 16, "tg_min_c": 75, "impact_required": True}),
    ("APP-BD-SHIP", "船舶舱室粘接", "钢/复合材料", 90, {"lap_shear_min_mpa": 14, "tg_min_c": 70, "salt_spray": True}),
    ("APP-BD-HVAC", "空调换热器", "铝/铜", 110, {"lap_shear_min_mpa": 12, "tg_min_c": 100, "thermal_cycling": True}),
    ("APP-BD-BAT", "储能电池 pack", "铝合金/钢", 95, {"lap_shear_min_mpa": 15, "tg_min_c": 85, "fire_retardant": True}),
    ("APP-BD-OPT", "光学镜头定位", "玻璃/黄铜", 80, {"lap_shear_min_mpa": 9, "tg_min_c": 65, "low_outgassing": True}),
    ("APP-BD-TEXT", "纺织品复合", "织物/PU 膜", 60, {"peel_min_nmm": 5, "hand_feel": True}),
    ("APP-BD-PRINT", "3D 打印后处理", "PLA/ABS/光敏树脂", 70, {"lap_shear_min_mpa": 11, "tg_min_c": 65, "fast_cure": True}),
    ("APP-BD-FOOD", "食品包装封合", "铝箔/PET", 80, {"peel_min_nmm": 4, "food_contact": True}),
]

# ================================================================= 新增项目
# 注意红线：客户一律用「客户X」泛称，不出现真实甲方名称。
PROJECTS_BD = [
    ("PRJ-BD-01", "显示模组窄边框粘接", "客户A", date(2024, 2, 5), "APP-BD-DISP"),
    ("PRJ-BD-02", "动力电池铝塑膜封合", "客户B", date(2024, 3, 18), "APP-BD-PKG"),
    ("PRJ-BD-03", "笔电结构件低 VOC 点胶", "客户C", date(2024, 5, 9), "APP-BD-CONS"),
    ("PRJ-BD-04", "高铁内饰板粘接", "客户D", date(2024, 7, 22), "APP-BD-RAIL"),
    ("PRJ-BD-05", "一次性输液器组装", "客户E", date(2024, 9, 2), "APP-BD-MED"),
    ("PRJ-BD-06", "Mini LED 固晶胶", "客户F", date(2024, 11, 11), "APP-BD-LED"),
    ("PRJ-BD-07", "光伏边框短边密封", "客户G", date(2025, 1, 20), "APP-BD-PV"),
    ("PRJ-BD-08", "车身焊点密封补强", "客户H", date(2025, 3, 3), "APP-BD-AUTO2"),
    ("PRJ-BD-09", "基站介质滤波器固定", "客户I", date(2025, 4, 28), "APP-BD-5G"),
    ("PRJ-BD-10", "IGBT 模块导热粘接", "客户J", date(2025, 6, 16), "APP-BD-SEMI"),
    ("PRJ-BD-11", "手表底盖密封", "客户K", date(2025, 8, 4), "APP-BD-WEAR"),
    ("PRJ-BD-12", "注塑模快速修补", "客户L", date(2025, 9, 22), "APP-BD-MOLD"),
    ("PRJ-BD-13", "碳纤维球拍层压", "客户M", date(2025, 11, 10), "APP-BD-SPORT"),
    ("PRJ-BD-14", "舱室岩棉复合板粘接", "客户N", date(2026, 1, 12), "APP-BD-SHIP"),
    ("PRJ-BD-15", "储能 pack 阻燃密封", "客户O", date(2026, 3, 9), "APP-BD-BAT"),
    ("PRJ-BD-16", "光通信器件定位", "客户P", date(2026, 5, 18), "APP-BD-OPT"),
]

# ================================================================= 配方族（时间窗口，彼此重叠）
# (code, 描述, (起, 止), 体系)
FAMILIES_BD = [
    ("BD-F1-GENERAL", "通用双酚A环氧体系", (date(2024, 1, 1), date(2025, 6, 30)), "epoxy"),
    ("BD-F2-TOUGH", "增韧高强体系", (date(2024, 4, 1), date(2026, 10, 1)), "epoxy"),
    ("BD-F3-FILLED", "高填充降本体系", (date(2024, 8, 1), date(2026, 10, 1)), "epoxy"),
    ("BD-F4-THERMAL", "导热填充体系", (date(2025, 1, 1), date(2026, 10, 1)), "epoxy"),
    ("BD-F5-FAST", "快速固化体系", (date(2025, 3, 1), date(2026, 10, 1)), "epoxy"),
    ("BD-F6-LATENT", "潜伏单组分体系", (date(2025, 6, 1), date(2026, 10, 1)), "epoxy"),
    ("BD-P1-FLEX", "柔性 PU 体系", (date(2024, 2, 1), date(2026, 10, 1)), "pu"),
    ("BD-P2-STRUC", "结构 PU 体系", (date(2024, 6, 1), date(2026, 10, 1)), "pu"),
    ("BD-A1-SGA", "第二代丙烯酸酯体系", (date(2024, 3, 1), date(2026, 10, 1)), "acrylic"),
]

# ================================================================= 机理与合成（与 gen_seed.py 同一套方程）
def hsp_distance(a, b):
    return math.sqrt(4 * (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def expand(items, inter_map, depth=0):
    """配方项（原料或中间体）递归展开到原料级 → {code: pct}（与 mat.v_bom_explode 同构）"""
    out = {}
    for kind, code, pct in items:
        if kind == "raw":
            out[code] = out.get(code, 0.0) + pct
        else:
            if depth >= 5:
                continue
            for c, p in expand(inter_map[code], inter_map, depth + 1).items():
                out[c] = out.get(c, 0.0) + p * pct / 100.0
    return out


def mech_features(comp):
    epoxy_eq = amine_h_eq = 0.0
    resin = hard = tough = fill = dil = add = poly = iso = 0.0
    for code, pct in comp.items():
        _, _, _, cat, props = RAW_BY_CODE[code]
        if cat in ("resin", "diluent") and props.get("eew"):
            epoxy_eq += pct / props["eew"]
        if cat == "hardener" and props.get("ahew"):
            amine_h_eq += pct / props["ahew"]
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


def embedding(comp, raw_idx, dim=32):
    v = [0.0] * dim
    for code, pct in comp.items():
        v[raw_idx[code] % dim] += pct
    nrm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / nrm for x in v]


def weighted_hsp(comp, cats):
    num_, tot = [0.0, 0.0, 0.0], 0.0
    for code, pct in comp.items():
        _, _, _, cat, props = RAW_BY_CODE[code]
        if cat in cats and props.get("hsp"):
            for i in range(3):
                num_[i] += props["hsp"][i] * pct
            tot += pct
    return [x / tot for x in num_] if tot else None


def lap_shear(system, comp, m, fmeta, rng):
    """按机理方程合成剪切强度（MPa）。不是实测值，仅用于验证算法选择机制。"""
    r = m["ee_ratio"]
    tough, fill, dil = m["toughener_pct"], m["filler_pct"], m["diluent_pct"]
    cure_T, post, t = fmeta["cure_temp_c"], fmeta["post_cure"], fmeta["t"]
    if system == "epoxy":
        base = 12.0
        term_ratio = 20.0 * math.exp(-((r - 1.0) / 0.25) ** 2)
        term_tough = 10.0 * (1 - math.exp(-tough / 7.0)) - 0.35 * max(0.0, tough - 16)
        term_fill = 5.5 * (1 - math.exp(-fill / 14.0)) - 0.09 * max(0.0, fill - 35)
        term_dil = -0.30 * dil
        term_cure = 0.10 * (cure_T - 110) * (1.0 if post else 0.35)
        term_inter = 0.055 * tough * (cure_T - 110) / 30.0
        hsp_r, hsp_t = weighted_hsp(comp, ("resin",)), weighted_hsp(comp, ("toughener",))
        term_hsp = 0.0
        if hsp_r and hsp_t:
            term_hsp = -0.5 * abs(hsp_distance(hsp_r, hsp_t) - 7.5) / 7.5
        term_stage = {"lab": 0.0, "pilot": -1.1, "prod": -1.9}[fmeta["stage"]]
        y = (base + term_ratio + term_tough + term_fill + term_dil + term_cure
             + term_inter + term_hsp + term_stage + 2.0 * t + rng.gauss(0, 1.3))
        return max(3.5, min(48.0, y))
    if system == "pu":
        idx = m["isocyanate_pct"] / max(m["polyol_pct"], 1e-6) * 3.0
        y = (9.0 + 14.0 * math.exp(-((idx - 1.05) / 0.30) ** 2)
             + 4.0 * (1 - math.exp(-fill / 12.0)) - 0.07 * max(0.0, fill - 30)
             + 0.06 * (cure_T - 80) * (1.0 if post else 0.5)
             + {"lab": 0.0, "pilot": -0.8, "prod": -1.4}[fmeta["stage"]]
             + 1.2 * t + rng.gauss(0, 1.1))
        return max(2.0, min(32.0, y))
    y = (14.0 + 8.0 * math.exp(-((m["toughener_pct"] - 12) / 9.0) ** 2)
         + 2.0 * (1 - math.exp(-fill / 15.0)) + 1.0 * t + rng.gauss(0, 1.5))
    return max(3.0, min(30.0, y))


def tg_c(comp, m, fmeta, rng):
    base = 62.0
    term_ratio = 42.0 * math.exp(-((m["ee_ratio"] - 1.0) / 0.30) ** 2)
    term_tgddm = 0.35 * comp.get("TGDDM", 0.0) + 0.22 * comp.get("F51", 0.0)
    term_dil = -0.55 * m["diluent_pct"]
    term_filler = 0.12 * m["filler_pct"]
    term_post = 12.0 if fmeta["post_cure"] else 0.0
    y = base + term_ratio + term_tgddm + term_dil + term_filler + term_post + rng.gauss(0, 3.0)
    return max(35.0, min(210.0, y))


def peel_nmm(comp, m, fmeta, rng):
    base = 2.0 + 0.22 * m["toughener_pct"] - 0.05 * m["filler_pct"]
    return max(0.8, base + rng.gauss(0, 0.6))


# ================================================================= 配方构造
HARDENERS = ("DETA", "TETA", "IPDA", "DDS", "PA650", "MTHPA", "MHHPA", "DICY", "BAC", "D230", "T403")


def build_formulation(rng, family, system, inter_map):
    items, fm = [], {"cure_temp_c": 120, "post_cure": True}
    fam = family[0]

    if system == "epoxy":
        resin_code = rng.choice(["E51", "E44", "DER331", "DER383", "BF210"])
        items.append(("raw", resin_code, rng.uniform(38, 58)))
        if fam == "BD-F2-TOUGH":
            items = [("raw", rng.choice(["TGDDM", "F51", "AG80", "E51"]), rng.uniform(34, 50))]
            items.append(("sub", rng.choice(["IM-CTBN-MB", "IM-ATBN-MB"]), rng.uniform(8, 22)))
        elif fam == "BD-F3-FILLED":
            items.append(("raw", rng.choice(["CACO3", "ALOH3", "SILICA-C"]), rng.uniform(12, 26)))
            items.append(("sub", rng.choice(["IM-FILL-MB", "IM-FILL-C-MB"]), rng.uniform(6, 18)))
        elif fam == "BD-F4-THERMAL":
            items.append(("raw", rng.choice(["AL2O3", "AL2O3-S5", "AL2O3-S20"]), rng.uniform(15, 32)))
            if rng.random() < 0.5:
                items.append(("sub", "IM-AL2O3-MB", rng.uniform(5, 15)))
        elif fam == "BD-F5-FAST":
            items.append(("raw", rng.choice(["MBS", "CSR"]), rng.uniform(3, 10)))
            if rng.random() < 0.5:
                items.append(("sub", "IM-EPOXY-DIL", rng.uniform(5, 15)))
        elif fam == "BD-F6-LATENT":
            items.append(("sub", rng.choice(["IM-TOUGH-FILL-MB", "IM-TOUGH-ATH-MB", "IM-TOUGH-FILL2-MB"]), rng.uniform(10, 24)))
        else:  # BD-F1-GENERAL
            if rng.random() < 0.45:
                items.append(("sub", rng.choice(["IM-CTBN-MB", "IM-ATBN-MB"]), rng.uniform(4, 14)))

        if rng.random() < 0.7:
            items.append(("raw", rng.choice(["SILICA", "SILICA-F", "CACO3", "CACO3-N", "ALOH3", "ATH-F", "TALC", "WOLL"]), rng.uniform(4, 24)))
        if rng.random() < 0.3:
            items.append(("raw", "FUMED", rng.uniform(0.5, 3.0)))
        if rng.random() < 0.35:
            items.append(("raw", rng.choice(["BGE", "AGE", "NPGDGE", "PGDGE"]), rng.uniform(1, 9)))
        if rng.random() < 0.5:
            items.append(("raw", rng.choice(["KH550", "KH560"]), rng.uniform(0.3, 1.6)))
        if rng.random() < 0.15:
            items.append(("raw", "LEVEL", rng.uniform(0.1, 0.6)))

        comp0 = expand(items, inter_map)
        m0 = mech_features(comp0)
        target_r = rng.gauss(1.0, 0.10)
        if fam == "BD-F6-LATENT":
            hard_code = "DICY"                       # PHENOL 是潜伏促进剂（助剂），不当固化剂
        elif fam == "BD-F5-FAST":
            hard_code = rng.choice(["D230", "T403", "BAC"])
        elif fam == "BD-F2-TOUGH":
            hard_code = rng.choice(["DDS", "IPDA", "MTHPA", "MHHPA"])
        elif fam == "BD-F3-FILLED":
            hard_code = rng.choice(["PA650", "TETA"])
        else:
            hard_code = rng.choice(["DETA", "TETA", "PA650", "IPDA", "BAC", "D230"])
        ahew = RAW_BY_CODE[hard_code][4]["ahew"]
        hard_pct = min(max(target_r * m0["epoxy_eq"] * ahew, 1.5), 40.0)
        items.append(("raw", hard_code, hard_pct))
        if rng.random() < 0.3:
            items.append(("raw", rng.choice(["DMP30", "E4MZ"]), rng.uniform(0.3, 1.5)))
        if fam == "BD-F6-LATENT" and rng.random() < 0.5:
            items.append(("raw", "PHENOL", rng.uniform(0.2, 1.0)))   # 潜伏促进剂（助剂）
        fm["cure_temp_c"] = {
            "DDS": rng.uniform(150, 180), "DICY": rng.uniform(150, 175),
            "MTHPA": rng.uniform(120, 150), "MHHPA": rng.uniform(110, 145),
        }.get(hard_code, rng.uniform(25, 120))
        fm["post_cure"] = fm["cure_temp_c"] > 100 or rng.random() < 0.35

    elif system == "pu":
        items.append(("sub", rng.choice(["IM-PU-PREPOLY", "IM-PU-PREPOLY2", "IM-PU-PREPOLY3"]), rng.uniform(45, 70)))
        items.append(("raw", rng.choice(["PPG2000", "PPG3000", "PTMEG1000", "PEA"]), rng.uniform(8, 25)))
        if rng.random() < 0.55:
            items.append(("raw", rng.choice(["CACO3", "SILICA", "CACO3-N", "TALC"]), rng.uniform(5, 22)))
        if rng.random() < 0.4:
            items.append(("raw", rng.choice(["KH550", "KH560"]), rng.uniform(0.3, 1.2)))
        comp0 = expand(items, inter_map)
        m0 = mech_features(comp0)
        items.append(("raw", rng.choice(["MDI", "PMDI", "TDI8020"]),
                      max(2.0, min(30.0, m0["polyol_pct"] * rng.uniform(0.30, 0.55)))))
        fm["cure_temp_c"] = rng.uniform(60, 110)
        fm["post_cure"] = rng.random() < 0.5

    else:  # acrylic
        items.append(("raw", rng.choice(["CTBN", "ATBN", "ETBN"]), rng.uniform(6, 20)))
        items.append(("raw", rng.choice(["MBS", "TPU", "CSR"]), rng.uniform(2, 10)))
        items.append(("raw", rng.choice(["CACO3", "SILICA", "CACO3-N", "ATH-F"]), rng.uniform(4, 18)))
        items.append(("raw", "DMP30", rng.uniform(0.2, 1.0)))
        items.append(("raw", rng.choice(["KH550", "KH560"]), rng.uniform(0.2, 1.0)))
        fm["cure_temp_c"] = rng.uniform(23, 80)
        fm["post_cure"] = False

    # 归一到 100（末位修正）
    tot = sum(p for _, _, p in items)
    items = [(k, c, round(p * 100.0 / tot, 3)) for k, c, p in items]
    diff = round(100.0 - sum(p for _, _, p in items), 3)
    if items:
        k, c, p = items[0]
        items[0] = (k, c, round(p + diff, 3))
    fm["stage"] = rng.choices(["lab", "pilot", "prod"], weights=[0.62, 0.25, 0.13])[0]
    return items, fm


def inject_negative(rng, items):
    """注入约 13% 的'真实脏'负例：配比算错 / 过量填充 / 固化不足 → verdict=miss"""
    kind = rng.choice(["stoich_error", "over_fill", "under_cure"])
    items = list(items)
    if kind == "stoich_error":
        fac = rng.choice([0.55, 1.75])
        items = [(k, c, p * fac if c in HARDENERS else p) for k, c, p in items]
    elif kind == "over_fill":
        items = items + [("raw", rng.choice(["CACO3", "SILICA", "ALOH3", "ATH-F"]), rng.uniform(15, 30))]
    return items, kind


# ================================================================= 大模型定性描述钩子
def llm_describe(items, kind, fallback):
    """只生成定性业务描述；失败/无 key 一律回退。绝不允许数值指标进入描述。"""
    import os
    if os.getenv("FS_NO_LLM") == "1" or not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage, SystemMessage
        kw = {"model": os.getenv("FS_MODEL", "gpt-4o-mini"), "temperature": 0.4, "timeout": 30}
        if os.getenv("OPENAI_BASE_URL"):
            kw["base_url"] = os["OPENAI_BASE_URL"]
        llm = ChatOpenAI(**kw)
        out = llm.invoke([SystemMessage(content=(
            "你是胶粘剂行业业务分析师。只输出一句话中文定性描述（≤40字），"
            "禁止出现任何数值、指标、牌号参数；不要解释。")),
            HumanMessage(content=f"为以下{kind}写一句业务画像：{items}")]).content.strip()
        return out[:60].replace("\n", " ")
    except Exception:
        return fallback


# ================================================================= 主流程
def main():
    global RAW_BY_CODE
    ap = argparse.ArgumentParser(description="四级库基础数据全面扩充生成器")
    ap.add_argument("--scale", type=float, default=1.0, help="配方数量缩放（1.0 ≈ 700 个）")
    ap.add_argument("--seed", type=int, default=20261013)
    ap.add_argument("--dry-run", action="store_true", help="只生成不写库")
    ap.add_argument("--llm", action="store_true", help="启用大模型定性描述 enrichment")
    ap.add_argument("--neg-rate", type=float, default=0.13, help="负例注入比例")
    args = ap.parse_args()
    rng = random.Random(args.seed)

    import importlib.util
    spec = importlib.util.spec_from_file_location("gen_seed", HERE / "gen_seed.py")
    gs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gs)          # 复用既有原料目录/映射（不重复造轮子）

    # ---- 合并目录（既有在前、新增在后，保证既有 embedding 索引不变）----
    all_raw = list(gs.RAW) + RAW_BD
    RAW_BY_CODE = {r[0]: r for r in all_raw}
    raw_idx = {c: i + 1 for i, (c, *_) in enumerate(all_raw)}
    all_inter = list(gs.INTERMEDIATES) + INTERMEDIATES_BD
    inter_map = {i[0]: i[3] for i in all_inter}

    conn = None if args.dry_run else connect()
    cur = None if args.dry_run else conn.cursor()

    def q(sql, params=None):
        cur.execute(sql, params or {})
        try:
            return cur.fetchall()
        except Exception:
            return []

    def one(sql, params=None):
        rows = q(sql, params)
        return rows[0] if rows else None

    def exec_sql(sql):
        cur.execute(sql)

    # ---- 1. 供应商 ----
    for code, name, country, tier, props in gs.SUPPLIERS + SUPPLIERS_BD:
        desc = llm_describe(props.get("scope", ""), "原料供应商画像",
                            props.get("scope", "")) if args.llm else None
        props2 = dict(props)
        if desc:
            props2["desc"] = desc
        if not args.dry_run:
            exec_sql(f"insert into mat.supplier(code,name,country,tier,props) values({s(code)},{s(name)},"
                     f"{s(country)},{s(tier)},{j(props2)}) on conflict(code) do nothing;")

    # ---- 2. 原材料（含供应商关联）----
    if not args.dry_run:
        sup_id = {r["code"]: r["id"] for r in q("select code,id from mat.supplier")}
        rm_id = {r["code"]: r["id"] for r in q("select code,id from mat.raw_material")}
    supplier_of = {**gs.RAW_SUPPLIER, **RAW_SUPPLIER_BD}
    for code, name, cas, cat, props in RAW_BD:
        if args.dry_run:
            continue
        sid = sup_id.get(supplier_of.get(code))
        exec_sql(f"insert into mat.raw_material(code,name,cas,category,props,supplier_id) "
                 f"values({s(code)},{s(name)},{s(cas)},{s(cat)},{j(props)},{num(sid)}) "
                 f"on conflict(code) do nothing;")
    if not args.dry_run:
        rm_id = {r["code"]: r["id"] for r in q("select code,id from mat.raw_material")}

    # ---- 3. 批次（关键原料各 2-3 批）----
    lot_rows = []
    for code, *_ in all_raw:
        for _ in range(rng.choice([2, 3])):
            lot_no = f"L{code.replace('-', '')}-{rng.randint(1000, 9999)}"
            produced = date(2024, 1, 1) + timedelta(days=rng.randint(0, 900))
            props = {}
            _, _, _, cat, p = RAW_BY_CODE[code]
            if p.get("eew"):
                props["eew"] = round(p["eew"] * rng.uniform(0.98, 1.02), 1)
            if p.get("ahew"):
                props["ahew"] = round(p["ahew"] * rng.uniform(0.98, 1.02), 1)
            if p.get("water_pct") is not None:
                props["water_pct"] = round(p["water_pct"] * rng.uniform(0.7, 1.4), 3)
            lot_rows.append((code, lot_no, produced, props))
    if not args.dry_run:
        for code, lot_no, produced, props in lot_rows:
            exec_sql(f"insert into mat.material_lot(raw_material_id,lot_no,produced_at,props) "
                     f"values({rm_id[code]},{s(lot_no)},{s(produced)},{j(props)}) "
                     f"on conflict(raw_material_id,lot_no) do nothing;")

    # ---- 4. 中间体 + BOM ----
    for code, name, process, inputs in all_inter:
        if not args.dry_run:
            exec_sql(f"insert into mat.intermediate(code,name,process) values({s(code)},{s(name)},{s(process)}) "
                     f"on conflict(code) do nothing;")
    if not args.dry_run:
        im_id = {r["code"]: r["id"] for r in q("select code,id from mat.intermediate")}
        for code, name, process, inputs in all_inter:
            for kind, sub, pct in inputs:
                col, ref = ("raw_material_id", rm_id[sub]) if kind == "raw" else ("sub_intermediate_id", im_id[sub])
                exec_sql(f"insert into mat.intermediate_input(intermediate_id,{col},mass_pct) "
                         f"select {im_id[code]},{ref},{num(pct)} where not exists("
                         f"select 1 from mat.intermediate_input where intermediate_id={im_id[code]} "
                         f"and {col}={ref});")

    # ---- 5. 客户应用 + 项目 ----
    for code, industry, substrate, temp, req in gs.APPLICATIONS + APPLICATIONS_BD:
        note = llm_describe(f"{industry}/{substrate}", "客户应用需求画像",
                            f"{industry}场景，基材 {substrate}，长期使用温度 {temp}℃") if args.llm else None
        req2 = dict(req)
        if note:
            req2["note"] = note
        if not args.dry_run:
            exec_sql(f"insert into mat.customer_application(code,industry,substrate,service_temp_c,requirement) "
                     f"values({s(code)},{s(industry)},{s(substrate)},{num(temp, 1)},{j(req2)}) "
                     f"on conflict(code) do nothing;")
    for code, name, customer, started, app in gs.PROJECTS + PROJECTS_BD:
        if not args.dry_run:
            exec_sql(f"insert into mat.project(code,name,customer,started_on) values({s(code)},{s(name)},"
                     f"{s(customer)},{s(started)}) on conflict(code) do nothing;")
    if not args.dry_run:
        prj_id = {r["code"]: r["id"] for r in q("select code,id from mat.project")}
        app_id = {r["code"]: r["id"] for r in q("select code,id from mat.customer_application")}
    app_req = {a[0]: a[4] for a in gs.APPLICATIONS + APPLICATIONS_BD}

    # ---- 6. 配方计划 ----
    n_total = int(700 * args.scale)
    n_epoxy, n_pu = int(n_total * 0.6), int(n_total * 0.185)
    per_system = {"epoxy": n_epoxy, "pu": n_pu, "acrylic": n_total - n_epoxy - n_pu}
    fams_by_sys = {}
    for f in FAMILIES_BD:
        fams_by_sys.setdefault(f[3], []).append(f)

    t0, t1 = date(2024, 1, 1), date(2026, 10, 1)
    span = (t1 - t0).days
    gen = {"epoxy": 0, "pu": 0, "acrylic": 0}
    prefix = {"epoxy": "EP", "pu": "PU", "acrylic": "AC"}
    plan = []          # (system, fam, code, created_at, items, fm, prj_code, app_code, neg)
    for system, count in per_system.items():
        fams = fams_by_sys[system]
        for _ in range(count):
            gen[system] += 1
            fam = rng.choice(fams)
            lo, hi = fam[2]
            d0, d1 = max(t0, lo), min(t1, hi)
            created = d0 + timedelta(days=rng.randint(0, max(0, (d1 - d0).days)))
            items, fm = build_formulation(rng, fam, system, inter_map)
            neg = None
            if rng.random() < args.neg_rate:
                items, neg = inject_negative(rng, items)
                if neg == "under_cure":
                    fm["cure_temp_c"] = rng.uniform(35, 65)
                    fm["post_cure"] = False
            # 重新归一（负例注入后总量可能超 100）
            tot = sum(p for _, _, p in items)
            items = [(k, c, round(p * 100.0 / tot, 3)) for k, c, p in items]
            diff = round(100.0 - sum(p for _, _, p in items), 3)
            k, c, p = items[0]
            items[0] = (k, c, round(p + diff, 3))
            prj = rng.choice(PROJECTS_BD)
            plan.append((system, fam, f"BD-{prefix[system]}-{gen[system]:04d}",
                         created, items, fm, prj[0], prj[4], neg))

    # ---- 7. 落库：配方 / 组成 / 性能 ----
    stats = {"formulations": 0, "items": 0, "perfs": 0, "neg": 0,
             "verdict": {"hit": 0, "partial": 0, "miss": 0}}
    for system, fam, code, created, items, fm, prj, app, neg in plan:
        comp = expand(items, inter_map)
        m = mech_features(comp)
        fm2 = dict(fm)
        fm2["t"] = (created - t0).days / span
        y = lap_shear(system, comp, m, fm2, rng)
        if neg:   # planted negative：直接劣化，保证 verdict=miss
            y *= {"stoich_error": 0.70, "over_fill": 0.75, "under_cure": rng.uniform(0.55, 0.80)}[neg]
        req = app_req.get(app, {})
        target = req.get("lap_shear_min_mpa") or round(rng.uniform(8, 30), 1)
        target = round(target * rng.uniform(0.92, 1.08), 1)
        gap = y / target if target else 1.0
        verdict = "hit" if gap >= 1.0 else ("partial" if gap >= 0.88 else "miss")
        if neg:
            verdict = "miss"
        stats["formulations"] += 1
        stats["items"] += len(items)
        stats["neg"] += 1 if neg else 0
        stats["verdict"][verdict] += 1

        if not args.dry_run:
            exec_sql(f"insert into mat.formulation(code,version,system,stage,family,project_id,"
                     f"application_id,target_lap_shear_mpa,created_at,comp_embedding) values("
                     f"{s(code)},1,{s(system)},{s(fm['stage'])},{s(fam[0])},{num(prj_id[prj])},"
                     f"{num(app_id[app])},{num(target, 1)},{s(created.isoformat())},"
                     f"{vec(embedding(comp, raw_idx))}) on conflict(code,version) do nothing;")
            fid = one("select id from mat.formulation where code=%(code)s", {"code": code})["id"]
            for kind, sub, pct in items:
                col, ref = ("raw_material_id", rm_id[sub]) if kind == "raw" else ("intermediate_id", im_id[sub])
                exec_sql(f"insert into mat.formulation_item(formulation_id,{col},mass_pct) "
                         f"values({fid},{ref},{num(pct)});")

        # 性能行
        cond = {"cure_temp_c": round(fm["cure_temp_c"], 1), "post_cure": fm["post_cure"],
                "test_temp_c": round(rng.uniform(20, 25), 0), "substrate": "铝合金",
                "test_speed_mm_min": 5.0}
        if rng.random() < 0.06:
            cond["batch_anomaly"] = rng.choice(["mix_viscosity_drift", "cure_exotherm_high", "filler_agglomerate"])
        tested = created + timedelta(days=rng.randint(3, 14))
        perf_rows = [("lap_shear_strength", round(y, 2), "MPa", cond, "GB/T 7124-2008", tested, verdict)]
        if system in ("epoxy", "pu"):
            perf_rows.append(("tg", round(tg_c(comp, m, fm2, rng), 1), "℃",
                              {"method": "DSC", "heating_rate_k_min": 10}, "GB/T 19466.2-2004", tested, None))
        else:
            perf_rows.append(("peel_strength", round(peel_nmm(comp, m, fm2, rng), 2), "N/mm",
                              {"test_speed_mm_min": 100}, "GB/T 2790-1995", tested, None))
        stats["perfs"] += len(perf_rows)
        if not args.dry_run:
            for item, val, unit, cd, std, t_at, vd in perf_rows:
                exec_sql(f"insert into mat.performance(formulation_id,test_item,value,unit,condition,"
                         f"method_std,tested_at,verdict) values({fid},{s(item)},{num(val)},{s(unit)},"
                         f"{j(cd)},{s(std)},{s(t_at.isoformat())},{s(vd)});")

    if not args.dry_run:
        # ---- 8. 全量重算 comp_embedding（用库内实际原料 ID 建索引，新旧一致）----
        # 注意：库内可能还有混凝土/光刻胶等外部代码体系的配方（load_real_concrete 等装入），
        # 这些代码不在本目录中 → 建索引时只认库内 raw_material，未知代码跳过。
        rm = {r["id"]: r["code"] for r in q("select id,code from mat.raw_material")}
        idx_db = {code: rid for rid, code in rm.items()}
        agg = {}
        for r in q("select formulation_id,raw_material_id,pct from mat.v_formulation_raw_pct"):
            agg.setdefault(r["formulation_id"], {})[rm[r["raw_material_id"]]] = float(r["pct"])
        for fid, comp in agg.items():
            comp2 = {c: p for c, p in comp.items() if c in idx_db}
            exec_sql(f"update mat.formulation set comp_embedding={vec(embedding(comp2, idx_db))} where id={fid};")
        conn.commit()

    # ---- 9. 报告 ----
    print("=" * 64)
    print("基础数据扩充报告（2026-10-13 全面检索轮 · 机理合成数据）")
    print("=" * 64)
    print(f"新增供应商   : {len(SUPPLIERS_BD)}（合计 {len(gs.SUPPLIERS) + len(SUPPLIERS_BD)}）")
    print(f"新增原材料   : {len(RAW_BD)}（合计 {len(all_raw)}，覆盖 {len({r[3] for r in all_raw})} 类）")
    print(f"新增批次     : {len(lot_rows)}")
    print(f"新增中间体   : {len(INTERMEDIATES_BD)}（合计 {len(all_inter)}，含 2 个二级 BOM）")
    print(f"新增客户应用 : {len(APPLICATIONS_BD)}（合计 {len(gs.APPLICATIONS) + len(APPLICATIONS_BD)}）")
    print(f"新增项目     : {len(PROJECTS_BD)}")
    print(f"新增配方     : {stats['formulations']}（epoxy {per_system['epoxy']} / pu {per_system['pu']} / acrylic {per_system['acrylic']}）")
    print(f"新增组成项   : {stats['items']}")
    print(f"新增性能行   : {stats['perfs']}（verdict: hit {stats['verdict']['hit']} / partial {stats['verdict']['partial']} / miss {stats['verdict']['miss']}）")
    print(f"负例注入     : {stats['neg']}（stoich_error / over_fill / under_cure → verdict=miss）")
    if not args.dry_run:
        for t in ("supplier", "raw_material", "material_lot", "intermediate", "formulation",
                  "formulation_item", "performance", "customer_application", "project"):
            n = one(f"select count(*) as n from mat.{t}")["n"]
            print(f"  库内 mat.{t:<20} = {n}")
        n = one("select count(*) as n from mat.v_arena_dataset where test_item='lap_shear_strength'")["n"]
        print(f"  建模宽表 lap_shear_strength 行数 = {n}")
        conn.close()
    print("-" * 64)
    print("诚实声明：数据为合成/脱敏，物性落在公开规格区间内但非任何公司实测台账；")
    print("性能由机理方程合成，仅用于验证算法选择机制，不声称胶粘剂真实精度。")
    if args.llm:
        print("已启用大模型定性描述 enrichment（无 OPENAI_API_KEY 时自动回退确定性文案）。")
    else:
        print("未启用 --llm（定性描述用确定性文案；配置 OPENAI_API_KEY 后可加 --llm 重跑 enrichment）。")


if __name__ == "__main__":
    main()
