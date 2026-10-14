"""FormulaAgents · 苹果 H5 气泡风格视觉主题（可复用）。

统一视觉语言：iMessage 风格圆角气泡、毛玻璃质感、系统蓝 #007AFF、
柔和阴影、SF / PingFang 字体、分组浅灰背景。
所有组件均为纯 HTML + CSS（通过 st.markdown(unsafe_allow_html=True) 注入），
不依赖任何第三方前端库，保持 Streamlit 原生数据流不变。
"""

# ---------------- 苹果调色板 ----------------
APPLE = {
    "blue":   "#007AFF",
    "blue_d": "#0A84FF",
    "green":  "#34C759",
    "red":    "#FF3B30",
    "orange": "#FF9500",
    "purple": "#AF52DE",
    "teal":   "#5AC8FA",
    "pink":   "#FF2D55",
    "yellow": "#FFCC00",
    "gray":   "#8E8E93",
    "gray_l": "#C7C7CC",
    "bg":     "#F2F2F7",   # 分组背景（Apple 浅灰）
    "card":   "#FFFFFF",
    "ink":    "#1C1C1E",
    "ink_2":  "#48484A",
    "ink_3":  "#8E8E93",
}

_RADIUS = "20px"


def _escape(text):
    """转义 HTML 特殊字符，避免注入与换行破坏布局。"""
    if text is None:
        return ""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


# ---------------- 全局 CSS 注入 ----------------
def inject_apple_css():
    """在页面顶部调用一次：覆盖 Streamlit 默认观感，统一为苹果 H5 风格。"""
    css = f"""
    <style>
    /* 字体与基础背景 */
    @import url('https://fonts.googleapis.com/css2?family=SF+Pro+Display:wght@400;500;600;700&display=swap');
    html, body, [class*="stApp"] {{
        background: {APPLE['bg']} !important;
        color: {APPLE['ink']} !important;
        font-family: "SF Pro Display", "PingFang SC", -apple-system,
                     BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei",
                     "Noto Sans CJK SC", sans-serif !important;
    }}
    .stApp {{ background: {APPLE['bg']} !important; }}

    /* 顶部标题区：毛玻璃胶囊 */
    h1, h2, h3 {{ font-weight: 700 !important; letter-spacing: -0.02em; }}
    .stApp header {{ backdrop-filter: saturate(180%) blur(20px); }}

    /* 按钮 → 苹果蓝圆角 */
    .stButton > button {{
        border-radius: 14px !important;
        background: {APPLE['blue']} !important;
        color: #fff !important;
        border: none !important;
        font-weight: 600 !important;
        padding: 0.5rem 1.1rem !important;
        box-shadow: 0 4px 14px rgba(0,122,255,0.28) !important;
        transition: transform .12s ease, box-shadow .12s ease;
    }}
    .stButton > button:hover {{
        transform: translateY(-1px);
        box-shadow: 0 6px 18px rgba(0,122,255,0.36) !important;
    }}
    .stButton > button:active {{ transform: translateY(0); }}

    /* 输入控件圆角 */
    .stTextInput > div > div > input,
    .stTextArea > div > div > textarea,
    .stSelectbox > div > div,
    .stMultiSelect > div > div {{
        border-radius: 14px !important;
        border: 1px solid {APPLE['gray_l']} !important;
        background: #fff !important;
    }}

    /* Tabs → 苹果分段控件风格 */
    .stTabs [data-baseweb="tab-list"] {{
        background: #E5E5EA !important;
        border-radius: 12px !important;
        padding: 4px !important;
        gap: 4px !important;
    }}
    .stTabs [data-baseweb="tab"] {{
        border-radius: 9px !important;
        font-weight: 600 !important;
        color: {APPLE['ink_2']} !important;
    }}
    .stTabs [aria-selected="true"] {{
        background: #fff !important;
        color: {APPLE['blue']} !important;
        box-shadow: 0 2px 6px rgba(0,0,0,0.08) !important;
    }}

    /* 指标卡圆角 + 轻阴影 */
    .stMetric {{
        background: {APPLE['card']} !important;
        border-radius: 18px !important;
        padding: 12px 16px !important;
        box-shadow: 0 2px 10px rgba(0,0,0,0.06) !important;
    }}

    /* Expander 圆角 */
    .streamlit-expanderHeader {{
        border-radius: 14px !important;
        background: #fff !important;
    }}
    .streamlit-expanderContent {{
        border-radius: 0 0 14px 14px !important;
    }}

    /* 表格/数据框圆角 */
    .stDataFrame, .stTable {{ border-radius: 16px !important; overflow: hidden; }}

    /* 代码块圆角 */
    .stCodeBlock {{ border-radius: 14px !important; }}

    /* 隐藏默认右侧菜单留白，让内容更"H5" */
    .block-container {{ padding-top: 2rem !important; }}
    </style>
    """
    import streamlit as st
    st.markdown(css, unsafe_allow_html=True)


