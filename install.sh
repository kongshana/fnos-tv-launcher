#!/bin/bash
# 飞牛电视启动器 —— 一键安装
#
# 用法：
#   sudo ./install.sh                      交互安装（先显示探测结果让你确认）
#   sudo ./install.sh --yes                跳过确认
#   sudo ./install.sh --ip 192.168.1.50    手动指定 NAS 内网 IP
#   sudo ./install.sh --port 5666          飞牛 Web 端口（改过就指定）
#   sudo ./install.sh --web-port 8088      启动器自带 Web 服务端口
#   sudo ./install.sh --files-root /vol1/1000/影视   「我的文件」的根目录
#   sudo ./install.sh --dir /vol1/1000     手动指定安装位置
#   sudo ./install.sh --scale 2.0          手动指定界面缩放（一般不用，默认自动探测）
#
# 必须用 sudo 运行（飞牛的 root 默认锁定，输的是你自己的管理员密码）

set -e

# ---------- 参数 ----------
IP=""; PORT=""; WEB_PORT=""; SCALE=""; BASEDIR=""; FILES_ROOT=""; YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ip)         IP="$2"; shift 2;;
    --port)       PORT="$2"; shift 2;;
    --web-port)   WEB_PORT="$2"; shift 2;;
    --scale)      SCALE="$2"; shift 2;;
    --dir)        BASEDIR="$2"; shift 2;;
    --files-root) FILES_ROOT="$2"; shift 2;;
    --yes|-y)     YES=1; shift;;
    -h|--help)    sed -n '2,18p' "$0"; exit 0;;
    *) echo "未知参数: $1"; exit 1;;
  esac
done

# ---------- 0. 权限 ----------
if [ "$(id -u)" != 0 ]; then
  echo "=============================================="
  echo " 请用 sudo 运行： sudo ./install.sh"
  echo " 密码输你登录飞牛的那个（不是 root 密码，飞牛 root 默认锁定）"
  echo "=============================================="
  exit 1
fi

echo "===== 飞牛电视启动器 · 安装 ====="
echo

# ---------- 1. 探测 NAS 内网 IP ----------
if [ -z "$IP" ]; then
  IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1)
  [ -z "$IP" ] && IP=$(hostname -I 2>/dev/null | awk '{print $1}')
fi
[ -z "$IP" ] && { echo "✗ 探测不到内网 IP，请用 --ip 手动指定"; exit 1; }

# ---------- 2. 端口 ----------
[ -z "$PORT" ]     && PORT=5666      # 飞牛 Web 管理页端口
[ -z "$WEB_PORT" ] && WEB_PORT=8088  # 启动器自带的同源 Web 服务

# ---------- 3. 安装目录（存储空间的个人文件夹） ----------
if [ -z "$BASEDIR" ]; then
  for v in /vol1 /vol2 /vol3 /vol4; do
    for d in "$v"/[0-9]*; do
      if [ -d "$d" ]; then BASEDIR="$d"; break 2; fi
    done
  done
fi
[ -z "$BASEDIR" ] && { echo "✗ 没找到 /vol*/<用户ID> 目录，请用 --dir 手动指定"; exit 1; }

INSTALL_DIR="$BASEDIR/fnos-launcher"
WEB_DIR="$INSTALL_DIR/launcher-web"

# 「我的文件」默认就把个人文件夹当根目录
[ -z "$FILES_ROOT" ] && FILES_ROOT="$BASEDIR"

