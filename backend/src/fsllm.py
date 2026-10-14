"""LLM 接入封装：只用于「选择 · 解释 · 辩论 · 质疑」，绝不产出性能数值。

无 OPENAI_API_KEY / 调用失败时静默回退到规则论证，保证离线也能跑完整流程。
"""
import os


def enabled():
    return bool(os.getenv("OPENAI_API_KEY")) and os.getenv("FS_NO_LLM") != "1"


def chat(system_prompt, user_prompt, fallback, max_chars=120):
    """返回论证文本；失败时返回 fallback（并标注回退原因）。"""
    if not enabled():
        return fallback
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI

        kw = {"model": os.getenv("FS_MODEL", "gpt-4o-mini"),
              "temperature": 0.2, "timeout": 30}
        if os.getenv("OPENAI_BASE_URL"):
            kw["base_url"] = os.getenv("OPENAI_BASE_URL")
        llm = ChatOpenAI(**kw)
        text = llm.invoke([SystemMessage(content=system_prompt),
                           HumanMessage(content=user_prompt)]).content.strip()
        return text[:max_chars]
    except Exception as e:                       # 网络/额度问题一律回退，不阻断流水线
        return f"{fallback}（LLM 不可用，回退规则论证：{type(e).__name__}）"


NO_NUMBER_RULE = ("你只能做定性论证与选择，禁止给出任何性能指标数值（MAE/R²/覆盖率等），"
                  "数值由确定性评测产生。回答不超过 40 字。")
