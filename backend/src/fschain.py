"""链层客户端（BOT Chain）：批准留痕 / 预测登记锁 / ERC-8004 Agent 身份。

只做三件事，对应 ai4s04 §4 必做档：
  ① attestApproval      —— evo.experiment_plan 人审批准留痕
  ② registerPrediction  —— evo.prediction_register 预测先行登记锁
  ③ register_agents     —— Agent 链上身份（ERC-8004，走 BOT Chain Agent OS API，不写合约）

红线（代码级，不是口头）：
  - 批准人姓名只以 keccak256(姓名) 上链；
  - redacted=True 时所有数值字段强制置 0，且合约侧二次校验；
  - 配方组分 / 真实实测值 / 供应商·客户·项目 / 采购订单 没有任何上链路径。

环境变量（chain/.env 或进程环境）：
    BOT_RPC_URL        测试网/主网 RPC
    BOT_PRIVATE_KEY    部署与写链私钥（绝不入库、绝不进事件）
    BOT_CONTRACT       已部署的合约地址
    BOT_CHAIN_ID       chain id（主网 677）
    BOT_WALLET         钱包地址（默认 0x4Bd9a6ab15329F592f885f7d3891406178c4070f）
    BOT_AGENTOS_URL / BOT_AGENTOS_TOKEN   ERC-8004 注册接口
    FS_CHAIN           0 = 关闭链层（全链路照跑，只落队列）
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from web3 import HTTPProvider

ROOT = Path(__file__).resolve().parents[1]          # backend/
CHAIN_DIR = ROOT.parent / "blockchain"              # 仓库根的 blockchain/
ABI_DIR = CHAIN_DIR / "abi"
DEPLOY_FILE = CHAIN_DIR / "deployments.json"
QUEUE_FILE = ROOT / "artifacts" / "chain" / "anchor_queue.jsonl"

DEFAULT_WALLET = "0x4Bd9a6ab15329F592f885f7d3891406178c4070f"
DEFAULT_CHAIN_ID = 677
# 合约侧数组上限是 200，但实测 200 条/笔 ≈ 1972 万 gas（单笔吃掉的区块配额过高）。
# 取 100：单笔约 990 万 gas，留足余量；条数多时自动多拆几笔。
BATCH_MAX = 100


def _chunks(seq: Sequence[Any], n: int) -> List[Sequence[Any]]:
    return [seq[i:i + n] for i in range(0, len(seq), n)] or []


# ---------------------------------------------------------------- 环境
def load_dotenv(path: Optional[Path] = None) -> None:
    """极简 .env 加载（避免额外依赖）。已存在的环境变量优先。"""
    p = path or (CHAIN_DIR / ".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def cfg() -> Dict[str, Any]:
    load_dotenv()
    return {
        "rpc": os.environ.get("BOT_RPC_URL", "").strip(),
        # 直连真实 IP（跳过被投毒的 DNS 与 SNI 检测）；留空则用 BOT_RPC_URL 常规连接
        "rpc_ip": os.environ.get("BOT_RPC_IP", "").strip(),
        "rpc_host": os.environ.get("BOT_RPC_HOST", "rpc.botchain.ai").strip(),
        "pk": os.environ.get("BOT_PRIVATE_KEY", "").strip(),
        "contract": os.environ.get("BOT_CONTRACT", "").strip(),
        "chain_id": int(os.environ.get("BOT_CHAIN_ID", DEFAULT_CHAIN_ID)),
        "wallet": os.environ.get("BOT_WALLET", DEFAULT_WALLET).strip(),
        "enabled": os.environ.get("FS_CHAIN", "0").strip() not in ("0", "", "false", "False"),
        "explorer": os.environ.get("BOT_EXPLORER", "https://scan.botchain.ai").rstrip("/"),
    }


def has_endpoint() -> bool:
    """是否有可用 RPC 端点：常规 URL 或直连 IP 二选一。"""
    c = cfg()
    return bool(c["rpc"] or c["rpc_ip"])


def enabled() -> bool:
    """链是否可用：开关打开 + 有端点 + 私钥 + 合约地址。"""
    c = cfg()
    return c["enabled"] and has_endpoint() and bool(c["pk"]) and bool(c["contract"])


# ---------------------------------------------------------------- 哈希（canonical）
def _keccak(text: str) -> bytes:
    from eth_utils import keccak  # 延迟导入：离线哈希场景不需要 web3 全量

    return keccak(text=text)


def _iso(ts: Any) -> str:
    """UTC ISO8601 秒级确定性格式 —— 链下重算必须与当时一致。"""
    if ts is None:
        return ""
    if isinstance(ts, str):
        return ts
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(ts)


def _num(v: Any, redacted: bool) -> str:
    if redacted:
        return "redacted"
    if v is None:
        return ""
    return f"{float(v):.6f}"


def plan_hash(row: Dict[str, Any]) -> bytes:
    """evo.experiment_plan 一行的 canonical 哈希。

    不含批准人姓名（另有 approver_hash）、不含任何商业敏感字段；
    但覆盖"批准了哪一组实验"的全部决策要素，改任何一个都失配。
    """
    parts = [
        "FSWP-PLAN-1",
        str(row.get("id")),
        str(row.get("run_id") or ""),
        str(row.get("round") if row.get("round") is not None else ""),
        str(row.get("candidate_id") or ""),
        str(row.get("target") or ""),
        str(row.get("acquisition") or ""),
        str(row.get("status") or ""),
        _iso(row.get("approved_at")),
    ]
    return _keccak("|".join(parts))


def approver_hash(name: Optional[str]) -> bytes:
    """keccak256(批准人姓名)：链上有值无名，可核对、不泄露。"""
    return _keccak(str(name or "").strip())


def prediction_hash(row: Dict[str, Any], redacted: bool = False) -> bytes:
    """evo.prediction_register 一行的 canonical 哈希。

    rationale 是自由文本，先哈希再嵌入，保证长文本定长且改动即失配。
    """
    rationale = str(row.get("rationale") or "")
    parts = [
        "FSWP-PRED-1",
        str(row.get("experiment_no") or ""),
        str(row.get("model_version") or ""),
        _num(row.get("predicted"), redacted),
        _num(row.get("ci_low"), redacted),
        _num(row.get("ci_high"), redacted),
        _keccak(rationale).hex(),
        _iso(row.get("registered_at")),
    ]
    return _keccak("|".join(parts))


def milli(v: Any, redacted: bool) -> int:
    """数值 → 定点 ×1000 的 int32；脱敏或缺失时恒为 0（合约侧会二次校验）。"""
    if redacted or v is None:
        return 0
    try:
        return max(-2_147_483_648, min(2_147_483_647, int(round(float(v) * 1000))))
    except (TypeError, ValueError):
        return 0


def epoch(ts: Any) -> int:
    """链下时间戳 → unix 秒（只作核对，链上以 block.timestamp 为准）。"""
    if ts is None:
        return 0
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    try:
        return int(ts)
    except (TypeError, ValueError):
        try:
            return int(datetime.strptime(_iso(ts), "%Y-%m-%dT%H:%M:%SZ")
                       .replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            return 0


# ---------------------------------------------------------------- web3
class HostOverrideProvider(HTTPProvider):
    """直连 IP、不发送 SNI、用 Host 头路由的 JSON-RPC provider。

    为什么需要它：`rpc.botchain.ai` 的域名被 DNS 投毒（返回 Facebook 段 IP），
    即使用正确 IP 直连，TLS 的 ClientHello 里携带 SNI=rpc.botchain.ai 也会被
    RST 注入（实测 `write:errno=104`）。SNI 在 TLS 中是可选字段：
    不传 server_hostname 即不发送 SNI，改用 HTTP Host 头让 ELB 路由，
    握手即可正常完成（实测返回 `0x2a5` = 677）。

    安全性：仍校验证书链（CERT_REQUIRED），仅关闭主机名校验
    （因为不发送 SNI，服务端返回的是 ELB 默认证书 fluxlayer.xyz）。
    流量是到真实 IP 的直连，不经任何第三方代理。
    """

    def __init__(self, ip: str, host: str, port: int = 443, path: str = "/", timeout: int = 30):
        super().__init__(endpoint_uri=f"https://{ip}:{port}{path}", request_kwargs={"timeout": timeout})
        self._ip, self._host, self._port = ip, host, port
        self._path, self._timeout = path, timeout

    @staticmethod
    def _decode(body: bytes, chunked: bool) -> bytes:
        if not chunked:
            return body
        out, i = b"", 0
        while i < len(body):
            j = body.find(b"\r\n", i)
            if j < 0:
                break
            try:
                size = int(body[i:j].split(b";")[0], 16)
            except ValueError:
                break
            if size == 0:
                break
            out += body[j + 2:j + 2 + size]
            i = j + 2 + size + 2
        return out

    def make_request(self, method: str, params: Any) -> Dict[str, Any]:
        rid = next(self.request_counter) if hasattr(self, "request_counter") else 1
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or [], "id": rid}).encode()
        req = (f"POST {self._path} HTTP/1.1\r\n"
               f"Host: {self._host}\r\n"
               f"Content-Type: application/json\r\n"
               f"Content-Length: {len(body)}\r\n"
               f"Connection: close\r\n\r\n").encode() + body

        ctx = ssl.create_default_context()
        ctx.check_hostname = False          # 不发送 SNI → 无法做主机名校验
        ctx.verify_mode = ssl.CERT_REQUIRED  # 但仍然校验证书链
        data = b""
        with socket.create_connection((self._ip, self._port), timeout=self._timeout) as sock:
            with ctx.wrap_socket(sock) as ssock:   # 不传 server_hostname → 不发送 SNI
                ssock.sendall(req)
                while True:
                    chunk = ssock.recv(65536)
                    if not chunk:
                        break
                    data += chunk

        head, _, raw = data.partition(b"\r\n\r\n")
        status = head.split(b"\r\n")[0].decode(errors="ignore")
        if "200" not in status:
            raise RuntimeError(f"RPC 返回异常状态：{status[:80]}")
        headers = head.decode(errors="ignore").lower()
        return json.loads(self._decode(raw, "transfer-encoding: chunked" in headers).decode())


def _tester_mode() -> bool:
    """BOT_RPC_URL=tester:// → 用本地 EVM（eth-tester）跑通全流程，用于离线冒烟测试。"""
    return cfg()["rpc"].startswith("tester://")


