#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""胶粘剂多体系 × 多通道数据发现采集器。

合法边界（务必遵守）
------------------------------------------------------------------
* 只用**官方开放接口**与**公网无需鉴权即可访问**的资源：
    OpenAlex REST API / Europe PMC REST API / Crossref REST API
* **不绕过**付费墙、不破解 Cloudflare/反爬、不使用任何账号共享或越权手段。
  被 MJ 限制（MDPI/PMC 网页 403、Figshare 403、Zenodo 超时）的源，
  本脚本不尝试绕过 —— 只如实记录"不可得"，交给人工在有授权的环境补取。
* 每条记录强制保留 provenance：doi / url / channel / query / license / oa 状态。
  没有来源的数据不许进库，这是本项目防自欺第一律在数据侧的延伸。

输出
------------------------------------------------------------------
  data_raw/harvest/<channel>.jsonl     分通道原始记录
  data_raw/harvest/unified.jsonl       去重后统一记录
  stdout                               体系 × 通道计数 + 可全文抽取候选数

用法
------------------------------------------------------------------
  python scripts/harvest_adhesive.py                    # 全体系 × 全通道
  python scripts/harvest_adhesive.py --systems epoxy pu # 只跑部分体系
  python scripts/harvest_adhesive.py --per-page 25 --max-pages 2
