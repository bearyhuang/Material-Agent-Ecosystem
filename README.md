# Material Agent Ecosystem · BOT Chain 677

> 材料预测 **Agent 生态**的链上化实现：Agent 身份注册 + 算法竞马排名 + C 端预测调用记录，
> 全部落在 BOT Chain 主网（Chain ID **677**），可用区块浏览器逐笔验证。

本项目由 FormulaSwarm（自进化配方算法蜂群）工程化抽取而来：链下负责计算，链上只负责
**身份、排名、调用记录的不可否认留痕**。

---

## 1. 仓库结构

```
Material-Agent-Ecosystem/
├── blockchain/                     # 链上层（对应《Agent竞马结果上链》§十四）
│   ├── contracts/
│   │   ├── MaterialAgentRegistry.sol   # 合约1：Agent 链上身份证（对应 AgentRegistry.sol）
│   │   ├── AgentCompetition.sol        # 合约2：算法竞马/排名（对应 Competition.sol）
│   │   ├── PredictionRecord.sol        # 合约3：C 端预测调用记录（对应 Prediction.sol）
│   │   └── FormulaSwarmAttestor.sol    # 历史留痕：V1 批准留痕/预测登记锁（已部署主网）
│   ├── abi/                        # solc 0.8.28 编译产物（abi + bytecode）
│   ├── lib/rpc.mjs                 # JSON-RPC 客户端（含 DNS 投毒/SNI 注入的直连解法）
│   ├── compile.js                  # node blockchain/compile.js
│   ├── deployments.json            # 主网部署记录（地址/交易/区块/浏览器链接）
│   └── .env.example                # 复制为 .env 后填私钥；.env 已在 .gitignore
├── backend/                        # 链下计算层
│   ├── src/                        # 确定性内核（模型/评测/数据卡/链客户端）
│   ├── app/                        # FastAPI 展示层 + Streamlit 页面
│   ├── scripts/                    # 链上脚本（部署/推送/校验）+ 数据生成
│   ├── sql/                        # PostgreSQL schema 与合成种子
│   ├── tests/
│   ├── docker-compose.yml          # db + arena + evolver 一键起栈
│   └── requirements.txt
├── frontend/                       # 静态 SPA（断网可演示，3D 库全部本地 vendor）
│   ├── index.html  css/  js/  data/  vendor/
├── .evolver/                       # 自进化资产（genes / capsules / provenance，审计轨迹）
├── research/                       # 研究产物（AI4S 测算、对比矩阵、数据源库、图表）⚠️ 见 §6
├── data_raw/                       # 实验室原始采集物 ⚠️ 未脱敏，见 §6
├── docs/
│   ├── architecture.md             # 技术架构
│   ├── data-model.md               # 数据模型
│   ├── business-architecture.md    # 业务架构
│   ├── Agent竞马结果上链.md         # 链上化方案设计（本文档的实现依据）
│   
└── README.md
```

**有意未复制的内容**：`.venv/`（330M 本地虚拟环境，用 `requirements.txt` 重建）、
`.git/`（原仓库历史）、`.pytest_cache/`。另：原 `web/app.js`、`web/styles.css`
是未被 `index.html` 引用的遗留重复文件，未纳入。

---

## 2. 链上信息（BOT Chain 主网 · Chain ID 677）

| 项 | 值 |
| --- | --- |
| RPC | `https://rpc.botchain.ai` |
| 浏览器 | `https://scan.botchain.ai` |
| 原生代币 | BOT |

已部署合约与逐笔交易见 [`blockchain/deployments.json`](blockchain/deployments.json)，
每一条都带 `explorer` / `tx_explorer` 链接，可直接点开核对。

> **网络可达性说明（重要）**：`rpc.botchain.ai` 的 DNS 在部分网络被投毒（返回 Facebook 段 IP），
> 且带明文 SNI 的 TLS 握手会被 RST 注入。`blockchain/lib/rpc.mjs` 与 `backend/src/fschain.py`
> 内置直连解法（真实 IP + `fluxlayer.xyz` SNI + Host 头路由），配置见 `blockchain/.env.example`。

---

## 3. 快速开始

### 3.0 数据（四级库基础数据，可选，已随快照装载）

```bash
docker compose -f backend/docker-compose.yml up -d        # db + arena + evolver
# 全面扩充基础数据（供应商→原料→中间体→配方→性能←客户应用，幂等可重跑）
docker exec -w /workspace/backend fs-arena python scripts/gen_basedata.py --dry-run   # 先看不写
docker exec -w /workspace/backend fs-arena python scripts/gen_basedata.py             # 正式装载
# 竞马频率：每天 03:00 一轮（crontab：0 3 * * * backend/scripts/swarm_cron.sh）
bash backend/scripts/swarm_cron.sh                        # 也可手动跑一轮
```