_W3 = None
_W3_KEY = ""


def w3():
    """连接单例：重复调用复用同一实例（tester 模式下尤其重要，否则每次都是一条新空链）。"""
    global _W3, _W3_KEY
    from web3 import Web3

    c = cfg()
    if not (c["rpc"] or c["rpc_ip"]):
        raise RuntimeError("BOT_RPC_URL / BOT_RPC_IP 均未配置")
    key = f"{c['rpc']}|{c['rpc_ip']}|{c.get('rpc_host','')}"
    if _W3 is not None and _W3_KEY == key:
        return _W3
    if _tester_mode():
        from web3 import EthereumTesterProvider

        web = Web3(EthereumTesterProvider())
    elif c.get("rpc_ip"):
        web = Web3(HostOverrideProvider(c["rpc_ip"], c.get("rpc_host") or "rpc.botchain.ai"))
    else:
        web = Web3(Web3.HTTPProvider(c["rpc"], request_kwargs={"timeout": 30}))
    _W3, _W3_KEY = web, key
    return _W3


def account():
    c = cfg()
    if not c["pk"]:
        raise RuntimeError("BOT_PRIVATE_KEY 未配置")
    from eth_account import Account

    return Account.from_key(c["pk"])


def abi_bytecode(name: str = "FormulaSwarmAttestor"):
    p = ABI_DIR / f"{name}.json"
    if not p.exists():
        raise RuntimeError(f"缺少 {p} —— 先跑 `node chain/compile.js`")
    j = json.loads(p.read_text())
    return j["abi"], j["bytecode"]


