#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Journal 还原工具: 把 --apply 写入的系列元数据恢复到写入前状态
- API 级还原, 零停机 (副本/读者完全不受影响)
- journal 里存的是写入前的完整 metadata DTO (含逐字段 LOCK 标志/链接/标签)
用法:
  python3 restore.py --run-id 20260929_1530            # 还原该批次全部
  python3 restore.py --run-id 20260929_1530 --series ID1,ID2   # 只还原指定系列
  python3 restore.py --run-id 20260929_1530 --covers   # 同时清掉这些系列的用户上传封面(回退为文件自动生成)
"""
import argparse
import json
import os
import sys

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_config():
    p = os.path.join(BASE_DIR, "config.json")
    if not os.path.exists(p):
        sys.exit("ERROR: 缺少 config.json")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class Komga:
    def __init__(self, cfg):
        self.base = cfg["komga"]["base_url"].rstrip("/")
        self.s = requests.Session()
        self.s.headers["X-API-Key"] = cfg["komga"]["api_key"]

    def patch(self, sid, payload):
        r = self.s.patch(f"{self.base}/api/v1/series/{sid}/metadata", json=payload, timeout=60)
        r.raise_for_status()

    def thumbs(self, sid):
        r = self.s.get(f"{self.base}/api/v1/series/{sid}/thumbnails", timeout=60)
        r.raise_for_status()
        return r.json()

    def del_thumb(self, sid, tid):
        r = self.s.delete(f"{self.base}/api/v1/series/{sid}/thumbnails/{tid}", timeout=60)
        r.raise_for_status()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True, help="apply 时的 run_id (journal 下的目录名)")
    ap.add_argument("--series", type=str, help="只还原指定系列, 逗号分隔")
    ap.add_argument("--covers", action="store_true", help="同时删除用户上传封面(回退为文件自动生成)")
    args = ap.parse_args()

    jdir = os.path.join(BASE_DIR, "journal", args.run_id)
    if not os.path.isdir(jdir):
        sys.exit(f"ERROR: journal 目录不存在: {jdir}")
    files = sorted(f for f in os.listdir(jdir) if f.endswith(".json"))
    if args.series:
        want = {s.strip() + ".json" for s in args.series.split(",")}
        files = [f for f in files if f in want]
    if not files:
        sys.exit("无匹配的 journal 记录")

    komga = Komga(load_config())

    def patch_safe(sid, meta):
        """全量 PATCH 失败时降级为逐字段写入, 返回无法还原的字段列表"""
        try:
            komga.patch(sid, meta)
            return []
        except Exception:
            pass
        bad = []
        for k, v in meta.items():
            try:
                komga.patch(sid, {k: v})
            except Exception:
                bad.append(k)
        return bad

    ok, fail, covers, bad_fields = 0, 0, 0, set()
    for f in files:
        with open(os.path.join(jdir, f), encoding="utf-8") as fh:
            rec = json.load(fh)
        sid = rec["series_id"]
        meta = {k: v for k, v in rec["metadata"].items() if k not in ("created", "lastModified")}
        try:
            bad = patch_safe(sid, meta)  # 原样写回 (含 *Lock 字段, 剔除只读时间戳)
            if bad:
                bad_fields.update(bad)
                print(f"[WARN] {rec.get('name') or sid} 以下字段无法写回: {bad}")
            if args.covers:
                for t in komga.thumbs(sid):
                    if t.get("type") == "USER_UPLOADED":
                        komga.del_thumb(sid, t["id"])
                        covers += 1
            ok += 1
            print(f"[OK] {rec.get('name') or sid}")
        except Exception as e:
            fail += 1
            print(f"[FAIL] {rec.get('name') or sid}: {str(e)[:100]}")
    print(f"\n还原完成: 成功 {ok} / 失败 {fail}" + (f" / 清除封面 {covers}" if args.covers else ""))
    if bad_fields:
        print(f"注意: 以下字段无法通过 API 写回(格式不兼容, 已跳过): {sorted(bad_fields)}")
    print("提示: 双击本地 Komga一键同步.bat 立即推平副本, 否则等凌晨5点")


if __name__ == "__main__":
    main()
