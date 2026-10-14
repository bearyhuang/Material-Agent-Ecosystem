"""方案 B + 方案 C 的回归测试（红线优先）。

运行（容器内）：
  python -m pytest tests -q

不依赖 LLM、不依赖网络；DB 写操作全部在事务里回滚或用唯一码隔离。
"""
import json
import os
import sys
import uuid
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fsdb import connect  # noqa: E402

# ============================================================================
# 方案 B · 配方生成硬约束
# ============================================================================
def test_mass_balance_enforced_in_db():
    """质量守恒是数据库级硬约束：Σ≠100 的候选写不进去。"""
    code = f"FC-TEST-{uuid.uuid4().hex[:6]}"
    with connect() as c, c.cursor() as cur:
        with pytest.raises(Exception) as e:
            cur.execute("""
                insert into mat.formulation_candidate(code, system, origin, items)
                values (%s,'epoxy','bo','[{"raw_material_code":"E51","pct":50}]'::jsonb)
            """, (code,))
        assert "质量守恒" in str(e.value) or "pct" in str(e.value).lower()
        c.rollback()


def test_candidate_mass_balance_and_ee():
    from fsrecipe import build_candidate, load_catalog, mech_features

    cat = load_catalog()
    items = [{"raw_material_code": "E51", "pct": 60.0},
             {"raw_material_code": "DETA", "pct": 6.34},
             {"raw_material_code": "SILICA", "pct": 33.66}]
    total = sum(i["pct"] for i in items)
    assert abs(total - 100.0) < 0.01
    mech = mech_features(cat, items)
    assert mech["ee_ratio"] is not None and mech["pct_total"] == pytest.approx(100.0, abs=0.01)
    cand = build_candidate(f"FC-TEST-{uuid.uuid4().hex[:6]}", "epoxy", items, catalog=cat)
    assert cand["compliance"]["decision"] in ("pass", "warn", "reject")


def test_violating_space_is_refused():
    """超出可行域（填料 90%）必须被拒答，而不是警告后照做。"""
    from fsrecipe import Refusal, build_candidate, load_catalog

    cat = load_catalog()
    items = [{"raw_material_code": "E51", "pct": 8.0},
             {"raw_material_code": "DETA", "pct": 2.0},
             {"raw_material_code": "SILICA", "pct": 90.0}]
    with pytest.raises(Refusal):
        build_candidate(f"FC-TEST-{uuid.uuid4().hex[:6]}", "epoxy", items, catalog=cat)


def test_feature_vector_matches_training_space():
    """候选的特征向量必须与竞技场训练空间同构（列数一致 → 可预测）。"""
    from fsarena import prepare
    from fsrecipe import build_feature_vector, load_catalog

    cat = load_catalog()
    ctx = prepare(2, "lap_shear_strength", "epoxy", "both")
    items = [{"raw_material_code": "E51", "pct": 60.0},
             {"raw_material_code": "DETA", "pct": 6.34},
             {"raw_material_code": "SILICA", "pct": 33.66}]
    x = build_feature_vector(items, ctx["feats"], "epoxy", catalog=cat)
    assert x.shape[0] == ctx["X"].shape[1]
    # 当量比列必须被正确计算（不是 0）
    i = ctx["feats"].index("ee_ratio")
    assert x[i] > 0


# ============================================================================
# 方案 B · 合规规则（规则为主，不靠 LLM）
# ============================================================================
def test_compliance_reject_on_restricted_material():
    """REACH 受限物质（MDI）必须触发拒答。"""
    from fscompliance import check_formulation

    r = check_formulation([{"raw_material_code": "MDI", "pct": 40.0},
                           {"raw_material_code": "PPG2000", "pct": 60.0}])
    assert r["decision"] == "reject"
    assert any("REACH" in b or "受限" in b or "GHS" in b for b in r["blocking"])


def test_compliance_voc_threshold():
    """高 VOC 稀释剂占比过大 → 触发 VOC 规则。"""
    from fscompliance import check_formulation

    r = check_formulation([{"raw_material_code": "BGE", "pct": 100.0}])
    assert r["voc_g_l"] > 250
    assert r["decision"] in ("warn", "reject")


def test_unknown_material_is_not_compliant():
    """未知原料不得默认合格。"""
    from fscompliance import check_material

    r = check_material("NO_SUCH_MATERIAL_XYZ")
    assert r["decision"] == "unknown" and r["known"] is False


