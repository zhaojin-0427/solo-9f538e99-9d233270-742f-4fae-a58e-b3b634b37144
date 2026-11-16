# 双端 FASTQ 配对修复台

测序质控场景：收到**顺序错乱的双端读段**（R1/R2 两个 gzip FASTQ），本服务将其恢复为
可交给比对器的成对文件，并分离出缺端记录。Flask + SQLite，全流式处理，
容器内存限制 128 MiB 下可处理单端解压后 256 MiB 的输入。

## 功能

- 网页上传 R1、R2 两份 gzip FASTQ（每份解压后 ≤ 256 MiB，超限整批失败）；
- 读段 ID = 标题首个词，去掉末尾 `/1`、`/2`（兼容 `@ID/1` 与 `@ID 1:N:0:XX` 两种标题）；
- **整批校验**：同侧重复 ID、非法四行记录（标题非 `@`、第三行非 `+`、
  序列与质量长度不符、记录残缺、非 gzip 数据）一律整批失败——
  包括文件末尾才出现的错误（先全量校验建索引，后产出结果）；
- 按 ID 配对，两个成对输出均按 **R1 原顺序** 排列；
  缺端记录分别输出 `unpaired_R1` / `unpaired_R2`（各自保持原侧顺序）；
  原始标题、序列、质量值逐字节保留；
- 任务列表/详情页展示状态（排队中/处理中/完成/失败）、配对数与缺端数，
  完成后可下载四个 gzip 结果；
- **可靠性**：结果先写入临时区，全部产出并 fsync、原子 rename、
  SQLite 登记成功后才开放下载；失败不保留任何部分结果；
  服务重启时自动清理中断任务的未完成产物，已完成结果仍可下载。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

- 容器内存上限 128 MiB（`mem_limit`），数据持久化在命名卷 `fastq_data`
  （上传文件、结果文件、SQLite 登记库都在 `/data`）；
- 用 `samples/` 下的 `normal_r1.fastq.gz` / `normal_r2.fastq.gz` 即可体验
  乱序 + 缺端配对；`dup_*`、`badqual_*`、`truncated_*`、`notgzip_*`
  为必失败样例，可验证整批失败行为。

## 本地开发运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd app && DATA_DIR=../data PORT=8000 ../.venv/bin/python app.py
```

流水线也可脱离 Web 独立自测：

```bash
python3 -I app/pipeline.py R1.fastq.gz R2.fastq.gz WORK_DIR OUT_DIR
```

## 样例数据

`samples/make_samples.py` 可重新生成全部样例（确定性随机）：

| 样例 | 说明 | 预期 |
|---|---|---|
| `normal_r1/r2.fastq.gz` | 240 对乱序 + 16 条 R1 缺端 + 8 条 R2 缺端 | paired=240, r1_only=16, r2_only=8 |
| `dup_*` | R1 末尾出现重复 ID | 整批失败 |
| `badqual_*` | R1 末尾记录序列/质量长度不符 | 整批失败 |
| `truncated_*` | R1 末尾记录只有 3 行 | 整批失败 |
| `notgzip_*` | 内容不是 gzip | 整批失败 |

## 输出文件

| 文件 | 内容 |
|---|---|
| `paired_R1.fastq.gz` | 有 mate 的 R1 记录，按 R1 原顺序 |
| `paired_R2.fastq.gz` | 对应的 R2 记录，与 paired_R1 同序 |
| `unpaired_R1.fastq.gz` | 缺端 R1（R2 中无配对），R1 原顺序 |
| `unpaired_R2.fastq.gz` | 缺端 R2（R1 中无配对），R2 原顺序 |

## 设计要点（为何能在 128 MiB 内存下工作）

1. **流式校验建索引**：逐行解压（`readline` 带 16 MiB 单行保护上限），
   原始记录字节顺序写入磁盘 records 文件，仅把 `(id, offset, length)`
   登记进临时 SQLite（唯一索引查重），全程不在内存保存读段集合；
2. **游标式配对**：按 R1 的 rowid（即原始顺序）分批游标遍历，
   对 R2 索引做点查，用 `os.pread` 按偏移取回记录写出四个 gzip；
3. **原子发布**：结果先写 `work/`，全部完成并 fsync 后 `rename` 进 `out/`，
   再提交 SQLite 登记；任一步失败即清理，下载接口只对 `done` 任务开放；
4. **启动恢复**：`queued`/`processing` 状态的任务在重启时标记失败并
   删除其 `work/`、`out/`；目录中无登记的任务目录一并清除。

实测：100 万对读段（每侧解压约 209 MiB）处理约 50 秒，
流水线峰值内存 17 MiB，含 Flask 的服务进程峰值约 39 MiB。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATA_DIR` | `/data`（容器） | 数据目录（上传、结果、SQLite） |
| `PORT` | `8000` | 监听端口 |
| `MAX_DECOMP_BYTES` | `268435456` | 每份文件解压后上限（256 MiB） |
| `MAX_UPLOAD_BYTES` | 上值 + 2 MiB | 上传压缩包大小上限 |
| `SECRET_KEY` | 开发默认值 | Flask 会话密钥，生产应覆盖 |

## 目录结构

```
app/
  app.py            Flask 应用：页面、任务登记、后台处理、下载、启动恢复
  pipeline.py       流式配对流水线（校验/索引/配对/原子发布）
  templates/        页面模板
samples/            演示样例 + 生成脚本
tests/
  verify_pairing.py 独立校验脚本（比对输入与四个输出）
  make_big.py       大样本生成（压测用）
Dockerfile / docker-compose.yml / requirements.txt
```

## 已知限制

- 内置 Flask 开发服务器（threaded），适合演示与内部工具；
  生产部署建议换 gunicorn/waitress 并设置 `SECRET_KEY`；
- 仅支持标准四行 FASTQ（不支持序列折行的多行 FASTQ）；
  单行超过 16 MiB 视为非法记录（资源保护）；
- 末尾记录缺少换行符时输出会补一个换行（保证输出为合法 FASTQ），
  其余内容逐字节保留；
- 无任务删除/清理界面，历史任务与文件会一直保留在数据卷中。
