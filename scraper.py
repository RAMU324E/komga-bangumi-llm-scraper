#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Komga × Bangumi 自动刮削器 (日漫库专用)
依据: 文件夹原名 + 第一话第1页原图 (不受旧刮削器污染)
判定:   标题文字 + 封面视觉 + 简介语义 三方核对, deepseek-flash 多模态判定
红线:   只碰配置指定的库 / 只写主库 / 无快照不写 / 无journal不写 / 置信度不足不写
用法:
  python3 scraper.py                      # dry-run (默认, 只读+报告)
  python3 scraper.py --apply              # 真写 (强制先全库快照)
  python3 scraper.py --apply --covers     # 追加封面档
  python3 scraper.py --limit 5            # 只跑前5个系列 (测试)
  python3 scraper.py --series ID1,ID2     # 指定系列
"""
import argparse
import base64
import collections
import concurrent.futures as cf
import datetime as dt
import difflib
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUN_TS = dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ============================================================ 提示词 (项目灵魂) ===

TITLE_CLEAN_PROMPT = """你是漫画文件夹名解析器。输入漫画库的文件夹名列表，其中含大量噪音：汉化组/出版社/作者名、卷数范围、完结状态、版本与来源标记。

任务：为每个文件夹名提取「作品主标题」——将用于在 Bangumi 番组计划数据库搜索该漫画。

规则：
1. 去掉与作品名无关的方括号内容（作者、汉化组、出版社、"电子版/全彩/豪华版/数码版"等来源标记），但注意有些方括号内是作品名本体
2. 去掉卷数与完结标记（Vol.xx / 第xx卷 / xx卷 / xx完 / xx未完 / 1-34 / 完 / 未完）
3. 保留作品序号与副标题（PART8、II、第2季、新装版、Λ等）——它们是标题的一部分，用于区分不同部
4. 繁体保留繁体，日文原名保留日文，不要翻译
5. title = 最可能用于搜索的通用名称（中文译名或原名均可，取文件夹中实际出现的形式）
6. alt = 若能判断另一语言/写法的名称（如日文原名或罗马字）则给出，否则 null

学习以下示例：
- "[安達充][H2][天下][C.C][34完]" → title="H2", alt=null （第2个方括号是作品名）
- "[JoJo的奇妙冒险PART8乔乔福音][荒木飛呂彦][27完][数码全彩]" → title="JoJo的奇妙冒险PART8乔乔福音", alt="ジョジョの奇妙な冒険 Part8"
- "[少年的深渊][峰浪りょう][9未完][青文][电子版]" → title="少年的深渊", alt="少年のアビス"
- "[GANTZ殺戮都市][奧浩哉][全彩Vol.01-Vol.37]" → title="GANTZ殺戮都市", alt="GANTZ"
- "[CLAMP][TSUBASA翼-WoRLD CHRoNiCLE-][3完]" → title="TSUBASA翼-WoRLD CHRoNiCLE-", alt=null （首括号是作者CLAMP）

输入: {"names": ["...", ...]}
仅输出 JSON（禁止任何多余文字）: {"results": [{"i": <行号从0>, "title": "...", "alt": "..."或null}, ...]}
输入的每一行必须恰好对应一个输出项，行号不能跳。"""

WEB_ALT_PROMPT = """你是漫画数据库查证员。给定一个漫画文件夹名/中文译名，请用 web_search 工具联网搜索，确定该漫画作品的【日文原名】（原题名）。

要求：
- 必须实际调用搜索验证，禁止仅凭记忆回答
- 日文原名 = 作者所在国出版时的原题（日文汉字/假名/罗马字均可）；若作品原著标题即英文，输出英文原名
- 注意区分同名不同作品、同系列不同部（II / Part8 / 第2季 是不同部）
- 若无法确定，japanese_title 输出 null

文件夹名: {folder}
解析出的标题: {title}

仅输出 JSON: {{"japanese_title": "..." 或 null, "confidence": 0-100, "evidence": "≤40字来源说明"}}"""

VLM_PROMPT_TEMPLATE = """你是资深漫画编辑。任务：判断下列 Bangumi 候选条目中，哪一个与「本地漫画系列」是【同一部作品】。这是元数据刮削的匹配判断，错配会污染数据库，宁可漏配不可错配。

【本地系列】
- 文件夹名: {folder}
- 解析标题: {clean_title}{alt_part}
- 首卷封面: 【图片1】（该漫画第一话的封面页，即单行本封面或首话扉页）

【候选条目】共 {n} 个，封面依次为【图片2】~【图片{n_plus1}】
{candidates_block}

【判断方法】逐项独立评估，写入 evidence：
1. 标题：字面一致？还是「不同译名/繁简体/罗马字/副标题省略」形式的同一作品？两岸三地译名可能完全不同（如 "H2" 台译 "好逑双物语"），不能只看字面差异
2. 封面：构图、角色、画风、logo、标题文字是否指向同一作品？注意：同一作品不同卷/不同版的封面不同，画风与角色阵容一致即可；卷号差异不算冲突
3. 简介：讲的是否同一个故事？（主角名、设定、情节要点）

【硬性规则】
- 续作/前传/番外/不同部 ≠ 同一部（JoJo 第8部≠第7部；标题编号 II≠III≠无编号）
- 同一作品的不同版本（全彩版/豪华版/新装版/电子版）= 同一部，可以匹配
- 同名不同作品：靠封面和简介区分
- 没有足够证据时 best_subject_id 必须为 null

【confidence 校准】
- 95-100：标题、封面、简介三项全部吻合
- 85-94：两项强吻合，第三项无冲突
- 70-84：标题吻合但封面或简介有无法解释的差异
- 低于70：证据不足，应返回 null

