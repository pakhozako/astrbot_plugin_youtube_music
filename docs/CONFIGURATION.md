# 配置

[文档目录](index.md) / 配置

在 AstrBot 插件配置页修改参数，保存后重载插件。重载后使用队列的“继续”操作恢复处理。

字段来源为 [`_conf_schema.json`](../_conf_schema.json)，运行解析见 [`core/settings.py`](../core/settings.py)。大小单位为 MiB，时间单位在各字段中注明。

## 填写示例

以下 JSON 展示一组配置值；在宿主配置页按字段填写：

```json
{
  "allowed_group_ids": "123456,234567",
  "queue_admin_user_ids": "10001",
  "search_results": 8,
  "max_duration_seconds": 300,
  "max_wav_size_mb": 16,
  "search_result_recall_delay_seconds": 45
}
```

ID 列表使用逗号、分号或空白分隔。权限分配见[安全指南](https://github.com/pakhozako/astrbot_plugin_youtube_music/blob/main/SECURITY.md)。

## 搜索与消息清理

| 字段 | 默认值 | 范围 / 选项 | 用途 |
| --- | --- | --- | --- |
| `search_results` | `5` | 3–10 | 搜索候选数量 |
| `result_ttl_minutes` | `15` | 1–120 分钟 | 候选有效期 |
| `request_cooldown_seconds` | `3` | 0–60 秒 | 同一用户会话的搜索冷却 |
| `search_result_auto_recall` | `true` | 布尔值 | 搜索结果自动撤回 |
| `search_result_recall_delay_seconds` | `60` | 5–120 秒 | 搜索结果消息撤回延迟 |

## 访问与发送

| 字段 | 默认值 | 范围 / 选项 | 用途 |
| --- | --- | --- | --- |
| `allowed_group_ids` | `""` | 群 ID 列表 | 启用点歌的群；留空允许所有群 |
| `queue_admin_user_ids` | `""` | 用户 ID 列表 | QQ 队列管理员 |
| `send_strategy` | `"voice_then_link"` | `voice_only` / `voice_then_link` / `link_only` | 歌曲发送方式 |

发送策略：

- `voice_only`：语音。
- `voice_then_link`：优先语音，提供视频链接回退。
- `link_only`：视频链接。

## 音频

| 字段 | 默认值 | 范围 / 选项 | 用途 |
| --- | --- | --- | --- |
| `max_duration_seconds` | `180` | 15–900 秒 | 歌曲时长上限 |
| `max_download_size_mb` | `50` | 5–500 MiB | 源媒体下载大小上限 |
| `max_wav_size_mb` | `8` | 1–100 MiB | WAV 文件大小上限 |

转换格式为 16 kHz、单声道、PCM S16LE WAV，每秒约 32,000 bytes。调整时长时同步检查 WAV 容量：300 秒约需 9.2 MiB。

## 队列与并发

| 字段 | 默认值 | 范围 / 选项 | 用途 |
| --- | --- | --- | --- |
| `max_session_queue` | `20` | 1–50 | 每会话的队列容量，包含当前未提交处理项 |
| `max_concurrent_jobs` | `2` | 1–5 | 全局媒体任务并发数 |
| `max_queued_jobs` | `5` | 0–50 | 等待媒体任务处理的数量 |
| `send_concurrency` | `2` | 1–5 | 消息 API 并发数 |

## 超时

| 字段 | 默认值 | 范围 / 选项 | 用途 |
| --- | --- | --- | --- |
| `search_timeout_seconds` | `45` | 10–180 秒 | 搜索 |
| `metadata_timeout_seconds` | `45` | 10–180 秒 | 视频信息读取 |
| `download_timeout_seconds` | `240` | 30–1800 秒 | 媒体下载 |
| `conversion_timeout_seconds` | `90` | 10–600 秒 | 音频转换 |
| `send_timeout_seconds` | `60` | 5–300 秒 | 消息发送 |
| `queue_wait_timeout_seconds` | `120` | 5–900 秒 | 等待任务或维护资源 |
| `dependency_update_timeout_seconds` | `1800` | 60–7200 秒 | 完整依赖更新流程 |

## 缓存与音乐库

| 字段 | 默认值 | 范围 / 选项 | 用途 |
| --- | --- | --- | --- |
| `audio_cache_retention_hours` | `24` | 1–720 小时 | 音频缓存保留时间 |
| `audio_cache_max_mb` | `512` | 32–8192 MiB | 音频缓存容量 |
| `max_history_items` | `100` | 10–500 | 每个用户会话的语音历史条数 |
| `max_favorites_per_user` | `100` | 10–500 | 每个用户会话的收藏容量 |

## 代理

| 字段 | 默认值 | 范围 / 选项 | 用途 |
| --- | --- | --- | --- |
| `youtube_proxy` | `""` | `http` / `https` / `socks5` | yt-dlp 网络代理 |
| `dependency_proxy` | `""` | `http` / `https` | 运行程序下载代理 |

地址示例：`http://127.0.0.1:7890`。填写代理主机与端口，凭据在宿主的秘密配置中管理。

`youtube_proxy` 留空时使用 yt-dlp 的系统环境；`dependency_proxy` 留空时直接连接下载源。网络诊断见[故障排查](TROUBLESHOOTING.md)。
