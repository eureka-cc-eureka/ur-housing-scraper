import asyncio
from playwright.async_api import async_playwright
import re
import requests
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

# --- 配置 (请确保 token 和 ID 正确) ---
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("DATABASE_ID")
MAX_PRICE = 160000
AREAS = ["tokyo", "kanagawa", "chiba"]

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

# 全局变量：用于存储数据库现有房源，实现加速比对和下架检测
# 格式: { "url": {"page_id": "xxx", "price": 123} }
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
            url_prop = page["properties"].get("链接", {}).get("url")
            price_prop = page["properties"].get("租金", {}).get("number")
            name_list = page["properties"].get("房源名称", {}).get("title", [])
            name_text = name_list[0].get("plain_text", "未知房源") if name_list else "未知房源"
            status_prop = page["properties"].get("房屋状态", {}).get("status", {}).get("name") 

            if url_prop:
                existing_pages_map[url_prop] = {
                    "page_id": page["id"],
                    "price": price_prop,
                    "name": name_text,
                    "status": status_prop
                }
        
        has_more = res.get("has_more")
        next_cursor = res.get("next_cursor")
    
    print(f"✅ 同步完成，库中现有 {len(existing_pages_map)} 条房源。")

async def scrape_room_details(page, detail_url, seen_urls):
    """
    seen_urls: 本次爬虫运行中见到的所有 URL 集合
    """
    try:
        seen_urls.add(detail_url) # 记录此 URL 依然存活
        
        await page.goto(detail_url, wait_until="domcontentloaded")
        await page.wait_for_selector(".roomprice_body_emphasis", timeout=10000)
        
        rent_text = await page.eval_on_selector(".roomprice_body_emphasis", "el => el.innerText")
        current_price = int(''.join(re.findall(r'\d+', rent_text)))
        
        if current_price > MAX_PRICE:
            return False
        
        async def get_coords(p):
            return await p.evaluate('''() => {
                const latEl = document.querySelector(".js-lat-data");
                const lngEl = document.querySelector(".js-lng-data");
                return latEl && lngEl ? { lat: latEl.value, lng: lngEl.value } : null;
            }''')

        coords = await get_coords(page)

        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # --- 核心逻辑：使用本地 Map 进行比对 ---
        if detail_url in existing_pages_map:
            page_info = existing_pages_map[detail_url]
            page_id = page_info["page_id"]
            old_price = page_info["price"]
            old_status = page_info.get("status")
            existing_name = page_info["name"]

            update_properties = {}

            if old_status == "已下线":
                update_properties["房屋状态"] = {"status": {"name": "空室可租"}}
                update_properties["我的状态"] = {"status": {"name": "待筛选"}} # 可选：复活后重新提醒筛选
                print(f"    🔥 [房源复活]: {existing_name} 重新上线了！")

            if current_price != old_price:
                update_properties["租金"] = {"number": current_price}
                update_properties["更新时间"] = {"date": {"start": now}}
                print(f"    🆙 [价格变动]: {existing_name} ￥{old_price}->￥{current_price}")

            if update_properties:
                # 确保包含更新时间
                if "更新时间" not in update_properties:
                    update_properties["更新时间"] = {"date": {"start": now}}
                call_notion_api("PATCH", f"https://api.notion.com/v1/pages/{page_id}", {"properties": update_properties})
            else:
                # 无变动，仅静默更新活跃时间
                call_notion_api("PATCH", f"https://api.notion.com/v1/pages/{page_id}", 
                                {"properties": {"更新时间": {"date": {"start": now}}}})
                print(f"    😴 [保持现状]: {existing_name}")
            
            return True

        # --- 新房源逻辑 ---
        # (字段抓取逻辑保持不变...)
        area_el = await page.query_selector(".item_subtitle")
        area_name = re.sub(r'\(.*?\).*', '', (await area_el.inner_text()).split('\n')[0]).strip() if area_el else "UR"
        room_el = await page.query_selector(".item_title.rep_room-nm") or await page.query_selector(".item_title")
        room_no = (await room_el.inner_text()).replace('最近見た部屋', '').strip() if room_el else ""
        full_title = f"{area_name} {room_no}".strip()

        price_area = await page.locator(".roomprice_item, li.roomprice, .roomprice_body").first.inner_text()
        fee_match = re.search(r'\((\d+,?\d+)円\)', price_area)
        fee = int(fee_match.group(1).replace(',', '')) if fee_match else 0
        
        layout_size_el = await page.query_selector(".rep_madori-yuka")
        layout_size_text = (await layout_size_el.inner_text()).strip() if layout_size_el else ""
        room_type, size_text = ("待确认", "未知")
        if "/" in layout_size_text:
            parts = layout_size_text.split("/")
            room_type, size_text = parts[0].strip(), parts[1].strip()

        floor_el = await page.query_selector(".rep_kai")
        floor_text = (await floor_el.inner_text()).strip() if floor_el else "未知"
        years_el = await page.query_selector(".rep_years")
        years_text = (await years_el.inner_text()).strip() if years_el else "未知"

        if not coords:
            map_url = detail_url.replace("_room.html", "_room_map.html")
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
            "房源名称": {"title": [{"text": {"content": full_title}}]},
            "租金": {"number": current_price},
            "管理费": {"number": fee},
            "纬度": {"number": lat_num}, 
            "经度": {"number": lng_num},
            "面积": {"rich_text": [{"text": {"content": size_text}}]},
            "总费用": {"number": current_price + fee},
            "我的状态": {"status": {"name": "待筛选"}},
            "楼层": {"rich_text": [{"text": {"content": floor_text}}]},
            "房型": {"select": {"name": room_type}},
            "管理年份": {"rich_text": [{"text": {"content": years_text}}]},
            "更新时间": {"date": {"start": now}},
            "链接": {"url": detail_url},
            "房屋状态": {"status": {"name": "空室可租"}},
        }
        
        if call_notion_api("POST", "https://api.notion.com/v1/pages", {"parent": {"database_id": DATABASE_ID}, "properties": props}):
            print(f"    ✨ [新录入]: {full_title}")
            return True
            
    except Exception as e:
        print(f"    ⚠️ 抓取失败: {e}")
    return False

