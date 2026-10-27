"""Lớp trừu tượng nhiều nhà cung cấp AI. Chỉ trả về văn bản; không có quyền gọi công cụ giao dịch."""
import httpx

from ..core.config import settings


class AIError(Exception):
    pass


async def _post(url: str, headers: dict, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(url, headers=headers, json=body)
    if r.status_code >= 400:
        raise AIError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


async def _openai_compat(base: str, key: str, model: str, system: str, user: str) -> str:
    d = await _post(f"{base.rstrip('/')}/chat/completions", {"Authorization": f"Bearer {key}"},
                    {"model": model, "temperature": 0.2,
                     "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    return d["choices"][0]["message"]["content"]


async def _anthropic(system: str, user: str) -> str:
    d = await _post("https://api.anthropic.com/v1/messages",
                    {"x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01"},
                    {"model": settings.anthropic_model, "max_tokens": 1500, "system": system,
                     "messages": [{"role": "user", "content": user}]})
    return "".join(b.get("text", "") for b in d["content"])


async def _gemini(system: str, user: str) -> str:
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{settings.gemini_model}:generateContent")
    d = await _post(url, {"x-goog-api-key": settings.gemini_api_key},
                    {"systemInstruction": {"parts": [{"text": system}]},
                     "contents": [{"role": "user", "parts": [{"text": user}]}],
                     "generationConfig": {"temperature": 0.2}})
    return "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])


def configured(provider: str) -> bool:
    return {
        "anthropic": bool(settings.anthropic_api_key),
        "openai": bool(settings.openai_api_key),
        "gemini": bool(settings.gemini_api_key),
        "groq": bool(settings.groq_api_key),
    }.get(provider, False)


async def complete(system: str, user: str, provider: str | None = None) -> str:
    p = provider or settings.ai_provider
    if not configured(p):
        raise AIError(f"Nhà cung cấp AI '{p}' chưa được cấu hình")
    try:
        if p == "anthropic":
            return await _anthropic(system, user)
        if p == "openai":
            return await _openai_compat("https://api.openai.com/v1", settings.openai_api_key,
                                        settings.openai_model, system, user)
        if p == "gemini":
            return await _gemini(system, user)
        return await _openai_compat("https://api.groq.com/openai/v1", settings.groq_api_key,
                                    settings.groq_model, system, user)
    except AIError:
        raise
    except (httpx.HTTPError, KeyError, IndexError) as e:
        raise AIError(repr(e)) from e
