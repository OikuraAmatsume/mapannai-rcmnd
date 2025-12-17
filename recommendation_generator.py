#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AWS Lambda Function: recommendation_generator
功能：根据用户地理位置和偏好，生成旅游推荐内容（Editor.js 格式）
"""

import json
import re
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Any, Optional, Tuple

import boto3
import requests
import google.generativeai as genai

# 导入配置
from config import Config

# 注意：S3 和 Gemini 客户端将在 lambda_handler 中初始化，确保配置验证后使用正确的密钥

# HTTP 请求头配置
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}


def map_budget_to_price_level(budget: str) -> Tuple[Optional[int], Optional[int]]:
    """
    将用户预算映射为 Google Places API 的 price_level
    """
    budget_mapping = {
        "3000日元以内": (None, 1),  # 0 到 1 (免费到便宜)
        "8000日元以内": (None, 2),  # 0 到 2 (免费到中等)
        "8000日元以上": (3, 4),      # 3 到 4 (中等到非常昂贵)
    }
    return budget_mapping.get(budget, (None, None))


def map_chinese_type_to_keyword(main_type: str, sub_type: str, include_time_keyword: bool = False) -> str:
    """
    将中文类型映射为 Google Places API 的英文关键词
    """
    if main_type == "美食":
        sub_type_mapping = {
            "异国料理": "international restaurant",
            "拉面": "ramen",
            "烤肉": "yakiniku",
            "寿喜烧": "sukiyaki",
            "中华": "chinese restaurant",
            "海鲜": "seafood",
            "居酒屋": "izakaya",
        }
        return sub_type_mapping.get(sub_type, "restaurant")
    elif main_type == "名胜古迹和旅游景点":
        return "tourist_attraction"
    elif main_type == "跳蚤市场或活动":
        if include_time_keyword:
            # 添加时间相关的关键词以提高搜索到近期活动的概率
            return "flea market event upcoming this month"
        return "market"
    else:
        return "point_of_interest"


def get_search_radius(main_type: str) -> int:
    """根据主类型返回搜索半径（米）"""
    radius_mapping = {
        "美食": Config.FOOD_SEARCH_RADIUS,
        "名胜古迹和旅游景点": Config.ATTRACTION_SEARCH_RADIUS,
        "跳蚤市场或活动": Config.MARKET_SEARCH_RADIUS,
    }
    return radius_mapping.get(main_type, Config.ATTRACTION_SEARCH_RADIUS)


def get_max_results(main_type: str) -> Optional[int]:
    """根据主类型返回最大结果数量"""
    if main_type == "美食":
        return Config.FOOD_MAX_RESULTS
    return None  # 不设上限


def get_cors_headers() -> Dict[str, str]:
    """
    获取 CORS 响应头
    """
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "POST, OPTIONS"
    }


def upload_image_to_s3(s3_client: Any, photo_reference: str, place_name: str) -> Optional[str]:
    """
    下载 Google Photo 并上传到 S3
    """
    try:
        if not Config.GOOGLE_PLACES_API_KEY:
            print(f"错误: GOOGLE_PLACES_API_KEY 未设置")
            return None
        
        if not Config.S3_BUCKET_NAME:
            print(f"错误: S3_BUCKET_NAME 未设置")
            return None
        
        # 使用配置的 S3 图片前缀（默认为 'poi-images/'）
        image_prefix = Config.S3_IMAGE_PREFIX
        
        # 获取 Google Photo URL
        photo_url = f"https://maps.googleapis.com/maps/api/place/photo?maxwidth={Config.IMAGE_MAX_WIDTH}&photoreference={photo_reference}&key={Config.GOOGLE_PLACES_API_KEY}"
        
        # 下载图片（添加 User-Agent 以避免 403 错误）
        response = requests.get(photo_url, stream=True, timeout=Config.PLACES_API_TIMEOUT, headers=DEFAULT_HEADERS)
        response.raise_for_status()
        
        # 生成 S3 key（使用时间戳和地点名称）
        # 图片将上传到 mapannai 桶中的 Temp 文件夹下
        safe_place_name = "".join(c for c in place_name if c.isalnum() or c in (' ', '-', '_')).strip()[:50]
        s3_key = f"{image_prefix}{int(time.time())}_{safe_place_name.replace(' ', '_')}.jpg"
        
        # 流式上传到 S3
        # 注意：新创建的 S3 bucket 默认不允许 ACL，因此移除 ACL 参数
        # 如果需要公开访问，请通过 bucket 策略或对象策略设置
        s3_client.upload_fileobj(
            response.raw,
            Config.S3_BUCKET_NAME,
            s3_key,
            ExtraArgs={'ContentType': 'image/jpeg'}
        )
        
        # 生成公共 URL
        s3_url = f"https://{Config.S3_BUCKET_NAME}.s3.{Config.S3_REGION}.amazonaws.com/{s3_key}"
        return s3_url
        
    except Exception as e:
        print(f"上传图片到 S3 失败: {str(e)}")
        return None


def is_event_within_date_range(place_details: Dict[str, Any], days_ahead: int = 30) -> bool:
    """
    检查活动是否在未来指定天数内
    """
    # 计算日期范围
    today = datetime.now()
    end_date = today + timedelta(days=days_ahead)
    
    # 尝试从多个字段中提取时间信息
    name = place_details.get("name", "").lower()
    description = place_details.get("editorial_summary", {}).get("overview", "").lower()
    if not description:
        description = place_details.get("description", "").lower()
    
    text_to_check = f"{name} {description}"
    
    # 检查是否包含明确的过去时间关键词（应排除）
    past_time_keywords = [
        "last month", "last week", "yesterday", "過去", "先月", "先週"
    ]
    for keyword in past_time_keywords:
        if keyword in text_to_check:
            return False
    
    # 检查是否包含近期/未来时间关键词（应包含）
    future_time_keywords = [
        "upcoming", "this month", "next month", "coming soon", "soon",
        "今月", "来月", "近日", "近日開催", "開催予定", "予定"
    ]
    for keyword in future_time_keywords:
        if keyword in text_to_check:
            return True
    
    # 尝试从文本中提取日期（简单模式匹配）
    # 格式：YYYY-MM-DD, MM/DD, 或类似格式
    date_patterns = [
        r'\d{4}[-/]\d{1,2}[-/]\d{1,2}',  # YYYY-MM-DD 或 YYYY/MM/DD
        r'\d{1,2}[-/]\d{1,2}',  # MM-DD 或 MM/DD
    ]
    
    for pattern in date_patterns:
        matches = re.findall(pattern, text_to_check)
        for match in matches:
            try:
                # 尝试解析日期
                if len(match.split('-')) == 3 or len(match.split('/')) == 3:
                    if '-' in match:
                        event_date = datetime.strptime(match, "%Y-%m-%d")
                    else:
                        event_date = datetime.strptime(match, "%Y/%m/%d")
                else:
                    # MM-DD 格式，假设是今年
                    if '-' in match:
                        month, day = match.split('-')
                    else:
                        month, day = match.split('/')
                    event_date = datetime(today.year, int(month), int(day))
                    # 如果日期已过，假设是明年
                    if event_date < today:
                        event_date = datetime(today.year + 1, int(month), int(day))
                
                # 检查日期是否在范围内
                if today <= event_date <= end_date:
                    return True
            except (ValueError, IndexError):
                continue
    
    # 如果无法确定时间，默认返回 True（不过滤）
    # 因为 Google Places API 可能不提供详细的活动时间信息
    # 通过搜索关键词已经尽量筛选了近期活动
    return True


def fetch_places_via_gemini(lat: float, lng: float, main_type: str) -> List[Dict[str, Any]]:
    """
    使用 Gemini AI 根据经纬度搜索名胜古迹和旅游景点或活动
    """
    try:
        # 构建 Gemini Prompt
        if main_type == "名胜古迹和旅游景点":
            search_type = "名胜古迹、历史遗迹、文化景点、旅游景点"
            description = "历史意义、文化价值、建筑特色、旅游价值"
        else:  # 跳蚤市场或活动
            search_type = "跳蚤市场、文化活动、节庆活动"
            description = "活动内容、特色亮点"
        
        # 对于跳蚤市场或活动，需要额外要求举办时间和官方网站
        if main_type == "跳蚤市场或活动":
            system_instruction = f"""你是一个专业的日本旅游信息专家。
