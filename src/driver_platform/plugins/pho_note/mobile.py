"""PhoNote 手机版页面：随包发布的一页 HTML（不依赖外部静态资源）。

为什么放在 Python 模块里而不是单独的 .html：PyInstaller 打出的 sidecar 只收集 Python
模块，独立数据文件会在打包版里消失；而这一页一共十几 KB，内联更省心。
"""

from __future__ import annotations

MOBILE_HTML = r"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <title>PhoNote 手机版</title>
    <meta name="theme-color" content="#12141a" />
    <meta name="apple-mobile-web-app-capable" content="yes" />
    <meta name="apple-mobile-web-app-title" content="PhoNote" />
    <link rel="manifest" href="/notes/api/m/manifest.webmanifest" />
    <style>
      :root {
        color-scheme: dark;
        --bg: #12141a;
        --panel: #1a1d25;
        --line: #2a2f3a;
        --text: #e6e9f0;
        --dim: #9aa3b2;
        --accent: #7c86ff;
      }
      * { box-sizing: border-box; }
      html,
      body {
        width: 100%;
        max-width: 100vw;
        overflow-x: hidden;
        overflow-wrap: break-word;
      }
      .row > * {
        min-width: 0;
      }
      body {
        margin: 0;
        background: var(--bg);
        color: var(--text);
        font: 15px/1.5 system-ui, -apple-system, "Segoe UI", "Noto Sans SC", sans-serif;
        padding: env(safe-area-inset-top) 12px calc(24px + env(safe-area-inset-bottom));
      }
      h1 { font-size: 17px; margin: 14px 0 6px; }
      h2 { font-size: 14px; margin: 18px 0 8px; color: var(--dim); font-weight: 600; }
      .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
      .card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 10px; margin-top: 10px; }
      button {
        background: var(--accent); color: #fff; border: 0; border-radius: 10px;
        padding: 10px 14px; font-size: 15px; font-weight: 600;
      }
      button.ghost { background: transparent; border: 1px solid var(--line); color: var(--text); font-weight: 500; }
      button:disabled { opacity: .45; }
      input[type="text"], input[type="password"] {
        flex: 1 1 160px; min-width: 0; background: #0f1117; color: var(--text);
        border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; font-size: 15px;
      }
      input[type="file"] { display: none; }
      .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 8px; }
      .tile { position: relative; border: 2px solid transparent; border-radius: 10px; overflow: hidden; background: #0f1117; }
      .tile img { width: 100%; height: 96px; object-fit: cover; display: block; }
      .tile.on { border-color: var(--accent); }
      .tile .pick { position: absolute; inset: 0; }
      .note { border-top: 1px solid var(--line); padding: 10px 0; }
      .note:first-child { border-top: 0; }
      .note .name { font-weight: 600; }
      .note .meta { color: var(--dim); font-size: 12px; }
      .hint { color: var(--dim); font-size: 13px; }
      .status { margin-top: 12px; font-size: 13px; color: var(--dim); min-height: 20px; }
      .status.err { color: #ff8f8f; }
      .hidden { display: none; }
      pre { white-space: pre-wrap; word-break: break-word; font-size: 14px; }
    </style>
  </head>
  <body>
    <h1>PhoNote 手机版</h1>
    <p class="hint">安卓上只做「导入图片 → 挑图 → 组成笔记 / 查看笔记」。截图受系统限制，改用相册或系统截屏后再导入。</p>

    <div class="row">
      <button id="pick" class="ghost">从相册 / 相机导入</button>
      <button id="refresh" class="ghost">刷新</button>
      <button id="toggle-key" class="ghost">设置</button>
    </div>
    <input id="files" type="file" accept="image/*" multiple />

    <div id="key-box" class="card hidden">
      <label class="hint" for="key">访问钥匙（后端开了鉴权才需要）</label>
      <div class="row" style="margin-top: 6px">
        <input id="key" type="password" placeholder="X-API-Key" autocomplete="off" />
        <button id="save-key">保存</button>
      </div>
    </div>

    <h2>图片区（缓冲区） <span id="count" class="hint"></span></h2>
    <div id="buffer" class="grid"></div>
    <div class="card">
      <div class="row">
        <input id="title" type="text" placeholder="新笔记标题" />
        <button id="compose" disabled>组成笔记</button>
      </div>
    </div>

    <h2>笔记</h2>
    <div id="notes" class="card"></div>

    <div id="detail" class="card hidden">
      <div class="row">
        <strong id="detail-title" style="flex: 1"></strong>
        <button id="detail-close" class="ghost">关闭</button>
      </div>
      <div id="detail-images" class="grid" style="margin-top: 8px"></div>
      <pre id="detail-body"></pre>
    </div>

    <div id="status" class="status"></div>

    <script>
      const KEY_STORE = 'phonote.key';
      const state = { captures: [], selected: new Set(), tree: [], detail: null };

      const el = (id) => document.getElementById(id);
      const say = (text, error) => {
        el('status').textContent = text;
        el('status').className = error ? 'status err' : 'status';
      };
      const getKey = () => localStorage.getItem(KEY_STORE) || '';

      async function api(path, options = {}) {
        const headers = { ...(options.headers || {}) };
        if (getKey()) headers['X-API-Key'] = getKey();
        const response = await fetch('/notes/api' + path, { ...options, headers });
        if (!response.ok) {
          let detail = '';
          try {
            detail = (await response.text()).slice(0, 240);
          } catch {
            detail = '';
          }
          throw new Error(`${response.status} ${detail}`);
        }
        return response.status === 204 ? null : response.json();
      }

      function renderBuffer() {
        const box = el('buffer');
        box.innerHTML = '';
        if (!state.captures.length) {
          box.innerHTML = '<p class="hint" style="grid-column: 1/-1">还没有图片，点上面「从相册 / 相机导入」。</p>';
        }
        for (const capture of state.captures) {
          const tile = document.createElement('div');
          tile.className = 'tile' + (state.selected.has(capture.capture_id) ? ' on' : '');
          const img = document.createElement('img');
          img.src = `/notes/api/captures/${capture.capture_id}/image`;
          img.loading = 'lazy';
          img.alt = capture.media_type || 'capture';
          const hit = document.createElement('div');
          hit.className = 'pick';
          hit.addEventListener('click', () => {
            if (state.selected.has(capture.capture_id)) state.selected.delete(capture.capture_id);
            else state.selected.add(capture.capture_id);
            renderBuffer();
          });
          tile.append(img, hit);
          box.append(tile);
        }
        el('count').textContent = `剩 ${state.captures.length} 张，已选 ${state.selected.size}`;
        el('compose').disabled = state.selected.size === 0;
      }

      function renderNotes() {
        const box = el('notes');
        box.innerHTML = '';
        const notes = state.tree.filter((node) => node.kind === 'note');
        if (!notes.length) {
          box.innerHTML = '<p class="hint">还没有笔记。</p>';
          return;
        }
        for (const note of notes.slice(0, 50)) {
          const line = document.createElement('div');
          line.className = 'note';
          line.innerHTML = `<div class="name"></div><div class="meta"></div>`;
          line.querySelector('.name').textContent = note.name;
          line.querySelector('.meta').textContent = note.updated_at ? new Date(note.updated_at).toLocaleString() : '';
          line.addEventListener('click', () => openNote(note.id).catch(fail));
          box.append(line);
        }
      }

      async function openNote(noteId) {
        const note = await api(`/notes/${noteId}`);
        state.detail = note;
        el('detail').classList.remove('hidden');
        el('detail-title').textContent = note.name;
        el('detail-body').textContent = note.body_md || '';
        const images = el('detail-images');
        images.innerHTML = '';
        for (const image of note.images || []) {
          const img = document.createElement('img');
          img.src = `/notes/api/captures/${image.capture_id}/image`;
          img.style.cssText = 'width:100%;border-radius:10px;display:block';
          img.loading = 'lazy';
          images.append(img);
        }
        el('detail').scrollIntoView({ behavior: 'smooth', block: 'start' });
      }

      async function refresh() {
        state.captures = await api('/captures?limit=60&unused_only=true');
        const ids = new Set(state.captures.map((item) => item.capture_id));
        for (const id of [...state.selected]) {
          if (!ids.has(id)) state.selected.delete(id);
        }
        renderBuffer();
        state.tree = await api('/tree');
        renderNotes();
      }

      async function importFiles(fileList) {
        const files = [...fileList];
        if (!files.length) return;
        say(`正在导入 ${files.length} 张…`);
        let done = 0;
        for (const file of files) {
          try {
            const dataUrl = await readAsDataUrl(file);
            const base64 = String(dataUrl).split(',')[1] || '';
            await api('/captures', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                image_b64: base64,
                source: 'android',
                app_title: file.name || null,
              }),
            });
            done += 1;
            say(`已导入 ${done}/${files.length}`);
          } catch (error) {
            say(`第 ${done + 1} 张失败：${error.message}`, true);
            return;
          }
        }
        await refresh();
        say(`导入完成：${done} 张入库`);
      }

      function readAsDataUrl(file) {
        return new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result);
          reader.onerror = () => reject(reader.error || new Error('读取文件失败'));
          reader.readAsDataURL(file);
        });
      }

      async function compose() {
        const title = el('title').value.trim();
        if (!title) {
          say('先填一个标题', true);
          el('title').focus();
          return;
        }
        const note = await api('/notes', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            parent_id: null,
            capture_ids: [...state.selected],
            title,
            body_md: '',
          }),
        });
        el('title').value = '';
        state.selected.clear();
        await refresh();
        await openNote(note.id);
        say(`已建笔记：${note.name}`);
      }

      function fail(error) {
        say(String(error.message || error), true);
      }

      el('pick').addEventListener('click', () => el('files').click());
      el('files').addEventListener('change', (event) => {
        importFiles(event.target.files).catch(fail);
        event.target.value = '';
      });
      el('refresh').addEventListener('click', () => refresh().then(() => say('已刷新')).catch(fail));
      el('compose').addEventListener('click', () => compose().catch(fail));
      el('detail-close').addEventListener('click', () => el('detail').classList.add('hidden'));
      el('toggle-key').addEventListener('click', () => el('key-box').classList.toggle('hidden'));
      el('key').value = getKey();
      el('save-key').addEventListener('click', () => {
        localStorage.setItem(KEY_STORE, el('key').value.trim());
        el('key-box').classList.add('hidden');
        refresh().then(() => say('钥匙已保存')).catch(fail);
      });

      refresh().then(() => say('就绪')).catch(fail);
    </script>
  </body>
</html>
"""


def manifest(app_name: str = "PhoNote") -> dict:
    """给安卓「添加到主屏幕」用的最小 manifest。"""
    return {
        "name": app_name,
        "short_name": app_name,
        "start_url": "/notes/api/m",
        "scope": "/notes/api/",
        "display": "standalone",
        "background_color": "#12141a",
        "theme_color": "#12141a",
        "icons": [
            {
                "src": "/notes/api/m/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any",
            }
        ],
    }


ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"><rect width="256" height="256" rx="56" fill="#181a20"/><rect x="64" y="76" width="128" height="104" rx="14" fill="none" stroke="#96a0ff" stroke-width="8"/><circle cx="110" cy="110" r="14" fill="none" stroke="#96a0ff" stroke-width="6"/><path d="M78 168l44-42 30 30 18-16 16 28z" fill="#96a0ff"/></svg>"""
