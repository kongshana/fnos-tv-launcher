#!/bin/sh
# 远程查看 / 操作电视画面（不插电视也能看效果）
#
# 原理：x11vnc 把正在跑的 Xorg（:0，也就是电视上那个全屏启动器）共享出来，
#       websockify + noVNC 再包一层网页，浏览器直接开就能看、能点。
#
# 用法：
#   sudo bash remote-view.sh start     启动（浏览器开 http://<NAS_IP>:6080/vnc.html）
#   sudo bash remote-view.sh stop      停止
#   sudo bash remote-view.sh status    查看状态
#   sudo bash remote-view.sh shot      只截一张当前画面到 /tmp/tv_screen.png
#
# 注意：默认没设密码（-nopw），只适合在内网用。要加密码改成 -passwd 你的密码。

DISP=":0"
RFB_PORT=5900
WEB_PORT=6080
SHOT=/tmp/tv_screen.png

[ "$(id -u)" = "0" ] || { echo "请用 sudo 运行"; exit 1; }

case "$1" in

  start)
    pkill x11vnc    2>/dev/null
    pkill websockify 2>/dev/null
    sleep 1

    x11vnc -display "$DISP" -forever -shared -nopw \
           -rfbport "$RFB_PORT" -bg -o /tmp/x11vnc.log >/dev/null 2>&1
    sleep 2

    if ! pgrep x11vnc >/dev/null; then
      echo "✗ x11vnc 启动失败，看 /tmp/x11vnc.log"
      tail -5 /tmp/x11vnc.log 2>/dev/null
      exit 1
    fi

    websockify -D --web=/usr/share/novnc "$WEB_PORT" localhost:"$RFB_PORT" >/dev/null 2>&1
    sleep 2

    IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1)
    [ -z "$IP" ] && IP=$(hostname -I 2>/dev/null | awk '{print $1}')

    echo "✓ 已启动"
    echo "  浏览器打开 : http://$IP:$WEB_PORT/vnc.html"
    echo "  VNC 客户端 : $IP:$RFB_PORT （TightVNC / RealVNC / MobaXterm 均可）"
    echo "  当前画面   : $(DISPLAY=$DISP xrandr --listmonitors 2>/dev/null | tail -1 | awk '{print $3}' | awk -F'[x/]' '{print $1"x"$3}')"
    ;;

  stop)
    pkill websockify 2>/dev/null
    pkill x11vnc     2>/dev/null
    echo "✓ 已停止"
    ;;

  status)
    if pgrep x11vnc >/dev/null; then echo "x11vnc     : 运行中 (端口 $RFB_PORT)"; else echo "x11vnc     : 未运行"; fi
    if pgrep websockify >/dev/null; then echo "websockify : 运行中 (端口 $WEB_PORT)"; else echo "websockify : 未运行"; fi
    IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1)
    echo "访问地址   : http://$IP:$WEB_PORT/vnc.html"
    echo "电视分辨率 : $(DISPLAY=$DISP xrandr --listmonitors 2>/dev/null | tail -1 | awk '{print $3}' | awk -F'[x/]' '{print $1"x"$3}')"
    ;;

  shot)
    DISPLAY=$DISP xwd -root -silent -out /tmp/_shot.xwd 2>/dev/null \
      && convert xwd:/tmp/_shot.xwd -resize 1600x "$SHOT" 2>/dev/null \
      && rm -f /tmp/_shot.xwd \
      && echo "✓ 已截图: $SHOT" \
      || echo "✗ 截图失败（需要 x11-apps 和 imagemagick）"
    ;;

  *)
    sed -n '2,16p' "$0"
    ;;
esac
