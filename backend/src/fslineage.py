"""方案 C · 四级关联库谱系查询与归因（原料 → 中间体 → 配方 → 性能）。

所有查询只读语义视图，不碰基表写入；每条返回都带 `source`（视图/函数）与原始 SQL，
保证 Agent 的回答可以逐条点开溯源。

查询能力：
  1. 原料被哪些项目/配方用过（原料级谱系，中间体已展开）
  2. 批次 → 配方 → 性能（批次漂移归因入口）
  3. 采购订单 → 原料 → 批次 → 项目 → 配方 → 性能（只读链路）
  4. 相似配方检索（原料级组成余弦相似度）
  5. 性能下降的归因候选排序（批次 / 当量比 / 相容性 / 工艺 / 交联密度 / 失效模式）
  6. 配方级合规（VOC 质量加权 + REACH/SVHC/GHS 命中）
"""
from __future__ import annotations

import re

import numpy as np

from fsdb import connect

# ---------------------------------------------------------------- 原料解析
def resolve_material(q):
    """原料名/代码/别名 → 原料行（pg_trgm 模糊匹配，确定性，不靠 LLM）。"""
    with connect() as c, c.cursor() as cur:
        cur.execute("select id, code, name, category from mat.raw_material where upper(code)=upper(%s)",
                    (q,))
        row = cur.fetchone()
        if row:
            return {"material": row, "match": "exact_code", "confidence": 1.0}
        cur.execute("select * from mat.fuzzy_material(%s, 5)", (q,))
        rows = cur.fetchall()
    if not rows:
        return {"material": None, "match": "unmatched", "confidence": 0.0, "candidates": []}
    top = rows[0]
    return {"material": {"id": top["raw_material_id"], "code": top["code"],
                         "name": top["name"], "category": top["category"]},
            "match": "trgm_fuzzy", "confidence": round(float(top["similarity"] or 0), 3),
            "candidates": [dict(r) for r in rows]}


def material_usage(q, limit=20, target="lap_shear_strength"):
    """原料 → 配方 → 项目 → 性能（中间体已展开到原料级）。"""
    res = resolve_material(q)
    if not res["material"]:
        return {"resolved": res, "rows": [], "source": "mat.v_material_genealogy",
                "note": "库里没有该原料（无匹配），不做推断"}
    code = res["material"]["code"]
    with connect() as c, c.cursor() as cur:
        cur.execute("""
            select raw_code, raw_name, category, formulation_id, formulation_code as code,
                   system, stage, family, project_code, project_name, customer, app_code, industry,
                   pct, pct_norm, basis, max_depth, via, lap_shear_mpa
            from mat.v_material_genealogy
            where raw_code = %(code)s
            order by pct_norm desc, lap_shear_mpa desc nulls last
            limit %(limit)s
        """, {"code": code, "limit": limit})
        rows = cur.fetchall()
        cur.execute("""
            select count(distinct formulation_id) n_formulations,
                   count(distinct project_code) n_projects,
                   round(avg(pct_norm), 3) avg_pct,
                   round(min(lap_shear_mpa)::numeric, 3) min_lss,
                   round(avg(lap_shear_mpa)::numeric, 3) avg_lss,
                   round(max(lap_shear_mpa)::numeric, 3) max_lss
            from mat.v_material_genealogy where raw_code = %(code)s
        """, {"code": code})
        summary = cur.fetchone()
    return {"resolved": res, "rows": rows, "summary": summary,
            "source": "mat.v_material_genealogy",
            "sql": "select * from mat.v_material_genealogy where raw_code = :code"}


def lot_trace(lot_no, limit=50):
    """批次 → 配方 → 性能（一批料用到哪些配方、结果如何）。"""
    with connect() as c, c.cursor() as cur:
        cur.execute("""
            select * from mat.v_lot_trace where lot_no = %(lot)s
            order by test_item, tested_at nulls last limit %(limit)s
        """, {"lot": lot_no, "limit": limit})
        rows = cur.fetchall()
    return {"rows": rows, "source": "mat.v_lot_trace", "lot_no": lot_no,
            "sql": "select * from mat.v_lot_trace where lot_no = :lot"}


