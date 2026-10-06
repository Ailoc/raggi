"""领域异常。

**为什么要有这一层**：早前业务层直接抛 HTTPException，或用裸 Exception
让 API 层去猜。结果是「SQL 语法错误」「向量维度失配」都被包装成
「embedding 服务不可用？」——排障被彻底带偏（这是实测踩过的坑）。

现在约定：
- 领域层只抛本模块定义的异常，语义明确；
- API 层在**一处**把它们映射成 HTTP 状态码（见 api/errors.py）；
- 任何未归类的异常都落到 `RaggiError`，映射为 500 且保留原始摘要，
  不再假装是模型服务的问题。
"""
from __future__ import annotations


class RaggiError(Exception):
    """所有领域异常的基类。API 层据此决定 HTTP 状态码。"""
    status_code = 500


class NotFound(RaggiError):
    """对象不存在（文档 / 分块 / 知识库 / 任务 / 密钥）。"""
    status_code = 404


class Invalid(RaggiError):
    """调用方输入有问题：参数越界、格式错误、前置条件不满足。"""
    status_code = 400


class Conflict(RaggiError):
    """状态冲突：例如对已被占用的资源做操作。"""
    status_code = 409


class EditConflict(RaggiError):
    """并发编辑冲突：读取之后这一行已被别人改过。

    映射为 409 而非静默覆盖——两个人同时编辑同一个分块时，后提交
    的那份会把前一份整个抹掉，而前者完全不知情。
    """
    status_code = 409


class Unavailable(RaggiError):
    """依赖服务不可用（embedding / LLM / rerank 服务未就绪）。

    这是**唯一**会被映射为 503 的类别——503 的含义是「稍后重试可能成功」，
    不该把参数错误或代码缺陷也算进来。
    """
    status_code = 503


class QueueFull(RaggiError):
    """入库队列已满。快速失败好过无限堆积。

    503 而不是 429：429 归**策略限流**（`rate_limit_writes_per_min`），
    503 归**容量背压**（队列满了）。两者对客户端的含义不同 —— 前者是
    「你打得太频繁」，后者是「我在忙，稍后再来」。混用会让人无法从
    状态码判断该降频还是该等。
    `Retry-After` 给一个明确的退避提示，配合 `wait=false` 的异步用法。
    """
    status_code = 503
    headers = {"Retry-After": "30"}


class AuthFailed(RaggiError):
    """鉴权失败（401）或权限不足（403）。"""
    status_code = 401

    def __init__(self, message: str, *, status_code: int = 401,
                 headers: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}


def short(exc: BaseException, limit: int = 400) -> str:
    """异常的可读摘要：取首行并截断。

    多行异常（如 Arrow 的错误会把整段上下文拼进去）直接回传会把响应
    撑得很大；取首行既保留原因又不淹没调用方。
    """
    msg = str(exc).strip() or exc.__class__.__name__
    return msg.splitlines()[0][:limit]
