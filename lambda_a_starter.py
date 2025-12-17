#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Lambda A (启动器): recommendation_starter
功能：接收客户端请求，生成 job_id，异步调用 Lambda B (执行器)
"""

import json
import uuid
import boto3
from datetime import datetime, timezone
from typing import Dict, Any

# Lambda B 的函数名称（需要在部署时配置）
import os
LAMBDA_B_FUNCTION_NAME = os.environ.get('LAMBDA_B_FUNCTION_NAME', 'recommendation_executor')


def get_cors_headers() -> Dict[str, str]:
    """获取 CORS 响应头"""
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "POST, OPTIONS"
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda A 主处理函数 - 启动器
    
    1. 生成唯一的 job_id
    2. 异步调用 Lambda B
    3. 立即返回 job_id 给客户端
    
    Args:
        event: API Gateway 事件
        context: Lambda 上下文
    
    Returns:
        API Gateway 响应，包含 job_id
    """
    try:
        # 解析请求体
        if isinstance(event.get("body"), str):
            body = json.loads(event["body"])
        else:
            body = event.get("body", {})
        
        # 提取参数
        lat = body.get("lat")
        lng = body.get("lng")
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
        
        # 生成唯一的 job_id
        job_id = f"job_{uuid.uuid4().hex}"
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        
        # 构建传递给 Lambda B 的 payload
        lambda_b_payload = {
            "job_id": job_id,
            "lat": float(lat),
            "lng": float(lng),
            "main_type": main_type,
            "sub_type": sub_type,
            "budget": budget,
            "created_at": created_at
        }
        
        # 异步调用 Lambda B
        lambda_client = boto3.client('lambda')
        
        response = lambda_client.invoke(
            FunctionName=LAMBDA_B_FUNCTION_NAME,
            InvocationType='Event',  # 异步调用
            Payload=json.dumps(lambda_b_payload)
        )
        
        # 检查调用是否成功（异步调用返回 202）
        status_code = response.get('StatusCode', 0)
        if status_code != 202:
            raise Exception(f"Lambda B 调用失败，状态码: {status_code}")
        
        # 立即返回 job_id 给客户端
        return {
            "statusCode": 202,  # Accepted
            "headers": get_cors_headers(),
            "body": json.dumps({
                "jobId": job_id,
                "status": "processing",
                "message": "请求已接受，正在处理中",
                "createdAt": created_at,
                "pollUrl": f"/recommendation/status/{job_id}",
                "estimatedTime": "30-60秒"
            }, ensure_ascii=False)
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
        print(f"Lambda A 执行错误: {str(e)}")
        import traceback
        traceback.print_exc()
        
        return {
            "statusCode": 500,
            "headers": get_cors_headers(),
            "body": json.dumps({
                "error": f"服务器内部错误: {str(e)}"
            }, ensure_ascii=False)
        }

