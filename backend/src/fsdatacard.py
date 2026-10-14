"""材料数据卡（方案 B 的输出物，对标 LOCTITE SOLVE 的概念数据卡）。

数据卡必须包含的六要素（缺一不可，写入前强校验）：
  1. 配方组分（Σ=100%，逐原料级）
  2. 预测性能 + 置信区间（含区间方法：split-conformal / gp_posterior）
  3. 适用域声明（ood_ratio / 最近邻 p95 / in_domain / 越界维度）
  4. `simulated` 标记 + disclaimer（明示不是实测值）
  5. 工艺条件（固化温度/时间/压力/后固化）
  6. provenance（冠军模型 / Gene / Capsule / dataset_hash / 留出集 MAE）

红线：数据卡不得出现任何"实测/保证/认证"字样；不得给出未经规则判定的合规结论。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np

from fsdb import connect

DISCLAIMER = (
    "本数据卡为模型预测结果（simulated），不是实测值。预测区间来自冠军模型 + split-conformal 校准，"
    "真实精度以留出集 MAE 为准，且留出集不含任何模拟数据。合规结论由规则引擎基于估算数据给出，"
    "不可用于真实合规判定。投放实验须经人工批准。"
)

DC_INSERT = """
insert into evo.datacard(code, candidate_id, plan_id, run_id, round, system, target,
                         composition, predicted, ci_low, ci_high, interval_method, coverage,
                         applicability, process, compliance, provenance, simulated, corrected,
                         disclaimer, payload)
values (%(code)s, %(candidate_id)s, %(plan_id)s, %(run_id)s, %(round)s, %(system)s, %(target)s,
        %(composition)s::jsonb, %(predicted)s, %(ci_low)s, %(ci_high)s, %(interval_method)s, %(coverage)s,
        %(applicability)s::jsonb, %(process)s::jsonb, %(compliance)s::jsonb, %(provenance)s::jsonb,
        %(simulated)s, %(corrected)s, %(disclaimer)s, %(payload)s::jsonb)
