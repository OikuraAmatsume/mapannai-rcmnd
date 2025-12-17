#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
配置文件 - 统一管理所有环境变量和 API 密钥
请在部署到 AWS Lambda 前，将实际值配置到 Lambda 环境变量中
本地测试时，可以直接修改此文件或设置环境变量
"""

import os
from typing import Optional, Tuple


class Config:
    """配置类 - 统一管理所有配置项"""
    
    # ==================== Google API 配置 ====================
    # Google Places API 密钥
    # 获取方式: https://console.cloud.google.com/apis/credentials
    GOOGLE_PLACES_API_KEY: str = os.environ.get('GOOGLE_PLACES_API_KEY', '')
    
    # Google Gemini API 密钥
    # 获取方式: https://makersuite.google.com/app/apikey
    GEMINI_API_KEY: str = os.environ.get('GEMINI_API_KEY', '')
    
    # Google Custom Search API 配置
    # 客户端 ID（用于 OAuth 2.0 认证，如果需要）
    GOOGLE_CUSTOM_SEARCH_CLIENT_ID: str = os.environ.get('GOOGLE_CUSTOM_SEARCH_CLIENT_ID', '')
    
    # 客户端密钥（用于 OAuth 2.0 认证，如果需要）
    GOOGLE_CUSTOM_SEARCH_CLIENT_SECRET: str = os.environ.get('GOOGLE_CUSTOM_SEARCH_CLIENT_SECRET', '')
    
    # Custom Search Engine ID (CX) - 需要创建自定义搜索引擎
    # 获取方式: https://programmablesearchengine.google.com/controlpanel/create
    # 注意：如果没有设置，将使用 API Key 方式（需要启用 Custom Search API）
    GOOGLE_CUSTOM_SEARCH_ENGINE_ID: str = os.environ.get('GOOGLE_CUSTOM_SEARCH_ENGINE_ID', '')
    
    # Gemini 模型名称
    # 注意：必须使用完整模型名称格式 "models/gemini-1.5-flash" 或 "gemini-1.5-flash-002"
    # 可用模型: models/gemini-1.5-flash, models/gemini-1.5-pro, models/gemini-pro
    # 注意：gemini-2.5-flash 可能不存在，如果报错请改回 models/gemini-1.5-flash
    GEMINI_MODEL_NAME: str = 'models/gemini-2.5-flash'
    
    # ==================== AWS S3 配置 ====================
    # S3 存储桶名称
    # 示例: 'my-poi-images-bucket'
    S3_BUCKET_NAME: str = os.environ.get('S3_BUCKET_NAME', '')
    
    # S3 区域
    S3_REGION: str = os.environ.get('S3_REGION', '')
    
    # S3 图片存储路径前缀
    # 重要：所有上传的图片必须使用 'poi-images/' 前缀
    # S3 服务将自动检查带有此前缀的对象，并在创建 24 小时后将其删除
    S3_IMAGE_PREFIX: str = 'poi-images/'
    
    # S3 异步任务结果存储路径前缀
    # 重要：所有任务结果必须使用 'rcmnd_job/' 前缀
    # S3 服务将自动检查带有此前缀的对象，并在创建 48 小时后将其删除
    S3_JOB_RESULT_PREFIX: str = 'rcmnd_job/'
    
    # ==================== Google Places API 配置 ====================
    # API 请求语言
    PLACES_API_LANGUAGE: str = 'zh-CN'
    
    # API 请求超时时间（秒）
    PLACES_API_TIMEOUT: int = 10
    
    # 分页请求间隔时间（秒，Google API 要求至少 2 秒）
    PLACES_PAGE_TOKEN_DELAY: int = 2
    
    # ==================== 图片处理配置 ====================
    # 图片最大宽度（像素）
    IMAGE_MAX_WIDTH: int = 800
    
    # 并发上传图片的最大线程数
    MAX_CONCURRENT_IMAGE_UPLOADS: int = 5
    
    # ==================== 搜索策略配置 ====================
    # 美食搜索半径（米）
    FOOD_SEARCH_RADIUS: int = 500
    
    # 美食最大结果数
    FOOD_MAX_RESULTS: int = 5
    
    # 名胜古迹和旅游景点搜索半径（米）
    ATTRACTION_SEARCH_RADIUS: int = 5000
    
    # 名胜古迹和旅游景点最大结果数
    ATTRACTION_MAX_RESULTS: int = 5
    
    # 跳蚤市场/活动搜索半径（米）
    MARKET_SEARCH_RADIUS: int = 5000
    
    # 跳蚤市场或活动最大结果数
    MARKET_MAX_RESULTS: int = 5
    
    # ==================== Gemini AI 配置 ====================
    # 概要最大字数（美食类型使用 FOOD_SUMMARY_MAX_LENGTH，其他类型使用此值）
    SUMMARY_MAX_LENGTH: int = 150
    
    # 美食类型概要最大字数（基于评论生成）
    FOOD_SUMMARY_MAX_LENGTH: int = 100
    
    # 名胜古迹和旅游景点/活动类型概要最大字数（基于历史意义生成）
    ATTRACTION_SUMMARY_MAX_LENGTH: int = 200
    
    # 每个地点获取的图片数量（名胜古迹和旅游景点以及活动类型）
    ATTRACTION_IMAGE_COUNT: int = 3
    
    # ==================== 活动/市场时间筛选配置 ====================
    # 活动搜索的未来天数范围
    EVENT_SEARCH_DAYS_AHEAD: int = 30
    
    # ==================== 验证配置 ====================
    @staticmethod
    def validate() -> Tuple[bool, Optional[str]]:
        """
        验证必需的配置项是否已设置
        
        Returns:
            (is_valid, error_message)
        """
        required_configs = {
            'GOOGLE_PLACES_API_KEY': Config.GOOGLE_PLACES_API_KEY,
            'GEMINI_API_KEY': Config.GEMINI_API_KEY,
            'S3_BUCKET_NAME': Config.S3_BUCKET_NAME,
        }
        
        missing = [key for key, value in required_configs.items() if not value]
        
        if missing:
            return False, f"缺少必需的配置项: {', '.join(missing)}"
        
        return True, None


# ==================== 本地测试配置（可选）====================
# 如果需要在本地测试，可以取消下面的注释并填入实际值
# 注意：不要将包含真实密钥的文件提交到版本控制系统！

# Config.GOOGLE_PLACES_API_KEY = 'your-google-places-api-key-here'
# Config.GEMINI_API_KEY = 'your-gemini-api-key-here'
# Config.S3_BUCKET_NAME = 'your-s3-bucket-name'
# Config.S3_REGION = 'ap-northeast-1'

