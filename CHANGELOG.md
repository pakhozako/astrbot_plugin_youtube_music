# Changelog

本项目使用 [Semantic Versioning](https://semver.org/)，更新记录采用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式。
当前完整源码作为全新线性历史的唯一基准，从 `0.1.0` 开始发布。

## [Unreleased]

## [0.1.0] - 2026-11-18

### Added

- QQ 会话中的 YouTube 搜索、候选确认、视频链接入队和 WAV 语音点播。
- 共享 FIFO 队列、管理权限、暂停与恢复、下一首、取消、移除、重排和待播总时长。
- 当前身份的收藏与语音提交历史、历史次数与时间、从历史直接收藏。
- Material Design 3 控制台：搜索、队列、音乐库和维护，中英文、亮暗主题及桌面、平板、手机布局。
- 音乐库每页 20 首，跨页、筛选和排序后保留选择，收藏与历史分别维护选择状态。
- 最多 20 首整批入队；确认目标、顺序、时长和容量，已排队曲目跳过，失败保留选择。
- 音乐 AI Tool，复用聊天会话、授权和资源限制；搜索结果消息的受控自动撤回。
- yt-dlp、Deno、FFmpeg/ffprobe 检查和受控更新，进度、取消、恢复及音频缓存管理。
- 常规文件与目录边界校验、严格有界 JSON、事务原子写入、媒体协议限制、进程树取消和资源准入。
- 失败队列关联精确条目，无关重排或移除不会清错；错误及暂停队列继续前确认潜在重发风险。
- Python 双 Pydantic 矩阵、Node 模型、五视口浏览器、兼容性和可重现安装包回归。
- Conventional Commits 和线性历史检查，按实际提交变化验证连续 SemVer；标签通过全套 CI 后自动校验并发布安装包。
- 同步版本声明、来源证明、安装包 manifest、完整用户及开发文档、精选截图与第三方许可。
- MIT License，版权署名 `Copyright (c) 2026 pakhozako`。

### Release Policy

- 首次版本从当前完整源码生成，与新的初始提交和 annotated `v0.1.0` 标签一致。
- 后续标签不可覆盖，版本必须连续；发布包必须逐字节匹配标签源码。
- 自动发布直接绑定创建响应的 Release ID 和上传响应的 Asset ID，不依赖草稿列表的即时可见性。
- 附件采用有界二进制下载，校验大小、SHA-256 和标签源码后才公开；不覆盖现有版本或附件。
- 离线测试与源码校验不证明真实 YouTube 下载、QQ 投递或生产环境验收。

[Unreleased]: https://github.com/pakhozako/astrbot_plugin_youtube_music/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/pakhozako/astrbot_plugin_youtube_music/tree/v0.1.0
