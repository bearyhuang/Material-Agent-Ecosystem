#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 scripts/harvest_adhesive.py 与 extract_fulltext.py 的产出物化进 PG。

  python scripts/load_harvest.py            # 载入体系字典 + 源清单 + 抽取表
  python scripts/load_harvest.py --init-only  # 只初始化体系字典

强约束：**必须先跑 60-schema-multisystem.sql**。
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

ROOT = Path("/workspace") if Path("/workspace").exists() else Path(__file__).resolve().parent.parent
HARVEST = ROOT / "data_raw" / "harvest"

# 体系字典：与 harvest_adhesive.py 的 SYSTEMS 保持一致
DICTIONARY = {
    "epoxy":     ("环氧树脂结构胶", "epoxy structural adhesive", "加成/缩聚固化（胺/酸酐/潜伏性）",
                  ["lap_shear_strength", "Tg", "elongation", "modulus"], ["GB/T 7124", "ASTM D1002", "GB/T 19466.2"],
                  ["metal", "composite", "glass"]),
    "pu":        ("聚氨酯胶", "polyurethane adhesive", "湿气固化(1K) / 多元醇-异氰酸酯(2K)",
                  ["lap_shear_strength", "T-peel", "elongation"], ["GB/T 7124", "GB/T 2790"], ["metal", "plastic", "wood"]),
    "acrylic":   ("丙烯酸酯结构胶(SGA)", "structural acrylic adhesive", "自由基聚合（双组分/底涂引发）",
                  ["lap_shear_strength", "impact_peel", "elongation"], ["GB/T 7124", "ISO 11343"], ["metal", "composite"]),
    "uv_acrylic": ("UV固化丙烯酸酯", "UV curable acrylic", "光引发自由基聚合",
                   ["cure_speed", "lap_shear_strength", "shrinkage"], ["GB/T 7124"], ["glass", "plastic"]),
    "psa_acrylic": ("丙烯酸酯压敏胶", "acrylic pressure sensitive adhesive", "乳液/溶剂聚合，常温初粘",
                    ["peel_adhesion", "loop_tack", "shear_holding"], ["GB/T 2792", "PSTC-101"], ["film", "foam", "plastic"]),
    "psa_rubber": ("橡胶型压敏胶(SIS/SBS/丁基)", "rubber PSA", "热熔涂布，弹性体+增粘树脂",
                   ["peel_adhesion", "loop_tack", "SAFT"], ["GB/T 2792", "PSTC-107"], ["film", "paper"]),
    "silicone":  ("有机硅胶", "silicone adhesive", "RTV 湿气/加成固化",
                  ["lap_shear_strength", "elongation", " peel"], ["GB/T 7124", "GB/T 13477"], ["glass", "metal", "ceramic"]),
    "cyanoacryl": ("氰基丙烯酸酯(瞬干)", "cyanoacrylate adhesive", "阴离子湿气快速聚合",
                   ["fixture_time", "lap_shear_strength"], ["GB/T 7124"], ["plastic", "rubber", "metal"]),
    "anaerobic": ("厌氧胶", "anaerobic adhesive", "缺氧金属离子引发聚合",
                  ["breakaway_torque", "prevail_torque", "cure_time"], ["GB/T 2792 类/军工标准"], ["metal"]),
    "hotmelt":   ("热熔胶(EVA/PO/PA/TPU)", "hot melt adhesive", "热熔-冷固（物理固化）",
                  ["lap_shear_strength", "open_time", "softening_point", "melt_viscosity"], ["GB/T 15332", "ASTM D4498"],
                  ["paper", "wood", "textile"]),
    "waterborne": ("水性胶(PVAc/VAE/水性PU)", "waterborne adhesive", "水分挥发/聚结成膜",
                   ["lap_shear_strength", "open_time", "viscosity", "solid_content"], ["GB/T 7124", "GB/T 2794"],
                   ["wood", "paper", "porous"]),
    "silane_ms": ("硅烷改性聚醚(MS/STP/SMP)", "silane modified polymer", "硅烷水解缩合湿气固化",
                  ["lap_shear_strength", "elongation", "modulus", "skin_over_time"], ["GB/T 13477", "GB/T 7124"],
                  ["metal", "glass", "concrete"]),
    "phenolic":  ("酚醛/脲醛/木材胶", "phenolic / amino resin adhesive", "热压缩聚固化",
                  ["bonding_strength", "water_resistance", "formaldehyde_emission"], ["GB/T 17657", "EN 314"], ["wood"]),
    "hightemp":  ("耐高温胶(PI/BMI)", "high temperature adhesive", "高温固化交联",
                  ["lap_shear_strength_at_T", "Tg", "thermal_decomposition"], ["GB/T 7124", "ASTM E1131"],
                  ["metal", "ceramic", "composite"]),
    "inorganic": ("无机/硅酸盐/水泥基", "inorganic / silicate adhesive", "水化/脱水硬化",
                  ["compressive_strength", "bond_strength", " heat_resistance"], ["GB/T 17671", "JC/T 547"],
                  ["concrete", "ceramic", "stone"]),
}


