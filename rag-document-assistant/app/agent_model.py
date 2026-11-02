"""Configured Gemini/OpenAI-compatible tool-calling adapter; explicit opt-in."""

from app.agent_loop import ModelTurn


class ConfiguredInvestigationModel:
    def __init__(self):
        from app.configure import LLM_MODEL
        from app.model_client import client
        self.model_name = LLM_MODEL
        self._client = client

    def next_turn(self, messages, tools, *, timeout_seconds):
        from openai import APITimeoutError
        try:
            response = self._client.with_options(
                timeout=timeout_seconds, max_retries=0,
            ).chat.completions.create(
                model=self.model_name, messages=messages, tools=tools,
                tool_choice="auto", max_tokens=2048,
            )
        except APITimeoutError as exc:
            raise TimeoutError("Model request timed out") from exc
        if not response.choices or response.choices[0].finish_reason == "length":
            raise ValueError("Model response is empty or truncated")
        return ModelTurn(
            message=response.choices[0].message.model_dump(exclude_none=True),
            usage=response.usage.model_dump(exclude_none=True) if response.usage else None,
        )