def contract(address: Optional[str] = None):
    c = cfg()
    addr = address or c["contract"]
    if not addr:
        raise RuntimeError("BOT_CONTRACT 未配置（先部署或回填地址）")
    abi, _ = abi_bytecode()
    web = w3()
    return web.eth.contract(address=web.to_checksum_address(addr), abi=abi)


def _tx_params(web, acct) -> Dict[str, Any]:
    """优先 EIP-1559，失败回退 legacy gasPrice。"""
    base = {"from": acct.address, "nonce": web.eth.get_transaction_count(acct.address)}
    try:
        prio = web.eth.max_priority_fee
        base_fee = web.eth.get_block("latest").get("baseFeePerGas", web.eth.gas_price)
        base.update({"maxPriorityFeePerGas": prio, "maxFeePerGas": base_fee * 2 + prio})
        return base
    except Exception:
        base["gasPrice"] = web.eth.gas_price
        return base


def send(call, gas: int = 0, timeout: int = 180) -> Dict[str, Any]:
    """签名并发送一笔交易，等待回执。"""
    web, acct, c = w3(), account(), cfg()
    params = _tx_params(web, acct)
    params["chainId"] = web.eth.chain_id if _tester_mode() else c["chain_id"]
    built = call.build_transaction(params)
    built["gas"] = gas or web.eth.estimate_gas(built)
    signed = acct.sign_transaction(built)
    txh = web.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = web.eth.wait_for_transaction_receipt(txh, timeout=timeout)
    return {
        "tx_hash": txh.hex(),
        "block": rcpt.get("blockNumber"),
        "status": rcpt.get("status"),
        "gasUsed": rcpt.get("gasUsed"),
        "explorer": f"{c['explorer']}/tx/{txh.hex()}",
    }


