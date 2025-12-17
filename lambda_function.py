#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Lambda 函数入口点
此文件作为 AWS Lambda 的默认处理程序入口
"""

from recommendation_generator import lambda_handler

# 直接导出 lambda_handler 函数
# 这样 Lambda 可以使用默认的 handler: lambda_function.lambda_handler
__all__ = ['lambda_handler']

