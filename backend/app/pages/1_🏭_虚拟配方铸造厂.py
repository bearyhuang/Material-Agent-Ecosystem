"""方案 B · 虚拟配方铸造厂-lite（Streamlit 页面）

四件事看清楚：
  1. 建议实验清单（默认 proposed，**没有自动执行路径**）
  2. 材料数据卡（预测 + 置信区间 + 适用域 + 合规 + provenance + simulated 标记）
  3. 假说 ⇄ 批判（证据驱动，不是 LLM 在聊天）
  4. 进化曲线：**模拟曲线**与**实测留出集**两条分开画
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st
from app.theme import *

plt.rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

API = "http://localhost:8000"
st.set_page_config(page_title="虚拟配方铸造厂", layout="wide", page_icon="🏭")
inject_apple_css()
st.title("🏭 方案 B · 虚拟配方铸造厂-lite")
st.caption("假说 →（批判）→ BO 选点 → 人工批准 → 虚拟实验 → 回灌 → 数据卡 → 策略继承")


def get(path, **params):
    try:
        r = httpx.get(f"{API}{path}", params={k: v for k, v in params.items() if v is not None},
                      timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"API 不可达 {path}: {e.__class__.__name__}")
        return None


SYSTEM = st.sidebar.selectbox("体系", ["epoxy", "pu", "acrylic"], index=0)
ov = get("/api/foundry/overview", system=SYSTEM)
gd = get("/api/foundry/guard")

# ---------------- 红线自检横幅 ----------------
if gd:
    bad = [c for c in gd["checks"] if not c["ok"]]
    if bad:
        st.markdown(banner(False, "红线自检失败：" + "；".join(c["name"] for c in bad)),
                    unsafe_allow_html=True)
    else:
        st.markdown(banner(True, "红线自检全部通过：" + " · ".join(c["name"] for c in gd["checks"])),
                    unsafe_allow_html=True)
    with st.expander("红线自检明细（每条对应一条硬约束）", expanded=False):
        st.dataframe(pd.DataFrame([{"检查项": c["name"], "结果": "✅" if c["ok"] else "❌",
                                    "命中数": c["value"], "红线": c["redline"]}
                                   for c in gd["checks"]]), hide_index=True,
                     use_container_width=True)

if ov:
    c = ov["counts"]
    ch = ov.get("champion")
    chips = [
        stat_chip("配方候选", c["candidates"], APPLE["blue"]),
        stat_chip("建议实验", c["plans"], APPLE["orange"], f"待批准 {c['plans_pending']}"),
        stat_chip("模拟结果", c["results_simulated"], APPLE["purple"], f"实测 {c['results_real']}"),
        stat_chip("数据卡", c["datacards"], APPLE["green"]),
        stat_chip("假说/批判", f"{c['hypotheses']}/{c['critiques']}", APPLE["teal"]),
        stat_chip("策略资产", c["strategies"], APPLE["pink"]),
    ]
    grid = ("<div style='display:grid;grid-template-columns:repeat(6,1fr);gap:12px'>"
            + "".join(chips) + "</div>")
    st.markdown(grid, unsafe_allow_html=True)
    if ch:
        st.markdown(bubble(
            f"当前冠军：**{ch['family']}**（体系 {ch['system']}）\n"
            f"留出集 MAE **{float(ch['holdout_mae']):.3f}**，conformal 覆盖 "
            f"{float(ch['coverage']):.2f}\n—— 这是唯一对外声称的精度口径",
            role="agent", avatar="🏆", accent=APPLE["green"]), unsafe_allow_html=True)

st.divider()
tab1, tab2, tab3, tab4 = st.tabs(["建议实验清单", "材料数据卡", "假说 ⇄ 批判", "进化曲线"])

# ---------------- 建议实验清单 ----------------
with tab1:
    st.subheader("建议实验清单（L4：投放实验由人批准）")
    st.caption("CLI：`python foundry.py --rounds 2 --budget 5 --approver \"张工\"`；"
               "不加 --approver 则全部停在 proposed。")
    status = st.selectbox("状态过滤", [None, "proposed", "approved", "rejected"],
                          format_func=lambda x: "全部" if x is None else x)
    rows = (get("/api/foundry/plans", status=status, limit=100) or {}).get("rows", [])
    if rows:
        df = pd.DataFrame([{
            "PLAN": f"PLAN-{r['id']}", "轮次": r["round"], "配方": r["code"],
            "状态": r["status"], "批准人": r["approved_by"] or "—",
            "预测": round(float(r["predicted"]), 3) if r["predicted"] is not None else None,
            "区间下": round(float(r["ci_low"]), 3) if r["ci_low"] is not None else None,
            "区间上": round(float(r["ci_high"]), 3) if r["ci_high"] is not None else None,
            "当量比": round(float(r["ee_ratio"]), 3) if r["ee_ratio"] is not None else None,
            "Σ%": round(float(r["pct_total"]), 2) if r["pct_total"] is not None else None,
            "选点方式": r["acquisition"], "合规": (r["compliance"] or {}).get("decision"),
        } for r in rows])
        st.dataframe(df, hide_index=True, use_container_width=True)
        pick = st.selectbox("查看某个建议的配方组分", [r["id"] for r in rows],
                            format_func=lambda i: f"PLAN-{i}")
        r = next(x for x in rows if x["id"] == pick)
        items = pd.DataFrame(r["items"])
        items["pct"] = items["pct"].astype(float)
        st.write(f"**{r['code']}** 组分（Σ={items['pct'].sum():.3f}%）｜当量比 {r['ee_ratio']}")
        st.dataframe(items, hide_index=True, use_container_width=True)
        comp = r["compliance"] or {}
        if comp.get("blocking"):
            for b in comp["blocking"]:
                st.error(f"🚫 {b}")
        if comp.get("warnings"):
            for w in comp["warnings"]:
                st.warning(f"⚠️ {w}")
    else:
        st.info("（暂无建议；先跑 `foundry.py`）")

    res = (get("/api/foundry/results", limit=50) or {}).get("rows", [])
    if res:
        st.markdown("**虚拟实验结果（全部 simulated=true，不进实测表）**")
        st.dataframe(pd.DataFrame([{
            "result": r["id"], "plan": r["plan_id"], "配方": r["code"],
            "value": round(float(r["value"]), 3) if r["value"] is not None else None,
            "simulated": r["simulated"], "scale": r["scale"], "corrected": r["corrected"],
            "计划状态": r["plan_status"], "批准人": r["approved_by"] or "—",
        } for r in res]), hide_index=True, use_container_width=True)

# ---------------- 数据卡 ----------------
with tab2:
    st.subheader("材料数据卡（预测 ≠ 实测）")
    cards = (get("/api/foundry/datacards", system=SYSTEM, limit=50) or {}).get("rows", [])
    if not cards:
        st.info("（暂无数据卡；跑 foundry.py --approver 后生成）")
    else:
        df = pd.DataFrame([{
            "编号": d["code"], "轮次": d["round"],
            "预测": round(float(d["predicted"]), 3),
            "区间": f"[{round(float(d['ci_low']), 2)}, {round(float(d['ci_high']), 2)}]",
            "方法": d["interval_method"],
            "适用域": d["applicability"]["decision"],
            "合规": d["compliance"]["decision"],
            "simulated": d["simulated"], "放大修正": d["corrected"],
            "外推": (d.get("payload") or {}).get("extrapolation_warning"),
        } for d in cards])
        st.dataframe(df, hide_index=True, use_container_width=True)
        code = st.selectbox("打开数据卡", [d["code"] for d in cards])
        d = get(f"/api/foundry/datacard/{code}")
        if d:
            st.markdown(d["markdown"])
            st.download_button("下载 JSON（可供 CAE/PLM 读取）",
                               data=json.dumps(d["card"], ensure_ascii=False, indent=2, default=str),
                               file_name=f"{code}.json", mime="application/json")

# ---------------- 假说 ⇄ 批判 ----------------
with tab3:
    st.subheader("假说 ⇄ 批判（证据来自库里的相关系数，不是 LLM 编的）")
    hyps = (get("/api/foundry/hypotheses", limit=50) or {}).get("rows", [])
    if not hyps:
        st.info("（暂无假说）")
    for h in hyps:
        ev = h.get("evidence") or {}
        pe = ev.get("pearson") or {}
        title = f"**{h['code']}** · {h['statement']}"
        with st.expander(title, expanded=False):
            st.write(f"机理：{h['mechanism']}")
            st.write(f"预期方向：{h['predicted_direction']} ｜ 先验 {h['prior']} → 后验 "
                     f"{h['posterior']} ｜ 状态 **{h['status']}**")
            if pe:
                st.write(f"证据：{ev.get('feature')} 与目标的 pearson r={pe.get('r')}"
                         f"（p={pe.get('p')}，n={pe.get('n')}）")
            st.write(f"可检验预测：{h['testable_prediction']}")
            for k in (h.get("critiques") or []):
                mark = {"fatal": "🚫", "major": "⚠️", "minor": "ℹ️"}.get(k["severity"], "·")
                st.write(f"  {mark} [{k['severity']}/{k['verdict']}] {k['attack']}")

# ---------------- 进化曲线 ----------------
with tab4:
    st.subheader("进化曲线：模拟曲线 vs 实测留出集（必须分开看）")
    cv = get("/api/foundry/curve", system=SYSTEM)
    if cv and cv["strategies"]:
        st.dataframe(pd.DataFrame([{
            "轮": s["round"], "策略ID": s["id"], "继承自": s["inherited_from"],
            "本轮最优预测(模拟口径)": round(float(s["score"]), 3) if s["score"] is not None else None,
            "留出集MAE(实测口径)": round(float(s["holdout_mae"]), 3) if s["holdout_mae"] is not None else None,
            "搜索代价": s["trials_total"], "通过": s["passed"], "原因": s["fail_reason"] or "",
        } for s in cv["strategies"]]), hide_index=True, use_container_width=True)
        xs = [s["round"] for s in cv["strategies"]]
        ys = [float(s["score"]) if s["score"] is not None else None for s in cv["strategies"]]
        hm = [float(s["holdout_mae"]) if s["holdout_mae"] is not None else None
              for s in cv["strategies"]]
        sim = {r["round"]: r for r in cv["simulated_per_round"]}
        fig, ax = plt.subplots(1, 2, figsize=(13, 4))
        ax[0].plot(xs, ys, "o-", color="#d62728", label="模拟口径：本轮最优预测")
        ax[0].set_xlabel("round"); ax[0].set_ylabel("预测性能（simulated）")
        ax[0].set_title("虚拟实验室曲线（模拟器口径）")
        ax[0].legend(); ax[0].grid(alpha=.3)
        ax[1].plot(xs, hm, "s-", color="#2ca02c", label="留存集 MAE（真实精度）")
        ax[1].set_xlabel("round"); ax[1].set_ylabel("MAE")
        ax[1].set_title("留出集曲线（真实数据，不含模拟点）")
        ax[1].legend(); ax[1].grid(alpha=.3)
        st.pyplot(fig)
        if len(xs) >= 2:
            st.caption(f"继承链：R{xs[0]} → R{xs[-1]}（第二轮起点 = 第一轮终点，"
                       f"搜索代价从「随机撞中最优点」降到「1 次继承」）")
        if sim:
            st.markdown("**每轮模拟结果统计**")
            st.dataframe(pd.DataFrame([{"轮": k, "模拟数": v["n"],
                                        "最优": round(float(v["best"]), 3) if v["best"] else None,
                                        "均值": round(float(v["mean"]), 3) if v["mean"] else None}
                                       for k, v in sorted(sim.items())]),
                         hide_index=True, use_container_width=True)
    else:
        st.info("（暂无策略资产；跑两轮 foundry.py）")
    st.warning("⚠️ 模拟曲线只用于展示「搜索过程」，真实精度只有留出集 MAE 一条曲线。")

st.caption("FormulaAgents · 方案 B · 数据为合成/脱敏数据，模拟结果一律标注 simulated")
