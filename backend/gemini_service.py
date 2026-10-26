
import json
import logging
import os

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types


load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
FALLBACK_MODEL_NAME = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.6-flash")
logger = logging.getLogger(__name__)

if not API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not configured.")

# Prevent API requests from waiting indefinitely when the provider or network
# does not respond. The Google Gen AI SDK expresses this timeout in milliseconds.
client = genai.Client(
    api_key=API_KEY,
    http_options=types.HttpOptions(timeout=30_000),
)


def translate_text(text: str, source_language: str) -> dict[str, str]:
    if source_language not in ("en", "vi"):
        raise ValueError("source_language must be 'en' or 'vi'.")

    if not text or not text.strip():
        raise ValueError("Text must not be empty.")

    source_name = "English" if source_language == "en" else "Vietnamese"
    target_name = "Vietnamese" if source_language == "en" else "English"

    prompt = f"""
You are a professional English-Vietnamese interpreter.

Translate the following {source_name} text into {target_name}.
Preserve the original meaning, names, technical terminology, and tone.
Do not add explanations or commentary.

Return both languages in JSON:
- en: the English text
- vn: the Vietnamese text

The source text is written in {source_name}:
<source_text>
{text}
</source_text>
"""

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema={
            "type": "OBJECT",
            "properties": {
                "en": {"type": "STRING"},
                "vn": {"type": "STRING"},
            },
            "required": ["en", "vn"],
        },
    )

    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=config,
        )
    except errors.ServerError as exc:
        if exc.code != 503 or FALLBACK_MODEL_NAME == MODEL_NAME:
            raise

        logger.warning(
            "Primary Gemini model returned 503; trying the fallback model."
        )
        response = client.models.generate_content(
            model=FALLBACK_MODEL_NAME,
            contents=prompt,
            config=config,
        )

    if not response.text:
        raise RuntimeError("Gemini returned an empty response.")

    result = json.loads(response.text)

    en = result.get("en")
    vn = result.get("vn")

    if not isinstance(en, str) or not isinstance(vn, str):
        raise RuntimeError("Gemini returned an invalid bilingual response.")

    if not en.strip() or not vn.strip():
        raise RuntimeError("Gemini returned empty bilingual text.")

    # Preserve the original source text instead of replacing it
    # with a model-generated version.
    if source_language == "en":
        en = text.strip()
    else:
        vn = text.strip()

    return {"en": en, "vn": vn}