### 3.1 合约（编译 / 部署）

```bash
cd blockchain
npm install
cp .env.example .env          # 填入 BOT_PRIVATE_KEY（绝不提交）
node compile.js               # 编译全部合约 → abi/
node mae.mjs deploy           # 部署三合约到主网 677（先 --dry-run 看计划）
node mae.mjs status           # 读链上状态：Agent 数 / 竞马数 / 预测数
node mae.mjs register --name champion_ridge --domain adhesive --version ridge-v3
node mae.mjs race             # 开今日竞马（同日重复调用自动复用）
node mae.mjs rank --race 1 --agent 1 --milli 412
node mae.mjs predict --agent 1 --class epoxy_adhesive --property lap_shear_strength --value 32.5 --ci-low 30.1 --ci-high 34.9
```

### 3.2 后端（FastAPI + PostgreSQL）

```bash
cd backend
pip install -r requirements.txt
docker compose up -d db                     # 或：docker compose up -d --build
export DATABASE_URL=postgresql://...
uvicorn app.api:app --host 0.0.0.0 --port 8000
# 页面：http://127.0.0.1:8000/   （frontend/ 由 app/api.py 挂载）
```

### 3.3 前端

`frontend/` 是纯静态 SPA，由后端同源挂载；也可任意静态服务器直接打开
（3D 库已 vendor 到本地，断网可演示）。

---

## 4. 链上化设计（为什么这样设计）

链下算、链上存证，且**链上不出现任何配方组分、实测值、供应商/客户/项目名**：

| 上链内容 | 形式 | 目的 |
| --- | --- | --- |
| Agent 身份 | `name / domain / modelHash / version / owner` | 算法组合的不可否认身份 |
| 竞马成绩 | `scoreMilli`（定点 ×1000，越小越好）+ `scoreHash` | "谁在什么材料任务上赢了" |
| 预测调用 | `valueMilli / ciLowMilli / ciHighMilli` + `payloadHash` | C 端操作的真实交易留痕 |
| 脱敏 | `redacted=true` 时合约强制所有数值为 0 | 红线由代码保证，不靠口头 |

`*Hash` 字段为 keccak256 的 canonical 描述，**链下重算必须与当时一致**：事后改本地记录，
链上哈希就对不上——这就是"预测先行登记锁"与"人审批准留痕"的技术本质。

---

## 5. 验收对照（`docs/review-requirements.md`）

| # | 复核要求 | 落地位置 |
| --- | --- | --- |
| 1 | 公开 GitHub 仓库、可审核 | 本仓库（`data_raw` 与私钥已剔除，见 `.gitignore`） |
| 2 | 钱包连接 + 至少一项核心操作上主网 677 | `frontend/js/wallet.js` + `PredictionRecord.submitPrediction` / `AgentCompetition.submitRanking` |
| 3 | 展示主网合约地址、交易记录、浏览器链接 | 页面「⛓ 链上」面板 + `deployments.json` |
| 4 | 审核人员可完成实际操作 | 选 Agent → 预测/提交排名 → 钱包签名 → 交易回执 + 浏览器核对 |

---

## 6. 红线（代码级，非口头）

- 链上无配方组分、无真实实测值、无供应商/客户/项目名、无采购订单；
- 批准人姓名只以 `keccak256(姓名)` 上链；
- 脱敏对象的数值字段合约侧强制归零（`redacted` 二次校验）；
- 数值唯一来源为链下确定性内核，LLM 不产出任何指标。

---

## 7. 敏感数据边界（公开前必读）

| 目录 | 内容 | `.gitignore` 默认 | 公开前处理 |
| --- | --- | --- | --- |
| `data_raw/` | 实验室原始采集物（1027 个文件，835M，含 `concrete/`、`harvest/`） | **已忽略** | 脱敏后才可公开；否则保持私有 |
| `research/` | 甲方名称（宁德时代/比亚迪/万华/国轩/中创新航）、授权清单、白名单、客户应用与项目信息 | **已忽略** | 同上 |
| `blockchain/.env` | RPC 端点与部署私钥 | **已忽略** | 永不提交 |

仓库若采用「**私有 + 审核员授权访问」方式提交（复核意见允许的替代方案），
可删除 `.gitignore` 中 `research/` 一行后再提交，使审核员能核对完整材料；
若采用公开仓库，则必须先对上述两个目录脱敏。

> 当前 `.gitignore` 的默认取向是**安全优先**：误操作 `git add .` 也不会把
> 私钥、原始采集物、甲方信息推出去。
