#!/usr/bin/env python3
"""生成演示样例（gzip FASTQ）：

  normal_r1/r2.fastq.gz   正常样例：R1/R2 顺序不同（乱序），含缺端记录
                          预期：paired=240, r1_only=16, r2_only=8
  dup_r1/r2.fastq.gz      R1 末尾出现重复 ID            -> 必须整批失败
  badqual_r1/r2.fastq.gz  R1 末尾记录序列/质量长度不符   -> 必须整批失败
  truncated_r1/r2.fastq.gz R1 末尾只有 3 行（记录残缺）  -> 必须整批失败
  notgzip_r1/r2.fastq.gz  内容不是 gzip                  -> 必须整批失败

用法: python3 make_samples.py [输出目录]   （默认写到脚本所在目录）
"""
import gzip
import os
import random
import sys

N_PAIRED = 240
N_R1_ONLY = 16
N_R2_ONLY = 8
SEQ_LEN = 100

rng = random.Random(20261116)


def seq_for(rid):
    r = random.Random(rid)
    return "".join(r.choice("ACGTN") for _ in range(SEQ_LEN))


def qual_for(rid):
    r = random.Random("q" + rid)
    return "".join(r.choice("IF5<") for _ in range(SEQ_LEN))


def fmt_record(rid, side, style):
    """side: 1 或 2；style 交替使用旧式 /1 与新式 1:N:0 标题。"""
    if style == 0:
        header = f"@INST:1:FC:1:1101:{rid}/{side}"
    else:
        header = f"@INST:1:FC:1:1101:{rid} {side}:N:0:ATCACG"
    return f"{header}\n{seq_for(rid)}\n+\n{qual_for(rid)}\n"


def write_gz(path, text):
    with gzip.open(path, "wt", newline="") as f:
        f.write(text)


def make_normal(out):
    paired_ids = [f"{1400+i}" for i in range(N_PAIRED)]
    r1_only_ids = [f"{9000+i}" for i in range(N_R1_ONLY)]
    r2_only_ids = [f"{9500+i}" for i in range(N_R2_ONLY)]

    r1_order = paired_ids + r1_only_ids
    rng.shuffle(r1_order)
    r2_order = paired_ids + r2_only_ids
    rng.shuffle(r2_order)

    r1 = "".join(fmt_record(rid, 1, i % 2) for i, rid in enumerate(r1_order))
    r2 = "".join(fmt_record(rid, 2, i % 2) for i, rid in enumerate(r2_order))
    write_gz(os.path.join(out, "normal_r1.fastq.gz"), r1)
    write_gz(os.path.join(out, "normal_r2.fastq.gz"), r2)


def _small_pair(n=12):
    ids = [f"{7000+i}" for i in range(n)]
    r1 = [fmt_record(rid, 1, i % 2) for i, rid in enumerate(ids)]
    r2 = [fmt_record(rid, 2, i % 2) for i, rid in enumerate(ids)]
    rng.shuffle(r2)
    return ids, r1, r2


def make_dup(out):
    ids, r1, r2 = _small_pair()
    # 重复 ID 放在文件末尾：验证“末尾才出现的错误也整批失败”
    r1.append(fmt_record(ids[3], 1, 0))
    write_gz(os.path.join(out, "dup_r1.fastq.gz"), "".join(r1))
    write_gz(os.path.join(out, "dup_r2.fastq.gz"), "".join(r2))


def make_badqual(out):
    ids, r1, r2 = _small_pair()
    bad = fmt_record(ids[-1], 1, 1)
    lines = bad.splitlines(keepends=True)
    lines[3] = lines[3][:10] + "\n"  # 质量值截短：与序列长度不符
    r1[-1] = "".join(lines)
    write_gz(os.path.join(out, "badqual_r1.fastq.gz"), "".join(r1))
    write_gz(os.path.join(out, "badqual_r2.fastq.gz"), "".join(r2))


def make_truncated(out):
    ids, r1, r2 = _small_pair()
    last = fmt_record(ids[-1], 1, 0).splitlines(keepends=True)
    r1[-1] = "".join(last[:3])  # 末尾记录只有 3 行
    write_gz(os.path.join(out, "truncated_r1.fastq.gz"), "".join(r1))
    write_gz(os.path.join(out, "truncated_r2.fastq.gz"), "".join(r2))


def make_notgzip(out):
    with open(os.path.join(out, "notgzip_r1.fastq.gz"), "w") as f:
        f.write("这不是 gzip 内容，只是普通文本。\n")
    _, _, r2 = _small_pair()
    write_gz(os.path.join(out, "notgzip_r2.fastq.gz"), "".join(r2))


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    os.makedirs(out, exist_ok=True)
    make_normal(out)
    make_dup(out)
    make_badqual(out)
    make_truncated(out)
    make_notgzip(out)
    print(f"样例已生成到 {out}")
    print(f"  normal_*: paired={N_PAIRED} r1_only={N_R1_ONLY} r2_only={N_R2_ONLY}")
    print("  dup_* / badqual_* / truncated_* / notgzip_*: 预期整批失败")


if __name__ == "__main__":
    main()