def po_trace(po_no, limit=50):
    """采购订单 → 原料 → 批次 → 项目 → 配方 → 性能（只读镜像）。"""
    with connect() as c, c.cursor() as cur:
        cur.execute("select * from mat.v_po_trace where po_no = %(po)s limit %(limit)s",
                    {"po": po_no, "limit": limit})
        rows = cur.fetchall()
    return {"rows": rows, "source": "mat.v_po_trace", "po_no": po_no,
            "sql": "select * from mat.v_po_trace where po_no = :po",
            "note": "ERP 只读镜像，不提供任何回写路径"}


def similar_formulations(code_or_id, k=5):
    """相似配方检索：原料级组成余弦相似度 + 性能对比。"""
    with connect() as c, c.cursor() as cur:
        if re.match(r"^\d+$", str(code_or_id)):
            fid = int(code_or_id)
        else:
            cur.execute("select id from mat.formulation where code = %(c)s", {"c": str(code_or_id)})
            row = cur.fetchone()
            if not row:
                return {"rows": [], "source": "mat.formulation_similarity",
                        "note": f"库里没有配方 {code_or_id}"}
            fid = row["id"]
        cur.execute("select * from mat.formulation_similarity(%(fid)s, %(k)s)",
                    {"fid": fid, "k": k})
        sims = cur.fetchall()
        out = []
        for s in sims:
            cur.execute("""
                select test_item, value, unit, verdict from mat.performance
                where formulation_id = %(id)s order by test_item
            """, {"id": s["formulation_id"]})
            out.append({**s, "performance": cur.fetchall()})
        cur.execute("""select f.code, f.system, f.family, pj.code as project_code
                       from mat.formulation f left join mat.project pj on pj.id = f.project_id
                       where f.id = %(id)s""", {"id": fid})
        base = cur.fetchone()
    return {"base": base, "rows": out, "source": "mat.formulation_similarity",
            "sql": "select * from mat.formulation_similarity(:fid, :k)"}


def compliance_of(code):
    """配方级合规（VOC 质量加权 + REACH/SVHC 命中）。"""
    with connect() as c, c.cursor() as cur:
        cur.execute("select * from mat.v_formulation_compliance where code = %(c)s", {"c": code})
        row = cur.fetchone()
    if not row:
        return {"rows": None, "source": "mat.v_formulation_compliance",
                "note": f"库里没有配方 {code}"}
    return {"rows": row, "source": "mat.v_formulation_compliance"}