# ---------- 4. 缩放兜底值 ----------
# 实际缩放每次启动都会实时探测（见 start.sh），这里只是万一探测全失败时的兜底
RES_W=""; RES_H=""
if [ -z "$SCALE" ]; then
  SCALE=1.0
  for d in /sys/class/drm/card*-*/; do
    [ "$(cat "$d/status" 2>/dev/null)" = "connected" ] || continue
    m=$(head -1 "$d/modes" 2>/dev/null)
    case "$m" in
      *x*) RES_W=$(echo "$m" | cut -dx -f1); RES_H=$(echo "$m" | cut -dx -f2)
           [ -n "$RES_W" ] && break;;
    esac
  done
  if [ -n "$RES_W" ] && [ -n "$RES_H" ]; then
    SCALE=$(awk -v w="$RES_W" -v h="$RES_H" 'BEGIN{
      s=(w/1920 < h/1080) ? w/1920 : h/1080;
      s=int(s*4+0.5)/4; if(s<0.5)s=0.5; if(s>3.0)s=3.0; printf "%.2f", s}')
  fi
fi

# ---------- 5. 确认 ----------
echo "  飞牛地址    : http://$IP:$PORT"
echo "  启动器服务  : http://$IP:$WEB_PORT  （同源，浏览器与我的文件靠它）"
echo "  安装目录    : $INSTALL_DIR"
echo "  我的文件根  : $FILES_ROOT"
if [ -n "$RES_W" ]; then
  echo "  电视分辨率  : ${RES_W}x${RES_H}（预估 ${SCALE}x 缩放）"
else
  echo "  电视分辨率  : 未探测到（兜底 ${SCALE}x 缩放）"
fi
echo "  缩放方式    : 每次启动实时探测，运行中还会监测分辨率变化"
echo "  开机自启    : launcher.service（vt1）+ launcher-web.service"
echo
if [ "$YES" != 1 ]; then
  printf "确认安装？[Y/n] "
  read -r ans
  case "$ans" in
    n|N|no|NO) echo "已取消"; exit 0;;
  esac
fi
echo

# ---------- 6. 放文件 ----------
SRC=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$WEB_DIR"

cp "$SRC/index.html"      "$INSTALL_DIR/index.html"
cp "$SRC/start.sh"        "$INSTALL_DIR/start.sh"
cp "$SRC/remote-view.sh"  "$INSTALL_DIR/remote-view.sh"
cp "$SRC/uninstall.sh"    "$INSTALL_DIR/uninstall.sh"
cp "$SRC/launcher-web/launcher_web.py" "$WEB_DIR/launcher_web.py"
cp "$SRC/launcher-web/nav.html"        "$WEB_DIR/nav.html"
cp "$SRC/launcher-web/files.html"      "$WEB_DIR/files.html"

# IP / 端口 / 缩放写进模板
sed -i "s|__FNOS_IP__|$IP|g; s|__FNOS_PORT__|$PORT|g" "$INSTALL_DIR/index.html"
sed -i "s|__FNOS_IP__|$IP|g; s|__WEB_PORT__|$WEB_PORT|g; s|__SCALE__|$SCALE|g" "$INSTALL_DIR/start.sh"
# Python 服务的首页就用上面这份（单一来源，别维护两份）
cp "$INSTALL_DIR/index.html" "$WEB_DIR/index.html"

chmod 755 "$INSTALL_DIR/start.sh" "$INSTALL_DIR/remote-view.sh" "$INSTALL_DIR/uninstall.sh"
chmod 644 "$INSTALL_DIR/index.html" "$WEB_DIR/"*
echo "✓ 文件已放到 $INSTALL_DIR"

# ---------- 7. 装依赖 ----------
echo "  正在安装依赖（xorg / chromium …），约 1-3 分钟，请稍候…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  xorg xinit x11-xserver-utils x11-utils chromium fonts-noto-cjk alsa-utils 2>&1 | tail -3
# 远程查看用的（不插电视时看效果），装不上也不影响主功能
apt-get install -y -qq --no-install-recommends \
  x11vnc websockify novnc imagemagick 2>&1 | tail -2 || true
echo "✓ 依赖安装完成"

# ---------- 8. 允许非控制台启动 X ----------
mkdir -p /etc/X11
printf 'allowed_users=anybody\nneeds_root_rights=yes\n' > /etc/X11/Xwrapper.config
echo "✓ Xwrapper 已配置"

