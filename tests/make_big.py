#!/usr/bin/env python3
"""生成大型乱序双端样例用于压测（默认约 1.0M 对，解压后每侧约 230 MiB）。"""
import gzip
import random
import sys

n = int(sys.argv[3]) if len(sys.argv) > 3 else 1_000_000
out1, out2 = sys.argv[1], sys.argv[2]

ids = list(range(n))
random.Random(7).shuffle(ids)
seq = "ACGT" * 25          # 100 bp
qual = "I" * 100

with gzip.open(out1, "wt", compresslevel=6) as f:
    for i in ids:
        f.write(f"@SIM:{i}/1\n{seq}\n+\n{qual}\n")

random.Random(9).shuffle(ids)
with gzip.open(out2, "wt", compresslevel=6) as f:
    for i in ids:
        f.write(f"@SIM:{i}/2\n{seq}\n+\n{qual}\n")
print("done", n)
