import asyncio
from playwright.async_api import async_playwright
import time

TARGET_URLS = [
    "https://www.ur-net.go.jp/chintai/kanto/kanagawa/40_0520.html",
    "https://www.ur-net.go.jp/chintai/kanto/kanagawa/40_1130.html",
    "https://www.ur-net.go.jp/chintai/kanto/kanagawa/40_1510.html",
    "https://www.ur-net.go.jp/chintai/kanto/tokyo/20_1960.html",
    "https://www.ur-net.go.jp/chintai/kanto/kanagawa/40_1770.html",
    "https://www.ur-net.go.jp/chintai/kanto/tokyo/20_2340.html",
    "https://www.ur-net.go.jp/chintai/kanto/kanagawa/40_2600.html",
    "https://www.ur-net.go.jp/chintai/kanto/kanagawa/40_3290.html",
    "https://www.ur-net.go.jp/chintai/kanto/tokyo/20_2910.html",
    "https://www.ur-net.go.jp/chintai/kanto/kanagawa/40_2660.html",
    "https://www.ur-net.go.jp/chintai/kanto/kanagawa/40_1710.html"
]

async def check_with_browser(context, url):
    page = await context.new_page()
    short_name = url.split('/')[-1]
    try:
        print(f"正在检查: {short_name}...")
        
        # 1. 访问页面
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        
        # 2. 模拟真实用户行为：向下滚动一点点，触发懒加载 JS
        await page.mouse.wheel(0, 500)
        
        # 3. 【关键修改】显式等待房源行出现，或者显示“无房”文字
        # 我们给它最多 15 秒的时间去“生”出房源行
        try:
            # 等待 tr.js-log-item 或者是那个特定的无房提示 ID/Class
            await page.wait_for_selector("tr.js-log-item, .item_no-data, .list_none", timeout=15000)
        except:
            # 如果 15 秒都没出结果，可能是真的没房，也可能是网络卡了
            pass

        # 4. 再次确保数据渲染，稍微停顿 1 秒（玄学但有用）
        await asyncio.sleep(1)

        # 5. 精准判定
        rooms = page.locator("tbody.rep_room tr.js-log-item")
        count = await rooms.count() / 2
        
        if count > 0:
            return True, f"🚨 发现空房！共 {count} 间"
        
        # 检查是否有明确的“无房”提示（文字判断最稳）
        content = await page.content()
        if "ご案内できるお部屋がございません" in content:
            return False, "暂无空房"
            
        return False, "未发现房源（确认无房）"

    except Exception as e:
        return None, f"检测失败: {str(e)[:30]}"
    finally:
        await page.close()

async def start_monitor():
    async with async_playwright() as p:
        # 启动浏览器
        browser = await p.chromium.launch(headless=True) # 调试时可改 False
        # 模拟真实的浏览器特征
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        print(f"--- 开启巡检 ({len(TARGET_URLS)}个目标) ---")
        
        # 为了防止被反爬封禁，建议不要跑太快
        for url in TARGET_URLS:
            status, msg = await check_with_browser(context, url)
            print(f"[{url.split('/')[-1]}] {msg}")
            await asyncio.sleep(3) # 增加间隔
            
        await browser.close()

if __name__ == "__main__":
    asyncio.run(start_monitor())