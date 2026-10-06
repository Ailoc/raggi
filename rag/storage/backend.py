"""存储后端：把「数据放在哪」从业务逻辑里彻底剥离。

两种后端：

| 后端 | LanceDB 位置 | 原文归档 | 适用 |
|---|---|---|---|
| `local`（默认） | `data_dir/lancedb` | `data_dir/files/` | 单机开箱即用，本地 NVMe 最快 |
| `s3` | `s3://bucket/prefix/lancedb` | 同 bucket 的 `prefix/files/` | RustFS / MinIO / AWS，多进程或多机共享 |

**为什么默认 local**：单机场景引入对象存储只会增加「必须先跑一个服务」
的运维负担，而本地盘本来就更快。需要扩展时改一个配置字段即可切换。

**S3 兼容性要点**（接 RustFS / MinIO 时必须注意）：
- `endpoint` 必须显式给出，且本地部署多为 http → 需要 `allow_http`
- **path-style 寻址**：RustFS/MinIO 默认不支持 virtual-host 风格
  （`bucket.endpoint`），必须走 `endpoint/bucket/key`
- `region` 即使服务端不校验也要给，部分 SDK 会强校验

LanceDB 通过 `storage_options` 接收这些参数；原文归档走
`pyarrow.fs.S3FileSystem`（pyarrow 已是核心依赖，**不新增依赖**）。
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Protocol

from rag.core.errors import Invalid

logger = logging.getLogger("raggi.storage")

# 归档文件名的合法字符：服务端生成，但写进对象键前仍要校验，
# 避免数据库被写脏后拼出越界的键
_BAD_NAME = ("/", "\\")


def _check_name(name: str) -> str:
    if not name or name.startswith(".") or any(c in name for c in _BAD_NAME):
        raise Invalid(f"非法的归档文件名: {name!r}")
    return name


class Backend(Protocol):
    """存储后端接口。业务层只依赖这个协议。"""

    kind: str

    def lance_uri(self) -> str:
        """LanceDB 的连接地址。"""

    def lance_options(self) -> dict:
        """LanceDB 的 storage_options（local 为空 dict）。"""

    # ---- 原文归档 ----
    def put_blob(self, name: str, data: bytes) -> None: ...
    def read_blob(self, name: str) -> bytes: ...
    def blob_exists(self, name: str) -> bool: ...
    def delete_blob(self, name: str) -> None: ...

    # ---- 解析器物化 ----
    def local_path(self, name: str) -> Path | None:
        """若该归档在本地可直接读取，返回其路径；对象存储返回 None。

        路由层据此决定用 FileResponse（支持 Range，大 PDF 分段加载）
        还是回落到流式读取——调用方不必判断后端类型。
        """

    def materialize(self, name: str) -> Path:
        """给解析器一个可读的本地路径（S3 后端会先下载）。"""

    def release(self, path: Path) -> None:
        """释放 materialize 产生的临时文件（local 后端无操作）。"""

    def stats(self) -> dict:
        """容量统计（按后端给不同维度）。"""


# ---------------------------------------------------------------- local

class LocalBackend:
    """本地文件系统。默认后端，零外部依赖。"""

    kind = "local"

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.files_dir = self.data_dir / "files"
        self.files_dir.mkdir(parents=True, exist_ok=True)

    def lance_uri(self) -> str:
        return str(self.data_dir / "lancedb")

    def lance_options(self) -> dict:
        return {}

    def _path(self, name: str) -> Path:
        path = (self.files_dir / _check_name(name)).resolve()
        base = self.files_dir.resolve()
        # 双重保险：即使校验被绕过，resolve 后仍必须在 files_dir 内
        if path.parent != base:
            raise Invalid(f"归档路径越界: {name!r}")
        return path

    def put_blob(self, name: str, data: bytes) -> None:
        self._path(name).write_bytes(data)

    def read_blob(self, name: str) -> bytes:
        return self._path(name).read_bytes()

    def blob_exists(self, name: str) -> bool:
        return self._path(name).exists()

    def delete_blob(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)

    def move_in(self, src: Path, name: str) -> None:
        """把上传的临时文件搬进归档目录（同盘 rename，零拷贝）。"""
        Path(src).replace(self._path(name))

    def local_path(self, name: str) -> Path | None:
        path = self._path(name)
        return path if path.exists() else None

    def materialize(self, name: str) -> Path:
        # 本地后端本来就有真实路径，无需复制
        return self._path(name)

    def release(self, path: Path) -> None:  # noqa: ARG002
        return None

    def stats(self) -> dict:
        def _dir_bytes(p: Path) -> int:
            try:
                return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            except OSError:
                return -1

        out = {
            "backend": self.kind,
            "data_dir_bytes": _dir_bytes(self.data_dir),
            "lancedb_dir_bytes": _dir_bytes(self.data_dir / "lancedb"),
        }
        try:
            out["disk_free_bytes"] = shutil.disk_usage(
                str(self.data_dir)).free
        except OSError:
            out["disk_free_bytes"] = -1
        return out


# ---------------------------------------------------------------- s3

class S3Backend:
    """S3 兼容对象存储（RustFS / MinIO / AWS S3）。

    LanceDB 的 Rust 侧自带 object_store 客户端，不需要 boto3；
    原文归档用 pyarrow.fs（已是核心依赖）。
    """

    kind = "s3"

    def __init__(self, *, endpoint: str, bucket: str, access_key: str,
                 secret_key: str, region: str = "us-east-1",
                 allow_http: bool = True, prefix: str = "",
                 tmp_dir: Path | None = None):
        if not endpoint:
            raise Invalid("storage.backend=s3 时必须配置 storage.endpoint")
        if not bucket:
            raise Invalid("storage.backend=s3 时必须配置 storage.bucket")
        if not access_key or not secret_key:
            raise Invalid(
                "storage.backend=s3 时必须配置 storage.access_key / secret_key")
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region or "us-east-1"
        self.allow_http = allow_http
        self.prefix = (prefix or "").strip("/")
        self._tmp_root = Path(tmp_dir) if tmp_dir else Path(
            tempfile.mkdtemp(prefix="raggi-s3-"))
        self._tmp_root.mkdir(parents=True, exist_ok=True)
        self._fs = self._make_fs()

    # ---- 连接 ----

    def _make_fs(self):
        try:
            from pyarrow.fs import S3FileSystem
        except ImportError as e:  # pragma: no cover - 取决于 pyarrow 构建
            raise Invalid(
                "当前 pyarrow 未包含 S3 支持，无法使用 s3 后端。"
                "请安装带 S3 的 pyarrow（官方 wheel 默认包含）") from e
        return S3FileSystem(
            access_key=self.access_key,
            secret_key=self.secret_key,
            endpoint_override=self.endpoint,
            region=self.region,
            scheme="http" if self.allow_http else "https",
        )

    def _key(self, name: str) -> str:
        parts = [p for p in (self.prefix, "files", _check_name(name)) if p]
        return str(PurePosixPath(*parts))

    def lance_uri(self) -> str:
        parts = [p for p in (self.prefix, "lancedb") if p]
        return f"s3://{self.bucket}/{PurePosixPath(*parts)}"

    def lance_options(self) -> dict:
        """LanceDB 的 storage_options。

        lance 的 Rust 侧对同一概念接受多个键名（aws_* 与短名），
        这里都给出，避免因版本差异导致某项被忽略。
        """
        opts = {
            "aws_access_key_id": self.access_key,
            "aws_secret_access_key": self.secret_key,
            "aws_region": self.region,
            "aws_endpoint": self.endpoint,
            "endpoint": self.endpoint,
            "region": self.region,
            # 本地 RustFS/MinIO 通常无 TLS
            "allow_http": "true" if self.allow_http else "false",
            "aws_allow_http": "true" if self.allow_http else "false",
            # 必须 path-style：RustFS/MinIO 默认不支持 virtual-host 寻址
            "virtual_hosted_style_request": "false",
            "bucket": self.bucket,
        }
        return opts

    # ---- 归档 ----

    def put_blob(self, name: str, data: bytes) -> None:
        key = self._key(name)
        with self._fs.open_output_stream(key) as f:
            f.write(data)

    def read_blob(self, name: str) -> bytes:
        key = self._key(name)
        if not self.blob_exists(name):
            raise Invalid(f"归档文件不存在: {name}")
        with self._fs.open_input_stream(key) as f:
            return f.read()

    def blob_exists(self, name: str) -> bool:
        from pyarrow.fs import FileType

        info = self._fs.get_file_info(self._key(name))
        return info.type == FileType.File

    def delete_blob(self, name: str) -> None:
        try:
            self._fs.delete_file(self._key(name))
        except Exception as e:  # noqa: BLE001
            logger.debug("删除归档对象失败 %s: %s", name, e)

    def put_blob_from_path(self, src: Path, name: str) -> None:
        """从本地文件上传（上传接口用；避免把大文件读进内存）。"""
        self._fs.upload(self._key(name), str(src))

    # ---- 物化 ----

    def local_path(self, name: str) -> Path | None:
        # 对象存储没有本地路径——调用方会回落到流式读取
        return None

    def materialize(self, name: str) -> Path:
        """下载到本地临时目录，供只认本地路径的解析器使用。"""
        local = self._tmp_root / _check_name(name)
        self._fs.download(self._key(name), str(local))
        return local

    def release(self, path: Path) -> None:
        # 只删自己创建的临时文件，绝不碰用户指定的路径
        try:
            p = Path(path).resolve()
            root = self._tmp_root.resolve()
            if p.parent == root or root in p.parents:
                p.unlink(missing_ok=True)
        except OSError as e:  # noqa: BLE001
            logger.debug("清理临时文件失败 %s: %s", path, e)

    def stats(self) -> dict:
        from pyarrow.fs import FileSelector

        total = 0
        count = 0
        try:
            base = str(PurePosixPath(*[p for p in (self.prefix,) if p])) or ""
            for info in self._fs.get_file_info(
                    FileSelector(base, recursive=True)):
                if info.type.name == "File":
                    count += 1
                    total += int(info.size or 0)
        except Exception as e:  # noqa: BLE001
            logger.debug("统计 S3 对象失败: %s", e)
            return {"backend": self.kind, "error": str(e)[:200]}
        return {
            "backend": self.kind,
            "endpoint": self.endpoint,
            "bucket": self.bucket,
            "prefix": self.prefix,
            "object_count": count,
            "bucket_prefix_bytes": total,
        }


# ---------------------------------------------------------------- 工厂

def build_backend(cfg, data_dir: Path) -> Backend:
    """按配置构造后端。未知 backend 在**配置阶段**就报错。"""
    kind = (getattr(cfg, "backend", "local") or "local").lower()
    if kind == "local":
        return LocalBackend(data_dir)
    if kind == "s3":
        return S3Backend(
            endpoint=cfg.endpoint,
            bucket=cfg.bucket,
            access_key=cfg.access_key,
            secret_key=cfg.secret_key,
            region=cfg.region,
            allow_http=cfg.allow_http,
            prefix=cfg.prefix,
            tmp_dir=Path(data_dir) / "tmp",
        )
    raise Invalid(f"未知的存储后端: {kind!r}（可选 local / s3）")
