#!/usr/bin/env python3
"""方案A · 编排层（H3–5）：LangGraph 蜂群

Supervisor → 并行 Champion×N → Arbiter → Red-team → Integrator → Scribe

红线：数值一律来自 src/fsarena.py 的确定性内核；LLM 只产出论证/质疑文本。
每轮最多重跑一次（Red-team 动作触发），避免无限循环。

用法：
  python swarm.py --round 1 --budget 10
  python swarm.py --round 2 --inherit --budget 3 --system epoxy
  python swarm.py --check                       # 退出码 0=闸门通过
  OPENAI_API_KEY=sk-xxx python swarm.py --llm   # 接入 LLM 论证（可选）
"""
import argparse
import operator
import os
import sys
import uuid
from pathlib import Path
from typing import Annotated, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from fsarena import (evaluate_trials, golden_evaluate, pick_finalists, prepare,
                     redteam_checks)
from fsdb import (append_event, connect, env_fingerprint, insert_capsule,
                  load_warm_capsules, upsert_gene)
from fsgep import signals_for
from fsmodels import CORE_FAMILIES, GENE_SIGNALS, SPACE
from fsllm import NO_NUMBER_RULE, chat
from fsselector import select

OOD_MAX_UNSEEN = 0.30
# Pugh 决策矩阵权重：为什么赢（不是 R² 最高即冠军）
PUGH_W = {"dev_mae": 0.40, "data_fit": 0.20, "overfit_risk": 0.15,
          "uq": 0.15, "explain": 0.10}
# 可解释性 / 原生 UQ 能力（化学先验，不是数据里学出来的）
EXPLAIN = {"ridge": 1.0, "pls": 1.0, "gp": 0.6, "rf": 0.4, "gbr": 0.4, "xgb": 0.4}
NATIVE_UQ = {"gp": 1.0, "ridge": 0.6, "pls": 0.6, "rf": 0.4, "gbr": 0.4, "xgb": 0.4}


class SwarmState(TypedDict):
    run_id: str
    goal: str
    round: int
    budget: int
    inherit: bool
    target: str
    system: str
    feature_mode: str
    seed: int
    folds: int
    families: list
    ctx: dict
    warm: list
    messages: Annotated[list, operator.add]
    trials: Annotated[list, operator.add]
    finalists: list
    red: dict
    findings: list
    actions: list
    exclude_unseen: bool
    selector: str
    request_rerun: bool
    rerun: int
    results: list
    passed: bool


# ---------------------------------------------------------------- 节点
def supervisor(state: SwarmState):
    ctx = prepare(state["round"], state["target"], state["system"], state["feature_mode"])
    with connect() as c, c.cursor() as cur:
        append_event(cur, state["run_id"], "supervisor", "start",
                     {"round": state["round"], "goal": state["goal"], "target": state["target"],
                      "system": state["system"], "n_dev": ctx["dev_end"],
                      "n_gold": ctx["gold_end"] - ctx["dev_end"],
                      "dataset_hash": ctx["dhash"], "budget": state["budget"],
                      "families": state["families"]})
        warm, source = [], None
        if state["inherit"]:
            if state.get("selector", "auto") in ("auto", "gep"):
                warm = select(signals_for(ctx["dev_end"], state["target"], state["system"]),
                              limit=6, system=state["system"])[1]
                source = "evomap-gep"
            if not warm:
                warm = load_warm_capsules(cur, state["round"], ctx["dhash"],
                                          system=state["system"]) or \
                       load_warm_capsules(cur, state["round"], None,
                                          system=state["system"])
                source = "pg-fallback"
            append_event(cur, state["run_id"], "selector", "inherit",
                         {"source": source,
                          "warm": [{"family": w["family"], "config": w["config"]} for w in warm]})
    msg = (f"🎯 Supervisor「目标={state['goal']}；n={ctx['X'].shape[0]}，dev={ctx['dev_end']}，"
           f"golden={ctx['gold_end'] - ctx['dev_end']}；时间前向切分，预算 {state['budget']}/族"
           f"{'；继承上轮 ' + str(len(warm)) + ' 个通过配置' if warm else ''}」")
    return {"ctx": ctx, "warm": warm, "messages": [msg]}


