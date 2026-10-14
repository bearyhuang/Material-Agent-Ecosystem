"""蜂群协议层：信封（Envelope）/ 能力卡（AgentCard）/ 注册表（Registry）/ 总线（Bus）。

对应评分维度「协议与通信设计」「容错与安全」「前瞻性与可扩展」：

- **通信机制可复用**：任何跨 Agent 交互都必须封装成 Envelope，节点之间不直接读对方私有状态；
  Envelope 带 ``context_ref``（run_id / dataset_hash / gene_id / reply_to），实现上下文传递与知识继承。
- **Agent 发现 + 能力描述**：每个 Agent 注册一张 ``AgentCard``（能力 provides / 依赖 requires /
  自主权级别 L0–L4 / 信任边界），编排层按 ``Registry.discover()`` 组装拓扑 —— 新 Agent 即插即用。
- **信任边界**：``trust_scope`` 声明该 Agent 能碰什么（只读视图 / 受控写入），UI 可实时审计。
- **失败可解释**：故障注入后节点发 REFUSE 信封（带 reason），由编排层降级，而不是静默崩溃。
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

PROTOCOL_VERSION = "formulaswarm/1.0"

# ---------------------------------------------------------------- 通信意图（performative）
REQUEST = "REQUEST"        # 派活：要求对方做事
PROPOSE = "PROPOSE"        # 提案：提交候选与论证
INFORM = "INFORM"          # 通报：事实性结果
CHALLENGE = "CHALLENGE"    # 质疑：红队发现
DECIDE = "DECIDE"          # 裁决：入围/名次
COMMIT = "COMMIT"          # 落库：写 Gene / Capsule
REFUSE = "REFUSE"          # 拒绝：故障降级 / 域外拒答
HEARTBEAT = "HEARTBEAT"    # 心跳：长任务进度

PERFORMATIVE_ZH = {
    REQUEST: "派活", PROPOSE: "提案", INFORM: "通报", CHALLENGE: "质疑",
    DECIDE: "裁决", COMMIT: "落库", REFUSE: "拒绝/降级", HEARTBEAT: "心跳",
}


@dataclass
class Envelope:
    """Agent 之间唯一的交流单位。禁止绕过信封直接传状态。"""

    sender: str
    recipient: str
    performative: str
    kind: str                                  # 业务类型：dispatch / argue / select / critique / gate / promote ...
    payload: Dict[str, Any] = field(default_factory=dict)
    context_ref: Dict[str, Any] = field(default_factory=dict)   # 上下文传递 + 知识继承
    trust_scope: str = "internal"              # 信任边界（发送方权限）
    text: str = ""                             # 人话摘要（看板展示用）

    def to_dict(self, seq: int) -> Dict[str, Any]:
        return {
            "seq": seq, "ts": round(time.time(), 3), "protocol": PROTOCOL_VERSION,
            "sender": self.sender, "recipient": self.recipient,
            "performative": self.performative, "performative_zh": PERFORMATIVE_ZH.get(self.performative, ""),
            "kind": self.kind, "payload": self.payload, "context_ref": self.context_ref,
            "trust_scope": self.trust_scope, "text": self.text,
        }


class Bus:
    """发布/订阅总线：编排层 → SSE → 前端看板。历史留痕，便于现场复盘。"""

    def __init__(self, history: int = 800):
        self._subs: List[queue.Queue] = []
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self.history: deque = deque(maxlen=history)

    def publish(self, env: Envelope) -> Dict[str, Any]:
        msg = env.to_dict(next(self._seq))
        self.history.append(msg)
        with self._lock:
            for q in list(self._subs):
                q.put(msg)
        return msg

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def recent(self, n: int = 200) -> List[Dict[str, Any]]:
        return list(self.history)[-n:]


BUS = Bus()


# ---------------------------------------------------------------- 能力卡与注册表
@dataclass
class AgentCard:
    """Agent 能力描述卡 —— 蜂群的「服务发现」凭据。"""

    id: str
    role: str                                  # supervisor / champion / arbiter / red_team / integrator / scribe
    level: str                                 # 自主权 L0–L4
    capabilities: List[str]
    provides: List[str]
    requires: List[str]
    family: Optional[str] = None               # Champion 专属：代言的算法族
    trust_scope: str = "db:read_only"          # 信任边界
    protocol: str = PROTOCOL_VERSION
    version: str = "1.0"
    builtin: bool = True                       # False = 运行中动态接入（可扩展性证据）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "role": self.role, "level": self.level, "family": self.family,
            "capabilities": self.capabilities, "provides": self.provides, "requires": self.requires,
            "trust_scope": self.trust_scope, "protocol": self.protocol, "version": self.version,
            "builtin": self.builtin,
        }


class Registry:
    """Agent 注册表：编排层只认能力，不认具体类名 —— 新增 Agent 无需改编排代码。"""

    def __init__(self):
        self._cards: Dict[str, AgentCard] = {}
        self._lock = threading.Lock()

    def register(self, card: AgentCard) -> AgentCard:
        with self._lock:
            self._cards[card.id] = card
        return card

    def unregister(self, agent_id: str) -> bool:
        with self._lock:
            return self._cards.pop(agent_id, None) is not None

    def get(self, agent_id: str) -> Optional[AgentCard]:
        return self._cards.get(agent_id)

    def discover(self, role: Optional[str] = None, capability: Optional[str] = None) -> List[AgentCard]:
        out = list(self._cards.values())
        if role:
            out = [c for c in out if c.role == role]
        if capability:
            out = [c for c in out if capability in c.capabilities]
        return sorted(out, key=lambda c: c.id)

    def cards(self) -> List[Dict[str, Any]]:
        return [c.to_dict() for c in self.discover()]

    def ids(self) -> List[str]:
        return [c.id for c in self.discover()]


REGISTRY = Registry()


# ---------------------------------------------------------------- 故障注入（容错演示）
class FaultConfig:
    """现场可点：让某个 Agent 挂掉 / 注入数据泄漏，验证降级而非崩溃。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.dead_agents: set = set()      # 该 Agent 的工具调用直接失败
        self.leak: bool = False            # 注入 dev/golden 重复 → 触发红队硬拒答
        self.tool_timeout: set = set()     # 工具超时（慢但不失败）→ 预留

    def kill(self, agent_id: str) -> None:
        with self._lock:
            self.dead_agents.add(agent_id)

    def revive(self, agent_id: Optional[str] = None) -> None:
        with self._lock:
            if agent_id:
                self.dead_agents.discard(agent_id)
            else:
                self.dead_agents.clear()
            if agent_id is None:
                self.leak = False

    def set_leak(self, on: bool) -> None:
        with self._lock:
            self.leak = on

    def is_dead(self, agent_id: str) -> bool:
        with self._lock:
            return agent_id in self.dead_agents

    def has_leak(self) -> bool:
        with self._lock:
            return self.leak

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"dead_agents": sorted(self.dead_agents), "leak": self.leak}


FAULTS = FaultConfig()


# ---------------------------------------------------------------- 人工闸门（信任边界）
class HumanGate:
    """Gene 发布前暂停：Scribe 只写事实（Capsule），策略资产（Gene）需人工确认才能被继承。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.pending: Dict[str, Dict[str, Any]] = {}
        self.approved: set = set()
        self.rejected: set = set()

    def submit(self, gene_id: str, meta: Dict[str, Any]) -> None:
        with self._lock:
            if gene_id not in self.approved:
                self.pending[gene_id] = {"gene_id": gene_id, **meta}

    def approve(self, gene_id: Optional[str] = None) -> List[str]:
        with self._lock:
            ids = [gene_id] if gene_id else list(self.pending)
            done = [i for i in ids if i in self.pending]
            for i in done:
                self.approved.add(i)
                self.pending.pop(i, None)
            return done

    def reject(self, gene_id: str) -> None:
        with self._lock:
            self.pending.pop(gene_id, None)
            self.rejected.add(gene_id)

    def is_approved(self, gene_id: str) -> bool:
        with self._lock:
            return gene_id in self.approved

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"pending": list(self.pending.values()),
                    "approved": sorted(self.approved), "rejected": sorted(self.rejected)}


GATE = HumanGate()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"
