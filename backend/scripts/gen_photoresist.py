#!/usr/bin/env python3
"""光刻胶（photoresist）体系种子数据生成器。

与 doc/04 的胶粘剂种子同构：数据按**可解释的机理方程**合成，保证"数据里真的有信号"，
算法竞技场与配方设计才有意义。刻意保留两类真实脏数据（配方族时间聚集 / 负例注入）。

⚠️ 诚实声明：牌号与物性落在公开规格区间内，但不是任何公司的实测台账；
   性能由机理方程合成，**不得对外声称真实光刻胶精度**。

产出：sql/seed_photoresist.sql（幂等，可重复装载）
用法：python3 scripts/gen_photoresist.py
"""
import datetime
import math
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "doc" / "files" / "seed_photoresist.sql"
SEED = 20261007

# ------------------------------------------------------------------ 原料台账
# category：polymer(成膜树脂) / pag(光致产酸剂或感光剂) / quencher(碱性淬灭剂)
#           solvent(溶剂) / additive(表面活性剂·附着力促进剂)
MATERIALS = [
    # code, name, cas, category, smiles, props
    ("PHS-PROT", "部分保护聚对羟基苯乙烯树脂", "24936-44-1", "polymer",
     "Oc1ccc(C=C)cc1",
     {"mw": 12000, "mw_unit": "g/mol", "pdi": 1.8, "protection_ratio": 0.35,
      "tg_c": 150, "form": "solid"}),
    ("NOVOLAC-MC", "间甲酚酚醛树脂", "25086-56-0", "polymer",
     "Cc1ccc(O)cc1",
     {"mw": 4500, "mw_unit": "g/mol", "pdi": 2.4, "tg_c": 105, "form": "solid"}),
    ("ACRYL-LAC", "含内酯丙烯酸酯共聚物（ArF）", "9010-98-4", "polymer",
     "CC(=C)C(=O)OC1CCCCO1",
     {"mw": 9500, "mw_unit": "g/mol", "pdi": 1.7, "tg_c": 160, "form": "solid"}),

    ("TPS-NF", "三苯基锍全氟丁基磺酸盐（PAG）", "144317-44-2", "pag",
     "[S+](c1ccccc1)(c1ccccc1)c1ccccc1.O=S(=O)([O-])C(F)(F)C(F)(F)C(F)(F)F",
     {"mw_pag": 512.5, "acid_yield": 0.85, "form": "solid"}),
    ("TPS-TF", "三苯基锍三氟甲磺酸盐（PAG）", "66003-78-9", "pag",
     "[S+](c1ccccc1)(c1ccccc1)c1ccccc1.O=S(=O)([O-])C(F)(F)F",
     {"mw_pag": 412.4, "acid_yield": 0.92, "form": "solid"}),
    ("DNQ-PAC", "重氮萘醌磺酸酯（感光剂/PAC）", "64132-95-2", "pag",
     "O=S(=O)(O)c1ccccc1",
     {"mw_pag": 404.4, "acid_yield": 0.55, "form": "solid"}),

    ("TBAH", "四丁基氢氧化铵（淬灭剂）", "2052-49-5", "quencher",
     "CCCC[N+](CCCC)(CCCC)CCCC.[OH-]",
     {"mw_base": 259.5, "basicity": 1.0, "form": "solution"}),
    ("TEA-OH", "三乙醇胺（淬灭剂）", "102-71-6", "quencher",
     "OCCN(CCO)CCO",
     {"mw_base": 149.2, "basicity": 0.45, "form": "liquid"}),

    ("PGMEA", "丙二醇甲醚醋酸酯（溶剂）", "108-65-6", "solvent",
     "COCC(C)OC(C)=O",
     {"bp_c": 146, "visc_25c_mpas": 1.1, "form": "liquid"}),
    ("EL", "乳酸乙酯（溶剂）", "97-64-3", "solvent",
     "CCOC(=O)C(O)C",
     {"bp_c": 154, "visc_25c_mpas": 2.4, "form": "liquid"}),

    ("SURF-FC", "含氟表面活性剂（添加剂）", "335-67-1", "additive",
     "FC(F)(F)C(F)(F)C(F)(F)C(=O)O",
     {"form": "liquid", "surface_tension_mn_m": 18}),
    ("ADH-SILANE", "氨基硅烷附着力促进剂", "13822-56-5", "additive",
     "CO[Si](OC)(OC)CCCN",
     {"form": "liquid"}),
]
MAT = {m[0]: m for m in MATERIALS}