def make_champion(family):
    def node(state: SwarmState):
        ctx = state["ctx"]
        trials = evaluate_trials([family], state["budget"], state["warm"], ctx,
                                 state["folds"], state["seed"])
        sig = GENE_SIGNALS[family]
        n = ctx["dev_end"]
        fit = "样本量匹配" if sig["n_min"] <= n <= sig["n_max"] else "样本量不匹配"
        best = min(t["dev_mae"] for t in trials)
        hist = [w for w in state["warm"] if w["family"] == family]
        base = (f"🧬 {family} Agent「n={n} vs 适用区间[{sig['n_min']},{sig['n_max']}] → {fit}；"
                f"本族本轮最佳 dev_mae={best:.3f}"
                f"{'；继承 ' + str(len(hist)) + ' 个历史通过配置' if hist else ''}」")
        text = chat("你是胶粘剂配方建模的算法代言人，为你的算法族争取入围名额。",
                    f"算法族={family}，样本量={n}，是否原生支持不确定性={sig['needs_uq']}，"
                    f"历史通过配置数={len(hist)}。{NO_NUMBER_RULE}", base)
        with connect() as c, c.cursor() as cur:
            append_event(cur, state["run_id"], f"champion:{family}", "argue", {"text": text})
            for t in trials:
                append_event(cur, state["run_id"], f"champion:{family}", "evaluate",
                             {"config": t["config"], "dev_mae": round(t["dev_mae"], 4)})
        return {"trials": trials, "messages": [text]}
    return node


def arbiter(state: SwarmState):
    trials = state["trials"]
    n = state["ctx"]["dev_end"]
    maes = [t["dev_mae"] for t in trials]
    lo, hi = min(maes), max(maes)

    def score(t):
        fam, cfg = t["family"], t["config"]
        dev_s = 1.0 - (t["dev_mae"] - lo) / (hi - lo) if hi > lo else 1.0
        sig = GENE_SIGNALS[fam]
        data_fit = 1.0 if sig["n_min"] <= n <= sig["n_max"] else 0.4
        # 过拟合风险：容量越大风险越高（树数 × 深度）
        cap = (cfg.get("n", 1) or 1) * (cfg.get("d") or 1)
        overfit = 1.0 if cap <= 400 else (0.6 if cap <= 1600 else 0.3)
        return (PUGH_W["dev_mae"] * dev_s + PUGH_W["data_fit"] * data_fit
                + PUGH_W["overfit_risk"] * overfit + PUGH_W["uq"] * NATIVE_UQ.get(fam, 0.5)
                + PUGH_W["explain"] * EXPLAIN.get(fam, 0.5))

    ranked = sorted(trials, key=score, reverse=True)
    finalists, seen = [], set()
    for t in ranked:                              # Pugh 打分后仍保多样性：每族至多 1 个
        if t["family"] not in seen and len(finalists) < 3:
            finalists.append(t); seen.add(t["family"])
    board = " | ".join(f"{t['family']}:{score(t):.2f}" for t in finalists)
    msg = (f"⚖️ Arbiter「Pugh 矩阵（dev_mae 0.4 / 数据匹配 0.2 / 过拟合风险 0.15 / UQ 0.15 / 可解释 0.1）"
           f"→ 入围 {board}」")
    with connect() as c, c.cursor() as cur:
        append_event(cur, state["run_id"], "arbiter", "select",
                     {"finalists": [{"family": f["family"], "config": f["config"],
                                     "score": round(score(f), 3)} for f in finalists]})
    return {"finalists": finalists, "messages": [msg]}