请根据提供的经纬度坐标 ({lat}, {lng})，搜索5公里范围内的{search_type}。

对于每个地点，你需要提供：
1. 地点名称（形式：中文名称（日文名称），尽量使用官方名称）
2. 详细地址
3. 经纬度坐标（latitude: 纬度, longitude: 经度）- 必须是准确的坐标值
4. {description}的中文概述（{Config.ATTRACTION_SUMMARY_MAX_LENGTH}字以内），必须在概述中明确指出举办时间（具体日期和时间）
5. 官方网站URL（如果存在）

重要要求和优先级规则：
- 必须返回至少{Config.MARKET_MAX_RESULTS}个地点
- **优先级规则：当搜索"跳蚤市场或活动"时，请务必优先推荐"跳蚤市场"（フリーマーケット、フリマ、跳蚤市场），然后再推荐其他类型的活动（文化活动、节庆活动等）**
- **结果必须按相关性降序排列，跳蚤市场排在前面，其他活动排在后面**
- 如果符合条件的活动不足{Config.MARKET_MAX_RESULTS}个，请扩大搜索范围或包含更多相关活动
- 不需要提供图片URL，跳蚤市场或活动不需要图片
- 仅返回 JSON 格式数据，不添加任何额外文字说明

返回格式必须是可解析的 JSON 数组，包含至少5个地点。"""
            
            user_prompt = f"""请搜索经纬度 ({lat}, {lng}) 周围5公里范围内的{search_type}。

要求：
1. **必须返回至少{Config.MARKET_MAX_RESULTS}个地点**（如果符合条件的活动不足{Config.MARKET_MAX_RESULTS}个，请尽量搜索更多相关活动，确保返回至少{Config.MARKET_MAX_RESULTS}个结果）
2. **优先级要求：请务必优先推荐"跳蚤市场"（フリーマーケット、フリマ、跳蚤市场），然后再推荐其他类型的活动。结果必须按此优先级排序，跳蚤市场排在前面。**
3. 每个地点必须包含：
   - place_name: 地点名称（形式：中文名称（日文名称），尽量使用官方名称）
   - place_address: 详细地址
   - latitude: 纬度（浮点数，例如：35.4437）- **必填项，必须是准确的坐标值**
   - longitude: 经度（浮点数，例如：139.6380）- **必填项，必须是准确的坐标值**
   - summary: {description}的中文概述（{Config.ATTRACTION_SUMMARY_MAX_LENGTH}字以内），必须在概述中明确指出举办时间（具体日期和时间，例如："2024年1月15日 10:00-16:00"）
   - website: 官方网站URL（如果存在，如果不存在则为空字符串）
4. 只返回未来30天内的活动
5. 不需要提供图片URL
6. **重要：必须返回至少{Config.MARKET_MAX_RESULTS}个地点，如果搜索结果不足{Config.MARKET_MAX_RESULTS}个，请扩大搜索范围或包含更多相关活动**
7. **经纬度（latitude 和 longitude）是必填项，必须为每个地点提供准确的坐标值**

返回格式（必须是有效的 JSON，包含至少{Config.MARKET_MAX_RESULTS}个地点，跳蚤市场优先）："""
            
            # 构造 JSON Schema
            # 定义单个地点对象的结构
            place_object_schema = {
                "type": "object",
                "properties": {
                    "place_name": {"type": "string", "description": "地点的中文名称（形式：中文名称（日文名称））。"},
                    "place_address": {"type": "string", "description": "地点的完整地址。"},
                    "latitude": {"type": "number", "description": "地点的准确纬度（浮点数）。"},
                    "longitude": {"type": "number", "description": "地点的准确经度（浮点数）。"},
                    "summary": {"type": "string", "description": f"地点的中文概要，字数不超过 {Config.ATTRACTION_SUMMARY_MAX_LENGTH} 字，必须在概述中明确指出举办时间（具体日期和时间）。"},
                    "website": {"type": "string", "description": "地点的官方网站或活动介绍链接。如果找不到官方链接，则提供一个包含活动信息的可靠网站链接。如果不存在则为空字符串。"},
                },
                # 确保这些字段是必填的
                "required": ["place_name", "place_address", "latitude", "longitude", "summary", "website"]
            }
            
            # 定义最终响应的结构
            response_schema = {
                "type": "object",
                "properties": {
                    "places": {
                        "type": "array",
                        "description": f"附近至少 {Config.MARKET_MAX_RESULTS} 个地点的列表，跳蚤市场优先。",
                        "items": place_object_schema
                    }
                },
                "required": ["places"]
            }
        else:
            system_instruction = f"""你是一个专业的日本旅游信息专家。
请根据提供的经纬度坐标，搜索5公里范围内的{search_type}。
对于每个地点，你需要提供：
1. 地点名称（中文或日文，尽量使用官方名称）
2. 详细地址
3. {description}的中文概述（{Config.ATTRACTION_SUMMARY_MAX_LENGTH}字以内）

注意：不需要提供图片URL，图片将通过其他方式获取。

返回格式必须是可解析的 JSON 数组。"""
            
            user_prompt = f"""请搜索经纬度 ({lat}, {lng}) 周围5公里范围内的{search_type}。

要求：
1. 返回{Config.ATTRACTION_MAX_RESULTS}个地点
2. 每个地点必须包含：
   - name: 地点名称（尽量使用官方名称，便于后续搜索）
   - address: 详细地址
   - summary: {description}的中文概述（{Config.ATTRACTION_SUMMARY_MAX_LENGTH}字以内）
3. 不需要提供图片URL

