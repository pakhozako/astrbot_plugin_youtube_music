# 开发

[文档目录](index.md) / 开发

## 准备环境

使用 Python 3.12+、Node.js 24、npm 和 Git。

```bash
git clone https://github.com/pakhozako/astrbot_plugin_youtube_music.git
cd astrbot_plugin_youtube_music
python -m venv .venv
```

激活虚拟环境：

Linux / macOS：

```bash
source .venv/bin/activate
```

Windows PowerShell：

```powershell
.venv\Scripts\Activate.ps1
```

安装开发依赖：

```bash
python -m pip install -r requirements-dev.txt
npm ci
npx playwright install chromium
```

[`requirements-dev.txt`](../requirements-dev.txt) 固定测试宿主 AstrBot 4.29.4；[`package.json`](../package.json) 和 [`package-lock.json`](../package-lock.json) 维护前端依赖、脚本与完整性摘要。

## 项目结构

| 路径 | 职责 |
| --- | --- |
| `main.py` | 插件生命周期、QQ 指令与队列工作器 |
| `core/service.py`、`core/process.py` | 媒体搜索、下载、转换与子进程 |
| `core/queue.py`、`core/state.py`、`core/persistence.py` | 并发准入、会话与持久化 |
| `core/library.py`、`core/audio_cache.py` | 收藏、历史与音频缓存 |
| `core/search_quality.py`、`core/search_recall.py` | 纯搜索排序与版本去重、受控搜索消息清理 |
| `core/dependency_manager.py`、`core/maintenance.py` | 运行程序更新与维护 |
| `core/ai_tools.py`、`core/webui.py` | AI Tool 与控制台 API |
| `frontend/` | Vanilla JavaScript 源码与按需图标 |
| `views/console/`、`.astrbot-plugin/i18n/` | 生产视图、组件许可与双语资源 |
| `tests/` | Python、Node、浏览器与 mock bridge |
| `scripts/` | ZIP 构建与依赖检查 |
| `docs/` | 用户指南、参考与文档截图 |

## 运行时约束

聊天、AI Tool 和控制台复用同一队列及媒体服务。修改时保留以下约束：

