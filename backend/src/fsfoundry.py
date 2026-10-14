"""方案 B · 虚拟配方铸造厂-lite 闭环编排（LangGraph）

假说 →（批判）→ BO 选点 → 虚拟实验 → 回灌 → 再进化 → 数据卡

红线（代码级强制，不是文档级承诺）：
  1. L4 禁止自动执行：实验计划默认 status='proposed'，只有传入 approver 才置 approved；
     未批准的 plan 写不进 evo.experiment_result（DB 触发器兜底）。
  2. 循环论证防线：模拟结果只进 evo.experiment_result(simulated=true)，
     永不写 mat.performance；对外精度只报冠军模型的留出集 MAE。
  3. 生成硬约束：质量守恒 / 可行域 / 当量比 / 法规，任一不满足 → 该候选被丢弃并记因。
  4. 适用域外硬拒答：OOD 候选不进建议清单（第五律）。
  5. LLM 只做解释与措辞，不产生任何数值、不做合规判定、不决定选点。
"""
from __future__ import annotations

import json
import operator
import uuid
from typing import Annotated, TypedDict

import numpy as np
from scipy.stats import pearsonr

from fsarena import prepare, redteam_checks
from fsbo import GPSurrogate, propose_batch
from fsdatacard import (applicability_report, build_datacard, nn_distances, save_datacard,
                        write_artifacts)
from fsdb import append_event, connect, env_fingerprint
from fsllm import NO_NUMBER_RULE, chat
from fsrecipe import (Refusal, build_candidate, build_feature_matrix, load_catalog,
                      mutate_candidate, sample_candidate, save_candidate)
from fsscaleup import apply as scale_apply
from fssim import VirtualLab, load_champion

# ---------------------------------------------------------------- SQL
HYP_INSERT = """
insert into evo.hypothesis(run_id, round, code, statement, mechanism, predicted_direction,
                           testable_prediction, evidence, prior, status)
values (%(run_id)s, %(round)s, %(code)s, %(statement)s, %(mechanism)s, %(predicted_direction)s,
        %(testable_prediction)s, %(evidence)s::jsonb, %(prior)s, %(status)s)
on conflict (run_id, round, code) do update set evidence = excluded.evidence
returning id
"""
HYP_UPDATE = "update evo.hypothesis set posterior=%(posterior)s, status=%(status)s where id=%(id)s"
CRIT_INSERT = """
insert into evo.hypothesis_critique(hypothesis_id, actor, attack, severity, verdict, evidence)
values (%(hypothesis_id)s, %(actor)s, %(attack)s, %(severity)s, %(verdict)s, %(evidence)s::jsonb)
returning id
"""
PLAN_INSERT = """
insert into evo.experiment_plan(run_id, round, candidate_id, target, acquisition, predicted,
                                ci_low, ci_high, interval_method, model_version, gene_id,
                                capsule_id, rationale, status)
values (%(run_id)s, %(round)s, %(candidate_id)s, %(target)s, %(acquisition)s, %(predicted)s,
        %(ci_low)s, %(ci_high)s, %(interval_method)s, %(model_version)s, %(gene_id)s,
        %(capsule_id)s::uuid, %(rationale)s, %(status)s)
returning id
"""
PLAN_APPROVE = """
update evo.experiment_plan set status='approved', approved_by=%(by)s, approved_at=now()
where id=%(id)s
"""
PLAN_REJECT = "update evo.experiment_plan set status='rejected' where id=%(id)s"
RESULT_INSERT = """
insert into evo.experiment_result(plan_id, simulated, source, scale, target, value,
                                  ci_low, ci_high, model_version, simulator_version,
                                  corrected, correction, payload)
values (%(plan_id)s, %(simulated)s, %(source)s, %(scale)s, %(target)s, %(value)s,
        %(ci_low)s, %(ci_high)s, %(model_version)s, %(simulator_version)s,
        %(corrected)s, %(correction)s::jsonb, %(payload)s::jsonb)
returning id
"""
PRED_INSERT = """
insert into evo.prediction_register(experiment_no, model_version, formulation_features,
                                    predicted, ci_low, ci_high, rationale)
values (%(experiment_no)s, %(model_version)s, %(features)s::jsonb, %(predicted)s,
        %(ci_low)s, %(ci_high)s, %(rationale)s)
on conflict (experiment_no) do nothing
returning id
"""
STRAT_INSERT = """
insert into evo.formula_strategy(run_id, round, system, target, strategy, score, holdout_mae,
                                 trials_total, passed, fail_reason, inherited_from, env_fingerprint)
values (%(run_id)s, %(round)s, %(system)s, %(target)s, %(strategy)s::jsonb, %(score)s,
        %(holdout_mae)s, %(trials_total)s, %(passed)s, %(fail_reason)s, %(inherited_from)s,
        %(env)s::jsonb)
returning id
"""
STRAT_LAST = """
select id, strategy, score, trials_total from evo.formula_strategy
where system = %(system)s and target = %(target)s and passed
  and round < %(round)s          -- 只能继承严格更早的轮次（禁止拿别轮/未来的结果当起点）
order by round desc, id desc limit 1
"""

# ---------------------------------------------------------------- 优化方向
# 铸造厂内部（BO / 策略择优 / 冠军贪心）一律按"越大越好"处理，因此对"越小越好"的指标
# 在进入优化器前统一取负号。**展示与落库仍用真实值**，不对外暴露这个内部符号。
# 漏掉这一步会直接导致反向优化：光刻胶分辨率会被"优化"得越来越差。
LOWER_IS_BETTER = {"resolution_nm", "ler_nm", "line_width_roughness_nm"}


def obj_sign(target):
    """返回优化方向符号：越小越好 → -1；越大越好 → +1。"""
    return -1.0 if target in LOWER_IS_BETTER else 1.0


