from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """SQLAlchemy mappings; Prisma Migrate owns all DDL changes."""


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def created_at() -> Mapped[datetime]:
    return mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())


def updated_at() -> Mapped[datetime]:
    return mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    email_verified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
    owned_workspace: Mapped[Workspace | None] = relationship(back_populates="owner")
    memberships: Mapped[list[WorkspaceMember]] = relationship(back_populates="user", passive_deletes="all")
    upload_sessions: Mapped[list[UploadSession]] = relationship(back_populates="initiated_by", passive_deletes="all")


class MembershipPlan(Base):
    __tablename__ = "membership_plans"
    __table_args__ = (
        CheckConstraint("storage_limit_bytes >= 0", name="membership_plans_storage_limit_nonnegative_check"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    storage_limit_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
    workspaces: Mapped[list[Workspace]] = relationship(back_populates="plan", passive_deletes="all")


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT", onupdate="CASCADE"), nullable=False, unique=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("membership_plans.id", ondelete="RESTRICT", onupdate="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
    owner: Mapped[User] = relationship(back_populates="owned_workspace")
    plan: Mapped[MembershipPlan] = relationship(back_populates="workspaces")
    members: Mapped[list[WorkspaceMember]] = relationship(back_populates="workspace", passive_deletes="all")
    projects: Mapped[list[Project]] = relationship(back_populates="workspace", passive_deletes="all")
    upload_sessions: Mapped[list[UploadSession]] = relationship(back_populates="workspace", passive_deletes="all")


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="workspace_members_workspace_id_user_id_key"),
        Index("workspace_members_user_id_idx", "user_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="RESTRICT", onupdate="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT", onupdate="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(Enum("OWNER", "ADMIN", "EDITOR", "VIEWER", name="WorkspaceRole"), nullable=False)
    joined_at: Mapped[datetime] = created_at()
    workspace: Mapped[Workspace] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (Index("projects_workspace_id_event_date_idx", "workspace_id", "event_date"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="RESTRICT", onupdate="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    client_name: Mapped[str | None] = mapped_column(String(255))
    event_date: Mapped[date | None] = mapped_column(Date)
    venue: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Enum("ACTIVE", "PROCESSING", "ARCHIVED", name="ProjectStatus"), nullable=False, server_default="ACTIVE")
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
    workspace: Mapped[Workspace] = relationship(back_populates="projects")
    media_files: Mapped[list[MediaFile]] = relationship(back_populates="project", passive_deletes="all")
    upload_sessions: Mapped[list[UploadSession]] = relationship(back_populates="project", passive_deletes="all")
    processing_jobs: Mapped[list[ProcessingJob]] = relationship(back_populates="project", passive_deletes="all")
    albums: Mapped[list[Album]] = relationship(back_populates="project", passive_deletes="all")


class MediaFile(Base):
    """Logical media metadata; its MediaItems are physical Backblaze objects."""

    __tablename__ = "media_files"
    __table_args__ = (
        CheckConstraint("file_size_bytes IS NULL OR file_size_bytes >= 0", name="media_files_file_size_nonnegative_check"),
        CheckConstraint("width IS NULL OR width > 0", name="media_files_width_positive_check"),
        CheckConstraint("height IS NULL OR height > 0", name="media_files_height_positive_check"),
        CheckConstraint("duration_seconds IS NULL OR duration_seconds >= 0", name="media_files_duration_nonnegative_check"),
        CheckConstraint("latitude IS NULL OR latitude BETWEEN -90 AND 90", name="media_files_latitude_range_check"),
        CheckConstraint("longitude IS NULL OR longitude BETWEEN -180 AND 180", name="media_files_longitude_range_check"),
        Index("media_files_project_id_captured_at_idx", "project_id", "captured_at"),
        Index("media_files_album_id_idx", "album_id"),
        Index("media_files_checksum_idx", "checksum"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT", onupdate="CASCADE"), nullable=False)
    album_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("albums.id", ondelete="RESTRICT", onupdate="CASCADE"))
    media_type: Mapped[str] = mapped_column(Enum("PHOTO", "VIDEO", name="MediaType"), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_extension: Mapped[str | None] = mapped_column(String(20))
    mime_type: Mapped[str | None] = mapped_column(String(100))
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(DOUBLE_PRECISION)
    captured_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    camera_make: Mapped[str | None] = mapped_column(String(255))
    camera_model: Mapped[str | None] = mapped_column(String(255))
    lens_model: Mapped[str | None] = mapped_column(String(255))
    iso: Mapped[int | None] = mapped_column(Integer)
    aperture: Mapped[str | None] = mapped_column(String(50))
    shutter_speed: Mapped[str | None] = mapped_column(String(50))
    latitude: Mapped[float | None] = mapped_column(DOUBLE_PRECISION)
    longitude: Mapped[float | None] = mapped_column(DOUBLE_PRECISION)
    checksum: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
    project: Mapped[Project] = relationship(back_populates="media_files")
    album: Mapped[Album | None] = relationship(back_populates="media_files")
    items: Mapped[list[MediaItem]] = relationship(back_populates="media_file", passive_deletes="all")
    processing_jobs: Mapped[list[ProcessingJob]] = relationship(back_populates="media_file", passive_deletes="all")


class MediaItem(Base):
    """One Backblaze B2 location for an original or derived representation."""

    __tablename__ = "media_items"
    __table_args__ = (
        UniqueConstraint("media_file_id", "file_role", name="media_items_media_file_id_file_role_key"),
        CheckConstraint("file_size_bytes IS NULL OR file_size_bytes >= 0", name="media_items_file_size_nonnegative_check"),
        CheckConstraint("width IS NULL OR width > 0", name="media_items_width_positive_check"),
        CheckConstraint("height IS NULL OR height > 0", name="media_items_height_positive_check"),
        Index("media_items_media_file_id_idx", "media_file_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    media_file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("media_files.id", ondelete="RESTRICT", onupdate="CASCADE"), nullable=False)
    file_role: Mapped[str] = mapped_column(Enum("ORIGINAL", "COMPRESSED", "PREVIEW", "THUMBNAIL", "PROXY", name="FileRole"), nullable=False)
    storage_provider: Mapped[str] = mapped_column(Enum("BACKBLAZE_B2", name="StorageProvider"), nullable=False, server_default="BACKBLAZE_B2")
    storage_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String(100))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    checksum: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(Enum("PENDING", "UPLOADING", "AVAILABLE", "FAILED", "DELETED", name="MediaItemStatus"), nullable=False, server_default="PENDING")
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
    media_file: Mapped[MediaFile] = relationship(back_populates="items")


class UploadSession(Base):
    __tablename__ = "upload_sessions"
    __table_args__ = (
        CheckConstraint("total_files >= 0", name="upload_sessions_total_files_nonnegative_check"),
        CheckConstraint("completed_files >= 0", name="upload_sessions_completed_files_nonnegative_check"),
        CheckConstraint("failed_files >= 0", name="upload_sessions_failed_files_nonnegative_check"),
        CheckConstraint("total_bytes IS NULL OR total_bytes >= 0", name="upload_sessions_total_bytes_nonnegative_check"),
        CheckConstraint("uploaded_bytes >= 0", name="upload_sessions_uploaded_bytes_nonnegative_check"),
        Index("upload_sessions_project_id_idx", "project_id"),
        Index("upload_sessions_status_idx", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT", onupdate="CASCADE"), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="RESTRICT", onupdate="CASCADE"), nullable=False)
    initiated_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT", onupdate="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(Enum("DESKTOP", "WEB", name="UploadSource"), nullable=False)
    status: Mapped[str] = mapped_column(Enum("PENDING", "IN_PROGRESS", "COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "CANCELLED", name="UploadSessionStatus"), nullable=False, server_default="PENDING")
    total_files: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_files: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_files: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    uploaded_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
    project: Mapped[Project] = relationship(back_populates="upload_sessions")
    workspace: Mapped[Workspace] = relationship(back_populates="upload_sessions")
    initiated_by: Mapped[User] = relationship(back_populates="upload_sessions")


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        CheckConstraint("progress BETWEEN 0 AND 100", name="processing_jobs_progress_range_check"),
        CheckConstraint("attempts >= 0", name="processing_jobs_attempts_nonnegative_check"),
        CheckConstraint("result_version > 0", name="processing_jobs_result_version_positive_check"),
        Index("processing_jobs_project_id_idx", "project_id"),
        Index("processing_jobs_media_file_id_idx", "media_file_id"),
        Index("processing_jobs_status_execution_location_idx", "status", "execution_location"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT", onupdate="CASCADE"), nullable=False)
    media_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("media_files.id", ondelete="RESTRICT", onupdate="CASCADE"))
    job_type: Mapped[str] = mapped_column(Enum("METADATA_EXTRACTION", "COMPRESSION", "THUMBNAIL_GENERATION", "PREVIEW_GENERATION", "AI_CULLING", "FACE_DETECTION", "FACE_GROUPING", "SEMANTIC_INDEXING", "VIDEO_ANALYSIS", "VIDEO_PROXY_GENERATION", name="ProcessingJobType"), nullable=False)
    execution_location: Mapped[str] = mapped_column(Enum("LOCAL", "CLOUD", name="ExecutionLocation"), nullable=False)
    status: Mapped[str] = mapped_column(Enum("QUEUED", "PROCESSING", "COMPLETED", "FAILED", "CANCELLED", "SKIPPED", name="ProcessingJobStatus"), nullable=False, server_default="QUEUED")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    processor_version: Mapped[str | None] = mapped_column(String(100))
    result_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    input_manifest: Mapped[dict | None] = mapped_column(JSONB)
    result_manifest: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
    project: Mapped[Project] = relationship(back_populates="processing_jobs")
    media_file: Mapped[MediaFile | None] = relationship(back_populates="processing_jobs")


class Album(Base):
    __tablename__ = "albums"
    __table_args__ = (
        Index("albums_project_id_idx", "project_id"),
        Index("albums_parent_album_id_idx", "parent_album_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT", onupdate="CASCADE"), nullable=False)
    parent_album_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("albums.id", ondelete="RESTRICT", onupdate="CASCADE"))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
    project: Mapped[Project] = relationship(back_populates="albums")
    parent: Mapped[Album | None] = relationship(back_populates="children", remote_side="Album.id")
    children: Mapped[list[Album]] = relationship(back_populates="parent", passive_deletes="all")
    media_files: Mapped[list[MediaFile]] = relationship(back_populates="album", passive_deletes="all")