# ---------------- 气泡组件 ----------------
def bubble(text, role="agent", avatar=None, time=None, accent=None):
    """iMessage 风格对话气泡。

    role="user"  → 苹果蓝气泡，靠右；role="agent" → 白色毛玻璃气泡，靠左。
    avatar 为 emoji 或短文本；time 为可选时间戳。
    """
    accent = accent or APPLE["blue"]
    esc = _escape(text)
    # 简单换行处理
    esc = esc.replace("\n", "<br/>")

    if role == "user":
        bubble_bg = f"linear-gradient(135deg, {APPLE['blue']}, {APPLE['blue_d']})"
        color = "#fff"
        justify = "flex-end"
        margin = "0 0 0 auto"
        radius = "20px 20px 6px 20px"
        shadow = "0 6px 16px rgba(0,122,255,0.32)"
    else:
        bubble_bg = "rgba(255,255,255,0.82)"
        color = APPLE["ink"]
        justify = "flex-start"
        margin = "0 auto 0 0"
        radius = "20px 20px 20px 6px"
        shadow = "0 4px 14px rgba(0,0,0,0.08)"
        # 在 agent 气泡左侧贴一条强调色（系统级语义）
        if accent:
            color = APPLE["ink"]

    av = ""
    if avatar:
        av_html = _escape(avatar)
        av = (f"<div style='font-size:22px;line-height:38px;width:38px;height:38px;"
              f"text-align:center;border-radius:50%;background:#fff;"
              f"box-shadow:0 2px 8px rgba(0,0,0,0.1);margin:2px 8px 0 0;flex:0 0 auto'>"
              f"{av_html}</div>")
        if role == "user":
            av = (f"<div style='font-size:22px;line-height:38px;width:38px;height:38px;"
                  f"text-align:center;border-radius:50%;"
                  f"background:linear-gradient(135deg,{APPLE['blue']},{APPLE['blue_d']});"
                  f"color:#fff;box-shadow:0 2px 8px rgba(0,122,255,0.3);"
                  f"margin:2px 0 0 8px;flex:0 0 auto'>{av_html}</div>")

    tm = (f"<div style='font-size:11px;color:{APPLE['ink_3']};"
          f"text-align:{'right' if role=='user' else 'left'};"
          f"margin:2px 6px 0'>{_escape(time)}</div>") if time else ""

    left = av if role == "agent" else ""
    right = av if role == "user" else ""
    accent_bar = ""
    if role == "agent" and accent:
        accent_bar = (f"<span style='position:absolute;left:0;top:10px;bottom:10px;"
                      f"width:4px;border-radius:4px;background:{accent}'></span>")

    return f"""
    <div style="display:flex;flex-direction:column;margin:10px 0;align-items:{justify}">
      <div style="display:flex;align-items:flex-start;max-width:86%">
        {left}
        <div style="position:relative;background:{bubble_bg};color:{color};
                    border-radius:{radius};padding:12px 16px;box-shadow:{shadow};
                    font-size:15px;line-height:1.55;backdrop-filter:blur(8px);
                    word-break:break-word">
          {accent_bar}
          {esc}
        </div>
        {right}
      </div>
      {tm}
    </div>
    """