# 适用域判据（ADR A4）：nn_distance > max(参考集留一法最近邻距离) → 域外硬拒答。
# 注意不是 p95：实测 p95 做硬拒答会把约 1/3 的真实留出集样本判成域外，故 p95 仅供严格档参考。
OOD_BOUNDARY = "max"
DEFAULT_POOL = 400               # 候选池大小（远大于单轮预算，BO 才有意义）


class FoundryState(TypedDict):
    run_id: str
    round: int
    target: str
    system: str
    budget: int
    pool_size: int
    seed: int
    feature_mode: str
    approver: str | None
    scale: str
    feed_simulated: bool
    ctx: dict
    champion: dict | None
    lab: object
    catalog: dict
    red: dict
    X_obs: object
    y_obs: object
    pool: list
    warm_start: float | None
    search_stats: dict
    ood: list
    chosen: Annotated[list, operator.add]
    plans: Annotated[list, operator.add]
    results: Annotated[list, operator.add]
    datacards: Annotated[list, operator.add]
    hypotheses: list
    critiques: list
    inherited: dict | None
    strategy_id: int | None
    rejected: Annotated[list, operator.add]
    messages: Annotated[list, operator.add]


# ---------------------------------------------------------------- 假说（证据驱动）
def _corr(x, y):
    if len(x) < 8 or np.std(x) == 0 or np.std(y) == 0:
        return None
    r, p = pearsonr(np.asarray(x, float), np.asarray(y, float))
    return {"r": round(float(r), 4), "p": round(float(p), 5), "n": int(len(x))}


def hypothesize(ctx, system):
    """从数据里算出候选机理假说（相关系数是算出来的，不是 LLM 编的）。"""
    df = ctx["meta"]
    y = ctx["yd"]
    feats = ctx["feats"]
    Xd = ctx["Xd"]

    def col(name):
        if name in feats:
            return Xd[:, feats.index(name)]
        return None

    cands = [
        ("H1_ee_deviation", "当量比偏离 1.0 会削弱交联网络完整性，降低剪切强度",
         "固化剂用量偏离化学计量 → 未反应环氧/胺残留 → 网络缺陷",
         "ee_deviation", "decrease"),
        ("H2_filler", "填料比例升高兴提高模量但引入应力集中，剪切强度非单调",
         "填料-基体界面成为裂纹起始点", "filler_pct", "non_monotonic"),
        ("H3_toughener", "增韧剂提高剥离强度但牺牲剪切强度（模量下降）",
         "橡胶相分散降低交联密度", "toughener_pct", "decrease"),
        ("H4_cure_temp", "固化温度升高提高转化率，但过高导致内应力",
         "固化度-内应力竞争", "cure_temp_c", "non_monotonic"),
        ("H5_epoxy_eq", "环氧当量密度（交联密度代理）升高提高强度",
         "交联密度 ↑ → 内聚强度 ↑", "epoxy_eq", "increase"),
    ]
    out = []
    for code, stmt, mech, feat, direction in cands:
        v = col(feat)
        if v is None:
            continue
        c = _corr(v, y)
        if not c:
            continue
        obs_dir = "increase" if c["r"] > 0 else "decrease"
        out.append({
            "code": code, "statement": stmt, "mechanism": mech,
            "predicted_direction": direction,
            "testable_prediction": f"在 {system} 体系内固定其他变量，单因素扫描 {feat} 验证方向",
            "evidence": {"feature": feat, "pearson": c, "observed_direction": obs_dir},
            "prior": 0.5, "status": "proposed",
        })
    # 批次异常假说（若有该标记）
    if "batch_anomaly" in getattr(df, "columns", []):
        ba = df["batch_anomaly"].fillna("").astype(str).iloc[:len(y)]
        mask = ba.isin(["true", "True", "1", "high"]).to_numpy()
        if mask.sum() >= 3 and (~mask).sum() >= 3:
            m1, m2 = float(np.mean(y[mask])), float(np.mean(y[~mask]))
            out.append({
                "code": "H6_batch", "statement": "原料批次异常（含水率/EEW 漂移）导致强度下降",
                "mechanism": "批次 EEW 漂移 → 实际当量比偏离 → 固化不完全",
                "predicted_direction": "decrease",
                "testable_prediction": "同配方换批次重测，比较 EEW 实测值与强度",
                "evidence": {"n_anomaly": int(mask.sum()), "mean_anomaly": round(m1, 3),
                             "mean_normal": round(m2, 3), "delta": round(m1 - m2, 3)},
                "prior": 0.5, "status": "proposed",
            })
    return out


def critique(hyp, ctx, red):
    """规则化批判：小样本 / 弱效应 / 共线性 / 离群点驱动 / 域外污染。"""
    y = ctx["yd"]
    feats = ctx["feats"]
    Xd = ctx["Xd"]
    ev = hyp.get("evidence") or {}
    atk = []
    feat = ev.get("feature")
    r = (ev.get("pearson") or {}).get("r")
    n = (ev.get("pearson") or {}).get("n") or len(y)

    if r is not None and abs(r) < 0.2:
        atk.append({"severity": "major", "verdict": "weakened",
                    "attack": f"效应量过小（|r|={abs(r):.2f}），无法与噪声区分"})
    if r is not None and abs(r) >= 0.2:
        v = Xd[:, feats.index(feat)] if feat in feats else None
        if v is not None and len(y) > 10:
            keep = np.argsort(-np.abs(y - np.mean(y)))[1:]        # 去掉最极端的一个点
            r2 = _corr(v[keep], y[keep])
            if r2 and (r2["r"] * r) < 0:
                atk.append({"severity": "fatal", "verdict": "refuted",
                            "attack": f"去掉一个离群点后相关系数变号（{r} → {r2['r']}），结论由单点驱动"})
            elif r2 and abs(r2["r"]) < abs(r) * 0.6:
                atk.append({"severity": "major", "verdict": "weakened",
                            "attack": f"去掉离群点后效应衰减（{r} → {r2['r']}），稳健性不足"})
    if n < 30:
        atk.append({"severity": "major", "verdict": "weakened",
                    "attack": f"样本量 n={n} < 30，小样本下相关系数本身不稳定"})
    if feat:
        for other in ("filler_pct", "toughener_pct", "resin_pct", "hardener_pct"):
            if other == feat or other not in feats or feat not in feats:
                continue
            c = _corr(Xd[:, feats.index(feat)], Xd[:, feats.index(other)])
            if c and abs(c["r"]) > 0.6:
                atk.append({"severity": "major", "verdict": "weakened",
                            "attack": f"与 {other} 共线（r={c['r']}），单因素归因不成立"})
                break
    if red.get("ood_ratio", 0) > 0.2:
        atk.append({"severity": "minor", "verdict": "weakened",
                    "attack": f"dev→golden 漂移比例 {red['ood_ratio']:.0%}，结论不可外推"})
    if not atk:
        atk.append({"severity": "minor", "verdict": "survived",
                    "attack": "未发现致命混淆；仍建议以单因素实验确认方向"})
    return atk