def rows_of(p):
    if not Path(p).exists():
        print(f"  ⚠️ 缺少 {p}（先跑对应的采集/抽取脚本）")
        return []
    return [json.loads(l) for l in Path(p).read_text(encoding="utf-8").splitlines() if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init-only", action="store_true")
    a = ap.parse_args()

    from fsdb import connect

    with connect() as c, c.cursor() as cur:
        for code, (zh, en, mech, props, tests, subs) in DICTIONARY.items():
            cur.execute(
                """insert into mat.adhesive_system(code,name_zh,name_en,cure_mechanism,
                       key_properties,typical_tests,typical_substrates)
                   values (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)
                   on conflict (code) do update
                     set name_zh=excluded.name_zh, cure_mechanism=excluded.cure_mechanism,
                         key_properties=excluded.key_properties, typical_tests=excluded.typical_tests""",
                (code, zh, en, mech, json.dumps(props, ensure_ascii=False),
                 json.dumps(tests, ensure_ascii=False), json.dumps(subs, ensure_ascii=False)))
        c.commit()
        print(f"✅ 体系字典 {len(DICTIONARY)} 条已写入 mat.adhesive_system")
        if a.init_only:
            return

        docs = rows_of(HARVEST / "unified.jsonl")
        n = 0
        for d in docs:
            if not (d.get("doi") or d.get("title")):
                continue
            status = "open" if (d.get("has_fulltext_xml") or d.get("pdf_url") or d.get("oa_url")) \
                else "metadata_only"
            pmcid = d.get("pmcid")
            try:
                cur.execute(
                    """insert into ext.source_doc(channel,system,doi,pmcid,title,year,venue,url,
                           fulltext_url,suppl_url,pdf_url,is_oa,license,cited_by,access_status,
                           query,harvested_at)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       on conflict (channel, doi) do nothing""",
                    (d.get("channel"), d.get("system"), d.get("doi"), pmcid,
                     (d.get("title") or "")[:1000], d.get("year"), d.get("venue"),
                     d.get("url"), d.get("fulltext_xml"),
                     (f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/supplementaryFiles"
                      if pmcid else None),
                     d.get("pdf_url"), bool(d.get("is_oa")), d.get("license"),
                     d.get("cited_by"), status, d.get("query"),
                     datetime.now(timezone.utc)))
                n += cur.rowcount
            except Exception as e:
                c.rollback()
                if "src_has_locator" not in str(e):
                    print(f"  skip {d.get('doi')}: {str(e)[:80]}")
        c.commit()
        print(f"✅ 源清单写入 {n} 条 → ext.source_doc")

        tabs = rows_of(HARVEST / "extracted_tables.jsonl")
        m = 0
        for t in tabs:
            if not t.get("pmcid"):
                continue
            cur.execute(
                """insert into ext.extracted_table(doi,pmcid,system,title,year,table_label,
                       caption,header,rows,n_rows,score,adhesion_relevant,source_url)
                   values (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s)
                   on conflict (pmcid, table_label, caption) do nothing""",
                (t.get("doi"), t.get("pmcid"), t.get("system"), (t.get("title") or "")[:1000],
                 t.get("year"), t.get("table_label"), (t.get("caption") or "")[:2000],
                 json.dumps(t.get("header") or [], ensure_ascii=False),
                 json.dumps(t.get("rows") or [], ensure_ascii=False),
                 sum(len(r) for r in t.get("rows") or []),
                 t.get("score"), bool(t.get("adhesion_relevant")), t.get("source")))
            m += cur.rowcount
        c.commit()
        print(f"✅ 抽取表写入 {m} 张 → ext.extracted_table（全部 review_status=unreviewed）")

        cur.execute("""select s.code, s.name_zh,
                              count(distinct d.id) as docs,
                              count(distinct t.id) as tabs,
                              count(distinct t.id) filter (where t.adhesion_relevant) as adh_tabs
                       from mat.adhesive_system s
                       left join ext.source_doc d on d.system=s.code
                       left join ext.extracted_table t on t.system=s.code
                       group by 1,2 order by adh_tabs desc, tabs desc""")
        print(f"\n{'体系':<14}{'中文名':<26}{'文献':>7}{'抽表':>7}{'胶接相关':>10}")
        for r in cur.fetchall():
            print(f"{r['code']:<14}{r['name_zh']:<26}{r['docs']:>7}{r['tabs']:>7}{r['adh_tabs']:>10}")


if __name__ == "__main__":
    main()
