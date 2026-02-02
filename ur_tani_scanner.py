import asyncio
from playwright.async_api import async_playwright
import re
import requests
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

# --- 配置 ---
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("DATABASE_D_ID")
AREAS = ["tokyo", "kanagawa", "chiba"]

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

existing_pages_map = {}

def call_notion_api(method, url, data=None):
    try:
        if method == "POST":
            response = requests.post(url, headers=HEADERS, json=data)
        elif method == "PATCH":
            response = requests.patch(url, headers=HEADERS, json=data)

        if response.status_code not in [200, 201]:
            print(f"❌ Notion API 错误 ({response.status_code}): {response.text}")
            return None
        return response.json()
    except Exception as e:
        print(f"❌ 网络请求异常: {e}")
        return None

async def fetch_all_existing_pages():
    """程序启动时，一次性获取数据库所有房源的 URL 和价格"""
    global existing_pages_map
    print("📡 正在同步 Notion 数据库现状...")
    query_url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    has_more = True
    next_cursor = None

    while has_more:
        payload = {"page_size": 100}
        if next_cursor:
            payload["start_cursor"] = next_cursor

        res = call_notion_api("POST", query_url, payload)
        if not res: break

        for page in res.get("results", []):
            name_list = page["properties"].get("团地名称", {}).get("title", [])
            name_text = name_list[0].get("plain_text", "未知房源") if name_list else "未知房源"
            url_prop = page["properties"].get("链接", {}).get("url")
            if url_prop:
                existing_pages_map[url_prop] = {
                    "page_id": page["id"],
                    "name": name_text,
                }
        has_more = res.get("has_more")
        next_cursor = res.get("next_cursor")

    print(f"✅ 同步完成，库中现有 {len(existing_pages_map)} 条团地。")

async def scrape_danchi_details(page, danchi_url, seen_urls):
    danchi_name = "未知团地"
    try:
        # 调试日志：确认进入了函数
        seen_urls.add(danchi_url)
        
        # 改用 networkidle，确保网络请求相对安静
        await page.goto(danchi_url, wait_until="commit", timeout=30000)
        await page.wait_for_selector("h1.article_headings", timeout=5000)
        try:
            # 使用 JavaScript 精准提取 span 里的文字，忽略 rt 注音
            danchi_name = await page.evaluate('''() => {
                const rubySpan = document.querySelector("h1.article_headings ruby span");
                const fallbackH1 = document.querySelector("h1.article_headings");
                if (rubySpan) return rubySpan.innerText.trim();
                if (fallbackH1) return fallbackH1.innerText.split('\\n')[0].trim();
                return "名称解析失败";
            }''')
        except Exception as e:
            print(f"    ⚠️ 名称抓取重试中... {e}")

        print(f"    🏘️ 抓取到团地名称: {danchi_name}")
        async def get_coords(p):
            return await p.evaluate('''() => {
                const latEl = document.querySelector(".js-lat-data");
                const lngEl = document.querySelector(".js-lng-data");
                return latEl && lngEl ? { lat: latEl.value, lng: lngEl.value } : null;
            }''')
        
        map_url = danchi_url.replace(".html", "_map.html")
        print(f"    🔄 主页未找到坐标，尝试跳转地图页: {map_url}")
        await page.goto(map_url, wait_until="domcontentloaded")
        # 在地图页给一点缓冲时间
        await page.wait_for_timeout(1000)
        coords = await get_coords(page)
        if coords:
            lat_num = float(coords['lat'])
            lng_num = float(coords['lng'])
            print(f"    📍 坐标抓取成功: {lat_num}, {lng_num}")
        else:
            print(f"    ⚠️ 最终未能找到坐标标签")
            lat_num, lng_num = 0.0, 0.0

        
        props = {
            "团地名称": {"title": [{"text": {"content": danchi_name}}]},
            "纬度": {"number": lat_num}, 
            "经度": {"number": lng_num},
            "链接": {"url": danchi_url},
            "更新时间": {"date": {"start": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")}}
        }
        
        # 执行上传
        call_notion_api("POST", "https://api.notion.com/v1/pages", {"parent": {"database_id": DATABASE_ID}, "properties": props})
        print(f"    ✨ [新增] {danchi_name} ({lat_num}, {lng_num})")
    except Exception as e:
        print(f"    ❌ 抓取失败 {danchi_url}: {e}")

async def main():
    await fetch_all_existing_pages()
    seen_urls = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        for area_code in AREAS:
            print(f"\n🌍 正在扫描地区: {area_code.upper()}")
            # 必须先经过这个页面并勾选，否则直接进入 result 可能会没数据
            await page.goto(f"https://www.ur-net.go.jp/chintai/kanto/{area_code}/area/")
            await page.evaluate('document.querySelectorAll("input[type=\'checkbox\']").forEach(i => i.checked = true)')
            
            # 点击搜索按钮或直接跳转结果页
            await page.goto(f"https://www.ur-net.go.jp/chintai/kanto/{area_code}/result/")

            page_num = 1
            while True:
                print(f"--- 📄 {area_code.upper()} 正在扫描第 {page_num} 页 ---")
                try:
                    await page.wait_for_selector("a.rep_bukken-link", timeout=10000)
                except:
                    print("  ℹ️ 该地区扫描完毕或未发现房源")
                    break
                
                # 获取当前页所有链接
                links = [f"https://www.ur-net.go.jp{await el.get_attribute('href')}" 
                         for el in await page.query_selector_all("a.rep_bukken-link")]

                # 复用同一个详情页对象，避免开太多窗口导致电脑卡死
                worker_page = await context.new_page()
                for link in links:
                    await scrape_danchi_details(worker_page, link, seen_urls)
                await worker_page.close()

                # 翻页
                next_btn = await page.query_selector("li.next a, a:has-text('次へ')")
                if next_btn and await next_btn.is_visible():
                    page_num += 1
                    await next_btn.click()
                    await page.wait_for_timeout(4000)
                else: break

        await browser.close()
        print("\n🎉 任务全部完成！")

if __name__ == "__main__":
    asyncio.run(main())