# Whcm_monitor
Monitor the number of remaining piano rooms.
琴房监控 Piano Room Monitor
============================

【怎么打开 / How to start】
1. 双击 “启动琴房监控.command”。
   Double-click "启动琴房监控.command".

2. 第一次打开时，Mac 可能提示“无法验证开发者”。
   这时：右键点击该文件 → 选择“打开” → 再点“打开”。（只需要这一次）
   The first time the Mac may say it cannot verify the developer.
   Then: right-click the file -> Open -> Open. (only needed once)

3. 每次启动都会先检查电脑上已经有什么（Python、Playwright、浏览器），
   已经装好的不会重复安装，只安装缺少的（需要联网，不需要密码；
   什么都没有的新电脑约 3-10 分钟）。
   Every start first CHECKS what is already on the Mac (Python, Playwright, a browser).
   What is installed is never installed again; only what is missing gets installed
   (internet needed, no password; a brand-new Mac takes about 3-10 minutes).

【如果双击后没有反应 / If double-clicking does nothing】
打开“终端”，输入 bash 加一个空格，把 “启动琴房监控.command” 拖进终端窗口，按回车。
Open Terminal, type "bash " (with a space), drag the .command file into the window, press Enter.

【使用中 / While it runs】
在窗口里输入后按回车：  p = 暂停   r = 继续   t 60 = 修改目标数量   q = 退出
Type then Enter:  p = pause | r = resume | t 60 = change target number | q = quit

【要求 / Needs】 macOS 12 或更新 / macOS 12 or newer, 网络 / internet (first time only).