def red_team(state: SwarmState):
    red = redteam_checks(state["ctx"])
    findings, actions = [], []
    if red["duplicates"]:
        findings.append(f"发现 {red['duplicates']} 条 dev/golden 重复配方（时间泄漏）")
        actions.append("中止：清洗重复后再评估")
    if len(state["trials"]) > 20:
        findings.append(f"本轮共试 {len(state['trials'])} 次，胜者 dev_mae 存在选择性过拟合高估")
        actions.append("声明试验总次数，报告里暴露该偏差")
    if red["ood_ratio"] > 0.2:
        findings.append(f"{red['ood_ratio']:.0%} 的 golden 样本落在 dev 分布之外")
        actions.append("结论不外推，按体系分域建模")
    if red["unseen_share"] > OOD_MAX_UNSEEN:
        findings.append(f"golden 中 {red['families_unseen_in_dev']} 在 dev 从未出现（占 {red['unseen_share']:.0%}）")
        actions.append("剔除域外样本后重评（第五律：域外硬拒答）")
    text = chat("你是科学红队，只做质疑与提出动作，不产出数值。",
                f"发现：{findings}；已有动作：{actions}。{NO_NUMBER_RULE}",
                f"🔴 Red Team「{len(findings)} 条发现 → {len(actions)} 条动作」")
    with connect() as c, c.cursor() as cur:
        append_event(cur, state["run_id"], "red_team", "critique",
                     {"findings": findings, "actions": actions, "checks": red})
    return {"red": red, "findings": findings, "actions": actions, "messages": [text]}


def integrator(state: SwarmState):
    red = state["red"]
    results = []
    for f in state["finalists"]:
        r = golden_evaluate(f, state["ctx"], red, len(state["trials"]),
                            exclude_unseen=state.get("exclude_unseen", False),
                            ood_max_unseen=OOD_MAX_UNSEEN)
        if r:
            results.append(r)
    request = False
    if red.get("duplicates"):
        results = []                               # 泄漏 → 硬失败
    elif not any(r["passed"] for r in results) and state["rerun"] < 1 \
            and red.get("unseen_share", 0) > OOD_MAX_UNSEEN:
        request = True                             # 应用 Red-team 动作后重评（只重跑一次）
    msg = (f"🧩 Integrator「golden 冻结，只评一次；"
           + (f"按 Red-team 动作剔除域外样本 {max((r['excluded_ood'] for r in results), default=0)} 条后重评"
              if state.get("exclude_unseen") else "首次评估")
           + f"；通过 {sum(bool(r['passed']) for r in results)}/{len(results)}」")
    with connect() as c, c.cursor() as cur:
        append_event(cur, state["run_id"], "integrator", "gate",
                     {"results": [{k: (round(v, 4) if isinstance(v, float) else v)
                                   for k, v in r.items() if k != "config"} for r in results],
                      "exclude_unseen": state.get("exclude_unseen", False)})
    return {"results": results, "request_rerun": request,
            "exclude_unseen": True if request else state.get("exclude_unseen", False),
            "rerun": state["rerun"] + (1 if request else 0), "messages": [msg]}


def scribe(state: SwarmState):
    env = env_fingerprint()
    ctx = state["ctx"]
    with connect() as c, c.cursor() as cur:
        for r in state["results"]:
            gid = f"gene_{r['family']}_small_n"
            upsert_gene(cur, {"id": gid, "family": r["family"],
                              "signals_match": dict(GENE_SIGNALS[r["family"]], target=state["target"]),
                              "strategy": {"space": SPACE[r["family"]], "features": ctx["feats"]},
                              "avoid": ["random_split", "tune_on_golden"],
                              "validation": "node run_eval.js --check"})
            insert_capsule(cur, {"gene_id": gid, "run_id": state["run_id"],
                                 "round": state["round"], "system": state["system"],
                                 "config": r["config"],
                                 "dataset_hash": ctx["dhash"], "split": "time_forward",
                                 "dev_mae": r["dev_mae"], "holdout_mae": r["holdout_mae"],
                                 "coverage": r["coverage"], "trials_total": r["trials_total"],
                                 "passed": r["passed"], "fail_reason": r["fail_reason"],
                                 "env_fingerprint": env})
            append_event(cur, state["run_id"], "scribe",
                         "promote" if r["passed"] else "reject",
                         {"gene_id": gid, "family": r["family"],
                          "holdout_mae": round(r["holdout_mae"], 4),
                          "fail_reason": r["fail_reason"]})
    passed = any(r["passed"] for r in state["results"])
    msg = (f"📝 Scribe「写入 {len(state['results'])} 条 Capsule（负例同样入库）+ "
           f"{len(state['finalists'])} 个 Gene 候选；本轮{'有冠军通过闸门' if passed else '无人通过闸门'}」")
    return {"passed": passed, "messages": [msg]}


def route_after_integrator(state: SwarmState):
    return "integrator" if state.get("request_rerun") else "scribe"