"""
import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OUT = Path("/workspace/data_raw/harvest") if Path("/workspace").exists() \
    else Path(__file__).resolve().parent.parent / "data_raw" / "harvest"
MAILTO = "formulaswarm@example.com"          # OpenAlex polite pool
UA = "FormulaSwarm-harvester/0.1 (mailto:%s)" % MAILTO

# ---------------------------------------------------------------- 体系矩阵
# code: (中文名, [检索式别名...])
SYSTEMS = {
    "epoxy":        ("环氧树脂结构胶", ["epoxy adhesive", "epoxy resin adhesive"]),
    "pu":           ("聚氨酯胶", ["polyurethane adhesive", "PU adhesive"]),
    "acrylic":      ("丙烯酸酯结构胶(SGA)", ["acrylic adhesive", "structural acrylic adhesive"]),
    "uv_acrylic":   ("UV固化丙烯酸酯", ["UV curable acrylic adhesive", "photocurable adhesive"]),
    "psa_acrylic":  ("丙烯酸酯压敏胶", ["acrylic pressure sensitive adhesive", "emulsion PSA"]),
    "psa_rubber":   ("橡胶型压敏胶(SIS/SBS/丁基)", ["rubber pressure sensitive adhesive",
                                              "SIS hot melt pressure sensitive adhesive"]),
    "silicone":     ("有机硅胶", ["silicone adhesive", "RTV silicone sealant adhesion"]),
    "cyanoacryl":   ("氰基丙烯酸酯(瞬干)", ["cyanoacrylate adhesive"]),
    "anaerobic":    ("厌氧胶", ["anaerobic adhesive"]),
    "hotmelt":      ("热熔胶(EVA/PO/PA/TPU)", ["hot melt adhesive", "EVA hot melt adhesive"]),
    "waterborne":   ("水性胶(PVAc/VAE/水性PU)", ["waterborne adhesive", "PVAc adhesive",
                                           "vinyl acetate emulsion adhesive"]),
    "silane_ms":    ("硅烷改性聚醚(MS/STP/SMP)", ["silane modified polymer adhesive",
                                            "MS polymer sealant adhesion"]),
    "phenolic":     ("酚醛/脲醛/木材胶", ["phenolic adhesive", "urea formaldehyde adhesive",
                                     "wood adhesive"]),
    "hightemp":     ("耐高温胶(PI/BMI)", ["high temperature adhesive", "polyimide adhesive"]),
    "inorganic":    ("无机/硅酸盐/水泥基", ["inorganic adhesive", "silicate adhesive",
                                     "cementitious adhesive"]),
}
# 每个体系关心的黏接读数量词（用于查询式构造）
PROPS = ["lap shear strength", "adhesive strength"]


def _get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(1.5 * (i + 1))
    return None


# ---------------------------------------------------------------- 通道 1: OpenAlex
def openalex(query, per_page, cursor="*"):
    q = urllib.parse.quote(query)
    sel = "id,doi,title,publication_year,primary_location,open_access,cited_by_count"
    url = (f"https://api.openalex.org/works?search={q}&per_page={per_page}"
           f"&cursor={urllib.parse.quote(cursor)}&select={sel}&mailto={MAILTO}")
    d = _get(url)
    if not d:
        return [], None
    out = []
    for w in d.get("results", []):
        loc = w.get("primary_location") or {}
        src = (loc.get("source") or {}).get("display_name")
        out.append({
            "doi": (w.get("doi") or "").replace("https://doi.org/", "") or None,
            "title": w.get("title"),
            "year": w.get("publication_year"),
            "venue": src,
            "url": loc.get("landing_page_url") or (w.get("id") if w.get("id") else None),
            "pdf_url": loc.get("pdf_url"),
            "is_oa": bool((w.get("open_access") or {}).get("is_oa")),
            "oa_url": (w.get("open_access") or {}).get("oa_url"),
            "cited_by": w.get("cited_by_count"),
        })
    return out, d.get("meta", {}).get("next_cursor")


# ---------------------------------------------------------------- 通道 2: Europe PMC
def europepmc(query, per_page, page=1):
    q = urllib.parse.quote(query)
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
           f"query={q}&format=json&pageSize={per_page}&page={page}&resultType=core&synonym=true")
    d = _get(url)
    if not d:
        return [], 0
    res = d.get("resultList", {}).get("result", [])
    hits = int(d.get("hitCount", 0))
    out = []
    for r in res:
        doi = r.get("doi")
        pmcid = r.get("pmcid")
        out.append({
            "doi": doi,
            "pmcid": pmcid,
            "title": r.get("title"),
            "year": (r.get("journalInfo") or {}).get("yearOfPublication"),
            "venue": (r.get("journalInfo") or {}).get("journal", {}).get("title"),
            "is_oa": r.get("isOpenAccess") == "Y" or bool(pmcid),
            "has_fulltext_xml": bool(pmcid),
            "fulltext_xml": (f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
                             if pmcid else None),
            "url": (f"https://europepmc.org/article/{r.get('source')}/{r.get('id')}"
                    if r.get("id") else None),
            "cited_by": r.get("citedByCount"),
        })
    return out, hits


# ---------------------------------------------------------------- 通道 3: Crossref
def crossref(query, per_page, cursor=None):
    q = urllib.parse.quote(query)
    sel = "DOI,title,issued,container-title,URL,abstract,license,type"
    url = f"https://api.crossref.org/works?query.bibliographic={q}&rows={per_page}&select={sel}"
    if cursor:
        url += f"&cursor={urllib.parse.quote(cursor)}"
    d = _get(url)
    if not d:
        return [], None
    items = d.get("message", {}).get("items", [])
    out = []
    for it in items:
        yr = None
        if it.get("issued", {}).get("date-parts"):
            yr = it["issued"]["date-parts"][0][0]
        lic = it.get("license") or []
        out.append({
            "doi": it.get("DOI"),
            "title": (it.get("title") or [None])[0],
            "year": yr,
            "venue": (it.get("container-title") or [None])[0],
            "url": it.get("URL"),
            "license": (lic[0].get("URL") if lic else None),
            "is_oa": bool(lic),
            "cited_by": None,
        })
    return out, d.get("message", {}).get("next-cursor")


CHANNELS = {"openalex": openalex, "europepmc": europepmc, "crossref": crossref}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--systems", nargs="*", default=sorted(SYSTEMS))
    ap.add_argument("--channels", nargs="*", default=sorted(CHANNELS))
    ap.add_argument("--per-page", type=int, default=25)
    ap.add_argument("--max-pages", type=int, default=1)
    ap.add_argument("--sleep", type=float, default=0.4)
    a = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    unified, per = {}, {}

    for syscode in a.systems:
        zh, aliases = SYSTEMS[syscode]
        queries = [f"{al} {pr}" for al in aliases for pr in PROPS]
        for ch in a.channels:
            fn = CHANNELS[ch]
            got = 0
            for q in queries:
                cursor, page = ("*", 1)
                for _ in range(a.max_pages):
                    if ch == "europepmc":
                        rows, hits = fn(q, a.per_page, page)
                        cursor = None
                    else:
                        rows, cursor = fn(q, a.per_page, cursor if ch != "openalex" else cursor)
                    for r in rows:
                        key = (r.get("doi") or r.get("title") or "").lower()
                        if not key:
                            continue
                        r.update({"channel": ch, "system": syscode, "system_zh": zh,
                                  "query": q, "harvested_at": stamp})
                        unified[key] = r
                        got += 1
                    if not cursor:
                        break
                    if ch == "europepmc":
                        page += 1
                    time.sleep(a.sleep)
                time.sleep(a.sleep)
            per[(syscode, ch)] = got
            print(f"  {syscode:<12}{ch:<11} {got:>5} 条")

    # 落盘
    bych = {}
    for r in unified.values():
        bych.setdefault(r["channel"], []).append(r)
    for ch, rows in bych.items():
        with (OUT / f"{ch}.jsonl").open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with (OUT / "unified.jsonl").open("w", encoding="utf-8") as f:
        for r in unified.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\n===== 汇总（去重后按体系）=====")
    print(f"{'体系':<14}{'中文名':<26}{'记录数':>7}{'可全文抽取':>11}{'OA':>6}")
    for syscode in a.systems:
        rows = [r for r in unified.values() if r["system"] == syscode]
        ft = sum(1 for r in rows if r.get("has_fulltext_xml") or r.get("pdf_url")
                 or r.get("oa_url"))
        oa = sum(1 for r in rows if r.get("is_oa"))
        print(f"{syscode:<14}{SYSTEMS[syscode][0]:<26}{len(rows):>7}{ft:>11}{oa:>6}")
    print(f"\n总计 {len(unified)} 条 → {OUT}/unified.jsonl")
    print("⚠️ 以上为公开可得的文献元数据；是否含可用配方/性能表需二次抽取验证")


if __name__ == "__main__":
    main()
