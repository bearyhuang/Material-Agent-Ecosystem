#!/usr/bin/env python3
"""链层离线冒烟：用本地 EVM（eth-tester）跑通"部署 → 批准留痕 → 预测登记锁"全流程。

    .venv/bin/python tests/test_chain_local.py

不发真实交易、不需要网络。验证的是：
  1. 合约能部署、能读（VERSION / stats / owner / scribes）
  2. 批准留痕能批量写入，事件正确，重复运行幂等
  3. 预测登记锁能写入；redacted 时链上数值恒为 0；重复登记被拒
  4. 红线：redacted=true 却夹带数值 → 合约 revert（代码级强制，不是口头承诺）
  5. 链下 canonical 哈希确定性（改一个数就失配）
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ["BOT_RPC_URL"] = "tester://local"
os.environ["FS_CHAIN"] = "1"
os.environ["BOT_CHAIN_ID"] = "677"

import fschain  # noqa: E402
from web3 import EthereumTesterProvider, Web3  # noqa: E402


def tester_key() -> str:
    t = Web3(EthereumTesterProvider())
    return "0x" + t.provider.ethereum_tester.backend.account_keys[0].to_bytes().hex()


def plan(i: int, approver: str = "张工", status: str = "approved") -> dict:
    return {
        "id": i, "run_id": "s1-test01", "round": 1, "candidate_id": 100 + i,
        "target": "lap_shear_strength", "acquisition": "EI", "status": status,
        "approved_by": approver,
        "approved_at": datetime(2026, 10, 7, 9, 30, 0, tzinfo=timezone.utc),
    }


def pred(no: str, predicted: float) -> dict:
    return {
        "experiment_no": no, "model_version": "gp-v1",
        "predicted": predicted, "ci_low": predicted - 1.5, "ci_high": predicted + 1.5,
        "rationale": "环氧树脂增韧机理：填料含量提升剪切强度",
        "registered_at": datetime(2026, 10, 7, 10, 0, 0, tzinfo=timezone.utc),
    }


class TestChainLocal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["BOT_PRIVATE_KEY"] = tester_key()
        web = fschain.w3()
        abi, bytecode = fschain.abi_bytecode()
        acct = fschain.account()
        factory = web.eth.contract(abi=abi, bytecode=bytecode)
        tx = factory.constructor().build_transaction({
            "from": acct.address,
            "nonce": web.eth.get_transaction_count(acct.address),
            "chainId": web.eth.chain_id,
            "gasPrice": web.eth.gas_price,
        })
        tx["gas"] = web.eth.estimate_gas(tx)
        signed = acct.sign_transaction(tx)
        rcpt = web.eth.wait_for_transaction_receipt(
            web.eth.send_raw_transaction(signed.raw_transaction), timeout=60)
        assert rcpt["status"] == 1, "部署失败"
        os.environ["BOT_CONTRACT"] = rcpt["contractAddress"]
        cls.addr = rcpt["contractAddress"]
        cls.web, cls.abi = web, abi

    def co(self):
        return self.web.eth.contract(address=self.web.to_checksum_address(self.addr), abi=self.abi)

    # ---------------------------------------------------------- 1 部署与读
    def test_01_deployed_and_readable(self):
        co = self.co()
        self.assertEqual(co.functions.VERSION().call(), "fs-attestor/1.0")
        self.assertEqual(co.functions.stats().call(), [0, 0])
        self.assertTrue(co.functions.scribes(fschain.account().address).call())

    # ---------------------------------------------------------- 2 批准留痕
    def test_02_approval_batch_and_idempotent(self):
        rows = [plan(1), plan(2), plan(3, approver="李工")]
        res = fschain.send_approvals(rows)
        self.assertEqual(res["sent"], 3)
        co = self.co()
        self.assertEqual(co.functions.stats().call()[0], 3)

        ev = co.events.ApprovalAttested().get_logs(from_block=0)
        self.assertEqual(len(ev), 3)
        self.assertEqual({e["args"]["approverHash"] for e in ev}.__len__(), 2)  # 两个批准人

        # 幂等：再来一次，全部已存在 → 抛 nothing new
        with self.assertRaises(Exception):
            fschain.send_approvals(rows)
        self.assertEqual(co.functions.stats().call()[0], 3)

    # ---------------------------------------------------------- 3 预测登记锁
    def test_03_prediction_lock_redacted(self):
        res = fschain.send_predictions([pred("EXP-001", 18.5), pred("EXP-002", 22.25)], redacted=True)
        self.assertEqual(res["sent"], 2)
        co = self.co()
        p = co.functions.getPrediction(fschain.prediction_hash(pred("EXP-001", 18.5), True)).call()
        self.assertTrue(p[5], "redacted 标志应为 True")          # redacted
        self.assertEqual((p[2], p[3], p[4]), (0, 0, 0))          # 数值必须全 0
        self.assertEqual(co.functions.stats().call()[1], 2)

        with self.assertRaises(Exception):                        # 重复登记被拒
            fschain.send_predictions([pred("EXP-001", 18.5)], redacted=True)

    def test_04_prediction_plain_numbers(self):
        fschain.send_predictions([pred("EXP-010", 30.125)], redacted=False)
        co = self.co()
        p = co.functions.getPrediction(fschain.prediction_hash(pred("EXP-010", 30.125), False)).call()
        self.assertFalse(p[5])
        self.assertEqual(p[2], 30125)                             # 定点 ×1000

    # ---------------------------------------------------------- 4 红线硬校验
    def test_05_redacted_must_blank_numbers(self):
        co = self.co()
        acct = fschain.account()
        bad = co.functions.registerPrediction(
            fschain.prediction_hash(pred("EXP-BAD", 1.0), True), "EXP-BAD",
            1000, 0, 0, True, 0)
        with self.assertRaises(Exception):
            bad.call({"from": acct.address})                      # 合约侧二次校验必须拦下

    # ---------------------------------------------------------- 5 哈希确定性
    def test_06_canonical_hash_determinism(self):
        a, b = fschain.plan_hash(plan(1)), fschain.plan_hash(plan(1))
        self.assertEqual(a, b)
        tampered = plan(1)
        tampered["approved_at"] = datetime(2026, 10, 8, 9, 30, 0, tzinfo=timezone.utc)
        self.assertNotEqual(a, fschain.plan_hash(tampered), "改批准时间必须失配")

        p1 = fschain.prediction_hash(pred("EXP-001", 18.5))
        p2 = fschain.prediction_hash(pred("EXP-001", 19.9))
        self.assertNotEqual(p1, p2, "改预测值必须失配 —— 这正是登记锁的意义")
        self.assertNotEqual(
            fschain.prediction_hash(pred("EXP-001", 18.5), redacted=True),
            p1, "脱敏口径不同，哈希也应不同")

        self.assertEqual(fschain.approver_hash("张工"), fschain.approver_hash("张工"))
        self.assertNotEqual(fschain.approver_hash("张工"), fschain.approver_hash("李工"))
        self.assertEqual(len(fschain.approver_hash("张工")), 32)

    # ---------------------------------------------------------- 6 超过合约上限自动分片
    def test_08_batch_chunking_over_200(self):
        rows = [pred(f"EXP-C{i:04d}", 10.0 + i * 0.001) for i in range(205)]
        before = self.co().functions.stats().call()[1]
        import math

        res = fschain.send_predictions(rows, redacted=True)
        self.assertEqual(res["sent"], 205)
        self.assertEqual(len(res["txs"]), math.ceil(205 / fschain.BATCH_MAX),
                         f"205 条应按 BATCH_MAX={fschain.BATCH_MAX} 分片")
        self.assertTrue(all(t["gasUsed"] < 12_000_000 for t in res["txs"]),
                        "单笔 gas 应留足区块配额余量")
        self.assertEqual(self.co().functions.stats().call()[1], before + 205)

    # ---------------------------------------------------------- 7 降级队列
    def test_07_queue_when_chain_off(self):
        os.environ["FS_CHAIN"] = "0"
        self.assertFalse(fschain.enabled())
        n = fschain.queue("approval", [plan(900)])
        self.assertEqual(n, 1)
        os.environ["FS_CHAIN"] = "1"


if __name__ == "__main__":
    unittest.main(verbosity=2)
