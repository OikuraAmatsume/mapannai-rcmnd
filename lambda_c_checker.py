#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Lambda C (查询器): recommendation_checker
"""

import json
import boto3
from botocore.exceptions import ClientError
from typing import Dict, Any

from config import Config

# S3 结果存储路径前缀（从 Config 获取，与 poi-images/ 同级）


def get_cors_headers() -> Dict[str, str]:
    """获取 CORS 响应头"""
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "GET, OPTIONS"
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda C 主处理函数 - 查询器
    """
    try:
        # 从路径参数或查询参数中获取 job_id
        job_id = None
        
        # 尝试从路径参数获取
        if event.get("pathParameters"):
            job_id = event["pathParameters"].get("job_id") or event["pathParameters"].get("jobId")
        
        # 尝试从查询参数获取
        if not job_id and event.get("queryStringParameters"):
            job_id = event["queryStringParameters"].get("job_id") or event["queryStringParameters"].get("jobId")
        
        # 尝试从请求体获取
        if not job_id:
            body = event.get("body", {})
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except:
                    body = {}
            job_id = body.get("job_id") or body.get("jobId")
        
        # 验证 job_id
        if not job_id:
            return {
                "statusCode": 400,
                "headers": get_cors_headers(),
                "body": json.dumps({
                    "error": "缺少必需参数: job_id"
                }, ensure_ascii=False)
            }
        
        # 验证配置
        if not Config.S3_BUCKET_NAME:
            return {
                "statusCode": 500,
                "headers": get_cors_headers(),
                "body": json.dumps({
                    "error": "S3_BUCKET_NAME 未配置"
                }, ensure_ascii=False)
            }
        
        # 初始化 S3 客户端
        s3_client = boto3.client('s3', region_name=Config.S3_REGION)
        
        # 构建 S3 Key
        s3_key = f"{Config.S3_JOB_RESULT_PREFIX}{job_id}.json"
        
        try:
            # 尝试获取 S3 对象
            response = s3_client.get_object(
                Bucket=Config.S3_BUCKET_NAME,
                Key=s3_key
            )
            
            # 读取并解析内容
            content = response['Body'].read().decode('utf-8')
            result_data = json.loads(content)
            
            # 获取状态
            status = result_data.get("status", "unknown")
            
            if status == "completed":
                # 任务完成，返回完整结果
                return {
                    "statusCode": 200,
                    "headers": get_cors_headers(),
                    "body": json.dumps({
                        "jobId": job_id,
                        "status": "completed",
                        "completedAt": result_data.get("completedAt"),
                        "result": result_data.get("result", {})
                    }, ensure_ascii=False)
                }
            elif status == "failed":
                # 任务失败，返回错误信息
                return {
                    "statusCode": 200,  # 返回 200，但 status 为 failed
                    "headers": get_cors_headers(),
                    "body": json.dumps({
                        "jobId": job_id,
                        "status": "failed",
                        "completedAt": result_data.get("completedAt"),
                        "error": result_data.get("error", "未知错误")
                    }, ensure_ascii=False)
                }
            else:
                # 未知状态
                return {
                    "statusCode": 200,
                    "headers": get_cors_headers(),
                    "body": json.dumps({
                        "jobId": job_id,
                        "status": status,
                        "data": result_data
                    }, ensure_ascii=False)
                }
                
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            
            if error_code == 'NoSuchKey' or error_code == '404':
                # 文件不存在，任务仍在处理中
                return {
                    "statusCode": 200,  # 返回 200，但 status 为 processing
                    "headers": get_cors_headers(),
                    "body": json.dumps({
                        "jobId": job_id,
                        "status": "processing",
                        "message": "任务正在处理中，请稍后再试",
                        "retryAfter": 5  # 建议 5 秒后重试
                    }, ensure_ascii=False)
                }
            else:
                # 其他 S3 错误
                raise e
        
    except json.JSONDecodeError as e:
        return {
            "statusCode": 400,
            "headers": get_cors_headers(),
            "body": json.dumps({
                "error": f"JSON 解析错误: {str(e)}"
            }, ensure_ascii=False)
        }
        
    except Exception as e:
        print(f"Lambda C 执行错误: {str(e)}")
        import traceback
        traceback.print_exc()
        
        return {
            "statusCode": 500,
            "headers": get_cors_headers(),
            "body": json.dumps({
                "error": f"服务器内部错误: {str(e)}"
            }, ensure_ascii=False)
        }

