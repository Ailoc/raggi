# rag-bench：并发基准与开销归因

这是「性能改动的验收工具」。所有数字都必须在**这台机器、这个数据集**上跑出来，
不接受口头结论。

```bash
# 1) 造一份可复现的合成数据集（默认 2000 文档 / 20000 分块 / 1024 维）
python3 -m tools.bench gen --data /tmp/bench/data --docs 2000 --chunks-per 10

# 2) 起一个假 embedding 服务（OpenAI 兼容；不花额度、不受网络抖动影响）
python3 -m tools.bench fake-embed --port 8390 &

# 3) 起被测服务（多进程形态就是今天的默认）
RAG_EMBED__BASE_URL=http://127.0.0.1:8390/v1 \
python3 -m rag.server serve --data /tmp/bench/data --port 8310 &

# 4) 跑基准
python3 -m tools.bench quick  --base http://127.0.0.1:8310      # 读矩阵
python3 -m tools.bench ingest --base http://127.0.0.1:8310      # 入库矩阵
python3 -m tools.bench scan-paths                                # 单次查询开销归因
python3 -m tools.bench report --save benchmarks/$(date +%F).json # 存快照
```

三条纪律，都是踩过坑换来的：

1. **每线程一个独立 client**。多个线程共享一个 `httpx.Client` 不是线程安全的，
   曾经据此得出「并发退化是 GIL 问题」的错误结论，改了客户端用法之后吞吐从
   5.4 跳到 9.5 req/s（DESIGN §12.1 有记录）。现在 `closed_loop()` 里
   每个工作线程自己建一个。
2. **压测前先把数据目录复制一份**。写类基准会真的入库、真的建索引，
   在 `data/` 上跑等于改生产数据。`gen --data` 之外的一切都用 `/tmp`。
3. **不碰真实模型凭据**。默认假 embedding 服务把网络抖动与额度摘掉，
   于是量到的是「后端排队与存储成本」。真实凭据下的端到端延迟要显式加
   `--real-embed` 再跑一次，两者不可混在一张表里比较。

## 与门槛测试的关系

`tests/test_perf_gates.py` 断言的是**结构性**门槛（每请求的 LanceDB 查询次数、
是否走原生投影、写放大上界），默认就能跑；
延迟类门槛（p50 / rps）在 `@pytest.mark.perf` 下，需要本工具的服务在跑，
默认 skip——CI 里机器抖动会让它假红。
