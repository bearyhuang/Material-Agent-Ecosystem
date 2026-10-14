"""EvoMap Selector：按 GEP 资产（Gene + Capsule）为新任务选策略并给出热启动配置。

选择依据（全是可计算的，不靠 LLM 拍脑袋）：
  1. signals_match 命中数（spec 三种格式）
  2. 该 Gene 历史 Capsule 的表现：通过率、平均 holdout_mae、success_streak
  3. anti_patterns（禁忌）一票否决
"""
from collections import defaultdict

from fsgep import GEP_HOME, read_assets, signal_match


def score_genes(genes, capsules, signals, top=6):
    """返回按分数排序的 [(gene, score, stats)]。"""
    by_gene = defaultdict(list)
    for c in capsules:
        by_gene[c.get("gene")].append(c)

    scored = []
    for g in genes:
        if g.get("category") not in (None, "optimize", "repair", "innovate", "explore"):
            continue
        hits = sum(1 for p in g.get("signals_match", []) if signal_match(p, signals))
        caps = by_gene.get(g["id"], [])
        passed = [c for c in caps if c.get("outcome", {}).get("status") == "success"]
        maes = [c["content"].get("dev_mae") for c in caps
                if isinstance(c.get("content"), dict) and c["content"].get("dev_mae") is not None]
        pass_rate = len(passed) / len(caps) if caps else 0.0
        mean_mae = sum(maes) / len(maes) if maes else None
        # 分数：信号命中 + 历史表现（无历史时不惩罚，只靠先验）
        s = 0.5 * (hits / max(len(g.get("signals_match", [])) or 1, 1)) \
            + 0.3 * pass_rate \
            + 0.2 * (1.0 / (1.0 + mean_mae) if mean_mae else 0.5)
        scored.append((g, round(s, 4), {"hits": hits, "capsules": len(caps),
                                        "pass_rate": round(pass_rate, 3),
                                        "mean_dev_mae": None if mean_mae is None else round(mean_mae, 3)}))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top]


def warm_configs(genes, capsules, signals, limit=6, system=None):
    """给 arena --inherit 用：优先继承通过闸门且信号命中的历史配置。

    system 过滤： Capsule 的 trigger 里带 system:<x>，与当前体系不一致的一律不继承。
    旧资产没有 system 标记时无法判断来源 → 保守起见排除（宁可不继承，也不跨体系污染）。
    """
    ranked = score_genes(genes, capsules, signals)
    keep = {g["id"] for g, _, _ in ranked}

    def _same_system(c):
        tags = [t for t in c.get("trigger", []) if t.startswith("system:")]
        if not tags:
            return False                      # 来源不明 → 不继承
        if system is None:
            return True                       # 调用方未指定体系，交由上层控制
        return any(t == f"system:{system}" for t in tags)

    cand = [c for c in capsules
            if c.get("gene") in keep
            and c.get("outcome", {}).get("status") == "success"
            and _same_system(c)
            and isinstance(c.get("content"), dict)]
    cand.sort(key=lambda c: (c["content"].get("dev_mae") is None,
                             c["content"].get("dev_mae") or 9e9))
    out = []
    for c in cand:
        fam = c["content"].get("family")
        if fam and c["content"].get("config") is not None:
            out.append({"family": fam, "config": c["content"]["config"],
                        "dev_mae": c["content"].get("dev_mae") or 9e9,
                        "gene_id": c.get("gene")})
    return out[:limit]


def select(signals, home=None, top=6, limit=6, system=None):
    genes, capsules, _ = read_assets(home or GEP_HOME)
    ranked = score_genes(genes, capsules, signals, top)
    return ranked, warm_configs(genes, capsules, signals, limit, system=system)
