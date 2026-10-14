"""方案 C · 跨项目发现问答（检索 + 归因，答案必须带引用溯源）。

红线（与 Henkel "Ask Albert" 同类场景的真痛点对齐）：
  1. 答案必须带证据链（SQL + 命中行），可点开；
  2. 无证据时直接说"库里没有"，不编造；
  3. LLM 只做归因措辞与总结，不产生事实、不产生数值；
  4. 每条问答落 evo.answer_audit（触发器强制 evidence 非空）。

意图识别用规则（关键词 + 实体正则），不是 LLM 分类器 —— 保证可复现、可审计。
"""
from __future__ import annotations

import json
import re
import uuid

import fslineage as L
from fsdb import connect, dumps
from fsllm import NO_NUMBER_RULE, chat

PO_RE = re.compile(r"\b(PO-\d{8}-\d{3})\b", re.I)
LOT_RE = re.compile(r"\b(L\d{4}-\d{2})\b", re.I)
PRJ_RE = re.compile(r"\b(PRJ-[A-Z0-9-]+)\b", re.I)
FS_RE = re.compile(r"\b(FS-[A-Z]{2}-\d{4})\b", re.I)

INTENT_RULES = [
    ("po", (r"采购", r"订单", r"\bPO\b", r"到货", r"供应商来料", r"批次L?\d")),
    ("compliance", (r"VOC", r"REACH", r"SVHC", r"合规", r"GHS", r"受限", r"SDS")),
    ("attribute", (r"为什么", r"下降", r"变差", r"异常", r"归因", r"原因", r"根因", r"波动",
                   r"失效", r"不合格", r"掉")),
    ("similar", (r"类似", r"相似", r"相近", r"其他项目", r"其它项目", r"有没有谁", r"同类体系")),
    ("genealogy", (r"用在哪", r"用到哪", r"被哪", r"哪些项目", r"哪个项目", r"用到", r"用于",
                   r"谱系", r"追溯", r"用过")),
]
DEFAULT_INTENT = "genealogy"

AUDIT_SQL = """
insert into evo.answer_audit(trace_id, question, intent, answer, evidence, engine)
values (%(trace_id)s, %(question)s, %(intent)s, %(answer)s, %(evidence)s::jsonb, %(engine)s)
returning id
"""


def detect_intent(q):
    for intent, pats in INTENT_RULES:
        for p in pats:
            if re.search(p, q, re.I):
                return intent
    return DEFAULT_INTENT


def _entities(q):
    return {
        "po_no": (PO_RE.search(q).group(1).upper() if PO_RE.search(q) else None),
        "lot_no": (LOT_RE.search(q).group(1).upper() if LOT_RE.search(q) else None),
        "project": (PRJ_RE.search(q).group(1).upper() if PRJ_RE.search(q) else None),
        "formulation": (FS_RE.search(q).group(1).upper() if FS_RE.search(q) else None),
    }


def _ev(kind, source, sql, rows, note=None):
    return {"kind": kind, "source": source, "sql": sql, "n_rows": len(rows or []),
            "rows": rows or [], "note": note}


def _fmt_genealogy(q, ent):
    """原料 → 项目 → 配方 → 性能。"""
    target = None
    m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9\-_]{2,20})\s*(?:到底|究竟)?\s*被", q)
    if m:
        target = m.group(1)
    if not target:
        m = re.search(r"(E51|E44|DER331|F51|TGDDM|DETA|TETA|IPDA|DDS|PA650|MTHPA|DICY|CTBN|ATBN|CSR|"
                      r"SILICA|CACO3|ALOH3|AL2O3|FUMED|BGE|AGE|KH560|DMP30|DEFOAM|CB|PPG2000|MDI|"
                      r"环氧树脂|固化剂|增韧剂|填料|稀释剂|硅微粉|气相二氧化硅)", q, re.I)
        target = m.group(1) if m else None
    if not target:
        return None
    r = L.material_usage(target)
    if not r["rows"]:
        return {"answer": f"库里没有「{target}」的投料记录（原料库无匹配或该原料未被任何配方使用）。",
                "evidence": [_ev("resolve", "mat.fuzzy_material",
                                 "select * from mat.fuzzy_material(:q, 5)",
                                 (r["resolved"].get("candidates") or []),
                                 "无匹配时不推断")],
                "intent": "genealogy"}
    s = r["summary"]
    top = r["rows"][0]
    answer = (f"「{r['resolved']['material']['name'] or r['resolved']['material']['code']}」"
              f"（{r['resolved']['material']['code']}，匹配方式 {r['resolved']['match']}）"
              f"共出现在 {s['n_formulations']} 个配方、{s['n_projects']} 个项目中，"
              f"平均占比 {s['avg_pct']}%，"
              f"对应剪切强度 {s['min_lss']}~{s['max_lss']} MPa（均值 {s['avg_lss']}）。"
              f"用量最大的配方是 {top['code']}（{top['pct_norm']}%，项目 {top['project_code']}，"
              f"{top['lap_shear_mpa']} MPa）。")
    return {"answer": answer, "evidence": [
        _ev("genealogy", r["source"], r["sql"], r["rows"][:10]),
        _ev("summary", r["source"], "聚合：count(distinct formulation_id/project_code)", [s])],
        "intent": "genealogy"}


