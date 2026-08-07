"""Amazon price lookup — pluggable provider for items not in the inventory DB.

When the Cost Estimator meets an item with no known cost, it asks this module for
a price. Fetching a real live price needs either the Amazon Product Advertising
API (Associates account + signed requests) or a third-party price API — scraping
the search page directly returns 503/CAPTCHA and is against Amazon's ToS, so it is
not done here.

Backends, tried in priority order:
    1. Google Shopping  SERPAPI_KEY — SerpAPI's google_shopping engine returns
                        real live prices across retailers (incl. Amazon). SerpAPI
                        handles the scraping legally; you just need an API key.
                        -> source "google-shopping" (a true live price).
    2. Real price API   PRICE_LOOKUP_URL — HTTP endpoint taking ?q=<terms>,
                        returning JSON {"price": <number>} (PA-API proxy or a
                        Rainforest/Zinc wrapper you host). PRICE_LOOKUP_KEY optional.
                        -> source "amazon" (a true live price).
    3. Model estimate   PRICE_PREDICT_MODEL_URL selects an OpenAI-compatible chat
                        endpoint (local, self-hosted, or any model behind that API
                        shape — e.g. the AI Box's llama.cpp/vLLM server); can be a
                        bare base URL (.../v1) or the full endpoint
                        (.../v1/chat/completions), same as INVOICE_READ_MODEL_URL.
                        PRICE_PREDICT_MODEL_NAME names the model on that
                        endpoint. If PRICE_PREDICT_MODEL_URL is unset, Claude is
                        used instead via the Anthropic SDK (PRICE_PREDICT_MODEL_NAME
                        as the Claude model ID, default claude-opus-5) — Claude
                        additionally runs the web_search tool for a live-ish quote,
                        which a generic chat endpoint cannot do.
                        PRICE_PREDICT_API_KEY is the bearer token (URL backend) or
                        the Anthropic API key (Claude backend, falls back to
                        ANTHROPIC_API_KEY). -> source "claude" or "llm".
    4. Mock             Deterministic per-item guesses. Always works, no network.
                        -> source "amazon-mock".
"""
import json
import os
import re
from typing import Optional

import httpx


class PriceResult:
    def __init__(self, price: float, source: str):
        self.price = price      # unit price
        self.source = source    # "google-shopping", "amazon", "claude", "llm", "amazon-mock", or "unknown"


# Rough per-item guesses for the mock backend (keyed by a word in the name).
_MOCK_PRICES = {
    "balloon": 0.30,
    "light": 9.99,
    "lantern": 1.50,
    "rose": 0.80,
    "flower": 1.20,
    "candle": 2.50,
    "ribbon": 3.00,
    "tulle": 6.00,
    "banner": 8.00,
    "confetti": 4.50,
}
_MOCK_DEFAULT = 5.00


def _mock_price(search_terms: str) -> PriceResult:
    lowered = search_terms.lower()
    for keyword, price in _MOCK_PRICES.items():
        if keyword in lowered:
            return PriceResult(price, "amazon-mock")
    return PriceResult(_MOCK_DEFAULT, "amazon-mock")


