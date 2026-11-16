#!/usr/bin/env python3
"""独立校验配对结果（不依赖被测代码的逻辑）：

  python3 verify_pairing.py R1.gz R2.gz paired_R1.gz paired_R2.gz \
                            unpaired_R1.gz unpaired_R2.gz

校验点：
  - 两个成对文件均按 R1 原顺序，且同一位置的 R1/R2 记录 ID 相同；
  - 缺端 R1 为 R1 中无 mate 的记录（保持 R1 原顺序）；
  - 缺端 R2 为 R2 中无 mate 的记录（保持 R2 原顺序）；
  - 输出记录与输入记录逐字节一致（标题/序列/质量完整保留）。
"""
import gzip
import sys


def read_fastq(path):
    recs = []
    with gzip.open(path, "rt") as f:
        while True:
            h = f.readline()
            if not h:
                break
            s, p, q = f.readline(), f.readline(), f.readline()
            if not (s and p and q):
                raise SystemExit(f"{path}: 记录不完整")
            rid = h.split()[0][1:]
            if rid.endswith(("/1", "/2")):
                rid = rid[:-2]
            recs.append((rid, h, s, p, q))
    return recs


def main():
    if len(sys.argv) != 7:
        sys.exit("用法: verify_pairing.py R1.gz R2.gz pR1.gz pR2.gz uR1.gz uR2.gz")
    r1, r2, pr1, pr2, ur1, ur2 = (read_fastq(p) for p in sys.argv[1:7])

    ids_r2 = {r[0] for r in r2}
    map_r2 = {r[0]: r for r in r2}
    ids_r1 = {r[0] for r in r1}

    exp_pr1 = [r for r in r1 if r[0] in ids_r2]
    exp_pr2 = [map_r2[r[0]] for r in exp_pr1]
    exp_ur1 = [r for r in r1 if r[0] not in ids_r2]
    exp_ur2 = [r for r in r2 if r[0] not in ids_r1]

    ok = True
    for name, exp, got in (("paired_R1", exp_pr1, pr1),
                           ("paired_R2", exp_pr2, pr2),
                           ("unpaired_R1", exp_ur1, ur1),
                           ("unpaired_R2", exp_ur2, ur2)):
        if exp == got:
            print(f"OK    {name}: {len(got)} 条记录")
        else:
            ok = False
            print(f"FAIL  {name}: 期望 {len(exp)} 条，实际 {len(got)} 条")
            for i, (e, g) in enumerate(zip(exp, got)):
                if e != g:
                    print(f"  首个差异在第 {i} 条:\n    期望 {e}\n    实际 {g}")
                    break
    # 成对文件同位置 ID 必须一致
    if len(pr1) == len(pr2):
        mism = [i for i, (a, b) in enumerate(zip(pr1, pr2)) if a[0] != b[0]]
        if mism:
            ok = False
            print(f"FAIL  成对文件第 {mism[0]} 条起 ID 不一致")
    else:
        ok = False
        print(f"FAIL  成对文件条数不等: {len(pr1)} vs {len(pr2)}")
    print("结果:", "全部通过" if ok else "存在错误")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