# 三个配方族按时间窗口依次引入且彼此重叠（供 Red-team 做配方族质疑）
FAMILIES = [
    ("F1-DNQ-NOVOLAC", "g/i-line 酚醛-DNQ 胶", 0.00, 0.40, ["NOVOLAC-MC"], ["DNQ-PAC"],
     0.18),
    ("F2-KRF-CAR", "KrF 化学放大胶", 0.28, 0.72, ["PHS-PROT"], ["TPS-NF", "TPS-TF"],
     0.48),
    ("F3-ARF-CAR", "ArF 化学放大胶", 0.58, 1.00, ["ACRYL-LAC"], ["TPS-NF", "TPS-TF"],
     0.80),
]


def choose_family(rng, t):
    """配方族按时间**概率**分布，而不是硬切窗口。

    硬切窗口会让早期配方只含酚醛材料、后期只含 ArF 材料 —— 时间前向切分后
    golden 集相对 dev 变成极端外推（实测 OOD 94%、R² 为负），竞技场直接失效。
    这里给每个族一个时间高斯权重 + 0.08 的地板权重，保证族之间在时间上重叠，
    同时仍保留"平台迁移"的真实趋势（供 Red-team 的配方族质疑有的放矢）。
    """
    w = [math.exp(-((t - f[6]) / 0.42) ** 2) + 0.08 for f in FAMILIES]
    tot = sum(w)
    r, acc = rng.random() * tot, 0.0
    for i, x in enumerate(w):
        acc += x
        if r <= acc:
            return FAMILIES[i]
    return FAMILIES[-1]
N = 220                    # 配方数
TARGETS = ["resolution_nm", "ler_nm"]


def pick(rng, fam):
    """按族生成一个组分方案（返回 dict code -> pct）。"""
    polymers, pags = fam[4], fam[5]
    poly = rng.choice(polymers)
    pag = rng.choice(pags)
    quencher = rng.choice(["TBAH", "TEA-OH"])
    solvent = rng.choice(["PGMEA", "EL"])
    add = rng.choice(["SURF-FC", "ADH-SILANE"])

    # 固体分：树脂是主体，PAG 相对固体分计量
    solid = rng.uniform(18.0, 32.0)          # 固含量 %
    pag_rel = rng.uniform(1.0, 8.0)          # PAG 占固体分 %
    qr = rng.uniform(0.05, 0.90)             # 淬灭剂/PAG 摩尔比
    add_pct = rng.uniform(0.0, 1.2)
    pag_pct = solid * pag_rel / 100.0
    # 由摩尔比反推淬灭剂质量分：q_pct = qr * pag_pct/MW_pag * MW_base * basicity
    mw_pag = MAT[pag][5]["mw_pag"]
    q_pct = qr * (pag_pct / mw_pag) * MAT[quencher][5]["mw_base"] / MAT[quencher][5]["basicity"]
    poly_pct = solid - pag_pct - q_pct - add_pct
    if poly_pct <= 2.0:
        return None
    solv_pct = 100.0 - solid
    return {poly: poly_pct, pag: pag_pct, quencher: q_pct,
            solvent: solv_pct, add: add_pct}


def mech(items, catalog_mw):
    """复算机理量（与 mat.v_mechanistic_feature 的口径保持一致，便于前端展示）。"""
    solid = sum(v for k, v in items.items() if MAT[k][3] != "solvent")
    pag_mol = sum(v / MAT[k][5]["mw_pag"] for k, v in items.items() if MAT[k][3] == "pag")
    q_mol = sum(v * MAT[k][5]["basicity"] / MAT[k][5]["mw_base"]
                for k, v in items.items() if MAT[k][3] == "quencher")
    return {
        "solid_content": solid,
        "pag_loading_pct": round(100.0 * sum(v for k, v in items.items()
                                             if MAT[k][3] == "pag") / solid, 4) if solid else 0,
        "quench_ratio": round(q_mol / pag_mol, 4) if pag_mol else None,
        "acid_mol_per_100g": round(pag_mol, 6),
    }