def _fmt_similar(q, ent):
    code = ent["formulation"]
    if not code:
        with connect() as c, c.cursor() as cur:
            cur.execute("""select f.code from mat.formulation f
                           join mat.performance p on p.formulation_id = f.id
                           where p.test_item = 'lap_shear_strength' and p.value is not null
                           order by p.value desc limit 1""")
            row = cur.fetchone()
        code = row["code"] if row else None
    if not code:
        return None
    r = L.similar_formulations(code, k=5)
    if not r["rows"]:
        return {"answer": f"库里没有与 {code} 可比的相似配方。", "evidence": [], "intent": "similar"}
    lines = [f"以 {code}（{r['base']['system']}，项目 {r['base'].get('project_code')}）为基准，"
             f"组成最相似的 {len(r['rows'])} 个配方："]
    for s in r["rows"]:
        lss = next((p["value"] for p in s["performance"] if p["test_item"] == "lap_shear_strength"), None)
        lines.append(f"  · {s['code']}（相似度 {s['cosine_similarity']:.3f}）"
                     f"剪切 {lss if lss is not None else '—'} MPa")
    return {"answer": "\n".join(lines),
            "evidence": [_ev("similar", r["source"], r["sql"], r["rows"])], "intent": "similar"}


def _fmt_attribute(q, ent):
    code = ent["formulation"]
    if not code:
        with connect() as c, c.cursor() as cur:
            cur.execute("""select f.code from mat.formulation f
                           join mat.performance p on p.formulation_id = f.id
                           where p.test_item = 'lap_shear_strength' and p.value is not null
                           order by p.value asc limit 1""")
            row = cur.fetchone()
        code = row["code"] if row else None
    if not code:
        return None
    r = L.attribute(code)
    if not r["candidates"]:
        return {"answer": f"库里没有 {code} 的异常证据（批次/当量比/工艺均在正常范围，"
                          f"也没有失效模式记录）。不做无证据的猜测。",
                "evidence": [_ev("attribute", r["source"], "归因候选扫描", [])],
                "intent": "attribute"}
    lines = [f"{code} 的根因候选（按证据强度排序，每条都有可点开的证据链）："]
    for i, c in enumerate(r["candidates"][:5], 1):
        lines.append(f"  {i}. {c['cause']}（证据强度 {c['score']}）：{c['detail']}"
                     f"\n      → 下一步：{c['next_action']}")
    if r.get("position"):
        p = r["position"]
        lines.append(f"  参考：该配方 {r['target']}={p['value']}，处于同体系 {p['percentile']:.0%} 分位"
                     f"（中位数 {p['median']}，n={p['peers_n']}）")
    return {"answer": "\n".join(lines),
            "evidence": [_ev("attribute", r["source"], "归因候选扫描",
                             r["candidates"][:5]),
                         _ev("performance", "mat.performance",
                             "select * from mat.performance where formulation_id = :id",
                             r["performance"])], "intent": "attribute"}


