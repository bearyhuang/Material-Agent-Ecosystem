"""方案A · 一个页面（Streamlit）——SECTION 9 多 Agent 蜂群协作赛道版。

评分对表（全部取真实 PG 数据，不造假）：
  涌现性    → /api/contrast 蜂群 vs 单 Agent 对比横幅
  协议通信  → /api/agents  角色注册表 + 事件种类 + 发言统计
  容错安全  → /api/failures 失败归因 + /api/trust 信任边界(agent_ro)
  前瞻扩展  → /api/genes   Gene 注册表（新 Agent 即插即用）
  监控可视化→ 蜂群看板 5s 自动刷新事件流
启动：streamlit run app/ui.py --server.port 8501 --server.headless true
"""
import json

import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st
from app.theme import *

plt.rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "Noto Sans CJK JP", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

API = "http://localhost:8000"
ACTOR_COLOR = {
    "supervisor": "#1f77b4", "selector": "#17becf", "arbiter": "#9467bd",
    "red_team": "#d62728", "integrator": "#ff7f0e", "scribe": "#7f7f7f",
}
DIM = {"emerge": "#2ca02c", "proto": "#1f77b4", "safety": "#d62728", "scale": "#ff7f0e"}


def get(path, **params):
    try:
        r = httpx.get(f"{API}{path}", params={k: v for k, v in params.items() if v is not None},
                      timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"API 不可达 {path}: {e.__class__.__name__} {e}")
        return None


st.set_page_config(page_title="FormulaAgents · 多Agent蜂群竞技场", layout="wide",
                   page_icon="🧪")
inject_apple_css()
st.title("🧪 FormulaAgents · 自进化配方算法竞技场")
st.caption("赛道 **SECTION 9 · 多 Agent 蜂群协作** ｜ 数据：合成/脱敏胶粘剂配方（PG18+pgvector）"
           "｜ 数值唯一来源：确定性评测内核（Agent 不自报分数）")

# ---------- 评分四维实时对表横幅 ----------
ov = get("/api/overview")
ct = get("/api/contrast")
tr = get("/api/trust")
ag = get("/api/agents")

if ct:
    sw, bl = ct["swarm"], ct["blind"]
    chips = [
        stat_chip("涌现性 · 协作>单Agent", f"{sw['best_mae']:.3f}", APPLE["green"],
                  f"拒谎报 {ct['emergent_gap']:.3f} · 否决率 {sw['gate_reject_rate']:.0%}"),
        stat_chip("协议通信 · 非硬编码", len((ag or {}).get("registry", [])), APPLE["blue"],
                  (ag or {}).get("protocol_version", "")),
        stat_chip("容错安全 · 失败回退", sw["failed"], APPLE["red"],
                  f"拒绝率 {sw['gate_reject_rate']:.0%}"),
        stat_chip("即插即用 · 可扩展", (ov or {}).get("genes", 0), APPLE["orange"],
                  "个 Gene 注册即用"),
        stat_chip("监控可视化", (ov or {}).get("events", 0), APPLE["gray"],
                  f"Capsule {(ov or {}).get('capsules_passed', 0)}/"
                  f"{(ov or {}).get('capsules', 0)}"),
    ]
    grid = ("<div style='display:grid;grid-template-columns:repeat(5,1fr);gap:12px'>"
            + "".join(chips) + "</div>")
    st.markdown(grid, unsafe_allow_html=True)

if ct and ct["champions_per_round"]:
    trail = " → ".join(f"R{r['round']}·{r['family']}({r['holdout_mae']:.2f})"
                       for r in ct["champions_per_round"])
    st.success(f"🏆 冠军轨迹（Integrator 冻结择优）：{trail}")

st.divider()
tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["总览", "排行榜", "蜂群看板", "容错与安全", "进化曲线"])