# ============================================================================
# 方案 B · BO / 模拟器
# ============================================================================
def test_ei_prefers_high_uncertainty_and_mean():
    from fsbo import expected_improvement, upper_confidence_bound

    mu = np.array([1.0, 1.0, 5.0])
    sigma = np.array([0.01, 2.0, 0.01])
    ei = expected_improvement(mu, sigma, best=1.0)
    assert ei[2] > ei[1] > ei[0]                     # 提升幅度大的优先；同等均值下不确定性大的优先
    assert ei[2] == pytest.approx(5.0 - 1.0 - 0.01, abs=1e-3)   # sigma→0 时 EI→确定性提升值
    u = upper_confidence_bound(mu, sigma, beta=1.0)
    assert u[1] > u[0] and u[2] > u[1]


def test_batch_bo_returns_distinct_points():
    from fsbo import GPSurrogate, propose_batch

    rng = np.random.default_rng(0)
    X = rng.normal(size=(40, 3))
    y = (X[:, 0] ** 2 + 0.1 * rng.normal(size=40))
    cand = rng.normal(size=(60, 3))
    s = GPSurrogate(cfg={"ls": 1.0}).fit(X, y)
    idx, scores = propose_batch(cand, s, y, k=4, kind="ei")
    assert len(idx) == 4 and len(set(idx)) == 4


def test_virtual_lab_marks_simulated_and_keeps_holdout():
    """虚拟实验：simulated=true，且留出集 MAE 只来自真实数据。"""
    from fsarena import prepare
    from fssim import VirtualLab, load_champion

    ctx = prepare(2, "lap_shear_strength", "epoxy", "both")
    champ = load_champion("epoxy")
    if champ is None:                                # 该体系还没有冠军 → 必须拒绝开实验室
        pytest.skip("epoxy 体系暂无通过闸门的冠军模型")
    lab = VirtualLab(champ, ctx)
    mu, half = lab.predict(ctx["Xg"][:3])
    assert len(mu) == 3 and np.all(np.isfinite(mu))
    assert lab.holdout_mae == pytest.approx(float(champ["holdout_mae"]), rel=1e-6)
    assert "fsim" in lab.version


def test_unapproved_plan_cannot_produce_result():
    """L4：未经批准的实验计划写不进实验结果（DB 触发器兜底）。"""
    code = f"FC-TEST-{uuid.uuid4().hex[:6]}"
    with connect() as c, c.cursor() as cur:
        cur.execute("""insert into mat.formulation_candidate(code, system, origin, items)
                       values (%s,'epoxy','bo','[{"raw_material_code":"E51","pct":100}]'::jsonb)
                       returning id""", (code,))
        cid = cur.fetchone()["id"]
        cur.execute("""insert into evo.experiment_plan(run_id, round, candidate_id, target, status)
                       values ('test-run', 99, %s, 'lap_shear_strength', 'proposed') returning id""",
                    (cid,))
        pid = cur.fetchone()["id"]
        with pytest.raises(Exception) as e:
            cur.execute("""insert into evo.experiment_result(plan_id, simulated, source, target, value)
                           values (%s, true, 'virtual_lab', 'lap_shear_strength', 1.0)""", (pid,))
        assert "L4" in str(e.value) or "批准" in str(e.value)
        c.rollback()


def test_plan_requires_approver_name():
    with connect() as c, c.cursor() as cur:
        cur.execute("""insert into mat.formulation_candidate(code, system, origin, items)
                       values (%s,'epoxy','bo','[{"raw_material_code":"E51","pct":100}]'::jsonb)
                       returning id""", (f"FC-TEST-{uuid.uuid4().hex[:6]}",))
        cid = cur.fetchone()["id"]
        with pytest.raises(Exception) as e:
            cur.execute("""insert into evo.experiment_plan(run_id, round, candidate_id, target, status)
                           values ('test-run', 99, %s, 'lap_shear_strength', 'approved')""", (cid,))
        assert "approved_by" in str(e.value) or "L4" in str(e.value)
        c.rollback()


def test_datacard_requires_elements():
    """数据卡缺少任何必需要素 → 拒绝生成。"""
    from fsdatacard import build_datacard

    class _Lab:
        champion = None
        ctx = {}
        version = "fsim-1.0:test"

        @property
        def holdout_mae(self):
            return 1.23

    cand = {"items": [{"raw_material_code": "E51", "pct": 100.0}], "code": "X",
            "system": "epoxy", "compliance": {}, "process": {}, "ee_ratio": None}
    sim = {"value": 10.0, "ci_low": 8.0, "ci_high": 12.0, "interval_method": "split_conformal"}
    with pytest.raises(ValueError):
        build_datacard(cand, sim, _Lab(), {}, "run-x", 1, "lap_shear_strength")


