#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dup_editions: 全库同名显示标题体检 + 自动加版本后缀（防撞车）
背景: 同一部作品的多个版本（普通/全彩、不同出版社或来源）刮削后显示标题相同,
     界面无法区分。本工具按「基础标题」分组找出同名/撞名系列并自动加（xx版）后缀。
分组规则: 先剥离标题尾部形如（xx版/篇/卷…）的后缀得到基础标题, 按基础标题分组:
  - 组内显示标题仍有完全重复的成员 → 只对这些成员改名（不动已互异的）
  - 改名时后缀避让组内全部已占用后缀（含未重复成员的）
  - 显示已互异（如 寓言杀手 vs 寓言杀手（番外））→ 不动
后缀规则（与库内既有风格一致）:
  1) 基础: 全彩/彩色/汉化/bili/出版社(东立 青文 尖端 长鸿 文传 大然 天下 玉皇朝 角川 东贩), 电子版优先组合
  2) 防撞车: 组内撞车时自动升级 "基础+卷数"(如 bili 6卷版), 再不行用组内独有片段
  3) 仍无法区分 → 该成员只报告不改
安全: 默认 dry-run 只打印计划; --apply 才写（全库快照 + 每系列 journal + 写后核对, 可回滚）
用法: python3 dup_editions.py [--apply]
"""
import argparse, datetime as dt, os, re, subprocess, sys
import scraper
from scraper import Komga, log, save_json

BASE = os.path.dirname(os.path.abspath(__file__))

PUBS = [("东立", "东立"), ("東立", "东立"), ("青文", "青文"), ("尖端", "尖端"), ("长鸿", "长鸿"),
        ("長鴻", "长鸿"), ("文傳", "文传"), ("文传", "文传"), ("大然", "大然"), ("天下", "天下"),
        ("玉皇朝", "玉皇朝"), ("角川", "角川"), ("東販", "东贩"), ("东贩", "东贩")]
SUFFIX_HINT = re.compile(r"版|篇|话|卷|电子|bili|B站|全彩|彩色|汉化")


def split_suffix(title):
    """'宿命恋人（东立电子版）' → ('宿命恋人', '东立电子版'); 无后缀 → (title, None)
    仅当尾部括号内容 ≤20 字且像版本标记时才剥离, 避免误伤正常标题括号"""
    m = re.search(r"（([^（）]{1,20})）$", title.strip())
    if m and SUFFIX_HINT.search(m.group(1)):
        return title.strip()[: m.start()], m.group(1)
    return title.strip(), None


def base_suffix(name):
    if "全彩" in name:
        return "全彩版"
    if "彩色" in name:
        return "彩色版"
    if "汉化" in name:
        return "汉化版"
    if "日文原版" in name:
        return "日文原版"
    elec = ("电子" in name) or ("電子" in name)
    for kw, pub in PUBS:
        if kw in name:
            return f"{pub}电子版" if elec else f"{pub}版"
    if elec:
        return "电子版"
    if "bili" in name.lower() or "B站" in name:
        return "bili版"
    return None


def vol_info(name):
    """文件夹名里的卷数: 'Vol.01-Vol.06'→'6卷'; '13完'→'13卷'; 单卷 'Vol.04'→'4卷'"""
    m = re.search(r"Vol\.(\d+)\s*[-～~]\s*Vol\.(\d+)", name, re.I)
    if m:
        return f"{int(m.group(2))}卷"
    m = re.search(r"(\d+)\s*[完卷]", name)
    if m and int(m.group(1)) < 100:
        return f"{int(m.group(1))}卷"
    m = re.search(r"Vol\.\s*(\d+)", name, re.I)
    if m:
        return f"{int(m.group(1))}卷"
    return None


def distinct_token(name, others, base=""):
    """name 独有的方括号片段（组内其他文件夹都没有的, 且与基础标题无包含关系）"""
    for t in re.findall(r"\[([^\]]{2,16})\]", name):
        if base and (t in base or base in t):
            continue  # 片段就是标题本身/含标题 → 当后缀毫无意义 (如 死亡笔记（死亡笔记）)
        if all(t not in o for o in others):
            return t
    return None


def plan_suffixes(members):
    """members: {sid: {"name","suffix"(已有后缀或None)}} → {sid: 新后缀}
    只给无后缀的成员定后缀; 保证组内互不相同且不与已有后缀重复"""
    plain = {sid: v for sid, v in members.items() if not v["suffix"]}
    used = {v["suffix"] for v in members.values() if v["suffix"]}
    names = {sid: v["name"] for sid, v in members.items()}
    base_title = next(iter(members.values())).get("base", "")
    bases = {sid: base_suffix(names[sid]) for sid in plain}
    vols = {sid: vol_info(names[sid]) for sid in plain}
    colliding = set()
    seen = {}
    for sid, b in bases.items():
        if b:
            seen.setdefault(b, []).append(sid)
    for b, sids in seen.items():
        if len(sids) > 1:
            colliding.update(sids)

    def chain(sid):
        c = []
        b, v = bases[sid], vols[sid]
        if sid in colliding and b and v:
            c.append(f"{b} {v}")
        if b:
            c.append(b)
        if v:
            c.append(v)
        t = distinct_token(names[sid], [names[o] for o in members if o != sid], base_title)
        if t:
            c.append(t)
        return [x for x in c if x]

    out = {}
    for sid in sorted(plain, key=lambda s: names[s]):
        pick = next((x for x in chain(sid) if x not in used), None)
        if pick:
            used.add(pick)
        out[sid] = pick
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写 (默认 dry-run)")
    args = ap.parse_args()
    cfg = scraper.load_config()
    k = Komga(cfg)
    lib = k.find_library(cfg["target"]["library_name"])
    all_series = k.all_series(lib["id"])

    groups = {}
    for s in all_series:
        m = s.get("metadata") or k.series_detail(s["id"])["metadata"]
        t = (m.get("title") or "").strip()
        if not t:
            continue
        base, suf = split_suffix(t)
        groups.setdefault(base, []).append({"id": s["id"], "name": s["name"],
                                            "title": t, "base": base, "suffix": suf})
    dups = {b: v for b, v in groups.items() if len(v) > 1}
    log(f"同名体检: 全库 {len(all_series)} 系列, 基础标题撞名组 {len(dups)} 个")

    plan = {}
    unresolved = 0
    for b, v in sorted(dups.items()):
        cnt = {}
        for x in v:
            cnt[x["title"]] = cnt.get(x["title"], 0) + 1
        dup_titles = {t for t, c in cnt.items() if c > 1}
        todo = [x for x in v if x["title"] in dup_titles]
        suffixed_dup = [x for x in todo if x["suffix"]]
        if not todo:
            continue  # 基础同名但显示已互异, 不动
        log(f"  组 [{b}]: " + " / ".join(f"{x['name'][:22]}={x['suffix'] or '无后缀'}" for x in v))
        for x in suffixed_dup:
            unresolved += 1
            log(f"     ? 带后缀仍重复, 需人工: {x['name'][:40]} ({x['title']})")
        plain_todo = [x for x in todo if not x["suffix"]]
        if not plain_todo:
            continue
        sufs = plan_suffixes({x["id"]: x for x in v})
        for sid, suf in sufs.items():
            x = next(i for i in v if i["id"] == sid)
            if suf:
                plan[sid] = (x, f"{b}（{suf}）")
                log(f"     {x['name'][:40]} -> {b}（{suf}）")
            else:
                unresolved += 1
                log(f"     ? 无法定后缀, 跳过: {x['name'][:40]}")
    log(f"计划改名 {len(plan)} 个, 无法处理 {unresolved} 个")
    if not args.apply or not plan:
        return

    r = subprocess.run(["bash", os.path.join(BASE, "snapshot_db.sh")], capture_output=True, text=True, timeout=600)
    log((r.stdout or "").strip())
    if r.returncode != 0:
        sys.exit("快照失败, 中止")
    run_dir = os.path.join(BASE, "journal", f"dupeditions_{dt.datetime.now():%Y%m%d_%H%M%S}")
    os.makedirs(run_dir, exist_ok=True)
    by_id = {s["id"]: s for s in all_series}
    for sid, (x, new) in plan.items():
        s = by_id[sid]
        save_json(os.path.join(run_dir, f"{sid}.json"),
                  {"series_id": sid, "act": "rename", "name": x["name"], "metadata": s.get("metadata") or {}})
        k.patch_metadata(sid, {"title": new, "titleSort": new, "titleLock": True, "titleSortLock": True})
    fresh = {y["id"]: y for y in k.all_series(lib["id"])}
    bad = 0
    for sid, (x, new) in plan.items():
        t = ((fresh.get(sid) or {}).get("metadata") or {}).get("title")
        if t != new:
            bad += 1
            log(f"  ! 未生效 {x['name'][:36]}: {t}")
    log(f"核对: {len(plan)} 项, 异常 {bad}, journal -> {run_dir}")


if __name__ == "__main__":
    main()
