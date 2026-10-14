#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第三阶段：下载 Europe PMC 的**公开补充材料**（ZIP，常含作者整理好的 xlsx/csv）。

为什么这条优先级最高
------------------------------------------------------------------
scripts/extract_fulltext.py 是"从 PDF/XML 表格里抠数字"，误差不可避免；
而 supplementaryFiles 里往往是作者自己整理的原始数据表 —— 精度高一个量级，
且常常直接就有"配方组成 → 性能"的完整行。

合法性：`/{PMCID}/supplementaryFiles` 与全文一样是 Europe PMC 官方开放接口，
只对本来就开放获取的文献生效，不是绕过付费墙。

输出
------------------------------------------------------------------
  data_raw/harvest/supp/<PMCID>/          解包后的附件（xlsx/csv/docx/zip...）
  data_raw/harvest/supplementary_index.jsonl   附件索引（含大小与类型）

用法
------------------------------------------------------------------
  python scripts/fetch_supplementary.py --limit-per-system 4
"""
import argparse
import io
import json
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

OUT = Path("/workspace/data_raw/harvest") if Path("/workspace").exists() \
    else Path(__file__).resolve().parent.parent / "data_raw" / "harvest"
SUPP = OUT / "supp"
REST = "https://www.ebi.ac.uk/europepmc/webservices/rest"
UA = "FormulaSwarm-supp/0.1 (mailto:formulaswarm@example.com)"
DATA_EXT = (".xlsx", ".xls", ".csv", ".txt", ".tsv", ".docx", ".pdf", ".zip")


def fetch_bin(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(1.5 * (i + 1))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--unified", default=str(OUT / "unified.jsonl"))
    ap.add_argument("--systems", nargs="*", default=None)
    ap.add_argument("--limit-per-system", type=int, default=4)
    ap.add_argument("--sleep", type=float, default=0.4)
    a = ap.parse_args()

    src = [json.loads(l) for l in Path(a.unified).read_text(encoding="utf-8").splitlines() if l.strip()]
    seen, cand = set(), []
    for r in src:
        if (r.get("system"), r.get("pmcid")) in seen or not r.get("pmcid"):
            continue
        if a.systems and r["system"] not in a.systems:
            continue
        seen.add((r["system"], r["pmcid"]))
        cand.append(r)

    by_sys = {}
    for r in cand:
        by_sys.setdefault(r["system"], []).append(r)

    SUPP.mkdir(parents=True, exist_ok=True)
    idx = (OUT / "supplementary_index.jsonl").open("w", encoding="utf-8")
    n_zip = n_file = 0
    print(f"{'体系':<14}{'取件':>5}{'有附件':>7}{'数据表文件':>10}")
    for syscode in sorted(by_sys):
        got_zip = got_files = 0
        for rec in by_sys[syscode][:a.limit_per_system]:
            pmcid = rec["pmcid"]
            raw = fetch_bin(f"{REST}/{pmcid}/supplementaryFiles")
            time.sleep(a.sleep)
            if not raw or len(raw) < 64:
                continue
            try:
                zf = zipfile.ZipFile(io.BytesIO(raw))
            except Exception:
                continue
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if not names:
                continue
            got_zip += 1
            dest = SUPP / pmcid
            dest.mkdir(parents=True, exist_ok=True)
            meta = []
            for n in names:
                try:
                    data = zf.read(n)
                except Exception:
                    continue
                p = dest / Path(n).name
                p.write_bytes(data)
                meta.append({"name": n, "size": len(data)})
                got_files += 1
                if n.lower().endswith((".xlsx", ".xls", ".csv", ".tsv")):
                    pass
            idx.write(json.dumps({"pmcid": pmcid, "doi": rec.get("doi"), "system": syscode,
                                  "system_zh": rec.get("system_zh"), "title": rec.get("title"),
                                  "files": meta,
                                  "endpoint": f"{REST}/{pmcid}/supplementaryFiles"},
                                 ensure_ascii=False) + "\n")
        print(f"{syscode:<14}{len(by_sys[syscode][:a.limit_per_system]):>5}{got_zip:>7}{got_files:>10}")
        n_zip += got_zip
        n_file += got_files
    idx.close()
    print(f"\n下载成功 {n_zip} 篇文献的补充材料，共 {n_file} 个文件 → {SUPP}")
    print("⚠️ 附件是作者自整理数据：许可随原文；入库前必须核对 ODC/CC 许可与单位口径")


if __name__ == "__main__":
    main()
