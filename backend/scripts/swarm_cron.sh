#!/usr/bin/env bash
# 周期性在 fs-arena 容器内运行 swarm.py，为 DB 持续产生新事件（前端 5s 轮询即持续滚动）
# 由 crontab 调用。round 从 event payload 自动递增，flock 防止上一轮未结束就叠加。
#
# 【竞马频率：每天 03:00 一次】（2026-10-13 起，从每分钟调为每天）
#   crontab -e 中的条目：
#     0 3 * * * /root/Material-Agent-Ecosystem/backend/scripts/swarm_cron.sh >> /var/log/swarm_cron.log 2>&1
#   历史教训：每分钟一轮曾在 20 小时里产生 64 万条 event / 2.9 万条 capsule 的垃圾竞马数据，
#   已于 2026-10-13 全量清理并改为每日一轮；PG 中的 evo.capsule / evo.evolution_event 即竞马数据，
#   如再次出现膨胀，先查 crontab 是否被改回高频。
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
CONTAINER=fs-arena
WORKDIR=/workspace/backend          # 新仓库结构调整：swarm.py 在 backend/ 下（自引导 sys.path）
LOCK=/tmp/swarm_cron.lock

# 防并发：上一轮还没跑完则跳过本轮
exec 9>"$LOCK"
flock -n 9 || { echo "$(date '+%F %T') skip: previous swarm still running"; exit 0; }

# 取当前最大 round（round 存于 event payload 的 JSON 中；-i 转发 stdin 给容器内 python）
MAX=$(docker exec -i -w "$WORKDIR" "$CONTAINER" python - <<'PY' 2>/dev/null || echo 0
import sys; sys.path.insert(0, "src")
from fsdb import connect
c = connect(); cur = c.cursor()
cur.execute("SELECT COALESCE(MAX((payload->>'round')::int),0) AS m FROM evo.evolution_event")
print(cur.fetchone()["m"]); c.close()
PY
)
NR=$((MAX + 1))
echo "$(date '+%F %T') === swarm round=$NR start ==="
docker exec -w "$WORKDIR" "$CONTAINER" python swarm.py --round "$NR" --inherit --budget 3 --seed "$RANDOM" 2>&1
RC=$?
echo "$(date '+%F %T') === swarm round=$NR end (exit $RC) ==="
