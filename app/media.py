"""Pre-signed uploads to S3-compatible storage (MinIO in dev). boto3 presigning is local
computation; network calls (HEAD) run in a thread so the event loop never blocks."""

import asyncio
import json
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import Settings

CONTENT_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
}


class MediaStore:
    def __init__(self, settings: Settings) -> None:
        cfg = Config(signature_version="s3v4", s3={"addressing_style": "path"})
        common: dict[str, Any] = {
            "region_name": settings.MEDIA_REGION,
            "aws_access_key_id": settings.MEDIA_ACCESS_KEY,
            "aws_secret_access_key": settings.MEDIA_SECRET_KEY,
            "config": cfg,
        }
        self._s3 = boto3.client("s3", endpoint_url=settings.MEDIA_S3_ENDPOINT, **common)
        self._presigner = boto3.client(
            "s3",
            endpoint_url=settings.MEDIA_PRESIGN_ENDPOINT or settings.MEDIA_S3_ENDPOINT,
            **common,
        )
        self.bucket = settings.MEDIA_BUCKET
        self.public_base = settings.MEDIA_PUBLIC_BASE_URL.rstrip("/")
        self.max_bytes = settings.MEDIA_MAX_BYTES

    def presign_put(self, key: str, content_type: str, expires: int = 600) -> str:
        url: str = self._presigner.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires,
        )
        return url

    def public_url(self, key: str) -> str:
        return f"{self.public_base}/{key}"

    async def head(self, key: str) -> tuple[int, str] | None:
        def _head() -> tuple[int, str] | None:
            try:
                meta = self._s3.head_object(Bucket=self.bucket, Key=key)
            except ClientError:
                return None
            return int(meta["ContentLength"]), str(meta.get("ContentType", ""))

        return await asyncio.to_thread(_head)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._s3.delete_object, Bucket=self.bucket, Key=key)

    async def dev_bootstrap(self) -> None:
        """Dev only (MEDIA_DEV_BOOTSTRAP): create the bucket and allow anonymous reads so
        file_url works without a CDN. In production the bucket and CDN are provisioned."""

        def _run() -> None:
            try:
                self._s3.create_bucket(Bucket=self.bucket)
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in (
                    "BucketAlreadyOwnedByYou",
                    "BucketAlreadyExists",
                ):
                    raise
            policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": ["*"]},
                        "Action": ["s3:GetObject"],
                        "Resource": [f"arn:aws:s3:::{self.bucket}/*"],
                    }
                ],
            }
            self._s3.put_bucket_policy(Bucket=self.bucket, Policy=json.dumps(policy))

        await asyncio.to_thread(_run)
