# iStoreOS 部署与 ADB 控制说明

本文说明在 iStoreOS 上运行 MPD Web、配置账号密码、连接小米电视并使用 ADB 控制台。文中的 IP、账号和密码均为示例，按实际环境替换；不要把真实密码或密钥提交到代码仓库。

## 1. 安装并连接 ADB

在 iStoreOS 上安装软件源提供的 `adb` 包：

```sh
apk update
apk add adb
adb version
```

如果 `android-tools-adb` 提示找不到包，使用当前源里的包名 `adb`。连接电视前，在电视设置中开启网络调试并允许调试授权：

```sh
adb connect 电视IP:5555
adb devices
adb -s 电视IP:5555 shell getprop ro.product.model
```

设备状态应显示为 `device`。当前 iStoreOS 上的 ADB 版本为 1.0.32；若电视只支持 Android 11 之后的“无线调试/配对码”方式，可能需要新版 ADB。不要把电视的 5555 端口映射到公网。

## 2. 配置登录凭据

登录信息由 Flask 进程的环境变量提供。环境变量不会自动保存，所以需要写入启动服务时读取的 root 专用文件。

先生成一次随机 session 密钥：

```sh
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

将命令输出保存下来，创建 `/etc/mpd-web.env`：

```sh
umask 077
cat > /etc/mpd-web.env <<'EOF'
MPD_WEB_USERNAME='admin'
MPD_WEB_PASSWORD='168888'
MPD_WEB_SECRET_KEY='47990b5d833bc65e0c2fd0c73c4695f96c9e378b51d7674734863af11a5d5725'
EOF
chmod 600 /etc/mpd-web.env
```

确保 `/root/mpd/` 下的 `mpd_web.py`、`adb.html` 和 `login.html` 三个文件一起部署，HTML 文件与 Python 程序必须位于同一目录。编辑 `/etc/init.d/mpd-web`，在 `start_service()` 中、`procd_open_instance` 之前读取文件，并把变量传给 Python 进程。保留原有 `APP`、`PYTHON` 及 `procd_set_param command` 配置，在服务函数中加入：

```sh
    [ -r /etc/mpd-web.env ] || return 1
    . /etc/mpd-web.env

    procd_open_instance
    procd_set_param command "$PYTHON" "$APP"
    procd_set_param env \
        "MPD_WEB_USERNAME=$MPD_WEB_USERNAME" \
        "MPD_WEB_PASSWORD=$MPD_WEB_PASSWORD" \
        "MPD_WEB_SECRET_KEY=$MPD_WEB_SECRET_KEY"
