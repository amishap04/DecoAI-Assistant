"""Decoration image analysis — Step 2 of the owner workflow.

The owner uploads a photo of a decoration setup (or an inspiration image); a
vision model lists the decoration items it can see. Each detected item is then
checked against the shared inventory DB so the owner learns what's already on
the shelf and what has to be bought.

Uses a single configured model:

    IMAGE_READ_MODEL_URL   OpenAI-compatible chat-completions endpoint for a
                            locally-run or self-hosted vision model (e.g.
                            http://localhost:11434/v1/chat/completions,
                            AI Box Qwen-VL, YOLOv8 on the Uno Q via a
                            wrapper). If unset, Claude is used instead via
                            the Anthropic SDK.
    IMAGE_READ_MODEL_NAME  Model name sent in the request body. For the URL
                            backend, the vision model's name on that server.
                            For the Claude backend, the model ID (default
                            claude-opus-5).
    IMAGE_READ_API_KEY     Bearer token for the URL backend, or the Anthropic
                            API key for the Claude backend (falls back to
                            ANTHROPIC_API_KEY / an `ant auth login` profile).

If neither IMAGE_READ_MODEL_URL nor an Anthropic key is configured, a
deterministic mock backend is used so the flow is testable without hardware.
"""
import base64
import json
import os
import sys
from typing import Optional

import httpx
from pydantic import BaseModel, Field


def _chat_completions_url(base_url: str) -> str:
    """Append the OpenAI-compatible chat-completions path if not already present.

    Lets IMAGE_READ_MODEL_URL be either a bare base URL (e.g. .../v1) or the
    full endpoint (e.g. .../v1/chat/completions) — both work.
    """
    url = base_url.rstrip("/")
    return url if url.endswith("/chat/completions") else f"{url}/chat/completions"


_MAX_ATTEMPTS = 3


class DetectedItem(BaseModel):
    """One decoration item the vision model found in the image."""
    item_name: str
    color: Optional[str] = None
    quantity: int = Field(1, ge=0)   # how many the model counted


class ImageDetection(BaseModel):
    items: list[DetectedItem] = []


_DETECT_PROMPT = (
    "You are a vision assistant for an event-decoration inventory system. "
    "Look at the attached photo of a decoration setup and list every distinct "
    "decoration item you can see, with its dominant color and how many you count. "
    "Return ONLY compact JSON matching this schema: "
    '{"items": [{"item_name": str, "color": str or null, "quantity": int}]}. '
    "Use short generic product names (e.g. \"Balloon\", \"Paper Lantern\", "
    "\"Fairy Lights\"). Do not add commentary."
)


def _mock_detect(content: bytes, filename: str) -> ImageDetection:
    """Deterministic stand-in used when no vision endpoint is configured.

    Returns a mix that exercises both branches downstream: some items that exist
    in the seeded inventory (present) and some that don't (missing).
    """
    return ImageDetection(items=[
        DetectedItem(item_name="Gold Balloon", color="gold", quantity=40),
        DetectedItem(item_name="Fairy Lights", color="warm white", quantity=3),
        DetectedItem(item_name="Paper Lantern", color="red", quantity=6),
        DetectedItem(item_name="Silk Rose", color="red", quantity=20),
        DetectedItem(item_name="Table Runner", color="ivory", quantity=2),
    ])


def _strip_fences(text: str) -> str:
    """Strip a leading ```/```json wrapper and its closing partner, if present.

    Only a *leading* fence counts as a wrapper. The previous `split("```")[1]`
    also fired on a single stray backtick fence anywhere in the response — which
    the degenerating local model emits often — and returned the garbage tail,
    discarding an otherwise-usable JSON prefix.
    """
    t = text.strip()
    if not t.startswith("```"):
        return t
    t = t[3:].removeprefix("json").lstrip()
    end = t.find("```")
    return (t[:end] if end != -1 else t).strip()


def _salvage_objects(text: str) -> list:
    """Return the leading run of complete, parseable `{...}` objects in an array.

    Stops at the first object that doesn't parse, so a half-written object at the
    truncation point is dropped rather than guessed at.
    """
    open_bracket = text.find("[")
    if open_bracket == -1:
        return []
    arr = text[open_bracket + 1:]
    items, depth, start = [], 0, None
    for i, ch in enumerate(arr):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    items.append(json.loads(arr[start:i + 1]))
                except json.JSONDecodeError:
                    break
                start = None
    return items


