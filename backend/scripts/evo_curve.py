#!/usr/bin/env python3
"""进化曲线：读 PG evo.capsule，按轮次画 holdout MAE / 累计试验次数 / 通过率。

  python scripts/evo_curve.py                 # 画到 artifacts/evolution_curve.png
  python scripts/evo_curve.py --out /tmp/c.png
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "Noto Sans CJK JP", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

from fsdb import connect

PER_ROUND_SQL = """
select round,
       min(holdout_mae)::float8 as best_mae,
       avg(holdout_mae)::float8 as mean_mae,
       max(trials_total)        as trials,
       count(*)                 as n,
       sum(case when passed then 1 else 0 end)::float8 / count(*) as pass_rate
from evo.capsule group by round order by round
"""
PER_FAMILY_SQL = """
select c.round, g.family, min(c.holdout_mae)::float8 as best_mae
from evo.capsule c join evo.gene g on g.id = c.gene_id
group by c.round, g.family order by g.family, c.round
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/workspace/artifacts/evolution_curve.png")
    ap.add_argument("--dhash", default=None,
                    help="只统计同一数据切分（dataset_hash）的轮次，避免跨体系轮次污染均值")
    a = ap.parse_args()

    where = "where dataset_hash = %(dhash)s" if a.dhash else ""
    with connect() as c, c.cursor() as cur:
        cur.execute(PER_ROUND_SQL.replace("group by round", where + " group by round"),
                    {"dhash": a.dhash})
        rounds = cur.fetchall()
        cur.execute(PER_FAMILY_SQL.replace("group by c.round", where + " group by c.round"),
                    {"dhash": a.dhash})
        fam = cur.fetchall()
    if not rounds:
        print("evo.capsule 为空，先跑 arena.py / swarm.py")
        return

    rounds = [dict(r) for r in rounds]
    fam = [dict(r) for r in fam]

    print("\n=== 进化曲线（每轮）===")
    print(f"{'轮次':<6}{'最优MAE':>9}{'平均MAE':>9}{'试验次数':>9}{'Capsule':>8}{'通过率':>8}")
    cum = 0
    for r in rounds:
        cum += r["trials"] or 0
        print(f"{r['round']:<6}{r['best_mae']:>9.3f}{r['mean_mae']:>9.3f}{r['trials']:>9}"
              f"{r['n']:>8}{r['pass_rate']:>8.0%}")
    print(f"累计试验总次数：{cum}（试得越多，胜者越被高估 → 报告必须暴露）")

    fig, ax = plt.subplots(1, 3, figsize=(15, 4))
    xs = [r["round"] for r in rounds]
    ax[0].plot(xs, [r["best_mae"] for r in rounds], "o-", label="最优 holdout MAE")
    ax[0].plot(xs, [r["mean_mae"] for r in rounds], "s--", label="平均 holdout MAE")
    ax[0].set_xlabel("round"); ax[0].set_ylabel("MAE (MPa)"); ax[0].set_title("进化曲线")
    ax[0].legend(); ax[0].grid(alpha=.3)

    for f in sorted({r["family"] for r in fam}):
        pts = [(r["round"], r["best_mae"]) for r in fam if r["family"] == f]
        ax[1].plot([p[0] for p in pts], [p[1] for p in pts], "o-", label=f)
    ax[1].set_xlabel("round"); ax[1].set_ylabel("最优 MAE"); ax[1].set_title("各算法族")
    ax[1].legend(fontsize=8); ax[1].grid(alpha=.3)

    ax[2].bar(xs, [r["trials"] for r in rounds], color="#4C78A8", label="本轮试验次数")
    ax[2].set_xlabel("round"); ax[2].set_ylabel("trials"); ax[2].set_title("搜索代价（累计可见）")
    ax[2].grid(alpha=.3, axis="y")

    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(); fig.savefig(out, dpi=120)
    print(f"\n图已保存：{out}")
    print("⚠️ 数据为合成/脱敏数据，进化的是策略资产（哪类数据体制用哪种算法），不是模型权重")


if __name__ == "__main__":
    main()
