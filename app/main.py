from __future__ import annotations

import hmac
import io
import os
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from fastapi import FastAPI, Header, HTTPException
from google.api_core.exceptions import AlreadyExists
from google.cloud import tasks_v2
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.orm import joinedload

from app.database import SessionLocal
from app.models import MediaFile, MediaItem, ProcessingJob, Project, Workspace

app = FastAPI(title="Redcloud media processing", docs_url=None, redoc_url=None)
PROCESSOR_VERSION = "image-webp-v1"
MAX_IMAGE_PIXELS = 60_000_000
PREVIEW_MAX_EDGE = 2560
THUMBNAIL_MAX_EDGE = 480
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
warnings.simplefilter("error", Image.DecompressionBombWarning)


class CompressionRequest(BaseModel):
    media_file_id: uuid.UUID
    original_item_id: uuid.UUID


class TaskRequest(BaseModel):
    job_id: uuid.UUID


def require_service_token(provided: str | None) -> None:
    expected = os.getenv("PROCESSING_SERVICE_TOKEN", "")
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def make_s3_client():
    bucket = os.getenv("B2_BUCKET_NAME")
    region = os.getenv("B2_REGION")
    key_id = os.getenv("B2_KEY_ID")
    application_key = os.getenv("B2_APPLICATION_KEY")
    if not all((bucket, region, key_id, application_key)):
        raise RuntimeError("B2_BUCKET_NAME, B2_REGION, B2_KEY_ID, and B2_APPLICATION_KEY are required")
    endpoint = os.getenv("B2_ENDPOINT_URL", f"https://s3.{region}.backblazeb2.com")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=key_id,
        aws_secret_access_key=application_key,
        config=Config(signature_version="s3v4", retries={"max_attempts": 4, "mode": "standard"}),
    )


def task_queue_config() -> tuple[str, str, str]:
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_TASKS_LOCATION")
    queue = os.getenv("GOOGLE_CLOUD_TASKS_QUEUE")
    service_url = os.getenv("PROCESSING_SERVICE_URL")
    if not all((project, location, queue, service_url)):
        raise HTTPException(status_code=503, detail="Cloud Tasks is not configured")
    return f"projects/{project}/locations/{location}/queues/{queue}", service_url.rstrip("/"), queue


def dispatch_job(job_id: uuid.UUID, result_version: int) -> None:
    parent, service_url, _ = task_queue_config()
    client = tasks_v2.CloudTasksClient()
    task_name = f"{parent}/tasks/image-compression-{job_id}-v{result_version}"
    task = {
        "name": task_name,
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{service_url}/internal/tasks/image-compression",
            "headers": {
                "Content-Type": "application/json",
                "X-Service-Token": os.environ["PROCESSING_SERVICE_TOKEN"],
            },
            "body": TaskRequest(job_id=job_id).model_dump_json().encode(),
        },
    }
    service_account = os.getenv("GOOGLE_CLOUD_TASKS_SERVICE_ACCOUNT")
    if service_account:
        task["http_request"]["oidc_token"] = {
            "service_account_email": service_account,
            "audience": service_url,
        }
    try:
        client.create_task(request={"parent": parent, "task": task})
    except AlreadyExists:
        pass


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/internal/jobs/image-compression", status_code=202)
def enqueue_image_compression(
    payload: CompressionRequest,
    x_service_token: str | None = Header(default=None),
):
    require_service_token(x_service_token)
    with SessionLocal() as session:
        original = session.scalar(
            select(MediaItem)
            .options(joinedload(MediaItem.media_file).joinedload(MediaFile.project))
            .where(MediaItem.id == payload.original_item_id)
        )
        if (
            original is None
            or original.media_file_id != payload.media_file_id
            or original.file_role != "ORIGINAL"
            or original.status != "AVAILABLE"
            or original.media_file.media_type != "PHOTO"
        ):
            raise HTTPException(status_code=409, detail="Available photo original not found")

        idempotency_key = f"image-compression:{payload.media_file_id}:{PROCESSOR_VERSION}"
        job = session.scalar(select(ProcessingJob).where(ProcessingJob.idempotency_key == idempotency_key))
        if job is None:
            job = ProcessingJob(
                project_id=original.media_file.project_id,
                media_file_id=payload.media_file_id,
                job_type="COMPRESSION",
                execution_location="CLOUD",
                status="QUEUED",
                processor_version=PROCESSOR_VERSION,
                idempotency_key=idempotency_key,
                input_manifest={"original_item_id": str(original.id)},
            )
            session.add(job)
            try:
                session.commit()
            except Exception:
                session.rollback()
                job = session.scalar(select(ProcessingJob).where(ProcessingJob.idempotency_key == idempotency_key))
                if job is None:
                    raise
        job_id = job.id
        if job.status == "FAILED":
            job.status = "QUEUED"
            job.result_version += 1
            job.error_message = None
            job.completed_at = None
            session.commit()
        status = job.status
        result_version = job.result_version

    if status not in ("COMPLETED", "SKIPPED", "CANCELLED"):
        dispatch_job(job_id, result_version)
    return {"job_id": str(job_id), "status": status, "result_version": result_version}


def encode_webp(source_path: Path, max_edge: int, quality: int) -> tuple[bytes, int, int]:
    try:
        with Image.open(source_path) as source:
            source.load()
            image = ImageOps.exif_transpose(source)
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "transparency" in image.info else "RGB")
            output = io.BytesIO()
            image.save(output, format="WEBP", quality=quality, method=5, exact=False,
                       icc_profile=image.info.get("icc_profile"))
            return output.getvalue(), image.width, image.height
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError("Unsupported or invalid image file") from error


