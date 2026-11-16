#!/usr/bin/env python3
"""双端 FASTQ 流式配对流水线。

设计约束：容器内存 128 MiB，单端解压后可达 256 MiB，
因此全程不在内存中保存读段集合：
  1. 逐行流式解压校验，记录原始字节写入磁盘 records 文件，
     (id, offset, length) 登记到临时 SQLite 库（唯一索引查重）；
  2. 按 R1 原顺序（rowid 序）游标遍历，点查 R2 索引完成配对，
     用 os.pread 按偏移取回原始记录写出四个 gzip 结果。
任何非法记录（含文件末尾才出现的错误）都会抛 PipelineError，
调用方据此整批失败并清理部分产物。
"""
import gzip
import os
import sqlite3
import zlib

# 单行长度保护上限（正常双端读段行长远低于此；防止畸形单行耗尽内存）
MAX_LINE_BYTES = 16 * 1024 * 1024
# 默认解压后大小上限：256 MiB
DEFAULT_MAX_DECOMP_BYTES = 256 * 1024 * 1024

# 四个结果文件名（app 与 pipeline 共用的唯一来源）
OUTPUT_FILES = {
    "paired_r1": "paired_R1.fastq.gz",
    "paired_r2": "paired_R2.fastq.gz",
    "unpaired_r1": "unpaired_R1.fastq.gz",
    "unpaired_r2": "unpaired_R2.fastq.gz",
}

_BATCH = 8192  # SQLite 批量插入行数


class PipelineError(Exception):
    """输入数据或资源限制导致的可预期失败（信息可直接展示给用户）。"""


def _fmt_id(rid):
    return rid.decode("utf-8", "replace")


def _readline(f, side):
    """读一行；EOF 返回 None；gzip 损坏/超长行抛 PipelineError。"""
    try:
        line = f.readline(MAX_LINE_BYTES + 1)
    except (OSError, EOFError, zlib.error) as e:
        raise PipelineError(f"{side} 不是有效的 gzip 文件或数据已损坏: {e}") from e
    if not line:
        return None
    if len(line) > MAX_LINE_BYTES:
        raise PipelineError(f"{side} 存在超过 {MAX_LINE_BYTES} 字节的超长行，记录非法")
    return line


def _parse_record_id(lines, side, nrec):
    """校验四行结构并提取读段 ID：标题首个词，去掉末尾 /1、/2。"""
    h, s, p, q = lines
    if not h.startswith(b"@"):
        raise PipelineError(f"{side} 第 {nrec} 条记录标题行未以 @ 开头")
    if not p.startswith(b"+"):
        raise PipelineError(f"{side} 第 {nrec} 条记录第三行未以 + 开头")
    seq = s.rstrip(b"\r\n")
    qual = q.rstrip(b"\r\n")
    if not seq:
        raise PipelineError(f"{side} 第 {nrec} 条记录序列为空")
    if len(seq) != len(qual):
        raise PipelineError(f"{side} 第 {nrec} 条记录序列与质量值长度不一致")
    body = h[1:].strip()
    if not body:
        raise PipelineError(f"{side} 第 {nrec} 条记录标题为空")
    token = body.split(None, 1)[0]
    if token.endswith((b"/1", b"/2")):
        token = token[:-2]
    if not token:
        raise PipelineError(f"{side} 第 {nrec} 条记录读段 ID 为空")
    return token


def _flush_batch(con, sql, batch, side):
    """批量登记；唯一索引冲突时逐条重放以定位重复 ID。"""
    try:
        con.executemany(sql, batch)
    except sqlite3.IntegrityError:
        con.rollback()
        for row in batch:
            try:
                con.execute(sql, row)
            except sqlite3.IntegrityError:
                con.rollback()
                raise PipelineError(
                    f"{side} 内读段 ID 重复: {_fmt_id(row[0])}") from None
        con.commit()
        return
    con.commit()


def index_side(gz_path, rec_path, scratch, table, side, max_bytes):
    """流式校验一侧 FASTQ，原始记录落盘，ID 登记唯一索引。返回记录数。"""
    con = sqlite3.connect(scratch)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA cache_size=-4000")  # 4 MiB
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {table} ("
        "id BLOB NOT NULL, off INTEGER NOT NULL, len INTEGER NOT NULL)")
    con.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_id ON {table}(id)")
    sql = f"INSERT INTO {table}(id, off, len) VALUES (?,?,?)"
    total = 0
    nrec = 0
    off = 0
    batch = []
    try:
        with gzip.open(gz_path, "rb") as gz, open(rec_path, "wb") as rec:
            while True:
                lines = []
                for _ in range(4):
                    ln = _readline(gz, side)
                    if ln is None:
                        break
                    total += len(ln)
                    if total > max_bytes:
                        raise PipelineError(
                            f"{side} 解压后大小超过上限 "
                            f"({max_bytes // (1024 * 1024)} MiB)")
                    lines.append(ln)
                if not lines:
                    break
                nrec += 1
                if len(lines) < 4:
                    raise PipelineError(
                        f"{side} 第 {nrec} 条记录不完整（文件末尾缺少行）")
                rid = _parse_record_id(lines, side, nrec)
                blob = b"".join(lines)
                if not blob.endswith(b"\n"):
                    blob += b"\n"  # 末尾记录缺换行时补齐，保证输出为合法 FASTQ
                rec.write(blob)
                batch.append((rid, off, len(blob)))
                off += len(blob)
                if len(batch) >= _BATCH:
                    _flush_batch(con, sql, batch, side)
                    batch.clear()
            if batch:
                _flush_batch(con, sql, batch, side)
    finally:
        con.close()
    return nrec