# ---------------------------------------------------------------- 三项写链
def send_approvals(rows: Sequence[Dict[str, Any]], batch: bool = True) -> Dict[str, Any]:
    """① 人审批准留痕。rows 来自 evo.experiment_plan（status ∈ approved/executed）。"""
    items, seen = [], set()
    for r in rows:
        ph = plan_hash(r)
        if ph in seen:
            continue
        seen.add(ph)
        items.append((ph, approver_hash(r.get("approved_by")), epoch(r.get("approved_at"))))
    if not items:
        return {"sent": 0, "note": "无待锚定的批准记录"}

    co = contract()
    if not batch:
        out = [{"plan_hash": "0x" + ph.hex(), **send(co.functions.attestApproval(ph, ah, 1, at))}
               for ph, ah, at in items]
        return {"sent": len(out), "txs": out}

    # 同一批按批准人分组，保证 approverHash 语义正确；再按合约上限分片
    groups: Dict[bytes, List[Any]] = {}
    for ph, ah, at in items:
        groups.setdefault(ah, []).append((ph, at))
    out = []
    for ah, group in groups.items():
        for chunk in _chunks(group, BATCH_MAX):
            call = co.functions.attestApprovalBatch([g[0] for g in chunk], ah,
                                                    max(g[1] for g in chunk))
            out.append({"count": len(chunk), "approver_hash": "0x" + ah.hex(), **send(call)})
    return {"sent": sum(o["count"] for o in out), "txs": out}


def send_predictions(rows: Sequence[Dict[str, Any]], redacted: bool = True) -> Dict[str, Any]:
    """② 预测先行登记锁。redacted=True 时数值不进链（链上只留哈希与登记时刻）。"""
    items, seen = [], set()
    for r in rows:
        rh = prediction_hash(r, redacted=redacted)
        if rh in seen:
            continue
        seen.add(rh)
        items.append({
            "regHash": rh,
            "experimentNo": str(r.get("experiment_no") or "")[:64],
            "predictedMilli": milli(r.get("predicted"), redacted),
            "ciLowMilli": milli(r.get("ci_low"), redacted),
            "ciHighMilli": milli(r.get("ci_high"), redacted),
            "redacted": bool(redacted),
            "claimedAt": epoch(r.get("registered_at")),
        })
    if not items:
        return {"sent": 0, "note": "无待登记的预测"}
    co = contract()
    out = []
    for chunk in _chunks(items, BATCH_MAX):
        out.append({"count": len(chunk), **send(co.functions.registerPredictionBatch(chunk))})
    return {"sent": sum(o["count"] for o in out), "txs": out}


