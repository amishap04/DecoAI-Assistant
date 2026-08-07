"""Pydantic models for inventory items — the shared data contract."""
from typing import Optional
from pydantic import BaseModel, Field


class ItemBase(BaseModel):
    item_name: str
    color: Optional[str] = None
    cost_ea: float = Field(0, ge=0)   # purchase price per unit
    rent_ea: float = Field(0, ge=0)   # rental price per unit
    quantity: int = Field(0, ge=0)    # current stock
    last_purchased: Optional[str] = None  # optional ISO date YYYY-MM-DD
    bin_id: Optional[str] = None


class ItemCreate(ItemBase):
    pass


class ItemUpdate(BaseModel):
    """All fields optional — partial update."""
    item_name: Optional[str] = None
    color: Optional[str] = None
    cost_ea: Optional[float] = Field(None, ge=0)
    rent_ea: Optional[float] = Field(None, ge=0)
    quantity: Optional[int] = Field(None, ge=0)
    last_purchased: Optional[str] = None
    bin_id: Optional[str] = None


class Item(ItemBase):
    id: int
    updated_at: str
