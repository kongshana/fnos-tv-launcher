#!/bin/sh
# 电视启动器：Xorg 起来后直接全屏跑 launcher，不加载任何桌面环境
#
# 【分辨率自适应】每次启动都实时读取电视当前分辨率，自动算出
#   --force-device-scale-factor 和 --window-size，不需要手动配置。
#   4K / 2K / 1080p / 720p 通吃，换电视、改分辨率、HDMI 重新握手都能自动跟上。
#   运行中每 10 秒复查一次，分辨率一变就重启 Chromium 重新适配。
#
# 换算要点：--window-size 是 CSS 像素，会再乘 scale 得到物理像素。
#   所以窗口尺寸必须 = 物理分辨率 / scale，直接填物理值会溢出屏幕。
#
# 参数：
#   --dry-run    只打印探测与计算结果，不启动 Chromium（排障用）
#   --no-watch   启动一次就退出，不做分辨率变化监测

DIR=$(cd "$(dirname "$0")" && pwd)

FALLBACK_SCALE=__SCALE__   # install.sh 写入；仅在所有探测手段都失败时使用
TARGET_W=1920              # 设计基准（CSS 逻辑分辨率），卡片按此尺寸设计
TARGET_H=1080
WATCH_INTERVAL=10          # 分辨率复查间隔（秒）

DRY=0
WATCH=1
for a in "$@"; do
  case "$a" in
    --dry-run)  DRY=1;;
    --no-watch) WATCH=0;;
  esac
done

# ---------------- 分辨率探测（四种手段依次回落） ----------------
# 结果写入全局变量 W / H（物理像素）
detect_res(){
  W=""
  H=""
  geo=""
  SRC="兜底值 1920x1080"

  # 1) xrandr --listmonitors —— 最可靠，能定位到主输出
  #    输出形如： 0: +*DP-3 3840/1600x2160/900+0+0  DP-3
  geo=$(xrandr --listmonitors 2>/dev/null | awk 'NR==2{print $3}')
  [ -n "$geo" ] && geo=$(echo "$geo" | sed 's/[+-].*//')
  if [ -n "$geo" ]; then
    W=$(echo "$geo" | cut -dx -f1 | cut -d/ -f1)
    H=$(echo "$geo" | cut -dx -f2 | cut -d/ -f1)
    SRC="xrandr --listmonitors"
  fi

  # 2) xrandr 当前模式行（带 * 的那条）
  case "$W$H" in ''|*[!0-9]*)
    geo=$(xrandr 2>/dev/null | awk '/\*/{print $1; exit}')
    W=$(echo "$geo" | cut -dx -f1)
    H=$(echo "$geo" | cut -dx -f2)
    [ -n "$W" ] && SRC="xrandr 当前模式";;
  esac

  # 3) xdpyinfo
  case "$W$H" in ''|*[!0-9]*)
    geo=$(xdpyinfo 2>/dev/null | awk '/dimensions:/{print $2; exit}')
    W=$(echo "$geo" | cut -dx -f1)
    H=$(echo "$geo" | cut -dx -f2)
    [ -n "$W" ] && SRC="xdpyinfo";;
  esac

  # 4) 无 X 时回落内核 DRM/EDID（用于 --dry-run 排查，或 X 还没就绪）
  case "$W$H" in ''|*[!0-9]*)
    for d in /sys/class/drm/card*-*/; do
      [ "$(cat "$d/status" 2>/dev/null)" = "connected" ] || continue
      geo=$(head -1 "$d/modes" 2>/dev/null)
      W=$(echo "$geo" | cut -dx -f1)
      H=$(echo "$geo" | cut -dx -f2)
      case "$W$H" in ''|*[!0-9]*) W=""; H="";; *) SRC="EDID /sys/class/drm"; break;; esac
    done;;
  esac

  # 数字校验 + 兜底
  case "$W$H" in
    ''|*[!0-9]*) W=1920; H=1080; SRC="兜底值 1920x1080";;
  esac
  [ "$W" -lt 640 ] && { W=1920; SRC="兜底值 1920x1080"; }
  [ "$H" -lt 480 ] && { H=1080; SRC="兜底值 1920x1080"; }
}

