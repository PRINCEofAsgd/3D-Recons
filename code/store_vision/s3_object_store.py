"""S3 兼容对象存储；对象与清单均使用带前缀的不可变键。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


class StorageUnavailable(RuntimeError):
    """连接失败或服务端暂态错误，执行器可以有限重试。"""


class S3ObjectStore:
    """使用 AWS Signature V4 的最小 path-style S3 客户端。"""

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str,
                 prefix: str = "store-vision", region: str = "us-east-1"):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
            raise ValueError("S3 endpoint 必须是 http(s) 主机地址")
        if not bucket or not all(c.isalnum() or c in ".-" for c in bucket):
            raise ValueError("S3 bucket 非法")
        parts = prefix.strip("/").split("/")
        if not parts or any(not p or not all(c.isalnum() or c in "-_" for c in p) for p in parts):
            raise ValueError("S3 前缀非法")
        if not access_key or not secret_key:
            raise ValueError("S3 凭据缺失")
        self.endpoint, self.bucket = endpoint.rstrip("/"), bucket
        self.access_key, self.secret_key = access_key, secret_key
        self.prefix, self.region = "/".join(parts), region
        self.scope = "unscoped"

    @classmethod
    def from_env(cls) -> "S3ObjectStore":
        """凭据只从环境变量获取，不进入仓库和命令行参数。"""
        return cls(os.environ["SV_S3_ENDPOINT"], os.environ["SV_S3_BUCKET"],
                   os.environ["SV_S3_ACCESS_KEY"], os.environ["SV_S3_SECRET_KEY"],
                   os.getenv("SV_S3_PREFIX", "store-vision"), os.getenv("SV_S3_REGION", "us-east-1"))

    def _key(self, suffix: str) -> str:
        """所有资源键强制落在配置前缀内。"""
        if not suffix or any(p in {"", ".", ".."} for p in suffix.split("/")):
            raise ValueError("对象键非法")
        return f"{self.prefix}/{suffix}"

    def _uri(self, key: str) -> str:
        """对外只给可移植 URI，不暴露 endpoint。"""
        return f"s3://{self.bucket}/{key}"

    def _uri_key(self, uri: str) -> str:
        """读取时核对 bucket、前缀与路径结构。"""
        parsed = urlsplit(uri)
        if (parsed.scheme != "s3" or parsed.netloc != self.bucket or parsed.query or parsed.fragment
                or not parsed.path.startswith(f"/{self.prefix}/")):
            raise ValueError("对象 URI 不属于当前 bucket/前缀")
        key = parsed.path[1:]
        if any(p in {"", ".", ".."} for p in key.split("/")):
            raise ValueError("对象 URI 路径非法")
        return key

    def _request(self, method: str, key: str, body: bytes = b"", *, absent: bool = False) -> bytes:
        """签名完整请求体；条件写只保护单个对象键的创建。"""
        moment = datetime.now(timezone.utc)
        stamp, day = moment.strftime("%Y%m%dT%H%M%SZ"), moment.strftime("%Y%m%d")
        path = "/" + quote(self.bucket + "/" + key, safe="/-_.~")
        host = urlsplit(self.endpoint).netloc
        digest = hashlib.sha256(body).hexdigest()
        headers = {"host": host, "x-amz-content-sha256": digest, "x-amz-date": stamp}
        if absent:
            headers["if-none-match"] = "*"
        names = ";".join(sorted(headers))
        canonical = method + "\n" + path + "\n\n" + "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)) + "\n" + names + "\n" + digest
        scope = f"{day}/{self.region}/s3/aws4_request"
        signing = "AWS4-HMAC-SHA256\n" + stamp + "\n" + scope + "\n" + hashlib.sha256(canonical.encode()).hexdigest()
        key_bytes = ("AWS4" + self.secret_key).encode()
        for component in (day, self.region, "s3", "aws4_request"):
            key_bytes = hmac.new(key_bytes, component.encode(), hashlib.sha256).digest()
        signature = hmac.new(key_bytes, signing.encode(), hashlib.sha256).hexdigest()
        headers["Authorization"] = f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope},SignedHeaders={names},Signature={signature}"
        request = Request(self.endpoint + path, data=body if method == "PUT" else None,
                          method=method, headers=headers)
        try:
            with urlopen(request, timeout=20) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code == 409 and absent:
                raise StorageUnavailable("S3 条件写发生并发冲突") from exc
            if exc.code in {408, 429, 500, 502, 503, 504}:
                raise StorageUnavailable(f"S3 暂时不可用：HTTP {exc.code}") from exc
            if exc.code == 404:
                raise ValueError(f"对象不存在：{self._uri(key)}") from exc
            if exc.code == 412:
                raise FileExistsError(f"对象键已存在：{self._uri(key)}") from exc
            raise ValueError(f"S3 请求失败：HTTP {exc.code}") from exc
        except URLError as exc:
            raise StorageUnavailable("S3 连接失败") from exc

    def put(self, data: bytes, media_type: str) -> dict:
        """内容哈希与 attempt 前缀共同隔离重复执行。"""
        digest = hashlib.sha256(data).hexdigest()
        key = self._key(f"{self.scope}/objects/{digest[:2]}/{digest}")
        try:
            self._request("PUT", key, data, absent=True)
        except FileExistsError:
            pass
        if self._request("GET", key) != data:
            raise ValueError("上传后对象大小或 SHA-256 不符")
        return {"uri": self._uri(key), "bytes": len(data), "sha256": digest, "media_type": media_type}

    def read(self, resource: dict) -> bytes:
        """下载后重新核对大小和 SHA-256，不信任 ETag。"""
        uri, digest = resource.get("uri"), resource.get("sha256")
        if not isinstance(uri, str) or not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("资源描述非法")
        key = self._uri_key(uri)
        if not key.endswith("/" + digest):
            raise ValueError("对象键与 SHA-256 不一致")
        data = self._request("GET", key)
        if type(resource.get("bytes")) is not int or len(data) != resource["bytes"] or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("对象大小或 SHA-256 不符")
        if not isinstance(resource.get("media_type"), str) or not resource["media_type"]:
            raise ValueError("资源媒体类型缺失")
        return data

    def publish(self, kind: str, identity: tuple[str, ...], manifest: dict) -> str:
        """调用方完成全量校验后条件创建清单；S3 仅保证单键条件写。"""
        if kind not in {"snapshots", "runs", "artifacts", "published"}:
            raise ValueError("未知清单类型")
        if any(not p or len(p) > 64 or not all(c.isalnum() or c in "-_" for c in p) for p in identity):
            raise ValueError("清单身份非法")
        key = self._key("manifests/" + kind + "/" + "/".join(identity) + "/manifest.json")
        data = (json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()
        self._request("PUT", key, data, absent=True)
        return self._uri(key)

    def load_manifest(self, uri: str) -> dict:
        """仅加载当前命名空间下的 JSON 清单。"""
        key = self._uri_key(uri)
        if "/manifests/" not in key or not key.endswith("/manifest.json"):
            raise ValueError("不是清单 URI")
        return json.loads(self._request("GET", key))

    def manifest_uri(self, kind: str, *identity: str) -> str:
        """由身份确定稳定清单地址，供控制面保存。"""
        return self._uri(self._key("manifests/" + kind + "/" + "/".join(identity) + "/manifest.json"))

    def create_bucket(self) -> None:
        """本地首次启动时建桶；已有桶保留。"""
        try:
            self._request("PUT", "")
        except ValueError as exc:
            if "HTTP 409" not in str(exc):
                raise