on conflict (code) do update set predicted = excluded.predicted
returning id
"""


def nn_distances(X, X_ref):
    """候选到参考集（dev）的最近邻标准化距离。

    返回 (每个候选的距离, 参考集留一法距离的分位数字典 p95/p99/max)。
    域边界取**留一法距离的最大值**（"不比参考集中最孤立的样本更远"）：
    实测用 p95 做硬拒答会把约 1/3 的真实留出集样本也判成域外，故 p95 只作严格档参考。
    """
    X_ref = np.asarray(X_ref, float)
    X = np.atleast_2d(np.asarray(X, float))
    mu, sd = X_ref.mean(0), X_ref.std(0) + 1e-9
    a = (X_ref - mu) / sd
    b = (X - mu) / sd
    d = np.sqrt(((b[:, None, :] - a[None, :, :]) ** 2).sum(-1).min(1))
    d_ref = np.sqrt(((a[:, None, :] - a[None, :, :]) ** 2).sum(-1)
                    + np.eye(len(a)) * 1e9).min(1)
    thr = {"mean": float(np.mean(d_ref)), "p95": float(np.quantile(d_ref, 0.95)),
           "p99": float(np.quantile(d_ref, 0.99)), "max": float(np.max(d_ref))}
    return d, thr


def applicability_report(x, X_ref, feature_cols, ood_max=0.20, boundary="max"):
    """适用域判定：候选是否落在历史数据的分布内（与 fsarena.redteam_checks 同口径）。"""
    X_ref = np.asarray(X_ref, float)
    d_arr, thr = nn_distances(x, X_ref)
    d = float(d_arr[0])
    limit = thr.get(boundary, thr["max"])
    mu, sd = X_ref.mean(0), X_ref.std(0) + 1e-9
    b = (np.asarray(x, float).ravel()[None, :] - mu) / sd
    in_domain = bool(d <= limit)
    worst = None
    if not in_domain:
        dev = np.abs(b.ravel())
        worst = sorted(((float(dev[i]), feature_cols[i]) for i in range(len(dev))
                        if dev[i] > 2.0), reverse=True)[:5]
    return {
        "nn_distance": round(d, 4),
        "nn_dist_p95": round(thr["p95"], 4), "nn_dist_max": round(thr["max"], 4),
        "boundary": boundary, "limit": round(limit, 4),
        "in_domain": in_domain,
        "strict_in_domain": bool(d <= thr["p95"]),
        "ood_threshold": "nn_distance > max(参考集留一法最近邻距离)",
        "reference_n": int(len(X_ref)),
        "out_of_domain_dims": [{"z": round(z, 2), "feature": f} for z, f in (worst or [])],
        "decision": "in_domain" if in_domain else "out_of_domain",
        "note": "域外不代表配方不可行，只表示模型在此处的预测无可信证据（第五律：域外硬拒答）",
    }


def build_datacard(cand, sim_result, lab, applicability, run_id, round_no, target,
                   scale=None, correction=None, code=None):
    """组装数据卡字典（写入前强校验，缺要素直接抛错）。"""
    items = cand["items"]
    total = round(sum(float(i["pct"]) for i in items), 3)
    if abs(total - 100.0) > 0.5:
        raise ValueError(f"数据卡组分 Σ={total}，必须为 100±0.5")

    value = sim_result["value"]
    ci_low, ci_high = sim_result["ci_low"], sim_result["ci_high"]
    corrected = bool(correction and correction.get("corrected"))
    # 放大修正不动点预测，只按机理风险项放宽区间（直接乘系数 = 编造精度）
    infl = 1.0
    if corrected:
        infl = float(correction.get("interval_inflation") or 1.0)
        half = (ci_high - ci_low) / 2.0
        ci_low = round(value - half * infl, 4)
        ci_high = round(value + half * infl, 4)

    # 外提示警：预测值落在观测值域之外（模型外推，不是插值）
    y_raw = (lab.ctx or {}).get("yd")
    y_ref = np.asarray(y_raw, float) if y_raw is not None else np.asarray([], float)
    obs_range = ({"min": float(np.min(y_ref)), "max": float(np.max(y_ref)),
                  "mean": float(np.mean(y_ref)), "n": int(len(y_ref))}
                 if len(y_ref) else None)
    extrapolated = bool(obs_range and (value > obs_range["max"] or value < obs_range["min"]))
    provenance = {
        "champion_family": lab.champion["family"] if lab.champion else None,
        "gene_id": lab.champion["gene_id"] if lab.champion else None,
        "capsule_id": str(lab.champion["capsule_id"]) if lab.champion else None,
        "model_version": lab.version,
        "simulator_version": sim_result.get("simulator_version"),
        "holdout_mae": lab.holdout_mae,
        "holdout_note": "唯一可对外声称的精度口径；留出集不含模拟数据",
        "dataset_hash": lab.ctx.get("dhash"),
        "split": lab.ctx.get("split", "time_forward"),
        "round": round_no, "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    card = {
        "code": code or f"DC-{run_id}-R{round_no}-{cand['code']}",
        "candidate_id": cand.get("id"), "plan_id": cand.get("plan_id"),
        "run_id": run_id, "round": round_no, "system": cand["system"], "target": target,
        "composition": items,
        "predicted": value,
        "ci_low": ci_low, "ci_high": ci_high,
        "interval_method": sim_result.get("interval_method", "split_conformal"),
        "coverage": float(lab.champion["coverage"]) if lab.champion and lab.champion.get("coverage") else None,
        "applicability": applicability,
        "process": cand.get("process") or {},
        "compliance": cand["compliance"],
        "provenance": provenance,
        "simulated": True,
        "corrected": corrected,
        "disclaimer": DISCLAIMER,
        "payload": {
            "ee_ratio": cand.get("ee_ratio"), "nco_index": cand.get("nco_index"),
            "mech": cand.get("mech") or sim_result.get("mech") or {},
            "correction": correction or {},
            "scale": scale or sim_result.get("scale", "lab_500g"),
            "acquisition": cand.get("acquisition"),
            "batch_rank": cand.get("rank"),
            "observed_range": obs_range,
            "extrapolation_warning": extrapolated,
        },
    }
    if extrapolated:
        card["disclaimer"] += (
            f" 注意：预测值 {value} 超出训练观测值域 "
            f"[{obs_range['min']:.2f}, {obs_range['max']:.2f}]，属模型外推，可信度低于插值区间。")
    if corrected:
        card["disclaimer"] += (
            f" 放大到 {card['payload'].get('scale')}：点预测不放宽，预测区间已按 "
            f"{len(correction.get('applied') or {})} 项机理风险放宽 {infl}×；"
            f"放大修正未经验证，真实值须由中试/量产实测确认。")
    missing = [k for k in ("composition", "predicted", "ci_low", "ci_high", "applicability",
                           "process", "compliance", "provenance") if not card.get(k)]
    if missing:
        raise ValueError(f"数据卡缺少必需要素：{missing}")
    return card


def save_datacard(card, cur):
    cur.execute(DC_INSERT, {
        **card,
        "composition": json.dumps(card["composition"], ensure_ascii=False),
        "applicability": json.dumps(card["applicability"], ensure_ascii=False),
        "process": json.dumps(card["process"], ensure_ascii=False),
        "compliance": json.dumps(card["compliance"], ensure_ascii=False),
        "provenance": json.dumps(card["provenance"], ensure_ascii=False),
        "payload": json.dumps(card["payload"], ensure_ascii=False),
        "candidate_id": card.get("candidate_id"), "plan_id": card.get("plan_id"),
    })
    row = cur.fetchone()
    card["id"] = row["id"]
    return card


def to_markdown(card):
    p = card["provenance"]
    ap = card["applicability"]
    lines = [
        f"# 材料数据卡 · {card['code']}",
        "",
        f"> ⚠️ {card['disclaimer']}",
        "",
        f"- 体系：{card['system']} ｜ 目标性能：{card['target']} ｜ 轮次：R{card['round']}",
        f"- **预测值：{card['predicted']}（区间 {card['ci_low']} ~ {card['ci_high']}，"
        f"{card['interval_method']}，名义覆盖 "
        f"{round(float(card['coverage']), 3) if card['coverage'] is not None else '—'}）**",
        f"- 适用域：**{ap['decision']}**（最近邻距离 {ap['nn_distance']} vs 域边界"
        f"({ap.get('boundary', 'max')}) {ap.get('limit', ap['nn_dist_p95'])}；"
        f"严格档 p95={ap['nn_dist_p95']} → strict={ap.get('strict_in_domain')}）",
        f"- 放大修正：{'是' if card['corrected'] else '否'}（{card['payload'].get('scale')}）",
        "",
        "## 组分（Σ=100%）",
        "| 原料 | 质量% |", "|---|---|",
    ]
    for it in card["composition"]:
        lines.append(f"| {it['raw_material_code']} | {float(it['pct']):.2f} |")
    mech = card["payload"].get("mech") or {}
    if mech.get("ee_ratio"):
        lines.append("")
        lines.append(f"- 当量比 ee = {mech['ee_ratio']}（偏离 1.0：{mech.get('ee_deviation')}）")
    if mech.get("nco_index"):
        lines.append(f"- NCO 指数 = {mech['nco_index']}")
    if card["payload"].get("extrapolation_warning"):
        obs = card["payload"].get("observed_range") or {}
        lines.append(f"- ⚠️ **外提示警**：预测值超出训练观测值域 "
                     f"[{obs.get('min')}, {obs.get('max')}]（n={obs.get('n')}），"
                     f"属模型外推，可信度低于插值区间")
    if card.get("corrected"):
        corr = card["payload"].get("correction") or {}
        lines.append(f"- 放大修正：{card['payload'].get('scale')}（点预测不放宽，"
                     f"区间放宽 {corr.get('interval_inflation')}×；"
                     f"风险项 {len(corr.get('applied') or {})} 条，"
                     f"实测验证={corr.get('validated')}）")
    lines += [
        "",
        "## 工艺条件",
        "```json",
        json.dumps(card["process"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## 合规（规则判定，非 LLM）",
        f"- 结论：{card['compliance'].get('decision')}",
        f"- VOC 估算：{card['compliance'].get('voc_g_l_est')} g/L",
    ]
    for b in card["compliance"].get("blocking", []):
        lines.append(f"- 🚫 拒答项：{b}")
    for w in card["compliance"].get("warnings", []):
        lines.append(f"- ⚠️ 提示：{w}")
    lines += [
        "",
        "## 溯源（provenance）",
        f"- 冠军模型：{p.get('champion_family')}（Gene {p.get('gene_id')}，Capsule {p.get('capsule_id')}）",
        f"- 模型版本：{p.get('model_version')}",
        f"- 留出集 MAE：**{p.get('holdout_mae')}**（真实精度口径，留出集不含模拟数据）",
        f"- 数据切分：{p.get('split')} ｜ dataset_hash：{p.get('dataset_hash')}",
        f"- 生成时间：{p.get('generated_at')}",
    ]
    if ap.get("out_of_domain_dims"):
        lines.append("")
        lines.append("## 越界维度")
        for d in ap["out_of_domain_dims"]:
            lines.append(f"- {d['feature']}：z={d['z']}")
    return "\n".join(lines)


def write_artifacts(card, outdir="artifacts/datacards"):
    """导出 JSON + Markdown（JSON 供下游 CAE/PLM 系统读取）。"""
    from pathlib import Path

    d = Path(outdir)
    d.mkdir(parents=True, exist_ok=True)
    jp = d / f"{card['code']}.json"
    mp = d / f"{card['code']}.md"
    jp.write_text(json.dumps(card, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    mp.write_text(to_markdown(card), encoding="utf-8")
    return {"json": str(jp), "markdown": str(mp)}


def latest(limit=20, system=None):
    with connect() as c, c.cursor() as cur:
        cur.execute("""
            select id, code, run_id, round, system, target, predicted, ci_low, ci_high,
                   interval_method, coverage, simulated, corrected,
                   applicability, process, compliance, provenance, disclaimer, payload,
                   composition, created_at::text
            from evo.datacard
            where (%(system)s::text is null or system = %(system)s)
            order by id desc limit %(limit)s
        """, {"limit": limit, "system": system})
        return cur.fetchall()