async def main():
    # 1. 初始化数据库快照
    await fetch_all_existing_pages()
    
    # 本次见到的所有 URL 集合
    seen_urls = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        for area_code in AREAS:
            print(f"\n🌍 === 正在开始抓取地区: {area_code.upper()} ===")
            await page.goto(f"https://www.ur-net.go.jp/chintai/kanto/{area_code}/area/")
            await page.evaluate("""() => {
                document.querySelectorAll("input[type='checkbox']:not(:disabled)").forEach(b => {
                    b.checked = true;
                    b.dispatchEvent(new Event('change', { bubbles: true }));
                });
            }""")
            await page.wait_for_timeout(2000)
            await page.goto(f"https://www.ur-net.go.jp/chintai/kanto/{area_code}/result/")

            page_num = 1
            while True:
                print(f"--- 📄 {area_code.upper()} 正在扫描第 {page_num} 页 ---")
                try:
                    await page.wait_for_selector("a:has-text('部屋詳細')", timeout=15000)
                except:
                    break

                links = [f"https://www.ur-net.go.jp{await btn.get_attribute('href')}" 
                         for btn in await page.query_selector_all("a:has-text('部屋詳細')")]
                
                detail_page = await context.new_page()
                for link in links:
                    await scrape_room_details(detail_page, link, seen_urls)
                await detail_page.close()

                # 翻页逻辑
                next_btn = await page.query_selector("li.next a, a:has-text('次へ')")
                if next_btn and await next_btn.is_visible():
                    page_num += 1
                    await next_btn.click()
                    await page.wait_for_timeout(4000)
                else: break

        await browser.close()

    # 3. 标记下架房源
    print("\n🧹 正在检查并更新已下架房源状态...")
    deleted_count = 0
    for url, info in existing_pages_map.items():
        if url not in seen_urls and info.get("status") != "已下线":
            # 该房源在数据库里有，但本次遍历网页没抓到 -> 说明已下架
            # 不再删除，而是将“我的状态”更新为“已下线”
            update_data = {
                "properties": {
                    "房屋状态": {"status": {"name": "已下线"}}
                }
            }
            # 如果你希望同时清空租金或者更新时间，可以在这里添加
            call_notion_api("PATCH", f"https://api.notion.com/v1/pages/{info['page_id']}", update_data)
            deleted_count += 1
            print(f"    💤 [房源下线]: {info['name']} ({url})")
    
    print(f"\n🎉 任务圆满完成！新增/更新完毕，并标记了 {deleted_count} 条已下线数据。")

if __name__ == "__main__":
    asyncio.run(main())