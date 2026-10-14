"""GEP（Genome Evolution Protocol）资产协议实现 —— 对齐 @evomap/gep-sdk spec v1。

要点（对照 spec）：
  Gene:    type/schema_version/id/category/signals_match/strategy/constraints/validation/asset_id
  Capsule: type/id/trigger/gene/summary/confidence/blast_radius/outcome/env_fingerprint/content/asset_id
  Event:   type/id/intent/signals/genes_used/mutation_id/blast_radius/outcome/source_type/meta/asset_id
  asset_id  = "sha256:" + SHA256(canonicalize(obj 去掉 asset_id))   （Appendix A）
  signals_match 三种格式：子串 / 正则 /pattern/flags / 多语言别名 a|b|c

本项目的映射约定：
  算法族 → Gene；一次评测记录 → Capsule（content 里放胜出的超参配置）；蜂群事件流 → EvolutionEvent
"""
import hashlib
import json
import math
import os
import re
from pathlib import Path

SCHEMA_VERSION = "1.13.0"
GEP_HOME = Path(os.environ.get("GEP_HOME", "/workspace/.evolver/gep"))
N_THRESHOLDS = (20, 50, 80, 150, 300, 500, 1000)


# ---------------------------------------------------------------- Appendix A
def _js_num_str(v):
    """JS Number→String 最短往返：0.0→'0'；repr 的 '1e-07' 对齐 JS '1e-7'（spec golden 陷阱1）。"""
    s = repr(v)
    if "e-" in s:
        mant, _, exp = s.partition("e-")
        return f"{mant}e-{int(exp)}"
    return s


def canonicalize(obj):
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        if not math.isfinite(obj):
            return "null"
        if obj.is_integer():          # 1.0→'1'，-0.0→'0'
            return str(int(obj))
        return _js_num_str(obj)
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(canonicalize(x) for x in obj) + "]"
    if isinstance(obj, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canonicalize(obj[k])
                              for k in sorted(obj.keys())) + "}"
    return "null"


def asset_id(obj):
    clean = {k: v for k, v in obj.items() if k != "asset_id"}
    return "sha256:" + hashlib.sha256(canonicalize(clean).encode("utf-8")).hexdigest()


def stamp(obj):
    obj = dict(obj)
    obj["asset_id"] = asset_id(obj)
    return obj


# ---------------------------------------------------------------- signals
def signal_match(pattern, signals):
    """支持 spec 的三种格式：子串 / /regex/flags / a|b|c 多语言别名。"""
    if pattern.startswith("/") and pattern.rfind("/") > 0:
        end = pattern.rfind("/")
        body, flags = pattern[1:end], pattern[end + 1:]
        rx = re.compile(body, re.I if "i" in flags else 0)
        return any(rx.search(s) for s in signals)
    if "|" in pattern:
        return any(alt.strip().lower() in s.lower() for alt in pattern.split("|") for s in signals)
    return any(pattern.lower() in s.lower() for s in signals)


def signals_for(n, target="lap_shear_strength", system=None, needs_uq=True):
    """把数据集状态编码成 GEP 运行时信号（Selector 的匹配输入）。"""
    s = [f"target:{target}", f"system:{system or 'ALL'}",
         "needs_uq:true" if needs_uq else "needs_uq:false",
         "dataset:small_sample" if n < 300 else "dataset:large_sample"]
    for t in N_THRESHOLDS:
        s.append(f"n_ge_{t}" if n >= t else f"n_lt_{t}")
    return s


def gene_signals(signals_match):
    """PG evo.gene.signals_match（jsonb）→ GEP signals_match 字符串数组。"""
    sm = signals_match or {}
    target = sm.get("target", "lap_shear_strength")
    out = [f"target:{target}", "dataset:small_sample"]
    n_min, n_max = int(sm.get("n_min", 0) or 0), int(sm.get("n_max", 10**9) or 10**9)
    if n_min:
        out.append(f"n_ge_{n_min}")
    if n_max <= max(N_THRESHOLDS):
        out.append(f"n_lt_{n_max}")
    out.append("needs_uq:true" if sm.get("needs_uq") else "needs_uq:false")
    return out


# ---------------------------------------------------------------- PG → GEP
FAMILY_NOTE = {
    "ridge": "线性先验 + L2，小样本最稳，可解释性最强",
    "pls": "偏最小二乘，适合共线性的配比数据",
    "rf": "随机森林，捕获非线性交互，UQ 需外挂 conformal",
    "gbr": "梯度提升，拟合能力强，样本不足时易过拟合",
    "gp": "高斯过程，原生给出预测区间，小样本 + 需 UQ 首选",
    "xgb": "XGBoost，表格数据强基线，容量大需控过拟合",
}


def gene_to_gep(row):
    fam = row["family"]
    g = {
        "type": "Gene", "schema_version": SCHEMA_VERSION,
        "id": row["id"], "category": "optimize",
        "signals_match": gene_signals(row.get("signals_match")),
        "preconditions": ["数据来自 mat.v_arena_dataset，按 created_at 排序",
                          "golden 集冻结且只评估一次"],
        "strategy": [
            f"选择算法族 {fam}：{FAMILY_NOTE.get(fam, '')}",
            "从 PG 语义视图取数并构建机理特征（当量比等）",
            "时间前向滚动评估（禁止随机切分）",
            "golden 集只评一次，做 split-conformal 覆盖率与基线增益双闸",
            "Capsule 写入 PG，负例同样入库",
        ],
        "constraints": {"max_files": 0,
                        "forbidden_paths": ["arena.py", "src/", "doc/files/schema.sql"]},
        "validation": ["node run_eval.js --check"],
        "summary": f"{fam} 小样本配方建模策略（{FAMILY_NOTE.get(fam, '')}）",
        "claims": [{"predicate": "split:time_forward and golden_evaluated_once",
                    "kind": "behavioral"}],
        "scope": {"signals": gene_signals(row.get("signals_match")),
                  "predicate": f"n in [{int((row.get('signals_match') or {}).get('n_min', 0))},"
                               f"{int((row.get('signals_match') or {}).get('n_max', 10**9))}]"},
        "anti_patterns": [{"pattern": p, "reason": r} for p, r in
                          (("random_split", "破坏时间前向假设"),
                           ("tune_on_golden", "golden 反复试 → 假冠军"))],
    }
    return stamp(g)


