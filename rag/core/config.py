"""配置：环境变量 RAG_* 与可选 data_dir/config.toml（嵌套用 __ 分隔）。

三层来源，优先级从高到低：
1. **环境变量**（部署时用，适合密钥：`RAG_RERANK__API_KEY=...`）
2. **config.toml**（设置页保存的落盘位置）
3. **代码默认值**

TOML 路径跟随 data_dir 动态解析——早前硬编码相对字面量，导致
`--data /srv/x` 时写入与读取分处两个目录，配置静默失效。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict, TomlConfigSettingsSource

# 环境变量名（--data 在 Settings 构造前写入它，config.toml 位置据此解析）
DATA_DIR_ENV = "RAG_DATA_DIR"


def _toml_path() -> Path:
    """config.toml 的实际位置：始终跟随 data_dir，绝不与它脱节。"""
    return Path(os.environ.get(DATA_DIR_ENV) or "data") / "config.toml"


# ---------------------------------------------------------------- 存储

class StorageConfig(BaseModel):
    """存储后端。

    `local` 是默认：单机开箱即用，零外部依赖，本地 NVMe 也是最快的。
    `s3` 面向需要多进程/多机共享，或把存储与计算解耦的场景——
    可直接对接 **RustFS** / MinIO / AWS S3（都是 S3 兼容协议）。

    切换后端只需改这一个字段；LanceDB 表与原文归档会一起搬过去。
    """
    backend: Literal["local", "s3"] = "local"

    # OLTP 元数据（jobs / apikeys / kbs / documents）放哪。
    # LanceDB 是列存 + 版本快照的引擎：用它做点查与高频小写，实测每次写
    # 都留下一个版本和一个数据文件，于是「用得越久越慢」
    # （docs/PERF-CONCURRENCY-2026-10-05.md §2.6）。
    #   auto    —— SQLite 为空时从 LanceDB 旧表一次性导入，之后以 SQLite 为准
    #   sqlite  —— 直接用 SQLite（新库就是这个）
    #   lancedb —— **回退开关**：完全走改造前的读写路径
    # 旧表在 LanceDB 里保留不删，所以回退不需要恢复备份。
    meta_engine: Literal["auto", "sqlite", "lancedb"] = "auto"

    # ---- s3（RustFS / MinIO / AWS）----
    endpoint: str = ""            # 例如 http://127.0.0.1:9000
    bucket: str = "raggi"
    access_key: str = ""
    secret_key: str = ""
    # 即使服务端不校验，部分 SDK 也要求有值，给个默认省去调用方困惑
    region: str = "us-east-1"
    # 本地部署的 RustFS/MinIO 通常没有 TLS，必须显式允许 http
    allow_http: bool = True
    # bucket 内的路径前缀，便于一个 bucket 放多套环境
    prefix: str = ""


# ---------------------------------------------------------------- 模型

class EmbedConfig(BaseModel):
    provider: Literal["ollama", "openai", "huggingface", "custom"] = "custom"
    model: str = "bge-m3"
    base_url: str = "http://127.0.0.1:11434"
    api_key: str = ""
    # 向量维度必须与模型实际输出一致；不一致会导致写入全部失败
    dim: int = 1024
    batch: int = 64
    concurrency: int = 4
    normalize: bool = True
    device: str = "cpu"
    timeout: int = 60


class RerankConfig(BaseModel):
    """精排配置。

    早前**没有 base_url / api_key 字段**——即使拿到服务凭据也无处可填，
    且 `provider="api"` 分支直接返回 None（等于没实现）。
    现在补全并实现真实的 HTTP 精排。
    """
    enabled: bool = False
    provider: Literal["none", "api", "cross-encoder"] = "none"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    top_n: int = 8
    timeout: int = 30
    # 仅 cross-encoder（本地 sentence-transformers）使用
    device: str = "cpu"


class LLMConfig(BaseModel):
    provider: Literal["openai", "ollama", "custom"] = "custom"
    model: str = "qwen2.5-7b-instruct"
    base_url: str = "http://127.0.0.1:8000/v1"
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 1024
    timeout: int = 120


# ---------------------------------------------------------------- 检索

class RetrieveConfig(BaseModel):
    mode: Literal["hybrid", "vector", "fts"] = "hybrid"
    candidate_k: int = 50
    top_k: int = 8
    k_rrf: int = 60
    nprobes: int = 20
    refine_factor: int = 10
    window: int = 1
    use_jieba: bool = True
    query_rewrite: bool = False


# ---------------------------------------------------------------- 其它

class ParserConfig(BaseModel):
    default: Literal["auto", "docling", "pymupdf4llm", "unstructured",
                     "native"] = "auto"
    overrides: dict[str, str] = Field(default_factory=dict)
    ocr: bool = False


class FeaturesConfig(BaseModel):
    answer: bool = False
    query_logs: bool = False


class SplitConfig(BaseModel):
    """系统默认分块参数。

    **注意**：这不再是「继承链上的一级」。分块方案已简化为两级
    （知识库 → 文档），本配置只用于**新建知识库时预填**，
    知识库一旦建立就持有具体数值，不存在运行时的「继承全局」状态。
    """
    chunk_size: int = 512
    chunk_overlap: int = 64


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings,
        file_secret_settings,
    ):
        # 让 config.toml 真正生效（嵌套表映射到嵌套模型）。
        # 路径在**实例化时**按 data_dir 解析，而不是类定义期的硬编码字面量。
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=_toml_path()),
            dotenv_settings,
            file_secret_settings,
        )

    data_dir: Path = Path("data")
    host: str = "0.0.0.0"
    port: int = 8000
    token: str = ""
    cors_origins: list[str] = Field(default_factory=list)
    max_upload_mb: int = 50
    # 入库队列的并发 worker 数。入库是重操作，并发过高只会互相争抢
    # LanceDB 写锁与 embedding 服务，2 个足以让解析与向量化的 IO 重叠。
    ingest_workers: int = 2
    # 只读 API 进程数："auto"（默认，min(cpu,4)）或数字；1 = 单进程形态。
    # 曾经这个值只能靠 `--processes` / `RAG_API_PROCESSES` 传，写进
    # config.toml 会被 `extra="ignore"` 静默吞掉 —— 用户以为固定了并发度，
    # 实际每次启动又回到 auto。放进 Settings 后它和其它配置同源同优先级。
    api_processes: str = "auto"
    # 任务记录保留天数
    job_retention_days: int = 7
    # 限流：每分钟允许的写操作次数。0 = 关闭（单机自用的默认）。
    # 只挡**写**操作：检索是读多写少且代价低，挡它只会影响正常使用；
    # 而入库/编辑/删除才是真正吃资源、可能被滥用打爆队列的路径。
    rate_limit_writes_per_min: int = 0
    # 限流：单次突发容量（令牌桶容量）。设 0 表示与每分钟额度同值。
    rate_limit_burst: int = 0

    storage: StorageConfig = StorageConfig()
    embed: EmbedConfig = EmbedConfig()
    rerank: RerankConfig = RerankConfig()
    llm: LLMConfig = LLMConfig()
    retrieve: RetrieveConfig = RetrieveConfig()
    parser: ParserConfig = ParserConfig()
    split: SplitConfig = SplitConfig()
    features: FeaturesConfig = FeaturesConfig()

    @property
    def config_file(self) -> Path:
        """落盘位置。与启动读取路径保持一致（否则重启后配置失效）。"""
        return self.data_dir / "config.toml"

    @property
    def tmp_dir(self) -> Path:
        """上传中转目录。

        上传先落到这里，再由存储后端搬进归档（local 是同盘 rename，
        s3 是上传对象）。解析器需要本地文件时也用它做物化落点。
        """
        p = self.data_dir / "tmp"
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()


# ---------------------------------------------------------------- 落盘

def _toml_value(v) -> str:
    """序列化为 TOML 字面量。

    dict 必须写成 inline table（overrides = { ".pdf" = "docling" }），
    早前当成字符串序列化出 overrides = "{}"，重启时 pydantic 校验
    直接 ValidationError——即「保存的配置永远读不回来」。
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return '""'
    if isinstance(v, dict):
        return "{" + ", ".join(
            f"{_toml_key(k)} = {_toml_value(x)}" for k, x in v.items()) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_key(k) -> str:
    """TOML 裸键只允许字母数字与 _-，其余需引号（如 ".pdf"）。"""
    k = str(k)
    if k and all(c.isalnum() or c in "_-" for c in k):
        return k
    return '"' + k.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_config(s: Settings, path: Path | None = None) -> Path:
    """把当前生效配置写回 config.toml（设置页保存时调用）。

    **原子写**：先写同目录临时文件再 os.replace。早前直接 write_text，
    写到一半崩溃会留下截断的 config.toml，下次启动 Settings 校验直接失败
    ——配置页一次误操作就能让服务起不来。

    **权限 0600**：文件里含明文 token 与 storage.secret_key（S3 凭据），
    而同目录下的签名密钥早就做了收紧（见 signing._load_or_create_secret）。
    """
    path = path or s.config_file
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f'data_dir = {_toml_value(str(s.data_dir))}',
        f'host = {_toml_value(s.host)}',
        f'port = {s.port}',
        f'token = {_toml_value(s.token)}',
        f'cors_origins = {_toml_value(s.cors_origins)}',
        f'max_upload_mb = {s.max_upload_mb}',
        f'ingest_workers = {s.ingest_workers}',
        f'api_processes = {_toml_value(s.api_processes)}',
        f'job_retention_days = {s.job_retention_days}',
        f'rate_limit_writes_per_min = {s.rate_limit_writes_per_min}',
        f'rate_limit_burst = {s.rate_limit_burst}',
        "",
    ]
    for section, sub in (
        ("storage", s.storage),
        ("embed", s.embed), ("rerank", s.rerank), ("llm", s.llm),
        ("retrieve", s.retrieve), ("parser", s.parser), ("split", s.split),
        ("features", s.features),
    ):
        lines.append(f"[{section}]")
        for k, v in sub.model_dump().items():
            lines.append(f'{k} = {_toml_value(v)}')
        lines.append("")
    # 临时文件与目标同目录，保证 replace 是同分区的原子改名
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, path)
        # 已存在的文件不会被 chmod 生效（open 的 mode 只在创建时用），
        # 因此显式再收一次，让历史上以 0644 落盘的配置也收紧。
        path.chmod(0o600)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path
