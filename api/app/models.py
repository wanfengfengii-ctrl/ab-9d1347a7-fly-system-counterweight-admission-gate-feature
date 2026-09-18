from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Batten(Base):
    """吊杆（固定夹具），全库仅 G-01 / G-02 两根。"""

    __tablename__ = "battens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    capacity_grams: Mapped[int] = mapped_column(Integer, nullable=False)

    loads: Mapped[list["Load"]] = relationship(back_populates="batten")


class Load(Base):
    """成功装载的配重片记录；被拒绝的请求永不写入此表。"""

    __tablename__ = "loads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batten_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("battens.id"), nullable=False, index=True
    )
    # 配重片标识全库唯一：同一标识只能成功登记一次
    piece_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    weight_grams: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    batten: Mapped[Batten] = relationship(back_populates="loads")
