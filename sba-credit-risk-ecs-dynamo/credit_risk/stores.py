"""The two places this pipeline reads and writes, each with a folder stand-in for ``--local``.

    FolderStore  ->  S3Store         list, local_copy, version, move, delete, put_text
    JsonTable    ->  DynamoTable     put_item
    report_local ->  report_task     the validator's answer to the state machine

This is the only module in ``credit_risk`` that knows AWS exists, and even here boto3 is imported
inside the AWS classes, so the ``--local`` path and the test suite never import it. A port to
another cloud replaces this file and the two container entrypoints; the model and the
validation logic only ever see these six methods.

Keys are S3-style, ``/``-separated and relative to the store's root, on both paths. The folder
store maps a key onto ``<root>/<key>`` and nothing more, so a key that works locally is the key
the bucket will hold.
"""

import contextlib
import json
import os
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path


class FolderStore:
    def __init__(self, root):
        self.root = Path(root)

    def _path(self, key):
        return self.root.joinpath(*key.split("/"))

    def list(self, prefix):
        base = self._path(prefix.rstrip("/"))
        if not base.is_dir():
            return []
        keys = [p.relative_to(self.root).as_posix() for p in base.rglob("*") if p.is_file()]
        return sorted(keys)

    @contextlib.contextmanager
    def local_copy(self, key):
        yield self._path(key)

    def version(self, key):
        """What a key holds right now -- modification time and size -- or None if it is gone."""
        path = self._path(key)
        return (path.stat().st_mtime_ns, path.stat().st_size) if path.is_file() else None

    def move(self, source, target, version=None):
        """Move a key; given ``version``, only if it still holds it. False if it did not move."""
        # ponytail: check then rename, a window a second writer could hit; fine for one laptop.
        if version is not None and self.version(source) != version:
            return False
        destination = self._path(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self._path(source), destination)
        return True

    def delete(self, key):
        path = self._path(key)
        if path.is_file():
            path.unlink()

    def put_text(self, key, text):
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class S3Store:
    def __init__(self, bucket):
        import boto3

        self.bucket = bucket
        self.client = boto3.client("s3")

    def list(self, prefix):
        keys = []
        for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=prefix
        ):
            keys += [item["Key"] for item in page.get("Contents", [])
                     if not item["Key"].endswith("/")]
        return sorted(keys)

    @contextlib.contextmanager
    def local_copy(self, key):
        # Fargate's ephemeral storage, not memory: the largest SBA file is 319 MB on disk and
        # several times that as a frame, so it is downloaded rather than streamed into pandas.
        folder = tempfile.mkdtemp()
        path = Path(folder) / key.split("/")[-1]
        try:
            self.client.download_file(self.bucket, key, str(path))
            yield path
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def version(self, key):
        from botocore.exceptions import ClientError

        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)["ETag"]
        except ClientError as error:
            if error.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return None
            raise

    def move(self, source, target, version=None):
        """Copy then delete; given ``version``, the copy itself refuses an object that changed."""
        from botocore.exceptions import ClientError

        extra = {} if version is None else {"CopySourceIfMatch": version}
        try:
            self.client.copy({"Bucket": self.bucket, "Key": source}, self.bucket, target,
                             ExtraArgs=extra)
        except ClientError as error:
            if version is not None and error.response["Error"]["Code"] in (
                    "404", "NoSuchKey", "412", "PreconditionFailed"):
                return False
            raise
        # ponytail: an overwrite landing between copy and delete is deleted unread; bucket
        # versioning closes that gap if executions ever overlap.
        self.client.delete_object(Bucket=self.bucket, Key=source)
        return True

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def put_text(self, key, text):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=text.encode("utf-8"))


class JsonTable:
    """One JSON document standing in for a DynamoDB table keyed on (chain, item)."""

    def __init__(self, path):
        self.path = Path(path)
        self.items = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def put_item(self, item):
        for value in item.values():
            if isinstance(value, float):
                raise TypeError("DynamoDB rejects float; build items with to_item()")
        key = f"{item['chain']}|{item['item']}"
        self.items[key] = {k: str(v) if isinstance(v, Decimal) else v for k, v in item.items()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items, indent=1, sort_keys=True), encoding="utf-8")


class DynamoTable:
    def __init__(self, name):
        import boto3

        self.table = boto3.resource("dynamodb").Table(name)

    def put_item(self, item):
        self.table.put_item(Item=item)


def report_local(output):
    print(json.dumps(output, indent=2))
    return output


def report_task(token, output=None, error=None):
    """Answer a ``.waitForTaskToken`` state: success with the output, or failure with a cause."""
    import boto3

    client = boto3.client("stepfunctions")
    if error is None:
        client.send_task_success(taskToken=token, output=json.dumps(output))
    else:
        client.send_task_failure(taskToken=token, error="ValidatorCrashed", cause=error[:32_000])
