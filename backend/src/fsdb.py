"""PG18 访问层：Capsule / Gene / Event 只经此处写入（Agent 不自报分数）。"""
import json
import math
import os
import platform
from datetime import date, datetime
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row


def jsonable(o):
    """JSON 兜底序列化：numeric→float，时间→ISO，其余→str（保证任何查询结果都能入审计）。"""
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


def sanitize(o):
    """把 NaN / ±Infinity 换成 None。

    Postgres 的 json/jsonb **不接受** NaN/Infinity 字面量；而 Python `json.dumps` 默认
    `allow_nan=True` 会原样吐出 `NaN`，导致写入时报
    `invalid input syntax for type json ... Token "NaN" is invalid`。

    NaN 的真实来源通常是 pandas 的空值（如某行 `family` 为 NULL → 集合里混入 float('nan')），
    在审计语义上它等价于"没有值"，故映射为 null 而不是 0 或空串（0 会被误读为真实数值）。
    """
    if isinstance(o, bool):
        return o
    if isinstance(o, dict):
        return {k: sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [sanitize(x) for x in o]
    if isinstance(o, (int, float)) and not isinstance(o, bool):
        try:
            if not math.isfinite(o):
                return None
        except (TypeError, OverflowError, ValueError):
            pass
    return o


def dumps(obj):
    return json.dumps(sanitize(obj), ensure_ascii=False, default=jsonable)

DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://formulaswarm:fs_pass_2026@localhost:5432/formulaswarm",
)


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


def env_fingerprint():
    import numpy
    import sklearn

    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "numpy": numpy.__version__,
        "sklearn": sklearn.__version__,
    }


# ---------------------------------------------------------------- 写入
GENE_UPSERT = """
insert into evo.gene(id, family, signals_match, strategy, avoid, validation)
values (%(id)s, %(family)s, %(signals_match)s::jsonb, %(strategy)s::jsonb, %(avoid)s, %(validation)s)
on conflict (id) do update
  set strategy = excluded.strategy, signals_match = excluded.signals_match, avoid = excluded.avoid
"""

CAPSULE_INSERT = """
insert into evo.capsule(gene_id, run_id, round, system, config, dataset_hash, split,
                        dev_mae, holdout_mae, coverage, trials_total, passed, fail_reason, env_fingerprint)
values (%(gene_id)s, %(run_id)s, %(round)s, %(system)s, %(config)s::jsonb, %(dataset_hash)s, %(split)s,
        %(dev_mae)s, %(holdout_mae)s, %(coverage)s, %(trials_total)s, %(passed)s, %(fail_reason)s,
        %(env_fingerprint)s::jsonb)
returning id
"""

EVENT_INSERT = """
insert into evo.evolution_event(run_id, actor, kind, payload)
values (%(run_id)s, %(actor)s, %(kind)s, %(payload)s::jsonb)
"""


def upsert_gene(cur, gene):
    cur.execute(
        GENE_UPSERT,
        {
            "id": gene["id"],
            "family": gene["family"],
            "signals_match": dumps(gene["signals_match"]),
            "strategy": dumps(gene["strategy"]),
            "avoid": list(gene.get("avoid", [])),
            "validation": gene.get("validation"),
        },
    )


def insert_capsule(cur, cap):
    cap = dict(cap)
    cap.setdefault("system", None)          # 体系标识；缺失即视为 unknown，不参与继承匹配
    cap["config"] = dumps(cap["config"])
    cap["env_fingerprint"] = dumps(cap["env_fingerprint"])
    cur.execute(CAPSULE_INSERT, cap)
    return cur.fetchone()["id"]


def append_event(cur, run_id, actor, kind, payload):
    cur.execute(
        EVENT_INSERT,
        {"run_id": run_id, "actor": actor, "kind": kind, "payload": dumps(payload)},
    )


# ---------------------------------------------------------------- 读取（继承用）
WARM_SQL = """
select c.gene_id, g.family, c.config, c.dev_mae, c.round, c.dataset_hash, c.system
from evo.capsule c join evo.gene g on g.id = c.gene_id
where c.passed and c.round < %(round)s
  and (%(dhash)s::text is null or c.dataset_hash = %(dhash)s)
  -- 禁止跨体系继承：只对同一体系（或双方都未标注体系）的历史 Capsule 热启动
  and (c.system is null or c.system = %(system)s)
order by c.dev_mae asc
limit %(limit)s
"""


def load_warm_capsules(cur, round_no, dataset_hash=None, limit=6, system=None):
    cur.execute(WARM_SQL, {"round": round_no, "dhash": dataset_hash,
                           "limit": limit, "system": system})
    return cur.fetchall()