返回格式（必须是有效的 JSON，包含{Config.ATTRACTION_MAX_RESULTS}个地点）：
[
  {{
    "name": "地点名称",
    "address": "详细地址",
    "summary": "{description}的中文概述（{Config.ATTRACTION_SUMMARY_MAX_LENGTH}字以内）"
  }},
  ...
]"""
        
        # 调用 Gemini API
        model = genai.GenerativeModel(
            model_name=Config.GEMINI_MODEL_NAME,
            system_instruction=system_instruction
        )
        
        # 对于跳蚤市场或活动，使用普通输出（当前 SDK 不支持 response_mime_type/response_schema）
        response = model.generate_content(user_prompt)
        response_text = response.text.strip() if response.text else ""

        # 检查响应是否为空
        if not response_text:
            raise ValueError("Gemini 返回空响应")

        # 清理响应文本（移除可能的 markdown 代码块标记）
        if response_text.startswith("```json"):
            response_text = response_text[7:]
        if response_text.startswith("```"):
            response_text = response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]
        response_text = response_text.strip()

        # 解析 JSON
        if not response_text:
            raise ValueError("清理后的响应为空")
        places = json.loads(response_text)
        
        # 验证和格式化数据
        processed_places = []
        for place in places:
            # 对于跳蚤市场或活动，需要处理两种可能的字段名：
            # 1. JSON Schema 返回的字段名（place_name, place_address, latitude, longitude）
            # 2. 普通输出返回的字段名（name, address, lat, lng）
            if main_type == "跳蚤市场或活动":
                # 优先使用 JSON Schema 字段名，如果没有则使用普通字段名
                place_name = place.get("place_name") or place.get("name", "")
                place_address = place.get("place_address") or place.get("address", "")
                latitude = place.get("latitude") or place.get("lat")
                longitude = place.get("longitude") or place.get("lng")
            else:
                place_name = place.get("name", "")
                place_address = place.get("address", "")
                latitude = None
                longitude = None
            
            if not place_name:
                continue
            
            # 对于跳蚤市场或活动，从 Gemini 返回的数据中获取 website 和经纬度
            # 对于名胜古迹和旅游景点，website 可能为空，后续从 Google Places API 获取
            place_data = {
                "name": place_name,
                "formatted_address": place_address,
                "summary": place.get("summary", ""),
                "rating": 0,  # Gemini 返回的数据可能没有评分，后续从 Google Places API 获取
                "website": place.get("website", ""),  # 对于跳蚤市场或活动，从 Gemini 获取；对于名胜古迹，后续从 Google Places API 获取
            }
            
            # 对于跳蚤市场或活动，添加经纬度信息
            if main_type == "跳蚤市场或活动":
                if latitude is not None and longitude is not None:
                    place_data["lat"] = float(latitude)
                    place_data["lng"] = float(longitude)
                else:
                    # 如果 JSON Schema 返回的数据中没有经纬度（不应该发生，因为设置了 required），记录警告
                    print(f"警告：地点 {place_name} 的 JSON Schema 响应中缺少经纬度，将在后续处理中通过地址获取")
            
            processed_places.append(place_data)
        
        return processed_places
        
    except json.JSONDecodeError as e:
        print(f"JSON 解析错误: {str(e)}")
        print(f"响应内容: {response.text if 'response' in locals() else 'N/A'}")
        raise
    except Exception as e:
        print(f"使用 Gemini 搜索地点失败: {str(e)}")
        import traceback
        traceback.print_exc()
        raise


def download_and_upload_image_from_url(s3_client: Any, image_url: str, place_name: str, image_index: int) -> Optional[str]:
    """
    从 URL 下载图片并上传到 S3
    """
    try:
        if not Config.S3_BUCKET_NAME:
            print(f"错误: S3_BUCKET_NAME 未设置")
            return None
        
        # 下载图片（添加 User-Agent 以避免 403 错误，特别是 Wikimedia Commons）
        response = requests.get(image_url, stream=True, timeout=Config.PLACES_API_TIMEOUT, headers=DEFAULT_HEADERS)
        response.raise_for_status()
        
        # 检查 Content-Type
        content_type = response.headers.get('Content-Type', 'image/jpeg')
        if not content_type.startswith('image/'):
            content_type = 'image/jpeg'
        
        # 生成 S3 key
        image_prefix = Config.S3_IMAGE_PREFIX
        safe_place_name = "".join(c for c in place_name if c.isalnum() or c in (' ', '-', '_')).strip()[:50]
        s3_key = f"{image_prefix}{int(time.time())}_{safe_place_name.replace(' ', '_')}_{image_index}.jpg"
        
        # 流式上传到 S3
        s3_client.upload_fileobj(
            response.raw,
            Config.S3_BUCKET_NAME,
            s3_key,
            ExtraArgs={'ContentType': content_type}
        )
        
        # 生成公共 URL
        s3_url = f"https://{Config.S3_BUCKET_NAME}.s3.{Config.S3_REGION}.amazonaws.com/{s3_key}"
        return s3_url
        
    except Exception as e:
        print(f"下载并上传图片失败 {image_url}: {str(e)}")
        return None


def fetch_data_and_process_images(s3_client: Any, lat: float, lng: float, main_type: str, sub_type: str, budget: str) -> List[Dict[str, Any]]:
    """
    获取地点数据并处理图片上传
    """
    try:
        # 对于名胜古迹和旅游景点以及活动类型，使用 Gemini 搜索
        if main_type in ["名胜古迹和旅游景点", "跳蚤市场或活动"]:
            return fetch_places_via_gemini_and_process_images(s3_client, lat, lng, main_type)
        
        # 对于美食类型，使用 Google Places API
        if not Config.GOOGLE_PLACES_API_KEY:
            raise ValueError("GOOGLE_PLACES_API_KEY 未设置")
        
        # 获取搜索参数
        # 对于「跳蚤市场或活动」，包含时间相关关键词
        include_time_keyword = (main_type == "跳蚤市场或活动")
        keyword = map_chinese_type_to_keyword(main_type, sub_type, include_time_keyword=include_time_keyword)
        radius = get_search_radius(main_type)
        max_results = get_max_results(main_type)
        min_price, max_price = map_budget_to_price_level(budget)
        
        # 调用 Google Places Nearby Search API
        nearby_url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        params = {
            "location": f"{lat},{lng}",
            "radius": radius,
            "keyword": keyword,
            "key": Config.GOOGLE_PLACES_API_KEY,
            "language": Config.PLACES_API_LANGUAGE,
        }
        
        # 添加价格筛选（如果适用）
        # Google Places API 支持 minprice 和 maxprice 参数（0-4）
        if min_price is not None:
            params["minprice"] = min_price
        if max_price is not None:
            params["maxprice"] = max_price
        
        all_places = []
        next_page_token = None
        
        # 分页获取所有结果（先收集，后排序）
        while True:
            if next_page_token:
                params["pagetoken"] = next_page_token
                time.sleep(Config.PLACES_PAGE_TOKEN_DELAY)  # Google API 要求分页请求之间至少间隔 2 秒
            
            response = requests.get(nearby_url, params=params, timeout=Config.PLACES_API_TIMEOUT, headers=DEFAULT_HEADERS)
            response.raise_for_status()
            data = response.json()
            
            if data.get("status") != "OK":
                error_status = data.get('status')
                error_message = data.get('error_message', '无详细信息')
                print(f"Google Places API 错误: {error_status}")
                print(f"错误详情: {error_message}")
                
                # 对于 REQUEST_DENIED，抛出更详细的异常
                if error_status == "REQUEST_DENIED":
                    raise ValueError(
                        f"Google Places API 请求被拒绝。"
                        f"状态: {error_status}, 详情: {error_message}。"
                        f"请检查：1) API Key 是否正确 2) Places API 是否已启用 3) API Key 限制设置"
                    )
                break
            
            results = data.get("results", [])
            all_places.extend(results)
            
            # 检查是否需要继续分页
            # 对于美食类型，收集足够的结果以便后续排序筛选（收集 2-3 页以确保有足够选择）
            # 对于其他类型，不设上限，继续收集直到没有更多结果
            if max_results and len(all_places) >= max_results * 3:
                # 美食类型：收集足够结果后停止，准备排序
                break
            
            next_page_token = data.get("next_page_token")
            if not next_page_token:
                break
        
        # 对美食类型，收集所有结果后按评分排序并只取前 N 个
        # 这样可以确保从所有可能的结果中选择评分最高的
        if main_type == "美食" and all_places:
            all_places = sorted(all_places, key=lambda x: x.get("rating", 0), reverse=True)[:Config.FOOD_MAX_RESULTS]
        
        # 获取每个地点的详细信息（包括 website）
        processed_places = []
        for place in all_places:
            place_id = place.get("place_id")
            if not place_id:
                continue
            
            # 获取地点详情（包括经纬度）
            details_url = "https://maps.googleapis.com/maps/api/place/details/json"
            # 对于活动类型，请求更多字段以便检查时间信息
            # 对于美食类型，请求评论数据以便生成基于评论的概述
            # 所有类型都需要 geometry 字段以获取经纬度
            if main_type == "跳蚤市场或活动":
                fields = "name,rating,formatted_address,photos,website,url,editorial_summary,description,opening_hours,geometry"
            elif main_type == "美食":
                fields = "name,rating,formatted_address,photos,website,url,reviews,geometry"
            else:
                fields = "name,rating,formatted_address,photos,website,url,geometry"
            
            details_params = {
                "place_id": place_id,
                "fields": fields,
                "key": Config.GOOGLE_PLACES_API_KEY,
                "language": Config.PLACES_API_LANGUAGE,
            }
            
            details_response = requests.get(details_url, params=details_params, timeout=Config.PLACES_API_TIMEOUT, headers=DEFAULT_HEADERS)
            details_response.raise_for_status()
            details_data = details_response.json()
            
            if details_data.get("status") != "OK":
                error_status = details_data.get('status')
                error_message = details_data.get('error_message', '无详细信息')
                print(f"Google Places Details API 错误: {error_status}, 详情: {error_message}")
                continue
            
            place_details = details_data.get("result", {})
            
            # 对于「跳蚤市场或活动」，检查是否在未来30天内
            if main_type == "跳蚤市场或活动":
                if not is_event_within_date_range(place_details, Config.EVENT_SEARCH_DAYS_AHEAD):
                    print(f"跳过活动（不在未来{Config.EVENT_SEARCH_DAYS_AHEAD}天内）: {place_details.get('name', '')}")
                    continue
            
            # 提取必要信息
            place_info = {
                "place_id": place_id,
                "name": place_details.get("name", ""),
                "rating": place_details.get("rating", 0),
                "formatted_address": place_details.get("formatted_address", ""),
                "website": place_details.get("website") or place_details.get("url", ""),
                "photo_reference": None,
                "s3_image_url": None,
            }
            
            # 获取经纬度（从 Google Places API 的 geometry 字段）
            geometry = place_details.get("geometry", {})
            location = geometry.get("location", {})
            if location:
                place_info["lat"] = location.get("lat")
                place_info["lng"] = location.get("lng")
            else:
                # 如果没有经纬度，使用搜索中心点坐标
                place_info["lat"] = lat
                place_info["lng"] = lng
                print(f"警告：地点 {place_info['name']} 未提供经纬度，使用搜索中心点坐标")
            
            # 获取第一张照片的 reference
            photos = place_details.get("photos", [])
            if photos:
                place_info["photo_reference"] = photos[0].get("photo_reference")
            
            # 对于美食类型，保存评论数据
            if main_type == "美食":
                reviews = place_details.get("reviews", [])
                # 筛选有文本的评论
                valid_reviews = [review for review in reviews if review.get("text")]
                
                # 按点赞数排序（如果有 likes 字段），否则按评分排序
                # Google Places API 的 reviews 可能包含 likes/thumbs_up 字段
                # 如果没有，则按 rating 排序，评分高的优先
                def get_review_score(review):
                    # 优先使用点赞数（可能字段名：likes, thumbs_up, helpful_votes）
                    likes = review.get("likes") or review.get("thumbs_up") or review.get("helpful_votes") or 0
                    # 如果没有点赞数，使用评分作为次要排序依据
                    rating = review.get("rating", 0)
                    # 返回一个元组，点赞数优先，评分其次
                    return (likes, rating)
                
                # 按分数降序排序（点赞数高的在前）
                sorted_reviews = sorted(valid_reviews, key=get_review_score, reverse=True)
                
                # 选择点赞数最高的评论（最多取前5条）
                top_reviews = sorted_reviews[:5]
                review_texts = [review.get("text", "") for review in top_reviews]
                place_info["reviews"] = review_texts
            
            processed_places.append(place_info)
        
        # 并发上传图片到 S3
        with ThreadPoolExecutor(max_workers=Config.MAX_CONCURRENT_IMAGE_UPLOADS) as executor:
            future_to_place = {}
            for place in processed_places:
                if place["photo_reference"]:
                    future = executor.submit(
                        upload_image_to_s3,
                        s3_client,
                        place["photo_reference"],
                        place["name"]
                    )
                    future_to_place[future] = place
            
            # 收集结果
            for future in as_completed(future_to_place):
                place = future_to_place[future]
                try:
                    s3_url = future.result()
                    place["s3_image_url"] = s3_url
                except Exception as e:
                    print(f"处理图片失败 {place['name']}: {str(e)}")
                    place["s3_image_url"] = None
        
        return processed_places
        
    except Exception as e:
        print(f"获取和处理数据失败: {str(e)}")
        raise


def search_images_via_custom_search(place_name: str, place_address: str = "") -> List[str]:
    """
    使用 Google Custom Search API 搜索地点图片
    """
    try:
        # 如果没有设置 Custom Search Engine ID，使用 API Key 方式
        # 注意：需要启用 Custom Search API 并使用 API Key
        if not Config.GOOGLE_CUSTOM_SEARCH_ENGINE_ID:
            # 如果没有 CX，尝试使用 Google Places API Key（如果支持）
            # 或者返回空列表
            print(f"警告: GOOGLE_CUSTOM_SEARCH_ENGINE_ID 未设置，无法使用 Custom Search API")
            return []
        
        # 构建搜索查询
        query = place_name
        if place_address:
            query = f"{place_name} {place_address}"
        
        # Google Custom Search API 端点
        search_url = "https://www.googleapis.com/customsearch/v1"
        
        params = {
            "key": Config.GOOGLE_PLACES_API_KEY,  # 使用 Google Places API Key（需要启用 Custom Search API）
            "cx": Config.GOOGLE_CUSTOM_SEARCH_ENGINE_ID,  # Custom Search Engine ID
            "q": query,
            "searchType": "image",  # 只搜索图片
            "num": Config.ATTRACTION_IMAGE_COUNT,  # 最多返回3张图片
            "safe": "active",  # 安全搜索
        }
        
        response = requests.get(search_url, params=params, timeout=Config.PLACES_API_TIMEOUT, headers=DEFAULT_HEADERS)
        response.raise_for_status()
        data = response.json()
        
        # 提取图片 URL
        image_urls = []
        items = data.get("items", [])
        for item in items[:Config.ATTRACTION_IMAGE_COUNT]:
            image_url = item.get("link")
            if image_url:
                image_urls.append(image_url)
        
        return image_urls
        
    except Exception as e:
        print(f"使用 Custom Search API 搜索图片失败 {place_name}: {str(e)}")
        return []


def is_flea_market(place_name: str, place_summary: str = "") -> bool:
    """
    判断一个地点是否是跳蚤市场
    """
    # 跳蚤市场相关关键词（中文和日文）
    flea_market_keywords = [
        "跳蚤市场", "フリーマーケット", "フリマ", "蚤の市", "古物市場",
        "flea market", "flea", "market", "古着", "中古", "リサイクル"
    ]
    
    text_to_check = f"{place_name} {place_summary}".lower()
    
    for keyword in flea_market_keywords:
        if keyword.lower() in text_to_check:
            return True
    
    return False


def geocode_address(address: str) -> Optional[Tuple[float, float]]:
    """
    使用 Google Geocoding API 根据地址获取经纬度
    """
    try:
        if not Config.GOOGLE_PLACES_API_KEY:
            return None
        
        if not address:
            return None
        
        # Google Geocoding API 端点
        geocode_url = "https://maps.googleapis.com/maps/api/geocode/json"
        
        params = {
            "address": address,
            "key": Config.GOOGLE_PLACES_API_KEY,
            "language": Config.PLACES_API_LANGUAGE,
        }
        
        response = requests.get(geocode_url, params=params, timeout=Config.PLACES_API_TIMEOUT, headers=DEFAULT_HEADERS)
        response.raise_for_status()
        data = response.json()
        
        if data.get("status") != "OK":
            print(f"Geocoding API 错误: {data.get('status')} for address: {address}")
            return None
        
        results = data.get("results", [])
        if not results:
            return None
        
        # 获取第一个结果的经纬度
        location = results[0].get("geometry", {}).get("location", {})
        if location:
            lat = location.get("lat")
            lng = location.get("lng")
            if lat is not None and lng is not None:
                return (float(lat), float(lng))
        
        return None
        
    except Exception as e:
        print(f"Geocoding 地址失败 {address}: {str(e)}")
        return None


def find_place_by_name_and_address(name: str, address: str, lat: float, lng: float) -> Optional[str]:
    """
    使用 Google Places API 根据名称和地址查找 place_id
    """
    try:
        if not Config.GOOGLE_PLACES_API_KEY:
            return None
        
        # 使用 Text Search API 搜索地点
        text_search_url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
        
        # 构建搜索查询：优先使用名称，如果地址存在也加入
        query = name
        if address:
            query = f"{name} {address}"
        
        params = {
            "query": query,
            "location": f"{lat},{lng}",
            "radius": 5000,  # 5公里范围内
            "key": Config.GOOGLE_PLACES_API_KEY,
            "language": Config.PLACES_API_LANGUAGE,
        }
        
        response = requests.get(text_search_url, params=params, timeout=Config.PLACES_API_TIMEOUT, headers=DEFAULT_HEADERS)
        response.raise_for_status()
        data = response.json()
        
        if data.get("status") != "OK":
            return None
        
        results = data.get("results", [])
        if not results:
            return None
        
        # 返回第一个结果的 place_id（通常最相关）
        return results[0].get("place_id")
        
    except Exception as e:
        print(f"查找地点失败 {name}: {str(e)}")
        return None


def fetch_places_via_gemini_and_process_images(s3_client: Any, lat: float, lng: float, main_type: str) -> List[Dict[str, Any]]:
    """
    使用 Gemini AI 搜索地点，然后通过 Google Places API 获取图片
    """
    try:
        if not Config.GOOGLE_PLACES_API_KEY:
            raise ValueError("GOOGLE_PLACES_API_KEY 未设置")
        
        # 使用 Gemini 搜索地点（只获取名称、地址、概述）
        places = fetch_places_via_gemini(lat, lng, main_type)
        
        if not places:
            return []
        
        # 对于"跳蚤市场或活动"类型，先排序（跳蚤市场优先），然后限制结果数量
        if main_type == "跳蚤市场或活动":
            # 为每个地点添加是否是跳蚤市场的标记
            for place in places:
                place["is_flea_market"] = is_flea_market(place.get("name", ""), place.get("summary", ""))
            
            # 排序：跳蚤市场优先（True 在前），然后按名称排序
            places.sort(key=lambda x: (not x.get("is_flea_market", False), x.get("name", "")))
            
            # 限制结果数量
            places = places[:Config.MARKET_MAX_RESULTS]
            
            # 如果结果少于预期，记录警告
            if len(places) < Config.MARKET_MAX_RESULTS:
                print(f"警告：跳蚤市场或活动只返回了 {len(places)} 个结果，少于预期的 {Config.MARKET_MAX_RESULTS} 个")
        
        # 对于"名胜古迹和旅游景点"类型，限制结果数量
        elif main_type == "名胜古迹和旅游景点":
            # 限制结果数量
            places = places[:Config.ATTRACTION_MAX_RESULTS]
            
            # 如果结果少于预期，记录警告
            if len(places) < Config.ATTRACTION_MAX_RESULTS:
                print(f"警告：名胜古迹和旅游景点只返回了 {len(places)} 个结果，少于预期的 {Config.ATTRACTION_MAX_RESULTS} 个")
        
        processed_places = []
        
        # 对于"跳蚤市场或活动"类型，直接使用 Gemini 返回的数据，不需要调用 Google Places API
        if main_type == "跳蚤市场或活动":
            for place in places:
                place_info = {
                    "place_id": f"gemini_{int(time.time())}_{place['name'][:20]}",
                "name": place["name"],
                    "formatted_address": place["formatted_address"],
                    "summary": place.get("summary", ""),
                    "rating": 0,
                    "website": place.get("website", ""),  # 使用 Gemini 返回的 website
                    "s3_image_urls": [],  # 跳蚤市场或活动不需要图片
                }
                # 添加经纬度信息
                # 优先使用 Gemini 返回的经纬度
                if place.get("lat") is not None and place.get("lng") is not None:
                    place_info["lat"] = float(place.get("lat"))
                    place_info["lng"] = float(place.get("lng"))
                elif "lat" in place and "lng" in place:
                    # 如果已经处理过（从 fetch_places_via_gemini 返回的数据中）
                    place_info["lat"] = place.get("lat")
                    place_info["lng"] = place.get("lng")
                else:
                    # 如果 Gemini 没有提供经纬度，通过地址获取
                    address = place.get("formatted_address", "")
                    if address:
                        print(f"通过地址获取经纬度: {place.get('name', '')} - {address}")
                        coordinates = geocode_address(address)
                        if coordinates:
                            place_info["lat"] = coordinates[0]
                            place_info["lng"] = coordinates[1]
                            print(f"成功获取经纬度: ({coordinates[0]}, {coordinates[1]})")
                        else:
                            # 如果 Geocoding 也失败，使用搜索中心点的经纬度作为默认值
                            place_info["lat"] = lat
                            place_info["lng"] = lng
                            print(f"警告：无法通过地址获取经纬度，使用搜索中心点坐标: ({lat}, {lng})")
                    else:
                        # 如果没有地址，使用搜索中心点的经纬度作为默认值
                        place_info["lat"] = lat
                        place_info["lng"] = lng
                        print(f"警告：地点 {place.get('name', '')} 没有地址，使用搜索中心点坐标")
                processed_places.append(place_info)
        else:
            # 对于"名胜古迹和旅游景点"类型，通过 Google Places API 查找并获取图片
            for place in places:
                place_name = place["name"]
                place_address = place["formatted_address"]
                
                # 使用 Google Places API 查找 place_id
                place_id = find_place_by_name_and_address(place_name, place_address, lat, lng)
                
                if not place_id:
                    print(f"未找到 Google Places 数据: {place_name}")
                    # 即使找不到 Google Places 数据，也保留 Gemini 返回的信息
                    place_info = {
                        "place_id": f"gemini_{int(time.time())}_{place_name[:20]}",
                        "name": place_name,
                        "formatted_address": place_address,
                        "summary": place.get("summary", ""),
                        "rating": 0,
                        "website": place.get("website", ""),
                        "s3_image_urls": [],  # 没有图片
                    }
                    # 如果没有找到 Google Places 数据，使用搜索中心点坐标
                    place_info["lat"] = lat
                    place_info["lng"] = lng
                    processed_places.append(place_info)
                    continue
                
                # 获取地点详情（包括图片和经纬度）
                details_url = "https://maps.googleapis.com/maps/api/place/details/json"
                fields = "name,rating,formatted_address,photos,website,url,geometry"
                
                details_params = {
                    "place_id": place_id,
                    "fields": fields,
                    "key": Config.GOOGLE_PLACES_API_KEY,
                    "language": Config.PLACES_API_LANGUAGE,
                }
                
                details_response = requests.get(details_url, params=details_params, timeout=Config.PLACES_API_TIMEOUT, headers=DEFAULT_HEADERS)
                details_response.raise_for_status()
                details_data = details_response.json()
                
                if details_data.get("status") != "OK":
                    print(f"获取地点详情失败: {place_name}")
                    continue
                
                place_details = details_data.get("result", {})
                
                # 构建地点信息（使用 Google Places API 的数据，但保留 Gemini 的 summary）
                place_info = {
                    "place_id": place_id,
                    "name": place_details.get("name", place_name),  # 优先使用 Google Places 的名称
                    "formatted_address": place_details.get("formatted_address", place_address),
                    "summary": place.get("summary", ""),  # 使用 Gemini 生成的概述
                    "rating": place_details.get("rating", 0),
                    "website": place_details.get("website") or place_details.get("url", ""),
                    "s3_image_urls": [],  # 存储多张图片URL
                }
                
                # 获取经纬度（从 Google Places API 的 geometry 字段）
                geometry = place_details.get("geometry", {})
                location = geometry.get("location", {})
                if location:
                    place_info["lat"] = location.get("lat")
                    place_info["lng"] = location.get("lng")
                else:
                    # 如果没有经纬度，使用搜索中心点坐标
                    place_info["lat"] = lat
                    place_info["lng"] = lng
                    print(f"警告：地点 {place_info['name']} 未提供经纬度，使用搜索中心点坐标")
                
                # 获取图片（最多3张）
                photos = place_details.get("photos", [])
                if photos:
                    # 使用 Google Places API 的图片（并发上传到 S3）
                    with ThreadPoolExecutor(max_workers=Config.MAX_CONCURRENT_IMAGE_UPLOADS) as executor:
                        future_to_index = {}
                        for idx, photo in enumerate(photos[:Config.ATTRACTION_IMAGE_COUNT]):
                            photo_reference = photo.get("photo_reference")
                            if photo_reference:
                                future = executor.submit(
                                    upload_image_to_s3,
                                    s3_client,
                                    photo_reference,
                                    f"{place_info['name']}_{idx}"
                                )
                                future_to_index[future] = idx
                        
                        # 收集结果（保持顺序）
                        s3_urls = [None] * min(len(photos), Config.ATTRACTION_IMAGE_COUNT)
                        for future in as_completed(future_to_index):
                            idx = future_to_index[future]
                            try:
                                s3_url = future.result()
                                if s3_url:
                                    s3_urls[idx] = s3_url
                            except Exception as e:
                                print(f"处理图片失败 {place_info['name']} 第{idx+1}张: {str(e)}")
                        
                        # 过滤掉 None 值
                        place_info["s3_image_urls"] = [url for url in s3_urls if url]
                
                processed_places.append(place_info)
        
        return processed_places
        
    except Exception as e:
        print(f"使用 Gemini 搜索并处理图片失败: {str(e)}")
        raise


def get_icon_type(main_type: str) -> str:
    """根据主类型返回图标类型"""
    icon_mapping = {
        "美食": "food",
        "名胜古迹和旅游景点": "attraction",
        "跳蚤市场或活动": "activity",
    }
    return icon_mapping.get(main_type, "poi")


def get_tags_for_type(main_type: str, sub_type: str = "") -> List[str]:
    """根据类型生成标签"""
    tags = []
    
    if main_type == "美食":
        tags.append("food")
        if sub_type:
            sub_type_tags = {
                "异国料理": ["international", "restaurant"],
                "拉面": ["ramen", "noodles"],
                "烤肉": ["yakiniku", "bbq"],
                "寿喜烧": ["sukiyaki", "hotpot"],
                "中华": ["chinese", "restaurant"],
                "海鲜": ["seafood", "restaurant"],
                "居酒屋": ["izakaya", "bar"],
            }
            tags.extend(sub_type_tags.get(sub_type, []))
    elif main_type == "名胜古迹和旅游景点":
        tags.extend(["attraction", "sightseeing", "tourism"])
    elif main_type == "跳蚤市场或活动":
        tags.extend(["activity", "event", "market"])
    
    return tags


def generate_marker_id(index: int) -> str:
    """生成标记ID"""
    return f"mk_{index + 1:02d}"


def generate_content_id(index: int) -> str:
    """生成内容ID"""
    return f"post_{index + 1:02d}"


def format_places_to_markers(processed_places: List[Dict[str, Any]], main_type: str, sub_type: str = "") -> Dict[str, Any]:
    """
    将地点列表格式化为新的 markers 格式
    """
    import uuid
    from datetime import datetime, timezone
    
    # 生成请求元数据
    request_id = f"req_{uuid.uuid4().hex[:8]}"
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    current_timestamp = int(time.time() * 1000)  # 毫秒时间戳
    
    markers = []
    
    for idx, place in enumerate(processed_places):
        name = place["name"]
        address = place["formatted_address"]
        summary = place.get("summary", "暂无概要")
        website = place.get("website", "")
        place_lat = place.get("lat")
        place_lng = place.get("lng")
        
        # 获取图片URL
        image_urls = []
        if main_type == "名胜古迹和旅游景点":
            image_urls = place.get("s3_image_urls", [])
        elif main_type == "美食":
            s3_url = place.get("s3_image_url")
            if s3_url:
                image_urls = [s3_url]
        # 跳蚤市场或活动不需要图片
        
        # 头图（第一张图片，如果没有则为空字符串）
        header_image = image_urls[0] if image_urls else ""
        
        # 构建该地点的 Editor.js blocks
        place_blocks = []
        
        # Header 块（地点名称）
        place_blocks.append({
            "type": "header",
            "data": {
                "text": name,
                "level": 2
            }
        })
        
        # 添加所有图片块（仅对需要图片的类型）
        for img_idx, image_url in enumerate(image_urls):
            if image_url:
                place_blocks.append({
                    "type": "image",
                    "data": {
                        "file": {
                            "url": image_url
                        },
                        "caption": f"{name} - {address}" + (f" (图{img_idx+1})" if len(image_urls) > 1 else ""),
                        "withBorder": True
                    }
                })
        
        # Paragraph 块（概要）
        place_blocks.append({
            "type": "paragraph",
            "data": {
                "text": f"【概要】{summary}"
            }
        })
        
        # Paragraph 块（信息来源）
        if website:
            place_blocks.append({
                "type": "paragraph",
                "data": {
                    "text": f"信息来源：[点击跳转原链接]({website})"
                }
            })
        
        # 生成 marker ID 和 content ID
        marker_id = generate_marker_id(idx)
        content_id = generate_content_id(idx)
        
        # 计算相关性分数（基于评分和位置，评分高的分数高）
        rating = place.get("rating", 0)
        # 归一化评分到 0-1 范围（Google Places 评分是 0-5）
        relevance_score = round(0.5 + (rating / 10) if rating else 0.5, 2)
        # 按顺序递减（第一个结果最相关）
        relevance_score = round(max(0.1, relevance_score - (idx * 0.05)), 2)
        
        # 构建 marker 对象
        marker = {
            "id": marker_id,
            "coordinates": {
                "latitude": place_lat if place_lat is not None else 0,
                "longitude": place_lng if place_lng is not None else 0
            },
            "content": {
                "id": content_id,
                "title": name,
                "headerImage": header_image,
                "iconType": get_icon_type(main_type),
                "editorData": {
                    "time": current_timestamp,
                    "blocks": place_blocks,
                    "version": "2.29.0"
                },
                "createdAt": generated_at,
                "updatedAt": generated_at
            },
            "relevanceScore": relevance_score,
            "tags": get_tags_for_type(main_type, sub_type),
            "actions": {
                "deeplink": f"mapannai://marker/{marker_id}"
            }
        }
        
        markers.append(marker)
    
    # 返回完整的响应结构
    return {
        "requestId": request_id,
        "generatedAt": generated_at,
        "ttlSeconds": 300,
        "markers": markers
    }


def format_places_to_editorjs(processed_places: List[Dict[str, Any]], main_type: str) -> Dict[str, Any]:
    """
    将地点列表格式化为 Editor.js 格式（保留兼容性）
    """
    blocks = []
    
    # 添加通用标题块
    type_titles = {
        "美食": "美食推荐",
        "名胜古迹和旅游景点": "名胜古迹和旅游景点推荐",
        "跳蚤市场或活动": "活动推荐",
    }
    title = type_titles.get(main_type, "推荐")
    blocks.append({
        "type": "header",
        "data": {
            "text": title,
            "level": 1
        }
    })
    
    # 为每个地点添加块
    for place in processed_places:
        name = place["name"]
        address = place["formatted_address"]
        summary = place.get("summary", "暂无概要")
        website = place.get("website", "")
        
        # Header 块
        blocks.append({
            "type": "header",
            "data": {
                "text": name,
                "level": 2
            }
        })
        
        # 处理图片（支持多张图片）
        # 对于名胜古迹和旅游景点，使用 s3_image_urls（数组）
        # 对于美食类型，使用 s3_image_url（单个）
        # 跳蚤市场或活动不需要图片
        image_urls = []
        if main_type == "名胜古迹和旅游景点":
            image_urls = place.get("s3_image_urls", [])
        elif main_type == "美食":
            s3_url = place.get("s3_image_url")
            if s3_url:
                image_urls = [s3_url]
        # 跳蚤市场或活动不显示图片
        
        # 添加所有图片块（仅对需要图片的类型）
        for idx, image_url in enumerate(image_urls):
            if image_url:
                blocks.append({
                    "type": "image",
                    "data": {
                        "file": {
                            "url": image_url
                        },
                        "caption": f"{name} - {address}" + (f" (图{idx+1})" if len(image_urls) > 1 else ""),
                        "withBorder": True
                    }
                })
        
        # Paragraph 块（概要）
        blocks.append({
            "type": "paragraph",
            "data": {
                "text": f"【概要】{summary}"
            }
        })
        
        # Paragraph 块（信息来源）
        if website:
            blocks.append({
                "type": "paragraph",
                "data": {
                    "text": f"信息来源：[点击跳转原链接]({website})"
                }
            })
        
        # Paragraph 块（经纬度信息）
        place_lat = place.get("lat")
        place_lng = place.get("lng")
        if place_lat is not None and place_lng is not None:
            blocks.append({
                "type": "paragraph",
                "data": {
                    "text": f"📍 坐标：纬度 {place_lat:.6f}，经度 {place_lng:.6f}"
                }
            })
    
    # 返回所有块（在循环外）
    return {
        "time": int(time.time()),
        "blocks": blocks
    }


def generate_content_and_format(processed_places: List[Dict[str, Any]], main_type: str, sub_type: str = "") -> Dict[str, Any]:
    """
    生成内容并格式化为新的 markers 格式
    """
    try:
        # 对于名胜古迹和旅游景点以及活动类型，summary 已经由 Gemini 生成，直接使用
        if main_type in ["名胜古迹和旅游景点", "跳蚤市场或活动"]:
            return format_places_to_markers(processed_places, main_type, sub_type)
        
        # 对于美食类型，需要使用 Gemini 基于评论生成 summary
        is_food_type = (main_type == "美食")
        summary_length = Config.FOOD_SUMMARY_MAX_LENGTH if is_food_type else Config.SUMMARY_MAX_LENGTH
        
        # 准备 Gemini 输入数据
        places_data = []
        for place in processed_places:
            place_info = {
                "place_id": place["place_id"],
                "name": place["name"],
                "address": place["formatted_address"],
                "rating": place["rating"],
            }
            
            # 对于美食类型，添加评论数据
            if is_food_type and place.get("reviews"):
                place_info["reviews"] = place["reviews"]
            
            places_data.append(place_info)
        
        # 构建 Gemini Prompt
        if is_food_type:
            system_instruction = f"""你是一个专业的日本美食评论家。