# ---------- 总览 ----------
with tab1:
    st.subheader("总览：防自欺五律 · 信任边界")
    if tr:
        t = tr["agent_ro"]
        st.table(pd.DataFrame([{
            "信任边界": "只读账号 agent_ro",
            "mat.* 可读": "✅" if t["ro_reads_mat"] else "❌",
            "evo.* 禁写（防伪造 Capsule）": "✅" if t["ro_writes_event"] is False and t["ro_writes_capsule"] is False else "❌",
            "Gate 现状": f"通过 {tr['capsule']['passed']} · 否决 {tr['capsule']['failed']}（负例照入库）",
        }], ))
    if ov:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Capsule 单测记录", ov["capsules"])
        c2.metric("通过闸门", ov["capsules_passed"])
        c3.metric("Gene 候选", ov["genes"])
        c4.metric("累计试验次数", ov["trials_total"], "试得越多，胜者越被高估")
    if ct:
        st.markdown("**涌现性对比（同一批实测 Capsule，非伪造）**")
        rows = [{
            "选手": f"{r['family']}（单Agent代表）", "golden MAE": round(r["best"], 3),
            "通过闸门": "✅", "Capsule": r["n"], "试验代价": r["trials"],
        } for r in ct["per_gene"]]
        rows.append({
            "选手": f"单Agent 盲选 {ct['blind']['family']}（全量最小数）",
            "golden MAE": round(ct["blind"]["best_mae"], 3), "通过闸门": "❌",
            "Capsule": ct["swarm"]["total"], "试验代价": ct["swarm"]["trials_total"],
        })
        rows.append({
            "选手": "🧠 蜂群（Integrator 冻结择优）",
            "golden MAE": round(ct["swarm"]["best_mae"], 3), "通过闸门": "✅",
            "Capsule": ct["swarm"]["total"], "试验代价": ct["swarm"]["trials_total"],
        })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        bl = ct["blind"]
        st.caption(f"单 Agent 盲选最优 {bl['best_mae']:.3f}（{bl['family']}，R{bl['round']}）"
                   f"golden 泛化不足被否决（{bl['fail_reason']}）；蜂群以 {ct['swarm']['gate_reject_rate']:.0%} "
                   f"否决率冻结出可信冠军 {ct['swarm']['best_mae']:.3f} —— "
                   f"协作产出不是『纸面最小数』，而是『可信最小数』，全程可审计。")
    st.info("⚠️ 数据为合成/脱敏数据，展示『流程』与『决策质量』，不声称胶粘剂真实精度。")

# ---------- 排行榜 ----------
with tab2:
    st.subheader("排行榜（golden 冻结，只评一次）")
    col_l, col_r = st.columns([2, 1])
    with col_l:
        dhash = st.text_input("dataset_hash 过滤（留空=全部）", key="lb_dhash")
    with col_r:
        rnd = st.selectbox("round 过滤", [None] + (ov or {}).get("rounds", []),
                           format_func=lambda x: "全部" if x is None else f"第 {x} 轮",
                           key="lb_round")
        only_pass = st.checkbox("只看通过闸门", value=True, key="lb_pass")
    rows = (get("/api/leaderboard", dhash=dhash or None, round_no=rnd, passed_only=only_pass) or {}).get("rows", [])
    if rows:
        df = pd.DataFrame([{
            "family": r["family"], "round": r["round"], "dev_mae": r["dev_mae"],
            "gold_mae": r["holdout_mae"], "coverage": r["coverage"],
            "trials": r["trials_total"], "status": "✅" if r["passed"] else "❌",
            "fail_reason": r["fail_reason"] or "",
            "config": json.dumps(r["config"], ensure_ascii=False),
        } for r in rows])
        st.dataframe(df, hide_index=True, use_container_width=True)
    else:
        st.caption("（无数据）")

# ---------- 蜂群看板（协议通信 + 5s 自动刷新监控）----------
with tab3:
    st.subheader("蜂群看板：Supervisor → Champion×N → Arbiter → Red-team → Integrator → Scribe")
    runs = (get("/api/runs") or {}).get("runs", [])
    run_ids = [r["run_id"] for r in runs]
    sel = st.selectbox("选择一轮运行（自动每 5 秒刷新）", run_ids or ["（先跑 swarm.py）"])
    if ag:
        with st.expander("📡 协议与通信：角色注册表（Agent 发现/能力/上下文传递）", expanded=False):
            st.dataframe(pd.DataFrame([{
                "角色": a["role"], "被发现方式": a["discover"], "能力(tools)": a["tools"],
                "协议事件": a["protocol"], "职责": a["desc"],
            } for a in ag["registry"]]), hide_index=True, use_container_width=True)
            st.caption(ag["protocol_version"])
            sp = pd.DataFrame([{"actor": s["actor"], "发言事件": s["n"],
                                "最后活跃": s["last_at"]} for s in ag["speech"]])
            st.dataframe(sp, hide_index=True, use_container_width=True)

    @st.fragment(run_every=5)
    def swarm_feed(run_id):
        evs = (get("/api/events", run_id=run_id, limit=600) or {}).get("events", [])
        st.caption(f"事件流 {len(evs)} 条 · 自动刷新中（5s）")
        if not evs:
            st.caption("该 run 暂无事件")
        for e in evs:
            actor, kind = e["actor"], e["kind"]
            color = ACTOR_COLOR.get(actor.split(":")[0], "#666666")
            p = e["payload"] or {}
            if kind == "argue":
                body = p.get("text", "")
            elif kind == "evaluate":
                body = (f"config={json.dumps(p.get('config'), ensure_ascii=False)[:90]}"
                        f" dev_mae={p.get('dev_mae')}")
            elif kind == "gate":
                body = (f"family={p.get('family')} gold_mae={p.get('holdout_mae')} "
                        f"passed={p.get('passed')}" + (f" reason={p.get('reason')}" if p.get("reason") else ""))
            else:
                body = json.dumps(p, ensure_ascii=False)[:160]
            st.markdown(bubble(f"**{actor}** · {kind}\n{body}", role="agent",
                               avatar="🐝", accent=color), unsafe_allow_html=True)
    swarm_feed(sel)

