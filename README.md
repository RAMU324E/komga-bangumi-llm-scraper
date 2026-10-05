# Komga × Bangumi 自动刮削器（komga-bangumi-llm-scraper）

> 只认「文件夹原名 + 第一话原始封面」：Bangumi 检索 + 多模态大模型
> （**标题 + 封面 + 简介**互相印证）判定，置信度达标才写入，**全程可逆**。
> 前身是很久之前朋友写的油猴脚本 [dyphire/KomgaBangumi](https://github.com/dyphire/KomgaBangumi)。
> 模型实测用 DeepSeek：API 可直接联网搜索（细节见「实战经验」），无需另接搜索 MCP。

## 核心特性

- **判档流水线**：文件夹名清洗 → Bangumi v0 检索（中/日文名、联网补全原名）→ 规则打分
  → 多模态 VLM 判定 → A 档直写 / B 档人工审 / C 档保持文件名
- **漫画专用纠偏**（自动，无需人工）：
  - 候选剔除**小说条目**；候选全是小说时沿 Bangumi 条目关联自动补**漫画版**
  - 选中**单卷条目**时自动换**系列条目**（沿「系列」关联，找不到则去卷号重搜）
- **AI 拒绝重试**：置信不足的再问 3 次，≥2 次认定同一条目才采纳，排除偶发误判
- **多版本友好**：同一条目被多个文件夹引用时显示标题带（xx版）后缀，互不覆盖；
  `dup_editions.py` 可对存量库体检并自动补后缀
- **搜索增强**：文件夹名写入系列别名（label=文件名），Komga 搜索可直接命中文件名
- **增量安全**：journal 判定已刮系列自动跳过；`skip_series.json` 永久跳过名单；
  尊重用户手动锁定的字段
- **三层备份**：VACUUM INTO 全库快照 / 逐系列 journal（含 LOCK 标志）/ 元数据表转储，
  单系列可 API 级零停机回滚
- **全链路缓存**：titles/searches/web_alts/bgm_subjects/bgm_relations/scores/vlm
  全部落盘，断点续跑秒级出结果

## 安装

```bash
# Python 3.9+；第三方依赖只有 requests 和 Pillow
pip install -r requirements.txt

cp config.example.json config.json
# 编辑 config.json：
#   komga.base_url / api_key  → 你的 Komga 地址 + 管理员 API key
#   deepseek.*                → DeepSeek key（模型需支持图片输入）
#   bangumi.user_agent        → 建议填项目名+联系方式（bgm 官方建议）
#   target.library_name       → 只处理这个库（白名单，其他库绝不碰）
chmod 600 config.json
```

前置：Komga 里已建好目标库、文件夹已扫描入库；想启用元数据表转储（备份层③）
就把 `komga.database_path` 指向 Komga 的 database.sqlite（不配也能跑，只是少一层备份）。

## 快速上手（3 条命令）

```bash
python3 scraper.py                   # 1. dry-run：只读，生成报告
# 2. 打开 reports/run_*/report.md 人工审 B 档
python3 scraper.py --apply --covers  # 3. 真写（强制先全库快照）
```

## 使用说明

### 1. 第一次跑全库

`python3 scraper.py` 是 dry-run：只读 Komga、Bangumi、DeepSeek，不改任何数据。
结束时打印 `决策汇总: A=xx B=xx C=xx`，报告在 `reports/run_<时间戳>/report.md`。

| 档位 | 含义 | apply 时 |
|---|---|---|
| A | 标题精确命中，或 VLM 判定置信 ≥85 | 自动写入 |
| B | 有候选但置信 60~84，或规则与 VLM 意见分歧 | 不写，等人工表态 |
| C | 无候选或置信 <60 | 不写，保持文件名 |

### 2. 报告怎么读

每行长这样（B 档示例）：

```
- `0JX1234ABCD` [戀愛寄生蟲][內尾梨花][3完] → [恋爱寄生虫](https://bgm.tv/subject/395077) ([系列](https://komga.example.com/series/0JX1234ABCD)) 置信72 分数58 来源VLM | 译名差异大但封面简介一致 ✅已刮过(apply 跳过, 无需再审)
```

- 行首反引号里是**系列 ID**，人工干预参数直接抄它；
- `→ [候选名](bgm链接)` 是机器选中的 Bangumi 条目，点进去可核对；
- 置信 = VLM 打分，分数 = 规则打分，来源 = 谁做的决定（规则 / VLM / 规则+VLM）；
- 结尾带 ✅ 表示 journal 里已有记录（写过或人工处理过），apply 自动跳过，不必再审。

### 3. 人工干预（机器拿不准时你说了算）

```bash
# 不想让某个 A 档写入（审计红旗、你看着不对）
python3 scraper.py --apply --exclude 0JXX,0JYY

# 审完报告，认可某个 B 档/有候选 C 档 → 升为 A 档一起写
python3 scraper.py --apply --accept 0JXX,0JYY --covers

# 某系列没匹配上（C 档无候选），你上 bgm.tv 查到正确条目后指定
python3 scraper.py --apply --assign 0JXX:110731 --covers   # 可逗号分隔多组
```

- `--accept` 采纳的是“本轮报告里的那个候选”，写入后 journal 记 `by=人工采纳`；
- `--assign` 的条目 ID 先经 API 验证存在才生效，打错号会拦下并跳过；
- 人工指定优先级最高，不会被小说→漫画、单卷→系列等自动纠偏改写；
- 写过之后 journal 判已刮 + 字段加锁，以后 apply 自动跳过，人工结果就是终局。

### 4. 到底会写什么

标题/排序标题、简介、出版社、作者/作画、语言、tags、Bangumi 链接、
别名（原名 / Bangumi 中文名 / 文件夹名）。写完的字段自动加锁
（`lock_written_fields`）；你在 Komga 里手动锁过的字段绝不覆盖
（`respect_locked_fields`）。`--covers` 追加封面：Bangumi 大图经 Pillow
压缩到 ≤1MB（Komga 限制），并清掉旧的 USER_UPLOADED 缩略图。

### 5. 日常增量（新漫画入库）

Komga 扫描出新文件夹后：`python3 scraper.py` —— 已刮过的系列有缓存和 journal，
秒级跳过；日志末尾「增量预览」列出本轮将写的系列，确认后 `--apply --covers`。

### 6. 写错了怎么回滚

```bash
python3 restore.py --run-id 20261005_160913                     # 整轮回滚（API 级，不停机）
python3 restore.py --run-id 20261005_160913 --series 0JXX,0JYY  # 只回滚指定系列
python3 restore.py --run-id 20261005_160913 --covers            # 连封面一起还原
```

三层备份：apply 前强制全库快照（snapshot_db.sh，可整库回退）；每系列 journal
记录写入前完整元数据；配了 database_path 还会转储元数据表。

### 7. 参数速查

| 参数 | 作用 |
|---|---|
| `--series ID1,ID2` | 只处理指定系列 |
| `--exclude ID1,ID2` | 强制降 B 档不写 |
| `--accept ID1,ID2` | 人工采纳 B 档/有候选 C 档 |
| `--assign 系列:条目` | 人工指定 bgm 条目（多组逗号分隔） |
| `--covers` | apply 时连封面一起写 |
| `--limit N` | 只跑前 N 个系列（试水用） |
| `--force` | 已刮过的也重写（锁定字段仍尊重） |
| `--no-snapshot` | 跳过快照（危险，别用） |

不想再被某个系列打扰：把文件夹名加进 `skip_series.json`，永久跳过。

## 配置（config.json）

| 键 | 说明 |
|---|---|
| `komga.base_url` / `api_key` | Komga 地址与管理员 API key（只写这一个库） |
| `komga.database_path` | 可选；Komga 的 database.sqlite 路径，用于 apply 后的元数据表转储（备份层③） |
| `deepseek.*` | 多模态模型（标题清洗、VLM 判定、联网原名补全） |
| `bangumi.*` | base_url / user_agent（建议带联系方式）/ 限速 |
| `target.library_name` | **库白名单**：只处理这个 library，其他库物理隔离 |
| `match.*` | 阈值：exact_score=95 / vlm_accept=85 / vlm_review_floor=60 / vlm_retry=3 等 |
| `report.public_base_url` | 报告里系列链接的站点前缀（可留空） |
| `apply.*` | write_covers / lock_written_fields / respect_locked_fields / writes_per_second |

## 工具箱

| 脚本 | 用途 |
|---|---|
| `scraper.py` | 主程序（采集/清洗/搜索/打分/VLM/归一化/报告/写入） |
| `restore.py` | journal 还原：`--run-id X [--series id1,id2] [--covers]`，API 级零停机 |
| `restore_c.py` | C 档文件名恢复：清掉旧刮削器/错误来源的元数据（`--clear-extras`） |
| `fix_fields.py` | 全字段统一（完结状态/genres/分级/出版日期/分册标题与封面） |
| `fix_covers.py` | 补传超限封面（Pillow 压缩到 ≤1MB） |
| `add_filename_aliases.py` | 全库文件夹名写入别名，让搜索命中文件名 |
| `dup_editions.py` | 同名显示标题体检 + 自动加（xx版）后缀；防撞车（撞车自动升级"基础+卷数"），显示已互异的组不动，选不出后缀宁可跳过；默认 dry-run |
| `audit_subject_type.py` | 核查已刮条目类型：写错成小说/可换单卷→系列（只读报告） |
| `snapshot_db.sh` | 全库快照（环境变量 `KOMGA_DATABASE`/`KOMGA_SNAPSHOT_DIR`） |

## 红线（写死在代码里）

1. 库白名单：只处理 `target.library_name` 指定的 library
2. 只写 `komga.base_url` 指定的那一个 Komga，不碰其他实例
3. 无快照不写入、无 journal 不写入、置信度不足不写入
4. Bangumi 限速 + 规范 UA；模型调用失败重试后降级处理，不当作否决
5. 尊重用户手动锁定的字段（`respect_locked_fields` 默认 true）

## 实战经验

- **Komga 缩略图上传限制 ≈1MB**：大图会 413，内置 Pillow 压缩（`compress_jpeg`）
- **DeepSeek 联网搜索**：Responses API 的 `web_search` 参数被官方忽略；真实入口是
  Anthropic 兼容端点 `/anthropic/v1/messages` + `web_search` 工具（phase_webalt 已集成）
- **Bangumi 限速**：5 rps 实测无压力；条目关联接口（`/v0/subjects/{id}/subjects`）
  是单卷→系列、小说→漫画的数据基础，带缓存
- **幂等**：写入按 journal 判定增量；SSH 断连导致远端脚本可能已执行完，重跑会自动跳过已处理项
- **别名搜索**：Komga 搜索匹配标题+别名，但不匹配文件夹名——所以要靠别名补

## 目录布局

```
komga-bangumi-llm-scraper/
  scraper.py  restore.py  restore_c.py  fix_fields.py  fix_covers.py
  add_filename_aliases.py  dup_editions.py  audit_subject_type.py
  snapshot_db.sh  requirements.txt  LICENSE
  config.example.json      # 复制为 config.json 填入真实 key (chmod 600, 勿提交)
  .gitignore
  cache/     # 全链路缓存 (titles/searches/bgm_subjects/bgm_relations/scores/vlm/...)
  journal/   # 每次写入的逐系列完整快照 (回滚依据)
  reports/   # 每次 run 的 decisions.json + report.md
  logs/
```

## 判定流水线

```
系列 → ① 原名采集: 文件夹名 + 第一话第1页原图(从文件直读, 不受旧元数据污染)
     → ② 标题清洗: LLM 解析汉化组前缀/卷号/副标题 → 干净标题+变体
     → ③ Bangumi 搜索: v0 API (type=1 书籍, 剔除小说), 中/日文名都搜, 全程缓存
     → ④ 规则打分: 标题精确/模糊 + 年份/作者加分
          ├─ 精确命中(≥95) ───────────────→ A 档
          ├─ 40~94 ──→ ⑤ VLM 判定 ──→ ≥85 A / 60~84 B(人工审) / <60 C
          └─ <40 或无候选 ─────────────────→ C 档(保持文件名)
     → ⑥ 归一化: 小说→漫画版, 单卷→系列
     → ⑦ dry-run 报告 → 人工审 → --apply 真写 (快照 + journal + 核对)
```

VLM 每个系列输入：原始文件夹名、清洗标题、第一话第1页原图；每个候选：原名/中文名/
发售日/标签/封面大图/简介。要求综合「标题文字 + 封面视觉 + 简介语义」三重一致，

## 许可

[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.zh-hans)：
非商业使用；衍生作品须以相同协议开源。详见 [LICENSE](LICENSE)。
输出 JSON `{subject_id, confidence, reason}`；解析失败一律按 C 档（宁可漏，不可错）。
