from functools import lru_cache

import boto3
from botocore.config import Config

from app.core.config import settings


@lru_cache
def client():
    cfg = settings()
    return boto3.client(
        "s3",
        endpoint_url=cfg.s3_endpoint,
        aws_access_key_id=cfg.s3_access_key,
        aws_secret_access_key=cfg.s3_secret_key,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=30,
            retries={"max_attempts": 2},
            s3={"addressing_style": "path"},
        ),
    )


def put(key, fileobj, size):
    client().put_object(
        Bucket=settings().s3_bucket,
        Key=key,
        Body=fileobj,
        ContentLength=size,
        ContentType="application/octet-stream",
    )


def open_source(key):
    return client().get_object(Bucket=settings().s3_bucket, Key=key)["Body"]


def delete(key):
    client().delete_object(Bucket=settings().s3_bucket, Key=key)


def chunks(body):
    try:
        while data := body.read(64 * 1024):
            yield data
    finally:
        body.close()


def ready():
    client().head_bucket(Bucket=settings().s3_bucket)