def chat_turn(question, answer, q_time=None, a_time=None, intent=None, trace=None):
    """一问一答：用户蓝气泡 + 助手白气泡，组合成一段对话。"""
    out = bubble(question, role="user", avatar="🧑", time=q_time)
    meta = ""
    if intent or trace:
        meta = (f"<div style='font-size:12px;color:{APPLE['ink_3']};"
                f"margin:0 0 4px 6px'>意图 `{_escape(intent)}`"
                f" · trace `{_escape(trace)}`</div>")
    out += f"<div>{meta}</div>" if meta else ""
    out += bubble(answer, role="agent", avatar="🤖", time=a_time, accent=APPLE["blue"])
    return out


# ---------------- 卡片 / 胶囊组件 ----------------
def card(title, body_html, accent=None, icon=None):
    """毛玻璃圆角卡片：标题 + 自定义 HTML 正文。"""
    accent = accent or APPLE["blue"]
    ic = (f"<span style='font-size:20px;margin-right:8px'>{_escape(icon)}</span>"
          if icon else "")
    return f"""
    <div style="background:rgba(255,255,255,0.78);border-radius:{_RADIUS};
                padding:16px 18px;box-shadow:0 4px 16px rgba(0,0,0,0.07);
                border-left:5px solid {accent};backdrop-filter:blur(10px);
                margin:8px 0">
      <div style="font-weight:700;font-size:15px;color:{APPLE['ink']};
                  display:flex;align-items:center;margin-bottom:8px">
        {ic}{_escape(title)}
      </div>
      <div style="color:{APPLE['ink_2']};font-size:14px;line-height:1.6">
        {body_html}
      </div>
    </div>
    """


def stat_chip(label, value, accent=None, sub=None):
    """苹果风格统计胶囊：彩色强调 + 大数值。"""
    accent = accent or APPLE["blue"]
    sub_html = (f"<div style='font-size:11px;color:{APPLE['ink_3']};margin-top:2px'>"
                f"{_escape(sub)}</div>") if sub else ""
    return f"""
    <div style="background:#fff;border-radius:18px;padding:14px 16px;
                box-shadow:0 3px 12px rgba(0,0,0,0.06);
                border-top:4px solid {accent};text-align:center">
      <div style="font-size:12px;color:{APPLE['ink_3']};font-weight:600">{_escape(label)}</div>
      <div style="font-size:24px;font-weight:700;color:{APPLE['ink']};margin:2px 0">{_escape(value)}</div>
      {sub_html}
    </div>
    """


def pill(text, accent=None):
    """小圆角标签胶囊（用于状态/分类）。"""
    accent = accent or APPLE["blue"]
    return (f"<span style='display:inline-block;background:{accent};color:#fff;"
            f"font-size:12px;font-weight:600;padding:3px 11px;border-radius:999px;"
            f"margin:2px 4px 2px 0'>{_escape(text)}</span>")


def banner(ok, text, icon_ok="✅", icon_bad="🚫"):
    """顶部状态横幅：通过=绿，失败=红。"""
    if ok:
        bg, fg, ic = "rgba(52,199,89,0.14)", APPLE["green"], icon_ok
    else:
        bg, fg, ic = "rgba(255,59,48,0.12)", APPLE["red"], icon_bad
    return (f"<div style='background:{bg};color:{fg};border-radius:16px;"
            f"padding:12px 16px;font-weight:600;font-size:14px;"
            f"display:flex;align-items:center;gap:8px;margin:8px 0'>{ic} {_escape(text)}</div>")


def section_title(text, sub=None):
    """H5 分区标题（带左侧强调条）。"""
    sub_html = (f"<div style='font-size:13px;color:{APPLE['ink_3']};font-weight:400;"
                f"margin-top:2px'>{_escape(sub)}</div>") if sub else ""
    return f"""
    <div style="display:flex;align-items:center;gap:10px;margin:18px 0 8px">
      <div style="width:5px;height:22px;border-radius:4px;background:{APPLE['blue']}"></div>
      <div>
        <div style="font-size:18px;font-weight:700;color:{APPLE['ink']}">{_escape(text)}</div>
        {sub_html}
      </div>
    </div>
    """
