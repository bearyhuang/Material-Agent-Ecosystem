"""合规规则引擎（VOC / REACH / GHS）—— 规则为主，LLM 不做合规判定。

红线：合规结论只能由 `mat.compliance_rule` + `mat.raw_material_compliance` 算出；
LLM 只能复述结论，不得给出判定。规则数据若缺失，结论必须是"无法判定"而不是"合格"。
"""
from __future__ import annotations

from fsdb import connect

MAT_SQL = """
select r.id, r.code, r.name, r.category,
       c.voc_g_l, c.voc_pct, c.density_g_ml, c.reach_status, c.svhc_listed,
       c.svhc_substances, c.ghs_codes, c.source, c.note
from mat.raw_material r
left join mat.raw_material_compliance c on c.raw_material_id = r.id
"""

RULE_SQL = """
select code, kind, scope, limit_value, unit, comparator, action, legal_ref, params
from mat.compliance_rule where enabled order by code
"""

# 判定为"拒答"的动作优先级高于 warn
ACTION_RANK = {"reject": 2, "warn": 1}


def load_materials(system: str | None = None):
    sql = MAT_SQL
    args = {}
    if system:
        sql += " where r.category not in ('binder','scm','water','admixture','aggregate','process')"
    with connect() as c, c.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def load_rules():
    with connect() as c, c.cursor() as cur:
        cur.execute(RULE_SQL)
        return cur.fetchall()


def _cmp(v, comparator, limit):
    return {
        "<=": v <= limit, "<": v < limit, ">=": v >= limit, ">": v > limit, "==": v == limit,
    }.get(comparator, False)


def _new_finding(rule, message, detail=None):
    return {
        "rule": rule["code"], "kind": rule["kind"], "action": rule["action"],
        "message": message, "legal_ref": rule["legal_ref"], "detail": detail or {},
    }


def check_material(code: str, materials=None, rules=None):
    """单原料合规检查：返回 findings / blocking / warnings / decision。"""
    materials = materials or load_materials()
    rules = rules or load_rules()
    row = next((m for m in materials if m["code"] == code), None)
    if row is None:
        return {"code": code, "known": False, "decision": "unknown",
                "findings": [_new_finding(
                    {"code": "MATERIAL_UNKNOWN", "kind": "data", "action": "reject",
                     "legal_ref": None},
                    f"原料 {code} 不在库中，无法判定合规性（不得默认合格）")],
                "blocking": [f"原料 {code} 不在库"], "warnings": []}
    if row.get("voc_g_l") is None and row.get("source") in (None, "unverified"):
        return {"code": code, "known": True, "decision": "unknown",
                "findings": [_new_finding(
                    {"code": "COMPLIANCE_DATA_MISSING", "kind": "data", "action": "warn",
                     "legal_ref": None},
                    f"{code} 缺少合规数据（source={row.get('source')}），须补 SDS 后判定")],
                "blocking": [], "warnings": [f"{code} 缺合规数据"]}

    findings, blocking, warnings = [], [], []
    for r in rules:
        if r["scope"] != "raw_material":
            continue
        if r["kind"] == "voc" and row.get("voc_g_l") is not None:
            bad = not _cmp(float(row["voc_g_l"]), r["comparator"], float(r["limit_value"]))
            if bad:
                msg = (f"{code} VOC {row['voc_g_l']} {r['unit']} 不满足 "
                       f"{r['comparator']} {r['limit_value']} {r['unit']}")
                findings.append(_new_finding(r, msg, {"voc_g_l": float(row["voc_g_l"])}))
        if r["kind"] == "reach":
            allowed = (r.get("params") or {}).get("status") or []
            if row.get("reach_status") in allowed:
                msg = f"{code} REACH 状态={row['reach_status']}（受限物质）"
                findings.append(_new_finding(r, msg, {"reach_status": row["reach_status"]}))
        if r["kind"] == "svhc" and row.get("svhc_listed"):
            subs = row.get("svhc_substances") or []
            findings.append(_new_finding(
                r, f"{code} 命中 SVHC 候选清单 {subs or ''}（>0.1% w/w 需通报）",
                {"svhc_substances": subs}))
        if r["kind"] == "ghs":
            codes = set((r.get("params") or {}).get("codes") or [])
            hit = sorted(codes & set(row.get("ghs_codes") or []))
            if hit:
                findings.append(_new_finding(r, f"{code} GHS 命中 {hit}", {"ghs": hit}))

    for f in findings:
        (blocking if f["action"] == "reject" else warnings).append(f["message"])
    decision = "reject" if blocking else ("warn" if warnings else "pass")
    return {"code": code, "known": True, "decision": decision, "findings": findings,
            "blocking": blocking, "warnings": warnings,
            "profile": {"voc_g_l": row.get("voc_g_l"), "reach_status": row.get("reach_status"),
                        "svhc_listed": row.get("svhc_listed"), "ghs_codes": row.get("ghs_codes"),
                        "source": row.get("source")}}