def reserve_derivative_rows(session, media_file: MediaFile, original: MediaItem, outputs: dict):
    workspace_id = media_file.project.workspace_id
    session.execute(text("SELECT id FROM workspaces WHERE id = :id FOR UPDATE"), {"id": workspace_id})
    workspace = session.scalar(select(Workspace).options(joinedload(Workspace.plan)).where(Workspace.id == workspace_id))
    if workspace is None:
        raise RuntimeError("Workspace not found")

    existing = {
        item.file_role: item
        for item in session.scalars(select(MediaItem).where(MediaItem.media_file_id == media_file.id))
        if item.file_role in ("COMPRESSED", "THUMBNAIL")
    }
    used = session.scalar(
        select(func.coalesce(func.sum(MediaItem.file_size_bytes), 0))
        .join(MediaFile, MediaFile.id == MediaItem.media_file_id)
        .join(Project, Project.id == MediaFile.project_id)
        .where(Project.workspace_id == workspace_id, MediaItem.status != "DELETED")
    ) or 0
    delta = sum(max(len(data) - (existing[role].file_size_bytes or 0), 0) for role, (data, _, _) in outputs.items())
    if used + delta > workspace.plan.storage_limit_bytes:
        raise RuntimeError("Workspace storage limit would be exceeded by image derivatives")

    parent = original.storage_key.rsplit("/", 1)[0]
    rows = {}
    for role, (data, width, height) in outputs.items():
        suffix = role.lower()
        item = existing.get(role)
        if item is None:
            item = MediaItem(
                media_file_id=media_file.id,
                file_role=role,
                storage_bucket=original.storage_bucket,
                storage_key=f"{parent}/{suffix}-{PROCESSOR_VERSION}.webp",
            )
            session.add(item)
        item.file_size_bytes = len(data)
        item.mime_type = "image/webp"
        item.width = width
        item.height = height
        item.status = "PENDING"
        rows[role] = item
    session.flush()
    return rows


def process_job(job_id: uuid.UUID) -> None:
    with SessionLocal() as session:
        job = session.scalar(
            select(ProcessingJob)
            .options(joinedload(ProcessingJob.media_file).joinedload(MediaFile.project))
            .where(ProcessingJob.id == job_id)
        )
        if job is None:
            raise HTTPException(status_code=404, detail="Processing job not found")
        if job.status == "COMPLETED":
            return
        original_id = uuid.UUID(job.input_manifest["original_item_id"])
        original = session.get(MediaItem, original_id)
        if original is None or original.status != "AVAILABLE":
            raise RuntimeError("The source original is unavailable")
        now = datetime.now(timezone.utc)
        claim = session.execute(
            update(ProcessingJob)
            .where(
                ProcessingJob.id == job_id,
                ProcessingJob.status != "COMPLETED",
                or_(
                    ProcessingJob.status != "PROCESSING",
                    ProcessingJob.started_at.is_(None),
                    ProcessingJob.started_at < now - timedelta(minutes=45),
                ),
            )
            .values(
                status="PROCESSING",
                attempts=ProcessingJob.attempts + 1,
                started_at=now,
                error_message=None,
                completed_at=None,
            )
        )
        session.commit()
        if claim.rowcount == 0:
            return

        source_path = Path(f"/tmp/{uuid.uuid4()}.source")
        try:
            s3 = make_s3_client()
            s3.download_file(original.storage_bucket, original.storage_key, str(source_path))
            preview = encode_webp(source_path, PREVIEW_MAX_EDGE, 84)
            thumbnail = encode_webp(source_path, THUMBNAIL_MAX_EDGE, 78)
            outputs = {"COMPRESSED": preview, "THUMBNAIL": thumbnail}
            rows = reserve_derivative_rows(session, job.media_file, original, outputs)
            session.commit()

            for role, (data, _, _) in outputs.items():
                item = rows[role]
                s3.put_object(
                    Bucket=item.storage_bucket,
                    Key=item.storage_key,
                    Body=data,
                    ContentType="image/webp",
                    CacheControl="public, max-age=31536000, immutable",
                )
            for item in rows.values():
                item.status = "AVAILABLE"
            job.status = "COMPLETED"
            job.progress = 100
            job.result_manifest = {
                role.lower(): {"item_id": str(item.id), "storage_key": item.storage_key, "bytes": item.file_size_bytes,
                               "width": item.width, "height": item.height}
                for role, item in rows.items()
            }
            job.completed_at = datetime.now(timezone.utc)
            session.commit()
        except Exception as error:
            session.rollback()
            failed_job = session.get(ProcessingJob, job_id)
            if failed_job:
                failed_job.status = "FAILED"
                failed_job.error_message = str(error)[:2000]
                failed_job.completed_at = datetime.now(timezone.utc)
                for item in session.scalars(
                    select(MediaItem).where(
                        MediaItem.media_file_id == job.media_file_id,
                        MediaItem.file_role.in_(["COMPRESSED", "THUMBNAIL"]),
                        MediaItem.status != "AVAILABLE",
                    )
                ):
                    item.status = "FAILED"
                session.commit()
            raise
        finally:
            source_path.unlink(missing_ok=True)


@app.post("/internal/tasks/image-compression")
def run_image_compression(
    payload: TaskRequest,
    x_service_token: str | None = Header(default=None),
):
    require_service_token(x_service_token)
    process_job(payload.job_id)
    return {"job_id": str(payload.job_id), "status": "COMPLETED"}
