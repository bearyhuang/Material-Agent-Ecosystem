#!/usr/bin/env python3
"""把 PG 里的批准留痕与预测登记推送上链（ai4s04 必做档 #4 / #5）。

    python scripts/chain_push.py --dry-run          # 只看会锚什么、哈希是什么
    python scripts/chain_push.py --approvals        # 只推批准留痕
    python scripts/chain_push.py --predictions      # 只推预测登记
    python scripts/chain_push.py --run-id s1-abc123 # 只推某个 run
    python scripts/chain_push.py --no-redact        # 预测数值明文上链（仅演示/合成数据）

降级：FS_CHAIN=0 或 RPC 不可达时不报错，落 artifacts/chain/anchor_queue.jsonl 待重放。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import fschain  # noqa: E402

PLAN_SQL = """
select id, run_id, round, candidate_id, target, acquisition, status, approved_by, approved_at
from evo.experiment_plan
where status in ('approved','executed')
  and approved_by is not null and approved_by <> ''
  and (%(run_id)s::text is null or run_id = %(run_id)s)
order by approved_at nulls last, id
limit %(limit)s
"""

PRED_SQL = """
select experiment_no, model_version, predicted, ci_low, ci_high, rationale, registered_at
from evo.prediction_register
where (%(run_id)s::text is null or experiment_no like '%%' || %(run_id)s || '%%')
order by registered_at desc nulls last, id desc
limit %(limit)s
"""


def fetch(sql: str, run_id: str | None, limit: int):
    from fsdb import connect  # 项目自有连接方式（读 DATABASE_URL）

    with connect() as c, c.cursor() as cur:
        cur.execute(sql, {"run_id": run_id, "limit": limit})
        return [dict(r) for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description="推送批准留痕 / 预测登记上链")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--approvals", action="store_true")
    ap.add_argument("--predictions", action="store_true")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--limit", type=int, default=1000,
                    help="每类最多拉取条数（脚本按合约上限 200 自动分片，当前库 259/295 条可一次推完）")
    ap.add_argument("--no-redact", action="store_true", help="预测数值明文上链（默认脱敏）")
    ap.add_argument("--no-batch", action="store_true", help="批准留痕逐条发交易（默认批量）")
    args = ap.parse_args()

    do_approvals = args.approvals or not (args.approvals or args.predictions)
    do_predictions = args.predictions or not (args.approvals or args.predictions)
    redacted = not args.no_redact
    c = fschain.cfg()
    print(f"[i] FS_CHAIN={'on' if c['enabled'] else 'off'}  rpc={c['rpc'] or '(未配置)'}  "
          f"contract={c['contract'] or '(未部署)'}  redacted={redacted}")

    summary: dict = {"approvals": None, "predictions": None}

    # ---------------- ① 批准留痕
    if do_approvals:
        try:
            rows = fetch(PLAN_SQL, args.run_id, args.limit)
        except Exception as e:
            print(f"[!] 读取 evo.experiment_plan 失败：{e}")
            rows = []
        print(f"\n① 批准留痕：{len(rows)} 条")
        for r in rows[:5]:
            print(f"   plan#{r['id']} run={r['run_id']} R{r['round']} status={r['status']} "
                  f"planHash=0x{fschain.plan_hash(r).hex()[:16]}… "
                  f"approverHash=0x{fschain.approver_hash(r.get('approved_by')).hex()[:16]}…")
        if len(rows) > 5:
            print(f"   … 共 {len(rows)} 条")
        if args.dry_run:
            print("   → [DRY-RUN] 仅预览，未上链、未落队列")
            summary["approvals"] = {"preview": len(rows)}
        elif not fschain.enabled():
            if rows:
                n = fschain.queue("approval", rows)
                print(f"   → 链不可用，已落队列 {n} 条：{fschain.QUEUE_FILE}")
            summary["approvals"] = {"queued": len(rows)}
        else:
            try:
                res = fschain.send_approvals(rows, batch=not args.no_batch)
                summary["approvals"] = res
                print(f"   → 已上链 {res.get('sent', 0)} 条")
                for t in res.get("txs", []):
                    print(f"      tx {t.get('tx_hash')} block={t.get('block')} "
                          f"gas={t.get('gasUsed')} {t.get('explorer', '')}")
            except Exception as e:
                print(f"   [!] 上链失败（不影响主业务）：{e}")
                n = fschain.queue("approval", rows)
                print(f"   → 已落队列 {n} 条待重放")

    # ---------------- ② 预测登记锁
    if do_predictions:
        try:
            rows = fetch(PRED_SQL, args.run_id, args.limit)
        except Exception as e:
            print(f"[!] 读取 evo.prediction_register 失败：{e}")
            rows = []
        print(f"\n② 预测登记：{len(rows)} 条")
        for r in rows[:5]:
            print(f"   {r['experiment_no']} predicted={'***' if redacted else r.get('predicted')} "
                  f"regHash=0x{fschain.prediction_hash(r, redacted=redacted).hex()[:16]}…")
        if len(rows) > 5:
            print(f"   … 共 {len(rows)} 条")
        if args.dry_run:
            print("   → [DRY-RUN] 仅预览，未上链、未落队列")
            summary["predictions"] = {"preview": len(rows)}
        elif not fschain.enabled():
            if rows:
                n = fschain.queue("prediction", rows, redacted=redacted)
                print(f"   → 链不可用，已落队列 {n} 条：{fschain.QUEUE_FILE}")
            summary["predictions"] = {"queued": len(rows)}
        else:
            try:
                res = fschain.send_predictions(rows, redacted=redacted)
                summary["predictions"] = res
                print(f"   → 已上链 {res.get('sent', 0)} 条")
                for t in res.get("txs", []):
                    print(f"      tx {t.get('tx_hash')} block={t.get('block')} "
                          f"gas={t.get('gasUsed')} {t.get('explorer', '')}")
            except Exception as e:
                print(f"   [!] 上链失败（不影响主业务）：{e}")
                n = fschain.queue("prediction", rows, redacted=redacted)
                print(f"   → 已落队列 {n} 条待重放")

    print("\n" + json.dumps(summary, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
