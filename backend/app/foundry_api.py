"""方案 B（虚拟配方铸造厂）+ 方案 C（四级库跨项目发现）的只读 API。

设计原则：本服务只做查询与组装，不产生任何性能指标、不做合规判定、不写 ERP。
所有写操作由受控工具（foundry.py / discover.py）经 PG 触发器校验后完成。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from typing import Optional  # noqa: E402

import fslineage as L  # noqa: E402
from fastapi import APIRouter, Body, HTTPException  # noqa: E402
from fsdb import connect  # noqa: E402

router = APIRouter(prefix="/api", tags=["foundry+discover"])


def _rows(sql, args=None):
    with connect() as c, c.cursor() as cur:
        cur.execute(sql, args or {})
        return cur.fetchall()


def _one(sql, args=None):
    rows = _rows(sql, args)
    return rows[0] if rows else None


# ============================================================================
# 方案 B · 虚拟配方铸造厂
# ============================================================================
@router.get("/foundry/overview")
def foundry_overview(system: Optional[str] = None):
    with connect() as c, c.cursor() as cur:
        cur.execute("""
            select
              (select count(*) from mat.formulation_candidate) candidates,
              (select count(*) from evo.experiment_plan) plans,
              (select count(*) from evo.experiment_plan where status='proposed') plans_pending,
              (select count(*) from evo.experiment_plan where status='approved') plans_approved,
              (select count(*) from evo.experiment_result where simulated) results_simulated,
              (select count(*) from evo.experiment_result where not simulated) results_real,
              (select count(*) from evo.datacard) datacards,
              (select count(*) from evo.hypothesis) hypotheses,
              (select count(*) from evo.hypothesis_critique) critiques,
              (select count(*) from evo.formula_strategy) strategies
        """)
        counts = cur.fetchone()
        champ_args = {}
        champ_where = "c.passed and c.system is not null and c.system <> 'unknown'"
        if system:
            champ_where += " and c.system = %(system)s"
            champ_args["system"] = system
        cur.execute(f"""select c.round, c.holdout_mae, g.family, c.coverage, c.system,
                               c.dataset_hash, c.id::text as capsule_id
                        from evo.capsule c join evo.gene g on g.id=c.gene_id
                        where {champ_where}
                        order by c.holdout_mae limit 1""", champ_args)
        champion = cur.fetchone()
    return {"counts": counts, "champion": champion,
            "redline": {"l4_auto_execute": False,
                        "simulated_into_performance": False,
                        "note": "实验计划默认 proposed，需人批准；模拟结果只进 evo.experiment_result"}}


@router.get("/foundry/plans")
def foundry_plans(status: Optional[str] = None, limit: int = 50):
    cond, args = ["1=1"], {"limit": limit}
    if status:
        cond.append("p.status = %(status)s")
        args["status"] = status
    return {"rows": _rows(f"""
        select p.id, p.run_id, p.round, p.status, p.approved_by, p.approved_at::text,
               p.target, p.predicted, p.ci_low, p.ci_high, p.acquisition, p.rationale,
               c.code, c.system, c.ee_ratio, c.pct_total, c.items, c.compliance,
               p.created_at::text
        from evo.experiment_plan p
        left join mat.formulation_candidate c on c.id = p.candidate_id
        where {' and '.join(cond)}
        order by p.id desc limit %(limit)s
    """, args)}


@router.get("/foundry/results")
def foundry_results(limit: int = 100):
    return {"rows": _rows("""
        select r.id, r.plan_id, r.simulated, r.source, r.scale, r.target, r.value,
               r.ci_low, r.ci_high, r.corrected, r.model_version, r.created_at::text,
               p.status as plan_status, p.approved_by, c.code
        from evo.experiment_result r
        join evo.experiment_plan p on p.id = r.plan_id
        left join mat.formulation_candidate c on c.id = p.candidate_id
        order by r.id desc limit %(limit)s
    """, {"limit": limit})}


@router.get("/foundry/datacards")
def foundry_datacards(system: Optional[str] = None, limit: int = 50):
    cond, args = ["1=1"], {"limit": limit}
    if system:
        cond.append("system = %(system)s")
        args["system"] = system
    return {"rows": _rows(f"""
        select id, code, run_id, round, system, target, composition, predicted, ci_low, ci_high,
               interval_method, coverage, applicability, process, compliance, provenance,
               simulated, corrected, created_at::text
        from evo.datacard where {' and '.join(cond)}
        order by id desc limit %(limit)s
    """, args)}


@router.get("/foundry/datacard/{code}")
def foundry_datacard(code: str):
    row = _one("""select * from evo.datacard where code = %(c)s""", {"c": code})
    if not row:
        raise HTTPException(404, f"数据卡 {code} 不存在")
    from fsdatacard import to_markdown

    return {"card": row, "markdown": to_markdown(row)}


@router.get("/foundry/hypotheses")
def foundry_hypotheses(run_id: Optional[str] = None, limit: int = 50):
    cond, args = ["1=1"], {"limit": limit}
    if run_id:
        cond.append("h.run_id = %(run_id)s")
        args["run_id"] = run_id
    rows = _rows(f"""
        select h.id, h.run_id, h.round, h.code, h.statement, h.mechanism,
               h.predicted_direction, h.testable_prediction, h.evidence, h.prior,
               h.posterior, h.status,
               coalesce(jsonb_agg(jsonb_build_object('attack', k.attack, 'severity', k.severity,
                                                     'verdict', k.verdict, 'actor', k.actor))
                        filter (where k.id is not null), '[]'::jsonb) as critiques
        from evo.hypothesis h
        left join evo.hypothesis_critique k on k.hypothesis_id = h.id
        where {' and '.join(cond)}
        group by h.id order by h.id desc limit %(limit)s
    """, args)
    return {"rows": rows}


@router.get("/foundry/curve")
def foundry_curve(system: Optional[str] = None):
    """进化曲线：每轮策略资产的起点/终点 + 真实留出集 MAE（两条曲线必须分开看）。"""
    cond, args = ["1=1"], {}
    if system:
        cond.append("s.system = %(system)s")
        args["system"] = system
    rows = _rows(f"""
        select s.id, s.run_id, s.round, s.system, s.target, s.score, s.holdout_mae,
               s.trials_total, s.passed, s.fail_reason, s.inherited_from,
               s.strategy->'seed_items' as seed_items,
               s.created_at::text
        from evo.formula_strategy s where {' and '.join(cond)}
        order by s.round, s.id
    """, args)
    sim = _rows("""
        select p.round, count(*) n, max(r.value) best, avg(r.value) mean
        from evo.experiment_result r join evo.experiment_plan p on p.id = r.plan_id
        where r.simulated group by p.round order by p.round
    """)
    return {"strategies": rows, "simulated_per_round": sim,
            "note": "score=模拟器口径下的本轮最优预测（仅用于排序）；holdout_mae=真实精度（唯一可对外声称）"}


@router.get("/foundry/alignment")
def foundry_alignment():
    """预测先行登记制度的量化：登记预测 vs 实测值（区间命中率 / 平均偏差）。"""
    with connect() as c, c.cursor() as cur:
        cur.execute("""
            select r.experiment_no, r.model_version, r.predicted, r.ci_low, r.ci_high, r.actual,
                   r.registered_at::text, r.aligned_at::text,
                   (r.actual is not null and r.ci_low is not null
                    and r.actual between r.ci_low and r.ci_high) as in_interval
            from evo.prediction_register r
            where r.experiment_no like 'PLAN-%'
            order by r.id desc limit 200
        """)
        rows = cur.fetchall()
        cur.execute("""
            select count(*) n,
                   count(*) filter (where actual is not null) aligned,
                   count(*) filter (where actual is not null and ci_low is not null
                                    and actual between ci_low and ci_high) hit
            from evo.prediction_register where experiment_no like 'PLAN-%'
        """)
        summary = cur.fetchone()
    hit_rate = (float(summary["hit"]) / summary["aligned"]) if summary["aligned"] else None
    return {"rows": rows, "summary": summary, "interval_hit_rate": hit_rate,
            "note": "先登记预测、后做实验：区间命中率是模型可信度的直接证据（不是事后挑的）"}


@router.get("/foundry/guard")
def foundry_guard():
    """红线自检（只读查询证明，不是口头承诺）。"""
    with connect() as c, c.cursor() as cur:
        cur.execute("""select count(*) n from evo.experiment_result r
                       join evo.experiment_plan p on p.id=r.plan_id
                       where p.approved_by is null""")
        unauthorized = cur.fetchone()["n"]
        cur.execute("""select count(*) n from evo.experiment_result
                       where simulated and source <> 'virtual_lab'""")
        bad_marker = cur.fetchone()["n"]
        cur.execute("""select count(*) n from mat.performance where method_std = 'VIRTUAL_LAB'""")
        leaked = cur.fetchone()["n"]
        cur.execute("""select count(*) n from evo.datacard
                       where simulated and (disclaimer is null or length(disclaimer) < 20)""")
        no_disclaimer = cur.fetchone()["n"]
        cur.execute("""select count(*) n from mat.formulation_candidate
                       where pct_total < 99.5 or pct_total > 100.5""")
        bad_mass = cur.fetchone()["n"]
        cur.execute("""select count(*) n from evo.answer_audit
                       where jsonb_array_length(evidence) = 0""")
        no_evidence = cur.fetchone()["n"]
    checks = [
        {"name": "无未批准即执行的实验", "ok": unauthorized == 0, "value": unauthorized,
         "redline": "L4：投放实验由人批准"},
        {"name": "模拟结果标记自洽", "ok": bad_marker == 0, "value": bad_marker,
         "redline": "simulated=true 必须 source=virtual_lab"},
        {"name": "模拟值未泄漏进实测表", "ok": leaked == 0, "value": leaked,
         "redline": "mat.performance 只放实测事实"},
        {"name": "数据卡均带免责声明", "ok": no_disclaimer == 0, "value": no_disclaimer,
         "redline": "不声称精度"},
        {"name": "配方候选质量守恒", "ok": bad_mass == 0, "value": bad_mass,
         "redline": "Σ=100±0.5（生成硬约束）"},
        {"name": "问答均带证据链", "ok": no_evidence == 0, "value": no_evidence,
         "redline": "无证据不许编"},
    ]
    return {"checks": checks, "all_pass": all(c["ok"] for c in checks)}


# ============================================================================
# 方案 C · 四级库跨项目发现
# ============================================================================
@router.get("/lineage/material")
def lineage_material(q: str, limit: int = 20):
    return L.material_usage(q, limit=limit)


@router.get("/lineage/lot")
def lineage_lot(lot: str, limit: int = 50):
    return L.lot_trace(lot, limit=limit)


@router.get("/lineage/po")
def lineage_po(po: str, limit: int = 50):
    return L.po_trace(po, limit=limit)


@router.get("/lineage/similar")
def lineage_similar(code: str, k: int = 5):
    return L.similar_formulations(code, k=k)


@router.get("/lineage/attribute")
def lineage_attribute(code: str):
    return L.attribute(code)


@router.get("/lineage/compliance")
def lineage_compliance(code: str):
    return L.compliance_of(code)


@router.get("/lineage/mass_balance")
def lineage_mass_balance():
    return {"rows": _rows("select * from mat.v_mass_balance_check order by system")}


@router.post("/ask")
def ask_question(payload: dict = Body(...)):
    from fsqna import ask

    q = (payload or {}).get("question")
    if not q:
        raise HTTPException(400, "缺少 question")
    return ask(q, use_llm=bool((payload or {}).get("llm")), save=True)


@router.get("/audit")
def audit(limit: int = 20):
    return {"rows": _rows("""
        select id, trace_id, question, intent, answer, evidence, engine, created_at::text
        from evo.answer_audit order by id desc limit %(limit)s
    """, {"limit": limit})}


@router.get("/compliance/materials")
def compliance_materials():
    return {"rows": _rows("""
        select r.code, r.name, r.category, c.voc_g_l, c.voc_pct, c.reach_status,
               c.svhc_listed, c.svhc_substances, c.ghs_codes, c.source
        from mat.raw_material r
        join mat.raw_material_compliance c on c.raw_material_id = r.id
        order by r.category, r.code
    """)}


@router.get("/compliance/rules")
def compliance_rules():
    return {"rows": _rows("""select code, kind, scope, limit_value, unit, comparator, action,
                                    legal_ref, params from mat.compliance_rule
                             where enabled order by code""")}


@router.get("/discover/overview")
def discover_overview():
    with connect() as c, c.cursor() as cur:
        cur.execute("""
            select (select count(*) from mat.raw_material) materials,
                   (select count(*) from mat.formulation) formulations,
                   (select count(*) from mat.intermediate) intermediates,
                   (select count(*) from mat.performance) performances,
                   (select count(*) from mat.formulation_lot) lot_links,
                   (select count(*) from mat.purchase_order) pos,
                   (select count(*) from mat.raw_material_compliance) compliance_rows,
                   (select count(*) from evo.answer_audit) answers
        """)
        counts = cur.fetchone()
        cur.execute("select * from mat.v_mass_balance_check order by system")
        balance = cur.fetchall()
        cur.execute("""select system, count(*) n from mat.formulation group by system order by 2 desc""")
        systems = cur.fetchall()
    return {"counts": counts, "mass_balance": balance, "systems": systems}
