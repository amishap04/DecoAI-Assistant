"""Invoice extraction — pluggable model backend.

The owner uploads an invoice (image/PDF); an extractor turns it into structured
line items, using a single configured model:

    INVOICE_READ_MODEL_URL   OpenAI-compatible chat-completions endpoint for a
                              locally-run or self-hosted vision model (e.g.
                              http://localhost:11434/v1/chat/completions). If
                              unset, Claude is used instead via the Anthropic
                              SDK.
    INVOICE_READ_MODEL_NAME  Model name sent in the request body. For the URL
                              backend, the vision model's name on that server.
                              For the Claude backend, the model ID (default
                              claude-opus-5).
    INVOICE_READ_API_KEY     Bearer token for the URL backend, or the
                              Anthropic API key for the Claude backend (falls
                              back to ANTHROPIC_API_KEY / an
                              `ant auth login` profile).

If neither INVOICE_READ_MODEL_URL nor an Anthropic key is configured, a
deterministic mock backend is used so the upload flow is testable without any
model configured.
"""
import base64
import io
import json
import os
import re
from typing import Optional

import httpx
from pydantic import BaseModel, Field


def _chat_completions_url(base_url: str) -> str:
    """Append the OpenAI-compatible chat-completions path if not already present.

    Lets INVOICE_READ_MODEL_URL be either a bare base URL (e.g. .../v1) or the
    full endpoint (e.g. .../v1/chat/completions) — both work.
    """
    url = base_url.rstrip("/")
    return url if url.endswith("/chat/completions") else f"{url}/chat/completions"


def _pdf_text(content: bytes, max_pages: int = 4) -> str:
    """Return the PDF's embedded text layer, or "" when there isn't a usable one.

    A digitally-generated invoice already carries every figure as text. Sending
    that text beats rasterizing and asking a vision model to read the numbers
    back: measured on deco_invoice.pdf, the image path returned roughly 30-60%
    of fields correctly across runs (wrong quantities, wrong unit prices, the
    due date reported as the invoice date), while the text layer carries all of
    them exactly. Scans have no text layer, so callers fall back to the image
    path when this returns "".
    """
    try:
        import pypdfium2 as pdfium  # imported lazily so the dep is optional
    except ImportError:
        return ""

    try:
        pdf = pdfium.PdfDocument(content)
        parts = []
        for page in list(pdf)[:max_pages]:
            parts.append(page.get_textpage().get_text_range())
        text = "\n".join(p for p in parts if p)
    except Exception:
        return ""

    # A scan usually yields a handful of stray glyphs rather than nothing at
    # all, so require enough characters and at least one digit before trusting
    # it as a real text layer.
    stripped = text.strip()
    if len(stripped) < 80 or not any(c.isdigit() for c in stripped):
        return ""
    return stripped


_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}

# "1 Paper Flower Hangings – Blue 4 $12.99 $51.96"
#  idx description                qty unit    amount
_TABLE_ROW = re.compile(
    r"^\s*\d+\s+(?P<desc>.+?)\s+(?P<qty>\d+)\s+\$?(?P<unit>[\d,]+\.\d{2})\s+\$?(?P<amount>[\d,]+\.\d{2})\s*$"
)
_DATE_LINE = re.compile(r"^\s*Date\s*:\s*([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", re.I)
_COLOUR_SUFFIX = re.compile(r"\s*[–\-]\s*([A-Za-z][A-Za-z ]{2,20})\s*$")


