"""容量统计只遍历一次目录。

`data_dir_bytes` 包含 `lancedb`，`lancedb_dir_bytes` 又是它的子树；
改前两值各做一次 `rglob`，最贵的那棵树被走了两遍。这里钉住
「一次遍历、两个口径」的等价性。
"""
from __future__ import annotations

from rag.storage.backend import LocalBackend, _dir_bytes_split


def test_split_walk_matches_two_independent_totals(tmp_path):
    (tmp_path / "files").mkdir()
    (tmp_path / "lancedb" / "chunks").mkdir(parents=True)
    (tmp_path / "files" / "a.bin").write_bytes(b"x" * 100)
    (tmp_path / "lancedb" / "chunks" / "b.bin").write_bytes(b"y" * 50)
    (tmp_path / "lancedb" / "c.bin").write_bytes(b"z" * 25)
    (tmp_path / "config.toml").write_bytes(b"k" * 10)

    total, lance = _dir_bytes_split(tmp_path, tmp_path / "lancedb")

    assert total == 185
    assert lance == 75
    # 分母口径与改造前一致：data_dir 仍然包含 lancedb 子树
    assert total == sum(
        p.stat().st_size for p in tmp_path.rglob("*") if p.is_file())


def test_local_backend_stats_uses_the_single_walk(tmp_path):
    (tmp_path / "lancedb").mkdir()
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "x.bin").write_bytes(b"x" * 7)
    (tmp_path / "lancedb" / "y.bin").write_bytes(b"y" * 3)

    out = LocalBackend(tmp_path).stats()

    assert out["data_dir_bytes"] == 10
    assert out["lancedb_dir_bytes"] == 3
    assert out["backend"] == "local"
