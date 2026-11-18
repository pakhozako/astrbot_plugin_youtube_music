# 依赖

[文档目录](index.md) / 依赖

## Python 依赖

AstrBot 根据 [`requirements.txt`](../requirements.txt) 安装运行依赖：

| 依赖 | 版本声明 | 用途 |
| --- | --- | --- |
| aiohttp | `>=3.14.5,<4` | 异步 HTTP 下载 |
| Pydantic | `>=2.12.5,<3` | AI Tool 参数与数据类 |

Python 环境安装操作见[安装指南](INSTALLATION.md)。前端和测试依赖在[开发指南](https://github.com/pakhozako/astrbot_plugin_youtube_music/blob/main/docs/DEVELOPMENT.md)维护；仓库已包含构建好的控制台。

## 运行程序

| 程序 | 用途 | 下载来源与校验 |
| --- | --- | --- |
| yt-dlp | 搜索、视频信息与音频下载 | [yt-dlp Releases](https://github.com/yt-dlp/yt-dlp/releases)，发布者签名与 SHA-256 |
| Deno | yt-dlp JavaScript runtime | [Deno Releases](https://github.com/denoland/deno/releases)，发布资产 SHA-256 |
| FFmpeg / ffprobe | 转换音频与检查媒体 | [Gyan](https://www.gyan.dev/ffmpeg/builds/) Windows 构建、[BtbN](https://github.com/BtbN/FFmpeg-Builds/releases) Linux 构建及校验清单 |
| GnuPG | 验证 yt-dlp 发布签名 | [GnuPG 官方下载](https://gnupg.org/download/)，由系统安装 |

插件执行 yt-dlp（包括版本检查）时禁用隐式 `yt-dlp.conf` 和自动插件目录。需要代理时使用[配置参考](CONFIGURATION.md)中的 `youtube_proxy`；留空保持已声明的系统环境代理回退，不读取其他 yt-dlp 配置中的下载、路径或执行选项。

音频探测和转换只允许常规单文件音频容器（M4A/MP4、WebM、OGG、MP3、AAC、FLAC、WAV 等），不接受 HLS 或 concat 播放列表。外部运行程序仍须来自可信发布并及时更新。

### 准备 GnuPG

在 AstrBot 所在的设备或容器安装 GnuPG，将 `gpg` 加入该运行进程的 PATH。重新启动运行环境后检查：

```bash
gpg --version
```

Windows 安装入口见 [Gpg4win](https://www.gpg4win.org/)。

### 在控制台安装和更新

1. 打开 `console → 维护`，执行依赖检查。
2. 选择一个组件或“全部组件”，点击 **更新依赖**并确认。
3. 查看下载、验证暂存程序、安装和最终验证的进度。
4. 完成后再次检查组件版本。

更新过程中可以点击 **取消更新**，等待当前操作和恢复结束。更新总超时由[配置参考](CONFIGURATION.md)中的 `dependency_update_timeout_seconds` 控制。

维护页也提供音频缓存清理。恢复目录的处理步骤统一见[故障排查](TROUBLESHOOTING.md)。

## 手动安装运行程序

先从上述来源取得对应运行环境的程序并核对发布摘要或签名。停用插件，将文件放入插件[数据目录](TROUBLESHOOTING.md)中的以下位置：

```text
dependencies/
  yt-dlp/
    yt-dlp[.exe]
  Deno/
    bin/
      deno[.exe]
  FFmpeg/
    bin/
      ffmpeg[.exe]
      ffprobe[.exe]
```

Windows 使用 `.exe` 后缀；其他环境使用无后缀文件。FFmpeg 与 ffprobe 使用同一构建。

在 AstrBot 项目根目录执行（默认数据目录，POSIX 环境）：

```bash
chmod +x data/plugin_data/astrbot_plugin_youtube_music/dependencies/yt-dlp/yt-dlp
chmod +x data/plugin_data/astrbot_plugin_youtube_music/dependencies/Deno/bin/deno
chmod +x data/plugin_data/astrbot_plugin_youtube_music/dependencies/FFmpeg/bin/ffmpeg
chmod +x data/plugin_data/astrbot_plugin_youtube_music/dependencies/FFmpeg/bin/ffprobe
```

重新启用插件，在维护页检查程序和版本。程序目录由插件管理，完整数据结构见[故障排查](TROUBLESHOOTING.md)。

## 许可

打包组件与外部程序的许可信息见[第三方声明](../THIRD_PARTY_NOTICES.md)。
