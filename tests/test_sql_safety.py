"""SQL 文本层的守卫：值转义、投影白名单、分层边界、死 schema、配置接线。

这一组测试盯的是**同一类缺陷的五个入口**，它们都在改造中真实出现过或被
审计点出来。共同点是「不出错、只是结果悄悄不对」，所以只能靠断言钉住：

1. 值：LanceDB 的 where 是字符串拼接，SQLite 是占位符 —— 双引擎分派下
   一条转义漏了就是注入或「莫名查不到」；
2. 列名：投影 `SELECT {', '.join(cols)}` 无法参数化，只能走白名单；
3. 分层：`sqlite3.connect` 只许出现在 `rag/storage/`，否则「换后端只改
   一层」这个前提就不成立（同类守卫已有 lancedb 的那条）；
4. schema：DDL 里建了没人用的表/列，会诱导后来人照着它写新代码；
5. 配置：写进 config.toml 却没人读的配置是**欺骗用户**，比没有配置更糟。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from rag.core.errors import Invalid
from rag.storage.meta import DDL, MetaStore
from rag.storage.repos import (
    docs_count,
    docs_query,
    get_documents,
    list_documents,
    upsert_documents,
)
from rag.storage.sql import only_cols

ROOT = Path(__file__).resolve().parent.parent

# 主机名里带引号与 SQL 片段：合法值，但必须**只**被当成字符串
HOSTILE_KB = "kb' OR 1=1--"
HOSTILE_TITLE = "标题' 带引号"


def _store(tmp_path, *, with_meta: bool):
    """建一个 store；`with_meta=False` 走 LanceDB 回退路径。

    刻意不经 API/入库：这里要测的是 SQL 文本层，用假 embedding 只会让
    失败原因变得难以归位。
    """
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    meta = MetaStore(tmp_path / "raggi.db") if with_meta else None
    if meta is not None:
        store.meta = meta
    return store


def _seed(store):
    """三篇文档：两篇在带引号的库里，一篇在正常库里。"""
    rows = []
    for i, (kb, title) in enumerate((
            (HOSTILE_KB, "覆盖率 100% 的计划"),
            (HOSTILE_KB, "a_b 下划线"),
            ("kb-normal", HOSTILE_TITLE))):
        rows.append({"doc_id": f"d{i}", "title": title, "kb_id": kb,
                     "text": f"正文 {i}", "char_count": 4, "chunk_count": 1,
                     "status": "ready", "mime": "text/plain",
                     "parser_engine": "native", "content_hash": f"h{i}",
                     "created_at": f"2026-01-0{i + 1}T00:00:00+00:00",
                     "meta": "{}", "stored_file": "", "error": None,
                     "source_uri": "", "updated_at": ""})
    upsert_documents(store, rows)
    return rows


# 两条引擎路径都要过：分派写成 if/else，最大的风险就是两支悄悄分叉
both = pytest.mark.parametrize("with_meta", [True, False],
                               ids=["sqlite", "lancedb"])


@both
def test_injection_looking_value_matches_nothing(tmp_path, with_meta):
    """把 `' OR 1=1--` 当**值**传进去，不许变成条件。"""
    store = _store(tmp_path, with_meta=with_meta)
    _seed(store)
    assert docs_count(store, eq={"kb_id": "x' OR 1=1--"}) == 0
    assert docs_query(store, ["doc_id"], eq={"kb_id": "x' OR 1=1--"}) == []
    # 而真正的值（含引号）必须原样命中 —— 转义不能把合法数据也弄丢
    hit = docs_query(store, ["doc_id"], eq={"kb_id": HOSTILE_KB})
    assert len(hit) == 2, f"含引号的合法值查不到：{len(hit)}"


@both
def test_title_with_quote_round_trips(tmp_path, with_meta):
    """写入含引号的标题 → 列表/详情/批量取都要能读回来。

    这条比上一条更容易被漏掉：注入测试只管「攻不进来」，
    而转义做过头会让**合法数据查不出来**（用户看到「文档存在但搜不到」，
    且全程没有任何报错）。
    """
    store = _store(tmp_path, with_meta=with_meta)
    _seed(store)
    rows = docs_query(store, ["doc_id"], contains=("title", "带引号"))
    assert [r["doc_id"] for r in rows] == ["d2"]
    got = get_documents(store, ["d2"], cols=["doc_id", "title"])
    assert got["d2"]["title"] == HOSTILE_TITLE
    listed = list_documents(store, q="带引号", limit=10)[0]
    assert [d["doc_id"] for d in listed] == ["d2"]


@both
def test_like_wildcards_stay_literal(tmp_path, with_meta):
    """子串过滤里 `%` / `_` 必须是字面量。

    DataFusion 的 LIKE/ILIKE 与 SQLite 的 LIKE 都把 `%`（任意串）和
    `_`（任意单字符）当通配符。不转义时「包含 100%」会退化成
    「以 100 结尾的任意内容」，而下推之后的语义变化**不会报错**。
    """
    store = _store(tmp_path, with_meta=with_meta)
    _seed(store)
    # 「100%」只该命中那一篇，不该命中「a_b 下划线」
    hit = {r["doc_id"] for r in
           docs_query(store, ["doc_id"], contains=("title", "100%"))}
    assert hit == {"d0"}, f"% 被当成通配符：{hit}"
    hit2 = {r["doc_id"] for r in
            docs_query(store, ["doc_id"], contains=("title", "a_b"))}
    assert hit2 == {"d1"}, f"_ 被当成通配符：{hit2}"


@both
def test_projection_cannot_reach_unlisted_columns(tmp_path, with_meta):
    """投影是**唯一**拼进 SQL 文本的部分，必须过白名单。

    列名做成占位符在 SQL 里不合法，所以只能白名单。漏了这一道，
    `cols=["doc_id, (SELECT secret FROM …)"]` 这类输入就能改写查询本体。
    """
    store = _store(tmp_path, with_meta=with_meta)
    _seed(store)
    with pytest.raises(Invalid):
        docs_query(store, ["doc_id, (SELECT 1)"])
    # 合法子集照常工作
    assert len(docs_query(store, ["doc_id", "title"])) == 3


def test_only_cols_skips_when_schema_unknown():
    """列元数据取不到（表还没建）时**放行**而不是全打挂。"""
    assert only_cols(["a", "b"], set(), table="t") == ["a", "b"]
    assert only_cols(["a"], {"a", "b"}, table="t") == ["a"]


# ---- 分层与 schema ------------------------------------------------------


def test_sqlite_connect_only_inside_storage():
    """`sqlite3.connect` 只许出现在 `rag/storage/`。

    与已有的 `test_no_lancedb_connection_outside_storage` 同一条纪律：
    连接参数（WAL、busy_timeout、按线程隔离）散到业务层之后，
    「换后端只改一层」就不成立了 —— 而且散出去的那份通常没有 pragma。
    """
    banned = re.compile(r"sqlite3\.connect")
    offenders = []
    for f in sorted((ROOT / "rag").rglob("*.py")):
        rel = str(f.relative_to(ROOT))
        if rel.startswith("rag/storage/"):
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if banned.search(line.split("#", 1)[0]):
                offenders.append(f"{rel}:{i}")
    assert not offenders, "storage/ 之外直连 SQLite: " + "; ".join(offenders)


def test_no_dead_tables_in_meta_ddl():
    """DDL 里每张表都要有代码引用。

    这一条是**被审计逼出来的**：DDL 曾预留 `idempotency` / `outbox` /
    `jobs.worker` 三处「以后用得上」的结构，实现最后落在别处，于是它们
    成了零调用点的死 schema —— 而死 schema 比死代码更坏，它看起来像
    契约，会诱导下一个人在上面写代码。
    """
    tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", DDL))
    assert tables, "DDL 解析失败"
    # 把 DDL 字面量本身从 meta.py 里摘掉再搜：否则「表名出现在 DDL 里」
    # 就等于「有人用」，这条测试会永远绿、也永远拦不住死 schema。
    meta_src = (ROOT / "rag" / "storage" / "meta.py").read_text(
        encoding="utf-8")
    meta_code = re.sub(r'DDL = """.*?"""', "", meta_src, flags=re.S)
    others = [meta_code] + [str(f) for f in (ROOT / "rag").rglob("*.py")
                            if "storage/meta.py" not in str(f)]
    corpus = "\n".join(others)
    dead = [t for t in sorted(tables)
            if not re.search(rf"\b{t}\b", corpus)]
    assert not dead, f"DDL 里存在无人使用的表：{dead}"


def test_ddl_columns_match_their_column_lists():
    """列清单必须与 DDL 完全一致 —— 迁移漏列事故的结构性防线。

    真实事故：`DOC_COLS` 漏了 `text`，自动迁移把每篇文档的正文写成空串，
    而「行数一致」的校验全绿。这里做的是**结构核对**，不依赖跑迁移：
    少一列、多一列都会在这里先红。
    """
    from rag.storage.meta import DOC_COLS, KB_COLS, KEY_COLS

    def ddl_cols(table: str) -> set[str]:
        m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);",
                      DDL, re.S)
        assert m, f"DDL 里找不到 {table}"
        names = []
        for line in m.group(1).splitlines():
            line = line.strip().rstrip(",")
            if not line or line.startswith("--"):
                continue
            head = line.split()[0].upper()
            if head in ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN"):
                continue
            names.append(line.split()[0])
        return set(names)

    for table, cols in (("documents", DOC_COLS), ("kbs", KB_COLS),
                        ("apikeys", KEY_COLS)):
        assert set(cols) == ddl_cols(table), (
            f"{table}：列清单与 DDL 不一致，"
            f"缺 {set(ddl_cols(table)) - set(cols)} / 多 "
            f"{set(cols) - set(ddl_cols(table))}")


def test_lance_models_and_sqlite_columns_agree():
    """同一张表在两个引擎里各有一份形状声明，它们**必须**完全一致。

    `documents/jobs/kbs/apikeys` 的形状现在写在三处：
    `storage/schema.py` 的 LanceModel、`storage/meta.py` 的 DDL、
    以及 `meta.py` 里那份列清单。DDL ↔ 列清单由上一条测试核对，
    而 **LanceModel ↔ 列清单** 这条边过去没有任何机械检查 ——
    偏偏它正是自动迁移读的那条边：LanceModel 多一个字段，
    `import_from_lance` 就会**静默不搬**它（因为按列清单取投影）。

    这不是假想的：迁移丢 `text` 那次事故就是「列清单落后于真实形状」，
    当时行数列都对了，正文全空。
    """
    from rag.storage.meta import DOC_COLS, JOB_COLS, KB_COLS, KEY_COLS
    from rag.storage.schema import ApiKey, Document, Job, KnowledgeBase

    pairs = ((Document, DOC_COLS, "documents"), (Job, JOB_COLS, "jobs"),
             (KnowledgeBase, KB_COLS, "kbs"), (ApiKey, KEY_COLS, "apikeys"))
    for model, cols, name in pairs:
        fields = set(model.model_fields)
        assert fields == set(cols), (
            f"{name}：LanceModel 有而列清单没有 "
            f"{sorted(fields - set(cols))}（迁移会静默丢掉它们），"
            f"列清单有而 LanceModel 没有 {sorted(set(cols) - fields)}")


# ---- 配置接线 ----------------------------------------------------------


def test_no_sql_text_outside_storage_layer():
    """分层不变量：SQL 文本工具只能在 `rag/storage/` 里出现。

    这是一条**棘轮**，而且已经还清了。2026-10-06 审计时它还带着三个例外
    （`api/chunks.py`、`retrieval/answer.py`、`retrieval/search.py` 各自
    拼 where），因为 chunks 表没有意图型读接口；B 档把
    `chunk_filters` / `chunks_page` / `chunk_prefilter` / `search_chunks`
    补进 `repos/chunks.py` 之后，例外清单可以清空。

    为什么值得钉住：绕过仓储层拼 SQL 的后果**不是立刻出错**，而是
    ① 转义漏一处就是注入或「莫名查不到」（本项目真出过），
    ② 同一份过滤条件在两个地方各写一遍，迟早悄悄分叉
    （`answer.py` 就自己抄了一份 `chunk_id IN (...)`，而仓储层早就有
    `texts_by_id` 做同一件事——那份副本存在期间，没人发现它是死的）。

    为什么不禁「一切跨层导入」：`now_iso` / `scalar` 这类纯工具无害，
    真正的风险只在「自己拼 SQL 文本」。
    """
    helper = re.compile(r"\b(escape_sql|escape_like|scalar_rows|fetch_rows|"
                        r"count_rows|quote_in|only_cols)\b")
    found = []
    for f in sorted((ROOT / "rag").rglob("*.py")):
        rel = str(f.relative_to(ROOT))
        if rel.startswith("rag/storage/"):
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"\s*(from|import)\s", line) and "storage" in line \
                    and helper.search(line):
                found.append(f"{rel}:{n}")
    assert not found, (
        f"这些位置又开始自己拼 SQL 了：{found}。正确做法是在 "
        "rag/storage/repos/ 里加意图型函数"
        "（参考 docs_query / chunks_page：传意图，不传 SQL 文本）")


def test_engine_dispatch_has_exactly_one_implementation():
    """「该读哪个引擎」这个问题的实现必须只有一处。

    改造期间四个仓储模块各写了一份 `def _meta(store): return getattr(store,
    "meta", None)`。形状一致，但没有强制 —— 任何一处写成 `store.meta` 直接
    访问、或把属性名打错一个字母，都会**静默走回退路径**，
    而「静默走回退」正是无鉴权事故与分裂读事故的共同根因
    （见 docs/ARCH-AUDIT-2026-10-06.md §1.1）。

    这条测试不防止「分派逻辑写错」，它防止的是**分派逻辑被复制**：
    复制之后，改一处漏三处就只是时间问题。
    """
    repos = ROOT / "rag" / "storage" / "repos"
    engine_src = (repos / "_engine.py").read_text(encoding="utf-8")
    assert re.search(r"^def meta_of\(", engine_src, re.M), \
        "_engine.py 里必须就是那个唯一的分派实现"

    # 除 _engine.py 之外，任何仓储模块都不许自带一份分派
    copies = sorted(p.name for p in repos.glob("*.py")
                    if p.name != "_engine.py"
                    and re.search(r"^(def _meta\(|\s*def meta_of\()",
                                  p.read_text(encoding="utf-8"), re.M))
    assert copies == [], (
        f"又出现了本地的引擎分派副本：{copies}；请改成 "
        "from ._engine import meta_of as _meta")

    # 而且所有仓储模块都必须真的引用它（漏一个就等于那条路径没分派）
    for name in ("documents.py", "jobs.py", "kbs.py", "keys.py"):
        src = (repos / name).read_text(encoding="utf-8")
        assert "meta_of as _meta" in src, f"{name} 没有走统一分派"
        assert 'getattr(store, "meta"' not in src, f"{name} 绕过分派点自己取"


def test_settings_fields_are_actually_read():
    """Settings 里每个顶层字段都要在业务代码里被读到。

    判据来自一次真实缺陷：`job_retention_days` 写进了 config.toml，
    但队列用的是模块常量 `JOB_RETENTION_DAYS` —— 用户改成 1 天，记录照样
    留 7 天。这类 bug 的特征是**代码全绿、配置在骗人**，只能这样兜。

    已知例外写在白名单里并注明理由，避免「白名单」变成万能借口。
    """
    from rag.core.config import Settings

    sources = "\n".join(f.read_text(encoding="utf-8")
                        for f in (ROOT / "rag").rglob("*.py"))
    # 这几个由 config.py 自身的读写函数使用（落盘/回读），不是业务读取点
    excused = {"cors_origins", "max_upload_mb", "host", "port", "token",
               "data_dir", "config_file"}
    unwired = []
    for name in Settings.model_fields:
        if name in excused:
            continue
        if not re.search(rf"\.{name}\b", sources):
            unwired.append(name)
    assert not unwired, f"这些配置写进 config.toml 却没人读：{unwired}"


def test_job_retention_days_reaches_the_queue(tmp_path):
    """具体到那条被审计点出来的链路：配置 → 队列 → 清理窗口。"""
    from rag.core.config import Settings
    from rag.ingest.queue import IngestQueue

    s = Settings()
    s.data_dir = tmp_path
    s.job_retention_days = 3
    store = _store(tmp_path / "store", with_meta=False)
    q = IngestQueue(store, workers=1, retention_days=s.job_retention_days)
    assert q.retention_days == 3


def test_api_processes_is_a_settings_field():
    """`--processes` / `RAG_API_PROCESSES` / config.toml 三条路同源。

    改前 `--processes` 在 argv 里就地解析并**绕过** Settings，
    于是 config.toml 里的值被 `extra="ignore"` 静默吞掉。
    """
    from rag.core.config import Settings

    assert "api_processes" in Settings.model_fields
    assert Settings().api_processes == "auto"
