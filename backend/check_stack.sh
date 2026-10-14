#!/usr/bin/env bash
# FormulaAgents · 方案A 环境自检（数据层 / 工具层 / 编排层 / 进化层 / 闸门退出码）
set -uo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
DC="docker compose -f docker-compose.yml"
fail=0
step(){ printf "\n\033[1m== %s ==\033[0m\n" "$1"; }
ok(){ printf "  \033[32m[PASS]\033[0m %s\n" "$1"; }
no(){ printf "  \033[31m[FAIL]\033[0m %s\n" "$1"; fail=1; }
q(){ docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$1"; }

step "1. 容器"
for s in db arena evolver; do
  st=$($DC ps --format '{{.Service}} {{.Status}}' | awk -v s=$s '$1==s{$1="";print}')
  [ -n "$st" ] && ok "$s: $st" || no "$s 未运行"
done

step "2. 数据层 PG18 + pgvector"
v=$(q "select version();"); echo "  ${v:0:60}"
q "select extversion from pg_extension where extname='vector';" | grep -q . \
  && ok "pgvector $(q "select extversion from pg_extension where extname='vector';")" || no "vector 扩展未安装"
q "select '[1,2,3]'::vector <=> '[1,2,4]'::vector;" >/dev/null 2>&1 && ok "vector 距离算子可用" || no "vector 距离算子不可用"
q "begin; create table _v(id int, e vector(3)); create index on _v using hnsw (e vector_l2_ops); insert into _v values (1,'[1,2,3]'); rollback;" >/dev/null 2>&1 \
  && ok "HNSW 向量索引可用" || no "HNSW 向量索引不可用"
for t in mat.raw_material mat.intermediate mat.formulation mat.performance evo.gene evo.capsule evo.evolution_event evo.prediction_register; do
  c=$(q "select count(*) from information_schema.tables where table_schema||'.'||table_name='$t';")
  [ "$c" = "1" ] && ok "表 $t" || no "缺少表 $t"
done
c=$(q "select count(*) from information_schema.views where table_schema='mat' and table_name='formulation_raw_pct';")
[ "$c" = "1" ] && ok "视图 mat.formulation_raw_pct（原料级展开）" || no "缺少展开视图"
c=$(q "select count(*) from pg_trigger where tgname='evolution_event_append_only';")
[ "$c" = "1" ] && ok "evo.evolution_event 追加式触发器" || no "缺少追加式触发器"

step "3. 工具层（Python 确定性计算）"
docker compose exec -T arena python - <<'PY' || fail=1
import importlib, importlib.metadata as md, sys
mods = ["numpy","pandas","sklearn","scipy","mapie","psycopg","pgvector","langgraph","langchain_core",
        "langgraph.checkpoint.postgres","fastapi","streamlit","matplotlib"]
bad = []
for m in mods:
    try: importlib.import_module(m)
    except Exception as e: bad.append(f"{m}({e.__class__.__name__})")
print("  versions:", ", ".join(f"{m}={md.version(m)}" for m in
      ("numpy","pandas","scikit-learn","mapie","langgraph","psycopg","pgvector")))
print("  \033[32m[PASS]\033[0m 全部依赖可导入" if not bad else "  \033[31m[FAIL]\033[0m " + ",".join(bad))
for m in ("xgboost", "torch", "botorch", "gpytorch"):
    try:
        __import__(m); print(f"  \033[32m[PASS]\033[0m 可选 {m}={md.version(m)}")
    except Exception: print(f"  \033[33m[WARN]\033[0m 可选 {m} 未安装（INSTALL_BO=0 构建）")
sys.exit(1 if bad else 0)
PY

step "4. 编排层 LangGraph 冒烟"
docker compose exec -T arena python - <<'PY' || fail=1
from langgraph.graph import StateGraph, START, END
from typing import TypedDict
class S(TypedDict): n: int
g = StateGraph(S)
g.add_node("a", lambda s: {"n": s["n"] + 1})
g.add_edge(START, "a"); g.add_edge("a", END)
print("  compiled:", g.compile().invoke({"n": 1}))
print("  \033[32m[PASS]\033[0m StateGraph 编译+调用成功")
PY

step "5. PG 连通 + 写入（Capsule 必须进 PG）"
docker compose exec -T arena python - <<'PY' || fail=1
import os, psycopg
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    cur = c.execute("select version()").fetchone()
    print("  ", cur[0][:50])
    c.execute("""insert into evo.gene(id,family,signals_match,strategy)
                 values('gene_smoke','gp','{"n_max":500}'::jsonb,'{}'::jsonb)""")
    c.execute("""insert into evo.capsule(gene_id,run_id,round,config,dataset_hash,split,dev_mae,holdout_mae,coverage,trials_total,passed)
                 values('gene_smoke','smoke',0,'{}'::jsonb,'smoke','time_forward',1.0,1.0,0.9,1,true)""")
    n = c.execute("select count(*) from evo.capsule").fetchone()[0]
    c.rollback()
    n0 = c.execute("select count(*) from evo.capsule").fetchone()[0]
print(f"  \033[32m[PASS]\033[0m capsule 写入可读（插入后 {n} 行，回滚后 {n0} 行）")
PY

step "6. 进化层 EvoMap Evolver"
docker compose exec -T evolver bash -c "node -v; evolver --version 2>/dev/null || evolver --help 2>&1 | head -5" | sed 's/^/  /'
docker compose exec -T evolver bash -c "evolver --help 2>&1 | head -3" | grep -qi . \
  && ok "evolver CLI 可用" || no "evolver CLI 不可用"

step "7. 工具层：PG 取数 + 5 族竞技场 + 评测门"
docker compose exec -T arena python arena.py --round 1 --budget 10 2>&1 | tail -9 | sed 's/^/  /'
rc=${PIPESTATUS[0]}
[ "$rc" = "0" ] && ok "arena.py --round 1 --budget 10 跑通" || no "arena.py round1 退出码 $rc"
docker compose exec -T arena python arena.py --round 2 --inherit --budget 3 2>&1 | tail -8 | sed 's/^/  /'
rc=${PIPESTATUS[0]}
[ "$rc" = "0" ] && ok "arena.py --round 2 --inherit --budget 3 跑通" || no "arena.py round2 退出码 $rc"

step "8. Capsule / Event 落 PG（不是 JSONL）"
q "select count(*) from evo.capsule;" | grep -qv '^0$' && ok "evo.capsule 有 $(q 'select count(*) from evo.capsule;') 行" \
  || no "evo.capsule 为空"
q "select count(*) from evo.gene;" | grep -qv '^0$' && ok "evo.gene 有 $(q 'select count(*) from evo.gene;') 行" || no "evo.gene 为空"
q "select count(*) from evo.evolution_event;" | grep -qv '^0$' \
  && ok "evo.evolution_event 有 $(q 'select count(*) from evo.evolution_event;') 行（追加式审计）" || no "事件表为空"
q "select count(*) from evo.capsule where not passed;" | grep -qv '^0$' \
  && ok "负例已入库（$(q 'select count(*) from evo.capsule where not passed;') 条 failed capsule）" \
  || echo "  [INFO] 本轮无负例"

step "9. node 包装退出码透传"
docker compose exec -T arena node run_eval.js --check >/dev/null 2>&1
rc=$?
[ "$rc" = "0" ] && ok "node run_eval.js --check 退出码 0（闸门通过）" || no "node run_eval.js --check 退出码 $rc"
docker compose exec -T arena python arena.py --round 2 --inherit --budget 3 --check >/dev/null 2>&1
rc=$?
echo "  （对比）第二轮 --check 退出码 $rc —— 0=通过 / 1=闸门拒绝，均属正确透传"

step "10. 编排层 LangGraph 蜂群（Supervisor→Champion×5→Arbiter→Red-team→Integrator→Scribe）"
docker compose exec -T arena python swarm.py --round 1 --budget 10 2>&1 | tail -22 | sed 's/^/  /'
rc=${PIPESTATUS[0]}
[ "$rc" = "0" ] && ok "swarm.py --round 1 跑通" || no "swarm.py 退出码 $rc"
docker compose exec -T arena python swarm.py --round 1 --budget 10 --check >/dev/null 2>&1
rc=$?
[ "$rc" = "0" ] && ok "swarm.py --check 退出码 0（闸门通过）" || no "swarm.py --check 退出码 $rc"
actors=$(q "select count(distinct actor) from evo.evolution_event where actor like 'champion:%';")
[ "$actors" -ge 5 ] && ok "蜂群 5 个 Champion 均已落事件流（actor 数=$actors）" || no "Champion actor 数=$actors"
crit=$(q "select count(*) from evo.evolution_event where actor='red_team' and kind='critique';")
[ "$crit" -ge 1 ] && ok "Red-team 质疑已入库（$crit 条）" || no "无 red_team critique 事件"

step "11. EvoMap 进化层（GEP 资产 + Selector + 进化曲线）"
docker compose exec -T arena python scripts/evo_sync.py --export --verify 2>&1 | tail -4 | sed 's/^/  /'
rc=${PIPESTATUS[0]}
[ "$rc" = "0" ] && ok "GEP 资产导出 + asset_id 自洽校验" || no "evo_sync 退出码 $rc"
n=$(q "select count(*) from information_schema.tables where table_name='capsule';")
docker compose exec -T arena python - <<'PY' || fail=1
import sys; sys.path.insert(0, "/workspace/src")
from fsgep import signals_for
from fsselector import select
sig = signals_for(69, "lap_shear_strength", "epoxy", True)
ranked, warm = select(sig, top=6, limit=6)
print(f"  Selector 信号：{sig[:3]} ...")
for g, s, st in ranked[:4]:
    print(f"    {g['id']:<20} score={s} hits={st['hits']} pass_rate={st['pass_rate']}")
print(f"  \033[32m[PASS]\033[0m 热启动配置 {len(warm)} 条："
      + ", ".join(f"{w['family']}" for w in warm))
sys.exit(0 if warm else 1)
PY
docker compose exec -T arena python - <<'PY' 2>/dev/null || true
import sys, json; sys.path.insert(0, "/workspace/src")
from fsdb import connect
with connect() as c, c.cursor() as cur:
    cur.execute("""select payload->>'source' from evo.evolution_event
                   where actor='selector' and kind='inherit' order by id desc limit 1""")
    print("  最近一次继承来源：", (cur.fetchone() or ["(无)"])[0])
PY
docker compose exec -T arena python arena.py --round 2 --inherit --budget 3 --system epoxy --selector gep \
  >/dev/null 2>&1 && ok "arena --selector gep 继承跑通" || no "arena --selector gep 失败"
docker compose exec -T arena python scripts/evo_curve.py 2>&1 | tail -8 | sed 's/^/  /'
[ -f /root/FormulaAgents/artifacts/evolution_curve.png ] \
  && ok "进化曲线已生成 artifacts/evolution_curve.png" || no "进化曲线未生成"

step "12. 展示层（FastAPI + Streamlit，保底录屏入口）"
code=$(curl -sS -o /dev/null -w "%{http_code}" ${API_URL:-http://localhost:8000}/api/health 2>/dev/null)
[ "$code" = "200" ] && ok "API ${API_URL:-http://localhost:8000}/api/health → 200" || no "API 不可达（$code）"
ui=$(curl -sS -o /dev/null -w "%{http_code}" http://localhost:8501/ 2>/dev/null)
[ "$ui" = "200" ] && ok "Streamlit 页面 → 200" || no "Streamlit 不可达（$ui）"
st_ok=$(curl -sS http://localhost:8501/_stcore/health 2>/dev/null)
[ "$st_ok" = "ok" ] && ok "Streamlit 内部健康检查 ok" || no "_stcore/health=$st_ok"
ov=$(curl -sS http://localhost:8000/api/overview 2>/dev/null)
echo "$ov" | grep -q '"capsules"' && ok "overview 返回 Capsule 数（$(echo "$ov" | python3 -c 'import json,sys;print(json.load(sys.stdin)["capsules"])')）" \
  || no "overview 无数据"
# SECTION 9 评分数值门：四个证据端点必须返回数据（用 jq 解析避免引号转义）
for ep in contrast trust failures agents; do
  r=$(curl -sS "http://localhost:8000/api/$ep" 2>/dev/null)
  case "$ep" in
    contrast) expr=$(echo "$r" | jq -r '"蜂群" + (.swarm.best_mae|tostring) + " vs 盲选" + (.blind.best_mae|tostring) + "(" + .blind.fail_reason + ")"' 2>/dev/null);;
    trust)    expr=$(echo "$r" | jq -r '"agent_ro=" + (.agent_ro.has_ro|tostring) + " mat_ro=" + (.agent_ro.ro_reads_mat|tostring) + " evo禁写=" + (((.agent_ro.ro_writes_event or .agent_ro.ro_writes_capsule)|not)|tostring)' 2>/dev/null);;
    failures) expr=$(echo "$r" | jq -r '"归因" + (.distribution|length|tostring) + "类 最近失败" + (.latest|length|tostring) + "条"' 2>/dev/null);;
    agents)   expr=$(echo "$r" | jq -r '"角色" + (.registry|length|tostring) + "个 发言族" + (.speech|length|tostring) + "类"' 2>/dev/null);;
  esac
  [ -n "$expr" ] && ok "演示端点 /api/$ep → $expr" || no "/api/$ep 无数据"
done
[ -f /root/FormulaAgents/doc/演示录屏脚本.md ] && ok "保底录屏脚本 doc/演示录屏脚本.md 就绪" \
  || no "缺少录屏脚本"

step "结果"
if [ "$fail" = "0" ]; then printf "  \033[32m全部检查通过\033[0m\n"; else printf "  \033[31m存在失败项\033[0m\n"; fi
exit $fail
