# 贡献

[文档目录](docs/index.md) / 贡献

欢迎提交问题、改进文档、完善测试或贡献功能。

## 提交 Issue

在 [GitHub Issues](https://github.com/pakhozako/astrbot_plugin_youtube_music/issues) 提供：

- 插件、AstrBot、Python、协议端版本和运行环境。
- 最小复现步骤、预期行为和实际结果。
- 已去除凭据、QQ 身份和个人内容的日志或截图。

提交前按[故障排查](docs/TROUBLESHOOTING.md)完成基础检查。安全问题使用[私密报告](SECURITY.md)。

## 开始开发

按[开发指南](docs/DEVELOPMENT.md)准备环境、运行预览和测试。AstrBot 接口以官方文档及测试宿主源码为依据。

修改行为时补充对应回归；前端变更同步生产资源；配置变更同步 Schema 与配置参考。

提交遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/v1.0.0/)。
例如 `feat(library): add batch queue selection`、`fix(queue): preserve failed entry identity`。
不兼容变更用 `!` 或 `BREAKING CHANGE:` 明示并记录迁移方式。`main` 保持线性历史；
合入前 rebase，并通过 `python -B scripts/check_history.py`。

## 修改文档

| 内容 | 维护位置 |
| --- | --- |
| 项目价值、功能摘要、截图和快速上手 | README |
| 安装方法与更新插件 | 安装指南 |
| QQ 指令、控制台和 AI Tool | 使用指南 |
| 配置字段和示例 | 配置参考 |
| 运行程序、来源与更新 | 依赖指南 |
| 故障诊断、数据和恢复 | 故障排查 |
| 环境、架构、测试和构建 | 开发指南 |
| 权限和漏洞报告 | 安全指南 |
| 贡献流程 | 本文件 |
| 版权、许可与素材来源 | 第三方声明 |

每个主题保留一个详细说明位置，其他页面通过链接引用。示例注明运行目录和环境；涉及行为、字段和资源时与代码及测试一同核对。

## 版本迭代

每轮先审查当前版本、CHANGELOG、发布脚本和 Git 标签。兼容修复使用 PATCH，兼容新功能使用 MINOR，不兼容变更使用 MAJOR；同步全部版本声明、文档和安装包，不重复或跳过版本。

正式标签必须指向经过测试的发布提交，并通过安装包与标签提交的逐文件校验。不能仅修改版本号就宣称发布成功，也不能覆盖已有版本或移动标签。完整步骤见[开发指南](docs/DEVELOPMENT.md#版本发布)。

初始基准为 `0.1.0`。发布门禁根据两个标签之间的 Conventional Commits 判定连续的 MAJOR、MINOR 或 PATCH，
四组 Python 矩阵及前端构建、浏览器回归通过后才由 CI 自动发布。安装包先作为草稿附件上传，
通过摘要和下载来源校验后公开，不覆盖任何已存在的 Release 或附件。

## Pull Request

描述问题、实现方式、用户可见变化和实际测试结果。提交前：

1. 完成开发指南中的检查。
2. 核对文档导航、更新记录和生产资源。
3. 审查完整 diff，确认修改围绕当前任务。
4. 核对新代码和素材的来源与许可。

公开仓库保留源码和测试；个人配置、运行数据、日志、虚拟环境及完整验证工件保留本地。

## 贡献许可

贡献以项目 [MIT License](LICENSE) 提供，版权署名为 `Copyright (c) 2026 pakhozako`。第三方代码或资源保留原许可、版权和来源。提交前确认有权提供相关贡献。
