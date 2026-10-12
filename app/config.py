import os
from dotenv import load_dotenv

load_dotenv()

# LLM Models (Ollama local inference)
INTENT_MODEL = os.getenv("INTENT_MODEL", "qwen2.5:14b-instruct")
SQL_MODEL = os.getenv("SQL_MODEL", "deepseek-coder:6.7b")
VALIDATION_MODEL = os.getenv("VALIDATION_MODEL", "qwen2.5:14b-instruct")
EXPLANATION_MODEL = os.getenv("EXPLANATION_MODEL", "qwen2.5:14b-instruct")

# System & Execution Limits
MAX_ROWS = int(os.getenv("MAX_ROWS", "100"))
QUERY_TIMEOUT = int(os.getenv("QUERY_TIMEOUT", "10"))  # seconds