【输出】仅输出 JSON（best_subject_id 只能从上方候选 ID 中选择，禁止编造不存在的 ID）：
{{"best_subject_id": <候选ID整数或null>, "confidence": <0-100整数>, "evidence": {{"title": "一致/译名差异/不一致", "cover": "一致/相似/不一致/无法判断", "summary": "一致/相似/不一致/缺失"}}, "reason": "≤50字中文理由"}}"""


# ============================================================ 基础设施 ===

def load_config():
    p = os.path.join(BASE_DIR, "config.json")
    if not os.path.exists(p):
        sys.exit("ERROR: 缺少 config.json")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def cache_json(path, default=None):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def extract_json(txt):
    try:
        return json.loads(txt)
    except Exception:
        pass
    m = re.search(r"\{.*\}", txt, re.S)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            return None
    return None


def norm(s):
    """标题归一化: 全角转半角、去空白与标点，用于确定性比对"""
    if not s:
        return ""
    s = s.lower()
    s = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in s)
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[·・\-—_:：!！?？~～'\"`()（）\[\]【】.,。、;；]", "", s)
    return s


def is_ok_image(b):
    """DeepSeek 可接受: JPEG/PNG 且 ≤4MB"""
    return bool(b) and len(b) <= 4_000_000 and (b[:3] == b"\xff\xd8\xff" or b[:8] == b"\x89PNG\r\n\x1a\n")


def compress_jpeg(b, limit=900_000):
    """Komga 缩略图上传限制≈1MB (超了 413), 超限则 Pillow 降质量/缩尺寸"""
    if len(b) <= limit:
        return b
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(b)).convert("RGB")
    q = 88
    while True:
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q, optimize=True)
        if buf.tell() <= limit or q <= 40:
            return buf.getvalue()
        if q > 60:
            q -= 10
        else:
            im = im.resize((im.width * 4 // 5, im.height * 4 // 5))


class RateLimiter:
    def __init__(self, rps):
        self.interval = 1.0 / max(rps, 0.1)
        self.lock = threading.Lock()
        self.t = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            w = self.t + self.interval - now
            if w > 0:
                time.sleep(w)
                now = time.monotonic()
            self.t = now


# ============================================================ API 客户端 ===

class Komga:
    def __init__(self, cfg):
        self.base = cfg["komga"]["base_url"].rstrip("/")
        self.s = requests.Session()
        self.s.headers["X-API-Key"] = cfg["komga"]["api_key"]
        self.timeout = 60

    def get_json(self, path):
        r = self.s.get(self.base + path, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_bytes(self, path):
        r = self.s.get(self.base + path, timeout=self.timeout)
        r.raise_for_status()
        return r.content

    def find_library(self, name):
        libs = self.get_json("/api/v1/libraries")
        m = [l for l in libs if l["name"] == name]
        if len(m) != 1:
            sys.exit(f"ERROR: 库 '{name}' 匹配到 {len(m)} 个 (红线: 库名白名单必须唯一), 中止")
        return m[0]

    def all_series(self, lib_id):
        out, page = [], 0
        while True:
            d = self.get_json(f"/api/v1/series?library_id={lib_id}&page={page}&size=100")
            out.extend(d.get("content") or [])
            if d.get("last", True):
                break
            page += 1
        return out

    def first_book(self, sid):
        d = self.get_json(f"/api/v1/series/{sid}/books?page=0&size=1&sort=metadata.numberSort,asc")
        c = d.get("content") or []
        return c[0] if c else None

    def page_image(self, bid, num=1):
        return self.get_bytes(f"/api/v1/books/{bid}/pages/{num}")

    def safe_page(self, bid):
        """首页图; PDF/AVIF/超大图 (DeepSeek 400) 改用 Komga 缩略图 (已渲染的小 JPEG)"""
        img = self.page_image(bid)
        return img if is_ok_image(img) else self.get_bytes(f"/api/v1/books/{bid}/thumbnail")

    def series_detail(self, sid):
        return self.get_json(f"/api/v1/series/{sid}")

    def patch_metadata(self, sid, payload):
        r = self.s.patch(f"{self.base}/api/v1/series/{sid}/metadata", json=payload, timeout=self.timeout)
        r.raise_for_status()

    def list_thumbs(self, sid):
        return self.get_json(f"/api/v1/series/{sid}/thumbnails")

    def upload_cover(self, sid, jpg_bytes):
        r = self.s.post(f"{self.base}/api/v1/series/{sid}/thumbnails?selected=true",
                        files={"file": ("cover.jpg", compress_jpeg(jpg_bytes), "image/jpeg")}, timeout=120)
        r.raise_for_status()

    def delete_thumb(self, sid, tid):
        r = self.s.delete(f"{self.base}/api/v1/series/{sid}/thumbnails/{tid}", timeout=self.timeout)
        r.raise_for_status()


class Bangumi:
    def __init__(self, cfg):
        self.base = cfg["bangumi"]["base_url"].rstrip("/")
        self.s = requests.Session()
        self.s.headers["User-Agent"] = cfg["bangumi"]["user_agent"]
        self.rl = RateLimiter(cfg["bangumi"].get("requests_per_second", 1.0))
        self.top = cfg["bangumi"].get("search_top", 6)

    def search(self, kw):
        self.rl.wait()
        try:
            r = self.s.post(f"{self.base}/v0/search/subjects",
                            json={"keyword": kw, "filter": {"type": [1]}}, timeout=30)
            if r.status_code == 200:
                return [x["id"] for x in (r.json().get("data") or [])[:self.top]]
        except Exception:
            pass
        try:  # 旧版端点兜底
            from urllib.parse import quote
            self.rl.wait()
            r = self.s.get(f"{self.base}/search/subject/{quote(kw)}?type=1&max_results={self.top}", timeout=30)
            if r.status_code == 200:
                return [x["id"] for x in (r.json().get("list") or [])[:self.top]]
        except Exception:
            pass
        return []

    def subject(self, sid, cache_dir):
        fp = os.path.join(cache_dir, f"{sid}.json")
        if os.path.exists(fp):
            with open(fp, encoding="utf-8") as f:
                return json.load(f)
        self.rl.wait()
        try:
            r = self.s.get(f"{self.base}/v0/subjects/{sid}", timeout=30)
            if r.status_code != 200:
                return None
            d = r.json()
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            return d
        except Exception:
            return None

    def related(self, sid, paths):
        """条目关联 (缓存 cache/bgm_relations/<sid>.json): 单卷→系列, 小说→漫画版等"""
        fp = os.path.join(paths["cache"], "bgm_relations", f"{sid}.json")
        if os.path.exists(fp):
            with open(fp, encoding="utf-8") as f:
                return json.load(f)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        self.rl.wait()
        try:
            r = self.s.get(f"{self.base}/v0/subjects/{sid}/subjects?limit=50", timeout=30)
            data = r.json() if r.status_code == 200 else []
            if isinstance(data, dict):
                data = data.get("data") or []
        except Exception:
            data = []
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return data

    def cover(self, sub, cache_dir):
        url = (sub.get("images") or {}).get("large") or (sub.get("images") or {}).get("common")
        if not url:
            return None
        fp = os.path.join(cache_dir, f"bgm_{sub['id']}.jpg")
        if os.path.exists(fp):
            with open(fp, "rb") as f:
                return f.read()
        self.rl.wait()
        try:
            r = requests.get(url, headers={"User-Agent": self.s.headers["User-Agent"]}, timeout=30)
            if r.status_code != 200:
                return None
            with open(fp, "wb") as f:
                f.write(r.content)
            return r.content
        except Exception:
            return None


class DeepSeek:
    def __init__(self, cfg):
        c = cfg["deepseek"]
        self.url = c["base_url"].rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {c['api_key']}"}
        self.base_url = c["base_url"].rstrip("/")
        self.api_key = c["api_key"]
        self.model = c.get("model", "deepseek-flash")
        self.max_tokens = c.get("max_tokens", 4000)
        self.temperature = c.get("temperature", 0.1)
        self.retries = c.get("max_retries", 2)
        self.timeout = c.get("timeout_seconds", 120)

    def chat(self, content, json_mode=True):
        body = {"model": self.model,
                "messages": [{"role": "user", "content": content}],
                "max_tokens": self.max_tokens,
                "temperature": self.temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        last = None
        for i in range(self.retries + 1):
            try:
                b = dict(body)
                if i > 0:
                    b.pop("response_format", None)  # 重试时放宽格式约束
                r = requests.post(self.url, headers=self.headers, json=b, timeout=self.timeout)
                r.raise_for_status()
                msg = r.json()["choices"][0]["message"]
                txt = (msg.get("content") or "").strip()
                if not txt:
                    raise ValueError("空回复(思维链耗尽额度?)")
                return txt
            except Exception as e:
                last = e
                time.sleep(2 * (i + 1))
        raise last

    def web_alt(self, title, folder):
        """Anthropic 兼容端点 + 服务端联网搜索, 查证日文原名
        (用于 alt 缺失/搜索无结果的系列; Responses API 的 web_search 被官方 Ignored, 此端点实测可用)"""
        url = self.base_url + "/anthropic/v1/messages"
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        body = {"model": self.model,
                "max_tokens": 8000,
                "messages": [{"role": "user",
                              "content": WEB_ALT_PROMPT.format(folder=folder, title=title)}],
                "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]}
        for i in range(self.retries + 1):
            try:
                r = requests.post(url, headers=headers, json=body, timeout=180)
                r.raise_for_status()
                d = r.json()
                txt = "".join(b.get("text", "") for b in (d.get("content") or []) if b.get("type") == "text")
                j = extract_json(txt)
                if not j:
                    raise ValueError("web_alt 输出无法解析")
                return j
            except Exception:
                time.sleep(2 * (i + 1))
        return None


# ============================================================ 阶段 1: 采集 ===

def phase_collect(komga, lib, paths, args):
    fp = os.path.join(paths["cache"], "series_index.json")
    index = cache_json(fp, {}) or {}
    series_list = komga.all_series(lib["id"])
    sids = [s["id"] for s in series_list]
    if args.series:
        want = set(args.series)
        sids = [s for s in sids if s in want]
        missing = want - set(sids)
        if missing:
            sys.exit(f"ERROR: 指定的系列不存在: {missing}")
    if args.limit:
        sids = sorted(sids)[: args.limit]
    skip_fp = os.path.join(BASE_DIR, "skip_series.json")
    if os.path.exists(skip_fp) and not args.series:
        skip_ids = set(json.load(open(skip_fp, encoding="utf-8")))
        before = len(sids)
        sids = [s for s in sids if s not in skip_ids]
        log(f"永久跳过名单 skip_series.json: {len(skip_ids)} 个, 剔除 {before - len(sids)}")
    log(f"采集: 目标 {len(sids)} 系列 (索引缓存 {len(index)})")

    todo = [s for s in sids if s not in index or not index[s].get("first_book_id")]
    if todo:
        def fetch(sid):
            try:
                b = komga.first_book(sid)
                if b:
                    imgp = os.path.join(paths["pages"], f"{sid}.jpg")
                    if not os.path.exists(imgp):
                        img = komga.safe_page(b["id"])
                        with open(imgp, "wb") as f:
                            f.write(img)
                return sid, b
            except Exception:
                return sid, None

        with cf.ThreadPoolExecutor(4) as ex:
            for sid, b in ex.map(fetch, todo):
                index[sid] = {"first_book_id": b["id"] if b else None}
    # 已缓存但格式不兼容的首页 (历史 PDF/AVIF/大图) → 换缩略图
    for sid in sids:
        imgp = os.path.join(paths["pages"], f"{sid}.jpg")
        bid = (index.get(sid) or {}).get("first_book_id")
        if bid and os.path.exists(imgp) and not is_ok_image(open(imgp, "rb").read()):
            try:
                with open(imgp, "wb") as f:
                    f.write(komga.get_bytes(f"/api/v1/books/{bid}/thumbnail"))
                log(f"  首页换缩略图: {sid}")
            except Exception as e:
                log(f"  首页修复失败 {sid}: {e}")
    for s in series_list:
        if s["id"] in index and "name" not in index[s["id"]]:
            index[s["id"]]["name"] = s["name"]
            index[s["id"]]["metadata"] = s.get("metadata") or {}
    save_json(fp, index)
    return {sid: index[sid] for sid in sids}


# ============================================================ 阶段 2: 标题清洗 ===

def phase_clean_titles(ds, series_map, paths, cfg):
    fp = os.path.join(paths["cache"], "titles.json")
    titles = cache_json(fp, {}) or {}
    todo = [sid for sid in series_map if sid not in titles]
    batch = cfg["match"].get("title_clean_batch", 20)
    log(f"标题清洗: 待处理 {len(todo)} (缓存 {len(titles)})")
    if todo:
        chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]

        def clean_chunk(chunk):
            names = [series_map[sid]["name"] for sid in chunk]
            try:
                txt = ds.chat(TITLE_CLEAN_PROMPT + '\n\n输入: ' + json.dumps({"names": names}, ensure_ascii=False))
                data = extract_json(txt) or {}
            except Exception:
                return {}  # 单批失败不炸全run, 缺失的由原文件夹名兑底
            res = data.get("results") or []
            out = {}
            for r in res:
                i = r.get("i")
                if isinstance(i, int) and 0 <= i < len(chunk):
                    out[chunk[i]] = {"title": (r.get("title") or "").strip(),
                                     "alt": (r.get("alt") or "").strip() or None}
            return out

        with cf.ThreadPoolExecutor(8) as ex:
            for n, part in enumerate(ex.map(clean_chunk, chunks)):
                titles.update(part)
                if (n + 1) % 10 == 0:
                    save_json(fp, titles)
                    log(f"  清洗进度 {n + 1}/{len(chunks)} 批")
        miss = [sid for sid in todo if sid not in titles]
        for sid in miss:  # 兜底: 清洗失败的用原名
            titles[sid] = {"title": series_map[sid]["name"], "alt": None}
        if miss:
            log(f"标题清洗: {len(miss)} 个未返回, 已用原文件夹名兜底")
        save_json(fp, titles)
    return titles


# ============================================================ 阶段 3: 搜索+取条目 ===

def phase_search(bg, series_map, titles, paths):
    search_fp = os.path.join(paths["cache"], "search.json")
    searches = cache_json(search_fp, {}) or {}
    todo = [sid for sid in series_map if sid not in searches]
    log(f"Bangumi 搜索: 待处理 {len(todo)} (缓存 {len(searches)})")
    for n, sid in enumerate(todo):
        t = titles.get(sid) or {}
        queries = [t.get("title") or series_map[sid]["name"]]
        if t.get("alt") and norm(t["alt"]) != norm(queries[0]):
            queries.append(t["alt"])
        seen, ids = set(), []
        for q in queries:  # 多查询合并去重提升召回 (主查询的结果可能全是同系列其他作品)
            for i in bg.search(q):
                if i not in seen:
                    seen.add(i)
                    ids.append(i)
        if not ids:
            ids = bg.search(series_map[sid]["name"])  # 原始文件夹名兜底
        searches[sid] = ids
        if (n + 1) % 50 == 0:
            save_json(search_fp, searches)
            log(f"  搜索进度 {n + 1}/{len(todo)}")
    save_json(search_fp, searches)
    return searches


def phase_subjects(bg, searches, paths):
    subj_dir = paths["bgm_subjects"]
    need = set()
    for ids in searches.values():
        need.update(ids)
    cached = {f[:-5] for f in os.listdir(subj_dir) if f.endswith(".json")}
    log(f"Bangumi 条目详情: 需 {len(need)} 个 (缓存 {len(cached & {str(i) for i in need})})")
    for n, sid in enumerate(need - {int(x) for x in cached}):
        bg.subject(sid, subj_dir)
        if (n + 1) % 100 == 0:
            log(f"  条目进度 {n + 1}")
    subjects, novels = {}, {}
    for sid in need:
        d = cache_json(os.path.join(subj_dir, f"{sid}.json"))
        if not d:
            continue
        if (d.get("platform") or "") == "小说":  # 漫画刮削器: 候选剔除小说
            novels[sid] = d
        else:
            subjects[sid] = d
    if novels:
        log(f"漫画刮削器: 候选剔除小说条目 {len(novels)} 个")
    rescued = 0
    for series_id, ids in list(searches.items()):
        if not ids or any(i in subjects for i in ids):
            continue
        # 该系列候选全是小说 → 沿小说关联补漫画版 (如 我的青春@comic)
        extra = []
        for nid in ids:
            for rel in bg.related(nid, paths) or []:
                if rel.get("type") == 1:
                    extra.append(rel["id"])
        keep = []
        for rid in dict.fromkeys(extra):
            d = bg.subject(rid, subj_dir)
            if d and (d.get("platform") or "") != "小说":
                subjects[rid] = d
                keep.append(rid)
        if keep:
            searches[series_id] = list(dict.fromkeys(list(ids) + keep))
            rescued += 1
            log(f"  {series_id} 候选只有小说 → 沿关联补漫画版 {keep[:4]}")
    if rescued:
        save_json(os.path.join(paths["cache"], "searches.json"), searches)
    return subjects


# ============================================================ 阶段 3.5: 联网原名补全 ===

def phase_webalt(ds, bg, series_map, titles, searches, paths, cfg):
    """对 alt 为空或搜索无结果的系列, 用 deepseek 原生联网搜索查证日文原名,
    查到后以原名重搜 Bangumi 并合并候选 (只增不减, 原三连搜结果全保留)"""
    m = cfg["match"]
    if not m.get("web_search_alt", True):
        return searches
    fp = os.path.join(paths["cache"], "web_alts.json")
    web = cache_json(fp, {}) or {}
    weak = [sid for sid in series_map
            if sid not in web and (not (titles.get(sid) or {}).get("alt") or not searches.get(sid))]
    log(f"联网原名补全: 待处理 {len(weak)} (缓存 {len(web)})")
    if weak:
        conc = cfg["deepseek"].get("web_alt_concurrency", 8)

        def ask(sid):
            t = (titles.get(sid) or {}).get("title") or series_map[sid]["name"]
            return sid, ds.web_alt(t, series_map[sid]["name"])

        with cf.ThreadPoolExecutor(conc) as ex:
            for n, (sid, j) in enumerate(ex.map(ask, weak)):
                web[sid] = j
                if (n + 1) % 25 == 0:
                    save_json(fp, web)
                    log(f"  原名补全进度 {n + 1}/{len(weak)}")
        save_json(fp, web)
    floor = m.get("web_alt_confidence_floor", 70)
    merged = 0
    for sid, j in web.items():
        if sid not in series_map:
            continue
        jp = (j or {}).get("japanese_title")
        if not jp or int((j or {}).get("confidence") or 0) < floor:
            continue
        t = titles.setdefault(sid, {})
        if norm(jp) in (norm(t.get("alt") or ""), norm(t.get("title") or "")):
            continue
        t["alt_web"] = jp
        old = searches.get(sid) or []
        add = [i for i in bg.search(jp) if i not in set(old)]
        if add:
            searches[sid] = old + add
            merged += 1
            for cache_name in ("scores.json", "vlm.json"):
                cp = os.path.join(paths["cache"], cache_name)
                cdata = cache_json(cp, {}) or {}
                if sid in cdata:
                    del cdata[sid]  # 候选已变, 旧判决作废重算
                    save_json(cp, cdata)
    save_json(os.path.join(paths["cache"], "titles.json"), titles)
    save_json(os.path.join(paths["cache"], "search.json"), searches)
    ok_n = sum(1 for j in web.values() if (j or {}).get("japanese_title"))
    log(f"原名补全: 查得原名 {ok_n} 个, 新增候选系列 {merged} 个 (置信下限 {floor})")
    return searches


# ============================================================ 阶段 4: 确定性打分 ===

def infobox_value(sub, key):
    for item in sub.get("infobox") or []:
        if item.get("key") == key:
            v = item.get("value")
            if isinstance(v, str):
                return v
            if isinstance(v, list):
                return " ".join(x.get("v", "") for x in v if isinstance(x, dict))
    return None


def score_one(item, tinfo, subs):
    folder = item["name"]
    n_folder, n_clean = norm(folder), norm(tinfo.get("title") or folder)
    n_alt = norm(tinfo.get("alt") or "")
    n_web = norm(tinfo.get("alt_web") or "")
    folder_year = re.search(r"(19|20)\d{2}", folder)
    per = {}
    for sub in subs:
        cands = [norm(x) for x in (sub.get("name_cn"), sub.get("name")) if x]
        ratios = []
        for target in filter(None, [n_clean, n_alt, n_web]):
            for c in cands:
                ratios.append(difflib.SequenceMatcher(None, target, c).ratio())
        s = (max(ratios) * 100) if ratios else 0.0
        if n_folder and n_folder in cands:
            s = 100.0  # 原始文件夹名与条目名完全一致
        if n_alt and n_alt in cands:
            s = 100.0  # 备选名(日文原名等)完全一致
        if n_web and n_web in cands:
            s = 100.0  # 联网查证原名完全一致
        author = infobox_value(sub, "作者")
        if author and any(a.strip() and a.strip() in folder for a in re.split(r"[、,，/]", author) if a.strip()):
            s += 8
        date = sub.get("date") or ""
        if folder_year and date and abs(int(folder_year.group()) - int(date[:4])) <= 1:
            s += 4
        vol_pat = re.compile(r"[（(]\s*\d+\s*[)）]\s*$")
        if vol_pat.search(sub.get("name") or "") or vol_pat.search(sub.get("name_cn") or ""):
            s -= 15  # 单行本卷条目降权, 优先系列级条目 (同分时不会被高人气的本篇卷挤掉)
        per[sub["id"]] = min(100, round(s))
    best_id = max(per, key=per.get) if per else None
    return {"score": per.get(best_id, 0) if best_id else 0, "subject_id": best_id, "per_subject": per}


def phase_score(series_map, titles, searches, subjects, paths):
    fp = os.path.join(paths["cache"], "scores.json")
    scores = cache_json(fp, {}) or {}
    todo = [sid for sid in series_map if sid not in scores
            or (scores[sid].get("subject_id") is not None and scores[sid]["subject_id"] not in subjects)]
    log(f"规则打分: 待处理 {len(todo)} (缓存 {len(scores)})")
    for sid in todo:
        subs = [subjects[i] for i in searches.get(sid, []) if i in subjects]
        scores[sid] = score_one(series_map[sid], titles.get(sid) or {}, subs) or {"score": 0, "subject_id": None}
    save_json(fp, scores)
    return scores


# ============================================================ 阶段 5: VLM 判定 ===

def vlm_one(ds, bg, sid, item, tinfo, ids, subjects, paths, cfg, per_subject=None):
    max_c = cfg["match"].get("max_candidates", 6)
    per = per_subject or {}
    subs = sorted((subjects[i] for i in ids if i in subjects),
                  key=lambda s: (-per.get(s["id"], 0), -((s.get("rating") or {}).get("score") or 0)))[:max_c]
    if not subs:
        return {"subject_id": None, "confidence": 0, "reason": "无候选"}
    page_fp = os.path.join(paths["pages"], f"{sid}.jpg")
    if not os.path.exists(page_fp):
        return {"subject_id": None, "confidence": 0, "reason": "无首页图片(无书籍?)"}
    cands_txt = []
    for idx, sub in enumerate(subs, 1):
        tags = "/".join(t.get("name", "") for t in (sub.get("tags") or [])[:5])
        summary = re.sub(r"\s+", " ", sub.get("summary") or "")[:280]
        cands_txt.append(f"[{idx}] ID={sub['id']}\n    日文原名: {sub.get('name') or '?'}\n"
                         f"    中文名: {sub.get('name_cn') or '(无)'}\n    发售日: {sub.get('date') or '?'}\n"
                         f"    标签: {tags or '(无)'}\n    简介: {summary or '(无)'}")
    alt_v = tinfo.get("alt") or tinfo.get("alt_web")
    alt_part = f" (备选: {alt_v})" if alt_v else ""
    prompt = VLM_PROMPT_TEMPLATE.format(
        folder=item["name"], clean_title=tinfo.get("title") or item["name"], alt_part=alt_part,
        n=len(subs), n_plus1=len(subs) + 1, candidates_block="\n".join(cands_txt))

    parts = [{"type": "text", "text": prompt}]
    with open(page_fp, "rb") as f:
        parts.append({"type": "image_url",
                      "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()}})
    for sub in subs:
        cov = bg.cover(sub, paths["bgm_covers"])
        if cov:
            parts.append({"type": "image_url",
                          "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(cov).decode()}})
        else:
            return {"subject_id": None, "confidence": 0, "reason": "候选封面下载失败"}

    try:
        txt = ds.chat(parts, json_mode=True)
    except Exception as e:
        return {"subject_id": None, "confidence": 0, "reason": f"LLM失败: {str(e)[:60]}"}
    d = extract_json(txt)
    if not d:
        return {"subject_id": None, "confidence": 0, "reason": "LLM输出无法解析"}
    pick, conf = d.get("best_subject_id"), int(d.get("confidence") or 0)
    valid = {s["id"] for s in subs}
    if pick is not None and pick not in valid:
        pick, conf = None, 0  # 防幻觉: 编造的ID一律作废
    return {"subject_id": pick, "confidence": conf,
            "evidence": d.get("evidence") or {}, "reason": (d.get("reason") or "")[:80]}


def phase_vlm(ds, bg, series_map, titles, searches, subjects, scores, paths, cfg, m):
    fp = os.path.join(paths["cache"], "vlm.json")
    vlm = cache_json(fp, {}) or {}
    always = m.get("always_vlm", True)  # 精确命中也过VLM复核, 双重确认才进A档

    def needs_vlm(sid):
        sc = scores[sid]
        if always and sc["score"] >= m["exact_score"] and sc.get("subject_id"):
            return True
        return m["search_floor"] <= sc["score"] < m["exact_score"]

    todo = [sid for sid in series_map if needs_vlm(sid)
            and (sid not in vlm or "LLM失败" in (vlm[sid].get("reason") or "")  # 调用失败≠否决, 重试
                 or (vlm[sid].get("subject_id") is not None and vlm[sid]["subject_id"] not in subjects))]
    log(f"VLM 判定: 待处理 {len(todo)} (缓存 {len(vlm)})  并发={cfg['deepseek'].get('concurrency', 16)}  always_vlm={always}")

    def work(sid):
        r = vlm_one(ds, bg, sid, series_map[sid], titles.get(sid) or {},
                    searches.get(sid, []), subjects, paths, cfg,
                    (scores.get(sid) or {}).get("per_subject"))
        return sid, r

    with cf.ThreadPoolExecutor(cfg["deepseek"].get("concurrency", 16)) as ex:
        for n, (sid, r) in enumerate(ex.map(work, todo)):
            vlm[sid] = r
            if (n + 1) % 25 == 0:
                save_json(fp, vlm)
                log(f"  VLM 进度 {n + 1}/{len(todo)}")
    save_json(fp, vlm)
    return vlm


def phase_vlm_retry(ds, bg, series_map, titles, searches, subjects, scores, vlm, paths, cfg, m):
    """AI 拒绝/置信不足 → 再问 N 次 (默认3), ≥多数次认定同一条目(置信≥vlm_accept)才采纳
    排除偶发的错误拒绝, 同时不让偶发的误通过混进来; 结果记在 vlm[sid]['votes'], 幂等不重复问"""
    n = m.get("vlm_retry", 3)
    if n <= 0:
        return vlm
    fp = os.path.join(paths["cache"], "vlm.json")
    acc = m["vlm_accept"]
    todo = [sid for sid, v in vlm.items() if sid in series_map and "votes" not in v
            and (scores.get(sid) or {}).get("subject_id")
            and "LLM失败" not in (v.get("reason") or "")
            and (not v.get("subject_id") or v.get("confidence", 0) < acc)]
    log(f"VLM 拒绝重试: 待处理 {len(todo)} 个, 每个再问 {n} 次 (≥{n // 2 + 1} 次一致才采纳)")

    def work(sid):
        rs = [vlm_one(ds, bg, sid, series_map[sid], titles.get(sid) or {}, searches.get(sid, []), subjects,
                      paths, cfg, (scores.get(sid) or {}).get("per_subject")) for _ in range(n)]
        return sid, rs

    changed = 0
    with cf.ThreadPoolExecutor(cfg["deepseek"].get("concurrency", 16)) as ex:
        for sid, rs in ex.map(work, todo):
            orig = vlm[sid]
            votes = [(r.get("subject_id"), r.get("confidence", 0)) for r in rs]
            ok = [r for r in rs if r.get("subject_id") and r.get("confidence", 0) >= acc]
            top = collections.Counter(r["subject_id"] for r in ok).most_common(1)
            if top and top[0][1] >= n // 2 + 1:
                best = max((r for r in ok if r["subject_id"] == top[0][0]), key=lambda r: r["confidence"])
                vlm[sid] = dict(best, reason=f"重试{n}次{top[0][1]}次一致: {(best.get('reason') or '')[:50]}",
                                votes=votes, first=orig)
                changed += 1
            else:
                vlm[sid] = dict(orig, votes=votes)
    save_json(fp, vlm)
    log(f"VLM 拒绝重试完成: {changed}/{len(todo)} 个经重试多数一致改判")
    return vlm


# ============================================================ 阶段 6: 决策与报告 ===

def phase_decisions(series_map, titles, searches, subjects, scores, vlm, m):
    vol_pat = re.compile(r"\s*[（(]\s*\d+\s*[)）]\s*$")

    def same_work_volume(pick, rule_id):
        """VLM 选的是规则所选作品的单行本卷条目 (如 'CLAYMORE (2)' vs 系列 'CLAYMORE') → 视为一致"""
        p, r = subjects.get(pick) or {}, subjects.get(rule_id) or {}
        pn = {norm(vol_pat.sub("", x)) for x in (p.get("name"), p.get("name_cn")) if x and vol_pat.search(x)}
        rn = {norm(x) for x in (r.get("name"), r.get("name_cn")) if x}
        return bool(pn & rn)

    floor = m.get("vlm_review_floor", 0)

    def rule_score(sc, subject_id):
        per = sc.get("per_subject") or {}
        return per.get(str(subject_id), per.get(subject_id, 0))

    decisions = {}
    for sid, item in series_map.items():
        sc = scores[sid]
        if not sc["subject_id"]:
            decisions[sid] = {"tier": "C", "reason": "无候选/低分"}
            continue
        if sc["score"] >= m["exact_score"]:
            v = vlm.get(sid)
            if m.get("always_vlm", True) and v:
                if v.get("subject_id") == sc["subject_id"] and v.get("confidence", 0) >= m["vlm_accept"]:
                    decisions[sid] = {"tier": "A", "subject_id": sc["subject_id"], "score": sc["score"],
                                      "confidence": v["confidence"],
                                      "reason": f"精确命中+VLM复核一致: {(v.get('reason') or '')[:40]}",
                                      "by": "规则+VLM"}
                elif (v.get("subject_id") and v.get("confidence", 0) >= m["vlm_accept"]
                      and same_work_volume(v["subject_id"], sc["subject_id"])):
                    decisions[sid] = {"tier": "A", "subject_id": sc["subject_id"], "score": sc["score"],
                                      "confidence": v["confidence"],
                                      "reason": f"精确命中+VLM选同作品卷条目{v['subject_id']}, 取系列条目",
                                      "by": "规则+VLM"}
                elif (v.get("subject_id") and v.get("confidence", 0) >= m["vlm_accept"]
                      and rule_score(sc, v["subject_id"]) >= m["exact_score"]):
                    # 规则并列满分 (如同名旧版/新装版、小说/漫画), VLM 选的也是规则满分条目 → 两路都支持
                    decisions[sid] = {"tier": "A", "subject_id": v["subject_id"], "score": sc["score"],
                                      "confidence": v["confidence"],
                                      "reason": f"规则并列满分, 取VLM所选: {(v.get('reason') or '')[:40]}",
                                      "by": "规则+VLM"}
                elif v.get("subject_id"):
                    decisions[sid] = {"tier": "B", "subject_id": v["subject_id"], "score": sc["score"],
                                      "confidence": v.get("confidence", 0),
                                      "reason": f"规则选{sc['subject_id']}但VLM选此候选, 人工判定", "by": "VLM"}
                else:
                    decisions[sid] = {"tier": "B", "score": sc["score"], "confidence": v.get("confidence", 0),
                                      "reason": f"精确命中但VLM复核拒绝: {(v.get('reason') or '')[:50]}", "by": "VLM"}
            else:
                decisions[sid] = {"tier": "A", "subject_id": sc["subject_id"],
                                  "score": sc["score"], "confidence": 100, "reason": "标题精确命中",
                                  "by": "规则"}
            continue
        v = vlm.get(sid)
        if not v:
            decisions[sid] = {"tier": "C", "reason": "无VLM结果"}
        elif v.get("subject_id") and v.get("confidence", 0) >= m["vlm_accept"]:
            decisions[sid] = {"tier": "A", "subject_id": v["subject_id"], "score": sc["score"],
                              "confidence": v["confidence"], "reason": v.get("reason", ""), "by": "VLM"}
        elif v.get("subject_id") and v.get("confidence", 0) >= floor:
            decisions[sid] = {"tier": "B", "subject_id": v["subject_id"], "score": sc["score"],
                              "confidence": v["confidence"], "reason": v.get("reason", ""), "by": "VLM"}
        else:
            decisions[sid] = {"tier": "C", "reason": v.get("reason") or f"VLM判null或置信<{floor}"}
    return decisions


def write_report(decisions, series_map, titles, subjects, paths, cfg, run_dir):
    pub = cfg.get("report", {}).get("public_base_url", "").rstrip("/")
    lines = [f"# 刮削 Dry-Run 报告  {RUN_TS}", ""]

    def stat(t):
        n = sum(1 for d in decisions.values() if d["tier"] == t)
        return n

    lines.append(f"**A 档(自动写入) {stat('A')} · B 档(人工确认) {stat('B')} · C 档(未匹配) {stat('C')} · 合计 {len(decisions)}**\n")
    for tier, title in (("A", "## A 档 — 将自动写入"), ("B", "## B 档 — 建议人工确认"), ("C", "## C 档 — 未匹配")):
        lines.append(title)
        lines.append("")
        rows = [(sid, d) for sid, d in decisions.items() if d["tier"] == tier]
        rows.sort(key=lambda x: series_map[x[0]]["name"])
        if not rows:
            lines.append("(无)\n")
        for sid, d in rows:
            name = series_map[sid]["name"]
            komga_lnk = f"[系列]({pub}/series/{sid})" if pub else sid
            if d.get("subject_id"):
                sub = subjects.get(d["subject_id"]) or {}
                nm = sub.get("name_cn") or sub.get("name") or d["subject_id"]
                bgm_lnk = f"[{nm}](https://bgm.tv/subject/{d['subject_id']})"
                lines.append(f"- {name} → {bgm_lnk} ({komga_lnk}) 置信{d.get('confidence','?')} 分数{d.get('score','?')} "
                             f"来源{d.get('by','?')} | {d.get('reason','')}")
            else:
                lines.append(f"- {name} ({komga_lnk}) | {d.get('reason','')}")
        lines.append("")
    fp = os.path.join(run_dir, "report.md")
    with open(fp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    save_json(os.path.join(run_dir, "decisions.json"), decisions)
    log(f"报告已生成: {fp}")
    return fp


# ============================================================ 阶段 7: Apply ===

def clean_summary(sub):
    s = sub.get("summary") or ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return s.strip()


def volume_to_series(sub, bg, paths):
    """单卷条目 → 系列条目: 沿 bgm '系列' 关联; 无关联则用去卷号名重搜。返回 (subject, note)"""
    if not sub or sub.get("series"):
        return sub, None
    for rel in bg.related(sub["id"], paths) or []:
        if rel.get("relation") == "系列" and rel.get("type") == 1:
            s = bg.subject(rel["id"], paths["bgm_subjects"])
            if s and (s.get("platform") or "") != "小说" and s.get("series"):
                return s, f"单卷{sub['id']}→系列{s['id']}(关联)"
    base = re.sub(r"\s*[（(]\s*\d+\s*[)）]\s*$", "", sub.get("name") or "")
    if base and base != (sub.get("name") or ""):
        for rid in bg.search(base):
            s = bg.subject(rid, paths["bgm_subjects"])
            if not s or (s.get("platform") or "") == "小说" or not s.get("series"):
                continue
            nm = s.get("name") or ""
            if base in nm or nm in base or base in (s.get("name_cn") or ""):
                return s, f"单卷{sub['id']}→系列{s['id']}(重搜)"
    return sub, None


def novel_to_manga(sub, bg, paths, folder=""):
    """小说条目 → 漫画版: 沿关联找 platform=漫画 (名字须含小说基础名), 优先系列条目/作画者匹配文件夹"""
    base = re.sub(r"[@＠].*$", "", sub.get("name") or "").strip()
    best = None
    for rel in bg.related(sub["id"], paths) or []:
        if rel.get("type") != 1:
            continue
        c = bg.subject(rel["id"], paths["bgm_subjects"])
        if not c or (c.get("platform") or "") != "漫画":
            continue
        if base and base not in (c.get("name") or ""):
            continue
        artists = " ".join(str(v) for v in (infobox_value(c, "作画"), infobox_value(c, "插画"),
                                            infobox_value(c, "作者")) if v)
        if folder and artists and not any(a.strip()[:2] in folder for a in artists.split()):
            continue
        if c.get("series"):
            return c
        best = best or c
    return best


def build_payload(sub, current_meta, cfg, series_name=None):
    ap = cfg["apply"]
    p = {}

    def set_f(field, value):
        if value is None or value == "":
            return
        if ap.get("respect_locked_fields", True) and current_meta.get(field + "Lock"):
            return
        p[field] = value
        if ap.get("lock_written_fields", True):
            p[field + "Lock"] = True

    title = sub.get("name_cn") or sub.get("name")
    set_f("title", title)
    set_f("titleSort", title)
    set_f("summary", clean_summary(sub))
    set_f("publisher", infobox_value(sub, "出版社"))
    author = infobox_value(sub, "作者")
    artist = infobox_value(sub, "插画") or author
    set_f("writer", author)
    set_f("artist", artist)
    set_f("language", "ja")
    tags = [t.get("name") for t in (sub.get("tags") or []) if t.get("name")][:8]
    if tags:
        set_f("tags", tags)
    keep = [l for l in (current_meta.get("links") or [])
            if not re.search(r"(bgm\.tv|bangumi\.tv|chii\.in)/subject/", l.get("url") or "")]
    keep.append({"label": "Bangumi", "url": f"https://bgm.tv/subject/{sub['id']}"})
    set_f("links", keep)
    alts = [{"label": "Original", "title": sub["name"]}] if sub.get("name") else []
    if sub.get("name_cn"):
        alts.append({"label": "Bangumi", "title": sub["name_cn"]})
    if series_name and series_name not in {a["title"] for a in alts}:
        alts.append({"label": "文件名", "title": series_name})  # Komga 可搜别名但不搜文件夹名 (实测 2026-09-29)
    set_f("alternateTitles", alts)  # 清掉老刮削器的错别名 (同 2026-09-29 别名修复规则)
    return p


def dump_metadata_tables(run_dir, cfg=None):
    db = ((cfg or {}).get("komga", {}) or {}).get("database_path") or os.environ.get("KOMGA_DATABASE")
    if not db or not os.path.exists(db):
        log("备份层③: 未配置 komga.database_path (或环境变量 KOMGA_DATABASE), 跳过元数据表转储")
        return
    tables = ["SERIES_METADATA", "SERIES_METADATA_TAG", "SERIES_METADATA_GENRE",
              "SERIES_METADATA_LINK", "SERIES_METADATA_ALTERNATE_TITLE", "SERIES_METADATA_SHARING"]
    out = os.path.join(run_dir, "metadata_tables.sql")
    with open(out, "w", encoding="utf-8") as f:
        for t in tables:
            r = subprocess.run(["sqlite3", "-readonly", db, f".dump {t}"],
                               capture_output=True, text=True, timeout=120)
            f.write(r.stdout or "-- dump failed\n")
    log(f"备份层③: 元数据表已转储 {out}")


def already_scraped(sid):
    """本刮削器写过 = journal/<YYYYMMDD_HHMMSS>/<sid>.json 存在
    (不看 bgm 链接: B 档常带老刮削器留下的链接, 会误判成已刮)"""
    return bool(glob.glob(os.path.join(BASE_DIR, "journal", "[0-9]" * 8 + "_" + "[0-9]" * 6, f"{sid}.json")))


def phase_apply(komga, decisions, series_map, subjects, paths, cfg, args):
    run_dir = os.path.join(paths["journal"], RUN_TS)
    a_list = [(sid, d) for sid, d in decisions.items() if d["tier"] == "A"]
    if not getattr(args, "force", False):
        before = len(a_list)
        a_list = [(sid, d) for sid, d in a_list if not already_scraped(sid)]
        log(f"增量模式: A 档 {before} 个中 {before - len(a_list)} 个已刮过(journal 有记录)跳过, 待写 {len(a_list)} (--force 强制重刮)")
    if not a_list:
        log("无 A 档系列, 无需写入")
        return
    log(f"APPLY: 将写入 {len(a_list)} 个 A 档系列, journal → {run_dir}")
    if not args.no_snapshot:
        r = subprocess.run(["bash", os.path.join(BASE_DIR, "snapshot_db.sh")],
                           capture_output=True, text=True, timeout=600)
        log((r.stdout or "").strip())
        if r.returncode != 0:
            sys.exit(f"ERROR: 全库快照失败, 拒绝写入\n{r.stderr}")
    else:
        log("!! --no-snapshot: 跳过快照 (风险自担)")
    os.makedirs(run_dir, exist_ok=True)
    wps = cfg["apply"].get("writes_per_second", 3)
    rl = RateLimiter(wps)
    ok, skip = 0, 0
    covers_done = 0
    for sid, d in a_list:
        try:
            fresh = komga.series_detail(sid)
            cur = fresh.get("metadata") or {}
            save_json(os.path.join(run_dir, f"{sid}.json"),
                      {"series_id": sid, "name": fresh.get("name"), "metadata": cur})
            payload = build_payload(subjects[d["subject_id"]], cur, cfg, series_map.get(sid, {}).get("name"))
            if not payload:
                skip += 1
                continue
            rl.wait()
            komga.patch_metadata(sid, payload)
            if args.covers:
                sub = subjects[d["subject_id"]]
                cov = Bangumi(cfg).cover(sub, paths["bgm_covers"])
                if cov:
                    komga.upload_cover(sid, cov)
                    for t in komga.list_thumbs(sid):
                        if t.get("type") == "USER_UPLOADED" and not t.get("selected"):
                            komga.delete_thumb(sid, t["id"])
                    covers_done += 1
            ok += 1
        except Exception as e:
            skip += 1
            log(f"  失败 {series_map[sid]['name']}: {str(e)[:80]}")
    log(f"APPLY 完成: 成功 {ok}, 跳过/失败 {skip}, 封面 {covers_done}")
    dump_metadata_tables(run_dir, cfg)
    log("提示: 双击本地 Komga一键同步.bat 立即推平副本, 否则等凌晨5点")


# ============================================================ 主流程 ===

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写 (默认 dry-run)")
    ap.add_argument("--covers", action="store_true", help="追加封面档 (需配合 --apply)")
    ap.add_argument("--limit", type=int, help="只处理前 N 个系列")
    ap.add_argument("--series", type=str, help="指定系列ID, 逗号分隔")
    ap.add_argument("--exclude", type=str, help="强制降为B档的系列ID, 逗号分隔 (审计红旗)")
    ap.add_argument("--no-snapshot", action="store_true", help="跳过快照 (危险)")
    ap.add_argument("--force", action="store_true", help="连已刮过(有 bgm 链接)的系列也重写; 注意已锁定字段仍会跳过")
    args = ap.parse_args()
    args.series = [s.strip() for s in args.series.split(",")] if args.series else None
    args.exclude = [s.strip() for s in args.exclude.split(",")] if args.exclude else None

    cfg = load_config()
    for d in ("cache", "journal", "reports", "logs"):
        os.makedirs(os.path.join(BASE_DIR, d), exist_ok=True)
    paths = {"cache": os.path.join(BASE_DIR, "cache"),
             "pages": os.path.join(BASE_DIR, "cache", "pages"),
             "bgm_subjects": os.path.join(BASE_DIR, "cache", "bgm_subjects"),
             "bgm_covers": os.path.join(BASE_DIR, "cache", "bgm_covers"),
             "journal": os.path.join(BASE_DIR, "journal")}
    for k, v in paths.items():
        os.makedirs(v, exist_ok=True)

    komga, bg, ds = Komga(cfg), Bangumi(cfg), DeepSeek(cfg)
    lib = komga.find_library(cfg["target"]["library_name"])
    log(f"目标库: {lib['name']} ({lib['id']})")

    m = cfg["match"]
    series_map = phase_collect(komga, lib, paths, args)
    titles = phase_clean_titles(ds, series_map, paths, cfg)
    searches = phase_search(bg, series_map, titles, paths)
    searches = phase_webalt(ds, bg, series_map, titles, searches, paths, cfg)
    subjects = phase_subjects(bg, searches, paths)
    scores = phase_score(series_map, titles, searches, subjects, paths)
    vlm = phase_vlm(ds, bg, series_map, titles, searches, subjects, scores, paths, cfg, m)
    vlm = phase_vlm_retry(ds, bg, series_map, titles, searches, subjects, scores, vlm, paths, cfg, m)
    decisions = phase_decisions(series_map, titles, searches, subjects, scores, vlm, m)
    if args.exclude:
        ex = set(args.exclude)
        demoted = 0
        for sid in list(decisions):
            if sid in ex and decisions[sid]["tier"] == "A":
                d = decisions[sid]
                decisions[sid] = {"tier": "B", "subject_id": d.get("subject_id"),
                                  "score": d.get("score"), "confidence": d.get("confidence"),
                                  "reason": "审计红旗人工复核: " + (d.get("reason") or "")[:36], "by": "审计降档"}
                demoted += 1
        log(f"审计排除: {demoted} 个 A 档降为 B 档 (人工复核)")
    if m.get("normalize_subject", True):
        fixed_n = fixed_v = 0
        for sid, d in list(decisions.items()):
            if d.get("tier") != "A" or not d.get("subject_id"):
                continue
            sub = subjects.get(d["subject_id"])
            if not sub:
                continue
            if (sub.get("platform") or "") == "小说":
                cand = novel_to_manga(sub, bg, paths, series_map.get(sid, {}).get("name") or "")
                if cand:
                    subjects[cand["id"]] = cand
                    decisions[sid] = dict(d, subject_id=cand["id"],
                                          reason=f"小说{sub['id']}→漫画{cand['id']} " + (d.get("reason") or "")[:36])
                    fixed_n += 1
                    continue
            s2, note = volume_to_series(sub, bg, paths)
            if note:
                subjects[s2["id"]] = s2
                decisions[sid] = dict(d, subject_id=s2["id"], reason=note + " " + (d.get("reason") or "")[:36])
                fixed_v += 1
        if fixed_n or fixed_v:
            log(f"条目归一化: 小说→漫画 {fixed_n}, 单卷→系列 {fixed_v}")
    run_dir = os.path.join(BASE_DIR, "reports", f"run_{RUN_TS}")
    os.makedirs(run_dir, exist_ok=True)
    write_report(decisions, series_map, titles, subjects, paths, cfg, run_dir)
    save_json(os.path.join(paths["cache"], "decisions.json"), decisions)

    a = sum(1 for d in decisions.values() if d["tier"] == "A")
    b = sum(1 for d in decisions.values() if d["tier"] == "B")
    c = sum(1 for d in decisions.values() if d["tier"] == "C")
    log(f"决策汇总: A={a} B={b} C={c}")
    if args.apply:
        phase_apply(komga, decisions, series_map, subjects, paths, cfg, args)
    else:
        if not args.force:
            todo = [sid for sid, d in decisions.items() if d["tier"] == "A" and not already_scraped(sid)]
            log(f"增量预览: --apply 将只写 {len(todo)} 个未刮过的 A 档系列: {todo[:20]}")
        log("dry-run 结束 (未写入任何数据). 审查报告后用 --apply 写入")


if __name__ == "__main__":
    main()
