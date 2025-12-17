#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Lambda B (执行器): recommendation_executor
"""

import json
import time
import boto3
from datetime import datetime, timezone
from typing import Dict, Any

# 导入现有的推荐生成逻辑
from recommendation_generator import (
    fetch_data_and_process_images,
    generate_content_and_format,
    get_cors_headers
)
from config import Config
import google.generativeai as genai

# S3 结果存储路径前缀（从 Config 获取）
# 与 poi-images/ 同级，文件会在 48 小时后自动删除


def save_result_to_s3(s3_client: Any, job_id: str, result: Dict[str, Any], status: str = "completed") -> str:
    """
    将结果保存到 S3
    """
    # 构建完整的结果对象
    result_object = {
        "jobId": job_id,
        "status": status,
        "completedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "result": result
    }
    
    # S3 Key: rcmnd_job/{job_id}.json
    s3_key = f"{Config.S3_JOB_RESULT_PREFIX}{job_id}.json"
    
    # 上传到 S3
    s3_client.put_object(
        Bucket=Config.S3_BUCKET_NAME,
        Key=s3_key,
        Body=json.dumps(result_object, ensure_ascii=False),
        ContentType='application/json'
    )
    
    print(f"结果已保存到 S3: s3://{Config.S3_BUCKET_NAME}/{s3_key}")
    return s3_key


def save_error_to_s3(s3_client: Any, job_id: str, error_message: str) -> str:
    """
    将错误信息保存到 S3
    """
    error_object = {
        "jobId": job_id,
        "status": "failed",
        "completedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "error": error_message
    }
    
    s3_key = f"{Config.S3_JOB_RESULT_PREFIX}{job_id}.json"
    
    s3_client.put_object(
        Bucket=Config.S3_BUCKET_NAME,
        Key=s3_key,
        Body=json.dumps(error_object, ensure_ascii=False),
        ContentType='application/json'
    )
    
    print(f"错误信息已保存到 S3: s3://{Config.S3_BUCKET_NAME}/{s3_key}")
    return s3_key


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda B 主处理函数 - 执行器
    """
    job_id = event.get("job_id", "unknown")
    s3_client = None
    
    try:
        print(f"开始处理任务: {job_id}")
        start_time = time.time()
        
        # 验证配置
        is_valid, error_message = Config.validate()
        if not is_valid:
            raise ValueError(error_message)
        
        # 初始化客户端
        s3_client = boto3.client('s3', region_name=Config.S3_REGION)
        genai.configure(api_key=Config.GEMINI_API_KEY)
        
        # 提取参数
        lat = float(event.get("lat", 0))
        lng = float(event.get("lng", 0))
        main_type = event.get("main_type", "")
        sub_type = event.get("sub_type", "")
        budget = event.get("budget", "")
        
        # 参数验证
        if not all([lat, lng, main_type]):
            raise ValueError("缺少必需参数: lat, lng, main_type")
        
        # 获取数据并处理图片（耗时操作）
        print(f"开始获取数据: lat={lat}, lng={lng}, main_type={main_type}")
        processed_places = fetch_data_and_process_images(s3_client, lat, lng, main_type, sub_type, budget)
        
        if not processed_places:
            # 没有找到结果
            import uuid
            result = {
                "requestId": f"req_{uuid.uuid4().hex[:8]}",
                "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ttlSeconds": 300,
                "markers": []
            }
        else:
            # 生成内容并格式化
            print(f"开始生成内容，共 {len(processed_places)} 个地点")
            result = generate_content_and_format(processed_places, main_type, sub_type)
        
        # 保存结果到 S3
        save_result_to_s3(s3_client, job_id, result, status="completed")
        
        elapsed_time = time.time() - start_time
        print(f"任务完成: {job_id}, 耗时: {elapsed_time:.2f}秒")
        
        return {
            "statusCode": 200,
            "body": json.dumps({
                "jobId": job_id,
                "status": "completed",
                "elapsedTime": f"{elapsed_time:.2f}秒"
            }, ensure_ascii=False)
        }
        
    except Exception as e:
        print(f"Lambda B 执行错误 (job_id={job_id}): {str(e)}")
        import traceback
        traceback.print_exc()
        
        # 尝试保存错误信息到 S3
        if s3_client is None:
            try:
                s3_client = boto3.client('s3', region_name=Config.S3_REGION)
            except:
                pass
        
        if s3_client:
            try:
                save_error_to_s3(s3_client, job_id, str(e))
            except Exception as save_error:
                print(f"保存错误信息到 S3 失败: {str(save_error)}")
        
        return {
            "statusCode": 500,
            "body": json.dumps({
                "jobId": job_id,
                "status": "failed",
                "error": str(e)
            }, ensure_ascii=False)
        }

