"""
web_ui.py
---------
Flask web UI for editing config.yaml.

Features:
  - Output format selector (JPEG / PNG / WebP / TIFF / Original)
  - Per-format quality sliders (JPEG quality, PNG compression, WebP quality)
  - Filename template editor with live preview
  - Max parallel workers slider
  - Input / output directory fields
  - Save → writes config.yaml; Run → triggers batch_process in background

Run:
    python web_ui.py
    → http://localhost:5000
"""

from __future__ import annotations
from pathlib import Path
import multiprocessing
import threading

from flask import Flask, render_template_string, request, jsonify, redirect, url_for

from config import load_config, save_config, validate_template, AppConfig
from template import preview_template
import processor as proc

app = Flask(__name__)
_run_lock = threading.Lock()
_run_log:  list[str] = []
_running:  bool      = False

# ---------------------------------------------------------------------------
# HTML template (single-file, no external assets needed)
# ---------------------------------------------------------------------------

PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Media Processor</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: system-ui, -apple-system, sans-serif;
    background: #0f1117; color: #e2e8f0;
    min-height: 100vh; padding: 2rem;
  }
  h1 { font-size: 1.5rem; font-weight: 700; margin-bottom: 1.5rem;
       color: #a78bfa; letter-spacing: -.5px; }
  h2 { font-size: 0.95rem; font-weight: 600; text-transform: uppercase;
       letter-spacing: .08em; color: #7c3aed; margin-bottom: .75rem; }
  .card {
    background: #1a1d27; border: 1px solid #2d2f3e;
    border-radius: 10px; padding: 1.25rem 1.5rem; margin-bottom: 1.25rem;
  }
  label { display: block; font-size: .85rem; color: #94a3b8; margin-bottom: .3rem; }
  input[type=text], select {
    width: 100%; padding: .5rem .75rem;
    background: #0f1117; border: 1px solid #2d2f3e; border-radius: 6px;
    color: #e2e8f0; font-size: .9rem;
  }
  input[type=range] { width: 100%; accent-color: #7c3aed; cursor: pointer; }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
  @media(max-width:640px){ .row { grid-template-columns: 1fr; } }
  .slider-row { display: flex; align-items: center; gap: .75rem; }
  .slider-row input[type=range] { flex: 1; }
  .slider-val {
    min-width: 2.5rem; text-align: right;
    font-size: .9rem; font-weight: 600; color: #a78bfa;
  }
  .preview-box {
    margin-top: .75rem; padding: .6rem .9rem;
    background: #0f1117; border: 1px solid #2d2f3e; border-radius: 6px;
    font-family: monospace; font-size: .85rem; color: #34d399;
    min-height: 2rem; word-break: break-all;
  }
  .warn { color: #f59e0b; font-size: .8rem; margin-top: .4rem; }
  .token-grid {
    display: flex; flex-wrap: wrap; gap: .4rem; margin-top: .5rem;
  }
  .token {
    background: #2d2f3e; border-radius: 4px; padding: .2rem .5rem;
    font-family: monospace; font-size: .78rem; color: #a78bfa; cursor: pointer;
    user-select: none; border: 1px solid #3d3f50;
  }
  .token:hover { background: #3d2e7c; }
  .format-tabs { display: flex; gap: .5rem; flex-wrap: wrap; margin-bottom: 1rem; }
  .fmt-btn {
    padding: .4rem 1rem; border-radius: 6px; border: 1px solid #2d2f3e;
    background: #0f1117; color: #94a3b8; cursor: pointer; font-size: .85rem;
    transition: all .15s;
  }
  .fmt-btn.active { background: #7c3aed; border-color: #7c3aed; color: #fff; font-weight: 600; }
  .quality-panels > div { display: none; }
  .quality-panels > div.visible { display: block; }
  .btn {
    padding: .55rem 1.4rem; border-radius: 7px; border: none;
    font-size: .9rem; font-weight: 600; cursor: pointer;
    transition: opacity .15s;
  }
  .btn:hover { opacity: .85; }
  .btn-primary { background: #7c3aed; color: #fff; }
  .btn-run { background: #059669; color: #fff; }
  .btn-row { display: flex; gap: .75rem; margin-top: .5rem; }
  #log {
    background: #0a0c14; border: 1px solid #1e2030; border-radius: 8px;
    padding: 1rem; font-family: monospace; font-size: .8rem; color: #94a3b8;
    max-height: 260px; overflow-y: auto; white-space: pre-wrap; margin-top: 1rem;
    display: none;
  }
  .status { font-size: .8rem; margin-top: .5rem; color: #94a3b8; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
         margin-right: 5px; }
  .dot.green { background: #22c55e; }
  .dot.amber { background: #f59e0b; }
</style>
</head>
<body>
<h1>⚡ Media Processor</h1>

<form id="cfg-form">

<!-- Paths -->
<div class="card">
  <h2>Paths</h2>
  <div class="row" style="margin-bottom:.75rem">
    <div>
      <label>Input directory</label>
      <input type="text" name="input_dir" value="{{ cfg.paths.input_dir }}">
    </div>
    <div>
      <label>Output directory</label>
      <input type="text" name="output_dir" value="{{ cfg.paths.output_dir }}">
    </div>
  </div>
</div>

<!-- Output format -->
<div class="card">
  <h2>Output Format</h2>
  <div class="format-tabs">
    {% for fmt in ['jpeg','png','webp','tiff','original'] %}
    <button type="button" class="fmt-btn {% if cfg.output.format == fmt %}active{% endif %}"
            data-fmt="{{ fmt }}" onclick="setFormat('{{ fmt }}')">
      {{ fmt.upper() }}
    </button>
    {% endfor %}
  </div>
  <input type="hidden" name="format" id="format-input" value="{{ cfg.output.format }}">

  <div class="quality-panels">

    <div id="panel-jpeg" class="{% if cfg.output.format == 'jpeg' %}visible{% endif %}">
      <label>JPEG Quality <span style="color:#7c3aed;font-size:.8rem">(1–100, 95 = visually lossless)</span></label>
      <div class="slider-row">
        <input type="range" name="jpeg_quality" min="1" max="100"
               value="{{ cfg.output.jpeg_quality }}"
               oninput="document.getElementById('jq-val').textContent=this.value">
        <span class="slider-val" id="jq-val">{{ cfg.output.jpeg_quality }}</span>
      </div>
    </div>

    <div id="panel-png" class="{% if cfg.output.format == 'png' %}visible{% endif %}">
      <label>PNG Compression <span style="color:#7c3aed;font-size:.8rem">(0=fast/large … 9=slow/small, always lossless)</span></label>
      <div class="slider-row">
        <input type="range" name="png_compression" min="0" max="9"
               value="{{ cfg.output.png_compression }}"
               oninput="document.getElementById('pc-val').textContent=this.value">
        <span class="slider-val" id="pc-val">{{ cfg.output.png_compression }}</span>
      </div>
    </div>

    <div id="panel-webp" class="{% if cfg.output.format == 'webp' %}visible{% endif %}">
      <label>WebP Quality <span style="color:#7c3aed;font-size:.8rem">(1–100)</span></label>
      <div class="slider-row">
        <input type="range" name="webp_quality" min="1" max="100"
               value="{{ cfg.output.webp_quality }}"
               oninput="document.getElementById('wq-val').textContent=this.value">
        <span class="slider-val" id="wq-val">{{ cfg.output.webp_quality }}</span>
      </div>
      <label style="margin-top:.6rem;display:flex;align-items:center;gap:.5rem;cursor:pointer">
        <input type="checkbox" name="webp_lossless" {% if cfg.output.webp_lossless %}checked{% endif %}>
        Lossless WebP (ignores quality slider)
      </label>
    </div>

    <div id="panel-tiff" class="{% if cfg.output.format == 'tiff' %}visible{% endif %}">
      <p style="font-size:.85rem;color:#94a3b8">TIFF uses LZW lossless compression — no quality setting needed.</p>
    </div>

    <div id="panel-original" class="{% if cfg.output.format == 'original' %}visible{% endif %}">
      <p style="font-size:.85rem;color:#94a3b8">Files are copied as-is — no conversion performed.</p>
    </div>

  </div>
</div>

<!-- Filename template -->
<div class="card">
  <h2>Filename Template</h2>
  <label>Template string</label>
  <input type="text" name="template" id="template-input"
         value="{{ cfg.filename.template }}"
         oninput="previewTemplate(this.value)">

  <div class="token-grid">
    {% for tok in ['year','month','day','hour','minute','second','make','model','filename','counter'] %}
    <span class="token" onclick="insertToken('{{ tok }}')">{{'{'}}{{ tok }}{{'}'}}</span>
    {% endfor %}
    <span class="token" style="opacity:.45" title="Coming soon">{city}</span>
    <span class="token" style="opacity:.45" title="Coming soon">{country}</span>
  </div>

  <div style="margin-top:.6rem">
    <label>Fallback value <span style="color:#7c3aed;font-size:.8rem">(used when a token has no data)</span></label>
    <input type="text" name="fallback" id="fallback-input"
           value="{{ cfg.filename.fallback }}"
           oninput="previewTemplate(document.getElementById('template-input').value)"
           style="width:180px">
  </div>

  <div style="margin-top:.75rem">
    <label style="color:#64748b;font-size:.8rem">PREVIEW (sample: Apple iPhone 15, 2026-03-21 16:01:06, IMG_0035)</label>
    <div class="preview-box" id="preview-box">{{ preview }}</div>
    <div class="warn" id="warn-box">{{ warnings }}</div>
  </div>
</div>

<!-- Processing -->
<div class="card">
  <h2>Processing</h2>
  <label>Parallel workers <span style="color:#7c3aed;font-size:.8rem">(0 = auto, max {{ max_workers }})</span></label>
  <div class="slider-row">
    <input type="range" name="workers" min="0" max="{{ max_workers }}"
           value="{{ cfg.processing.workers }}"
           oninput="document.getElementById('wk-val').textContent=this.value||'auto'">
    <span class="slider-val" id="wk-val">
      {{ cfg.processing.workers if cfg.processing.workers else 'auto' }}
    </span>
  </div>
</div>

<!-- Actions -->
<div class="card">
  <h2>Actions</h2>
  <div class="btn-row">
    <button type="button" class="btn btn-primary" onclick="saveConfig()">💾 Save config</button>
    <button type="button" class="btn btn-run"     onclick="runBatch()">▶ Run batch</button>
  </div>
  <div class="status" id="status"></div>
  <div id="log"></div>
</div>

</form>

<script>
// --- Format tabs ---
function setFormat(fmt) {
  document.querySelectorAll('.fmt-btn').forEach(b => b.classList.remove('active'));
  document.querySelector(`.fmt-btn[data-fmt="${fmt}"]`).classList.add('active');
  document.getElementById('format-input').value = fmt;
  document.querySelectorAll('.quality-panels > div').forEach(p => p.classList.remove('visible'));
  const panel = document.getElementById('panel-' + fmt);
  if (panel) panel.classList.add('visible');
}

// --- Template preview ---
async function previewTemplate(tmpl) {
  const fallback = document.getElementById('fallback-input').value || 'unknown';
  const resp = await fetch('/api/preview', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({template: tmpl, fallback})
  });
  const data = await resp.json();
  document.getElementById('preview-box').textContent = data.preview + '.ext';
  document.getElementById('warn-box').textContent    = data.warnings.join('  ');
}

// --- Insert token at cursor ---
function insertToken(tok) {
  const el  = document.getElementById('template-input');
  const pos = el.selectionStart;
  const val = el.value;
  el.value  = val.slice(0, pos) + '{' + tok + '}' + val.slice(pos);
  el.setSelectionRange(pos + tok.length + 2, pos + tok.length + 2);
  el.focus();
  previewTemplate(el.value);
}

// --- Collect form data ---
function collectForm() {
  const fd = new FormData(document.getElementById('cfg-form'));
  return {
    input_dir:       fd.get('input_dir'),
    output_dir:      fd.get('output_dir'),
    format:          fd.get('format'),
    jpeg_quality:    parseInt(fd.get('jpeg_quality') || 95),
    png_compression: parseInt(fd.get('png_compression') || 1),
    webp_quality:    parseInt(fd.get('webp_quality') || 90),
    webp_lossless:   fd.get('webp_lossless') === 'on',
    template:        fd.get('template'),
    fallback:        fd.get('fallback') || 'unknown',
    workers:         parseInt(fd.get('workers') || 0),
  };
}

// --- Save ---
async function saveConfig() {
  const resp = await fetch('/api/save', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(collectForm())
  });
  const data = await resp.json();
  setStatus(data.ok ? '✓ Config saved' : '✗ ' + data.error, data.ok);
}

// --- Run batch ---
async function runBatch() {
  await saveConfig();
  const logEl   = document.getElementById('log');
  const statusEl = document.getElementById('status');
  logEl.style.display = 'block';
  logEl.textContent   = 'Starting…\n';
  setStatus('⏳ Running…', true);

  const resp = await fetch('/api/run', { method: 'POST' });
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();

  while (true) {
    const {done, value} = await reader.read();
    if (done) break;
    logEl.textContent += decoder.decode(value);
    logEl.scrollTop = logEl.scrollHeight;
  }
  setStatus('✓ Done', true);
}

function setStatus(msg, ok) {
  const el = document.getElementById('status');
  el.innerHTML = `<span class="dot ${ok ? 'green' : 'amber'}"></span>${msg}`;
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    cfg      = load_config()
    preview  = preview_template(cfg.filename.template, cfg.filename.fallback) + ".ext"
    warnings = "  ".join(validate_template(cfg.filename.template))
    return render_template_string(
        PAGE,
        cfg=cfg,
        preview=preview,
        warnings=warnings,
        max_workers=multiprocessing.cpu_count(),
    )


@app.route("/api/preview", methods=["POST"])
def api_preview():
    body     = request.get_json()
    tmpl     = body.get("template", "")
    fallback = body.get("fallback", "unknown")
    preview  = preview_template(tmpl, fallback)
    warnings = validate_template(tmpl)
    return jsonify({"preview": preview, "warnings": warnings})


@app.route("/api/save", methods=["POST"])
def api_save():
    body = request.get_json()
    try:
        cfg = load_config()
        cfg.paths.input_dir        = body["input_dir"]
        cfg.paths.output_dir       = body["output_dir"]
        cfg.output.format          = body["format"]
        cfg.output.jpeg_quality    = int(body["jpeg_quality"])
        cfg.output.png_compression = int(body["png_compression"])
        cfg.output.webp_quality    = int(body["webp_quality"])
        cfg.output.webp_lossless   = bool(body["webp_lossless"])
        cfg.filename.template      = body["template"]
        cfg.filename.fallback      = body["fallback"]
        cfg.processing.workers     = int(body["workers"])
        save_config(cfg)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/run", methods=["POST"])
def api_run():
    """Stream batch processing results line by line to the browser."""
    import queue

    cfg = load_config()
    q: queue.Queue[str | None] = queue.Queue()

    def run():
        cfg.output_path.mkdir(parents=True, exist_ok=True)
        cfg.tmp_path.mkdir(parents=True, exist_ok=True)
        files = [f for f in cfg.input_path.iterdir() if f.is_file()]

        if not files:
            q.put("No files found.\n")
            q.put(None)
            return

        from concurrent.futures import ProcessPoolExecutor, as_completed
        work = [(f, cfg.output_path, cfg) for f in files]

        with ProcessPoolExecutor(max_workers=cfg.effective_workers) as pool:
            futures = {pool.submit(proc._worker, item): item[0] for item in work}
            for future in as_completed(futures):
                q.put(future.result() + "\n")
        q.put(None)

    t = threading.Thread(target=run, daemon=True)
    t.start()

    def generate():
        while True:
            line = q.get()
            if line is None:
                break
            yield line

    from flask import Response
    return Response(generate(), mimetype="text/plain")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
