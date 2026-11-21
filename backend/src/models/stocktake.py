from datetime import datetime
from sqlalchemy import JSON, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.utils.time import now


class StocktakeBatch(Base):
    """一次月末扫码实盘对账批次；输入变化后整体重算，结果以新版本保存。"""

    __tablename__ = 'stocktake_batches'

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    snapshot_date: Mapped[str] = mapped_column(String(10), index=True)
    status: Mapped[str] = mapped_column(String(20), default='queued', index=True)
    # 当前生效的工具快照与扫码账（服务层保证仅在重算成功后整体替换）。
    tools: Mapped[list] = mapped_column(JSON, default=list)
    scans: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[dict | None] = mapped_column(JSON)
    input_checksum: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(default=1)
    last_error: Mapped[str | None] = mapped_column(String(500))
    attempt: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None]
    recalculating: Mapped[bool] = mapped_column(default=False)
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)
    finished_at: Mapped[datetime | None]


class ScanRecord(Base):
    """扫码账流水：只追加不改写，状态戳记录扫码进入时批次所处版本。

    并发补扫时以 (batch_id, scan_id) 唯一约束兜底；旧计算在提交前比对输入指纹，
    指纹已变则放弃落账，保证旧结果不会盖掉新账。
    """

    __tablename__ = 'stocktake_scans'
    __table_args__ = (
        UniqueConstraint('batch_id', 'scan_id', name='uq_stocktake_scan_id'),
        Index('ix_stocktake_scan_batch', 'batch_id', 'id'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('stocktake_batches.id'))
    scan_id: Mapped[str] = mapped_column(String(80))
    tag_read: Mapped[str | None] = mapped_column(String(160))
    candidates: Mapped[list] = mapped_column(JSON)
    source: Mapped[str | None] = mapped_column(String(120))
    scanned_at: Mapped[str | None] = mapped_column(String(40))
    # 该扫码进入账本时批次的输入版本；重算后用于判断是否为「本次新账」。
    input_version: Mapped[int]
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)


class DisputeGroup(Base):
    """重算后落库的待核组快照，供盘点页面按组追踪人工核示。"""

    __tablename__ = 'stocktake_dispute_groups'
    __table_args__ = (UniqueConstraint('batch_id', 'group_seq'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('stocktake_batches.id'))
    version: Mapped[int]
    group_seq: Mapped[int]
    scan_ids: Mapped[list] = mapped_column(JSON)
    tool_ids: Mapped[list] = mapped_column(JSON)
    optional_pairs: Mapped[list] = mapped_column(JSON)
    max_pairs: Mapped[int]
    basis: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=now)


class DisputeResolution(Base):
    """主管对争议组的人工核示，只作处置留痕，不改写引擎计算结论。"""

    __tablename__ = 'stocktake_dispute_resolutions'
    __table_args__ = (UniqueConstraint('batch_id', 'group_id'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('stocktake_batches.id'))
    group_id: Mapped[int]
    scan_id: Mapped[str] = mapped_column(String(80))
    tool_id: Mapped[str] = mapped_column(String(80))
    note: Mapped[str] = mapped_column(String(500))
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
