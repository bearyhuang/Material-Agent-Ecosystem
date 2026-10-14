#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实公开数据集装载器：UCI Concrete Compressive Strength → mat.* 四级库。

数据源性质（务必如实声明）
------------------------------------------------------------------
* UCI ML Repository #165，Prof. I-Cheng Yeh, Chung-Hua University（2007 捐赠）
* 1030 条**真实实验室实测**：7 个组分 (kg/m^3) + 养护龄期 (day) → 抗压强度 (MPa)
* 是真实实验数据，**不是 MD 模拟值**，也不是本项目"按机理方程合成"的数据
* 用途：在真实信号下检验「防自欺闸门 + 经验继承」是否还成立

两个必须诚实标注的适配妥协
------------------------------------------------------------------
1. **伪时间轴**：真实数据没有时间戳。created_at 按原始文件行序等距生成
   （行序 ≈ 采集先后代理），故「时间前向切分」检验的是分组漂移鲁棒性，
   而非真实时间漂移。
2. **龄期走伪原料槽位**：schema 没有"养护龄期"列。Age 放入 composition 字典
   （raw code = CURE_AGE_DAY）。工具层因此零改动，也不伪造未知数值。
   → 必须用 feature_mode='raw'（组分+工艺+体系独热），不要用 'both'：
     cat/mech 是针对环氧体系设计的当量比等化学特征，对混凝土全为 0。

用法（在 fs-arena 容器内执行）：
  python scripts/load_real_concrete.py                          # 全量 1030 条 → system=CONCRETE
  python scripts/load_real_concrete.py --limit 200 --system CONCRETE_SMALL
  python scripts/load_real_concrete.py --dry-run                # 只看统计，不写库
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 容器内路径优先，宿主机路径兜底
XLS = [Path("/workspace/data_raw/concrete/Concrete_Data.xls"),
       Path(__file__).resolve().parent.parent / "data_raw" / "concrete" / "Concrete_Data.xls"]

TEST_ITEM = "compressive_strength_MPa"
TARGET_UNIT = "MPa"
PROJECT_CODE = "REAL-UCI-165"

# 组分 → 中性类别（不复用环氧体系的 resin/hardener 语义，避免假装水泥是树脂）
COMPONENTS = [
    ("CONC_CEMENT",     "Cement (component 1)(kg in a m^3 mixture)",            "binder"),
    ("CONC_SLAG",       "Blast Furnace Slag (component 2)(kg in a m^3 mixture)", "scm"),
    ("CONC_FLYASH",     "Fly Ash (component 3)(kg in a m^3 mixture)",           "scm"),
    ("CONC_WATER",      "Water  (component 4)(kg in a m^3 mixture)",            "water"),
    ("CONC_SUPERPLAST", "Superplasticizer (component 5)(kg in a m^3 mixture)",  "admixture"),
    ("CONC_COARSE_AGG", "Coarse Aggregate  (component 6)(kg in a m^3 mixture)", "aggregate"),
    ("CONC_FINE_AGG",   "Fine Aggregate (component 7)(kg in a m^3 mixture)",    "aggregate"),
]
AGE_COL = "Age (day)"
Y_COL = "Concrete compressive strength(MPa, megapascals) "
AGE_CODE = "CURE_AGE_DAY"
ALL_RAW = [(c, cat) for c, _x, cat in COMPONENTS] + [(AGE_CODE, "process")]


def family_of(it):
    """配方族：按胶凝体系划分 —— Red-team 分组漂移质疑的依据。"""
    slag, fly = it["CONC_SLAG"] > 0, it["CONC_FLYASH"] > 0
    base = {0: "plain", 1: "slag", 2: "flyash", 3: "ternary"}[slag * 1 + fly * 2]
    return base + "_sp" if it["CONC_SUPERPLAST"] > 0 else base


def load_frame():
    import pandas as pd
    for p in XLS:
        if p.exists():
            return pd.read_excel(p)
    raise SystemExit(f"找不到数据文件：{[str(p) for p in XLS]}")


