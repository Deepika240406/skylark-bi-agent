"""quota_check.py — identify which Gemini quota is exhausted.

Google returns the specific quota metric in the 429 body. Per-minute limits
clear on their own; per-day limits do not, and that difference decides whether
the deployed app will work for a reviewer today.
"""

import os

from dotenv import load_dotenv
from google import genai

load_dotenv()

MODELS = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash-lite"]

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

for model in MODELS:
    try:
        client.models.generate_content(model=model, contents="hi")
        print(f"[OK]   {model} — working, use this one")
    except Exception as exc:
        msg = str(exc)
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg.upper():
            if "PerDay" in msg or "per day" in msg.lower():
                print(f"[DAY]  {model} — DAILY quota exhausted, will not clear today")
            elif "PerMinute" in msg or "per minute" in msg.lower():
                print(f"[MIN]  {model} — per-minute quota, clears within 60s")
            else:
                print(f"[429]  {model} — quota hit, metric unclear")
            for line in msg.split(","):
                if "quotaId" in line or "quotaValue" in line or "retryDelay" in line:
                    print("        ", line.strip()[:110])
        elif "404" in msg:
            print(f"[GONE] {model} — model retired")
        else:
            print(f"[ERR]  {model} — {msg[:100]}")