def _parse_invoice_text(text: str) -> Optional["InvoiceExtraction"]:
    """Parse a line-item table straight out of the PDF text layer.

    Digital invoices already carry every figure exactly, so for the standard
    "# Description Qty Unit Price Amount" layout there is nothing worth asking
    a model to infer. Each row is only accepted when qty * unit == amount, which
    makes a mis-parse fail closed rather than produce a plausible wrong number.

    Returns None when the text does not look like that table, so callers fall
    back to the LLM path for other layouts.
    """
    lines: list[InvoiceLine] = []
    for raw in text.splitlines():
        m = _TABLE_ROW.match(raw)
        if not m:
            continue
        try:
            qty = int(m.group("qty"))
            unit = float(m.group("unit").replace(",", ""))
            amount = float(m.group("amount").replace(",", ""))
        except ValueError:
            continue
        # Self-check: the row has to be internally consistent.
        if abs(qty * unit - amount) > 0.02:
            continue
        desc = m.group("desc").strip()
        colour = None
        cm = _COLOUR_SUFFIX.search(desc)
        if cm:
            colour = cm.group(1).strip()
            desc = _COLOUR_SUFFIX.sub("", desc).strip()
        lines.append(InvoiceLine(item_name=desc, color=colour, quantity=qty, cost_ea=unit))

    if not lines:
        return None

    invoice_date = None
    for raw in text.splitlines():
        d = _DATE_LINE.match(raw)
        if d:
            month = _MONTHS.get(d.group(1).lower())
            if month:
                invoice_date = f"{int(d.group(3)):04d}-{month:02d}-{int(d.group(2)):02d}"
            break

    return InvoiceExtraction(invoice_date=invoice_date, lines=lines)


def _pdf_first_page_to_png(content: bytes) -> bytes:
    """Rasterize a PDF's first page to PNG bytes.

    Many OpenAI-compatible gateways (incl. qgenie) only accept text/image_url
    content blocks — sending a PDF as image_url makes the underlying model
    reject it ("Could not process image"). Rendering to an image first sidesteps
    that; multi-page invoices are rare enough that page 1 is a fine default.
    """
    import pypdfium2 as pdfium  # imported lazily so the dep is optional

    pdf = pdfium.PdfDocument(content)
    bitmap = pdf[0].render(scale=150 / 72)
    buf = io.BytesIO()
    bitmap.to_pil().save(buf, format="PNG")
    return buf.getvalue()


class InvoiceLine(BaseModel):
    """One extracted line item from an invoice."""
    item_name: str
    color: Optional[str] = None
    quantity: int = Field(1, ge=0)
    cost_ea: float = Field(0, ge=0)
    # None = not decided yet (extraction never sets this — the agent decides reusability/rent
    # between extract and commit). Only a non-None value overwrites rent_ea on commit, so an
    # existing item's rent price is never clobbered back to 0 by a plain restock.
    rent_ea: Optional[float] = Field(None, ge=0)


class InvoiceExtraction(BaseModel):
    lines: list[InvoiceLine] = []
    invoice_date: Optional[str] = None  # ISO date YYYY-MM-DD if found


_EXTRACTION_PROMPT = (
    "You are an invoice parser for an event-decoration inventory system. "
    "Read the attached purchase invoice and return ONLY compact JSON matching "
    "this schema: {\"invoice_date\": \"YYYY-MM-DD or null\", \"lines\": "
    "[{\"item_name\": str, \"color\": str or null, \"quantity\": int, "
    "\"cost_ea\": number}]}. cost_ea is the unit price. Do not add commentary."
)


def _mock_extract(content: bytes, filename: str) -> InvoiceExtraction:
    """Deterministic stand-in used when no LLM endpoint is configured."""
    return InvoiceExtraction(
        invoice_date="2026-07-22",
        lines=[
            InvoiceLine(item_name="Gold Balloon", color="gold", quantity=100, cost_ea=0.45),
            InvoiceLine(item_name="Paper Lantern", color="red", quantity=24, cost_ea=1.20),
        ],
    )


