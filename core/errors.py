"""Map yt-dlp and subprocess failures to concise, actionable user messages."""

from __future__ import annotations

from .process import ProcessCaptureLimit, ProcessError, ProcessOutputLimit, ProcessTimeout


class UserFacingError(RuntimeError):
    """An expected error suitable for displaying to the user."""


def friendly_process_error(
    exc: BaseException, stage: str, *, max_download_mb: int | None = None
) -> str:
    raw = str(exc)
    text = raw.casefold()

    if isinstance(exc, ProcessCaptureLimit):
        return f"{stage}输出超过上限，已停止。"
    if isinstance(exc, ProcessOutputLimit) or any(
        marker in text
        for marker in (
            "exceeded the size limit",
            "file is larger than max-filesize",
            "max-filesize",
        )
    ):
        cap = f"{max_download_mb} MiB" if max_download_mb else "配置的大小上限"
        return f"下载文件超过上限（{cap}），已停止。"
    if (
        isinstance(exc, ProcessTimeout)
        or "超过" in raw
        and "秒仍未完成" in raw
        or "timed out" in text
        or "timeout" in text
    ):
        return f"{stage}超时，请稍后重试。"
    if any(
        marker in text
        for marker in ("http error 429", "429 too many requests", "rate limit", "too many requests")
    ):
        return "YouTube 请求受限（429），请稍后重试。"
    if any(
        marker in text
        for marker in (
            "not available in your country",
            "not available in your region",
            "geo-restricted",
            "geographic restriction",
            "blocked in your country",
            "country restriction",
        )
    ):
        return "该视频存在地区限制，当前运行环境无法访问。"
    if any(
        marker in text
        for marker in (
            "video unavailable",
            "this video is unavailable",
            "private video",
            "has been removed",
            "video has been removed",
            "not available",
        )
    ):
        return "视频不可用，请更换公开视频。"
    if any(
        marker in text
        for marker in (
            "javascript challenge",
            "js challenge",
            "ejs",
            "deno",
            "javascript runtime",
            "challenge solver",
            "signature extraction failed",
        )
    ):
        return "YouTube 验证失败，请检查 yt-dlp 和 Deno。"
    if any(
        marker in text
        for marker in (
            "urlopen error",
            "connection reset",
            "connection refused",
            "network is unreachable",
            "temporary failure in name resolution",
            "name or service not known",
            "tls",
            "ssl:",
            "http error 5",
            "unable to download webpage",
            "unable to download api page",
        )
    ):
        return f"{stage}网络请求失败，请稍后重试。"
    if any(
        marker in text
        for marker in ("sign in to confirm", "age-restricted", "confirm your age", "login required")
    ):
        return "视频需要登录或年龄验证，无法下载。"
    if "no audio" in text or "does not contain any audio" in text:
        return "视频没有可用音轨，无法转换为语音。"
    if isinstance(exc, ProcessError):
        return f"{stage}失败，请检查依赖与视频限制。"
    return f"{stage}失败，请查看插件日志。"