def performance(items, peb, fam_idx, rng):
    """机理合成的种植真值（planted ground truth）。

    分辨率：化学放大胶的经典权衡 —— PAG 负载↑分辨率↑但过量会酸扩散粗化；
            淬灭剂/PAG 摩尔比存在最优窗口（≈0.35）；PEB 温度偏离最优会劣化。
    LER：   PAG 少 → 光子/酸统计噪声大 → 粗糙度↑；PEB 偏离也↑。
    """
    m = mech(items, None)
    qr = m["quench_ratio"] or 0.0
    a = m["pag_loading_pct"]
    solid = m["solid_content"]

    res = 46.0
    res -= 20.0 * math.exp(-((qr - 0.35) / 0.20) ** 2)          # 淬灭比高斯窗
    res -= 9.0 * (1.0 - math.exp(-a / 1.8))                     # PAG 负载收益（饱和）
    res += 26.0 * max(0.0, a - 6.5)                             # PAG 过量 → 酸扩散粗化
    res += 0.22 * abs(peb - 105.0)                              # PEB 偏离最优 105℃
    res += 14.0 * max(0.0, 0.30 - solid / 100.0) * 3.0          # 固含过低 → 膜不均
    res += 2.0 * fam_idx                                        # 族间基线差异（ArF 更难）
    res += rng.gauss(0, 1.8)                                    # 测量噪声

    ler = 2.1
    ler += 9.0 * math.exp(-a / 1.2)                             # 酸统计噪声
    ler += 0.018 * abs(peb - 105.0)
    ler += 1.4 * max(0.0, qr - 0.70)                            # 过度淬灭 → 表面粗糙
    ler += rng.gauss(0, 0.35)

    return {"resolution_nm": round(max(8.0, res), 3),
            "ler_nm": round(max(1.2, ler), 3)}


def embedding(items, dim=32):
    """由组成确定性生成 32 维向量（与 pgvector 列契约一致），供相似配方检索。"""
    keys = sorted(MAT)
    v = [items.get(k, 0.0) / 100.0 for k in keys]
    rng = random.Random(SEED)
    out = []
    for _ in range(dim):
        w = [rng.gauss(0, 1) for _ in keys]
        out.append(sum(a * b for a, b in zip(v, w)))
    n = math.sqrt(sum(x * x for x in out)) or 1.0
    return "[" + ",".join(f"{x/n:.6f}" for x in out) + "]"


