from openai import OpenAI

from app.configure import (
    MODEL_BASE_URL,
    MODEL_API_KEY,
)

client = OpenAI(
    base_url=MODEL_BASE_URL,
    api_key=MODEL_API_KEY,
)