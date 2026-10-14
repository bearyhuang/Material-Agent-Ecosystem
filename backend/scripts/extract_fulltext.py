#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二阶段：把 Europe PMC 的**开放获取**全文 XML，逆向解析成结构化配方/性能表。

合法性
------------------------------------------------------------------
* 只取 Europe PMC 标记为开放获取、且能通过官方 REST 接口
  `/{PMCID}/fullTextXML` 直接取到的全文 —— 这是出版方自己放开的通道，
  不是绕过付费墙。
* `/{PMCID}/supplementaryFiles` 是同样公开的附件列表，优先于抓表格：
  附件里常直接带着作者整理好的 xlsx/csv，比解析 PDF 表格可靠一个量级。
* 每条产出强制带 doi + pmcid + 表格标签，可回溯、可人工复核。

输出
------------------------------------------------------------------
  data_raw/harvest/extracted_tables.jsonl   命中的表格（逐行结构化）
  data_raw/harvest/supplementary.jsonl      可用的公开附件清单

用法
------------------------------------------------------------------
  python scripts/extract_fulltext.py --limit-per-system 6
  python scripts/extract_fulltext.py --systems epoxy pu --limit-per-system 3
"""
import argparse
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

OUT = Path("/workspace/data_raw/harvest") if Path("/workspace").exists() \
    else Path(__file__).resolve().parent.parent / "data_raw" / "harvest"
UA = "FormulaSwarm-extractor/0.1 (mailto:formulaswarm@example.com)"
REST = "https://www.ebi.ac.uk/europepmc/webservices/rest"

# 命中"配方/性能表"的特征词
PROP_HINT = re.compile(
    r"(lap\s*shear|shear\s*strength|peel|tensile|strength|elongation|modulus|"
    r"toughness|viscosity|pot\s*life|open\s*time|cure\s*time|glass\s*transition|"
    r"\bTg\b|MPa|N/mm|N/25mm|wt%|wt\.%|phr|parts\s*by\s*weight|hardness|shore)", re.I)
NUM = re.compile(r"-?\d+(?:\.\d+)?")
# 真正属于"胶接"的测量项（区别于普通块体力学性能）
ADH_HINT = re.compile(
    r"(lap\s*shear|single\s*lap|shear\s*strength|adhesive\s*strength|adhesion\s*strength|"
    r"peel|t-peel|cleavage|拉伸剪切|剪切强度|剥离)", re.I)


def fetch(url, tries=3, binary=False):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
            return raw if binary else raw.decode("utf-8", "replace")
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(1.5 * (i + 1))
    return None


def cell_text(td):
    """JATS 单元格文本：把下标/上标/tex-math 全部拼出来。"""
    parts = [t for t in td.itertext()]
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def parse_tables(xml):
    """返回 [{label, caption, header, rows}] —— 对 JATS <table-wrap> 的结构逆向。"""
    root = ET.fromstring(xml)
    out = []
    for tw in root.iter("table-wrap"):
        label = cell_text(tw.find("label")) if tw.find("label") is not None else ""
        cap = tw.find("caption")
        caption = cell_text(cap) if cap is not None else ""
        tab = tw.find("table")
        if tab is None:
            tab = tw.find(".//table")
        if tab is None:
            continue
        def cells_of(tr):
            out = []
            for ch in tr:
                if ch.tag in ("td", "th"):
                    out.append(cell_text(ch))
            return [c for c in out if c != ""]

        header, rows = [], []
        for child in tab:
            if child.tag == "tr":
                rows.append(cells_of(child))
            elif child.tag in ("thead", "tbody", "tfoot"):
                for tr in child.findall("tr"):
                    c = cells_of(tr)
                    if not c:
                        continue
                    # 第一条完全没有数字的行视为表头（JATS 里常漏标 thead）
                    if not header and len(NUM.findall(" ".join(c))) == 0:
                        header = c
                    else:
                        rows.append(c)
        if rows:
            out.append({"label": label, "caption": caption, "header": header, "rows": rows})
    return out


def score_table(t):
    """给表格打分：同时含"性能/配方"词 + 足够多的数值，才算像数据表。"""
    txt = " ".join([t["caption"]] + t["header"] + [" ".join(r) for r in t["rows"]])
    hits = len(set(m.group(0).lower() for m in PROP_HINT.finditer(txt)))
    nums = len(NUM.findall(txt))
    return hits * 3 + min(nums, 60) / 3.0, hits, nums


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--unified", default=str(OUT / "unified.jsonl"))
    ap.add_argument("--systems", nargs="*", default=None)
    ap.add_argument("--limit-per-system", type=int, default=6)
    ap.add_argument("--min-score", type=float, default=12.0)
    ap.add_argument("--sleep", type=float, default=0.5)
    a = ap.parse_args()

    src = [json.loads(l) for l in Path(a.unified).read_text(encoding="utf-8").splitlines() if l.strip()]
    cand = [r for r in src if r.get("fulltext_xml")]
    if a.systems:
        cand = [r for r in cand if r["system"] in a.systems]

    by_sys = {}
    for r in cand:
        by_sys.setdefault(r["system"], []).append(r)

    tables_f = (OUT / "extracted_tables.jsonl").open("w", encoding="utf-8")
    suppl_f = (OUT / "supplementary.jsonl").open("w", encoding="utf-8")
    n_tab = n_sup = n_art = n_adh = 0
    print(f"{'体系':<14}{'文献':>5}{'命中表':>7}{'粘接相关':>9}{'数值行':>7}{'公开附件':>9}")
    for syscode in sorted(by_sys):
        rows = by_sys[syscode][:a.limit_per_system]
        hit_t = hit_rows = hit_s = hit_adh = 0
        for rec in rows:
            xml = fetch(rec["fulltext_xml"])
            time.sleep(a.sleep)
            if xml:
                n_art += 1
                for t in parse_tables(xml):
                    sc, hits, nums = score_table(t)
                    if sc < a.min_score:
                        continue
                    adh = bool(ADH_HINT.search(" ".join([t["caption"]] + t["header"]
                                                         + [" ".join(r) for r in t["rows"]])))
                    tables_f.write(json.dumps({
                        "doi": rec.get("doi"), "pmcid": rec.get("pmcid"),
                        "system": syscode, "system_zh": rec.get("system_zh"),
                        "title": rec.get("title"), "year": rec.get("year"),
                        "table_label": t["label"], "caption": t["caption"],
                        "header": t["header"], "rows": t["rows"],
                        "score": round(sc, 1), "adhesion_relevant": adh,
                        "source": rec["fulltext_xml"],
                    }, ensure_ascii=False) + "\n")
                    hit_t += 1
                    hit_adh += 1 if adh else 0
                    hit_rows += sum(len(r) for r in t["rows"])
            # 公开附件清单（作者整理好的 xlsx/csv，优先级高于表格解析）
            s = fetch(f"{REST}/{rec['pmcid']}/supplementaryFiles")
            time.sleep(a.sleep)
            if s and len(s) > 64:
                parts = [p for p in re.split(r"[\r\n]+", s) if p.strip()]
                if parts:
                    suppl_f.write(json.dumps({
                        "doi": rec.get("doi"), "pmcid": rec.get("pmcid"),
                        "system": syscode, "files": parts[:20],
                        "endpoint": f"{REST}/{rec['pmcid']}/supplementaryFiles",
                    }, ensure_ascii=False) + "\n")
                    hit_s += 1
        print(f"{syscode:<14}{len(rows):>5}{hit_t:>7}{hit_adh:>9}{hit_rows:>7}{hit_s:>9}")
        n_tab += hit_t
        n_sup += hit_s
        n_adh += hit_adh
    tables_f.close()
    suppl_f.close()
    print(f"\n抓取全文 {n_art} 篇 → 命中数据表 {n_tab} 张，含附件清单 {n_sup} 篇")
    print(f"→ {OUT/'extracted_tables.jsonl'}")
    print("⚠️ 每张表都带 doi/pmcid 来源；进入建模前必须做单位归一化与人工复核")


if __name__ == "__main__":
    main()
