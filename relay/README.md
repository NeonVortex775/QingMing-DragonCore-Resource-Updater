relay/ —— 内网穿透客户端的放置处（仅 mode = relay 需要）

如果你用 frp 做穿透：到 https://github.com/fatedier/frp/releases 下载
`frp_0.35.x_windows_amd64.zip`，解压出里面的 frpc.exe（约 10MB）放进来即可，
不需要 frps.exe 和任何 ini —— 启动器会自动按 ../config.ini 生成配置再调用它。

用其它穿透工具的话：不需要放这里，按该工具自己的用法把本地端口指到 mux_port。

mode = direct（VPS / 云服务器直连）时：本目录用不上，忽略即可。