def build_rows(limit, prefix="CONC"):
    df = load_frame()
    rows = []
    for i, r in df.iterrows():
        if limit and len(rows) >= limit:
            break
        rows.append({
            "code": f"{prefix}-{i:04d}",
            "items": {code: float(r[col]) for code, col, _c in COMPONENTS},
            "age": float(r[AGE_COL]),
            "y": float(r[Y_COL]),
        })
    base = datetime.fromisoformat("2023-01-01")
    for i, r in enumerate(rows):
        r["created_at"] = (base + timedelta(hours=8 * i)).isoformat(sep=" ")
        r["family"] = family_of(r["items"])
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", default="CONCRETE")
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 条（模拟小样本体制）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    # code 必须全局唯一（formulation_code_version_key），故把 system 编进前缀
    rows = build_rows(a.limit, prefix=a.system.replace("_", ""))
    fam = {}
    for r in rows:
        fam[r["family"]] = fam.get(r["family"], 0) + 1
    ys = [r["y"] for r in rows]
    print(f"system={a.system}  n={len(rows)}")
    print(f"配方族分布：{fam}")
    print(f"{TEST_ITEM}：min={min(ys):.2f} max={max(ys):.2f} "
          f"mean={sum(ys) / len(ys):.2f} {TARGET_UNIT}")
    if a.dry_run:
        print("--dry-run：未写库")
        return

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from fsdb import connect

    with connect() as c, c.cursor() as cur:
        cur.execute("select id from mat.formulation where system = %s", (a.system,))
        old = [r["id"] for r in cur.fetchall()]
        if old:
            cur.execute("delete from mat.performance where formulation_id = any(%s)", (old,))
            cur.execute("delete from mat.formulation_item where formulation_id = any(%s)", (old,))
            cur.execute("delete from mat.formulation where id = any(%s)", (old,))
            print(f"已清理旧数据 {len(old)} 条")

        cur.execute("insert into mat.supplier(code,name,country,tier) values (%s,%s,%s,%s) "
                    "on conflict (code) do nothing", ("UCI", "UCI ML Repository", "Taiwan", "C"))
        cur.execute("select id from mat.supplier where code='UCI'")
        sup_id = cur.fetchone()["id"]
        for code, cat in ALL_RAW:
            cur.execute("insert into mat.raw_material(code,name,category,props) "
                        "values (%s,%s,%s,%s::jsonb) on conflict (code) do nothing",
                        (code, code, cat, json.dumps({"source": "UCI-165"})))
        cur.execute("update mat.raw_material set supplier_id=%s where code like 'CONC_%%' "
                    "or code=%s", (sup_id, AGE_CODE))
        cur.execute("select code, id from mat.raw_material where code like 'CONC_%%' or code=%s",
                    (AGE_CODE,))
        rid = {r["code"]: r["id"] for r in cur.fetchall()}

        cur.execute("insert into mat.project(code,name,customer) values (%s,%s,%s) "
                    "on conflict (code) do nothing",
                    (PROJECT_CODE, "真实公开数据验证（UCI #165）", "UCI ML Repository"))
        cur.execute("select id from mat.project where code=%s", (PROJECT_CODE,))
        pid = cur.fetchone()["id"]

        for r in rows:
            cur.execute("insert into mat.formulation(code,version,system,stage,project_id,"
                        "created_at,family) values (%s,1,%s,'lab',%s,%s,%s) returning id",
                        (r["code"], a.system, pid, r["created_at"], r["family"]))
            fid = cur.fetchone()["id"]
            items = dict(r["items"])
            items[AGE_CODE] = r["age"]            # 龄期走伪原料槽位
            for code, v in items.items():
                cur.execute("insert into mat.formulation_item(formulation_id,raw_material_id,"
                            "mass_pct) values (%s,%s,%s)", (fid, rid[code], v))
            cur.execute("insert into mat.performance(formulation_id,test_item,value,unit,"
                        "condition,tested_at) values (%s,%s,%s,%s,'{}'::jsonb,%s)",
                        (fid, TEST_ITEM, r["y"], TARGET_UNIT, r["created_at"]))
        c.commit()

    print(f"✅ 已装载 {len(rows)} 条真实数据 → system={a.system} "
          f"（test_item={TEST_ITEM}）；合成数据未受影响")


if __name__ == "__main__":
    main()