def test_scaleup_correction_is_flagged_unvalidated():
    """放大修正只能作为修正项，且必须标注未验证。"""
    from fsscaleup import apply, correction_factors

    f = correction_factors("lab_500g", "pilot_50kg")
    assert f, "放大修正规则缺失"
    assert all(not v["validated"] for v in f.values())
    out = apply(30.0, "lab_500g", "pilot_50kg")
    assert out["corrected"] is True and out["validated"] is False
    assert abs(out["value"] - 30.0) / 30.0 <= 0.5      # 修正幅度被夹住，不是任意外推


# ============================================================================
# 方案 C · 四级库与问答
# ============================================================================
def test_mass_balance_view_has_zero_violations():
    with connect() as c, c.cursor() as cur:
        cur.execute("""select coalesce(sum(violations),0) v from mat.v_mass_balance_check""")
        assert cur.fetchone()["v"] == 0


def test_genealogy_and_similarity_and_lot():
    import fslineage as L

    g = L.material_usage("E51")
    assert g["rows"], "原料谱系查询为空"
    s = L.similar_formulations("FS-EP-0001", 3)
    assert s["rows"], "相似配方检索为空"
    assert all(0 <= r["cosine_similarity"] <= 1.0001 for r in s["rows"])
    lt = L.lot_trace("L2489-01")
    assert lt["rows"], "批次追溯为空"
    pt = L.po_trace("PO-20260923-001")
    assert pt["rows"], "采购订单链路为空"


def test_attribute_returns_evidence():
    import fslineage as L

    r = L.attribute("FS-EP-0001")
    assert "candidates" in r
    for c in r["candidates"]:
        assert c["evidence"] and c["next_action"]


def test_question_always_has_evidence():
    """问答必须有证据链（触发器强制 evidence 非空，无证据要说"库里没有"）。"""
    from fsqna import ask

    out = ask("环氧树脂 E51 被哪些项目用了？")
    assert out["evidence"], "答案缺少证据链"
    assert "E51" in out["answer"] or "库里没有" in out["answer"]
    with connect() as c, c.cursor() as cur:
        cur.execute("select evidence from evo.answer_audit where trace_id=%s", (out["trace_id"],))
        row = cur.fetchone()
    assert row and len(row["evidence"]) > 0


def test_unknown_question_says_no_data():
    from fsqna import ask

    out = ask("ZZZ-不存在的原料-XYZ 用在哪？")
    assert "库里没有" in out["answer"] or "没有" in out["answer"]


def test_compliance_view_basis_aware():
    """混凝土数据是 kg/m³ 基准，不能被当成质量百分比。"""
    with connect() as c, c.cursor() as cur:
        cur.execute("""select basis from mat.formulation where system='CONCRETE' limit 1""")
        row = cur.fetchone()
    assert row and row["basis"] == "kg_per_m3"


# ============================================================================
# 端到端（慢，默认跑；可用 -m "not slow" 跳过）
# ============================================================================
@pytest.mark.slow
def test_foundry_two_rounds_end_to_end():
    from fsfoundry import run_foundry

    out = run_foundry(rounds=2, system="epoxy", budget=3, pool=80, seed=0,
                      approver="pytest", verbose=False)
    assert len(out["rounds"]) == 2
    for r in out["rounds"]:
        assert r["plans"], "每轮都应产出建议实验清单"
        assert all(p["status"] == "approved" for p in r["plans"])
    r1 = max(c["predicted"] for c in out["rounds"][0]["chosen"])
    r2_start = out["rounds"][1]["warm_start"]
    assert r2_start is not None and r2_start >= r1 - 1e-3, "第二轮起点应 ≥ 第一轮终点"


@pytest.mark.slow
def test_no_simulation_leaked_into_performance():
    """模拟值绝不能出现在 mat.performance（实测事实表）。"""
    with connect() as c, c.cursor() as cur:
        cur.execute("""select count(*) n from mat.performance
                       where method_std = 'VIRTUAL_LAB' or coalesce(condition->>'simulated','') = 'true'""")
        assert cur.fetchone()["n"] == 0