# ---------- 容错与安全 ----------
with tab4:
    st.subheader("容错与安全：可解释失败 + 信任边界 + 降级回退")
    fl = get("/api/failures")
    if fl:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**失败归因分布（负例照入库，归因可解释）**")
            dist = pd.DataFrame([{"fail_reason": r["reason"], "次数": r["n"]}
                                 for r in fl["distribution"]])
            if not dist.empty:
                fig, ax = plt.subplots(figsize=(7, 3.2))
                ax.barh(dist["fail_reason"], dist["次数"], color="#d62728", alpha=.85)
                ax.set_xlabel("次数"); ax.set_title("gate 失败原因分布", fontsize=11)
                ax.grid(alpha=.3, axis="x")
                st.pyplot(fig)
            else:
                st.caption("（暂无失败记录）")
        with c2:
            st.markdown("**最近失败回退样本（Arbiter/Integrator 否决 → 蜂群继续）**")
            st.dataframe(pd.DataFrame([{
                "round": r["round"], "family": r["family"], "gold_mae": round(r["holdout_mae"], 3) if r["holdout_mae"] else None,
                "fail_reason": r["fail_reason"], "trials": r["trials_total"], "time": r["created_at"],
            } for r in fl["latest"]]), hide_index=True, use_container_width=True)
    if tr:
        t = tr["agent_ro"]
        st.markdown("**信任边界实况**")
        st.table(pd.DataFrame([{
            "检查项": "只读账号 agent_ro 存在", "结果": "✅" if t["has_ro"] else "❌"},
            {"检查项": "mat.* 数据只读给 Agent/页面", "结果": "✅" if t["ro_reads_mat"] else "❌"},
            {"检查项": "evo.* 事件/Capsule 禁写（防自报分数）", "结果": "✅" if not (t["ro_writes_event"] or t["ro_writes_capsule"]) else "❌"},
        ]), hide_index=True)
        st.caption("降级策略演示：任一 Champion 被 gate 否决 → fail_reason 落库、Arbiter 另择配置继承继续（事件流可回放）。")
    st.caption("Gene 带 avoid 名单（禁用红区策略）数量：" + str((tr or {}).get("genes_with_avoid", 0)))

# ---------- 进化曲线 ----------
with tab5:
    st.subheader("进化曲线（策略资产在数据体制间的进化，非模型权重）")
    cd = st.text_input("dataset_hash 过滤（建议填：限定同一数据体制）", key="cv_dhash")
    cv = get("/api/curve", dhash=cd or None)
    if cv and cv["per_round"]:
        pr = cv["per_round"]
        fig, ax = plt.subplots(1, 3, figsize=(15, 4))
        xs = [r["round"] for r in pr]
        ax[0].plot(xs, [r["best_mae"] for r in pr], "o-", label="最优 golden MAE")
        ax[0].plot(xs, [r["mean_mae"] for r in pr], "s--", label="平均 golden MAE")
        ax[0].set_xlabel("round"); ax[0].set_ylabel("MAE"); ax[0].set_title("Evolution curve")
        ax[0].legend(); ax[0].grid(alpha=.3)
        fam = cv["per_family"]
        for f in sorted({r["family"] for r in fam}):
            pts = [(r["round"], r["best_mae"]) for r in fam if r["family"] == f]
            ax[1].plot([p[0] for p in pts], [p[1] for p in pts], "o-", label=f)
        ax[1].set_xlabel("round"); ax[1].set_ylabel("best MAE"); ax[1].set_title("Per family")
        ax[1].legend(fontsize=8); ax[1].grid(alpha=.3)
        ax[2].bar(xs, [r["trials"] for r in pr], color="#4C78A8")
        ax[2].set_xlabel("round"); ax[2].set_ylabel("trials"); ax[2].set_title("Search cost")
        ax[2].grid(alpha=.3, axis="y")
        st.pyplot(fig)
        st.dataframe(pd.DataFrame([{
            "round": r["round"], "best_mae": round(r["best_mae"], 3) if r["best_mae"] else None,
            "mean_mae": round(r["mean_mae"], 3) if r["mean_mae"] else None,
            "trials": r["trials"], "capsules": r["n"], "pass_rate": round(r["pass_rate"], 3),
        } for r in pr]), hide_index=True)
    else:
        st.caption("（先跑 arena.py / swarm.py 产生 Capsule）")

st.caption("FormulaAgents · SECTION 9 赛道演示页 · 全部数据来自 PG evo.*（合成/脱敏）")
