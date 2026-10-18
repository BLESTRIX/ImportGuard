from botocore.exceptions import ClientError

from app.core.config import settings
from app.storage.s3 import client


def main():
    bucket = settings().s3_bucket
    try:
        client().head_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise
        client().create_bucket(Bucket=bucket)
    # No anonymous bucket policy; tolerate the already-private case.
    client().delete_bucket_policy(Bucket=bucket)
    print(f"Private bucket ready: {bucket}")


if __name__ == "__main__":
    main()