def main():
    rng = random.Random(SEED)
    L = []
    a = L.append
    a("-- 光刻胶（photoresist）体系种子数据 —— 由 scripts/gen_photoresist.py 生成，可重复装载")
    a(f"-- seed={SEED}  配方数={N}")
    a("-- ⚠️ 性能由机理方程合成：仅用于验证算法选择与设计机制，不得声称真实光刻胶精度")
    a("begin;")
    a("")
    a("-- 1. 研发项目")
    a("""insert into mat.project(code, name, customer, started_on)
values ('PRJ-PR-01','ArF/KrF 光刻胶配方开发','内部研发','2025-03-01')
on conflict (code) do nothing;""")
    a("")
    a("-- 2. 原料（含 SMILES，供分子 3D 与官能团标注；props 扩展不破坏既有列）")
    for code, name, cas, cat, smiles, props in MATERIALS:
        p = dict(props)
        p["smiles"] = smiles
        pj = "{" + ",".join(f'"{k}":{_v(v)}' for k, v in p.items()) + "}"
        a(f"""insert into mat.raw_material(code, name, cas, category, props, supplier_id)
values ({q(code)}, {q(name)}, {q(cas)}, {q(cat)}, '{pj}'::jsonb,
        (select id from mat.supplier order by id limit 1))
on conflict (code) do update set props = excluded.props, category = excluded.category;""")
    a("")
    a("-- 3. 配方 + 组成 + 性能")
    rows = []
    for i in range(N):
        t = i / (N - 1)
        fam = choose_family(rng, t)
        fam_idx = FAMILIES.index(fam)
        items = None
        for _ in range(20):
            items = pick(rng, fam)
            if items:
                break
        if not items:
            continue
        code = f"FS-PR-{i+1:04d}"
        peb = round(rng.uniform(88.0, 122.0), 1)
        dose = round(rng.uniform(12.0, 55.0), 1)
        thick = round(rng.uniform(180.0, 420.0), 0)
        # 时间轴：2025-03-01 起，按族引入顺序推进（配方族在时间上依次引入且重叠）
        created = (datetime.date(2025, 3, 1) + datetime.timedelta(
            days=int(i * 3 + rng.randint(0, 2)))).isoformat()
        # 负例注入（约 13%）：感光剂算错 / 过度淬灭 / PEB 失温
        bad = None
        r = rng.random()
        if r < 0.06:
            items = {k: (v * 0.35 if MAT[k][3] == "pag" else v) for k, v in items.items()}
            bad = "pag_dosing_error"
        elif r < 0.10:
            items = {k: (v * 2.2 if MAT[k][3] == "quencher" else v) for k, v in items.items()}
            bad = "over_quench"
        elif r < 0.13:
            peb = round(peb - 22.0, 1)
            bad = "peb_deviation"
        items = _trim(items)
        m = mech(items, None)
        perf = performance(items, peb, fam_idx, rng)
        rows.append((code, fam[0], created, items, perf, peb, dose, thick, bad, m))

    for code, fam, created, items, perf, peb, dose, thick, bad, m in rows:
        emb = embedding(items)
        # 注意：这段 INSERT 会被包进 `with ins as (...)`，**末尾不能带分号**
        tid = f"""insert into mat.formulation(code, version, system, stage, family, project_id,
                       created_at, basis, comp_embedding)
values ({q(code)}, 1, 'photoresist', 'lab', {q(fam)},
        (select id from mat.project where code='PRJ-PR-01'),
        {q(created)}::timestamptz, 'mass_pct', {q(emb)}::vector)
on conflict (code, version) do nothing
returning id"""
        a(f"-- {code}  solid={m['solid_content']:.2f}%  quench_ratio={m['quench_ratio']}  {bad or ''}")
        a("with ins as (" + tid + ")")
        a("insert into mat.formulation_item(formulation_id, raw_material_id, mass_pct)")
        a("select ins.id, r.id, v.pct from ins")
        a("     join (values " + ", ".join(
            f"({q(k)}, {v:.4f})" for k, v in sorted(items.items())) + ") as v(code, pct)")
        a("     on true join mat.raw_material r on r.code = v.code;")
        for tgt in TARGETS:
            val = perf[tgt]
            unit = "nm"
            verdict = ("hit" if (tgt == "resolution_nm" and val <= 40) or
                       (tgt == "ler_nm" and val <= 3.5) else
                       "partial" if (tgt == "resolution_nm" and val <= 55) or
                       (tgt == "ler_nm" and val <= 5.0) else "miss")
            cond = {"peb_temp_c": peb, "exposure_dose_mj_cm2": dose,
                    "film_thickness_nm": thick,
                    # 工艺类比：光刻胶的热烘烤步骤（PEB）落在既有 cure_temp_c 语义列上，
                    # 使工艺维特征在跨体系时保持可比
                    "cure_temp_c": peb, "test_temp_c": 23.0,
                    "post_cure": False, "batch_anomaly": bad}
            a(f"""insert into mat.performance(formulation_id, test_item, value, unit, condition,
                                          method_std, verdict, tested_at)
select f.id, {q(tgt)}, {val}, {q(unit)},
       '{_json(cond)}'::jsonb, {q('CD-SEM' if tgt == 'resolution_nm' else 'CD-SEM-LER')},
       {q(verdict)}, {q(created)}::timestamptz + interval '7 days'
from mat.formulation f where f.code = {q(code)}
on conflict do nothing;""")
        a("")
    a("commit;")
    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"已生成 {OUT}（{len(rows)} 个配方，材料 {len(MATERIALS)} 种）")


def _v(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace('"', '\\"') + '"'


def _json(d):
    import json
    return json.dumps(d, ensure_ascii=False)


def q(s):
    return "'" + str(s).replace("'", "''") + "'"


def _trim(items, target=100.0):
    tot = sum(items.values())
    return {k: round(v * target / tot, 4) for k, v in items.items()}


if __name__ == "__main__":
    main()