def _llm_extract(content: bytes, filename: str, url: str, model_name: str,
                  key: Optional[str]) -> InvoiceExtraction:
    """Send the invoice image to an OpenAI-compatible vision chat endpoint.

    The chat-completions content-block schema only carries text/image_url — most
    gateways (incl. qgenie) reject a PDF sent that way, so PDFs are rasterized
    to a PNG of their first page first; images pass through unchanged.
    """
    # Prefer the embedded text layer for digital PDFs — the figures are already
    # exact there, so there is nothing for the model to misread. Falls through
    # to rasterizing for scans (and for images, which never have a text layer).
    pdf_text = _pdf_text(content) if filename.lower().endswith(".pdf") else ""

    # Exact path first: a standard line-item table in the text layer is parsed
    # directly, with a qty*unit==amount check per row. No model involved, so no
    # variance and nothing to misread. Other layouts fall through below.
    if pdf_text:
        parsed = _parse_invoice_text(pdf_text)
        if parsed is not None:
            return parsed

    if pdf_text:
        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"{_EXTRACTION_PROMPT}\n\n"
                        f"Invoice text (verbatim, copy figures exactly as written):\n"
                        f"---\n{pdf_text}\n---"
                    ),
                }
            ],
            "temperature": 0,
        }
    else:
        if filename.lower().endswith(".pdf"):
            content = _pdf_first_page_to_png(content)
            mime = "image/png"
        else:
            mime = "image/png" if filename.lower().endswith(".png") else "image/jpeg"
        b64 = base64.b64encode(content).decode()

        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _EXTRACTION_PROMPT},
                        {"type": "image_url",
                         "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                }
            ],
            "temperature": 0,
        }
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    verify = os.environ.get("DECOAI_INSECURE_SSL", "").lower() not in ("1", "true", "yes")
    resp = httpx.post(_chat_completions_url(url), json=payload, headers=headers,
                       timeout=120, verify=verify)
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    return InvoiceExtraction.model_validate(_loads_lenient(text))


def _claude_extract(content: bytes, filename: str, model_name: str,
                     key: Optional[str]) -> InvoiceExtraction:
    """Read the invoice with Claude's vision (image) or document (PDF) input."""
    import anthropic  # imported lazily so the dep is optional

    client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()

    if filename.lower().endswith(".pdf"):
        source_block = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf",
                       "data": base64.standard_b64encode(content).decode()},
        }
    else:
        mime = "image/png" if filename.lower().endswith(".png") else "image/jpeg"
        source_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": mime,
                       "data": base64.standard_b64encode(content).decode()},
        }

    response = client.messages.create(
        model=model_name,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [source_block, {"type": "text", "text": _EXTRACTION_PROMPT}],
        }],
    )
    text = " ".join(b.text for b in response.content if b.type == "text")
    return InvoiceExtraction.model_validate(_loads_lenient(text))


def _loads_lenient(text: str) -> dict:
    """Parse JSON that may be wrapped in prose or ```json fences.

    Only a *leading* fence is treated as a wrapper: `split("```")[1]` also fired
    on a single stray backtick fence anywhere in the response and returned the
    tail after it, turning a good extraction into a parse error.

    Deliberately does NOT salvage a partial line-item list the way vision.py
    does — these lines get committed to inventory, and silently importing an
    invoice minus its truncated tail would understate stock with no error.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text[3:].removeprefix("json").lstrip()
        fence = text.find("```")
        text = (text[:fence] if fence != -1 else text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    return json.loads(text)


def extract_invoice(content: bytes, filename: str) -> InvoiceExtraction:
    """Extract line items using the configured model, or a mock if unset.

    INVOICE_READ_MODEL_URL selects the OpenAI-compatible backend; when unset,
    Claude is used via INVOICE_READ_MODEL_NAME (default claude-opus-5) and
    INVOICE_READ_API_KEY (falling back to ANTHROPIC_API_KEY).
    """
    url = os.environ.get("INVOICE_READ_MODEL_URL")
    key = os.environ.get("INVOICE_READ_API_KEY")

    if url:
        model_name = os.environ.get("INVOICE_READ_MODEL_NAME", "default")
        return _llm_extract(content, filename, url, model_name, key)

    if key or os.environ.get("ANTHROPIC_API_KEY"):
        model_name = os.environ.get("INVOICE_READ_MODEL_NAME", "claude-opus-5")
        return _claude_extract(content, filename, model_name, key)

    return _mock_extract(content, filename)
