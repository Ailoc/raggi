"""手写 TTL 缓存的直接单测。

为什么单独一组：`rag/core/cache.py` 是**手写的并发数据结构**，而且站在
两条关键路径上 —— 鉴权（`repos/keys.py` 的两张缓存）与观测端点
（`api/system.py` 的快照）。改造前它只有间接覆盖（跑 API 时顺带用到），
而它的每一条注释都在声称一个具体的并发/过期保证。手写件的保证必须有
直接断言，否则改坏的人不会知道，用的人更不会。

覆盖四条承诺：
1. TTL 到期即失效，且**每项用自己的 ttl**（不是缓存的默认值）；
2. 上界真的成立（并发写也不突破 `max_items`），满时丢最久未用的；
3. 线程安全：并发读出不丢条目、不死锁；
4. `loader` 在锁**外**跑 —— 这条是性能承诺，慢 loader 不许把别的请求串起来。
"""
from __future__ import annotations

import threading
import time

from rag.core.cache import TTLCache


def test_entry_expires_by_its_own_ttl():
    """同一张缓存里允许不同寿命的条目：`set(k, v, ttl=…)` 必须按项生效。

    这是改造前的真实缺陷形式：条目里只存「截止时刻」，`age_ms()` 却用
    缓存默认 ttl 反推年龄 —— 于是任何自定义 ttl 的项都报出错误的年龄，
    而这个数字是要显示给用户看的（`/api/health` 的「N 秒前的数据」）。
    """
    c = TTLCache(max_items=8, ttl_seconds=100.0)
    c.set("short", "v", ttl=0.05)
    c.set("long", "v")
    assert c.get("short") == "v" and c.get("long") == "v"
    time.sleep(0.06)
    assert c.get("short") is None, "自定义 ttl 到期后必须失效"
    assert c.get("long") == "v", "默认 ttl 不该被邻居的短 ttl 带偏"


def test_age_ms_measures_wall_age_not_default_ttl():
    c = TTLCache(max_items=4, ttl_seconds=60.0)
    c.set("k", 1, ttl=5.0)
    time.sleep(0.02)
    age = c.age_ms("k")
    assert age is not None
    # 断言的不是 20ms 这个数，而是**量级**：错误实现会算出
    # (now - (deadline - 60)) ≈ 60000ms，把 20ms 前写的快照说成一分钟前。
    assert age < 1000, f"age_ms 被默认 ttl 污染：{age}"
    assert c.age_ms("nope") is None, "未命中要返回 None 而不是 0"


def test_expired_entry_is_evicted_not_resurrected():
    c = TTLCache(max_items=4, ttl_seconds=0.02)
    c.set("k", 1)
    time.sleep(0.03)
    assert c.get("k") is None
    # 过期项要在读路径上就地删除，否则长跑进程里它会一直占着上界额度
    assert len(c) == 0, f"过期条目没被清掉：{len(c)}"


def test_max_items_bound_holds_under_concurrent_writes():
    """并发写 200 个键，上界 16 必须一次都没被突破。

    裸 dict 的失效模式正是这个：单看每次都成功，并发下 `len()` 超界、
    甚至丢更新（本仓库改造前 `Embedder._one_cache` 就是裸 dict）。
    """
    c = TTLCache(max_items=16, ttl_seconds=60.0)
    seen_over = []
    stop = threading.Event()

    def writer(tid: int):
        for i in range(50):
            c.set(f"{tid}-{i}", i)
            with c._lock:                      # 只观测，不改变行为
                if len(c._data) > c.max_items:
                    seen_over.append(len(c._data))
            if stop.is_set():
                return

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not seen_over, f"上界被突破过：{seen_over[:5]}"
    assert len(c) <= 16, f"结束后仍超界：{len(c)}"


def test_lru_eviction_drops_least_recently_used():
    c = TTLCache(max_items=2, ttl_seconds=60.0)
    c.set("a", 1)
    c.set("b", 2)
    assert c.get("a") == 1          # a 变成最近使用
    c.set("c", 3)                   # 该淘汰 b，而不是 a
    assert c.get("a") == 1, "刚读过的条目不该被淘汰"
    assert c.get("b") is None


def test_invalidate_and_clear_are_visible_immediately():
    """吊销/写库后必须**本进程立刻**生效：这是安全语义，不是性能语义。"""
    c = TTLCache(max_items=4, ttl_seconds=600.0)
    c.set(("store", "hash"), {"key_id": "k1"})
    assert c.get(("store", "hash")) is not None
    c.invalidate(("store", "hash"))
    assert c.get(("store", "hash")) is None
    c.set(("s", "h2"), 1)
    c.clear()
    assert c.get(("s", "h2")) is None and len(c) == 0


def test_keys_are_scoped_by_whatever_the_caller_passes():
    """键是元组时按整条元组区分。

    这条看着平凡，但 `repos/keys.py` 的越权事故就是「只按 key_hash 做键」：
    A 数据目录签发的密钥在 B 目录里也被判有效。缓存本身只能提供机制
    （键由调用方给），所以这里钉住机制，确保「带作用域」是可实现的。
    """
    c = TTLCache(max_items=8, ttl_seconds=60.0)
    c.set(("dirA", "hash"), "A 的密钥")
    assert c.get(("dirB", "hash")) is None, "跨作用域串了就是越权"
    assert c.get(("dirA", "hash")) == "A 的密钥"


