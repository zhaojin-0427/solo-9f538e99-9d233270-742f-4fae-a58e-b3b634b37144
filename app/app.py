#!/usr/bin/env python3
"""双端 FASTQ 配对修复服务（Flask + SQLite）。

- 上传 R1/R2 两份 gzip FASTQ，后台流式配对；
- 全部结果文件发布且 SQLite 登记成功后才开放下载；
- 失败不暴露任何部分结果；重启时清理中断任务的未完成产物。
"""
import os
import shutil
import sqlite3
import threading
import time
import uuid

from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, send_file, url_for)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

import pipeline

DATA_DIR = os.path.abspath(os.environ.get(
    "DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")))
JOBS_DIR = os.path.join(DATA_DIR, "jobs")
DB_PATH = os.path.join(DATA_DIR, "app.db")

# 解压后上限（默认 256 MiB，可用环境变量覆盖以便测试）
MAX_DECOMP_BYTES = int(os.environ.get(
    "MAX_DECOMP_BYTES", str(pipeline.DEFAULT_MAX_DECOMP_BYTES)))
# 上传压缩包上限：解压上限 + 2 MiB 余量（不可压缩数据 gzip 后略大于原始）
MAX_UPLOAD_BYTES = int(os.environ.get(
    "MAX_UPLOAD_BYTES", str(MAX_DECOMP_BYTES + 2 * 1024 * 1024)))

