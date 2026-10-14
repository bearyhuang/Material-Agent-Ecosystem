"""方案A + 方案B + 方案C · 展示层 API（FastAPI，只读）

一个页面要什么就给什么：总览 / 排行榜 / 蜂群看板 / 进化曲线 / Capsule 谱系
+ 虚拟铸造厂（建议实验清单 / 数据卡 / 假说批判 / 红线自检）
+ 四级库跨项目发现（谱系 / 相似 / 归因 / 合规 / 问答溯源）。
数值全部来自 PG，本服务只做查询，不产生任何性能指标。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from fsdb import connect

app = FastAPI(title="FormulaAgents 演示 API", version="0.3.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

sys.path.insert(0, str(Path(__file__).resolve().parent))          # app/（同目录模块可导入）

from foundry_api import router as foundry_router  # noqa: E402
from journey_api import router as journey_router  # noqa: E402

app.include_router(foundry_router)
app.include_router(journey_router)

SCORES = {  # 排行榜展示用（只读聚合，非实验生成）
    "dev_mae": True, "holdout_mae": True, "coverage": True, "trials_total": True,
}


@app.get("/api/overview")
def overview():
    with connect() as c, c.cursor() as cur:
        cur.execute("select count(*) total, count(*) filter (where passed) passed from evo.capsule")
        row = cur.fetchone()
        caps, passed = row["total"], row["passed"] or 0
        cur.execute("select count(*) from evo.gene"); genes = cur.fetchone()["count"]
        cur.execute("select count(*) from evo.evolution_event"); events = cur.fetchone()["count"]
        cur.execute("select coalesce(sum(trials_total),0) from evo.capsule")
        trials = cur.fetchone()["coalesce"]
        cur.execute("select distinct round from evo.capsule order by round")
        rounds = [r["round"] for r in cur.fetchall()]
        cur.execute("""select g.family, c.config, c.dev_mae, c.holdout_mae, c.coverage,
                              c.round, c.dataset_hash, c.passed
                       from evo.capsule c join evo.gene g on g.id=c.gene_id
                       where c.passed order by c.holdout_mae asc limit 1""")
        champion = cur.fetchone()
    return {"capsules": caps, "capsules_passed": passed, "genes": genes, "events": events,
            "trials_total": trials, "rounds": rounds, "champion": champion}


@app.get("/api/runs")
def runs(limit: int = 20):
    with connect() as c, c.cursor() as cur:
        cur.execute("""
            select run_id, max(round) round, count(*) capsules,
                   count(*) filter (where passed) passed,
                   max(created_at)::text created_at
            from evo.capsule group by run_id order by max(created_at) desc limit %(limit)s
        """, {"limit": limit})
        return {"runs": cur.fetchall()}


@app.get("/api/leaderboard")
def leaderboard(dhash: str | None = None, round_no: int | None = None,
                passed_only: bool = False, limit: int = 50):
    cond, args = ["1=1"], {}
    if dhash:
        cond.append("c.dataset_hash = %(dhash)s"); args["dhash"] = dhash
    if round_no is not None:
        cond.append("c.round = %(round)s"); args["round"] = round_no
    if passed_only:
        cond.append("c.passed")
    sql = f"""
        select g.family, g.id gene_id, c.round, c.config, c.dataset_hash,
               c.dev_mae, c.holdout_mae, c.coverage, c.trials_total,
               c.passed, c.fail_reason, c.split, c.created_at::text
        from evo.capsule c join evo.gene g on g.id = c.gene_id
        where {' and '.join(cond)}
        order by c.holdout_mae asc nulls last, c.created_at desc
        limit %(limit)s
    """
    args["limit"] = limit
    with connect() as c, c.cursor() as cur:
        cur.execute(sql, args)
        return {"rows": cur.fetchall()}


@app.get("/api/genes")
def genes():
    with connect() as c, c.cursor() as cur:
        cur.execute("""
            select g.id, g.family, g.signals_match, g.strategy,
                   count(c.id) capsules,
                   count(c.id) filter (where c.passed) passed,
                   min(c.holdout_mae) best_holdout_mae,
                   round(avg(c.holdout_mae), 3) mean_holdout_mae
            from evo.gene g left join evo.capsule c on c.gene_id = g.id
            group by g.id, g.family, g.signals_match, g.strategy
            order by g.family
        """)
        return {"genes": cur.fetchall()}


@app.get("/api/events")
def events(run_id: str | None = None, limit: int = 400):
    cond, args = ["1=1"], {"limit": limit}
    if run_id:
        cond.append("run_id = %(run_id)s"); args["run_id"] = run_id
    with connect() as c, c.cursor() as cur:
        cur.execute(f"""
            select id, run_id, actor, kind, payload, created_at::text
            from evo.evolution_event where {' and '.join(cond)}
            order by id desc limit %(limit)s
        """, args)
        return {"events": cur.fetchall()}


@app.get("/api/curve")
def curve(dhash: str | None = None):
    where, args = "", {}
    if dhash:
        where = "where c.dataset_hash = %(dhash)s"; args["dhash"] = dhash
    with connect() as c, c.cursor() as cur:
        cur.execute(f"""
            select c.round,
                   min(c.holdout_mae) best_mae,
                   avg(c.holdout_mae) mean_mae,
                   max(c.trials_total) trials,
                   count(*) n,
                   (count(*) filter (where c.passed)::float8 / nullif(count(*),0)) pass_rate
            from evo.capsule c {where}
            group by c.round order by c.round
        """, args)
        per_round = cur.fetchall()
        cur.execute(f"""
            select c.round, g.family, min(c.holdout_mae) best_mae
            from evo.capsule c join evo.gene g on g.id = c.gene_id {where}
            group by c.round, g.family order by g.family, c.round
        """, args)
        return {"per_round": per_round, "per_family": cur.fetchall()}


@app.get("/api/health")
def health():
    return {"status": "ok", "db": "up"}


# ---------------- SECTION 9 评分维度证据 ----------------

@app.get("/api/contrast")
def contrast():
    """涌现性证据：蜂群（多 Agent 协作择优）vs 单 Agent（各算法族独立最优），
    同一批真实 Capsule 数据，不生成任何新数值。"""
    with connect() as c, c.cursor() as cur:
        # 公平对比：单 Agent 代表也只统计“通过了同一闸门”的记录（被 Arbiter 否决的不算成绩）
        cur.execute("""select g.family, min(c.holdout_mae) best, count(*) n,
                              sum(c.trials_total) trials
                       from evo.capsule c join evo.gene g on g.id=c.gene_id
                       where c.passed and c.holdout_mae is not null
                       group by g.family order by best""")
        per_gene = cur.fetchall()
        cur.execute("""select min(holdout_mae) from evo.capsule
                       where passed and holdout_mae is not null""")
        swarm_best = cur.fetchone()["min"]
        # 盲选（单Agent全量最优，可能被闸门否决）：对比“纸面最小数 vs 可信冠军”
        cur.execute("""select g.family, c.holdout_mae, c.fail_reason, c.round
                       from evo.capsule c join evo.gene g on g.id=c.gene_id
                       where c.holdout_mae is not null
                       order by c.holdout_mae limit 1""")
        blind = cur.fetchone()
        cur.execute("""select count(*) filter (where not passed) failed,
                              count(*) total from evo.capsule""")
        f = cur.fetchone()
        cur.execute("""select coalesce(sum(trials_total),0) from evo.capsule""")
        trials = cur.fetchone()["coalesce"]
        cur.execute("""select distinct on (round) round, g.family, c.holdout_mae
                       from evo.capsule c join evo.gene g on g.id=c.gene_id
                       where c.passed and c.holdout_mae is not null
                       order by round, c.holdout_mae""")
        champions = cur.fetchall()
    gene_best = per_gene[0]["best"] if per_gene else None
    return {
        "swarm": {"best_mae": swarm_best, "trials_total": trials,
                  "failed": f["failed"], "total": f["total"],
                  "gate_reject_rate": round(f["failed"] / f["total"], 3) if f["total"] else None},
        "per_gene": per_gene, "gene_best_mae": gene_best,
        "blind": {"family": blind["family"], "best_mae": blind["holdout_mae"],
                  "fail_reason": blind["fail_reason"], "round": blind["round"]},
        "emergent_gap": round(blind["holdout_mae"] - swarm_best, 3) if swarm_best else None,
        "champions_per_round": champions,
        "note": "单Agent盲选=全量最小数（可能被闸门否决）；蜂群=Integrator冻结的可信冠军；"
                "两者差值即‘谎报最优点’被否决的幅度",
    }


@app.get("/api/trust")
def trust():
    """容错与安全：信任边界与权限隔离实况（只读查询证明）。"""
    with connect() as c, c.cursor() as cur:
        cur.execute("select exists(select 1 from pg_roles where rolname='agent_ro') has_ro")
        has_ro = cur.fetchone()["has_ro"]
        r = {"has_ro": has_ro, "ro_reads_mat": False, "ro_writes_event": True, "ro_writes_capsule": True}
        if has_ro:
            cur.execute("""select has_table_privilege('agent_ro','mat.formulation','SELECT') ro_reads_mat,
                                  has_table_privilege('agent_ro','evo.evolution_event','INSERT') ro_writes_event,
                                  has_table_privilege('agent_ro','evo.capsule','INSERT') ro_writes_capsule""")
            r.update(cur.fetchone())
        cur.execute("select count(*) from evo.gene where avoid is not null")
        n_avoid = cur.fetchone()["count"]
        cur.execute("""select count(*) filter (where passed) pass,
                              count(*) filter (where not passed) fail from evo.capsule""")
        g = cur.fetchone()
    return {"agent_ro": r, "genes_with_avoid": n_avoid,
            "capsule": {"passed": g["pass"], "failed": g["fail"]},
            "note": "agent_ro 仅 mat.* 只读、evo.* 禁写 → 演示页/Agent 查询无权伪造 Capsule"}


@app.get("/api/failures")
def failures(limit: int = 10):
    """容错与安全：失败归因（可解释的失败，不是神秘崩溃）。"""
    with connect() as c, c.cursor() as cur:
        cur.execute("""select coalesce(fail_reason,'(unset)') reason, count(*) n
                       from evo.capsule where not passed group by 1 order by 2 desc""")
        dist = cur.fetchall()
        cur.execute("""select c.round, g.family, c.holdout_mae, c.fail_reason, c.trials_total,
                              c.created_at::text
                       from evo.capsule c join evo.gene g on g.id=c.gene_id
                       where not passed and c.fail_reason is not null
                       order by c.created_at desc limit %(limit)s""", {"limit": limit})
        latest = cur.fetchall()
    return {"distribution": dist, "latest": latest}


@app.get("/api/agents")
def agents():
    """协议与通信：Agent 发现/能力/角色（静态注册表）+ 蜂群内实际发言统计。"""
    registry = [
        {"role": "supervisor", "discover": "任务解析→按信号匹配Gene", "tools": "dataset/schema/steering",
         "protocol": "start/plan", "desc": "派活并登记 run_id"},
        {"role": "champion:*", "discover": "selector 按 signals_match 命中", "tools": "build/fit/predict",
         "protocol": "propose/argue", "desc": "并行提案，携带 config+论据"},
        {"role": "arbiter", "discover": "订阅 champion 提案", "tools": "pugh_matrix",
         "protocol": "verdict", "desc": "Pugh 矩阵裁决：为什么赢，而非谁R²高"},
        {"role": "red_team", "discover": "订阅胜者", "tools": "leak/ood/family_audit",
         "protocol": "challenge", "desc": "泄漏/适用域/配方族质疑"},
        {"role": "integrator", "discover": "订阅裁决结果", "tools": "freeze/evaluate",
         "protocol": "gate", "desc": "golden 冻结只评一次，写 Capsule"},
        {"role": "scribe", "discover": "订阅全事件", "tools": "append_event",
         "protocol": "audit", "desc": "追加式审计日志（不可篡改口径）"},
    ]
    with connect() as c, c.cursor() as cur:
        cur.execute("""select actor, count(*) n, max(created_at)::text last_at
                       from evo.evolution_event group by actor order by n desc""")
        speech = cur.fetchall()
        cur.execute("""select g.family, count(distinct g.id) genes, count(c.id) capsules,
                              count(c.id) filter (where c.passed) passed
                       from evo.gene g left join evo.capsule c on c.gene_id=g.id
                       group by g.family order by g.family""")
        families = cur.fetchall()
    return {"registry": registry, "speech": speech, "families": families,
            "protocol_version": "fs-protocol/v1 (event kinds: start/plan/propose/argue/verdict/challenge/gate/audit)"}


# ---------------------------------------------------------------- 静态前端
# 统一旅程地图 SPA。**必须放在全部 API 路由注册之后**：FastAPI 按注册顺序匹配，
# 若提前挂载 "/"，StaticFiles 会把 /api/* 一起吃掉（实测 /api/health 变 404）。
WEB = Path(__file__).resolve().parents[2] / "frontend"
if (WEB / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(WEB), html=True), name="web")