def _loads_lenient(text: str) -> dict:
    """Parse JSON that may be wrapped in prose or ```json fences.

    Falls back to salvaging the leading run of well-formed item objects. The
    W4A16-quantized local vision model reliably emits valid JSON for the first
    ~7 items and then collapses into multilingual token garbage mid-object;
    without this, a response carrying seven good detections is thrown away
    whole. Salvage is announced on stderr — a partial read must never look like
    a complete one.
    """
    text = _strip_fences(text)
    start, end = text.find("{"), text.rfind("}")
    body = text[start:end + 1] if start != -1 and end != -1 else text
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        items = _salvage_objects(text[start:] if start != -1 else text)
        if not items:
            raise
        print(f"WARNING: vision output was malformed ({exc}); salvaged the first "
              f"{len(items)} well-formed item(s) and discarded the rest",
              file=sys.stderr)
        return {"items": items}


def _llm_detect(content: bytes, filename: str, url: str, model_name: str,
                 key: Optional[str]) -> ImageDetection:
    """Send the image to an OpenAI-compatible vision chat endpoint."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "jpeg"
    mime = "image/png" if ext == "png" else "image/jpeg"
    b64 = base64.b64encode(content).decode()

    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _DETECT_PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }
        ],
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    verify = os.environ.get("DECOAI_INSECURE_SSL", "").lower() not in ("1", "true", "yes")

    # The local quantized model degenerates into token garbage part-way through
    # a response often enough that a single shot is unreliable; each retry is an
    # independent generation, so a clean one usually turns up within three.
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        resp = httpx.post(_chat_completions_url(url), json=payload, headers=headers,
                           timeout=120, verify=verify)
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        try:
            detection = ImageDetection.model_validate(_loads_lenient(text))
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"WARNING: vision model returned unusable output "
                  f"(attempt {attempt}/{_MAX_ATTEMPTS}): {exc}", file=sys.stderr)
            continue
        if detection.items:
            return detection
        print(f"WARNING: vision model returned no items "
              f"(attempt {attempt}/{_MAX_ATTEMPTS})", file=sys.stderr)

    # Empty rather than raising: the caller reports "no items detected" and exits
    # 2, which is a real error the owner sees — not a traceback, and not a
    # fabricated detection.
    print(f"ERROR: vision model at {url} produced no parseable JSON in "
          f"{_MAX_ATTEMPTS} attempts", file=sys.stderr)
    return ImageDetection(items=[])


def _claude_detect(content: bytes, filename: str, model_name: str,
                    key: Optional[str]) -> ImageDetection:
    """Send the image to Claude's vision (image) input."""
    import anthropic  # imported lazily so the dep is optional

    client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "jpeg"
    mime = "image/png" if ext == "png" else "image/jpeg"

    response = client.messages.create(
        model=model_name,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime,
                                              "data": base64.standard_b64encode(content).decode()}},
                {"type": "text", "text": _DETECT_PROMPT},
            ],
        }],
    )
    text = " ".join(b.text for b in response.content if b.type == "text")
    return ImageDetection.model_validate(_loads_lenient(text))


def detect_items(content: bytes, filename: str) -> ImageDetection:
    """Detect decoration items using the configured model, or a mock if unset.

    IMAGE_READ_MODEL_URL selects the OpenAI-compatible backend; when unset,
    Claude is used via IMAGE_READ_MODEL_NAME (default claude-opus-5) and
    IMAGE_READ_API_KEY (falling back to ANTHROPIC_API_KEY).
    """
    url = os.environ.get("IMAGE_READ_MODEL_URL")
    key = os.environ.get("IMAGE_READ_API_KEY")

    if url:
        model_name = os.environ.get("IMAGE_READ_MODEL_NAME", "default")
        return _llm_detect(content, filename, url, model_name, key)

    if key or os.environ.get("ANTHROPIC_API_KEY"):
        model_name = os.environ.get("IMAGE_READ_MODEL_NAME", "claude-opus-5")
        return _claude_detect(content, filename, model_name, key)

    return _mock_detect(content, filename)
