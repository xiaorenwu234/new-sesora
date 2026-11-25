"""可插拔 LLM 客户端（LangChain ChatOpenAI，OpenAI 兼容接口）。

方案约束：LLM 只提出假设/实验建议/结果解释，不允许修改质量契约阈值，
也没有任何执行权限。未配置 API key 时 available() 为 False，
各 Agent 节点必须提供确定性启发式回退，保证系统可离线运行。

格式化输出两条路径：
- complete_structured：with_structured_output(Pydantic schema)，走服务端
  json_schema 约束解码（vLLM guided decoding），输出合法性由解码器保证；
- complete_json：通用 JSON 路径（json_mode + JsonOutputParser），兼容
  无固定 schema 的调用。

推理模式：默认启用 thinking（llm.enable_thinking），服务端需配
--reasoning-parser qwen3，思考过程被分离到 reasoning_content，
content 只含最终答案并受约束解码约束，两者兼得。
"""
from __future__ import annotations

import os
from typing import Type

from pydantic import BaseModel

from common.config import Config


class LLMClient:
    def __init__(self, cfg: Config):
        lc = cfg.get("llm", {}) or {}
        self.model = lc.get("model", "gpt-4o-mini")
        self.temperature = lc.get("temperature", 0.1)
        self.base_url = lc.get("base_url") or os.environ.get("CNAA_LLM_BASE_URL")
        self.api_key = os.environ.get(lc.get("api_key_env", "CNAA_LLM_API_KEY"), "")
        self.enable_thinking = bool(lc.get("enable_thinking", True))
        self.tokens_used = 0
        self._chat = None
        if self.api_key:
            try:
                from langchain_openai import ChatOpenAI
                self._chat = ChatOpenAI(
                    model=self.model, temperature=self.temperature,
                    base_url=self.base_url, api_key=self.api_key,
                    timeout=120, max_retries=1,
                    # 推理开关：启用时服务端 --reasoning-parser 把思考分离到
                    # reasoning_content，content 仅含最终答案（受约束解码约束）
                    extra_body={"chat_template_kwargs":
                                {"enable_thinking": self.enable_thinking}},
                )
            except Exception:
                self._chat = None

    def available(self) -> bool:
        return self._chat is not None

    def _count(self, msg) -> None:
        um = getattr(msg, "usage_metadata", None) or {}
        self.tokens_used += int(um.get("total_tokens", 0) or 0)

    def complete_structured(self, system: str, user: str,
                            schema: Type[BaseModel]) -> dict | None:
        """按 Pydantic schema 结构化输出；失败/不可用返回 None（走启发式回退）。"""
        if not self.available():
            return None
        try:
            chain = self._chat.with_structured_output(
                schema, method="json_schema", include_raw=True)
            out = chain.invoke([("system", system), ("user", user)])
            self._count(out.get("raw"))
            parsed = out.get("parsed")
            return parsed.model_dump() if parsed is not None else None
        except Exception:
            return None

    def complete_json(self, system: str, user: str, schema_hint: str = "") -> dict | None:
        """要求 LLM 输出 JSON；失败或不可用时返回 None（调用方走启发式回退）。"""
        if not self.available():
            return None
        prompt = user + ("\n\n输出格式要求（仅输出 JSON）：\n" + schema_hint if schema_hint else "")
        try:
            from langchain_core.output_parsers import JsonOutputParser
            chat = self._chat.bind(response_format={"type": "json_object"})
            msg = chat.invoke([("system", system), ("user", prompt)])
            self._count(msg)
            return JsonOutputParser().parse(msg.content)
        except Exception:
            return None