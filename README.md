# 大A纪要置顶评论监控器

独立 Windows EXE 工具，监控 B 站《大A纪要》指定充电动态当前置顶的一级评论，以及该评论楼中楼里 **UP 主本人**（按 UID 判定）的回复。

- 固定动态：https://www.bilibili.com/opus/1219141791464292387
- 每 120 秒检查一次，置顶变更自动切换
- 自动保留历史，生成 `history.sqlite3` 和 Excel 可打开的 `up_pinned_history.csv`
- 使用本机 Chrome / Edge 登录；无需提供密码，无需安装 Python

## 获取 Windows EXE
进入 [Actions → Build Windows EXE](../../actions/workflows/build-windows.yml)，选择成功的运行，下载 Artifacts 中的 `DaAJiYaoMonitor-Windows`，解压后运行 `DaAJiYaoMonitor.exe`。

首次启动会打开浏览器，请登录有该充电动态权限的 B 站账号。确认页面正常显示后回到命令行按 Enter。

## 注意
- 请勿上传 `browser_profile/`、`history.sqlite3`、`monitor.log`、Cookies。
- 评论接口或置顶字段变动、B 站权限限制可能造成抓取失败；本工具尚待有权限账号实际验证。
- 不能找回首次监控以前已经取消置顶的历史评论。
- 程序需要持续开启才会监控；关闭 EXE 会停止监控。
