#!/bin/bash
# 卸载飞牛电视启动器，恢复到原来的飞牛控制台界面
# 用法：sudo ./uninstall.sh
#
# 会做：停两个服务 → 删服务文件 → 删安装目录 → 删音频配置
# 不会卸载 apt 装的 xorg/chromium（占几百 MB，留着方便以后重装；想删见文末）

if [ "$(id -u)" != 0 ]; then
  echo "请用 sudo 运行： sudo ./uninstall.sh"
  exit 1
fi

# 从服务文件里读回安装目录
INSTALL_DIR=""
if [ -f /etc/systemd/system/launcher.service ]; then
  INSTALL_DIR=$(sed -n 's|^ExecStart=/usr/bin/xinit \(.*\)/start.sh.*|\1|p' /etc/systemd/system/launcher.service | head -1)
fi
[ -z "$INSTALL_DIR" ] && INSTALL_DIR="/vol1/1000/fnos-launcher"

echo "===== 卸载飞牛电视启动器 ====="
echo "  安装目录 : $INSTALL_DIR"
echo
printf "确认卸载？[y/N] "
read -r ans
case "$ans" in
  y|Y|yes|YES) ;;
  *) echo "已取消"; exit 0;;
esac

for s in launcher.service launcher-web.service; do
  systemctl stop "$s"    >/dev/null 2>&1 || true
  systemctl disable "$s" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$s"
done
systemctl daemon-reload
echo "✓ 服务已停止并移除（重启后回到飞牛控制台登录界面）"

if [ -d "$INSTALL_DIR" ]; then
  rm -rf "$INSTALL_DIR"
  echo "✓ 已删除 $INSTALL_DIR"
fi

if [ -f /etc/asound.conf ] && grep -q 'hw:' /etc/asound.conf 2>/dev/null; then
  rm -f /etc/asound.conf
  echo "✓ 已删除 /etc/asound.conf（音频恢复默认设备）"
fi

echo
echo "完成。重启飞牛即可回到原来的登录界面。"
echo
echo "附：想连 xorg/chromium 一起删掉，执行："
echo "  sudo apt-get purge -y xorg xinit x11-xserver-utils x11-utils chromium alsa-utils"
echo "  sudo apt-get autoremove -y"
echo
