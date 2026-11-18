# 安装

[文档目录](index.md) / 安装

## 从仓库安装

1. 打开 AstrBot 管理面板，进入 **插件 → 已安装**。
2. 点击 **安装插件（+）→ 从链接安装**。
3. 填入下列地址并确认：

   ```text
   https://github.com/pakhozako/astrbot_plugin_youtube_music
   ```

4. 回到已安装列表，检查插件已启用。
5. 按[依赖指南](DEPENDENCIES.md)准备运行程序。

## 手动安装

在 AstrBot 项目根目录执行：

```bash
git clone https://github.com/pakhozako/astrbot_plugin_youtube_music.git data/plugins/astrbot_plugin_youtube_music
python -m pip install -r data/plugins/astrbot_plugin_youtube_music/requirements.txt
```

使用 AstrBot 实际运行的 Python 环境执行安装命令；容器部署时，在对应容器中执行。随后在插件页加载插件。

## 从 ZIP 安装

在 AstrBot 插件页选择 **安装插件（+）→ 从文件安装**，上传已构建的插件 ZIP，完成后检查已安装列表。

正式安装包见 [GitHub Releases](https://github.com/pakhozako/astrbot_plugin_youtube_music/releases)。使用发布页附件中的插件 ZIP，而非不含安装包 manifest 的自动源码归档。

需要固定版本时，使用对应的 `vMAJOR.MINOR.PATCH` Git 标签和同版本安装包，核对包内 `metadata.yaml` 与 `RELEASE_MANIFEST.json` 的版本。`main` 的未发布快照不等同于正式标签版本，包内摘要也不能代替可信下载来源。

从源码生成 ZIP 的步骤见[开发指南](https://github.com/pakhozako/astrbot_plugin_youtube_music/blob/main/docs/DEVELOPMENT.md)。

## 完成初始化

1. 从插件详情打开 `console` 视图，进入 **维护** 页面检查依赖。
2. 在插件配置页填写需要调整的参数，保存后重载。
3. 按[使用指南](USAGE.md)完成第一次搜索和点歌。

配置字段见[配置参考](CONFIGURATION.md)；加载或依赖问题见[故障排查](TROUBLESHOOTING.md)。

## 更新插件

通过仓库安装的插件在 AstrBot 已安装列表中执行 **更新**，完成后重载。手动安装的插件可在 AstrBot 项目根目录执行：

```bash
git -C data/plugins/astrbot_plugin_youtube_music pull --ff-only
python -m pip install -r data/plugins/astrbot_plugin_youtube_music/requirements.txt
```

再从插件页重载。更新前的数据备份步骤见[故障排查](TROUBLESHOOTING.md)。

## 相关资料

[AstrBot 插件安装说明](https://github.com/AstrBotDevs/AstrBot/blob/2d3384f1d6cdb272c686065cb6c29fe122682f59/docs/zh/use/plugin.md) · [运行依赖](DEPENDENCIES.md) · [开始点歌](USAGE.md)
