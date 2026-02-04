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

async def scrape_detail_page(page, url):
    page_info = existing_pages_map.get(url)
    if not page_info: return
    
    page_id = page_info["page_id"]
    name = page_info["name"]

    try:
        print(f"🧐 正在抓取: {name}")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        
        table_selector = "div.article_sliders_table"
        await page.wait_for_selector(table_selector, timeout=10000)

        rows = await page.query_selector_all(f"{table_selector} tr")
        
        data = {
            "price_min": None, "price_max": None, "common_fee": None,
            "room_min": None, "room_max": None,
            "area_min": None, "area_max": None
        }

        for row in rows:
            th = await row.query_selector("th")
            if not th: continue
            label = await th.inner_text()
            td = await row.query_selector("td")
            if not td: continue
            text = (await td.inner_text()).replace("\n", "").strip()

            # 1. 解析价格和共益费
            if "家賃" in label:
                prices = re.findall(r"([\d,]+)円", text)
                if len(prices) >= 1:
                    data["price_min"] = prices[0].replace(",", "")
                    # 兜底：如果没有上限，就等于下限
                    data["price_max"] = prices[1].replace(",", "") if len(prices) >= 2 else data["price_min"]
                
                fee = re.search(r"\(([\d,]+)円\)", text)
                if fee: data["common_fee"] = fee.group(1).replace(",", "")

            # 2. 解析间取和面积
            elif "間取り/床面積" in label:
                # 匹配如 2LDK, 3DK
                rooms = re.findall(r"(\d[A-Z]+)", text)
                if len(rooms) >= 1:
                    data["room_min"] = rooms[0]
                    data["room_max"] = rooms[1] if len(rooms) >= 2 else data["room_min"]
                
                # 匹配如 64, 80
                areas = re.findall(r"([\d.]+)㎡", text)
                if len(areas) >= 1:
                    data["area_min"] = areas[0]
                    data["area_max"] = areas[1] if len(areas) >= 2 else data["area_min"]

        # --- 构造 Notion 属性 (带安全检查) ---
        props = {}
        
        # 数字类型转换：必须转为 int，且不能为 None
        if data["price_min"]: props["租金下限"] = {"number": int(data["price_min"])}
        if data["price_max"]: props["租金上限"] = {"number": int(data["price_max"])}
        if data["common_fee"]: props["管理费"] = {"number": int(data["common_fee"])}
        
        # 文本类型
        if data["area_min"]: props["面积下限"] = {"rich_text": [{"text": {"content": f"{data['area_min']}㎡"}}]}
        if data["area_max"]: props["面积上限"] = {"rich_text": [{"text": {"content": f"{data['area_max']}㎡"}}]}
        
        # Select 类型 (选项必须是字符串)
        if data["room_min"]: props["房型下限"] = {"select": {"name": data["room_min"]}}
        if data["room_max"]: props["房型上限"] = {"select": {"name": data["room_max"]}}
        
        if props:
            call_notion_api("PATCH", f"https://api.notion.com/v1/pages/{page_id}", {"properties": props})
            print(f"✅ 更新成功: {name}")

    except Exception as e:
        print(f"    ❌ 抓取/更新失败 {url}: {e}")

# main 函数保持你最后提供的那个版本即可，它已经是基于 Notion URL 列表遍历的了。

async def main():
    # 1. 第一步：获取 Notion 数据库中现有的所有页面和 URL
    await fetch_all_existing_pages()
    
    if not existing_pages_map:
        print("终止：Notion 数据库中没有发现任何带有 URL 的数据。")
        return

    # 2. 第二步：启动浏览器，遍历 URL 进行爬取
    async with async_playwright() as p:
        # headless=True 建议正式运行时开启，速度更快
        browser = await p.chromium.launch(headless=False) 
        context = await browser.new_context()
        page = await context.new_page()

        print(f"\n🚀 开始根据 Notion 列表更新详细数据，共 {len(existing_pages_map)} 个房源...")

        # 直接遍历同步回来的 URL 字典
        for url in existing_pages_map.keys():
            await scrape_detail_page(page, url)
            # 适当延迟，防止请求过快被封或触发 Notion API 速率限制
            await asyncio.sleep(1)

        await browser.close()
        print("\n✨ 所有房源数据更新任务已完成！")

if __name__ == "__main__":
    asyncio.run(main())