def _fmt_compliance(q, ent):
    code = ent["formulation"]
    if not code:
        with connect() as c, c.cursor() as cur:
            cur.execute("select code from mat.formulation where system='epoxy' order by id limit 1")
            row = cur.fetchone()
        code = row["code"] if row else None
    if not code:
        return None
    r = L.compliance_of(code)
    if not r["rows"]:
        return {"answer": f"库里没有 {code} 的合规数据。", "evidence": [], "intent": "compliance"}
    row = r["rows"]
    return {"answer": (f"{code}：VOC 估算 {row['voc_g_l_est']} g/L（{row['voc_pct_est']}%），"
                       f"SVHC 命中={row['svhc_hit']}，受限物质={row['restricted_hit']}，"
                       f"缺合规数据的原料 {row['materials_missing_compliance']} 种。"
                       f"结论由规则引擎给出，合规数据为估算值，不可用于真实合规判定。"),
            "evidence": [_ev("compliance", r["source"],
                             "select * from mat.v_formulation_compliance where code = :c", [row])],
            "intent": "compliance"}


def _fmt_po(q, ent):
    po = ent["po_no"]
    if not po:
        with connect() as c, c.cursor() as cur:
            cur.execute("select po_no from mat.purchase_order order by id limit 1")
            row = cur.fetchone()
        po = row["po_no"] if row else None
    if not po:
        return None
    r = L.po_trace(po)
    if not r["rows"]:
        return {"answer": f"库里没有采购订单 {po} 的到货记录。", "evidence": [], "intent": "po"}
    rows = [x for x in r["rows"] if x.get("formulation_code")]
    head = r["rows"][0]
    answer = (f"{po}（{head['supplier_name']}，{head['supplier_tier']} 级供应商，"
              f"到货 {head['received_at']}，状态 {head['po_status']}）→ "
              f"原料 {head['raw_code']} 批次 {head['lot_no']} → "
              f"项目 {head['project_code']}")
    if rows:
        x = rows[0]
        answer += f" → 配方 {x['formulation_code']} → {x['test_item']} {x['value']}"
    n_f = len({x["formulation_code"] for x in rows})
    answer += f"。共关联 {n_f} 支配方（只读镜像，不回写 ERP）。"
    return {"answer": answer,
            "evidence": [_ev("po_trace", r["source"], r["sql"], r["rows"][:10],
                             r.get("note"))], "intent": "po"}


HANDLERS = {"genealogy": _fmt_genealogy, "similar": _fmt_similar,
            "attribute": _fmt_attribute, "compliance": _fmt_compliance, "po": _fmt_po}


def ask(question, use_llm=False, save=True):
    """跨项目发现问答：先检索证据，再组织答案（无证据 → 明说"库里没有"）。"""
    trace = f"q-{uuid.uuid4().hex[:8]}"
    intent = detect_intent(question)
    ent = _entities(question)
    out = HANDLERS[intent](question, ent) or {
        "answer": "库里没有可回答该问题的证据（未识别到配方/原料/订单/批次实体）。",
        "evidence": [_ev("entity", "regex", "实体识别", [ent])], "intent": intent}

    if use_llm and out["evidence"]:
        base = out["answer"]
        out["answer"] = chat(
            "你是化工研发知识助手。只能依据给定证据作答，禁止补充证据外的任何事实与数值。",
            f"问题：{question}\n证据摘要：{dumps(out['evidence'])[:1500]}\n"
            f"已有答案：{base}\n{NO_NUMBER_RULE}",
            base, max_chars=600)

    out["trace_id"] = trace
    out["question"] = question
    out["entities"] = ent
    out["engine"] = "rule+llm" if use_llm else "rule"
    if save:
        with connect() as c, c.cursor() as cur:
            cur.execute(AUDIT_SQL, {"trace_id": trace, "question": question,
                                    "intent": out["intent"], "answer": out["answer"],
                                    "evidence": dumps(out["evidence"]),
                                    "engine": out["engine"]})
            out["audit_id"] = cur.fetchone()["id"]
    return out


DEMOS = [
    "这批环氧树脂 E51 到底被哪些研发项目用了？",
    "有没有其他项目用过类似的高填料低增韧体系？结果如何？",
    "FS-EP-0001 的剪切强度突然下降，可能原因是什么？",
    "FS-EP-0001 的 VOC 和 REACH 合规情况如何？",
    "PO-20260923-001 这批货用到哪个项目、哪支配方上了？",
]