def register_agents(cards: Sequence[Dict[str, Any]], dry_run: bool = False) -> Dict[str, Any]:
    """③ Agent 链上身份（ERC-8004，走 BOT Chain Agent OS API，不写合约）。

    POST /v1/wallets → POST /v1/agents/identities。
    未配置 BOT_AGENTOS_URL / TOKEN 时返回等价 curl，便于人工执行。
    """
    base = os.environ.get("BOT_AGENTOS_URL", "").strip().rstrip("/")
    token = os.environ.get("BOT_AGENTOS_TOKEN", "").strip()
    c = cfg()
    payloads = [{
        "external_agent_id": f"formulaswarm:{a['id']}",
        "chain_id": c["chain_id"],
        "wallet_address": c["wallet"],
        "metadata": {
            "name": a["id"],
            "role": a.get("role", ""),
            "level": a.get("level", ""),
            "capabilities": a.get("capabilities", []),
            "trust_scope": a.get("trust_scope", "db:read_only"),
            "protocol": a.get("protocol", "formulaswarm/1.0"),
            "version": a.get("version", "1.0"),
        },
    } for a in cards]

    if not base or not token or dry_run:
        host = base or "<BOT_AGENTOS_URL>"
        body = json.dumps({"external_agent_id": "formulaswarm:<agent_id>", "chain_id": c["chain_id"]})
        return {
            "mode": "dry-run",
            "endpoint": f"{host}/v1/agents/identities",
            "count": len(payloads),
            "curl": (f"curl -s -X POST {host}/v1/wallets "
                     f"-H 'Authorization: Bearer $BOT_AGENTOS_TOKEN' "
                     f"-H 'content-type: application/json' "
                     f"-d '{body}'"),
            "payloads": payloads,
        }

    import requests

    out = []
    for p in payloads:
        w = requests.post(f"{base}/v1/wallets",
                          headers={"Authorization": f"Bearer {token}",
                                   "content-type": "application/json"},
                          json={"external_agent_id": p["external_agent_id"],
                                "chain_id": p["chain_id"]}, timeout=30)
        w.raise_for_status()
        wid = (w.json() or {}).get("wallet_id") or (w.json() or {}).get("id")
        r = requests.post(f"{base}/v1/agents/identities",
                          headers={"Authorization": f"Bearer {token}",
                                   "content-type": "application/json"},
                          json={"wallet_id": wid, "wallet_address": p["wallet_address"],
                                "chain_id": p["chain_id"], "metadata": p["metadata"]},
                          timeout=30)
        r.raise_for_status()
        out.append({"agent": p["external_agent_id"], "wallet_id": wid, "resp": r.json()})
    return {"mode": "live", "registered": out}


# ---------------------------------------------------------------- 降级队列
def queue(kind: str, rows: Iterable[Dict[str, Any]], redacted: bool = True) -> int:
    """FS_CHAIN=0 或 RPC 不可达时落队列，恢复后重放（与 FS_NO_LLM=1 同构）。"""
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with QUEUE_FILE.open("a", encoding="utf-8") as f:
        for r in rows:
            if kind == "approval":
                rec = {"kind": "approval", "plan_id": r.get("id"),
                       "plan_hash": "0x" + plan_hash(r).hex(),
                       "approver_hash": "0x" + approver_hash(r.get("approved_by")).hex(),
                       "claimed_at": epoch(r.get("approved_at")), "queued_at": time.time()}
            else:
                rec = {"kind": "prediction", "experiment_no": r.get("experiment_no"),
                       "reg_hash": "0x" + prediction_hash(r, redacted=redacted).hex(),
                       "redacted": bool(redacted),
                       "claimed_at": epoch(r.get("registered_at")), "queued_at": time.time()}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    return n
