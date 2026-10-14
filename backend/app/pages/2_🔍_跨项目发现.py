"""方案 C · 四级关联库 + 跨项目发现（Streamlit 页面）

三问三答（每条答案必须带可点开的证据链）：
  1. 这批原料/这票货到底被哪个项目、哪支配方用了？
  2. 有没有其他项目用过类似的体系？结果如何？
  3. 这个体系性能突然下降，可能原因是什么？
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
import pandas as pd
import streamlit as st
from app.theme import *

API = "http://localhost:8000"
st.set_page_config(page_title="跨项目发现", layout="wide", page_icon="🔍")
inject_apple_css()
st.title("🔍 方案 C · 四级关联库 · 跨项目发现")
st.caption("原料 → 中间体 → 配方 → 性能 ｜ 答案必须带溯源；无证据时直接说「库里没有」")


def get(path, **params):
    try:
        r = httpx.get(f"{API}{path}", params={k: v for k, v in params.items() if v is not None},
                      timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"API 不可达 {path}: {e.__class__.__name__}")
        return None


def post(path, payload):
    try:
        r = httpx.post(f"{API}{path}", json=payload, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"API 不可达 {path}: {e.__class__.__name__}")
        return None


ov = get("/api/discover/overview")
if ov:
    c = ov["counts"]
    chips = [
        stat_chip("原料", c["materials"], APPLE["blue"]),
        stat_chip("配方", c["formulations"], APPLE["green"]),
        stat_chip("中间体", c["intermediates"], APPLE["orange"]),
        stat_chip("性能读数", c["performances"], APPLE["purple"]),
        stat_chip("批次快照", c["lot_links"], APPLE["teal"]),
        stat_chip("采购订单", c["pos"], APPLE["pink"]),
        stat_chip("问答审计", c["answers"], APPLE["gray"]),
    ]
    grid = ("<div style='display:grid;grid-template-columns:repeat(7,1fr);gap:10px'>"
            + "".join(chips) + "</div>")
    st.markdown(grid, unsafe_allow_html=True)
    with st.expander("质量守恒体检（原料级展开 Σ=100±0.5）", expanded=False):
        st.dataframe(pd.DataFrame(ov["mass_balance"]), hide_index=True, use_container_width=True)
        st.caption("CONCRETE 为 kg/m³ 配料单基准（已标注 basis），不与质量百分比混算")

st.divider()
tab1, tab2, tab3, tab4 = st.tabs(["问答（带溯源）", "谱系查询", "归因候选", "合规"])

# ---------------- 问答 ----------------
with tab1:
    st.subheader("跨项目发现问答（检索 + 规则归因，LLM 只做措辞）")
    demo = st.selectbox("示例问题", [
        "这批环氧树脂 E51 到底被哪些研发项目用了？",
        "有没有其他项目用过类似的高填料低增韧体系？结果如何？",
        "FS-EP-0001 的剪切强度突然下降，可能原因是什么？",
        "FS-EP-0001 的 VOC 和 REACH 合规情况如何？",
        "PO-20260923-001 这批货用到哪个项目、哪支配方上了？",
    ])
    q = st.text_area("问题", value=demo, height=68)
    if st.button("提问", type="primary"):
        out = post("/api/ask", {"question": q, "llm": False})
        if out:
            convo = chat_turn(q, out["answer"], intent=out.get("intent"),
                              trace=out.get("trace_id"))
            st.markdown(convo, unsafe_allow_html=True)
            if out.get("engine"):
                st.caption(f"engine={out.get('engine')}")
            st.markdown(section_title("证据链（可点开）",
                                      "每条答案都带着能回放的 SQL 与命中行"),
                        unsafe_allow_html=True)
            for i, e in enumerate(out["evidence"], 1):
                with st.expander(f"{i}. {e['kind']} ← {e['source']}（{e['n_rows']} 行）",
                                 expanded=(i == 1)):
                    st.code(e["sql"], language="sql")
                    if e.get("note"):
                        st.caption(e["note"])
                    if e["rows"]:
                        st.dataframe(pd.DataFrame(e["rows"]), hide_index=True,
                                     use_container_width=True)
    aud = (get("/api/audit", limit=10) or {}).get("rows", [])
    if aud:
        with st.expander("最近问答审计（每条都写库，可复盘）", expanded=False):
            for a in aud:
                st.markdown(f"- `{a['trace_id']}` [{a['intent']}] {a['question']}")

# ---------------- 谱系 ----------------
with tab2:
    st.subheader("谱系查询：原料 / 批次 / 采购订单 / 相似配方")
    mode = st.radio("查询类型", ["原料 → 项目/配方", "批次 → 配方/性能",
                                "采购订单 → 项目/配方", "相似配方"], horizontal=True)
    if mode == "原料 → 项目/配方":
        code = st.text_input("原料代码或名称", value="E51")
        r = get("/api/lineage/material", q=code)
        if r:
            st.write(f"解析：{r['resolved'].get('match')} → "
                     f"{(r['resolved'].get('material') or {}).get('code')}")
            if r["rows"]:
                st.dataframe(pd.DataFrame(r["rows"]), hide_index=True, use_container_width=True)
                st.json(r["summary"])
            else:
                st.warning("库里没有该原料的投料记录")
    elif mode == "批次 → 配方/性能":
        lot = st.text_input("批次号", value="L2489-01")
        r = get("/api/lineage/lot", lot=lot)
        if r and r["rows"]:
            st.dataframe(pd.DataFrame(r["rows"]), hide_index=True, use_container_width=True)
        else:
            st.warning("库里没有该批次的投料记录")
    elif mode == "采购订单 → 项目/配方":
        po = st.text_input("采购订单号", value="PO-20260923-001")
        r = get("/api/lineage/po", po=po)
        if r and r["rows"]:
            st.caption("只读镜像：不提供任何回写 ERP 的路径")
            st.dataframe(pd.DataFrame(r["rows"]), hide_index=True, use_container_width=True)
        else:
            st.warning("库里没有该采购订单")
    else:
        code = st.text_input("配方代码", value="FS-EP-0001")
        r = get("/api/lineage/similar", code=code, k=8)
        if r and r["rows"]:
            rows = []
            for s in r["rows"]:
                lss = next((p["value"] for p in s["performance"]
                            if p["test_item"] == "lap_shear_strength"), None)
                rows.append({"配方": s["code"], "体系": s["system"],
                             "余弦相似度": round(float(s["cosine_similarity"]), 4),
                             "剪切强度": float(lss) if lss is not None else None})
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        else:
            st.warning("库里没有可比配方")

# ---------------- 归因 ----------------
with tab3:
    st.subheader("性能归因：候选排序 + 证据链（规则打分，不是 LLM 直觉）")
    code = st.text_input("配方代码", value="FS-EP-0001", key="attr_code")
    r = get("/api/lineage/attribute", code=code)
    if r:
        if r.get("position"):
            p = r["position"]
            st.info(f"{code} 的 {r['target']} = {p['value']}，处于同体系 {p['percentile']:.0%} 分位"
                    f"（中位数 {p['median']}，p25 {p['p25']}，p75 {p['p75']}，n={p['peers_n']}）")
        if r.get("candidates"):
            for i, c in enumerate(r["candidates"][:8], 1):
                with st.expander(f"{i}. {c['cause']}（证据强度 {c['score']}）", expanded=(i <= 3)):
                    st.write(c["detail"])
                    st.write(f"→ 下一步：{c['next_action']}")
                    st.json(c["evidence"])
        else:
            st.warning("库里没有该配方的异常证据（不做无证据的猜测）")

# ---------------- 合规 ----------------
with tab4:
    st.subheader("合规（规则为主；LLM 不做判定）")
    code = st.text_input("配方代码", value="FS-EP-0001", key="comp_code")
    r = get("/api/lineage/compliance", code=code)
    if r and r["rows"]:
        row = r["rows"]
        st.metric("VOC 估算", f"{float(row['voc_g_l_est']):.1f} g/L")
        st.write(f"SVHC 命中：{row['svhc_hit']} ｜ 受限物质：{row['restricted_hit']} ｜ "
                 f"缺合规数据的原料：{row['materials_missing_compliance']} 种")
    rules = (get("/api/compliance/rules") or {}).get("rows", [])
    if rules:
        st.markdown("**规则表**")
        st.dataframe(pd.DataFrame(rules), hide_index=True, use_container_width=True)
    mats = (get("/api/compliance/materials") or {}).get("rows", [])
    if mats:
        with st.expander("原料合规数据（source 列标明了数据出处）", expanded=False):
            st.dataframe(pd.DataFrame(mats), hide_index=True, use_container_width=True)
            st.caption("synthetic_estimate = 演示估算值，不可用于真实合规判定")

st.caption("FormulaAgents · 方案 C · 只读接入；采购订单为 ERP 只读镜像，无任何回写路径")
