#!/usr/bin/env python3
"""方案 C · 四级关联库跨项目发现（命令行入口）

用法：
  python discover.py "环氧树脂 E51 被哪些项目用了？"          # 问答（带溯源）
  python discover.py --demo                                  # 跑 5 条演示剧本（方案 C 第 6 节）
  python discover.py --json "..."                            # 机器可读输出
  python discover.py --genealogy E51                         # 谱系查询
  python discover.py --similar FS-EP-0001                    # 相似配方检索
  python discover.py --attribute FS-EP-0001                  # 归因候选排序
  python discover.py --compliance FS-EP-0001                 # 合规
  python discover.py --po PO-20260923-001                    # 采购订单只读链路
  python discover.py --lot L2489-01                          # 批次追溯
  python discover.py --check                                 # 验收自检：3 条谱系查询 + 质量守恒
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import fslineage as L                     # noqa: E402
from fsqna import DEMOS, ask              # noqa: E402


def show_answer(out):
    print(f"\n【意图】{out['intent']} ｜ trace={out.get('trace_id')} ｜ engine={out.get('engine')}")
    print(f"【问题】{out['question']}")
    print(f"【回答】{out['answer']}")
    print("【证据链】")
    for i, e in enumerate(out["evidence"], 1):
        rows = e["rows"][:3]
        print(f"  {i}. {e['kind']} ← {e['source']}（{e['n_rows']} 行）")
        print(f"     SQL: {e['sql']}")
        for r in rows:
            print(f"     · {json.dumps(r, ensure_ascii=False, default=str)[:200]}")


def check():
    """方案 C 验收自检。"""
    ok = True

    def step(name, cond, detail=""):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")
        ok = ok and bool(cond)

    print("== 方案 C 验收自检 ==")
    with __import__("fsdb").connect() as c, c.cursor() as cur:
        cur.execute("""select count(*) filter (where basis='mass_pct' and abs(t-100)>0.5) bad,
                              count(*) filter (where basis='mass_pct') n
                       from (select f.basis, sum(p.pct) t
                             from mat.formulation_raw_pct p join mat.formulation f on f.id=p.formulation_id
                             group by 1, p.formulation_id, f.basis) x""")
        r = cur.fetchone()
    step("质量守恒（mass_pct 体系 Σ=100±0.5）", r["bad"] == 0,
         f"（{r['n']} 个配方，违规 {r['bad']}）")

    g = L.material_usage("E51")
    step("谱系查询 1：原料 → 项目/配方/性能", len(g["rows"]) > 0,
         f"（{len(g['rows'])} 条）")
    lot = "L2489-01"
    lt = L.lot_trace(lot)
    step("谱系查询 2：批次 → 配方 → 性能", len(lt["rows"]) > 0, f"（{len(lt['rows'])} 条）")
    sm = L.similar_formulations("FS-EP-0001", 3)
    step("谱系查询 3：相似配方检索", len(sm["rows"]) > 0, f"（{len(sm['rows'])} 条）")
    pt = L.po_trace("PO-20260923-001")
    step("彩蛋：采购订单只读链路", len(pt["rows"]) > 0, f"（{len(pt['rows'])} 条）")
    at = L.attribute("FS-EP-0001")
    step("归因候选（带证据链）", len(at["candidates"]) > 0, f"（{len(at['candidates'])} 条候选）")
    out = ask("环氧树脂 E51 被哪些项目用了？")
    step("问答溯源（evidence 非空）", len(out["evidence"]) > 0, f"（trace={out['trace_id']}）")
    print("== 结果 ==", "全部通过" if ok else "存在失败项")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="?", default=None)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--genealogy", metavar="MATERIAL")
    ap.add_argument("--similar", metavar="FORMULATION")
    ap.add_argument("--attribute", metavar="FORMULATION")
    ap.add_argument("--compliance", metavar="FORMULATION")
    ap.add_argument("--po", metavar="PO_NO")
    ap.add_argument("--lot", metavar="LOT_NO")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    if not a.llm:
        import os
        os.environ["FS_NO_LLM"] = "1"

    if a.check:
        return check()
    if a.genealogy:
        r = L.material_usage(a.genealogy)
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        return 0
    if a.similar:
        print(json.dumps(L.similar_formulations(a.similar), ensure_ascii=False, indent=2,
                         default=str))
        return 0
    if a.attribute:
        print(json.dumps(L.attribute(a.attribute), ensure_ascii=False, indent=2, default=str))
        return 0
    if a.compliance:
        print(json.dumps(L.compliance_of(a.compliance), ensure_ascii=False, indent=2, default=str))
        return 0
    if a.po:
        print(json.dumps(L.po_trace(a.po), ensure_ascii=False, indent=2, default=str))
        return 0
    if a.lot:
        print(json.dumps(L.lot_trace(a.lot), ensure_ascii=False, indent=2, default=str))
        return 0

    qs = DEMOS if a.demo else ([a.question] if a.question else [])
    if not qs:
        ap.print_help()
        return 1
    for q in qs:
        out = ask(q, use_llm=a.llm)
        if a.json:
            print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        else:
            show_answer(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