def capsule_to_gep(row, family=None, target="lap_shear_strength", system=None):
    """system 显式传入优先：兼容 PG 行字典尚未同步新列的情况。"""
    system = system or (row.get("system") if hasattr(row, "get") else None)
    passed = bool(row["passed"])
    hm = float(row["holdout_mae"]) if row["holdout_mae"] is not None else 0.0
    cov = float(row["coverage"]) if row["coverage"] is not None else 0.0
    c = {
        "type": "Capsule", "schema_version": SCHEMA_VERSION,
        "id": f"capsule_{row['run_id']}_{str(row['id'])[:8]}",
        "trigger": [f"arena_round:{row['round']}", f"target:{target}",
                    f"system:{row.get('system') or 'unknown'}"],
        "gene": row["gene_id"],
        "summary": (f"round={row['round']} {family or row['gene_id']} "
                    f"holdout_mae={hm:.3f} coverage={cov:.2f} "
                    f"{'通过闸门' if passed else '未过闸门：' + str(row['fail_reason'])}"),
        "confidence": round(0.9 - 0.5 * abs(cov - 0.90) if passed else 0.2, 3),
        "blast_radius": {"files": 0, "lines": 0},          # 不改代码，只产出配置
        "outcome": {"status": "success" if passed else "failed",
                    "score": round(1.0 / (1.0 + hm), 4)},
        "success_streak": 1 if passed else 0,
        "env_fingerprint": row.get("env_fingerprint") or {},
        "source_type": "generated",
        "content": {"config": row["config"], "family": family,
                    "dev_mae": float(row["dev_mae"]) if row["dev_mae"] is not None else None,
                    "trials_total": row["trials_total"], "split": row["split"]},
        "strategy": [f"split={row['split']}", f"trials_total={row['trials_total']}"],
    }
    if passed:
        c["success_reason"] = f"holdout_mae={hm:.3f} 且 conformal 覆盖率 {cov:.2f} 落在合格带"
    return stamp(c)


def event_to_gep(row):
    kind = row["kind"]
    ok = kind in ("promote", "gate", "evaluate", "select", "critique", "start", "inherit")
    e = {
        "type": "EvolutionEvent", "schema_version": SCHEMA_VERSION,
        "id": f"evt_{row['id']}",
        "intent": "optimize",
        "signals": [f"actor:{row['actor']}", f"kind:{kind}"],
        "genes_used": [row["payload"].get("gene_id")] if isinstance(row.get("payload"), dict)
                       and row["payload"].get("gene_id") else [],
        "mutation_id": f"mut_{row['run_id']}",
        "blast_radius": {"files": 0, "lines": 0},
        "outcome": {"status": "success" if kind == "promote" else "failed",
                    "score": 0.0},
        "source_type": "generated",
        "meta": {"actor": row["actor"], "kind": kind, "run_id": row["run_id"],
                 "payload": row["payload"]},
    }
    return stamp(e)


# ---------------------------------------------------------------- 资产文件读写（spec §8）
def write_assets(genes, capsules, events, home=None):
    """按 @evomap/evolver 2.x 实际期望的资产目录落盘：
    genes.jsonl（每行一个 Gene，非 genes.json）、capsules.jsonl、events.jsonl、
    anti-genes.jsonl + provenance/review/asset-sync 三个 sidecar（内容可空，真实 Evolver 会扫描）。"""
    home = Path(home or GEP_HOME)
    home.mkdir(parents=True, exist_ok=True)
    with (home / "genes.jsonl").open("w", encoding="utf-8") as f:
        for g in genes:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")
    with (home / "capsules.jsonl").open("w", encoding="utf-8") as f:
        for c in capsules:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    with (home / "events.jsonl").open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    for side in ("anti-genes.jsonl", "provenance.jsonl", "review.jsonl", "asset-sync.jsonl"):
        p = home / side
        if not p.exists():
            p.write_text("", encoding="utf-8")
    return home


def read_assets(home=None):
    home = Path(home or GEP_HOME)
    genes = []
    gj = home / "genes.jsonl"
    if gj.exists():
        genes = [json.loads(l) for l in gj.read_text(encoding="utf-8").splitlines() if l.strip()]
    else:  # 兼容早期 genes.json 格式
        gj = home / "genes.json"
        if gj.exists():
            genes = json.loads(gj.read_text(encoding="utf-8")).get("genes", [])
    capsules = [json.loads(l) for l in (home / "capsules.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()] if (home / "capsules.jsonl").exists() else []
    events = [json.loads(l) for l in (home / "events.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()] if (home / "events.jsonl").exists() else []
    return genes, capsules, events


def verify_assets(home=None):
    """追加式完整性校验：asset_id 必须与内容自洽（篡改即失败）。"""
    genes, capsules, events = read_assets(home)
    bad = [a.get("id") for a in genes + capsules + events if a.get("asset_id") != asset_id(a)]
    return len(genes + capsules + events), bad