# ---------------------------------------------------------------- 归因
def _hansen_distance(a, b):
    """Hansen 溶解度参数距离 Ra（越大越不相容）。"""
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) != 3 or len(b) != 3:
        return None
    return float(np.sqrt(4 * (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2))


def attribute(code, target="lap_shear_strength"):
    """性能异常的根因候选排序：每条都带可点开的证据链。

    候选维度（按化工归因习惯排序）：批次漂移 / 当量比 / 相容性 / 工艺 / 交联密度 / 失效模式。
    分数是证据强度的确定性加权，不是 LLM 的直觉。
    """
    with connect() as c, c.cursor() as cur:
        cur.execute("""select f.id, f.code, f.system, f.family, f.stage, f.basis,
                              pj.code as project_code
                       from mat.formulation f left join mat.project pj on pj.id = f.project_id
                       where f.code = %(c)s""", {"c": code})
        f = cur.fetchone()
        if not f:
            return {"rows": [], "note": f"库里没有配方 {code}"}
        cur.execute("""select test_item, value, unit, verdict, failure_mode, condition, tested_at::text
                       from mat.performance where formulation_id = %(id)s""", {"id": f["id"]})
        perfs = cur.fetchall()
        cur.execute("""select m.* from mat.v_mechanistic_feature m where m.formulation_id = %(id)s""",
                    {"id": f["id"]})
        mech = cur.fetchone()
        cur.execute("""select r.code, r.category, r.props, c.pct, fl.lot_no
                       from mat.v_formulation_raw_pct c
                       join mat.raw_material r on r.id = c.raw_material_id
                       left join mat.formulation_lot fl
                         on fl.formulation_id = c.formulation_id and fl.raw_material_id = r.id
                       where c.formulation_id = %(id)s order by c.pct desc""", {"id": f["id"]})
        comp = cur.fetchall()
        cur.execute("""select distinct lot_no, raw_code, lot_eew, spec_eew, lot_water_pct,
                              lot_produced_at, supplier_code, supplier_tier
                       from mat.v_lot_trace where formulation_id = %(id)s""", {"id": f["id"]})
        lots = cur.fetchall()
        # 同类配方的分布（判断该配方是否处在异常分位）
        cur.execute("""select test_item, value from mat.performance p
                       join mat.formulation f2 on f2.id = p.formulation_id
                       where f2.system = %(sys)s and p.test_item = %(t)s and p.value is not null""",
                    {"sys": f["system"], "t": target})
        peers = cur.fetchall()

    vals = [float(p["value"]) for p in peers if p["value"] is not None]
    cur_val = next((float(p["value"]) for p in perfs if p["test_item"] == target
                    and p["value"] is not None), None)
    cands = []

    # 1) 批次漂移：实测 EEW/含水率 vs 规格
    for l in lots:
        try:
            lot_eew = float(l["lot_eew"]) if l["lot_eew"] is not None else None
            spec_eew = float(l["spec_eew"]) if l["spec_eew"] is not None else None
        except (TypeError, ValueError):
            lot_eew = spec_eew = None
        if lot_eew and spec_eew:
            dev = abs(lot_eew - spec_eew) / spec_eew
            if dev >= 0.02:
                cands.append({
                    "cause": "原料批次漂移",
                    "detail": f"{l['raw_code']} 批次 {l['lot_no']} 实测 EEW {lot_eew} vs 规格 {spec_eew}"
                              f"（偏差 {dev:.1%}）",
                    "score": round(min(1.0, dev / 0.10), 3),
                    "evidence": {"lot_no": l["lot_no"], "raw_code": l["raw_code"],
                                 "supplier": l["supplier_code"], "produced_at": str(l["lot_produced_at"])},
                    "next_action": "复测该批次 EEW/含水率，必要时按实测当量比重新配料"})
        if l["lot_water_pct"] is not None and float(l["lot_water_pct"]) > 0.15:
            cands.append({"cause": "原料含水率偏高",
                          "detail": f"{l['raw_code']} 批次 {l['lot_no']} 含水率 "
                                    f"{float(l['lot_water_pct']):.3f}% > 0.15%",
                          "score": round(min(1.0, float(l["lot_water_pct"]) / 0.3), 3),
                          "evidence": {"lot_no": l["lot_no"], "water_pct": float(l["lot_water_pct"])},
                          "next_action": "查干燥/储存条件；水分会消耗异氰酸酯或影响环氧固化"})

    # 2) 当量比偏离
    if mech and mech.get("ee_ratio") is not None:
        ee = float(mech["ee_ratio"])
        dev = abs(ee - 1.0)
        if dev >= 0.05:
            cands.append({"cause": "当量比偏离化学计量",
                          "detail": f"ee = {ee:.3f}（偏离 1.0 达 {dev:.1%}）",
                          "score": round(min(1.0, dev / 0.30), 3),
                          "evidence": {"ee_ratio": ee, "epoxy_eq": float(mech["epoxy_eq"] or 0),
                                       "amine_h_eq": float(mech["amine_h_eq"] or 0)},
                          "next_action": "按实测 EEW/AHEW 重算固化剂用量，做 ee 扫描实验"})

    # 3) 相容性（Hansen 距离）
    resins = [r for r in comp if r["category"] in ("resin", "toughener", "polyol")]
    hsp = {r["code"]: (r["props"] or {}).get("hsp") for r in resins}
    base = next((h for c0, h in hsp.items() if h), None)
    for code_r, h in hsp.items():
        if not h or not base or code_r == list(hsp.keys())[0]:
            continue
        ra = _hansen_distance(base, h)
        if ra is not None and ra >= 8.0:
            cands.append({"cause": "组分相容性不足",
                          "detail": f"{list(hsp.keys())[0]} 与 {code_r} 的 Hansen 距离 Ra={ra:.1f}"
                                    f"（>8 提示相容性风险）",
                          "score": round(min(1.0, ra / 20.0), 3),
                          "evidence": {"hsp_distance": ra, "pair": [list(hsp.keys())[0], code_r]},
                          "next_action": "查相分离/雾影；考虑增容剂或换增韧剂牌号"})

    # 4) 工艺（固化温度偏离体系默认）
    cond = perfs[0]["condition"] if perfs and perfs[0].get("condition") else {}
    cure = cond.get("cure_temp_c") if isinstance(cond, dict) else None
    if cure is not None:
        default = {"epoxy": 120.0, "pu": 80.0, "acrylic": 25.0}.get(f["system"])
        if default and abs(float(cure) - default) > 25:
            cands.append({"cause": "固化工艺偏离",
                          "detail": f"固化温度 {float(cure)}℃ vs 体系常规 {default}℃",
                          "score": round(min(1.0, abs(float(cure) - default) / 60.0), 3),
                          "evidence": {"cure_temp_c": float(cure), "default": default,
                                       "condition": cond},
                          "next_action": "核实固化曲线与后固化；做 DSC 确认固化度"})

    # 5) 交联密度代理
    if mech and mech.get("epoxy_eq") is not None:
        cands.append({"cause": "交联密度（环氧当量密度）",
                      "detail": f"环氧当量密度 {float(mech['epoxy_eq']):.4f} eq/100g",
                      "score": 0.2,
                      "evidence": {"epoxy_eq": float(mech["epoxy_eq"])},
                      "next_action": "与同类配方对比后判断是否为交联密度问题"})

    # 6) 失效模式（信息量最大）
    fm = [p for p in perfs if p.get("failure_mode")]
    if fm:
        modes = sorted({p["failure_mode"] for p in fm})
        strong = 0.9 if "adhesive" in modes else 0.5
        cands.append({"cause": "失效模式指向",
                      "detail": f"失效模式 {modes}" +
                                ("（adhesive = 界面失效，通常指向表面处理/润湿而非配方强度不足）"
                                 if "adhesive" in modes else ""),
                      "score": strong,
                      "evidence": {"failure_modes": modes,
                                   "tests": [{"item": p["test_item"], "mode": p["failure_mode"]}
                                             for p in fm]},
                      "next_action": "查表面处理记录与开放时间；界面失效优先查工艺不是配方"})

    # 7) 与同类配方分布的位置
    position = None
    if cur_val is not None and len(vals) >= 5:
        arr = np.asarray(vals, float)
        pct_rank = float(np.mean(arr <= cur_val))
        position = {"value": cur_val, "peers_n": len(vals),
                    "percentile": round(pct_rank, 3),
                    "p25": round(float(np.quantile(arr, 0.25)), 3),
                    "median": round(float(np.median(arr)), 3),
                    "p75": round(float(np.quantile(arr, 0.75)), 3)}
        if pct_rank <= 0.25:
            cands.append({"cause": "该配方处于同体系低分位",
                          "detail": f"{target}={cur_val} 处于 {f['system']} 体系的 "
                                    f"{pct_rank:.0%} 分位（中位数 {position['median']}）",
                          "score": round(1.0 - pct_rank, 3),
                          "evidence": position,
                          "next_action": "对比高分位配方的组分差异（见相似配方检索）"})

    cands.sort(key=lambda c: -c["score"])
    return {"formulation": f, "target": target, "current": cur_val, "position": position,
            "candidates": cands, "performance": perfs, "composition": comp,
            "source": "mat.v_mechanistic_feature + mat.v_lot_trace + mat.performance"}
