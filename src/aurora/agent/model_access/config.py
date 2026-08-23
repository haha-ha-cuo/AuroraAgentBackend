"""本地 API 管理层配置：读取环境变量并构建模型客户端。"""

from __future__ import annotations

import os

from dotenv import find_dotenv, load_dotenv
from langchain_openai import ChatOpenAI

# 向上搜索并加载项目根目录的 .env，不依赖 uv run 是否自动加载
load_dotenv(find_dotenv())


def build_llm():
    """构建模型客户端（ChatOpenAI，OpenAI 兼容端点）。

    必需配置缺失时直接抛异常，不静默回退。
    """
    api_key = os.getenv("AGENT_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = os.getenv("AGENT_MODEL")
    base_url = os.getenv("AGENT_BASE_URL", "https://api.openai.com/v1")

    missing = []
    if not api_key:
        missing.append("AGENT_API_KEY")
    if not model:
        missing.append("AGENT_MODEL")

    if missing:
        raise RuntimeError(
            f"模型配置不完整，缺少：{', '.join(missing)}。"
            "请在项目根目录 .env 中配置（参考 .env.example）。"
        )

    return ChatOpenAI(api_key=api_key, model=model, base_url=base_url, temperature=0)