- 每个会话只有一个 FIFO 工作器；队列修改由 `_queue_lock` 串行处理，媒体准入遵循全局并发及等待容量。
- 单首入队复用 `enqueue_many`，默认保留重复歌曲错误；显式跳过只用于音乐库批量操作。每批最多 20 首，在同一个 `JsonStore.update` 内重新判断重复和容量，整个批次一起提交。
- 已开始的批量文件事务会在取消返回前完整结束，不产生半批；取消或响应丢失不证明未提交，调用方须刷新核对，不自动重试。
- 当前未提交项仍属于持久队列。确认消息提交后才移除精确 entry ID，只有语音提交写入历史；超时结果未知时不自动再发一条。
- 队列移除与历史写入在同一次 `run_blocking` 中完成，不在两者之间响应取消。提交后的独立收尾任务等待已开始的写入结束，取消不掩盖真实写入失败；失败仍记录已提交警告与精确条目。两份 JSON 仍是各自的原子文件，并非跨文件崩溃事务；保留状态保存失败的暂停与人工核对语义。
- 当前歌曲直到语音等待结束仍参与当前群的重复判断，提交收尾窗口的队列反馈只计当前项一次。等待使用已验证 WAV 的时长，而不是搜索元数据。
- 取消自己的待播项不会跳过已经提交语音的播放等待。管理端的下一首、暂停和清空具有不同的队列语义。
- 依赖使用与维护互斥；取消须等待已开始的文件操作和进程树终止完成后再清理或回滚。初始化幂等且串行，卸载取消并等待所有所属任务，旧实例不能重新初始化。
- JSON 仅在事务写入成功后发布新的内存快照。解析拒绝重复键、非有限数字和超过 64 层的容器嵌套（根对象或数组计为第 1 层），路径漂移或不合法更新不覆盖原数据。
- 音乐库只复制当前用户的收藏或历史子树，不为读取一个用户复制整个库。队列和整库事务仍保留完整快照及总容量校验。
- 历史收藏和批量来源解析使用解析时的身份快照；并发移除不会撤销已经解析的歌曲。数据格式继续使用 schema 1，不增加配置，原有聊天指令与 AI Tool 参数兼容。
- 缓存只操作固定根目录内、身份相符的文件对。缓存读取在 `RLock` 内复制到任务私有 WAV，后续失效或缓存清理不能删除已交给发送链路的音频。新下载同样返回任务 WAV，而不是共享缓存路径。
- 临时任务根目录固定初始化时的路径，拒绝路径漂移、链接和 junction；清理前核对有界的插件归属标记。清理失败仅记日志，后续启动时可清理过期归属目录，不因已提交任务的清理失败暂停队列。
- 群聊分页使用 `service.search(candidate_pool=True)` 获取原有三倍候选；默认调用与 AI Tool、WebUI 仍返回原有候选数。`Song` 保持六字段，版本标签由纯函数推导。
- 搜索列表按用户与会话隔离，搜索代际结果码与每页发送 token 分开；全局编号在翻页时稳定。启用自动撤回的受控发送路径在发送前及获取资源后复核 token 和有效期，发送失败会使该候选失效。关闭撤回时回复交给宿主，不能确认宿主的发送结果；裸编号在两种模式下都只表示当前个人列表。
- 不自动向群发送处理阶段消息，状态查询和现有 WebUI 是进度入口；控制台完整播放阶段复用现有双语资源并随语言切换更新。详情复用 aiohttp 有界抓取，禁用跳转和解压，限时 4 秒、1 MiB 并核对图片 MIME/签名；取消和失败均不启动第二条详情通知。
- 网络与媒体工具的其他输入限制见[安全指南](https://github.com/pakhozako/astrbot_plugin_youtube_music/blob/main/SECURITY.md)。
- SSE 通知在进行中的状态读取之后合并一次新读取。页面离开后的旧请求不能应用到恢复的界面，后退缓存恢复时只重建一个实时订阅。

## 本机预览

```bash
npm run preview
```

打开 `http://127.0.0.1:8814/preview/`。预览使用独立 mock bridge、示例会话和项目生成的图片。

在 AstrBot 中调试时，从插件详情进入 `console`，使用宿主提供的 `window.AstrBotPluginView` bridge。接口依据见 [AstrBot 官方文档](https://github.com/AstrBotDevs/AstrBot/tree/2d3384f1d6cdb272c686065cb6c29fe122682f59/docs)。

## 构建前端

```bash
npm run build
```

esbuild 从 `frontend/console.js` 按需打包到 `views/console/app.js` 和 `app.css`，生成关联许可声明。前端变更与对应生产资源一同提交。

## 回归测试

```bash
ruff check .
ruff format --check .
python -B -m unittest discover -s tests -v
python -B scripts/check_history.py
npm test
npm run test:browser
npm audit --audit-level=low
```

Python 覆盖配置、官方接口、媒体、队列、存储、依赖、文档及批量原子性，另覆盖群聊隔离、搜索代际/分页、版本去重、
私有音频缓存竞争、取消后收尾和路径漂移。Node 覆盖连接生命周期、分页、排序和选择模型；
浏览器覆盖主题、双语、队列、音乐库批量流程、加载、错误、焦点和响应式交互。

在 PowerShell 中将测试运行数据及下载缓存限制在仓库内：

```powershell
New-Item -ItemType Directory -Force .verification/tmp | Out-Null
$env:TEMP = Join-Path $PWD ".verification/tmp"
$env:TMP = $env:TEMP
$env:ASTRBOT_ROOT = Join-Path $PWD ".verification/astrbot"
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD ".verification/playwright"
$env:npm_config_cache = Join-Path $PWD ".verification/npm-cache"
$env:PIP_CACHE_DIR = Join-Path $PWD ".verification/pip-cache"
```

环境变量应在安装依赖、浏览器和启动测试之前设置。`.verification/` 与 `.venv/` 不进入提交或安装包。

最低 Pydantic 矩阵在独立虚拟环境中执行：

```bash
python -m pip install -r requirements-dev.txt "pydantic==2.12.5"
python -B -m unittest discover -s tests -v
```

公开 CI 在 Ubuntu、Windows 上执行两组 Pydantic 矩阵，并执行生产构建与 Chromium 回归。

## 文档截图

浏览器回归覆盖 1440 x 900、1920 x 1080、834 x 1112、390 x 844、320 x 740。结构化结果和全部截图写入 `.verification/m3/`。

README 仅收录 `docs/images/` 中的四张界面截图。界面变更后运行浏览器测试，核对截图再更新文档素材；示例图片由 `tests/fixtures/music-thumbnail.mjs` 生成。

文档链接、配置值、AI Tool 示例、截图尺寸和 ZIP 中的文档资源由自动化测试检查。

## 构建安装包

完成测试与生产构建后执行：

```bash
python -B scripts/build_release.py
python -B scripts/build_release.py --check-built
```

`--check-built` 从 `metadata.yaml` 推导当前包名，不在 CI 或命令中固定版本。
`--check <ZIP 路径>` 使用当前构建器的运行文件白名单；检查历史包时使用对应标签源码中的构建器，避免新增模块导致历史白名单不匹配。

构建器拒绝覆盖同名输出。只有同一提交的可重现校验可以归档旧文件后重建；源码变化必须先提升版本，不能复用已发布版本。

构建器采用显式白名单和固定 ZIP 元数据，加入包含路径、大小、SHA-256 的 `RELEASE_MANIFEST.json`。写包前检查源码语法、严格 JSON、容量、版本声明和正式更新日志；校验覆盖 CRC、常规文件条目、manifest 字段与类型、插件身份和摘要。

包校验证明内容与 manifest 的完整性，不证明下载来源、真实 QQ 发送或生产环境验收。离线回归使用模拟下载与消息组件，不连接真实会话。

运行包包含用户文档、精选截图和完整许可。源码仓库额外保留开发资料；测试、构建工具、运行数据、日志和完整测试截图保留在各自的开发或本地目录。

`.gitattributes` 管理源码归档的导出范围，`scripts/build_release.py` 管理带 manifest 的安装包。

## 版本发布

当前仓库以完整源码建立全新线性历史，初始版本为 `0.1.0`。`main` 不接受 merge commit，
提交使用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/v1.0.0/)：

```text
feat(library): add batch queue selection
fix(queue): preserve failed entry identity
docs: clarify queue recovery
feat!: change the public queue schema
```

允许的类型为 `feat`、`fix`、`docs`、`style`、`refactor`、`perf`、`test`、`build`、`ci`、`chore`、`revert`。
类型后可以带 scope；标题使用 `type(scope): description` 或 `type: description`。
不兼容变更使用 `!` 或正文中的 `BREAKING CHANGE:` / `BREAKING-CHANGE:`，并描述迁移方式。
合入 `main` 前 rebase，保留线性且符合规范的提交；不要引入自动生成的 merge 标题。

每轮迭代先审查当前 `metadata.yaml`、CHANGELOG、发布脚本和本地及远端 Git 标签，再按实际变化选择升级级别。
`scripts/check_history.py --tag <标签>` 根据前一个可达版本标签之后的全部提交验证下一版本：

- PATCH：兼容的问题修复、安全硬化、性能与文档修正，只增加 PATCH。
- MINOR：新增向后兼容的公开功能，MINOR 增加一并将 PATCH 归零。
- MAJOR：不兼容的公开接口、行为或数据格式变更，MAJOR 增加一并将 MINOR、PATCH 归零。

区间内任何 breaking marker 需要 MAJOR；否则有 `feat` 需要 MINOR；其余需要 PATCH。
首个标签必须为 annotated `v0.1.0`，后续标签必须按实际变更连续升级，不允许没有新提交的空发布。
提交标题只是自动门禁的一部分，仍须审查实际功能、兼容性和更新记录，不能用虚假的类型或条目制造版本。

发布前同步 `metadata.yaml`、`package.json`、`package-lock.json` 的根及根包版本、依赖管理器 User-Agent、README 版本标识和 CHANGELOG。将已完成条目从 Unreleased 移入带真实日期的新版本，保留历史记录；版本不重复、不倒退、不跳号。

1. 执行 `git fetch origin main --tags`，确认分支和标签未漂移。已存在的版本标签不能删除、强推或移动。
2. 完成两组 Pydantic 回归、Node 和浏览器测试、依赖检查及适用的兼容验证，准确记录未执行的线上验证。
3. 构建安装包并执行 `--check-built`。检查完整 diff，排除凭据、运行数据和本地工件，以 Conventional Commit 提交并正常推送 `main`；等待该提交的 Ubuntu、Windows 和前端 CI 全部成功，失败时先修复，不创建正式版本标签。
4. 从该提交创建对应的 annotated tag，执行历史门禁和 `--check-built --ref <版本标签>`，逐文件核对 ZIP 与标签提交的 Git blob，并验证版本声明一致。
5. 校验成功后正常推送该标签，等待标签 CI 成功。`Verified Tag Release` 只在四组 Python 矩阵和前端回归全部通过后运行，并且仅该 job 有 `contents: write` 权限。
6. 自动发布重建同一标签的可重现 ZIP，要求 `origin/main` 和远端 annotated tag 与源码匹配、工作树干净、同名 Release 不存在。先创建空草稿，直接绑定创建响应中的 Release ID，再按该 ID 上传 ZIP 并绑定返回的 Asset ID；不依赖创建后的草稿列表或按标签重新查找。
7. 核对草稿唯一附件的身份、大小和 API 摘要，按固定 Asset ID 流式下载，限制字节数和超时，再逐文件验证标签来源。再次核对远端、草稿及附件 ID 后，按同一 Release ID 公开，并读取该 ID 确认发布结果。
8. 核对远端提交、标签对象、Release 状态和下载附件摘要。自动化不覆盖附件或旧版本；任何失败都不能标记成功。

PowerShell 中可从已验证的安装包读取版本，避免再次手填版本号：

```powershell
$release = python -B scripts/build_release.py --check-built | ConvertFrom-Json
$tag = "v$($release.version)"
git tag -a $tag -m "Release $($release.version)"
python -B scripts/check_history.py --tag $tag
python -B scripts/build_release.py --check-built --ref $tag
git push --atomic origin main "refs/tags/$tag"
```

这些命令应在检查远端、完成测试及发布提交的 CI 之后逐步执行。`--atomic` 要求远端同时接受分支和标签，否则全部不更新；不要使用强制推送或批量推送无关标签。

普通 `--check` 只证明包自身完整性。只有成功的 `--ref` 检查才证明安装包字节与指定标签提交对应；这仍不等同于真实 YouTube 下载、QQ 投递或生产验收。

`scripts/publish_release.py --tag <标签> --dry-run` 仅执行本地和远端只读发布检查。
正常发布交给标签 CI，不在测试未通过时手动调用写入模式。上传、网络或校验失败可能留下草稿；
先审查草稿和附件，不要用 `--clobber`、移动标签或重复上传掩盖问题。流程不会擅自清理既有 Release。
已公开版本保持不可覆盖；仓库历史重建必须获得明确授权，不属于常规发布操作。

## 控制台接口

`POST queue/batch` 仅接受 `session`、`source`、`video_ids`，其中 `source` 为 `favorites` 或 `history`。
`video_ids` 是 1 至 20 个唯一视频 ID；歌曲信息从当前官方用户名的来源列表解析，客户端不能提交歌曲对象、媒体地址或替代 owner。
结果包含 `added`、按实际队列位置排列的 `positions`、按请求顺序排列的 `skipped` 和 `queue_size`。
`POST queue/add` 的参数和 `{position}` 结果保持不变。

`POST library/action` 增加 `action=favorite`、`source=history`、`video_id` 的历史收藏方式，不接受搜索 `index`。
原搜索收藏可以省略 `source` 或指定 `search`，仍须匹配候选编号和视频 ID。其他操作不接受 `source`。
`GET status` 增加 `limits.batch` 和每个队列的 `summary`，包含 `count`、`duration_seconds`、`unknown_duration_count`。
这些总计只包含持久待播项，不能作为实际播放进度或 ETA。

## 独立依赖检查

准备好 GnuPG 后执行：

```bash
python -B scripts/check_dependencies.py --live
```

脚本在临时目录下载、验证并运行组件，生成本地测试音，输出版本与媒体验证结果。

## 提交前

1. 核对 README、配置、指令和实际代码。
2. 同步版本文件、更新记录和生产资源。
3. 检查测试、依赖审计、文档链接与安装包。
4. 审查 diff 和文件清单，确认凭据和运行工件已排除。
5. 核对项目 LICENSE、第三方声明和新素材来源。

提交流程见[贡献指南](../CONTRIBUTING.md)，权限和漏洞报告见[安全指南](../SECURITY.md)。