# ---------------- 缩放与窗口尺寸计算 ----------------
# 逻辑分辨率锁在 ~1920x1080，物理分辨率只影响清晰度：
#   3840x2160 → 2.00x → 窗口 1920x1080
#   2560x1440 → 1.25x → 窗口 2048x1152
#   1920x1080 → 1.00x → 窗口 1920x1080
#   1366x768  → 0.75x → 窗口 1821x1024
#   1280x720  → 0.75x → 窗口 1706x960
calc_scale(){
  SCALE=$(awk -v w="$W" -v h="$H" -v tw="$TARGET_W" -v th="$TARGET_H" 'BEGIN{
    s = (w/tw < h/th) ? w/tw : h/th;
    s = int(s*4 + 0.5) / 4;          # 量化到 0.25 档，保证字体渲染锐利
    if (s < 0.5) s = 0.5;
    if (s > 3.0) s = 3.0;
    printf "%.2f", s
  }')
  [ -z "$SCALE" ] && SCALE=$FALLBACK_SCALE

  # 取整用 floor，保证 窗口 x scale <= 物理分辨率，绝不会溢出屏幕
  WINW=$(awk -v w="$W" -v s="$SCALE" 'BEGIN{printf "%d", w/s}')
  WINH=$(awk -v h="$H" -v s="$SCALE" 'BEGIN{printf "%d", h/s}')
  [ "$WINW" -lt 800 ] && WINW=800
  [ "$WINH" -lt 600 ] && WINH=600
}

# ---------------- 清会话（保证开机回首页，但保留登录态） ----------------
# 只删 Sessions（窗口/标签记录），保留 Cookies 与 Local Storage，
# 所以飞牛登录态不会丢 —— 下次点影视仍是海报墙，不用重新输密码。
clean_session(){
  rm -rf "$DIR/.chrome/Default/Sessions" 2>/dev/null
  rm -f "$DIR/.chrome/Default/Current Session" "$DIR/.chrome/Default/Current Tabs" 2>/dev/null
  rm -f "$DIR/.chrome/SingletonLock" 2>/dev/null
}

# 禁止屏幕休眠（无 DISPLAY 时静默失败，不影响后续）
xset -dpms     2>/dev/null
xset s off     2>/dev/null
xset s noblank 2>/dev/null

detect_res
calc_scale

# ---------------- --dry-run：只报告，不启动 ----------------
if [ "$DRY" = 1 ]; then
  echo "===== 分辨率自适应 · 探测结果 ====="
  echo "  电视物理分辨率 : ${W} x ${H}"
  echo "  界面缩放       : ${SCALE}x"
  echo "  窗口(CSS)      : ${WINW} x ${WINH}"
  echo "  实际占用物理   : $(awk -v a="$WINW" -v s="$SCALE" 'BEGIN{printf "%d", a*s}') x $(awk -v a="$WINH" -v s="$SCALE" 'BEGIN{printf "%d", a*s}')"
  echo "  探测手段       : $SRC"
  echo "  分辨率监测     : $([ "$WATCH" = 1 ] && echo "开启（每 ${WATCH_INTERVAL} 秒复查，变化自动重适配）" || echo '关闭')"
  exit 0
fi

# ---------------- 主循环 ----------------
# 注意：停止请用 systemctl stop launcher.service。
#       直接 pkill chromium 会被这里的循环和 systemd 一起拉起来。
while : ; do
  detect_res
  calc_scale
  echo "[launcher] 分辨率 ${W}x${H}（来源：$SRC）→ 缩放 ${SCALE}x → 窗口 ${WINW}x${WINH}"
  echo "${W}x${H} scale=${SCALE} win=${WINW}x${WINH} src=${SRC}" > "$DIR/.resolution" 2>/dev/null

  clean_session

  chromium \
    --no-sandbox \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --disable-translate \
    --disable-features=TranslateUI,Translate,ThirdPartyStoragePartitioning,PartitionedCookies,TrackingProtection3pcd \
    --disable-suggestions-service \
    --autoplay-policy=no-user-gesture-required \
    --disable-session-crashed-bubble \
    --no-first-run \
    --no-default-browser-check \
    --force-device-scale-factor=$SCALE \
    --window-size=$WINW,$WINH \
    --window-position=0,0 \
    --alsa-output-device=default \
    --enable-gpu-rasterization \
    --ignore-gpu-blocklist \
    --user-data-dir=$DIR/.chrome \
    --check-for-update-interval=31536000 \
    http://__FNOS_IP__:__WEB_PORT__/index.html &
  CHPID=$!

  # 不做监测时：等它结束就退出（--no-watch）
  if [ "$WATCH" != 1 ]; then
    wait "$CHPID" 2>/dev/null
    break
  fi

  # 监测分辨率变化：电视换台/待机唤醒/HDMI 重新握手都会触发
  while kill -0 "$CHPID" 2>/dev/null; do
    sleep "$WATCH_INTERVAL"
    OW=$W
    OH=$H
    detect_res
    if [ "$W" != "$OW" ] || [ "$H" != "$OH" ]; then
      echo "[launcher] 分辨率变化 ${OW}x${OH} → ${W}x${H}，正在重新适配"
      kill "$CHPID" 2>/dev/null
      break
    fi
  done

  wait "$CHPID" 2>/dev/null
  sleep 3          # 防崩溃时高频重启
done
