from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, Numeric, PrimaryKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"
    __table_args__ = (
        PrimaryKeyConstraint("currency", "valid_on", name="pk_exchange_rates"),
        CheckConstraint("rate_to_usd > 0", name="ck_exchange_rates_rate_positive"),
        # As-of lookups use the PK btree (currency, valid_on) scanned backwards — no second index.
    )

    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rate_to_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    valid_on: Mapped[date] = mapped_column(Date, nullable=False)