def _serpapi_price(search_terms: str, key: str) -> PriceResult:
    """Get a live price from Google Shopping via SerpAPI.

    Uses the cheapest in-stock result. SerpAPI returns each result's numeric
    `extracted_price`; we take the minimum across the first page.
    """
    resp = httpx.get(
        os.environ.get("SERPAPI_URL", "https://serpapi.com/search"),
        params={
            "engine": "google_shopping",
            "q": search_terms,
            "api_key": key,
            "gl": os.environ.get("SERPAPI_GL", "us"),
            "hl": os.environ.get("SERPAPI_HL", "en"),
        },
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json().get("shopping_results", [])
    prices = [r["extracted_price"] for r in results
              if isinstance(r.get("extracted_price"), (int, float))]
    if not prices:
        raise ValueError("no priced shopping results")
    return PriceResult(round(float(min(prices)), 2), "google-shopping")


def _remote_price(search_terms: str, url: str) -> PriceResult:
    key = os.environ.get("PRICE_LOOKUP_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    resp = httpx.get(url, params={"q": search_terms}, headers=headers, timeout=30)
    resp.raise_for_status()
    price = float(resp.json()["price"])
    return PriceResult(price, "amazon")


_CLAUDE_PROMPT = (
    "Search the web for the current price of this event-decoration item and "
    "report the typical price for ONE unit in USD: {item}. "
    "After searching, reply with ONLY a JSON object on its own line: "
    '{{"price": <number>}} — no currency symbol, no commentary.'
)


def _claude_price(search_terms: str, model_name: str, key: Optional[str]) -> PriceResult:
    """Use Claude with the web_search server tool to find a current price.

    Anthropic runs the search server-side, so this isn't blocked by Amazon's
    anti-bot 503s.
    """
    import anthropic  # imported lazily so the dep is optional

    client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    response = client.messages.create(
        model=model_name,
        max_tokens=1024,
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}],
        messages=[{"role": "user", "content": _CLAUDE_PROMPT.format(item=search_terms)}],
    )
    # Concatenate all text blocks (search happens server-side; the final answer
    # is in the text blocks) and pull the price out.
    text = " ".join(b.text for b in response.content if b.type == "text")
    return PriceResult(round(_extract_price(text), 2), "claude")


_LLM_PROMPT = (
    "You are a pricing assistant for an event-decoration shopping tool. "
    "Estimate the typical current Amazon price in USD for ONE unit of the item "
    "below. Base it on your knowledge of similar products. Reply with ONLY a JSON "
    'object: {{"price": <number>}}. No currency symbol, no commentary.\n\nItem: {item}'
)


def _extract_price(text: str) -> float:
    """Pull a numeric price out of the model's reply (JSON or loose text)."""
    try:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            return float(json.loads(text[start:end + 1])["price"])
    except (ValueError, KeyError):
        pass
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match:
        return float(match.group())
    raise ValueError(f"no price found in LLM reply: {text!r}")


def _chat_completions_url(base_url: str) -> str:
    """Append the OpenAI-compatible chat-completions path if not already present.

    Lets PRICE_PREDICT_MODEL_URL be either a bare base URL (e.g. .../v1) or the
    full endpoint (e.g. .../v1/chat/completions) — both work.
    """
    url = base_url.rstrip("/")
    return url if url.endswith("/chat/completions") else f"{url}/chat/completions"


def _llm_price(search_terms: str, url: str, model_name: str, key: Optional[str]) -> PriceResult:
    """Ask an OpenAI-compatible chat endpoint to estimate a unit price."""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": _LLM_PROMPT.format(item=search_terms)}],
        "temperature": 0,
    }
    resp = httpx.post(_chat_completions_url(url), json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    return PriceResult(round(_extract_price(text), 2), "llm")


def lookup_price(item_name: str, color: Optional[str] = None) -> PriceResult:
    """Look up a unit price for an item using the highest-priority configured backend."""
    search_terms = " ".join(t for t in (color, item_name) if t).strip()

    serpapi_key = os.environ.get("SERPAPI_KEY")
    if serpapi_key:
        try:
            return _serpapi_price(search_terms, serpapi_key)
        except Exception:
            pass  # fall through to the next backend rather than crash the estimate

    api_url = os.environ.get("PRICE_LOOKUP_URL")
    if api_url:
        try:
            return _remote_price(search_terms, api_url)
        except Exception:
            pass  # fall through to the next backend rather than crash the estimate

    model_url = os.environ.get("PRICE_PREDICT_MODEL_URL")
    model_key = os.environ.get("PRICE_PREDICT_API_KEY")
    if model_url:
        model_name = os.environ.get("PRICE_PREDICT_MODEL_NAME", "default")
        try:
            return _llm_price(search_terms, model_url, model_name, model_key)
        except Exception:
            pass  # fall through to mock
    elif model_key or os.environ.get("ANTHROPIC_API_KEY"):
        model_name = os.environ.get("PRICE_PREDICT_MODEL_NAME", "claude-opus-5")
        try:
            return _claude_price(search_terms, model_name, model_key)
        except Exception:
            pass  # fall through to mock

    return _mock_price(search_terms)