请根据每个餐厅的高质量用户评论（已按点赞数排序，选择最受欢迎的评论），生成一个{summary_length}字以内的中文概述。
概述应该：
1. 总结这些高点赞评论中的主要观点和评价
2. 突出餐厅的特色、口味、服务等亮点
3. 重点反映大多数用户认可的特点
4. 使用自然流畅的中文表达
5. 不要使用任何 Markdown 格式
最终返回一个可解析的 JSON 数组，数组中每个对象包含 place_id 和 summary_text（即{summary_length}字概述）。"""
            
            user_prompt = f"""请根据以下餐厅的高质量用户评论（已按点赞数排序），为每个餐厅生成概述（每个{summary_length}字以内，中文）：
{json.dumps(places_data, ensure_ascii=False, indent=2)}

要求：
- 这些评论已经按点赞数排序，代表了最受用户认可的评价
- 仔细阅读每条评论，提取关键信息
- 总结评论中提到的菜品特色、口味、服务、环境等
- 重点突出大多数用户都认可的特点
- 用自然流畅的中文表达，让读者了解这家餐厅的特点

返回格式示例：
[
  {{"place_id": "xxx", "summary_text": "这是基于高点赞评论生成的{summary_length}字以内的中文概述"}},
  {{"place_id": "yyy", "summary_text": "这是另一个餐厅的概述"}}
]"""
        else:
            system_instruction = f"""你是一个专业的日本旅游向导。
