"""SQLAlchemy 2.0 ORM models. Schema mirrors scripts/init_db.sql."""

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Table, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from imagelib.config import config

IMAGE_STATUSES = ("pending", "indexed", "analysed", "error")


class Base(DeclarativeBase):
    pass


class Source(Base):
    """One watched directory on a drive."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(String, unique=True)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)

    images: Mapped[list["Image"]] = relationship(back_populates="source")


class Person(Base):
    """A cluster of face embeddings (an identified/grouped person)."""

    __tablename__ = "persons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(512), nullable=True)
    cover_face_id: Mapped[int | None] = mapped_column(ForeignKey("faces.id"), nullable=True)

    faces: Mapped[list["Face"]] = relationship(back_populates="person")


class Image(Base):
    """One indexed image file and its metadata/status."""

    __tablename__ = "images"
    __table_args__ = (
        CheckConstraint("status IN ('pending','indexed','analysed','error')", name="ck_images_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(String, unique=True)
    content_hash: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gps_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_place: Mapped[str | None] = mapped_column(String, nullable=True)
    camera_make: Mapped[str | None] = mapped_column(String, nullable=True)
    camera_model: Mapped[str | None] = mapped_column(String, nullable=True)
    thumb_path: Mapped[str | None] = mapped_column(String, nullable=True)
    face_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    source: Mapped[Source] = relationship(back_populates="images")
    faces: Mapped[list["Face"]] = relationship(back_populates="image", cascade="all, delete-orphan")


class Face(Base):
    """A detected face: bounding box, embedding, optional person link."""

    __tablename__ = "faces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    image_id: Mapped[int] = mapped_column(ForeignKey("images.id", ondelete="CASCADE"))
    person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id", ondelete="SET NULL"), nullable=True)
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    w: Mapped[float] = mapped_column(Float)
    h: Mapped[float] = mapped_column(Float)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(512), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    image: Mapped[Image] = relationship(back_populates="faces")
    person: Mapped[Person | None] = relationship(back_populates="faces")


def database_url() -> str:
    return config["db"]["dsn"]