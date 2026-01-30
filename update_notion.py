import requests
import json
from datetime import datetime
from googlemaps import Client as GoogleMapsClient

import os
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

# 从环境变量读取（代码里不再出现真实的字符串）
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("DATABASE_ID")
GMAPS_KEY = os.getenv("GMAPS_KEY")

gmaps = GoogleMapsClient(key=GMAPS_KEY)

def call_notion_api(method, url, data=None):
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    response = requests.request(method, url, headers=headers, json=data)
    if response.status_code != 200:
        print(f"Error: {response.status_code}, {response.text}")
    return response.json()

def update_walking_time_via_coords():
    query_url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    
    # 过滤器：只抓取“步行时间”为空，且“纬度/经度”已有的数据
    filter_data = {
        "filter": {
            "and": [
                {"property": "步行时间", "number": {"is_empty": True}},
                {"property": "纬度", "number": {"is_not_empty": True}},
                {"property": "经度", "number": {"is_not_empty": True}}
            ]
        }
    }

    print("📡 正在从 Notion 抓取具备坐标的数据...")
    all_pages = []
    has_more = True
    next_cursor = None

    while has_more:
        payload = filter_data.copy()
        if next_cursor: payload["start_cursor"] = next_cursor
        res = call_notion_api("POST", query_url, data=payload)
        all_pages.extend(res.get("results", []))
        has_more = res.get("has_more", False)
        next_cursor = res.get("next_cursor")

    if not all_pages:
        print("🎉 没有需要计算步行时间的数据（或坐标缺失）。")
        return

    print(f"🔎 找到 {len(all_pages)} 条具备坐标的数据，开始计算...")

    for page in all_pages:
        page_id = page["id"]
        props = page["properties"]

        # 1. 直接获取经纬度数值
        lat = props["纬度"].get("number")
        lng = props["经度"].get("number")
        
        # 获取房源名称（仅用于日志打印）
        name_list = props.get("房源名称", {}).get("title", [])
        address = name_list[0]["text"]["content"] if name_list else "未知房源"

        print(f" 🚀 [开始计算]: {address} ({lat}, {lng})")

        try:
            # 2. 搜索最近车站（基于经纬度坐标）
            # 直接使用坐标 (lat, lng)，精确度极高
            places = gmaps.places_nearby(location=(lat, lng), radius=3000, type='train_station')
            stations = places.get('results', [])[:4]

            if not stations:
                print(f" ⚠️ [未找到]: {address} 周边 3km 无车站")
                call_notion_api("PATCH", f"https://api.notion.com/v1/pages/{page_id}", 
                               {"properties": {"步行时间": {"number": 999}}})
                continue

            # 3. 计算从坐标到车站的步行时间
            dest_ids = [f"place_id:{st['place_id']}" for st in stations]
            matrix = gmaps.distance_matrix(
                origins=(lat, lng),
                destinations=dest_ids,
                mode="walking"
            )

            durations = []
            for element in matrix['rows'][0]['elements']:
                if element.get('status') == 'OK':
                    durations.append(element['duration']['value'])

            if durations:
                min_time = (min(durations) + 59) // 60
                
                # 4. 更新回 Notion
                update_data = {"properties": {"步行时间": {"number": min_time}}}
                call_notion_api("PATCH", f"https://api.notion.com/v1/pages/{page_id}", update_data)
                print(f" ✅ [成功]: {address} -> 步行 {min_time} 分钟")

        except Exception as e:
            print(f" ❌ [异常]: {address} -> {e}")

if __name__ == "__main__":
    update_walking_time_via_coords()