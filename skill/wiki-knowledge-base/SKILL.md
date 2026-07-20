---
name: wiki-knowledge-base
description: >-
  接入 llm-wiki 风格的个人知识库仓库（git repo 或本地目录），按
  index → category/tag → page → source 的层级检索沉淀知识，并能从页面回溯到
  归档原文。当用户给出一个 wiki 知识库的 git 地址或本地路径、说"查一下我的
  wiki / 知识库"、要求基于个人知识库回答问题、或提到 llm-wiki / wikilink /
  知识沉淀库时，使用本 skill。即使用户只是丢来一个包含 index.md + pages/ +
  sources/ 结构的仓库链接而没有明说用途，也应先用本 skill 建立检索路径，
  而不是盲目全文搜索。
---

# wiki-knowledge-base：接入 llm-wiki 知识库

这类知识库是一个 git 仓库（设计源自 Karpathy 的 llm-wiki）：机器归档原始文章
（sources），LLM 把值得长期沉淀的知识编译进人工可读的页面（pages），索引由代码
维护。你的任务是把它当作可信的外部记忆来检索，而不是当作一堆待 grep 的文本。

## 第一步：拿到并更新仓库

- 给的是 git 地址：clone 到本地工作目录（浅克隆即可，`git clone --depth 1`）。
  私有仓库 clone 失败时，把报错如实告诉用户，请他们提供有权限的地址或本地路径。
- 给的是本地路径：直接使用，但**每次执行本 skill 前先 `git pull --ff-only`**，
  知识库每天都在增量更新，不 pull 就可能基于过期知识回答。pull 失败（离线、
  冲突）不阻塞检索，继续用本地版本并向用户说明数据可能滞后。
- 仓库对你只读。不要修改、提交任何内容——页面由知识库自己的编译管线维护。

## 仓库结构

```
index.md              全库目录：按 category 分节，每行一个页面
pages/*.md            知识页面（简体中文），互相用 [[wikilink]] 引用
sources/YYYY/MM/*.md  归档原文（不可变），含摘要与正文
log.md                追加式编译日志（一般不用读）
```

- `index.md` 每节形如 `## agent-engineering`，节内每行：
  `- [[page-stem]] `tag1` `tag2` — 一句话简介`。
- 页面 frontmatter：`description`（一行简介）、`category`（单值）、
  `tags`（2-4 个，全库共享一套小词汇表）、`updated`。
- source frontmatter：`title` / `source` / `url`（原网页地址）/ `date` /
  `compiled`，以及 `pages:`（该源沉淀进了哪些页面，反向链接）。

## 检索路径：index → category/tag → page → source

按层级下钻，每层只读需要的部分，控制进入上下文的量：

1. **读 index.md**。它不大，整读。根据问题选出 1-2 个相关 category 节，
   在节内按 tag 和简介圈定候选页面。tag 词汇表就是从这里学的——不要凭空
   猜 tag 名。
2. **读候选页面** `pages/<stem>.md`。页面是浓缩后的知识本体，多数问题读到
   这层就够了。正文里的 `[[some-page]]` 指向 `pages/some-page.md`，需要
   展开时跟着链走；标着 `（未建）` 或指向不存在文件的 wikilink 是有意留下
   的占位（值得建但还没建的页面），不是坏数据，不要报告为错误。
3. **需要原始证据再下钻 source**。页面末尾的 `## 来源` 段列出了这个页面由
   哪些归档源编译而来，链接形如 `../sources/2026/07/xxx.md`。当需要原文
   细节、准确引文、数据出处、或怀疑页面转述有偏差时，打开对应 source 读
   `## 原文`；source frontmatter 的 `url` 可给用户作为最终出处。反过来，
   从一篇 source 的 `pages:` 字段能找到它沉淀进的所有页面。注意：来源段与
   `pages:` 反向链接是较新的编译产物，早期页面/源可能还没有——缺失时用
   `grep -rl <页面名或关键词> sources/` 反查。

层级检索找不到时（新概念可能还没沉淀成页面），再退回全文搜索：
`grep -ril <关键词> pages/`，仍无果就 grep `sources/`——那里有未编译或
未被采纳的原文。两边都没有就直说知识库里没有，不要编。

## 回答时

- 答案以 pages 内容为准（那是人工筛选过的沉淀），sources 用来补证据。
- 引用出处给到能追溯的位置：页面文件名，必要时附 source 的 `url`。
- 页面 frontmatter 的 `updated` 是最近编译日期，回答时效敏感的问题时
  一并说明。