请为每个地点生成一个{summary_length}字以内的中文概要。
请不要使用任何 Markdown 格式。
最终返回一个可解析的 JSON 数组，数组中每个对象包含 place_id 和 summary_text（即{summary_length}字概要）。"""
            
            user_prompt = f"""请为以下地点生成推荐概要（每个{summary_length}字以内，中文）：
{json.dumps(places_data, ensure_ascii=False, indent=2)}

返回格式示例：
[
  {{"place_id": "xxx", "summary_text": "这是{summary_length}字以内的中文概要"}},
  {{"place_id": "yyy", "summary_text": "这是另一个地点的概要"}}
]"""
        
        # 调用 Gemini API
        model = genai.GenerativeModel(
            model_name=Config.GEMINI_MODEL_NAME,
            system_instruction=system_instruction
        )
        
        response = model.generate_content(user_prompt)
        response_text = response.text.strip()
        
        # 清理响应文本（移除可能的 markdown 代码块标记）
        if response_text.startswith("```json"):
            response_text = response_text[7:]
        if response_text.startswith("```"):
            response_text = response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]
        response_text = response_text.strip()
        
        # 解析 JSON
        summaries = json.loads(response_text)
        summaries_dict = {item["place_id"]: item["summary_text"] for item in summaries}
        
        # 为美食类型添加 summary 到 processed_places
        for place in processed_places:
            place_id = place["place_id"]
            place["summary"] = summaries_dict.get(place_id, "暂无概要")
        
        # 使用新的 markers 格式化函数
        return format_places_to_markers(processed_places, main_type, sub_type)
        
    except Exception as e:
        print(f"生成内容和格式化失败: {str(e)}")
        raise


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AWS Lambda 主处理函数
    """
    try:
        # 验证配置
        is_valid, error_message = Config.validate()
        if not is_valid:
            return {
                "statusCode": 500,
                "headers": get_cors_headers(),
                "body": json.dumps({
                    "error": error_message
                }, ensure_ascii=False)
            }
        
        # 在配置验证后初始化客户端
        s3_client = boto3.client('s3', region_name=Config.S3_REGION)
        genai.configure(api_key=Config.GEMINI_API_KEY)
        
        # 解析请求体
        if isinstance(event.get("body"), str):
            body = json.loads(event["body"])
        else:
            body = event.get("body", {})
        
        # 提取参数
        lat = float(body.get("lat", 0))
        lng = float(body.get("lng", 0))
        main_type = body.get("main_type", "")
        sub_type = body.get("sub_type", "")
        budget = body.get("budget", "")
        
        # 参数验证
        if not all([lat, lng, main_type]):
            return {
                "statusCode": 400,
                "headers": get_cors_headers(),
                "body": json.dumps({
                    "error": "缺少必需参数: lat, lng, main_type"
                }, ensure_ascii=False)
            }
        
        # 获取数据并处理图片
        processed_places = fetch_data_and_process_images(s3_client, lat, lng, main_type, sub_type, budget)
        
        if not processed_places:
            import uuid
            from datetime import datetime, timezone
            return {
                "statusCode": 200,
                "headers": get_cors_headers(),
                "body": json.dumps({
                    "requestId": f"req_{uuid.uuid4().hex[:8]}",
                    "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "ttlSeconds": 300,
                    "markers": []
                }, ensure_ascii=False)
            }
        
        # 生成内容并格式化（传递 sub_type 用于生成标签）
        result = generate_content_and_format(processed_places, main_type, sub_type)
        
        # 返回成功响应
        return {
            "statusCode": 200,
            "headers": get_cors_headers(),
            "body": json.dumps(result, ensure_ascii=False)
        }
        
    except json.JSONDecodeError as e:
        return {
            "statusCode": 400,
            "headers": get_cors_headers(),
            "body": json.dumps({
                "error": f"JSON 解析错误: {str(e)}"
            }, ensure_ascii=False)
        }
        
    except Exception as e:
        print(f"Lambda 函数执行错误: {str(e)}")
        import traceback
        traceback.print_exc()
        
        return {
            "statusCode": 500,
            "headers": get_cors_headers(),
            "body": json.dumps({
                "error": f"服务器内部错误: {str(e)}"
            }, ensure_ascii=False)
        }

