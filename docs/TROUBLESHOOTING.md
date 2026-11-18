# 故障排查

[文档目录](index.md) / 故障排查

## 先检查什么

1. **插件状态**：在 AstrBot 已安装列表查看加载状态和插件日志。
2. **依赖状态**：打开控制台维护页，执行检查，确认四个运行程序及版本。
3. **会话状态**：核对目标会话、队列状态和当前处理阶段。
4. **配置与网络**：检查插件参数，以及 AstrBot 运行环境中的网络和代理。

一次调整一个因素，再重复同一个操作，便于确认问题来源。

## 常见问题

| 现象 | 检查与处理 |
| --- | --- |
| 插件加载失败 | 查看首条错误；Python 依赖相关问题按[安装指南](INSTALLATION.md)在 AstrBot 环境重新安装后重载 |
| 提示找不到程序 | 在维护页检查具体组件，按[依赖指南](DEPENDENCIES.md)核对托管目录和执行权限 |
| yt-dlp 签名检查提示缺少 GnuPG | 按[依赖指南](DEPENDENCIES.md)准备 GnuPG，更新运行进程的 PATH 后重新检查 |
| 搜索为空 | 检查网络、yt-dlp 状态及[搜索和音频参数](CONFIGURATION.md) |
| 聊天指令没有响应 | 检查 AstrBot 唤醒前缀、插件开关、群 ID 配置和日志 |
| 控制台没有目标会话 | 在目标 QQ 会话使用点歌帮助指令，再刷新控制台 |
| 队列暂停 | 查看当前错误，处理依赖或配置问题后执行使用指南中的继续操作 |
| 提示处理等待队列已满或等待超时 | 等待现有任务完成后重试，按实际容量检查并发与等待配置；没有自动重复提交歌曲 |
| 提示存储或缓存路径无效 | 停用插件，核对数据目录权限、符号链接和 Windows junction；保留备份，不将缓存目录指向其他项目 |
| 依赖正常但媒体格式被拒绝 | 核对是否为单个视频及受支持的单文件音频；不要通过放宽播放列表或网络协议白名单绕过检查 |
| 语音处理或发送出现错误 | 查看日志中的下载、转换、消息阶段；检查协议端的语音设置及音频文件访问路径 |
| 更新一直等待 | 查看是否有音乐任务或其他更新占用维护资源，并核对[超时配置](CONFIGURATION.md) |

## 网络检查

检查 AstrBot 所在的设备或容器，而不是浏览器所在设备。使用系统已有的网络工具检查 YouTube 和依赖下载源的连接：

```bash
curl -I --connect-timeout 10 https://www.youtube.com
curl -I --connect-timeout 10 https://api.github.com
```

这些命令检查连接和 HTTP 响应；进一步的媒体问题结合插件日志定位。YouTube 和依赖下载的代理设置在[配置参考](CONFIGURATION.md)中分别说明。

## 数据位置

插件使用 AstrBot 的 `StarTools.get_data_dir()` 管理数据。默认目录如下：

```text
data/plugin_data/astrbot_plugin_youtube_music/
  dependencies/          # 外部运行程序
  cache/audio/           # 音频缓存
  cache/jobs/            # 临时媒体任务
  dependency-staging/    # 更新暂存与恢复备份
  library.json           # 收藏和语音历史
  queue_state.json       # 会话与队列
```

AstrBot 使用自定义数据根目录时，以实际数据目录为准。容器中的音频路径应与协议端可访问的目录映射对应。

## 备份

1. 在 AstrBot 中停用插件。
2. 复制整个插件数据目录到独立备份位置。
3. 完成备份后重新启用。

在 AstrBot 项目根目录执行，以下 PowerShell 示例从默认数据目录创建带时间戳的备份：

```powershell
$backup = "youtube-music-backup-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
Copy-Item -LiteralPath "data/plugin_data/astrbot_plugin_youtube_music" -Destination $backup -Recurse
```

## 恢复更新

维护页显示恢复目录时：

1. 停用插件，记录 `recovery_directory` 和 `recovery_targets`。
2. 在 `dependency-staging/<任务标识>/` 中检查对应的 `backup-*` 文件。
3. 将备份恢复到对应的托管程序路径，保留恢复副本。
4. 确认恢复完整后清理该任务的暂存目录，重新启用并检查依赖版本。

运行程序路径见[依赖指南](DEPENDENCIES.md)。恢复完成前保留所有备份。

## 恢复收藏和队列

出现 JSON 存储错误时，停用插件并备份数据目录，检查 `.corrupt-*` 文件。将备份中有效的 `library.json` 或 `queue_state.json` 恢复到对应位置，再启用插件。需要创建空数据时，先归档相关数据文件及损坏备份，再启动。

## 提交问题

仍有问题时，按[贡献指南](https://github.com/pakhozako/astrbot_plugin_youtube_music/blob/main/CONTRIBUTING.md)提供版本、复现步骤和脱敏日志。涉及凭据或权限的问题通过[安全报告入口](https://github.com/pakhozako/astrbot_plugin_youtube_music/security/advisories/new)提交。
