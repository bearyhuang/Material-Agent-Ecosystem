#!/usr/bin/env bash
# FormulaAgents · 方案 B（虚拟配方铸造厂）+ 方案 C（四级库跨项目发现）验收自检
# 用法：cd infra && ./check_foundry.sh           （含端到端闭环，约 1-2 分钟）
#       ./check_foundry.sh --fast                （跳过端到端与 pytest 慢测）
set -uo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
DC="docker compose -f docker-compose.yml"
FAST=0; [ "${1:-}" = "--fast" ] && FAST=1
fail=0
step(){ printf "\n\033[1m== %s ==\033[0m\n" "$1"; }
ok(){ printf "  \033[32m[PASS]\033[0m %s\n" "$1"; }
no(){ printf "  \033[31m[FAIL]\033[0m %s\n" "$1"; fail=1; }
q(){ docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$1"; }

step "1. 库表（方案 B / C 新增）"
for t in mat.formulation_candidate mat.formulation_lot mat.purchase_order mat.purchase_order_item \
         mat.compliance_rule mat.raw_material_compliance mat.scaleup_correction \
         evo.experiment_plan evo.experiment_result evo.datacard evo.hypothesis \
         evo.hypothesis_critique evo.formula_strategy evo.answer_audit; do
  c=$(q "select count(*) from information_schema.tables where table_schema||'.'||table_name='$t';")
  [ "$c" = "1" ] && ok "表 $t" || no "缺少表 $t"
done
for v in mat.v_material_genealogy mat.v_lot_trace mat.v_po_trace mat.v_formulation_compliance \
         mat.v_mass_balance_check mat.v_formulation_raw_pct_norm; do
  c=$(q "select count(*) from information_schema.views where table_schema='mat' and table_name='${v#mat.}';")
  [ "$c" = "1" ] && ok "视图 $v" || no "缺少视图 $v"
done

step "2. 触发器（硬约束写进库，不是写在文档里）"
for tg in candidate_compute_ins plan_guard_ins result_guard_ins; do
  c=$(q "select count(*) from pg_trigger where tgname='$tg';")
  [ "$c" = "1" ] && ok "触发器 $tg" || no "缺少触发器 $tg"
done

step "3. 硬约束负测试（越界必须失败，而不是警告后照做）"
docker compose exec -T arena python - <<'PY' && ok "质量守恒：Σ≠100 的候选被拒" || no "质量守恒未生效"
import sys; sys.path.insert(0,'/workspace/src')
from fsdb import connect
try:
    with connect() as c, c.cursor() as cur:
        cur.execute("""insert into mat.formulation_candidate(code,system,origin,items)
                       values ('NEG-TEST-1','epoxy','bo','[{"raw_material_code":"E51","pct":50}]'::jsonb)""")
    print("  [FAIL] 竟写入成功"); sys.exit(1)
except Exception as e:
    print("  拒绝原因：", str(e).split('\n')[0][:90]); sys.exit(0)
PY
docker compose exec -T arena python - <<'PY' && ok "L4：未批准的计划写不进实验结果" || no "L4 闸门未生效"
import sys; sys.path.insert(0,'/workspace/src')
from fsdb import connect
try:
    with connect() as c, c.cursor() as cur:
        cur.execute("""insert into mat.formulation_candidate(code,system,origin,items)
                       values ('NEG-TEST-2','epoxy','bo','[{"raw_material_code":"E51","pct":100}]'::jsonb)
                       returning id""")
        cid = cur.fetchone()["id"]
        cur.execute("""insert into evo.experiment_plan(run_id,round,candidate_id,target,status)
                       values ('neg',99,%s,'lap_shear_strength','proposed') returning id""", (cid,))
        pid = cur.fetchone()["id"]
        cur.execute("""insert into evo.experiment_result(plan_id,simulated,source,target,value)
                       values (%s,true,'virtual_lab','lap_shear_strength',1.0)""", (pid,))
    print("  [FAIL] 竟写入成功"); sys.exit(1)
except Exception as e:
    print("  拒绝原因：", str(e).split('\n')[0][:90]); sys.exit(0)
PY
docker compose exec -T arena python - <<'PY' && ok "审计：无证据的问答写不进 answer_audit" || no "证据闸门未生效"
import sys; sys.path.insert(0,'/workspace/src')
from fsdb import connect
try:
    with connect() as c, c.cursor() as cur:
        cur.execute("""insert into evo.answer_audit(trace_id,question,intent,answer,evidence,engine)
                       values ('neg','x','genealogy','x','[]'::jsonb,'rule')""")
    print("  [FAIL] 竟写入成功"); sys.exit(1)
except Exception as e:
    print("  拒绝原因：", str(e).split('\n')[0][:90]); sys.exit(0)
PY

step "4. 方案 C 验收（四级库 / 谱系 / 溯源）"
v=$(q "select coalesce(sum(violations),0) from mat.v_mass_balance_check;")
[ "$v" = "0" ] && ok "原料级展开质量守恒 0 违规" || no "质量守恒违规 $v 处"
for pair in "mat.v_material_genealogy:E51" "mat.v_lot_trace:L2489-01" "mat.v_po_trace:PO"; do
  view=${pair%%:*}; key=${pair##*:}
  n=$(q "select count(*) from $view;")
  [ "${n:-0}" -gt 0 ] && ok "$view 有 $n 行（谱系可查）" || no "$view 为空"
done
n=$(q "select count(*) from mat.formulation_similarity(1, 3);")
[ "${n:-0}" -gt 0 ] && ok "相似配方检索可用（$n 条）" || no "相似配方检索不可用"
n=$(q "select count(*) from evo.answer_audit;")
[ "${n:-0}" -gt 0 ] && ok "问答审计 $n 条（每条带证据链）" || no "无问答审计"

step "5. 方案 C 命令行自检（discover.py --check）"
docker compose exec -T arena python discover.py --check 2>&1 | sed 's/^/  /'
[ "${PIPESTATUS[0]}" = "0" ] && ok "discover.py --check 退出码 0" || no "discover.py --check 失败"

if [ "$FAST" = "0" ]; then
  step "6. 方案 B 端到端闭环（2 轮）"
  docker compose exec -T arena python foundry.py --rounds 2 --budget 5 --pool 200 \
      --approver "验收自检" 2>&1 | tail -14 | sed 's/^/  /'
  rc=${PIPESTATUS[0]}
  [ "$rc" = "0" ] && ok "foundry.py 两轮闭环退出码 0" || no "foundry.py 退出码 $rc"
  docker compose exec -T arena python foundry.py --rounds 1 --budget 3 --pool 100 --check >/dev/null 2>&1
  [ "$?" = "0" ] && ok "foundry.py --check 退出码 0（有可用数据卡）" || no "foundry.py --check 失败"

  step "7. 防自欺核查（查询证明，不是口头承诺）"
  n=$(q "select count(*) from evo.experiment_result r join evo.experiment_plan p on p.id=r.plan_id where p.approved_by is null;")
  [ "${n:-0}" = "0" ] && ok "无未批准即执行的实验" || no "存在 $n 条未批准的执行结果"
  n=$(q "select count(*) from mat.performance where method_std='VIRTUAL_LAB';")
  [ "${n:-0}" = "0" ] && ok "模拟值未泄漏进 mat.performance" || no "实测表被模拟值污染 $n 条"
  n=$(q "select count(*) from evo.datacard where simulated and (disclaimer is null or length(disclaimer)<20);")
  [ "${n:-0}" = "0" ] && ok "数据卡均带免责声明" || no "$n 张数据卡缺免责声明"
  n=$(q "select count(*) from mat.formulation_candidate where pct_total<99.5 or pct_total>100.5;")
  [ "${n:-0}" = "0" ] && ok "配方候选全部满足质量守恒" || no "$n 个候选质量不守恒"

  step "8. 回归测试（pytest）"
  # 先确认 pytest 在镜像里：镜像过期时会缺 pytest，此时应报"环境问题"而非"测试失败"
  if ! docker compose exec -T arena python -c "import pytest" >/dev/null 2>&1; then
    no "pytest 未安装 —— 镜像为旧版本构建，需 docker compose build arena 重建（requirements.txt 已声明 pytest）"
  else
    docker compose exec -T arena python -m pytest tests -q 2>&1 | tail -5 | sed 's/^/  /'
    [ "${PIPESTATUS[0]}" = "0" ] && ok "pytest 全绿" || no "pytest 有失败"
  fi
else
  step "6-8. 已跳过（--fast）"
fi

step "9. 展示层 API 端点"
for ep in foundry/overview foundry/plans foundry/datacards foundry/hypotheses foundry/curve \
          foundry/guard foundry/results foundry/alignment discover/overview lineage/mass_balance \
          compliance/rules compliance/materials audit; do
  code=$(curl -sS -o /dev/null -w "%{http_code}" "http://localhost:8000/api/$ep?limit=5&q=E51&po=PO-20260923-001&lot=L2489-01&status=approved" 2>/dev/null)
  [ "$code" = "200" ] && ok "/api/$ep → 200" || no "/api/$ep → $code"
done
ui=$(curl -sS -o /dev/null -w "%{http_code}" http://localhost:8501/ 2>/dev/null)
[ "$ui" = "200" ] && ok "Streamlit 主页面 → 200" || no "Streamlit 不可达（$ui）"

step "结果"
if [ "$fail" = "0" ]; then printf "  \033[32m方案 B + 方案 C 全部检查通过\033[0m\n"; else printf "  \033[31m存在失败项\033[0m\n"; fi
exit $fail
