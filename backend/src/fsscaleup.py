"""多保真放大修正（500g → 50kg → 5t）—— 只能作为修正项，不能替代实测。

机理（化工放大经典四条）：
  P/V（单位体积功率） ∝ N³D²，等叶尖线速度放大时 P/V ∝ 1/D
  叶尖线速度  πND          → 恒定为第一准则
  传热  A/V ∝ 1/D          → 放大后放热峰温度升高、固化度分布变宽
  滴加时间常数 τ ∝ V/加料速率 → 放大后局部浓度过高风险

硬约束：
  - 修正系数一律带 `validated` 标记；validated=false 的修正只能出现在数据卡的修正项里，
    不得写回 mat.performance，也不得被当作实测值引用。
  - 修正后的预测必须标注 corrected=true 与 correction 明细（谁修正的、按哪条规则）。
"""
from __future__ import annotations

from fsdb import connect

SCALE_VOLUME_L = {"lab_500g": 0.5, "pilot_50kg": 50.0, "prod_5t": 5000.0}
ORDER = ["lab_500g", "pilot_50kg", "prod_5t"]

CORR_SQL = """
select from_scale, to_scale, factor_kind, formula, coefficient, exponent,
       reference_value, validated, evidence_ref, note
from mat.scaleup_correction
"""


def load_corrections():
    with connect() as c, c.cursor() as cur:
        cur.execute(CORR_SQL)
        return cur.fetchall()


def volume_ratio(from_scale, to_scale):
    v1 = SCALE_VOLUME_L.get(from_scale)
    v2 = SCALE_VOLUME_L.get(to_scale)
    if not v1 or not v2:
        raise ValueError(f"未知规模：{from_scale} → {to_scale}")
    return v2 / v1


def correction_factors(from_scale, to_scale, corrections=None):
    """返回 {factor_kind: {factor, validated, formula, note, evidence_ref}}。"""
    corrections = corrections or load_corrections()
    ratio = volume_ratio(from_scale, to_scale)
    out = {}
    for r in corrections:
        if r["from_scale"] != from_scale or r["to_scale"] != to_scale:
            continue
        factor = float(r["coefficient"]) * (ratio ** float(r["exponent"]))
        out[r["factor_kind"]] = {
            "factor": round(factor, 4),
            "validated": bool(r["validated"]),
            "formula": r["formula"],
            "volume_ratio": round(ratio, 3),
            "evidence_ref": r["evidence_ref"],
            "note": r["note"],
        }
    return out


def apply(value, from_scale, to_scale, corrections=None, kinds=None, max_shift=0.15,
          max_inflation=1.0):
    """放大修正：**不动点预测，只放宽不确定度**。

    为什么不动点预测：P/V、叶尖线速度、A/V、滴加时间常数这些是**物理量之比**，
    不是性能换算系数；把它们直接乘到强度上等于编造精度。
    正确做法是把它们列为"放大风险项"，据此把置信区间放宽，并注明须由中试实测确认。

    返回：value 原值不变；interval_inflation 为区间放大倍数（1.0 = 不放宽）。
    """
    factors = correction_factors(from_scale, to_scale, corrections)
    if not factors:
        return {"value": float(value), "corrected": False, "applied": {},
                "interval_inflation": 1.0,
                "note": f"无 {from_scale}→{to_scale} 的修正规则，未做修正"}
    applied, total_shift = {}, 0.0
    for kind, f in factors.items():
        if kinds and kind not in kinds:
            continue
        shift = (f["factor"] - 1.0)
        shift = max(-max_shift, min(max_shift, shift))      # 夹住单项外推幅度
        applied[kind] = {"factor": f["factor"], "applied_shift": round(shift, 4),
                         "validated": f["validated"], "formula": f["formula"],
                         "note": f["note"]}
        total_shift += abs(shift)
    inflation = round(1.0 + min(max_inflation, total_shift), 4)
    return {
        "value": float(value), "corrected": True,
        "raw_value": round(float(value), 4),
        "total_shift": round(total_shift, 4),
        "interval_inflation": inflation,
        "applied": applied,
        "validated": all(a["validated"] for a in applied.values()),
        "note": "放大修正只放宽区间、不改动点预测；validated=false 表示尚无实测证据支撑，"
                "真实值须由中试/量产实测确认",
    }


def chain(from_scale, to_scale):
    """跨级放大时按序串起各级修正（lab→5t = lab→50kg + 50kg→5t）。"""
    i, j = ORDER.index(from_scale), ORDER.index(to_scale)
    if i > j:
        raise ValueError("只支持放大方向")
    return [(ORDER[k], ORDER[k + 1]) for k in range(i, j)]
