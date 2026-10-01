import os

from dotenv import load_dotenv

from app.schema_constants import DEFAULT_EMBEDDING_DIMENSION

load_dotenv()

MODEL_BASE_URL = os.getenv("MODEL_BASE_URL")
MODEL_API_KEY = os.getenv("MODEL_API_KEY")

LLM_MODEL = os.getenv("LLM_MODEL")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL")
EMBEDDING_DIMENSION = int(
    os.getenv("EMBEDDING_DIMENSION", str(DEFAULT_EMBEDDING_DIMENSION))
)

DATABASE_URL = os.getenv("DATABASE_URL")

if not MODEL_BASE_URL:
    raise ValueError("MODEL_BASE_URL is not set")

if not MODEL_API_KEY:
    raise ValueError("MODEL_API_KEY is not set")

if not LLM_MODEL:
    raise ValueError("LLM_MODEL is not set")

if not EMBEDDING_MODEL:
    raise ValueError("EMBEDDING_MODEL is not set")

if EMBEDDING_DIMENSION <= 0:
    raise ValueError("EMBEDDING_DIMENSION must be positive")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL is not set")