def check_formulation(items, materials=None, rules=None, system=None):
    """配方级合规：VOC 按质量加权估算，REACH/SVHC/GHS 按组分命中。

    items: [{'raw_material_code': 'E51', 'pct': 55.0}, ...]（pct 为质量百分比，Σ=100）
    返回 decision ∈ {pass, warn, reject, unknown}；blocking 非空 = 硬拒答。
    """
    materials = materials or load_materials()
    rules = rules or load_rules()
    prof = {m["code"]: m for m in materials}

    total = sum(float(i["pct"]) for i in items)
    if abs(total - 100.0) > 0.5:
        return {"decision": "reject",
                "blocking": [f"质量守恒失败：Σ={total:.3f}，必须为 100±0.5"],
                "warnings": [], "findings": [], "voc_g_l": None}

    voc_g_l = 0.0
    voc_pct = 0.0
    missing = []
    findings, blocking, warnings = [], [], []
    for it in items:
        code, pct = it["raw_material_code"], float(it["pct"])
        m = prof.get(code)
        if m is None:
            blocking.append(f"原料 {code} 不在库中，无法判定合规性")
            continue
        if m.get("voc_g_l") is None:
            missing.append(code)
        else:
            voc_g_l += pct / 100.0 * float(m["voc_g_l"])
            voc_pct += pct / 100.0 * float(m.get("voc_pct") or 0)
        r = check_material(code, materials, rules)
        for f in r["findings"]:
            if f["rule"] in ("MATERIAL_UNKNOWN", "COMPLIANCE_DATA_MISSING"):
                continue
            # 原料级命中，按其在配方中的占比决定是否升级为配方级结论
            if f["action"] == "reject":
                blocking.append(f"{f['message']}（配方中占 {pct:.1f}%）")
            else:
                warnings.append(f"{f['message']}（配方中占 {pct:.1f}%）")
        findings.extend(r["findings"])

    for r in rules:
        if r["scope"] != "formulation" or r["kind"] != "voc":
            continue
        bad = not _cmp(voc_g_l, r["comparator"], float(r["limit_value"]))
        if bad:
            msg = (f"配方 VOC 估算 {voc_g_l:.1f} {r['unit']} 不满足 "
                   f"{r['comparator']} {r['limit_value']} {r['unit']}")
            findings.append(_new_finding(r, msg, {"voc_g_l": round(voc_g_l, 3)}))
            (blocking if r["action"] == "reject" else warnings).append(msg)

    if missing:
        warnings.append(f"缺少合规数据的原料：{sorted(set(missing))}（VOC 为下界估算）")
    decision = "reject" if blocking else ("warn" if warnings else "pass")
    return {
        "decision": decision, "voc_g_l": round(voc_g_l, 3), "voc_pct": round(voc_pct, 3),
        "voc_g_l_est": round(voc_g_l, 3),          # 与配方级合规视图同名的别名，避免两套口径
        "voc_pct_est": round(voc_pct, 3),
        "blocking": sorted(set(blocking)), "warnings": sorted(set(warnings)),
        "findings": findings, "materials_missing_compliance": sorted(set(missing)),
        "rules_version": len(rules),
    }


def summarize(items, system=None):
    """给数据卡用的精简合规块。"""
    r = check_formulation(items, system=system)
    return {
        "decision": r["decision"],
        "voc_g_l_est": r["voc_g_l"],
        "voc_pct_est": r["voc_pct"],
        "blocking": r["blocking"],
        "warnings": r["warnings"],
        "note": "规则判定（非 LLM 判定）；合规数据为估算值，真实判定须以最新版 SDS/ECHA 清单为准",
    }
