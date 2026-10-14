#!/usr/bin/env python3
"""PG18 ⇄ EvoMap GEP 资产同步（离线，不连 Hub）。

  python scripts/evo_sync.py --export    # PG → .evolver/gep/{genes.json,capsules.jsonl,events.jsonl}
  python scripts/evo_sync.py --verify    # 校验 asset_id 自洽（资产不可篡改）
  python scripts/evo_sync.py --show      # 打印资产摘要
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fsdb import connect
from fsgep import (GEP_HOME, capsule_to_gep, event_to_gep, gene_to_gep,
                   read_assets, verify_assets, write_assets)

GENE_SQL = "select id, family, signals_match, strategy, avoid, validation from evo.gene order by id"
CAPSULE_SQL = """
select c.id, c.gene_id, c.run_id, c.round, c.system, c.config, c.split, c.dev_mae, c.holdout_mae,
       c.coverage, c.trials_total, c.passed, c.fail_reason, c.env_fingerprint, g.family
from evo.capsule c join evo.gene g on g.id = c.gene_id
order by c.round, c.created_at
"""
EVENT_SQL = "select id, run_id, actor, kind, payload from evo.evolution_event order by id"


def export(home=None):
    with connect() as c, c.cursor() as cur:
        cur.execute(GENE_SQL); genes = [gene_to_gep(r) for r in cur.fetchall()]
        cur.execute(CAPSULE_SQL)
        rows = cur.fetchall()
        capsules = [capsule_to_gep(r, family=r.get("family")) for r in rows]
        cur.execute(EVENT_SQL); events = [event_to_gep(r) for r in cur.fetchall()]
    home = write_assets(genes, capsules, events, home)
    passed = sum(1 for c in capsules if c["outcome"]["status"] == "success")
    print(f"已导出 GEP 资产 → {home}")
    print(f"  Gene {len(genes)} 条 / Capsule {len(capsules)} 条（通过 {passed}）/ Event {len(events)} 条")
    return home


def show(home=None):
    genes, capsules, events = read_assets(home)
    print(f"资产目录 {home or GEP_HOME}：Gene {len(genes)} / Capsule {len(capsules)} / Event {len(events)}")
    for g in genes:
        print(f"  {g['id']:<24} signals={g['signals_match']}")
    for c in capsules[-5:]:
        print(f"  {c['id']:<34} {c['outcome']['status']:<7} score={c['outcome']['score']} "
              f"conf={c['confidence']} | {c['summary'][:60]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--home", default=None)
    a = ap.parse_args()
    if a.export:
        export(a.home)
    n, bad = verify_assets(a.home)
    if a.verify or a.export:
        print(f"  资产完整性校验：{n} 条，{'全部自洽 ✅' if not bad else '被篡改/损坏 ❌ ' + str(bad[:3])}")
    if a.show or not (a.export or a.verify):
        show(a.home)


if __name__ == "__main__":
    main()
