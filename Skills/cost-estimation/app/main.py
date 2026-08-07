"""DecoAI Cost Estimator — FastAPI service (Diagram 1).

Takes a decoration concept's needed items, checks the shared inventory DB, and
returns an itemized estimate plus the list of missing items to purchase.
Registered as an OpenClaw skill; also runnable standalone.
"""
from fastapi import FastAPI

from .estimator import NeededItem, Estimate, estimate

app = FastAPI(title="DecoAI Cost Estimator", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/estimate", response_model=Estimate)
def create_estimate(items: list[NeededItem]) -> Estimate:
    """Itemized cost estimate for a decoration concept's needed items."""
    return estimate(items)