# ---------------------------------------------------------------- 节点
def supervisor(state):
    ctx = prepare(2, state["target"], state["system"], state.get("feature_mode", "both"))
    with connect() as c, c.cursor() as cur:
        champ = load_champion(state["system"], cur)
        append_event(cur, state["run_id"], "foundry:supervisor", "start",
                     {"round": state["round"], "system": state["system"], "target": state["target"],
                      "n_dev": ctx["dev_end"], "n_gold": ctx["gold_end"] - ctx["dev_end"],
                      "champion": (champ or {}).get("family"),
                      "holdout_mae": float(champ["holdout_mae"]) if champ else None,
                      "budget": state["budget"], "pool": state["pool_size"]})
        cur.execute(STRAT_LAST, {"system": state["system"], "target": state["target"],
                                 "round": state["round"]})
        inherited = cur.fetchone()
    red = redteam_checks(ctx)
    lab = VirtualLab(champ, ctx)
    if champ:
        msg = (f"🏭 Supervisor「第 {state['round']} 轮；冠军={champ['family']}"
               f"（留出集 MAE={float(champ['holdout_mae']):.3f}，覆盖 {float(champ['coverage'] or 0):.2f}）"
               f"；dev={ctx['dev_end']}，golden={ctx['gold_end'] - ctx['dev_end']}」")
    else:
        msg = (f"🏭 Supervisor「体系 {state['system']} 没有通过闸门的冠军模型 "
               f"→ 拒绝开启虚拟实验室（先跑 swarm.py --system {state['system']}）」")
    sign = obj_sign(state["target"])
    return {"ctx": ctx, "champion": champ, "lab": lab, "red": red, "inherited": inherited,
            # 优化器内部按"越大越好"工作 → 越小越好的指标在进 BO 前取负
            "X_obs": ctx["Xd"], "y_obs": ctx["yd"] * sign, "obj_sign": sign,
            "catalog": load_catalog(),
            "hypotheses": [], "critiques": [], "pool": [], "rejected": [],
            "messages": [msg]}


def hypothesis_node(state):
    hyps = hypothesize(state["ctx"], state["system"])
    with connect() as c, c.cursor() as cur:
        for h in hyps:
            cur.execute(HYP_INSERT, {"run_id": state["run_id"], "round": state["round"], **h,
                                     "evidence": json.dumps(h["evidence"], ensure_ascii=False)})
            h["id"] = cur.fetchone()["id"]
        append_event(cur, state["run_id"], "foundry:hypothesis", "propose",
                     {"n": len(hyps), "codes": [h["code"] for h in hyps]})
    text = chat("你是胶粘剂机理专家，只能复述已有证据，不得给出任何数值。",
                f"假说代码：{[h['code'] for h in hyps]}。{NO_NUMBER_RULE}",
                f"💡 Hypothesis「提出 {len(hyps)} 条证据驱动假说」")
    return {"hypotheses": hyps, "messages": [text]}


def critic_node(state):
    crits = []
    with connect() as c, c.cursor() as cur:
        for h in state["hypotheses"]:
            atk = critique(h, state["ctx"], state.get("red", {}))
            worst = max(atk, key=lambda a: {"fatal": 3, "major": 2, "minor": 1}[a["severity"]])
            posterior = {"fatal": 0.05, "major": 0.3, "minor": 0.6}.get(worst["severity"], 0.5)
            posterior = round(posterior * (1.0 if h["evidence"].get("pearson") else 0.8), 3)
            status = {"refuted": "refuted", "weakened": "weakened",
                      "survived": "survived"}.get(worst["verdict"], "proposed")
            for a in atk:
                cur.execute(CRIT_INSERT, {
                    "hypothesis_id": h["id"], "actor": "critic", "attack": a["attack"],
                    "severity": a["severity"], "verdict": a["verdict"],
                    "evidence": json.dumps({"hypothesis": h["code"]}, ensure_ascii=False)})
                a["id"] = cur.fetchone()["id"]
                crits.append({"hypothesis": h["code"], **a})
            cur.execute(HYP_UPDATE, {"id": h["id"], "posterior": posterior, "status": status})
            h["posterior"], h["status"] = posterior, status
        append_event(cur, state["run_id"], "foundry:critic", "critique",
                     {"n": len(crits),
                      "fatal": sum(1 for x in crits if x["severity"] == "fatal")})
    survived = [h for h in state["hypotheses"] if h["status"] in ("survived", "weakened")]
    text = chat("你是科学红队，只做质疑，不产出数值。",
                f"被驳倒：{[h['code'] for h in state['hypotheses'] if h['status']=='refuted']}。{NO_NUMBER_RULE}",
                f"🔴 Critic「{len(crits)} 条攻击；存活 {len(survived)}/{len(state['hypotheses'])} 条假说」")
    return {"critiques": crits, "messages": [text]}