class _GzWriter:
    """gzip 结果写出器：小缓冲流式压缩，关闭时 fsync。"""

    def __init__(self, path):
        self.raw = open(path, "wb")
        self.gz = gzip.GzipFile(filename="", mode="wb", fileobj=self.raw,
                                compresslevel=6, mtime=0)

    def write(self, data):
        self.gz.write(data)

    def close(self):
        self.gz.close()  # 写 gzip 尾，不关闭底层 fileobj
        self.raw.flush()
        os.fsync(self.raw.fileno())
        self.raw.close()


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def pair_pass(scratch, rec1_path, rec2_path, staged):
    """按 ID 配对并写出四个结果（两个成对文件均按 R1 原顺序）。返回计数。"""
    con = sqlite3.connect(scratch)
    con.execute("PRAGMA cache_size=-4000")
    fd1 = os.open(rec1_path, os.O_RDONLY)
    fd2 = os.open(rec2_path, os.O_RDONLY)
    writers = {k: _GzWriter(p) for k, p in staged.items()}
    paired = r1_only = r2_only = 0
    try:
        q2 = con.cursor()
        # R1 原顺序遍历；有 mate 进成对文件，否则进缺端 R1
        cur = con.execute("SELECT id, off, len FROM r1 ORDER BY rowid")
        while True:
            rows = cur.fetchmany(4096)
            if not rows:
                break
            for rid, off, ln in rows:
                rec = os.pread(fd1, ln, off)
                hit = q2.execute(
                    "SELECT off, len FROM r2 WHERE id=?", (rid,)).fetchone()
                if hit is None:
                    writers["unpaired_r1"].write(rec)
                    r1_only += 1
                else:
                    writers["paired_r1"].write(rec)
                    writers["paired_r2"].write(os.pread(fd2, hit[1], hit[0]))
                    paired += 1
        # R2 原顺序遍历；无 mate 的进缺端 R2
        q1 = con.cursor()
        cur = con.execute("SELECT id, off, len FROM r2 ORDER BY rowid")
        while True:
            rows = cur.fetchmany(4096)
            if not rows:
                break
            for rid, off, ln in rows:
                if q1.execute(
                        "SELECT 1 FROM r1 WHERE id=?", (rid,)).fetchone() is None:
                    writers["unpaired_r2"].write(os.pread(fd2, ln, off))
                    r2_only += 1
    finally:
        for w in writers.values():
            w.close()
        os.close(fd1)
        os.close(fd2)
        con.close()
    return {"paired": paired, "r1_only": r1_only, "r2_only": r2_only}


def run(r1_gz, r2_gz, work_dir, out_dir, max_bytes=DEFAULT_MAX_DECOMP_BYTES):
    """完整流程：校验建索引 -> 配对 -> 结果原子发布到 out_dir。返回计数。

    任何一步失败都抛异常，此时 out_dir 中不会有任何结果文件
    （结果先写在 work_dir，全部成功后才 rename 发布）。
    """
    scratch = os.path.join(work_dir, "scratch.db")
    rec1 = os.path.join(work_dir, "r1.rec")
    rec2 = os.path.join(work_dir, "r2.rec")
    index_side(r1_gz, rec1, scratch, "r1", "R1", max_bytes)
    index_side(r2_gz, rec2, scratch, "r2", "R2", max_bytes)
    staged = {k: os.path.join(work_dir, name + ".tmp")
              for k, name in OUTPUT_FILES.items()}
    counts = pair_pass(scratch, rec1, rec2, staged)
    os.makedirs(out_dir, exist_ok=True)
    for key, tmp in staged.items():
        os.replace(tmp, os.path.join(out_dir, OUTPUT_FILES[key]))
    _fsync_dir(out_dir)
    return counts


if __name__ == "__main__":
    # 独立运行便于命令行自测:
    #   python3 -I pipeline.py R1.fastq.gz R2.fastq.gz WORK_DIR OUT_DIR
    import sys
    if len(sys.argv) != 5:
        sys.exit("用法: pipeline.py R1.gz R2.gz WORK_DIR OUT_DIR")
    os.makedirs(sys.argv[3], exist_ok=True)
    c = run(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
    print(f"paired={c['paired']} r1_only={c['r1_only']} r2_only={c['r2_only']}")