```

每次启动都必须使用同一组账号、密码和 session 密钥。不要每次启动重新生成密钥，否则已有登录会话会失效。修改服务脚本后重启：

```sh
/etc/init.d/mpd-web restart
```

如需开机启动：

```sh
/etc/init.d/mpd-web enable
```

## 3. 登录和页面功能

在浏览器打开：

```text
http://iStoreOS地址:8800/
```

登录后可使用播放器和媒体库；ADB 控制台位于：

```text
http://iStoreOS地址:8800/adb
```

ADB 控制台可连接/断开设备、查看应用包名、启动应用、向电视发送常用遥控按键、尝试打开网址、预览截图，以及保存和导出 ADB 指令记录。电视 IP 示例：`192.168.68.156`，默认 ADB 端口通常为 `5555`。

### 网页上传并安装 APK

在 ADB 页面选择 APK 并点击“上传并安装”。文件从当前浏览器上传到 iStoreOS，再执行 `adb install -r` 安装到当前选中的电视。APK 上限为 256MB；iStoreOS 需要有足够的 `/tmp` 临时空间。安装完成后服务端临时 APK 会删除。若提示设备不允许安装，需要在电视设置中允许安装未知来源应用；只安装可信来源的 APK。

### 自定义 ADB 命令

在“自定义 ADB 指令”中输入子命令，不要加 `adb -s 设备地址` 前缀。示例：

```sh
shell ls -lah /sdcard/Download
shell pm list packages
shell input keyevent 3
```

允许的顶层子命令为 `shell`、`install`、`uninstall`、`push`、`pull`、`logcat` 和 `reboot`，执行结果会写入指令记录。网页的“ADB 指令手册与示例”可点击“填入”将命令放入输入框。常用示例：

```sh
shell getprop ro.product.model
shell ls -lah /sdcard/Download
shell monkey -p com.phlox.tvwebbrowser 1
install -r /root/mpd/tvbro.apk
uninstall com.phlox.tvwebbrowser
push /root/mpd/tvbro.apk /sdcard/Download/tvbro.apk
pull /sdcard/Download/tvbro.apk /tmp/tvbro.apk
logcat -d -t 200
reboot
```

`install` 和 `push` 的本地源路径在 iStoreOS；`push` 的目标路径和 `pull` 的源路径在电视。`pull` 的目标路径写入 iStoreOS。`shell` 命令在电视端执行，具有设备端操作能力；仅对可信用户开放，不要将服务暴露到公网。R1 的 `pm list packages` 可能返回 `error: closed`；可以在 iStoreOS SSH 终端使用 `adb -s R1地址 shell dumpsys package > /tmp/r1-package-dump.txt`，再从转储文本筛选包名。

打开网址失败时，可先让“限制处理应用”留空，由电视默认处理应用接收网址。如果页面提示无法解析 Intent，检查电视是否安装并注册了能处理 HTTP/HTTPS 的浏览器。也可从应用列表中单独启动浏览器验证。

### 安装 TV Bro 浏览器

此电视安装成功的版本是 `tvbro-generic-2.0.1.armeabi-v7a.apk`，应用包名为 `com.phlox.tvwebbrowser`。通用 APK 安装曾报 `INSTALL_FAILED_NO_MATCHING_ABIS`；应选择与电视 ABI 匹配的 APK。可用下面命令查看电视 ABI：

```sh
adb -s 192.168.68.156:5555 shell getprop ro.product.cpu.abilist
```

如果 APK 已经放在 iStoreOS 的 `/root/mpd/`，直接从 iStoreOS 本地路径安装：

```sh
adb -s 192.168.68.156:5555 install -r /root/mpd/tvbro-generic-2.0.1.armeabi-v7a.apk
```

`adb install` 接受的是 ADB 主机（iStoreOS）上的文件路径，不是电视路径。若要先把文件复制到电视下载目录，使用 `push`：

```sh
adb -s 192.168.68.156:5555 push -p /root/mpd/tvbro-generic-2.0.1.armeabi-v7a.apk /sdcard/Download/tvbro.apk
adb -s 192.168.68.156:5555 shell ls -lah /sdcard/Download
```

网页 ADB 控制台中可用“上传并安装”，也可在自定义命令中输入不带 `adb -s ...` 前缀的命令，例如：

```sh
install -r /root/mpd/tvbro-generic-2.0.1.armeabi-v7a.apk
shell monkey -p com.phlox.tvwebbrowser 1
```

## 4. MPD HTTP 音频流和开机歌单

将仓库中的 `mpd.conf` 部署到 `/etc/mpd.conf`，并将 `mpd-autoplay` 部署到 `/etc/init.d/mpd-autoplay`。在 `mpd-autoplay` 顶部修改 `PLAYLIST`，填写 MPD 已存在的歌单名；默认是 `华语歌`。脚本会先确认歌单存在，再清空队列，避免歌单名写错时误清正在播放的队列。然后在 iStoreOS 上检查 MPD 编译功能并启用开机自动播放：

MPD 默认监听 `6601`。网页顶部可在 MPD 和 OwnTone 之间切换，选择保存在当前浏览器登录会话中。OwnTone 的 MPD 兼容端口默认是 `6600`，网页歌单通过 OwnTone JSON API（默认 `3689`）读取；如监听地址不同，可在 `/etc/mpd-web.env` 设置 `MPD_HOST`、`MPD_PORT`、`OWNTONE_HOST`、`OWNTONE_PORT` 和 `OWNTONE_API_PORT`，然后重启 `mpd-web`。OwnTone 模式可浏览、展开、播放和删除歌单；保存/导入歌单以及单曲移入/移出已存歌单仍使用 MPD 的文件歌单功能。

```sh
mpd --version
mpc lsplaylists
chmod +x /etc/init.d/mpd-autoplay
/etc/init.d/mpd-autoplay enable
/etc/init.d/mpd restart
/etc/init.d/mpd-autoplay start
mpc status
mpc outputs
```

`mpd --version` 应包含 `httpd` 和 `lame` 输出/编码器支持；若缺少其中之一，需要安装对应的 MPD 插件包或改用设备已支持的编码器。MPD 重启后，开机服务会等待 MPD 就绪，清空当前队列、载入配置的歌单并播放；手动重启 MPD 后，如需立即重新载入指定歌单，再执行一次 `/etc/init.d/mpd-autoplay start`。HTTP 流地址为 `http://iStoreOS地址:8000/`，可先用 VLC 等播放器测试。

