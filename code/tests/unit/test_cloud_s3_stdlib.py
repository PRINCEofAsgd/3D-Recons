"""不依赖第三方包的 S3 故障合同测试，全部对象为合成字节。"""

from __future__ import annotations

import hashlib
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from store_vision.cloud_s3 import S3ObjectStore, StorageUnavailable


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.data


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.partial = self.unavailable = False

    def __call__(self, request, timeout):
        path = urlsplit(request.full_url).path
        if self.unavailable:
            raise URLError("synthetic disconnection")
        if request.get_method() == "PUT":
            if request.get_header("If-none-match") == "*" and path in self.objects:
                raise HTTPError(request.full_url, 412, "exists", {}, None)
            self.objects[path] = request.data[:2] if self.partial else request.data
            return FakeResponse(b"")
        if path not in self.objects:
            raise HTTPError(request.full_url, 404, "missing", {}, None)
        return FakeResponse(self.objects[path])


class S3Tests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeS3()
        self.patch = patch("store_vision.cloud_s3.urlopen", self.fake)
        self.patch.start()
        self.store = S3ObjectStore("http://127.0.0.1:9000",
                                   "synthetic", "test-access", "test-secret", "phase2")
        self.store.scope = "attempts/synthetic/run1/attempt1"

    def tearDown(self):
        self.patch.stop()

    def test_immutable_object_and_manifest(self):
        row = self.store.put(b"synthetic-image", "image/jpeg")
        self.assertEqual(self.store.read(row), b"synthetic-image")
        self.assertEqual(self.store.put(b"synthetic-image", "image/jpeg"), row)
        uri = self.store.publish("runs", ("synthetic", "run1", "attempt1"), {"kind": "run"})
        self.assertEqual(self.store.load_manifest(uri), {"kind": "run"})
        with self.assertRaises(FileExistsError):
            self.store.publish("runs", ("synthetic", "run1", "attempt1"), {"kind": "other"})

    def test_bad_checksum_missing_object_and_foreign_prefix(self):
        row = self.store.put(b"synthetic", "application/octet-stream")
        wrong = dict(row, sha256=hashlib.sha256(b"other").hexdigest())
        with self.assertRaises(ValueError):
            self.store.read(wrong)
        with self.assertRaises(ValueError):
            self.store.read(dict(row, uri=row["uri"].replace("phase2", "foreign")))
        self.fake.objects.clear()
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.store.read(row)

    def test_partial_upload_and_temporary_unavailable(self):
        self.fake.partial = True
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.store.put(b"synthetic", "application/octet-stream")
        self.fake.partial = False
        row = self.store.put(b"another", "application/octet-stream")
        self.fake.unavailable = True
        with self.assertRaises(StorageUnavailable):
            self.store.put(b"third", "application/octet-stream")
        with self.assertRaises(StorageUnavailable):
            self.store.read(row)


if __name__ == "__main__":
    unittest.main()