def test_cached_distinguishes_stored_none():
    """`get` 用 None 表示未命中，所以「算出来就是 None」必须靠 `cached`。"""
    calls = []

    def loader():
        calls.append(1)
        return None

    c = TTLCache(max_items=4, ttl_seconds=60.0)
    assert c.cached("k", loader) is None
    assert c.cached("k", loader) is None
    assert len(calls) == 1, "None 也是有效值：TTL 内不该反复回源"


def test_cached_loader_runs_outside_the_lock():
    """慢 loader 不许把别的请求串起来 —— 这是这个类存在的性能理由。

    做法：线程 A 在 loader 里等事件；这期间线程 B 必须能读到**别的**条目
    并且能写入。若 loader 在锁内，B 会一直卡住，`done` 永远不为 True。
    """
    c = TTLCache(max_items=8, ttl_seconds=60.0)
    c.set("other", "visible")
    inside = threading.Event()
    release = threading.Event()
    done_b = threading.Event()

    def slow_loader():
        inside.set()
        release.wait(timeout=5)
        return "late"

    def a():
        c.cached("slow", slow_loader)

    def b():
        inside.wait(timeout=5)
        assert c.get("other") == "visible", "读被锁住了"
        c.set("written-by-b", 1)
        done_b.set()
        release.set()

    ta, tb = threading.Thread(target=a), threading.Thread(target=b)
    ta.start(); tb.start()
    ta.join(timeout=5); tb.join(timeout=5)
    assert done_b.is_set(), "loader 持锁了：并发请求被串起来"
    assert c.get("slow") == "late"


def test_concurrent_readers_do_not_lose_entries():
    c = TTLCache(max_items=64, ttl_seconds=60.0)
    for i in range(64):
        c.set(i, i)
    misses = []

    def reader():
        for _ in range(200):
            for i in range(64):
                if c.get(i) != i:
                    misses.append(i)

    ts = [threading.Thread(target=reader) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not misses, f"并发读把条目读没了（前 5 个）：{misses[:5]}"


# ---- Embedder.embed_one 的结果缓存（查询路径）--------------------------
# 这一组不是重复测 TTLCache，而是测**站在它上面的那条承诺**：
# 同一个模型 + 同一段文本不重复调远端，而换个模型必须重算。


class _CountingLC:
    def __init__(self):
        self.calls: list[int] = []
        self._lock = threading.Lock()

    def embed_documents(self, texts):
        with self._lock:
            self.calls.append(len(texts))
        return [[0.5] * 4 for _ in texts]


def _embedder(model="m1"):
    from rag.models.embeddings import Embedder

    lc = _CountingLC()
    return Embedder(lc, 4, model, False), lc


def test_embed_one_caches_repeat_and_scopes_by_model():
    emb, lc = _embedder("modelA")
    emb.embed_one("逆变器额定电压")
    emb.embed_one("逆变器额定电压")
    assert len(lc.calls) == 1, "同文本第二次必须命中缓存，不能再打远端"
    # 向量随模型变化：换模型后旧缓存必须失效，否则会拿到旧模型的向量
    emb.model = "modelB"
    emb.embed_one("逆变器额定电压")
    assert len(lc.calls) == 2, "换了模型还吃缓存 ⇒ 返回另一个模型空间的向量"


def test_embed_one_does_not_merge_inflight_identical_queries():
    """**记录现状，不是理想行为**：缓存不合并「正在飞」的同 key 请求。

    第 1 条请求卡在 provider 里时，第 2 条同样的文本不会等它的结果，
    而是**再打一次**（cache stampede）。串行则只 1 次（上一条测试）。
    这正是审计 §4.2 里被否证的 B6 条目：队列式合批不做，
    而 in-flight 合并是日后真要收益时的落点。

    所以这条测试**故意**断言「会重复调用」：将来谁实现了 in-flight 合并它会红，
    那时该做的是「拿真实流量确认收益 + 改断言 + 更新 §4.2 的决定」，
    而不是顺手把断言改小 —— 那等于偷偷推翻一个有记录的决定。

    为什么不用「8 个线程同时冲」：假 provider 瞬间返回，第 2..8 条其实命中了缓存，
    测出来的数取决于线程调度 —— 本条第一版就是这样写死的，实测拿不到 8。
    「卡住第一条」才是对「有没有 in-flight 合并」的确定性判定。
    """
    from rag.models.embeddings import Embedder

    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    class _BlockingLC:
        def embed_documents(self, texts):
            calls.append(len(texts))
            entered.set()
            release.wait(timeout=5.0)
            return [[0.5] * 4 for _ in texts]

    emb = Embedder(_BlockingLC(), 4, "m", False)
    t1 = threading.Thread(target=emb.embed_one, args=("同一个热点查询",))
    t1.start()
    assert entered.wait(timeout=2.0), "第 1 条请求没能进入 provider"
    t2 = threading.Thread(target=emb.embed_one, args=("同一个热点查询",))
    t2.start()
    time.sleep(0.3)          # 足够：命中缓存会立刻返回，没合并则会再进一次
    release.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    assert len(calls) == 2, (
        f"provider 被调了 {len(calls)} 次："
        "若这是有意的 in-flight 合并，请连同 §4.2 一起更新（并改断言）")
