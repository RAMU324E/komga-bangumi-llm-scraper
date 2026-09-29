#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全库: 文件夹名写进系列别名 (label=文件名), 让搜索命中文件夹名
背景: Komga 搜索匹配 标题+别名, 不匹配文件夹名 (实测 2026-09-29)
锁定说明: alternateTitlesLock 多为本刮削器先前所设, 此脚本照写并记录原值, 可按 journal 回滚
用法: python3 add_filename_aliases.py [--apply]
"""
import argparse, datetime as dt, os, subprocess, sys
import scraper
from scraper import Komga, log, save_json

BASE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    cfg = scraper.load_config()
    k = Komga(cfg)
    rl = scraper.RateLimiter(cfg["apply"].get("writes_per_second", 3))
    lib = k.find_library(cfg["target"]["library_name"])
    if args.apply:
        r = subprocess.run(["bash", os.path.join(BASE, "snapshot_db.sh")], capture_output=True, text=True, timeout=600)
        log((r.stdout or "").strip())
        if r.returncode != 0:
            sys.exit("快照失败, 中止")
    run_dir = os.path.join(BASE, "journal", f"filealts_{dt.datetime.now():%Y%m%d_%H%M%S}")
    if args.apply:
        os.makedirs(run_dir, exist_ok=True)
    n = dup = 0
    for s in k.all_series(lib["id"]):
        m = s.get("metadata") or k.series_detail(s["id"])["metadata"]
        name, alts = s["name"], m.get("alternateTitles") or []
        if name == m.get("title") or any(a.get("title") == name for a in alts):
            dup += 1
            continue
        if args.apply:
            save_json(os.path.join(run_dir, f"{s['id']}.json"),
                      {"series_id": s["id"], "name": name, "old_alts": alts,
                       "locked": bool(m.get("alternateTitlesLock"))})
            k.patch_metadata(s["id"], {"alternateTitles": alts + [{"label": "文件名", "title": name}]})
            rl.wait()
        n += 1
        if n % 200 == 0:
            log(f"  进度 {n}")
    log(f"文件名别名: {'写入' if args.apply else 'dry'} {n} 个, 已有/同名跳过 {dup}"
        + (f", journal -> {run_dir}" if args.apply else " (未写入)"))


if __name__ == "__main__":
    main()
