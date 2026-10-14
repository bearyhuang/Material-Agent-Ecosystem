#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""胶粘剂数据源 · 统一 API 客户端（预留各源 token 接入）。

设计要点
------------------------------------------------------------------
* 每个数据源在 SOURCES 里登记：base URL、auth 类型、env 凭证名、本机可达性、是否免费、相关性。
* auth_request() 按 auth 类型自动注入：mailto / Bearer / X-API-KEY / token / Basic / OAuth2。
* 所有密钥只从环境变量读取，绝不落盘明文；status_report() 只报"是否已配"，不打印值。
* EPO OPS 走 OAuth2 client_credentials，epo_ops_token() 负责换 Bearer 并缓存。
* 本模块零业务依赖（不 import fsdb），可独立 `python -m src.fsio.sources` 自检。

合法边界（重申）：只接官方接口与公开/免费注册通道；不绕过付费墙、不破解反爬、
不共享账号。详见 doc/14。
"""
import base64
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_FILES = [ROOT / "infra" / ".env", ROOT / "config" / "sources.env"]
UA = "FormulaSwarm-sourceclient/0.1"

for _f in ENV_FILES:
    if _f.exists():
        for _line in _f.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

SOURCES = {
    "openalex":        {"kind": "文献元数据", "base": "https://api.openalex.org",
                        "auth": "mailto", "env": {"mailto": "OPENALEX_MAILTO"},
                        "reachable": True, "free": True,
                        "relevance": "全15体系论文+OA全文链接"},
    "crossref":        {"kind": "文献元数据", "base": "https://api.crossref.org",
                        "auth": "none", "env": {}, "reachable": True, "free": True,
                        "relevance": "宽覆盖，含中文期刊DOI"},
    "europepmc":       {"kind": "文献+OA全文", "base": "https://www.ebi.ac.uk/europepmc/webservices/rest",
                        "auth": "none", "env": {}, "reachable": True, "free": True,
                        "relevance": "OA全文XML+附件ZIP（已抽127表）"},
    "unpaywall":       {"kind": "OA链接", "base": "https://api.unpaywall.org/v2",
                        "auth": "mailto", "env": {"mailto": "UNPAYWALL_EMAIL"},
                        "reachable": False, "free": True,
                        "relevance": "付费论文→合法OA副本"},
    "semanticscholar": {"kind": "文献图谱", "base": "https://api.semanticscholar.org/graph/v1",
                        "auth": "x-api-key", "env": {"key": "SEMANTIC_SCHOLAR_API_KEY"},
                        "reachable": True, "free": True,
                        "relevance": "引用图+摘要，找高引/综述"},
    "pubchem":         {"kind": "化学单体", "base": "https://pubchem.ncbi.nlm.nih.gov/rest/pug",
                        "auth": "none", "env": {}, "reachable": True, "free": True,
                        "relevance": "单体CAS→物性（EEW/AHEW反查）"},
    "materialsproject":{"kind": "材料库", "base": "https://api.materialsproject.org",
                        "auth": "header:X-API-KEY", "env": {"key": "MATERIALS_PROJECT_API_KEY"},
                        "reachable": False, "free": True,
                        "relevance": "单体/聚合物热力学，支撑机理特征"},
    "figshare":        {"kind": "数据集", "base": "https://api.figshare.com/v2",
                        "auth": "token", "env": {"token": "FIGSHARE_TOKEN"},
                        "reachable": False, "free": True,
                        "relevance": "研究者上传原始数据表"},
    "zenodo":          {"kind": "数据集", "base": "https://zenodo.org/api",
                        "auth": "optional", "env": {"token": "ZENODO_TOKEN"},
                        "reachable": False, "free": True,
                        "relevance": "欧洲开放数据，含SI表"},
    "kaggle":          {"kind": "数据集", "base": "https://www.kaggle.com/api/v1",
                        "auth": "basic", "env": {"user": "KAGGLE_USERNAME", "key": "KAGGLE_KEY"},
                        "reachable": True, "free": True,
                        "relevance": "社区数据集（胶粘剂稀疏）"},
    "lens":            {"kind": "专利+文献", "base": "https://api.lens.org",
                        "auth": "bearer", "env": {"token": "LENS_API_TOKEN"},
                        "reachable": True, "free": "academic",
                        "relevance": "1亿+专利+2亿文献，实施例含配方"},
    "patentsview_odp": {"kind": "专利", "base": "https://data.uspto.gov/api/v1",
                        "auth": "bearer", "env": {"token": "USPTO_ODP_TOKEN"},
                        "reachable": False, "free": True,
                        "relevance": "美国专利实施例（组分phr→强度）"},
    "epo_ops":         {"kind": "专利", "base": "https://ops.epo.org/3.2",
                        "auth": "oauth2", "env": {"key": "EPO_OPS_KEY", "secret": "EPO_OPS_SECRET"},
                        "reachable": True, "free": True,
                        "relevance": "欧专实施例+同族，跨语言"},
    "huggingface":     {"kind": "数据集", "base": "https://huggingface.co/api",
                        "auth": "optional", "env": {"token": "HF_TOKEN"},
                        "reachable": False, "free": True,
                        "relevance": "社区数据集（胶粘剂稀疏）"},
}

_EPO_TOKEN_CACHE = {"tok": None, "exp": 0}


def get_token(source):
    """返回该源已配置的凭证字典（值已脱敏前先不打印）。"""
    out = {}
    for role, envname in SOURCES[source]["env"].items():
        out[role] = os.environ.get(envname) or ""
    return out


def _epo_ops_token():
    tok = _EPO_TOKEN_CACHE["tok"]
    if tok and time.time() < _EPO_TOKEN_CACHE["exp"] - 30:
        return tok
    sec = get_token("epo_ops")
    key, secret = sec.get("key"), sec.get("secret")
    if not (key and secret):
        return None
    req = urllib.request.Request(
        "https://ops.epo.org/3.2/auth/accesstoken",
        data=b"grant_type=client_credentials",
        headers={"Authorization": "Basic " + base64.b64encode(
            f"{key}:{secret}".encode()).decode(),
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json_loads(r.read())
        _EPO_TOKEN_CACHE["tok"] = d["access_token"]
        _EPO_TOKEN_CACHE["exp"] = time.time() + int(d.get("expires_in", 3600))
        return d["access_token"]
    except Exception:
        return None


def json_loads(b):
    import json
    return json.loads(b.decode("utf-8", "replace"))


def auth_request(source, url, params=None, binary=False, retries=2, timeout=30):
    """对 source 发起带鉴权的请求，返回 (status_code, data|bytes)。"""
    meta = SOURCES[source]
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    sec = get_token(source)
    auth = meta["auth"]

    if auth == "bearer":
        if sec.get("token"):
            req.add_header("Authorization", "Bearer " + sec["token"])
    elif auth == "x-api-key":
        if sec.get("key"):
            req.add_header("x-api-key", sec["key"])
    elif auth == "header:X-API-KEY":
        if sec.get("key"):
            req.add_header("X-API-KEY", sec["key"])
    elif auth == "token":
        if sec.get("token"):
            req.add_header("Authorization", "token " + sec["token"])
    elif auth == "basic":
        if sec.get("user") and sec.get("key"):
            req.add_header("Authorization", "Basic " + base64.b64encode(
                f"{sec['user']}:{sec['key']}".encode()).decode())
    elif auth == "oauth2":
        t = _epo_ops_token()
        if t:
            req.add_header("Authorization", "Bearer " + t)
    elif auth == "mailto":
        mail = sec.get("mailto")
        if mail and "mailto=" not in url:
            url = url + ("&" if "?" in url else "?") + "mailto=" + urllib.parse.quote(mail)
            req.full_url = url

    for _ in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            return r.status, (raw if binary else json_loads(raw))
        except Exception as e:
            last = e
            time.sleep(1.2)
    return getattr(last, "code", 0) if hasattr(last, "code") else 0, None


def status_report():
    print(f"{'源':<16}{'类型':<12}{'鉴权':<14}{'本机可达':>9}{'已配token':>11}")
    print("-" * 64)
    for name, m in SOURCES.items():
        sec = get_token(name)
        configured = "是" if any(v for v in sec.values()) else "否"
        reach = "✅" if m["reachable"] else ("⚠️需token" if any(sec.values()) else "❌")
        print(f"{name:<16}{m['kind']:<12}{m['auth']:<14}{reach:>9}{configured:>11}")
    print("\n注：'❌' = 当前无 token 且本机不可达；'⚠️需token' = 配好 token 后可用。")


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv or len(sys.argv) == 1:
        status_report()
    elif sys.argv[1] == "--probe":
        # 实测 tokenless 通道是否仍活着
        for s in ["openalex", "crossref", "europepmc", "pubchem"]:
            code, _ = auth_request(s, SOURCES[s]["base"].rstrip("/") +
                                   ("/works?per_page=1" if s != "europepmc" else
                                    "/search?query=adhesive&format=json&pageSize=1"), retries=0)
            print(f"{s:<16} HTTP {code}")
