from datetime import datetime, timezone
from sqlalchemy import String, Integer, Text, DateTime, ForeignKey, Boolean, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base

def now_utc():
    return datetime.now(timezone.utc)

class DocumentVersion(Base):
    __tablename__ = "document_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_key: Mapped[str] = mapped_column(String(100), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    is_latest: Mapped[bool] = mapped_column(Boolean, default=True)
    nodes: Mapped[list["DocumentNode"]] = relationship(back_populates="version", cascade="all, delete-orphan")
    __table_args__ = (UniqueConstraint("document_key", "version_number", name="uq_document_version"),)

class DocumentNode(Base):
    __tablename__ = "document_nodes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    logical_id: Mapped[str] = mapped_column(String(36), index=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("document_nodes.id"), nullable=True, index=True)
    heading: Mapped[str] = mapped_column(Text)
    section_number: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    level: Mapped[int] = mapped_column(Integer, default=0)
    node_type: Mapped[str] = mapped_column(String(30), default="section")
    body_text: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    path: Mapped[str] = mapped_column(Text, default="")
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    version: Mapped[DocumentVersion] = relationship(back_populates="nodes")
    parent: Mapped["DocumentNode | None"] = relationship(remote_side=[id], back_populates="children")
    children: Mapped[list["DocumentNode"]] = relationship(back_populates="parent", cascade="all, delete-orphan")
    __table_args__ = (UniqueConstraint("version_id", "path", name="uq_node_path_per_version"),)

class Selection(Base):
    __tablename__ = "selections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    items: Mapped[list["SelectionItem"]] = relationship(back_populates="selection", cascade="all, delete-orphan")

class SelectionItem(Base):
    __tablename__ = "selection_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    selection_id: Mapped[str] = mapped_column(ForeignKey("selections.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("document_nodes.id"), index=True)
    selection: Mapped[Selection] = relationship(back_populates="items")
    node: Mapped[DocumentNode] = relationship()