HTTP 流会向局域网开放，且任何能访问该端口的设备都可能收听；不要将 8000 端口映射到公网。网页的 MPD 输出区允许同时勾选多个输出，例如 USB 声卡和 HTTP 流。顶部音量滑块会同时调整 MPD 输出和当前浏览器的 HTTP 音频音量。HTTP 流默认对进度和歌词补偿约 6 秒缓冲，歌词栏的半秒按钮可继续微调；纯 USB 输出不应用这项补偿。切歌时网页保持 HTTP 连接，避免丢弃浏览器中已缓冲的上一首尾音。启用 HTTP 流时，前端会自动尝试接收音频；若浏览器拦截自动播放，MPD 正在播放时中央播放键会显示为播放图标，点击后只重连网页音频，不会暂停 MPD。此方式是在网页内播放，不会向 DLNA Renderer 发送推送命令。

## 5. 屏幕预览说明

ADB 控制台使用 `screencap` 将电视截图保存到临时文件，再用 ADB 拉回网页显示。预览约每 1.2 秒刷新一次，不是实时视频；停止预览后不会继续轮询。电视端临时截图会在传回后删除。

普通 ADB 截图在 HDMI 输入或受保护的视频画面上可能得到黑屏。若主页可截图而 HDMI 画面黑屏，通常是显示层/内容保护的限制；小米电视助手可能使用不同的厂商截图通道，标准 `screencap` 不一定能取得同一画面。

## 6. 外网访问安全

不要把 Flask 的 `8800` 端口或电视的 `5555` ADB 端口直接映射到公网。ADB 控制台具备远程控制电视的能力，登录页面也应只在可信网络使用。

推荐先连接 WireGuard 等 VPN，再访问 iStoreOS 的 VPN 地址。若必须通过反向代理提供外部 HTTPS 访问，应同时限制来源、启用 HTTPS，并确认防火墙不允许公网直连 Flask/ADB 端口。

## 7. 常见检查

- 查看 ADB 设备：`adb devices -l`
- 重新连接电视：`adb connect 电视IP:5555`
- 确认网页服务状态：`/etc/init.d/mpd-web status`
- 重启网页服务：`/etc/init.d/mpd-web restart`
- 查看服务日志：`logread -e mpd-web`
- 登录页提示未配置：检查服务脚本是否读取 `/etc/mpd-web.env`，并通过 `procd_set_param env` 将三个变量传入 Python 进程。
- 登录后反复跳回登录页：检查所有 worker 是否使用相同且固定的 `MPD_WEB_SECRET_KEY`，并确认浏览器允许 session cookie。
- ADB 页面显示没有设备：检查 iStoreOS 上的 `adb connect` 状态、电视授权提示，以及运行 Flask 的用户能否执行 `adb`。
