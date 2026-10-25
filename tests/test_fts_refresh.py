"""分块编辑后的 FTS 刷新：小库同步、大库后台。

一次编辑触发**全量** FTS 重建：5 万块以下原先每次都在请求线程里重建
（秒级），既卡住这个请求，又和入库抢跨进程写锁。现在只有小库同步重建
（几十毫秒，保证"改完立刻搜得到"），大库只标 stale、交给后台去抖。
"""
from __future__ import annotations

from pathlib import Path

from rag.storage import tables
from rag.storage.tables import LanceStore

CHUNK_EDIT = Path(__file__).resolve().parents[1] / "rag" / "chunk_edit.py"


def _store(tmp_path) -> LanceStore:
    return LanceStore.for_data_dir(tmp_path, 4)


def test_small_table_rebuilds_inline(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(tables, "FTS_SYNC_MAX_ROWS", 500)
    sync: list[dict] = []
    bg: list[dict] = []
    monkeypatch.setattr(store, "ensure_fts_index", lambda **kw: sync.append(kw))
    monkeypatch.setattr(store, "schedule_maintenance", lambda **kw: bg.append(kw))

    store.refresh_fts_after_edit()

    assert sync == [{"force": True}], "小库必须立刻重建，否则改完搜不到"
    assert bg == []


def test_large_table_defers_to_background(tmp_path, monkeypatch):
    store = _store(tmp_path)
    # 阈值调到 -1：空库（0 行）也算"大库"，稳定走到后台分支
    monkeypatch.setattr(tables, "FTS_SYNC_MAX_ROWS", -1)
    sync: list[dict] = []
    bg: list[dict] = []
    monkeypatch.setattr(store, "ensure_fts_index", lambda **kw: sync.append(kw))
    monkeypatch.setattr(store, "schedule_maintenance", lambda **kw: bg.append(kw))

    store.refresh_fts_after_edit()

    assert sync == [], "大库不该在请求线程里全量重建 FTS"
    assert bg == [{"debounce_seconds": 1.0, "force_fts": True}]


def test_chunk_edit_paths_all_go_through_the_helper():
    """结构守卫：三条编辑路径都走同一个刷新入口，别再有谁偷偷同步重建。"""
    src = CHUNK_EDIT.read_text(encoding="utf-8")
    assert "ensure_fts_index(force=True)" not in src, \
        "分块编辑仍在请求里全量重建 FTS"
    assert src.count("store.refresh_fts_after_edit()") == 3, \
        "新增/单条编辑/批量编辑三条路径必须共用同一个刷新入口"
