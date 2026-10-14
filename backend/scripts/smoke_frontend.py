import sys, asyncio
from playwright.async_api import async_playwright

URL = "http://localhost:8000/"

async def main():
    errors, console_errs = [], []
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page()
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: console_errs.append(m.text) if m.type == "error" else None)
        await pg.goto(URL, wait_until="networkidle", timeout=30000)
        # 等首屏数据
        await pg.wait_for_timeout(1500)
        # 逐一切换旅程阶段
        steps = ["s2","s3","s4","s5","s6","s7","s8"]
        for s in steps:
            await pg.evaluate(f"window.go && go('{s}')")
            await pg.wait_for_timeout(1200)
        # 切到光刻胶并生成工艺窗口
        await pg.evaluate("() => { SYS='photoresist'; TARGET='resolution_nm'; buildTargetSeg && buildTargetSeg(); reload && reload(); }")
        await pg.wait_for_timeout(800)
        await pg.evaluate("window.go && go('s3')"); await pg.wait_for_timeout(800)
        # 点击分子化学键 tab
        await pg.evaluate("() => { const t=document.querySelector('#mol-tabs button[data-t=group]'); t && t.click(); }")
        await pg.wait_for_timeout(500)
        await pg.evaluate("window.go && go('s8')"); await pg.wait_for_timeout(500)
        await pg.evaluate("document.querySelector('#pw-btn').click()")
        await pg.wait_for_timeout(2000)
        shots = await pg.screenshot(path="/tmp/front.png", full_page=False)
        await b.close()
    print("PAGE_ERRORS:", errors)
    print("CONSOLE_ERRORS:", console_errs[:20])
    print("OK" if not errors else "HAS_ERRORS")

asyncio.run(main())