# ---------- 9. 音频走 HDMI ----------
HDMI_LINE=$(aplay -l 2>/dev/null | grep -i 'hdmi' | head -1)
if [ -n "$HDMI_LINE" ]; then
  CARD=$(echo "$HDMI_LINE" | sed -n 's/^card \([0-9]\+\).*/\1/p')
  DEV=$(echo  "$HDMI_LINE" | sed -n 's/^card [0-9]\+.*device \([0-9]\+\).*/\1/p')
  if [ -n "$CARD" ] && [ -n "$DEV" ]; then
    cat > /etc/asound.conf <<EOF
pcm.!default {
    type plug
    slave {
        pcm "hw:$CARD,$DEV"
        rate 48000
    }
}
ctl.!default {
    type hw
    card $CARD
}
EOF
    amixer -c "$CARD" sset "IEC958" on  >/dev/null 2>&1 || true
    amixer -c "$CARD" sset "Master" 100% unmute >/dev/null 2>&1 || true
    echo "✓ 音频已设为 HDMI (hw:$CARD,$DEV)"
  fi
else
  echo "! 没探测到 HDMI 音频设备，跳过音频配置（声音会走主板 3.5mm 口）"
fi

# ---------- 10. 两个 systemd 服务 ----------
cat > /etc/systemd/system/launcher-web.service <<EOF
[Unit]
Description=Launcher web (same-site host + file browser)
After=network-online.target
Wants=network-online.target
Before=launcher.service

[Service]
User=root
WorkingDirectory=$WEB_DIR
Environment=LAUNCHER_WEB_PORT=$WEB_PORT
Environment=LAUNCHER_WEB_ROOT=$WEB_DIR
Environment=LAUNCHER_FILES_ROOT=$FILES_ROOT
ExecStart=/usr/bin/python3 $WEB_DIR/launcher_web.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/launcher.service <<EOF
[Unit]
Description=TV Launcher
After=network-online.target launcher-web.service
Wants=network-online.target launcher-web.service

[Service]
User=root
Environment=DISPLAY=:0
ExecStart=/usr/bin/xinit $INSTALL_DIR/start.sh -- :0 vt1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable launcher-web.service >/dev/null 2>&1 || true
systemctl enable launcher.service     >/dev/null 2>&1 || true
systemctl restart launcher-web.service
systemctl restart launcher.service
echo "✓ 服务已创建并启动（开机自动进启动器）"

# ---------- 11. 结果 ----------
sleep 6
echo
echo "===== 安装完成 ====="
if systemctl is-active --quiet launcher.service; then
  echo "  电视启动器 : $(systemctl is-active launcher.service)"
  echo "  Web 服务   : $(systemctl is-active launcher-web.service)"
  echo "  生效参数   : $(cat "$INSTALL_DIR/.resolution" 2>/dev/null || echo '(未知)')"
else
  echo "  服务状态 : 未运行 —— 执行 journalctl -u launcher -n 50 看报错"
fi
echo
echo "  五个入口   : 飞牛影视 / 飞牛相册 / 飞牛音乐 / 我的文件 / 浏览器"
echo "  看效果     : 接 HDMI 的电视上现在应该已经有画面了"
echo "  远程查看   : sudo bash $INSTALL_DIR/remote-view.sh start"
echo "             然后浏览器开 http://$IP:6080/vnc.html"
echo "  看分辨率   : sudo bash $INSTALL_DIR/start.sh --dry-run"
echo "  改网址     : 编辑 $INSTALL_DIR/index.html 顶部 APPS，然后"
echo "              systemctl restart launcher.service"
echo "  停止       : systemctl stop launcher.service（可加 disable 取消自启）"
echo "  卸载       : sudo bash $INSTALL_DIR/uninstall.sh"
echo
