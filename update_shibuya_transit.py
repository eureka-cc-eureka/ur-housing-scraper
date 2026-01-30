import requests
import json
import datetime
from datetime import timedelta
from googlemaps import Client as GoogleMapsClient
import os
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

# === 配置区域 ===
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
    # 打印调试信息，防止静默错误
    response = requests.request(method, url, headers=headers, json=data)
    if response.status_code != 200:
        print(f"Error: {response.status_code}, {response.text}")
    return response.json()

def update_shibuya_driving_commute():
    # 1. 准备时间参数：设定为明天早上 8:00 出发，模拟早高峰开车
    now = datetime.datetime.now()
    dept_time = datetime.datetime(now.year, now.month, now.day, 8, 0) + timedelta(days=1)
    
    # 2. 过滤器：抓取【通勤时间】为空，且【纬度】【经度】不为空的数据
    filter_data = {
        "filter": {
            "and": [
                {"property": "通勤时间", "number": {"is_empty": True}},
                {"property": "纬度", "number": {"is_not_empty": True}},
                {"property": "经度", "number": {"is_not_empty": True}}
            ]
        }
    }

    query_url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    all_pages = []
    has_more = True
    next_cursor = None

    print("📡 正在抓取具备坐标且待计算的数据...")
    while has_more:
        payload = filter_data.copy()
        if next_cursor: payload["start_cursor"] = next_cursor
        res = call_notion_api("POST", query_url, data=payload)
        all_pages.extend(res.get("results", []))
        has_more = res.get("has_more", False)
        next_cursor = res.get("next_cursor")

    if not all_pages:
        print("🎉 没有需要计算的数据。")
        return

    print(f"🔎 找到 {len(all_pages)} 条房源，开始计算开车到涩谷的时间...")

    for page in all_pages:
        page_id = page["id"]
        props = page["properties"]

        # 直接获取经纬度
        lat = props["纬度"].get("number")
        lng = props["经度"].get("number")
        
        # 获取名称用于显示日志
        name_list = props.get("房源名称", {}).get("title", [])
        name = name_list[0]["text"]["content"] if name_list else "未知房源"

        print(f" 🚗 [处理中]: {name} (坐标: {lat}, {lng})")

        try:
            # 3. 使用经纬度元组作为起点计算开车路径
            directions_result = gmaps.directions(
                origin=(lat, lng),  # 直接传入元组
                destination="涩谷站", # 也可以传入 "35.6580,139.7016"
                departure_time=dept_time,
                mode="driving",
                traffic_model="best_guess", # 考虑实时路况预测
                language="ja"
            )

            if directions_result:
                # duration_in_traffic 是包含路况预估的时间
                leg = directions_result[0]['legs'][0]
                if 'duration_in_traffic' in leg:
                    shibuya_min = (leg['duration_in_traffic']['value'] + 59) // 60
                else:
                    shibuya_min = (leg['duration']['value'] + 59) // 60
                
                print(f"   ⏱️ 开车预计: {shibuya_min} 分钟")
                
                # 4. 更新 Notion
                update_data = {"properties": {"通勤时间": {"number": shibuya_min}}}
                call_notion_api("PATCH", f"https://api.notion.com/v1/pages/{page_id}", update_data)
            else:
                print(f"   ⚠️ 无法规划路线: {name}")

        except Exception as e:
            print(f" ❌ [异常]: {name} -> {e}")

if __name__ == "__main__":
    update_shibuya_driving_commute()