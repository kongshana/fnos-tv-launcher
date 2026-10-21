#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Launcher Web — 启动器静态服务 + 同源网页代理

为什么要代理：
  大多数站点带 X-Frame-Options / CSP frame-ancestors，直接 iframe 会白屏，
  所以之前只能整页跳转，一跳走启动器的菜单栏就没了。
  这里把目标页的 HTML 抓回来、剥掉限制头、改写链接指向代理自身，
  于是 iframe 里加载的是"同源"内容 —— 顶部菜单栏与导航栏得以常驻，
  而且因为同源，启动器能完全控制焦点与前进/后退。

只代理 HTML 文档；图片/CSS/JS 由浏览器直连源站（静态资源不受 frame 限制）。
"""

import gzip
import html as html_lib
import ipaddress
import json
import mimetypes
import os
import re
import socket
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urljoin, quote, unquote, urlencode
import urllib.request
import urllib.error

PORT = int(os.environ.get('LAUNCHER_WEB_PORT', '8088'))
ROOT = os.environ.get('LAUNCHER_WEB_ROOT', '/vol1/1000/launcher-web')
TIMEOUT = 20

# 「我的文件」：直接浏览 NAS 数据盘，不依赖飞牛登录态
FILES_ROOT = os.environ.get('LAUNCHER_FILES_ROOT', '/vol1/1000')
MAX_ITEMS = 800

IMG_EXT = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.avif', '.svg', '.heic'}
VID_EXT = {'.mp4', '.mkv', '.webm', '.mov', '.m4v', '.avi', '.ts', '.m2ts', '.flv'}
AUD_EXT = {'.mp3', '.flac', '.wav', '.aac', '.m4a', '.ogg', '.opus', '.ape', '.wma'}
TXT_EXT = {'.txt', '.md', '.log', '.json', '.csv', '.ini', '.conf', '.cfg',
           '.srt', '.ass', '.nfo', '.xml', '.yml', '.yaml', '.py', '.sh', '.html', '.htm'}

UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36')

# 代理页里注入的脚本：把遥控器的返回/菜单键交给启动器处理（同源，因此可注入）
# 这段会被插到 <head> 最前面，尽量抢在站点自己的脚本之前挂上监听。
INJECT_JS = """
<script>
(function(){
  var KEYS = ['Escape','ContextMenu','GoBack','BrowserBack'];
  function isTyping(t){
    if(!t) return false;
    var n = t.tagName;
    return n === 'INPUT' || n === 'TEXTAREA' || n === 'SELECT' || t.isContentEditable;
  }
  function relay(k){ try { parent.postMessage({__launcherKey:k}, '*'); } catch(e){} }
  function onKey(e){
    var k = e.key;
    if (KEYS.indexOf(k) >= 0) {
      e.preventDefault(); e.stopPropagation(); relay(k); return;
    }
    // 方向键只在没有输入焦点时上报，避免抢走网页自身的键盘操作
    if (!isTyping(e.target) && k && k.indexOf('Arrow') === 0) {
      e.preventDefault(); e.stopPropagation(); relay(k);
    }
  }
  // window 上的捕获阶段最早拿到按键，站点在 document 上 stopPropagation 也拦不住
  window.addEventListener('keydown', onKey, true);
  document.addEventListener('keydown', onKey, true);

  // 双保险：新窗口一律在当前网页内打开。
  // 注意：绝对不要改写 window.open 成 _self —— 很多站点会无参调用 window.open 做探测，
  // 一改就会把当前页导航成 about:blank（必应就是这么变空白的）。
  // 这里只拦 <a target>，window.open 交给 iframe 的 sandbox 拦（没给 allow-popups）。
  try {
    document.addEventListener('click', function(e){
      var a = e.target && e.target.closest ? e.target.closest('a[target]') : null;
      if (a && (a.target === '_blank' || a.target === '_top' || a.target === '_parent')) {
        e.preventDefault();
        try { location.href = a.href; } catch(err){}
      }
    }, true);
  } catch(e){}

  // 表单提交：改成我们自己的代理地址。
  // 这里不能用「往表单里塞 hidden 字段」的办法 —— 站点脚本按字段顺序取值，
  // 多两个字段就会把它的参数全部错位（必应搜索直接变成乱码）。
  document.addEventListener('submit', function(e){
    var f = e.target;
    if (!f || f.tagName !== 'FORM') return;
    if (((f.getAttribute('method') || 'get') + '').toLowerCase() !== 'get') return;
    var base = f.getAttribute('data-pbase');
    if (!base) return;
    var parts = [], els = f.elements, i, el, t;
    for (i = 0; i < els.length; i++) {
      el = els[i];
      if (!el.name || el.disabled) continue;
      t = (el.type || '').toLowerCase();
      if (t === 'submit' || t === 'button' || t === 'file' || t === 'image' || t === 'reset') continue;
      if ((t === 'checkbox' || t === 'radio') && !el.checked) continue;
      parts.push(encodeURIComponent(el.name) + '=' + encodeURIComponent(el.value == null ? '' : el.value));
    }
    var sep = base.indexOf('?') >= 0 ? '&' : '?';
    var full = base + (parts.length ? sep + parts.join('&') : '');
    var org = '';
    try { org = (location.origin && location.origin !== 'null') ? location.origin : (parent.location.origin || ''); } catch(err){}
    if (!org) return;
    e.preventDefault();
    location.href = org + '/p?u=' + encodeURIComponent(full);
  }, true);

  // 让启动器的软键盘能把字直接打进网页里的输入框（电视上没有实体键盘）
  var lastInput = null;
  var lastTap = 0;
  function isField(t){
    return !!t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
  }
  document.addEventListener('focusin', function(e){
    if (isField(e.target)) {
      lastInput = e.target;
      // 用户刚点过输入框 -> 让启动器把软键盘弹出来（电视上没有实体键盘）
      if (Date.now() - lastTap < 1500) {
        try { parent.postMessage({__launcherKbNeed: 1}, '*'); } catch(err){}
      }
    }
  }, true);
  /* 注意：不能拿 document.activeElement 判断 —— 软键盘弹出后焦点会转到顶层文档，
     iframe 里的 activeElement 变成 body，会误判成“没有输入框”。
     这里改成：点到输入框就记住，点到网页其它地方才清掉。 */
  document.addEventListener('pointerdown', function(e){
    lastTap = Date.now();
    if (!isField(e.target)) lastInput = null;
  }, true);
  window.__launcherHasInput = function(){
    try { return isField(lastInput); } catch(e){ return false; }
  };
  window.__launcherType = function(txt){
    try {
      if (!window.__launcherHasInput()) return false;
      if (lastInput.isContentEditable) {
        lastInput.textContent += txt;
      } else {
        lastInput.value = (lastInput.value || '') + txt;
      }
      lastInput.dispatchEvent(new Event('input', {bubbles:true}));
      return true;
    } catch(e){ return false; }
  };
  window.__launcherEnter = function(){
    try {
      if (!window.__launcherHasInput()) return false;
      lastInput.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', keyCode:13, which:13, bubbles:true}));
      lastInput.dispatchEvent(new KeyboardEvent('keypress', {key:'Enter', keyCode:13, which:13, bubbles:true}));
      lastInput.dispatchEvent(new KeyboardEvent('keyup', {key:'Enter', keyCode:13, which:13, bubbles:true}));
      if (lastInput.form) { try { lastInput.form.requestSubmit(); } catch(e){ lastInput.form.submit(); } }
      return true;
    } catch(e){ return false; }
  };
  window.__launcherBack = function(){
    try {
      if (!window.__launcherHasInput()) return false;
      if (lastInput.isContentEditable) {
        lastInput.textContent = String(lastInput.textContent || '').slice(0, -1);
      } else {
        lastInput.value = String(lastInput.value || '').slice(0, -1);
      }
      lastInput.dispatchEvent(new Event('input', {bubbles:true}));
      return true;
    } catch(e){ return false; }
  };
  window.__launcherValue = function(){
    try {
      if (!isField(lastInput)) return null;
      return lastInput.isContentEditable ? String(lastInput.textContent || '')
                                         : String(lastInput.value || '');
    } catch(e){ return null; }
  };
  window.__launcherClear = function(){
    try {
      if (!window.__launcherHasInput()) return false;
      if (lastInput.isContentEditable) lastInput.textContent = '';
      else lastInput.value = '';
      lastInput.dispatchEvent(new Event('input', {bubbles:true}));
      return true;
    } catch(e){ return false; }
  };
})();
</script>
"""


def decode_body(raw, ctype):
    enc = None
    m = re.search(r'charset=([\w-]+)', ctype or '', re.I)
    if m:
        enc = m.group(1)
    if not enc:
        m = re.search(rb'<meta[^>]+charset=["\']?\s*([\w-]+)', raw[:8192], re.I)
        if m:
            enc = m.group(1).decode('ascii', 'ignore')
    for e in (enc, 'utf-8', 'gb18030', 'latin-1'):
        if not e:
            continue
        try:
            return raw.decode(e)
        except Exception:
            continue
    return raw.decode('utf-8', 'replace')


def attr_val(m):
    return m.group(2) or m.group(3) or m.group(4) or ''


ATTR_RE = r'(?i)\b%s\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))'
SKIP_PREFIX = ('#', 'javascript:', 'mailto:', 'tel:', 'data:', 'about:', 'blob:')


def proxify(v, base, origin=''):
    """把一个原始链接换成经过代理的地址。

    注意：必须带上本站的 origin（http://<ip>:<port>），不能只写 /p?u=。
    因为被代理的页面里有 <base href="https://源站/">，
    根相对路径 /p?u=... 会被解析成 https://源站/p?u=... → 404，
    表现就是 CSS/图片全部加载失败，页面只剩裸文字。
    """
    if not v:
        return None
    s = v.strip()
    if not s or s.lower().startswith(SKIP_PREFIX):
        return None
    if s.startswith('//'):
        s = (urlparse(base).scheme or 'https') + ':' + s
    abs_url = urljoin(base, s)
    if not abs_url.startswith(('http://', 'https://')):
        return None
    return (origin or '') + '/p?u=' + quote(abs_url, safe='')


def rewrite_html(doc, base, origin=''):
    esc = lambda s: html_lib.escape(s, quote=True)

    # 1) 表单：action 指向代理，原始目标放进 data-pbase（交给注入脚本用）。
    #    注意：绝对不要在表单里塞 hidden 字段 —— 站点自己的脚本是按字段顺序取值的，
    #    多两个字段就把它的参数全打乱（必应搜索会变成一堆乱码参数）。
    def form_sub(m):
        tag = m.group(0)
        am = re.search(ATTR_RE % 'action', tag)
        raw_act = attr_val(am) if am else ''
        target = urljoin(base, raw_act) if raw_act else base
        new_tag = tag
        if am:
            new_tag = re.sub(ATTR_RE % 'action', 'action="%s/p"' % (origin or ''), tag, count=1)
        else:
            new_tag = tag[:-1] + ' action="%s/p"' % (origin or '')
        # 原始目标写进自定义属性，注入脚本提交时用它拼真实地址
        if 'data-pbase' not in new_tag:
            new_tag = new_tag[:-1] + ' data-pbase="%s">' % esc(target)
        return new_tag

    doc = re.sub(r'(?i)<form\b[^>]*>', form_sub, doc)

    # 2) 普通链接
    def href_sub(m):
        v = attr_val(m)
        p = proxify(v, base, origin)
        if p is None:
            return m.group(0)
        q = m.group(2) is not None and '"' or "'"
        return 'href=%s%s%s' % (q, p, q)

    doc = re.sub(ATTR_RE % 'href', href_sub, doc)

    # 3) meta refresh 跳转也要走代理，否则会跳出去变跨域白屏
    def refresh_sub(m):
        inner = m.group(1)
        pm = re.search(r'(?i)url\s*=\s*([^;"]+)', inner)
        if not pm:
            return m.group(0)
        p = proxify(pm.group(1).strip().strip('\'"'), base, origin)
        if p is None:
            return m.group(0)
        return '<meta http-equiv="refresh" content="%s;url=%s">' % (
            inner.split(';')[0], p)

    doc = re.sub(r'(?i)<meta\s+http-equiv=["\']?refresh["\']?\s+content=["\']([^"\']+)["\'][^>]*>',
                 refresh_sub, doc)

    # 4) 去掉会阻止内嵌的 CSP meta
    doc = re.sub(r'(?i)<meta\s+http-equiv=["\']?Content-Security-Policy["\']?[^>]*>', '', doc)

    # 5) 注入 base + 遥控转发脚本：都放到 <head> 最前面，
    #    这样能抢在站点自己的脚本之前挂上按键监听（放 </head> 前面会被站点抢先）。
    base_tag = '<base href="%s">' % esc(base)
    if re.search(r'(?i)<head\b', doc):
        doc = re.sub(r'(?i)<head([^>]*)>',
                     lambda m: m.group(0) + base_tag + INJECT_JS, doc, count=1)
    else:
        doc = base_tag + INJECT_JS + doc

    return doc


# ---------------- 「我的文件」辅助 ----------------
def safe_join(sub):
    """把用户给的相对路径锁在 FILES_ROOT 里，越界返回 None"""
    sub = (sub or '').strip().lstrip('/')
    target = os.path.realpath(os.path.join(FILES_ROOT, sub))
    root = os.path.realpath(FILES_ROOT)
    if target != root and not target.startswith(root + os.sep):
        return None
    return target


def rel_of(abspath):
    root = os.path.realpath(FILES_ROOT)
    rel = os.path.relpath(abspath, root)
    return '' if rel == '.' else rel.replace(os.sep, '/')


def kind_of(name, isdir):
    if isdir:
        return 'dir'
    ext = os.path.splitext(name)[1].lower()
    if ext in IMG_EXT:
        return 'image'
    if ext in VID_EXT:
        return 'video'
    if ext in AUD_EXT:
        return 'audio'
    if ext in TXT_EXT:
        return 'text'
    return 'file'


def human_size(n):
    if n < 1024:
        return '%d B' % n
    units = ['KB', 'MB', 'GB', 'TB']
    v = n / 1024.0
    for u in units:
        if v < 1024 or u == 'TB':
            return ('%.0f %s' % (v, u)) if v >= 100 or u == 'B' else ('%.1f %s' % (v, u))
        v /= 1024.0
    return '%.1f TB' % v


def host_blocked(host):
    """禁止代理访问内网，避免变成 SSRF 跳板"""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except Exception:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local \
                or ip.is_reserved or ip.is_multicast:
            return True
    return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'LauncherWeb/1.0'

    def log_message(self, fmt, *args):
        pass

    # ---------- 通用 ----------
    def _send(self, code, body, ctype='text/html; charset=utf-8'):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        # 注意：不发 X-Frame-Options / CSP，否则我们自己就嵌不了
        self.end_headers()
        if getattr(self, '_head', False):
            return
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _send_json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   'application/json; charset=utf-8')

    def _send_file(self, path, dl=False):
        """支持 Range，视频拖动进度条与音频播放都靠它"""
        try:
            size = os.path.getsize(path)
            f = open(path, 'rb')
        except Exception:
            return self._err('读不到这个文件', 404)

        start, end, code = 0, size - 1, 200
        rng = self.headers.get('Range')
        if rng:
            m = re.match(r'bytes=(\d*)-(\d*)', rng.strip())
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                code = 206
        end = min(end, size - 1)
        if start > end:
            start = 0
        length = end - start + 1

        ctype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(length))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'no-store')
        if code == 206:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        disp = 'attachment' if dl else 'inline'
        self.send_header('Content-Disposition',
                         "%s; filename*=UTF-8''%s" % (disp, quote(os.path.basename(path))))
        self.end_headers()

        try:
            if getattr(self, '_head', False):
                return
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)
        except Exception:
            pass
        finally:
            try:
                f.close()
            except Exception:
                pass

    def _err(self, msg, code=400):
        self._send(code,
                   '<!doctype html><meta charset="utf-8">'
                   '<body style="background:#0b0f16;color:#c9d4e3;font:22px/1.8 system-ui;'
                   'display:flex;align-items:center;justify-content:center;height:100vh;margin:0">'
                   '<div style="text-align:center;max-width:70%%">'
                   '<div style="font-size:30px;margin-bottom:18px">打不开这个页面</div>'
                   '<div style="color:#8794a8;font-size:19px">%s</div>'
                   '<div style="margin-top:26px;color:#6b7788;font-size:17px">'
                   '按遥控器返回键，或用顶部工具条回到桌面</div></div></body>'
                   % html_lib.escape(msg))

    def _file(self, name):
        path = os.path.join(ROOT, name)
        try:
            with open(path, 'rb') as f:
                return f.read()
        except Exception:
            return None

    # ---------- 路由 ----------
    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_HEAD(self):
        u = urlparse(self.path)
        if u.path in ('/', '/index.html', '/nav.html', '/files', '/files.html',
                      '/api/list', '/raw'):
            self._head = True
            return self.route()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def route(self):
        u = urlparse(self.path)
        if u.path in ('/', '/index.html'):
            body = self._file('index.html')
            if body is None:
                return self._err('缺少 index.html', 500)
            return self._send(200, body)
        if u.path == '/nav.html':
            body = self._file('nav.html')
            if body is None:
                return self._err('缺少 nav.html', 500)
            return self._send(200, body)
        if u.path in ('/files', '/files.html'):
            body = self._file('files.html')
            if body is None:
                return self._err('缺少 files.html', 500)
            return self._send(200, body)
        if u.path == '/api/list':
            return self.api_list(u)
        if u.path == '/raw':
            return self.raw(u)
        if u.path == '/p':
            return self.proxy(u)
        self._err('没有这个地址', 404)

    # ---------- 「我的文件」 ----------
    def api_list(self, u):
        q = parse_qs(u.query)
        sub = (q.get('p') or [''])[0]
        target = safe_join(sub)
        if target is None:
            return self._send_json({'error': '不在允许的目录内', 'path': '', 'items': []})
        if not os.path.isdir(target):
            return self._send_json({'error': '不是文件夹', 'path': rel_of(target), 'items': []})

        root = os.path.realpath(FILES_ROOT)
        parent = None
        if target != root:
            up = os.path.dirname(target.rstrip(os.sep))
            parent = '' if os.path.realpath(up) == root else rel_of(up)

        entries = []
        try:
            names = os.listdir(target)
        except Exception as e:
            return self._send_json({'error': '打不开：%s' % e, 'path': rel_of(target), 'items': []})

        for n in names:
            if n.startswith('.'):
                continue
            full = os.path.join(target, n)
            try:
                st = os.stat(full)
            except Exception:
                continue
            isdir = os.path.isdir(full)
            entries.append({
                'name': n,
                'path': rel_of(full),
                'type': 'dir' if isdir else 'file',
                'kind': kind_of(n, isdir),
                'size': '' if isdir else human_size(st.st_size),
                'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime)),
                '_sort': (0 if isdir else 1, n.lower()),
            })

        entries.sort(key=lambda x: x['_sort'])
        truncated = len(entries) > MAX_ITEMS
        entries = entries[:MAX_ITEMS]
        for e in entries:
            e.pop('_sort', None)

        return self._send_json({
            'path': rel_of(target),
            'parent': parent,
            'truncated': truncated,
            'items': entries,
        })

    def raw(self, u):
        q = parse_qs(u.query)
        sub = (q.get('p') or [''])[0]
        dl = (q.get('dl') or [''])[0] in ('1', 'true', 'yes')
        target = safe_join(sub)
        if target is None or not os.path.isfile(target):
            return self._err('没有这个文件', 404)
        return self._send_file(target, dl=dl)

    # ---------- 代理 ----------
    def proxy(self, u):
        q = parse_qs(u.query)
        target = (q.get('u') or q.get('url') or [''])[0]

        # GET 表单提交：action 被改成了 /p，字段以 __p_url / __p_method 的形式带在查询串里，
        # 例如 /p?__p_url=https%3A%2F%2Fcn.bing.com%2Fsearch&__p_method=get&q=n
        # 这里要把它还原成「源站地址 + 表单字段」，否则必应一搜索就报“没有指定网址”。
        if not target and q.get('__p_url'):
            target = q['__p_url'][0]
            extra = []
            for k, vals in q.items():
                if k in ('__p_url', '__p_method', 'u', 'url'):
                    continue
                for v in vals:
                    extra.append((k, v))
            if extra:
                target += ('&' if '?' in target else '?') + urlencode(extra)

        if not target:
            # 兜底：站点用 form.submit() 直接提交时不会经过注入脚本，
            # 请求会落到 /p?字段...，这里从 Referer 里把源站找回来。
            ref = self.headers.get('Referer') or ''
            m = re.search(r'[?&]u=([^&]+)', ref)
            if m:
                base = unquote(m.group(1))
                extra = []
                for k, vals in q.items():
                    if k in ('u', 'url', '__p_url', '__p_method'):
                        continue
                    for v in vals:
                        extra.append((k, v))
                if extra:
                    base += ('&' if '?' in base else '?') + urlencode(extra)
                target = base

        if not target:
            return self._err('没有指定网址')

        body_data = None
        if self.command == 'POST':
            length = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(length) if length else b''
            form = parse_qs(raw.decode('utf-8', 'replace'))
            target = (form.get('__p_url') or [target])[0]
            method = (form.get('__p_method') or ['get'])[0].lower()
            rest = {k: v for k, v in form.items()
                    if k not in ('__p_url', '__p_method')}
            flat = []
            for k, vals in rest.items():
                for v in vals:
                    flat.append((k, v))
            if method == 'post':
                body_data = urlencode(flat).encode('utf-8')
            else:
                if flat:
                    target = target + ('&' if '?' in target else '?') + urlencode(flat)

        if not target.lower().startswith(('http://', 'https://')):
            target = 'https://' + target

        pu = urlparse(target)
        if not pu.hostname or host_blocked(pu.hostname):
            return self._err('这个地址不允许通过代理访问')

        # 本站的绝对地址：改写链接时必须用它，否则 <base> 会把它指到源站去
        origin = 'http://' + (self.headers.get('Host') or ('127.0.0.1:%d' % PORT))

        headers = {
            'User-Agent': UA,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,'
                      'image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
        }
        if body_data is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'

        req = urllib.request.Request(target, data=body_data, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=TIMEOUT)
            raw = resp.read()
            ctype = resp.headers.get('Content-Type', '')
            ce = (resp.headers.get('Content-Encoding') or '').lower()
            final_url = resp.geturl()
            resp.close()
        except urllib.error.HTTPError as e:
            return self._err('网站返回 %s' % e.code)
        except Exception as e:
            return self._err('请求失败：%s' % e)

        if 'gzip' in ce:
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        elif 'deflate' in ce:
            try:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except Exception:
                try:
                    raw = zlib.decompress(raw)
                except Exception:
                    pass

        ctype_l = ctype.lower()

        # 样式表里的 url(/rp/x.png) 会按本站地址解析而 404，转成源站绝对地址
        if 'text/css' in ctype_l:
            try:
                css = raw.decode('utf-8', 'replace')
                css = re.sub(
                    r'url\(\s*([\'\"]?)(/[^\'\"\)]+)\1\s*\)',
                    lambda m: 'url(%s%s%s)' % (m.group(1), urljoin(final_url, m.group(2)), m.group(1)),
                    css)
                raw = css.encode('utf-8')
            except Exception:
                pass

        if any(t in ctype_l for t in ('text/html', 'application/xhtml', 'text/plain')) \
                or (not ctype_l.strip()):
            doc = decode_body(raw, ctype)
            doc = rewrite_html(doc, final_url, origin)
            return self._send(200, doc.encode('utf-8'), 'text/html; charset=utf-8')

        # 非文档：原样转发
        self.send_response(200)
        self.send_header('Content-Type', ctype or 'application/octet-stream')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(raw)
        except Exception:
            pass


def main():
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == '__main__':
    main()
