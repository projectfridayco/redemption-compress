# Python processing service database mappings

These SQLAlchemy mappings target the existing Prisma-managed Neon schema. Prisma Migrate is the only schema migration authority. Do not call `Base.metadata.create_all()` or introduce Alembic migrations here.

Set `DATABASE_URL` to a Neon PostgreSQL connection string before importing `app.database`. Keep credentials in deployment environment variables and out of source control.

## Image processing service

Run locally from this directory with `uvicorn app.main:app --host 0.0.0.0 --port 8080`. The service receives authenticated enqueue requests from the Next.js app after an original photo is confirmed in B2. It records an idempotent `COMPRESSION` job, sends a Cloud Task, downloads the original in the task handler, and stores `COMPRESSED` and `THUMBNAIL` WebP objects. The original is never modified.

Configure these environment variables on both the Next.js app and the processing service:

- `PROCESSING_SERVICE_URL`: base HTTPS URL of this service, with no trailing slash.
- `PROCESSING_SERVICE_TOKEN`: long random secret shared by the app and service.

Configure these on the processing service:

- `DATABASE_URL`: Neon pooled connection string.
- `B2_BUCKET_NAME`, `B2_REGION`, `B2_KEY_ID`, `B2_APPLICATION_KEY`: a restricted B2 key with read and write access to the media bucket.
- `B2_ENDPOINT_URL` (optional): defaults to `https://s3.<B2_REGION>.backblazeb2.com`.
- `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_TASKS_LOCATION`, `GOOGLE_CLOUD_TASKS_QUEUE`: existing Cloud Tasks project, region, and queue.
- `GOOGLE_CLOUD_TASKS_SERVICE_ACCOUNT` (recommended): service account used for the task's OIDC token when invoking a Cloud Run service that requires authentication.

The Cloud Run runtime identity needs permission to enqueue tasks. The task service account needs permission to invoke the processing service. If Cloud Run allows unauthenticated invocation, keep the shared service token configured; it is checked by both internal endpoints. Use a queue with retry enabled so transient B2 or database failures are retried. The upload confirmation stays successful only when the task has been accepted; confirmation can be retried safely if enqueueing is temporarily unavailable.

Image outputs are capped at 2560 pixels on the longest edge for `COMPRESSED` (WebP quality 84) and 480 pixels for `THUMBNAIL` (quality 78). The decoder rejects images above 60 megapixels. For Cloud Run, allow at least 1 GiB of memory and set concurrency to 1 so decoding large originals does not compete with other jobs. Unsupported or malformed image formats fail the processing job while preserving the original.