def build_graph(families):
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(SwarmState)
    g.add_node("supervisor", supervisor)
    for fam in families:
        g.add_node(f"champion_{fam}", make_champion(fam))
    g.add_node("arbiter", arbiter)
    g.add_node("red_team", red_team)
    g.add_node("integrator", integrator)
    g.add_node("scribe", scribe)

    g.add_edge(START, "supervisor")
    for fam in families:
        g.add_edge("supervisor", f"champion_{fam}")
        g.add_edge(f"champion_{fam}", "arbiter")
    g.add_edge("arbiter", "red_team")
    g.add_edge("red_team", "integrator")
    g.add_conditional_edges("integrator", route_after_integrator,
                            {"integrator": "integrator", "scribe": "scribe"})
    g.add_edge("scribe", END)
    return g.compile()


def run_swarm(round_no=1, inherit=False, budget=4, seed=0, target="lap_shear_strength",
              system=None, feature_mode="both", folds=4, with_xgb=False, selector="auto",
              goal="提升环氧胶粘剂剪切强度预测精度与不确定性质量", verbose=True):
    families = CORE_FAMILIES + (["xgb"] if with_xgb else [])
    init = {
        "run_id": f"s{round_no}-{uuid.uuid4().hex[:6]}", "goal": goal, "round": round_no,
        "budget": budget, "inherit": inherit, "target": target, "system": system,
        "feature_mode": feature_mode, "seed": seed, "folds": folds, "families": families,
        "ctx": {}, "warm": [], "messages": [], "trials": [], "finalists": [], "red": {},
        "findings": [], "actions": [], "exclude_unseen": False, "selector": selector,
        "request_rerun": False,
        "rerun": 0, "results": [], "passed": False,
    }
    out = build_graph(families).invoke(init)
    if verbose:
        print(f"\n===== 蜂群实时看板 [{out['run_id']}] =====")
        for m in out["messages"]:
            print(" ", m)
        print("\n===== Arena 排行榜（golden 冻结，只评一次）=====")
        print(f"{'Model':<8}{'devMAE':>8}{'goldMAE':>9}{'R2':>7}{'Stab':>7}{'UQ(cov)':>9}{'±CI':>7}  Status")
        for r in sorted(out["results"], key=lambda r: r["holdout_mae"]):
            st = "🏆 PASS" if r["passed"] else f"FAIL:{r['fail_reason']}"
            print(f"{r['family']:<8}{r['dev_mae']:>8.3f}{r['holdout_mae']:>9.3f}{r['r2']:>7.2f}"
                  f"{r['stability']:>7.2f}{r['coverage']:>9.2f}{r['interval_half_width']:>7.2f}  {st}")
        if out["results"] and any(r["excluded_ood"] for r in out["results"]):
            print(f"  （已按 Red-team 动作剔除域外样本 {max(r['excluded_ood'] for r in out['results'])} 条后重评）")
        print("\n===== Scientific Critique =====")
        for i, (f, a) in enumerate(zip(out["findings"], out["actions"]), 1):
            print(f"  {i}. 发现：{f}\n     → 动作：{a}")
        if not out["findings"]:
            print("  （本轮未触发质疑）")
        print("⚠️ 数据为合成/脱敏数据，不声称胶粘剂真实精度")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--inherit", action="store_true")
    ap.add_argument("--budget", type=int, default=4)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--target", default="lap_shear_strength")
    ap.add_argument("--system", default=None)
    ap.add_argument("--features", default="both", choices=["cat", "raw", "both"])
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--with-xgb", action="store_true")
    ap.add_argument("--selector", default="auto", choices=["auto", "gep", "pg"],
                    help="继承来源：auto=优先 EvoMap GEP 资产 / gep=只用资产 / pg=只用 PG")
    ap.add_argument("--llm", action="store_true", help="允许调用 LLM 生成论证（需 OPENAI_API_KEY）")
    ap.add_argument("--check", action="store_true", help="退出码 0=闸门通过")
    a = ap.parse_args()
    if not a.llm:
        os.environ["FS_NO_LLM"] = "1"
    out = run_swarm(a.round, a.inherit, a.budget, a.seed, a.target, a.system,
                    a.features, a.folds, a.with_xgb, a.selector)
    if a.check:
        sys.exit(0 if out["passed"] else 1)