OUTPUTS = {
    "paired_r1": "配对 R1（按 R1 原顺序）",
    "paired_r2": "配对 R2（按 R1 原顺序）",
    "unpaired_r1": "缺端 R1（R2 中无配对）",
    "unpaired_r2": "缺端 R2（R1 中无配对）",
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.secret_key = os.environ.get("SECRET_KEY", "fastq-pairer-dev-secret")


# ---------------- SQLite 登记库 ----------------

def connect_db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def get_db():
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_and_recover():
    """建表 + 启动恢复：中断任务标记失败并清理其未完成产物。"""
    os.makedirs(JOBS_DIR, exist_ok=True)
    con = connect_db()
    con.execute(
        """CREATE TABLE IF NOT EXISTS jobs(
               id TEXT PRIMARY KEY,
               created_at TEXT NOT NULL,
               status TEXT NOT NULL,          -- queued/processing/done/failed
               message TEXT NOT NULL DEFAULT '',
               r1_name TEXT NOT NULL DEFAULT '',
               r2_name TEXT NOT NULL DEFAULT '',
               paired INTEGER, r1_only INTEGER, r2_only INTEGER)""")
    con.commit()
    interrupted = [r["id"] for r in con.execute(
        "SELECT id FROM jobs WHERE status IN ('queued','processing')")]
    for jid in interrupted:
        job_dir = os.path.join(JOBS_DIR, jid)
        shutil.rmtree(os.path.join(job_dir, "work"), ignore_errors=True)
        shutil.rmtree(os.path.join(job_dir, "out"), ignore_errors=True)
        con.execute(
            "UPDATE jobs SET status='failed', "
            "message='服务重启导致任务中断，未完成产物已清理' WHERE id=?", (jid,))
    con.commit()
    known = {r["id"] for r in con.execute("SELECT id FROM jobs")}
    for name in os.listdir(JOBS_DIR):
        if name not in known:
            shutil.rmtree(os.path.join(JOBS_DIR, name), ignore_errors=True)
    con.close()
    if interrupted:
        app.logger.info("已清理 %d 个中断任务", len(interrupted))


# ---------------- 后台处理 ----------------

def process_job(jid):
    """后台线程：跑流水线；全部结果发布 + SQLite 登记成功才算完成。"""
    job_dir = os.path.join(JOBS_DIR, jid)
    work = os.path.join(job_dir, "work")
    out = os.path.join(job_dir, "out")
    con = connect_db()
    try:
        con.execute("UPDATE jobs SET status='processing' WHERE id=?", (jid,))
        con.commit()
        os.makedirs(work, exist_ok=True)
        counts = pipeline.run(
            os.path.join(job_dir, "uploads", "r1.fastq.gz"),
            os.path.join(job_dir, "uploads", "r2.fastq.gz"),
            work, out, max_bytes=MAX_DECOMP_BYTES)
        # 结果文件已全部落盘，最后登记；若此处失败则走 except 删除结果
        con.execute(
            "UPDATE jobs SET status='done', paired=?, r1_only=?, r2_only=?, "
            "message='' WHERE id=?",
            (counts["paired"], counts["r1_only"], counts["r2_only"], jid))
        con.commit()
    except pipeline.PipelineError as e:
        con.rollback()
        shutil.rmtree(out, ignore_errors=True)  # 失败不暴露部分结果
        con.execute("UPDATE jobs SET status='failed', message=? WHERE id=?",
                    (str(e), jid))
        con.commit()
    except Exception as e:  # 意外错误（含登记失败）：同样清理结果
        con.rollback()
        shutil.rmtree(out, ignore_errors=True)
        con.execute("UPDATE jobs SET status='failed', message=? WHERE id=?",
                    (f"内部错误: {e!r}", jid))
        con.commit()
        app.logger.exception("任务 %s 处理异常", jid)
    finally:
        shutil.rmtree(work, ignore_errors=True)  # 中间产物（索引/记录文件）即清
        con.close()


# ---------------- 页面与接口 ----------------

@app.route("/")
def index():
    jobs = get_db().execute(
        "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT 200").fetchall()
    active = any(j["status"] in ("queued", "processing") for j in jobs)
    return render_template("index.html", jobs=jobs, active=active,
                           max_mib=MAX_DECOMP_BYTES // (1024 * 1024))


@app.route("/jobs", methods=["POST"])
def create_job():
    f1 = request.files.get("r1")
    f2 = request.files.get("r2")
    if not f1 or not f1.filename or not f2 or not f2.filename:
        flash("请同时选择 R1 和 R2 两个 gzip FASTQ 文件", "error")
        return redirect(url_for("index"))
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    uploads = os.path.join(JOBS_DIR, jid, "uploads")
    os.makedirs(uploads)
    f1.save(os.path.join(uploads, "r1.fastq.gz"))
    f2.save(os.path.join(uploads, "r2.fastq.gz"))
    db = get_db()
    db.execute(
        "INSERT INTO jobs(id, created_at, status, r1_name, r2_name) "
        "VALUES (?,?,?,?,?)",
        (jid, time.strftime("%Y-%m-%d %H:%M:%S"), "queued",
         secure_filename(f1.filename) or "r1.fastq.gz",
         secure_filename(f2.filename) or "r2.fastq.gz"))
    db.commit()
    threading.Thread(target=process_job, args=(jid,), daemon=True).start()
    return redirect(url_for("job_detail", jid=jid))


@app.route("/jobs/<jid>")
def job_detail(jid):
    job = get_db().execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if job is None:
        abort(404)
    return render_template("job.html", job=job, outputs=OUTPUTS,
                           output_files=pipeline.OUTPUT_FILES)


@app.route("/jobs/<jid>/download/<key>")
def download(jid, key):
    if key not in OUTPUTS:
        abort(404)
    job = get_db().execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
    if job is None:
        abort(404)
    if job["status"] != "done":
        # 未完成/失败任务一律不暴露产物
        abort(409)
    path = os.path.join(JOBS_DIR, jid, "out", pipeline.OUTPUT_FILES[key])
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="application/gzip", as_attachment=True,
                     download_name=f"{jid}_{pipeline.OUTPUT_FILES[key]}")


@app.route("/healthz")
def healthz():
    return "ok"


@app.errorhandler(RequestEntityTooLarge)
def too_large(_e):
    flash(f"上传文件超过压缩大小上限（{MAX_UPLOAD_BYTES // (1024 * 1024)} MiB）",
          "error")
    return redirect(url_for("index")), 413


if __name__ == "__main__":
    init_and_recover()
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