def _pool_candidates(state):
    """候选池 = 继承种子 + 邻域扰动 + 全局随机采样（池 ≫ 预算，BO 才有意义）。"""
    import random

    rng = random.Random(state["seed"] + state["round"] * 101)
    system, catalog = state["system"], state["catalog"]
    pool = []
    seen = set()
    refused = []

    def push(items, origin, acquisition):
        """硬约束不满足 → 拒答（记因，不进池）。"""
        try:
            cand = build_candidate(f"FC-{state['run_id']}-{len(pool):04d}", system, items,
                                   origin=origin, catalog=catalog)
        except Refusal as e:
            refused.append({"origin": origin, "reason": str(e)})
            return
        key = tuple(sorted((i["raw_material_code"], round(float(i["pct"]), 2)) for i in items))
        if key in seen:
            return
        seen.add(key)
        cand["acquisition"] = acquisition
        pool.append(cand)

    seeds = []
    if state.get("inherited"):
        for it in (state["inherited"]["strategy"] or {}).get("seed_items", [])[:3]:
            seeds.append(it)
    if not seeds:
        for _ in range(40):
            it = sample_candidate(system, rng, catalog)
            if it:
                seeds.append(it)
                break

    for s in seeds:
        push(s, "seed", "inherited")
        for _ in range(max(1, state["pool_size"] // (8 * max(len(seeds), 1)))):
            push(mutate_candidate(s, rng, catalog, system=system, scale=0.25), "bo", "mutation")
    tries = 0
    while len(pool) < state["pool_size"] and tries < state["pool_size"] * 20:
        tries += 1
        it = sample_candidate(system, rng, catalog)
        if it is None:                                # 单次不可行很正常（机理配平失败），继续
            continue
        push(it, "bo", "random")
    return pool, refused


def propose(state):
    if not state.get("champion"):
        with connect() as c, c.cursor() as cur:
            append_event(cur, state["run_id"], "foundry:bo", "refuse",
                         {"reason": f"体系 {state['system']} 无通过闸门的冠军模型"})
        return {"pool": [], "chosen": [], "messages": [
            f"🚫 BO「体系 {state['system']} 没有冠军模型，拒绝选点（先跑 swarm.py --system "
            f"{state['system']} 产出冠军）」"]}
    pool, refused = _pool_candidates(state)
    if not pool:
        with connect() as c, c.cursor() as cur:
            append_event(cur, state["run_id"], "foundry:bo", "refuse",
                         {"reason": "候选池为空（硬约束全部拒答）"})
        return {"pool": [], "chosen": [], "messages": ["🚫 BO「候选池为空：所有采样均被硬约束拒答」"]}

    Xc = build_feature_matrix(pool, state["ctx"]["feats"], state["system"], state["catalog"])
    lab = state["lab"]

    # 适用域前置过滤：域外点不进 BO 候选（第五律：域外硬拒答）。
    # 放在选点之前，避免"选了才发现域外"导致预算被浪费。
    dists, thr = nn_distances(Xc, state["ctx"]["Xd"])
    limit = thr[OOD_BOUNDARY]                 # 域边界：不比参考集中最孤立的样本更远
    in_domain = dists <= limit
    ood = [{"code": pool[i]["code"], "reason": "域外硬拒答",
            "nn_distance": round(float(dists[i]), 4),
            "boundary": OOD_BOUNDARY, "boundary_limit": round(limit, 4),
            "nn_dist_p95": round(thr["p95"], 4), "nn_dist_max": round(thr["max"], 4)}
           for i in range(len(pool)) if not in_domain[i]]
    if in_domain.sum() == 0:
        with connect() as c, c.cursor() as cur:
            append_event(cur, state["run_id"], "foundry:bo", "refuse",
                         {"reason": "整个候选池都在适用域外"})
        return {"pool": pool, "chosen": [], "ood": ood, "messages": [
            f"🚫 BO「候选池 {len(pool)} 全部落在适用域外（第五律：硬拒答）」"]}
    keep = np.where(in_domain)[0]
    pool = [pool[i] for i in keep]
    Xc = Xc[keep]

    mu_champ, _ = lab.predict(Xc)                       # 冠军模型先验（用于 fallback 排序）

    surrogate = GPSurrogate(cfg=(state["champion"] or {}).get("config") if
                            (state["champion"] or {}).get("family") == "gp" else {"ls": 1.0})
    surrogate.fit(state["X_obs"], state["y_obs"])
    mu, sigma = surrogate.predict(Xc)

    # batch 构成 = 继承起点（warm start）+ 冠军贪心（exploitation）+ EI 探索（exploration）
    # 继承起点保证第二轮起点 ≥ 第一轮终点（进化可度量）；EI 负责探索新区域。
    forced, forced_kind = [], []
    seed_idx = next((i for i, c in enumerate(pool) if c.get("acquisition") == "inherited"), None)
    if seed_idx is None and pool:
        seed_idx, pool[0]["acquisition"] = 0, "seed_random"
    if seed_idx is not None:
        forced.append(seed_idx)
        forced_kind.append(pool[seed_idx]["acquisition"])
    # 冠军贪心：mu_champ 是真实值，按目标方向取极值（越小越好 → argmin）
    greedy = int(np.argmax(mu_champ * obj_sign(state["target"])))
    if greedy not in forced:
        forced.append(greedy)
        forced_kind.append("champion_greedy")

    idx_ei, scores_ei = propose_batch(Xc, surrogate, state["y_obs"],
                                      k=max(0, min(state["budget"], len(pool)) - len(forced)),
                                      kind="ei", exclude=forced)
    idx = forced + list(idx_ei)
    scores = [None] * len(forced) + list(scores_ei)
    kinds = forced_kind + ["ei"] * len(idx_ei)
    if not idx:
        idx = list(np.argsort(-mu_champ * obj_sign(state["target"]))[:state["budget"]])
        scores = [float(mu_champ[i]) for i in idx]
        kinds = ["champion_greedy"] * len(idx)

    for rank, i in enumerate(idx):
        pool[i]["rank"] = rank
        pool[i]["acq"] = scores[rank] if rank < len(scores) else None
        pool[i]["acquisition"] = kinds[rank]
        pool[i]["mu_surrogate"] = float(mu[i])
        pool[i]["sigma_surrogate"] = float(sigma[i])
        pool[i]["mu_champion"] = float(mu_champ[i])
        pool[i]["X"] = Xc[i]

    warm = float(mu_champ[seed_idx]) if seed_idx is not None else None

    # 搜索代价证据：随机采样要多久才能撞到本轮最优点（继承把这件事的成本降到 1 次）
    best_mu = float(np.max(mu_champ)) if len(mu_champ) else None
    hit_rate = float(np.mean(mu_champ >= best_mu - 1e-9)) if best_mu is not None else 0.0
    search_stats = {
        "pool": len(pool),
        "best_champion_pred": None if best_mu is None else round(best_mu, 4),
        "random_hit_rate": round(hit_rate, 5),
        "expected_random_trials": (round(1.0 / hit_rate, 1) if hit_rate > 0 else None),
        "warm_start_trials": 1 if warm is not None else None,
    }

    chosen = [pool[i] for i in idx]
    with connect() as c, c.cursor() as cur:
        for cand in chosen:
            save_candidate(cand, cur)
        append_event(cur, state["run_id"], "foundry:bo", "select",
                     {"pool": len(pool), "budget": state["budget"],
                      "refused_by_constraints": len(refused),
                      "degraded": surrogate.degraded, "warm_start": warm,
                      "search_stats": search_stats,
                      "top": [{"code": c["code"], "acq": c.get("acq"), "kind": c["acquisition"],
                               "ee": c.get("ee_ratio")} for c in chosen]})
    msg = (f"🎯 BO「池 {len(pool)}（硬约束拒答 {len(refused)}，域外剔除 {len(ood)}）"
           f"→ 预算 {len(chosen)}；"
           f"继承起点 {warm if warm is not None else 0:.3f} + 冠军贪心 + EI 探索"
           f"{'（GP 退化→冠军模型排序）' if surrogate.degraded else ''}；"
           f"最优采集值 {max([s for s in scores if s is not None] or [0]):.4f}；"
           f"随机采样撞中最优点需 ~{search_stats['expected_random_trials']} 次，"
           f"继承起点 1 次」")
    return {"pool": pool, "chosen": chosen, "warm_start": warm, "ood": ood,
            "search_stats": search_stats, "messages": [msg]}


def plan_gate(state):
    """把选中点变成"建议实验清单"；approver 为空 → 停在 proposed（待人批准）。"""
    lab = state["lab"]
    plans = []
    approver = state.get("approver")
    with connect() as c, c.cursor() as cur:
        for cand in state["chosen"]:
            x = cand["X"]
            mu, half = lab.predict(np.atleast_2d(x))
            ap = applicability_report(x, state["ctx"]["Xd"], state["ctx"]["feats"])
            cand["applicability"] = ap
            if not ap["in_domain"]:
                state["rejected"].append({"code": cand["code"], "reason": "域外硬拒答",
                                          "nn_distance": ap["nn_distance"],
                                          "boundary": ap.get("boundary", OOD_BOUNDARY),
                                          "boundary_limit": ap.get("limit"),
                                          "nn_dist_p95": ap["nn_dist_p95"],
                                          "nn_dist_max": ap["nn_dist_max"]})
                continue
            cur.execute(PLAN_INSERT, {
                "run_id": state["run_id"], "round": state["round"], "candidate_id": cand["id"],
                "target": state["target"], "acquisition": cand.get("acquisition"),
                "predicted": round(float(mu[0]), 4), "ci_low": round(float(mu[0] - half[0]), 4),
                "ci_high": round(float(mu[0] + half[0]), 4),
                "interval_method": "split_conformal",
                "model_version": lab.version,
                "gene_id": (state["champion"] or {}).get("gene_id"),
                "capsule_id": str((state["champion"] or {}).get("capsule_id")) if state["champion"] else None,
                "rationale": f"EI={cand.get('acq')}；ee={cand.get('ee_ratio')}",
                "status": "proposed"})
            pid = cur.fetchone()["id"]
            cand["plan_id"] = pid
            cand["predicted"] = round(float(mu[0]), 4)
            cand["ci_low"] = round(float(mu[0] - half[0]), 4)
            cand["ci_high"] = round(float(mu[0] + half[0]), 4)
            # 先登记预测，后做实验（13 号文档制度基石）
            cur.execute(PRED_INSERT, {
                "experiment_no": f"PLAN-{pid}", "model_version": lab.version,
                "features": json.dumps({"items": cand["items"], "mech": cand.get("mech")},
                                       ensure_ascii=False),
                "predicted": cand["predicted"], "ci_low": cand["ci_low"],
                "ci_high": cand["ci_high"],
                "rationale": f"BO 建议（{cand.get('acquisition')}），待人工批准"})
            if approver:
                cur.execute(PLAN_APPROVE, {"id": pid, "by": approver})
            plans.append({"plan_id": pid, "code": cand["code"], "status":
                          "approved" if approver else "proposed",
                          "predicted": cand["predicted"], "acq": cand.get("acq")})
        append_event(cur, state["run_id"], "foundry:gate", "plan",
                     {"plans": plans, "rejected_ood": len(state["rejected"]),
                      "approver": approver})
    msg = (f"🛂 Gate「建议 {len(plans)} 个实验" +
           (f"，已由 {approver} 批准」" if approver else "；**未批准**，停在 proposed，无自动执行路径」") +
           (f"；域外拒答 {len(state['rejected'])} 个" if state.get("rejected") else ""))
    return {"plans": plans, "messages": [msg]}


def virtual_lab(state):
    if not state.get("approver"):
        return {"results": [], "messages": ["⏸ 未获人工批准，虚拟实验不执行（L4 红线）"]}
    if not state.get("champion"):
        return {"results": [], "messages": ["🚫 无冠军模型，虚拟实验室不开机（拒绝编造数值）"]}
    lab = state["lab"]
    results = []
    with connect() as c, c.cursor() as cur:
        for cand in state["chosen"]:
            if not cand.get("plan_id"):
                continue
            mu, half = lab.predict(np.atleast_2d(cand["X"]))
            correction = scale_apply(float(mu[0]), "lab_500g", state.get("scale", "lab_500g")) \
                if state.get("scale", "lab_500g") != "lab_500g" else None
            cur.execute(RESULT_INSERT, {
                "plan_id": cand["plan_id"], "simulated": True, "source": "virtual_lab",
                "scale": state.get("scale", "lab_500g"), "target": state["target"],
                "value": round(float(mu[0]), 4), "ci_low": round(float(mu[0] - half[0]), 4),
                "ci_high": round(float(mu[0] + half[0]), 4), "model_version": lab.version,
                "simulator_version": lab.version,
                "corrected": bool(correction and correction.get("corrected")),
                "correction": json.dumps(correction or {}, ensure_ascii=False),
                "payload": json.dumps({"ee_ratio": cand.get("ee_ratio"),
                                       "acq": cand.get("acq")}, ensure_ascii=False)})
            rid = cur.fetchone()["id"]
            results.append({"result_id": rid, "plan_id": cand["plan_id"], "code": cand["code"],
                            "value": round(float(mu[0]), 4), "simulated": True,
                            "corrected": bool(correction and correction.get("corrected"))})
        append_event(cur, state["run_id"], "foundry:lab", "simulate",
                     {"n": len(results), "simulated": True, "scale": state.get("scale"),
                      "note": "模拟结果不进 mat.performance"})
    msg = f"🧪 虚拟实验室「{len(results)} 个模拟结果（全部 simulated=true），不写入实测表」"
    return {"results": results, "messages": [msg]}


def ingest(state):
    """回灌：模拟点进入下一轮的搜索集（仍标记为 simulated），真实留出集不动。"""
    if not state.get("results"):
        return {"messages": ["（无结果可回灌）"]}
    X_new, y_new = [], []
    for cand in state["chosen"]:
        if not cand.get("plan_id"):
            continue
        X_new.append(cand["X"])
        y_new.append(cand["predicted"])
    X_obs = state["X_obs"]
    y_obs = state["y_obs"]
    if state.get("feed_simulated", True):
        # y_obs 空间已按优化方向取过符号，回灌的模拟点必须用同一符号，否则方向不一致
        X_obs = np.vstack([X_obs, np.asarray(X_new, float)])
        y_obs = np.concatenate([y_obs, np.asarray(y_new, float) * obj_sign(state["target"])])
    with connect() as c, c.cursor() as cur:
        append_event(cur, state["run_id"], "foundry:ingest", "feedback",
                     {"added": len(X_new), "observed_total": int(len(y_obs)),
                      "feed_simulated": state.get("feed_simulated", True),
                      "note": "模拟点只进搜索集；留出集仍为真实数据"})
    return {"X_obs": X_obs, "y_obs": y_obs,
            "messages": [f"🔄 回灌「+{len(X_new)} 个模拟点 → 搜索集 {len(y_obs)}（留出集仍为真实数据）」"]}


def datacard_node(state):
    if not state.get("champion"):
        return {"datacards": [], "messages": ["📇 无冠军模型，不生成数据卡"]}
    cards = []
    for cand in state["chosen"]:
        if not cand.get("plan_id") or not cand.get("predicted"):
            continue
        sim = {"value": cand["predicted"], "ci_low": cand["ci_low"], "ci_high": cand["ci_high"],
               "interval_method": "split_conformal",
               "simulator_version": state["lab"].version, "scale": state.get("scale", "lab_500g"),
               "mech": cand.get("mech")}
        correction = None
        if state.get("scale", "lab_500g") != "lab_500g":
            correction = scale_apply(cand["predicted"], "lab_500g", state["scale"])
        card = build_datacard(cand, sim, state["lab"], cand["applicability"], state["run_id"],
                              state["round"], state["target"], scale=state.get("scale"),
                              correction=correction)
        with connect() as c, c.cursor() as cur:
            card = save_datacard(card, cur)
            append_event(cur, state["run_id"], "foundry:datacard", "publish",
                         {"code": card["code"], "predicted": card["predicted"],
                          "in_domain": card["applicability"]["in_domain"],
                          "simulated": True})
        card["artifacts"] = write_artifacts(card)
        cards.append(card)
    msg = f"📇 数据卡「生成 {len(cards)} 张（含置信区间 / 适用域 / 合规 / provenance / simulated 标记）」"
    return {"datacards": cards, "messages": [msg]}


def evolve(state):
    """配方策略跨轮继承：分数只由确定性评测写，负例同样入库。"""
    sign = obj_sign(state["target"])          # 越小越好 → 取最小真实值
    best = None
    for cand in state["chosen"]:
        if cand.get("predicted") is None:
            continue
        if best is None or cand["predicted"] * sign > best["predicted"] * sign:
            best = cand
    env = env_fingerprint()
    strategy = {
        "seed_items": [best["items"]] if best else [],
        "ee_window": [round(float(best["ee_ratio"]) - 0.05, 3),
                      round(float(best["ee_ratio"]) + 0.05, 3)] if best and best.get("ee_ratio") else None,
        "acquisition": "ei", "batch": state["budget"], "pool": state["pool_size"],
        "scale": state.get("scale", "lab_500g"),
        "round": state["round"], "run_id": state["run_id"],
    }
    passed = bool(state.get("datacards")) and bool(state.get("approver"))
    fail_reason = None if passed else ("未获人工批准（L4）" if not state.get("approver")
                                       else "无可用数据卡")
    with connect() as c, c.cursor() as cur:
        cur.execute(STRAT_INSERT, {
            "run_id": state["run_id"], "round": state["round"], "system": state["system"],
            "target": state["target"], "strategy": json.dumps(strategy, ensure_ascii=False),
            "score": round(float(best["predicted"]), 4) if best else None,
            "holdout_mae": state["lab"].holdout_mae,
            "trials_total": len(state["pool"]), "passed": passed, "fail_reason": fail_reason,
            "inherited_from": (state.get("inherited") or {}).get("id"),
            "env": json.dumps(env, ensure_ascii=False)})
        sid = cur.fetchone()["id"]
        append_event(cur, state["run_id"], "foundry:evolve", "promote" if passed else "reject",
                     {"strategy_id": sid,
                      "score": (round(float(best["predicted"]), 4) if best else None),
                      "inherited_from": (state.get("inherited") or {}).get("id"),
                      "passed": passed, "fail_reason": fail_reason})
    msg = (f"🧬 EvoMap「策略资产 R{state['round']} 入库（继承自 "
           f"{(state.get('inherited') or {}).get('id') or '无'}）；"
           f"本轮最优预测 {round(float(best['predicted']), 3) if best else '—'}"
           f"；留出集 MAE {state['lab'].holdout_mae}」")
    return {"strategy_id": sid, "messages": [msg]}


PRED_ALIGN = """
update evo.prediction_register
   set actual = %(actual)s, aligned_at = now()
 where experiment_no = %(no)s
returning id, predicted, ci_low, ci_high
"""

PLAN_ONE = """
select p.id, p.run_id, p.round, p.target, p.status, p.approved_by, p.predicted,
       p.ci_low, p.ci_high, p.acquisition,
       c.id as candidate_id, c.code, c.system, c.items, c.ee_ratio
from evo.experiment_plan p
left join mat.formulation_candidate c on c.id = p.candidate_id
where p.id = %(id)s
"""


def import_real_result(plan_id, value, tester=None, unit="MPa", test_item=None,
                       promote=True, condition=None, method_std="GB/T 7124"):
    """真实实验结果回灌（人做完实验后调用；不是自动执行）。

    做三件事：
      1. 写 evo.experiment_result(simulated=false) —— 与模拟结果严格区分；
      2. 对齐 evo.prediction_register（先登记预测、后做实验的制度闭环：预测 vs 实测）；
      3. promote=True 时把候选配方正式入库为 mat.formulation + items + performance
         （只有实测过的配方才进事实库，模拟配方永远不进）。
    """
    with connect() as c, c.cursor() as cur:
        cur.execute(PLAN_ONE, {"id": plan_id})
        p = cur.fetchone()
        if not p:
            raise SystemExit(f"实验计划 PLAN-{plan_id} 不存在")
        if p["status"] not in ("approved", "executed") or not p["approved_by"]:
            raise SystemExit(f"L4 红线：PLAN-{plan_id} 状态 {p['status']}，未经批准不得回灌实测值")
        cur.execute(RESULT_INSERT, {
            "plan_id": plan_id, "simulated": False, "source": "real_lab",
            "scale": "lab_500g", "target": test_item or p["target"], "value": float(value),
            "ci_low": None, "ci_high": None, "model_version": None,
            "simulator_version": None, "corrected": False,
            "correction": json.dumps({}, ensure_ascii=False),
            "payload": json.dumps({"tester": tester, "unit": unit}, ensure_ascii=False)})
        rid = cur.fetchone()["id"]

        cur.execute(PRED_ALIGN, {"actual": float(value), "no": f"PLAN-{plan_id}"})
        aligned = cur.fetchone()

        formulation_code = None
        if promote and p["candidate_id"]:
            cur.execute("""insert into mat.formulation(code, version, system, stage, created_at)
                           values (%(code)s, 1, %(system)s, 'lab', now())
                           on conflict (code, version) do nothing returning id""",
                        {"code": p["code"], "system": p["system"]})
            row = cur.fetchone()
            if row:
                fid = row["id"]
                for it in p["items"]:
                    cur.execute("""select id from mat.raw_material where code = %(code)s""",
                                {"code": it["raw_material_code"]})
                    rm = cur.fetchone()
                    if not rm:
                        continue
                    cur.execute("""insert into mat.formulation_item(formulation_id, raw_material_id, mass_pct)
                                   values (%(fid)s, %(rm)s, %(pct)s)
                                   on conflict do nothing""",
                                {"fid": fid, "rm": rm["id"], "pct": float(it["pct"])})
                cur.execute("""insert into mat.performance(formulation_id, test_item, value, unit,
                                                           condition, method_std, tested_at, verdict)
                               values (%(fid)s, %(t)s, %(v)s, %(u)s, %(cond)s::jsonb, %(m)s, now(), %(verdict)s)""",
                            {"fid": fid, "t": test_item or p["target"], "v": float(value),
                             "u": unit, "cond": json.dumps(condition or {"source": "foundry_real_lab"},
                                                           ensure_ascii=False),
                             "m": method_std,
                             "verdict": "hit" if (aligned and p["predicted"] is not None
                                                  and abs(float(value) - float(p["predicted"]))
                                                  <= max(1.0, 0.15 * abs(float(p["predicted"]))))
                                        else "partial"})
                formulation_code = p["code"]

        append_event(cur, p["run_id"], "foundry:real", "ingest",
                     {"plan_id": plan_id, "value": float(value), "tester": tester,
                      "promoted_formulation": formulation_code,
                      "register_aligned": bool(aligned)})
    return {
        "result_id": rid, "plan_id": plan_id,
        "predicted": float(aligned["predicted"]) if aligned and aligned["predicted"] is not None else None,
        "actual": float(value),
        "in_interval": (bool(aligned and aligned["ci_low"] is not None
                             and float(aligned["ci_low"]) <= float(value) <= float(aligned["ci_high"]))
                        if aligned else None),
        "promoted_formulation": formulation_code,
        "note": "实测值进 mat.performance；模拟值永远不进（循环论证防线）",
    }


def build_graph():
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(FoundryState)
    g.add_node("supervisor", supervisor)
    g.add_node("hypothesis", hypothesis_node)
    g.add_node("critic", critic_node)
    g.add_node("propose", propose)
    g.add_node("gate", plan_gate)
    g.add_node("lab", virtual_lab)
    g.add_node("ingest", ingest)
    g.add_node("datacard", datacard_node)
    g.add_node("evolve", evolve)
    for a, b in (("supervisor", "hypothesis"), ("hypothesis", "critic"), ("critic", "propose"),
                 ("propose", "gate"), ("gate", "lab"), ("lab", "ingest"),
                 ("ingest", "datacard"), ("datacard", "evolve")):
        g.add_edge(a, b)
    g.add_edge(START, "supervisor")
    g.add_edge("evolve", END)
    return g.compile()


def _init_state(run_id, round_no, target, system, budget, pool, seed, approver, scale,
                feature_mode, feed_simulated):
    return {
        "run_id": run_id, "round": round_no, "target": target, "system": system,
        "budget": budget, "pool_size": pool, "seed": seed, "approver": approver,
        "scale": scale, "feature_mode": feature_mode, "feed_simulated": feed_simulated,
        "ctx": {}, "champion": None, "lab": None, "catalog": {}, "red": {},
        "X_obs": None, "y_obs": None, "pool": [], "warm_start": None,
        "search_stats": {}, "ood": [], "chosen": [], "plans": [],
        "results": [], "datacards": [], "hypotheses": [], "critiques": [],
        "inherited": None, "strategy_id": None, "rejected": [], "messages": [],
    }


def run_round(run_id, round_no, target="lap_shear_strength", system="epoxy", budget=5,
              pool=DEFAULT_POOL, seed=0, approver=None, scale="lab_500g",
              feature_mode="both", feed_simulated=True, verbose=True):
    out = build_graph().invoke(_init_state(run_id, round_no, target, system, budget, pool,
                                           seed, approver, scale, feature_mode, feed_simulated))
    if verbose:
        print(f"\n===== 铸造厂 · 第 {round_no} 轮 [{run_id}] =====")
        for m in out["messages"]:
            print(" ", m)
        if out["plans"]:
            print("  -- 建议实验清单 --")
            for p in out["plans"]:
                print(f"    PLAN-{p['plan_id']:<5} {p['code']:<22} 预测={p['predicted']:<8} "
                      f"status={p['status']}")
        ood = list(out.get("ood") or []) + list(out.get("rejected") or [])
        if ood:
            print(f"  -- 域外硬拒答 {len(ood)} 个 --")
            for r in ood[:5]:
                print(f"    {r['code']} nn={r['nn_distance']} > "
                      f"{r.get('boundary', OOD_BOUNDARY)}={r.get('boundary_limit')}")
        if out["datacards"]:
            print("  -- 数据卡 --")
            for d in out["datacards"]:
                ap = d["applicability"]
                print(f"    {d['code']:<34} {d['predicted']:>7} "
                      f"[{d['ci_low']}, {d['ci_high']}] {ap['decision']}")
    return out


def run_foundry(rounds=2, target="lap_shear_strength", system="epoxy", budget=5,
                pool=DEFAULT_POOL, seed=0, approver=None, scale="lab_500g",
                feature_mode="both", feed_simulated=True, verbose=True):
    """跑 N 轮闭环：每轮都重新取冠军 → 假说 → 批判 → BO → 虚拟实验 → 回灌 → 数据卡 → 进化。"""
    run_id = f"fd{system[:2]}-{uuid.uuid4().hex[:6]}"
    rounds_out = []
    for r in range(1, rounds + 1):
        out = run_round(run_id, r, target, system, budget, pool, seed, approver, scale,
                        feature_mode, feed_simulated, verbose)
        rounds_out.append(out)
    if verbose:
        print("\n===== 闭环摘要（进化可度量）=====")
        print(f"{'轮':<4}{'候选池':>7}{'建议':>6}{'域外拒答':>9}{'模拟':>6}{'数据卡':>7}"
              f"{'起点(继承)':>11}{'终点(本轮最优)':>14}{'增量':>8}")
        sgn = obj_sign(target)          # 越小越好 → 取最小真实值
        prev_end = None
        for o in rounds_out:
            vals = [(c.get("predicted") or -1e9) * sgn for c in o["chosen"]] or [-1e9]
            best = max(vals) * sgn
            warm = o.get("warm_start")
            delta = (best - prev_end) if (prev_end is not None and best > -1e8) else None
            n_ood = len(o.get("ood") or []) + len(o.get("rejected") or [])
            print(f"R{o['round']:<3}{len(o['pool']):>7}{len(o['plans']):>6}"
                  f"{n_ood:>9}{len(o['results']):>6}{len(o['datacards']):>7}"
                  f"{warm if warm is not None else float('nan'):>11.3f}"
                  f"{best if best > -1e8 else float('nan'):>14.3f}"
                  f"{('—' if delta is None else f'{delta:+.3f}'):>8}")
            if best > -1e8:
                prev_end = best
        if len(rounds_out) >= 2:
            w2 = rounds_out[1].get("warm_start")
            # 终点按**优化方向**取极值：分辨率/LER 越小越好，取 min；强度类取 max
            e1 = max([(c.get("predicted") or -1e9) * sgn for c in rounds_out[0]["chosen"]] or [-1e9]) * sgn
            if w2 is not None and e1 > -1e8:
                ok = (w2 * sgn) >= (e1 * sgn) - 1e-3
                verdict = ("✅ 第二轮起点 ≥ 第一轮终点（继承生效）" if ok
                           else "❌ 起点未达到上轮终点")
                cmp_word = "≤（更优）" if sgn < 0 else "≥"
                print(f"  {verdict}：R2 起点 {w2:.3f} {cmp_word} R1 终点 {e1:.3f}"
                      f"（目标方向：{'越小越好' if sgn < 0 else '越大越好'}）")
            s2 = rounds_out[1].get("search_stats") or {}
            if s2.get("expected_random_trials"):
                print(f"  搜索代价：继承起点 1 次 vs 随机采样 ~{s2['expected_random_trials']} 次"
                      f"（池 {s2['pool']}，命中率 {s2['random_hit_rate']:.4f}）")
        print("⚠️ 所有模拟结果均标注 simulated；真实精度只以留出集 MAE 为准")
    return {"run_id": run_id, "rounds": rounds_out}
