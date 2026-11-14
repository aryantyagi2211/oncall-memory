"""Application configuration shared by the incident assistant."""
import os
from dotenv import load_dotenv

load_dotenv()

HINDSIGHT_URL = os.getenv("HINDSIGHT_URL", "").strip()
HINDSIGHT_API_KEY = os.getenv("HINDSIGHT_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
BANK_ID = "oncall-v3"
PRIMARY_MODEL = "openai/gpt-oss-120b"
FALLBACK_MODEL = "openai/gpt-oss-20b"
HINDSIGHT_TIMEOUT_SECONDS = float(os.getenv("HINDSIGHT_TIMEOUT", "15"))
GROQ_TOTAL_DEADLINE_SECONDS = 60
GROQ_TRANSIENT_RETRIES = 3
GROQ_RETRY_DELAY_SECONDS = 2
GROQ_EMPTY_RETRIES = 1
GROQ_MAX_COMPLETION_TOKENS = 2000
HINDSIGHT_RETRIES = 1
HINDSIGHT_RETRY_DELAY_SECONDS = 1
MAX_RECALLED_INCIDENTS = 5
FEEDBACK_OUTCOME_FAILED = "did not work"
