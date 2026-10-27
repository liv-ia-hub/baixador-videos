#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Baixador de Vídeos (versão web).
Cada navegador recebe uma sessão própria (cookie) e só vê os próprios downloads.
Os downloads entram numa fila única e rodam um de cada vez.
Os arquivos ficam no servidor por algumas horas e depois são apagados.
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import zipfile
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, quote

PORT = int(os.environ.get("PORT", "8765"))
DATA = os.environ.get("DATA_DIR", "/tmp/baixador")
KEEP_HOURS = float(os.environ.get("KEEP_HOURS", "6"))
MAX_LINKS = int(os.environ.get("MAX_LINKS", "300"))
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
ID_RE = re.compile(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)")
PCT_RE = re.compile(r"\[download\]\s+([\d.]+)%")
DIM_RE = re.compile(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b")
CODEC_RE = re.compile(r"Video:\s*(\w+)")
DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):([\d.]+)")
TARGET_W, TARGET_H = 1080, 1920
VF = ("scale=%d:%d:force_original_aspect_ratio=decrease:flags=lanczos,"
      "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1" % (TARGET_W, TARGET_H, TARGET_W, TARGET_H))

SECRET_COOKIES = os.environ.get("COOKIES_FILE", "/etc/secrets/cookies.txt")
WORK_COOKIES = os.path.join(DATA, "cookies.txt")
IG = {"state": "missing"}   # missing | ok | expired

YTDLP = shutil.which("yt-dlp")
FFMPEG = shutil.which("ffmpeg")

SESSIONS = {}          # sid -> {"job": job|None, "seen": ts}
QUEUE = deque()        # jobs esperando
CURRENT = [None]       # job rodando agora
LOCK = threading.Lock()
WAKE = threading.Event()


def now():
    return time.time()


def safe_name(name):
    name = re.sub(r"[/:\\\x00]", "-", name).strip().strip(".")
    return name[:80] or "Sem categoria"


def parse_links(text):
    cat, items, seen = "Sem categoria", [], set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("##"):
            name = line.lstrip("#").strip()
            if name:
                cat = safe_name(name)
            continue
        if line.startswith("#"):
            continue
        for u in URL_RE.findall(line):
            u = u.rstrip(".,;)]")
            if (cat, u) in seen:
                continue
            seen.add((cat, u))
            m = ID_RE.search(u)
            items.append({"url": u, "cat": cat, "vid": m.group(1) if m else ""})
    return items


def summarize(items):
    order, counts = [], {}
    for it in items:
        if it["cat"] not in counts:
            order.append(it["cat"])
            counts[it["cat"]] = 0
        counts[it["cat"]] += 1
    return {"count": len(items), "cats": [{"name": c, "n": counts[c]} for c in order]}


def is_login_error(err):
    e = (err or "").lower()
    return ("empty media response" in e or "login" in e or "rate-limit" in e
            or "rate limit" in e or "429" in e or "checkpoint" in e)


def prepare_cookies():
    """Copia o arquivo secreto (somente leitura) para uma cópia que o yt-dlp pode atualizar."""
    try:
        if os.path.isfile(SECRET_COOKIES) and os.path.getsize(SECRET_COOKIES) > 0:
            if (not os.path.isfile(WORK_COOKIES)
                    or os.path.getmtime(SECRET_COOKIES) > os.path.getmtime(WORK_COOKIES) + 1):
                shutil.copyfile(SECRET_COOKIES, WORK_COOKIES)
            if IG["state"] == "missing":
                IG["state"] = "ok"
            return WORK_COOKIES
    except OSError:
        pass
    IG["state"] = "missing"
    return None


def friendly_error(err):
    e = (err or "").lower()
    if not e:
        return "Falhou (veja o registro técnico)"
    if is_login_error(e):
        if IG["state"] in ("ok", "expired"):
            return "O login do Instagram do sistema expirou. Avise o responsável para renovar."
        return "O Instagram bloqueou este vídeo (pediu login). Falta configurar o login do sistema."
    if "unsupported url" in e:
        return "Link não suportado"
    if "not available" in e or "unavailable" in e or "404" in e or "removed" in e or "private" in e:
        return "Vídeo indisponível, privado ou removido"
    if "no video" in e:
        return "Esse post não tem vídeo (só foto)"
    return re.sub(r"^error:\s*", "", err.strip(), flags=re.I)[:160]


def video_files(folder):
    try:
        return {os.path.join(folder, f) for f in os.listdir(folder)
                if os.path.splitext(f)[1].lower() in VIDEO_EXT and ".part" not in f
                and ".convertendo" not in f}
    except OSError:
        return set()


def jlog(job, msg):
    job["log"].append(time.strftime("%H:%M:%S  ") + msg)


# ------------------------------------------------------------------ conversão
def convert_one(job, src, dstdir, it):
    os.makedirs(dstdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(src))[0]
    dst = os.path.join(dstdir, base + ".mp4")
    if os.path.exists(dst):
        return dst
    info = subprocess.run([FFMPEG, "-hide_banner", "-i", src], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, errors="replace").stderr
    dm, du, cm = DIM_RE.search(info), DUR_RE.search(info), CODEC_RE.search(info)
    w, h = (int(dm.group(1)), int(dm.group(2))) if dm else (0, 0)
    codec = cm.group(1).lower() if cm else ""
    dur = (int(du.group(1)) * 3600 + int(du.group(2)) * 60 + float(du.group(3))) if du else 0
    if (w, h) == (TARGET_W, TARGET_H) and codec == "h264" and src.lower().endswith(".mp4"):
        shutil.copy2(src, dst)
        return dst
    tmp = os.path.join(dstdir, base + ".convertendo.mp4")
    # 1 thread: o plano grátis tem 512 MB de memória; sem limite o ffmpeg abre uma thread
    # por processador da máquina e estoura a memória convertendo 1080x1920
    cmd = [FFMPEG, "-nostdin", "-y", "-v", "error", "-threads", "1", "-filter_threads", "1",
           "-i", src, "-vf", VF,
           "-c:v", "libx264", "-threads", "1", "-x264-params", "rc-lookahead=10:sync-lookahead=0",
           "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
           "-progress", "pipe:1", "-nostats", tmp]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True, errors="replace")
    job["proc"] = p
    for line in p.stdout:
        if line.startswith("out_time=") and dur > 0:
            try:
                hh, mm, ss = line.strip().split("=", 1)[1].split(":")
                it["progress"] = max(0.0, min(99.0, (int(hh) * 3600 + int(mm) * 60 + float(ss)) / dur * 100))
            except ValueError:
                pass
    err = p.stderr.read()
    rc = p.wait()
    job["proc"] = None
    if rc != 0 or job["stop"]:
        if os.path.exists(tmp):
            os.remove(tmp)
        if job["stop"]:
            raise InterruptedError()
        raise RuntimeError(err.strip()[-200:] or "erro no ffmpeg")
    os.replace(tmp, dst)
    jlog(job, "Convertido: %s (%dx%d %s → %dx%d h264)" % (os.path.basename(dst), w, h, codec, TARGET_W, TARGET_H))
    return dst


# ------------------------------------------------------------------ trabalho
def run_job(job):
    root = job["root"]
    raw_root = os.path.join(root, "originais")
    conv_root = os.path.join(root, "1080x1920")
    items = job["items"]
    jlog(job, "Início: %d vídeos" % len(items))
    for i, it in enumerate(items):
        if job["stop"]:
            break
        it["status"], it["progress"] = "baixando", 0.0
        catdir = os.path.join(raw_root, it["cat"])
        os.makedirs(catdir, exist_ok=True)
        before = video_files(catdir)
        cmd = [YTDLP, "--newline", "-o", os.path.join(catdir, "%(id)s.%(ext)s"),
               "--ffmpeg-location", FFMPEG, "--merge-output-format", "mp4",
               "-S", "res,vcodec:h264,acodec:aac"]
        ck = prepare_cookies()
        if ck:
            cmd += ["--cookies", ck]
        cmd.append(it["url"])
        jlog(job, "▶ [%d/%d] %s" % (i + 1, len(items), it["url"]))
        err = ""
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True, errors="replace", bufsize=1)
        job["proc"] = p
        for line in p.stdout:
            line = line.rstrip()
            if not line:
                continue
            m = PCT_RE.search(line)
            if m and "Destination" not in line:
                it["progress"] = float(m.group(1))
                if not line.strip().startswith("[download] 100"):
                    continue
            if line.startswith("ERROR"):
                err = line
            jlog(job, line[:300])
        rc = p.wait()
        job["proc"] = None
        if job["stop"]:
            it["status"] = "cancelado"
            break
        if rc != 0:
            if ck and is_login_error(err) and "instagram" in it["url"].lower():
                IG["state"] = "expired"
            it["status"], it["msg"] = "falhou", friendly_error(err)
            continue
        if ck and "instagram" in it["url"].lower():
            IG["state"] = "ok"
        files = sorted(video_files(catdir) - before)
        if not files and it["vid"]:
            files = sorted(f for f in video_files(catdir) if os.path.basename(f).startswith(it["vid"]))
        if not files:
            it["status"], it["msg"] = "falhou", "Nenhum vídeo encontrado nesse link"
            continue
        it["progress"] = 100.0
        final = files
        if job["convert"]:
            it["status"], it["progress"] = "convertendo", 0.0
            try:
                final = [convert_one(job, f, os.path.join(conv_root, it["cat"]), it) for f in files]
            except InterruptedError:
                it["status"] = "cancelado"
                break
            except Exception as e:  # noqa
                it["msg"] = "Baixou, mas a conversão falhou (o original está no .zip): %s" % str(e)[:100]
                final = files
        it["files"] = [os.path.relpath(f, root) for f in final]
        it["progress"] = 100.0
        it["status"] = "ok"
        if i < len(items) - 1:
            end = now() + float(job["sleep"])
            while now() < end and not job["stop"]:
                time.sleep(0.2)
    for it in items:
        if it["status"] in ("aguardando", "baixando", "convertendo"):
            it["status"] = "cancelado"
    ok = sum(1 for x in items if x["status"] == "ok")
    jlog(job, "Fim: %d de %d prontos." % (ok, len(items)))


def worker():
    while True:
        job = None
        with LOCK:
            if QUEUE:
                job = QUEUE.popleft()
                CURRENT[0] = job
                job["state"] = "running"
                job["started"] = now()
        if not job:
            WAKE.wait(5)
            WAKE.clear()
            continue
        try:
            run_job(job)
        except Exception as e:  # noqa
            jlog(job, "Erro inesperado: %s" % e)
        finally:
            with LOCK:
                job["state"] = "stopped" if job["stop"] else "done"
                job["finished"] = now()
                CURRENT[0] = None


def janitor():
    while True:
        time.sleep(600)
        limit = now() - KEEP_HOURS * 3600
        with LOCK:
            for sid, s in list(SESSIONS.items()):
                job = s.get("job")
                if job and job["state"] in ("done", "stopped") and job["finished"] < limit:
                    shutil.rmtree(job["root"], ignore_errors=True)
                    s["job"] = None
                if not s.get("job") and s["seen"] < now() - 7 * 86400:
                    SESSIONS.pop(sid, None)


def stop_job(job):
    job["stop"] = True
    with LOCK:
        if job in QUEUE:
            QUEUE.remove(job)
            job["state"] = "stopped"
            job["finished"] = now()
            for it in job["items"]:
                it["status"] = "cancelado"
    p = job.get("proc")
    if p and p.poll() is None:
        try:
            p.terminate()
        except Exception:
            pass


def build_zip(job):
    ready = [it for it in job["items"] if it["status"] == "ok" and it.get("files")]
    if not ready:
        return None
    zdir = os.path.join(job["root"], "_zip")
    os.makedirs(zdir, exist_ok=True)
    key = "%d-%d" % (len(ready), int(job.get("finished") or 0))
    path = os.path.join(zdir, "videos-%s.zip" % key)
    if os.path.exists(path):
        return path
    for f in os.listdir(zdir):
        os.remove(os.path.join(zdir, f))
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        used = set()
        for it in ready:
            for rel in it["files"]:
                src = os.path.join(job["root"], rel)
                if not os.path.isfile(src):
                    continue
                arc = "%s/%s" % (it["cat"], os.path.basename(src))
                if arc in used:
                    continue
                used.add(arc)
                z.write(src, arc)
    os.replace(tmp, path)
    return path


# ------------------------------------------------------------------ servidor
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # sessão por navegador
    def session(self):
        sid = None
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "bx_sid" and re.fullmatch(r"[A-Za-z0-9_-]{20,64}", v or ""):
                sid = v
        self._new_cookie = None
        with LOCK:
            if not sid or sid not in SESSIONS:
                if not sid:
                    sid = secrets.token_urlsafe(24)
                    self._new_cookie = sid
                SESSIONS.setdefault(sid, {"job": None, "seen": now()})
            SESSIONS[sid]["seen"] = now()
            return sid, SESSIONS[sid]

    def send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.cookie_header()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def cookie_header(self):
        if getattr(self, "_new_cookie", None):
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            self.send_header("Set-Cookie", "bx_sid=%s; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax%s"
                             % (self._new_cookie, secure))

    def send_file(self, path, name, ctype):
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % quote(name))
        self.cookie_header()
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1024 * 256)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_HEAD(self):
        self.send(200, b"", "text/plain")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/healthz":
            return self.send(200, b"ok", "text/plain")
        sid, s = self.session()
        if u.path == "/":
            return self.send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if u.path == "/api/status":
            job = s.get("job")
            prepare_cookies()
            out = {"job": None, "tools_ok": bool(YTDLP and FFMPEG), "ig": IG["state"]}
            if job:
                with LOCK:
                    pos = list(QUEUE).index(job) + 1 if job in QUEUE else 0
                    busy = CURRENT[0] is not None and CURRENT[0] is not job
                out["job"] = {
                    "id": job["id"], "state": job["state"], "position": pos, "others_running": busy,
                    "convert": job["convert"], "created": job["created"], "finished": job.get("finished") or 0,
                    "items": [{k: it.get(k, "") for k in ("url", "cat", "vid", "status", "progress", "msg")}
                              | {"has_file": bool(it.get("files"))} for it in job["items"]],
                    "log": list(job["log"])[-200:],
                }
            return self.send(200, out)
        if u.path == "/api/zip":
            job = s.get("job")
            if not job:
                return self.send(404, {"error": "Nada para baixar."})
            path = build_zip(job)
            if not path:
                return self.send(404, {"error": "Nenhum vídeo pronto ainda."})
            return self.send_file(path, "videos-%s.zip" % time.strftime("%Y%m%d-%H%M", time.localtime(job["created"])),
                                  "application/zip")
        if u.path == "/api/file":
            job = s.get("job")
            q = parse_qs(u.query)
            try:
                it = job["items"][int(q.get("i", ["-1"])[0])]
                rel = it["files"][0]
            except (TypeError, ValueError, IndexError, KeyError):
                return self.send(404, {"error": "Arquivo não encontrado."})
            path = os.path.realpath(os.path.join(job["root"], rel))
            if not path.startswith(os.path.realpath(job["root"]) + os.sep) or not os.path.isfile(path):
                return self.send(404, {"error": "Arquivo não encontrado."})
            return self.send_file(path, os.path.basename(path), "video/mp4")
        self.send(404, {"error": "não encontrado"})

    def do_POST(self):
        sid, s = self.session()
        d = self.body()
        if self.path == "/api/parse":
            return self.send(200, summarize(parse_links(d.get("text", ""))))

        if self.path == "/api/start":
            if not (YTDLP and FFMPEG):
                return self.send(500, {"error": "Servidor sem yt-dlp ou ffmpeg."})
            old = s.get("job")
            if old and old["state"] in ("queued", "running"):
                return self.send(409, {"error": "Você já tem um download em andamento."})
            items = parse_links(d.get("text", ""))
            if not items:
                return self.send(400, {"error": "Nenhum link encontrado."})
            if len(items) > MAX_LINKS:
                return self.send(400, {"error": "Máximo de %d links por vez." % MAX_LINKS})
            for it in items:
                it.update({"status": "aguardando", "progress": 0.0, "msg": "", "files": []})
            if old:
                shutil.rmtree(old["root"], ignore_errors=True)
            jid = secrets.token_hex(6)
            try:
                pause = max(0.0, min(60.0, float(d.get("sleep") or 0)))
            except ValueError:
                pause = 3.0
            job = {"id": jid, "sid": sid, "items": items, "state": "queued", "stop": False,
                   "root": os.path.join(DATA, sid, jid), "created": now(), "finished": 0,
                   "convert": bool(d.get("convert")), "sleep": pause,
                   "log": deque(maxlen=400), "proc": None}
            os.makedirs(job["root"], exist_ok=True)
            with LOCK:
                s["job"] = job
                QUEUE.append(job)
            WAKE.set()
            return self.send(200, {"ok": True})

        if self.path == "/api/stop":
            job = s.get("job")
            if job and job["state"] in ("queued", "running"):
                stop_job(job)
            return self.send(200, {"ok": True})

        if self.path == "/api/clear":
            job = s.get("job")
            if job and job["state"] in ("queued", "running"):
                return self.send(409, {"error": "Espere terminar ou clique em Parar antes de limpar."})
            if job:
                shutil.rmtree(job["root"], ignore_errors=True)
            s["job"] = None
            return self.send(200, {"ok": True})

        self.send(404, {"error": "não encontrado"})


PAGE = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Baixador de Vídeos</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%23c2410c'/%3E%3Cpath d='M16 8v11M11 14l5 5 5-5M10 24h12' stroke='white' stroke-width='2.6' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#f5f4f1; --card:#ffffff; --ink:#1d1c1a; --muted:#6f6b64; --line:#e4e1db;
  --accent:#c2410c; --accent-ink:#fff; --accent-soft:#fdeee6;
  --ok:#15803d; --ok-soft:#e5f5ea; --bad:#b91c1c; --bad-soft:#fbe9e9;
  --info:#1d4ed8; --info-soft:#e7eefc; --conv:#7c3aed; --conv-soft:#f0e9fd;
  --wait:#8a857c; --wait-soft:#efede9; --field:#fbfaf8;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#161513; --card:#201f1c; --ink:#ecebe7; --muted:#a19c93; --line:#34322e;
    --accent:#f0713a; --accent-ink:#1a0f08; --accent-soft:#3a2418;
    --ok:#4ade80; --ok-soft:#173222; --bad:#f87171; --bad-soft:#3b1d1d;
    --info:#7aa2ff; --info-soft:#1d2a47; --conv:#b794f6; --conv-soft:#2c2142;
    --wait:#8f8a82; --wait-soft:#2a2925; --field:#1a1917;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:22px 16px 60px}
header{display:flex;flex-wrap:wrap;gap:14px;align-items:center;justify-content:space-between;margin-bottom:18px}
.brand{display:flex;gap:12px;align-items:center}
.logo{width:38px;height:38px;border-radius:11px;background:var(--accent);display:grid;place-items:center;color:var(--accent-ink);flex:none}
h1{font-size:20px;margin:0;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px}
.grid{display:grid;grid-template-columns:1.5fr 1fr;gap:16px}
@media (max-width:860px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;min-width:0}
.card h2{font-size:14px;margin:0 0 10px;display:flex;align-items:center;justify-content:space-between;gap:8px}
.pill{font-size:12px;font-weight:600;background:var(--accent-soft);color:var(--accent);border-radius:999px;padding:2px 9px}
textarea{width:100%;min-height:290px;resize:vertical;border:1px solid var(--line);background:var(--field);color:var(--ink);border-radius:10px;padding:11px;font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
textarea:focus,input:focus{outline:2px solid var(--accent-soft);border-color:var(--accent)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.hint{font-size:12.5px;color:var(--muted);margin-top:8px}
.hint code{background:var(--wait-soft);padding:1px 5px;border-radius:5px;font-size:12px}
.cats{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.cat{font-size:12px;border:1px solid var(--line);border-radius:7px;padding:2px 8px;color:var(--muted)}
.cat b{color:var(--ink);font-weight:600}
label.f{display:block;font-size:13px;font-weight:600;margin:14px 0 5px}
input[type=number]{width:100%;border:1px solid var(--line);background:var(--field);color:var(--ink);border-radius:9px;padding:8px 10px;font:inherit}
.check{display:flex;gap:10px;align-items:flex-start;padding:11px;border:1px solid var(--line);border-radius:10px;background:var(--field);cursor:pointer}
.check input{margin-top:3px;accent-color:var(--accent);width:16px;height:16px;flex:none}
.check small{display:block;color:var(--muted);font-weight:400}
.btn{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:10px;padding:8px 13px;font:inherit;font-weight:600;font-size:13.5px;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:6px}
.btn:hover{border-color:var(--muted)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);padding:11px 18px;font-size:15px;justify-content:center}
.btn.primary:hover{filter:brightness(1.07)}
.btn.danger{color:var(--bad);border-color:var(--bad)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.actions{display:flex;gap:8px;margin-top:16px}
.actions .primary{flex:1}
.err{color:var(--bad);font-size:13px;margin-top:8px;min-height:1em}
.note{font-size:12.5px;color:var(--muted);background:var(--wait-soft);border-radius:9px;padding:9px 11px;margin-top:14px}
#prog,#hist{margin-top:16px}
.sumline{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:baseline;font-size:13.5px;color:var(--muted)}
.sumline .big{font-size:22px;font-weight:700;color:var(--ink);letter-spacing:-.02em}
.bar{height:8px;background:var(--wait-soft);border-radius:99px;overflow:hidden;margin:10px 0 14px}
.bar i{display:block;height:100%;background:var(--accent);width:0;transition:width .4s}
.tablewrap{max-height:460px;overflow:auto;border:1px solid var(--line);border-radius:10px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{position:sticky;top:0;background:var(--card);text-align:left;font-size:12px;color:var(--muted);font-weight:600;padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
td.n{color:var(--muted);width:36px}
td.code a{color:var(--ink);font-family:ui-monospace,Menlo,monospace;font-size:12px;text-decoration:none;word-break:break-all}
td.code a:hover{color:var(--accent)}
td.st{width:170px}
td.dl{width:80px;text-align:right}
td.dl a{color:var(--accent);font-weight:600;font-size:12.5px;text-decoration:none}
.badge{display:inline-flex;gap:6px;align-items:center;font-size:12px;font-weight:600;border-radius:999px;padding:2px 9px;white-space:nowrap}
.b-aguardando,.b-cancelado{background:var(--wait-soft);color:var(--wait)}
.b-baixando{background:var(--info-soft);color:var(--info)}
.b-convertendo{background:var(--conv-soft);color:var(--conv)}
.b-ok{background:var(--ok-soft);color:var(--ok)}
.b-falhou{background:var(--bad-soft);color:var(--bad)}
.minibar{height:4px;background:var(--wait-soft);border-radius:9px;margin-top:5px;overflow:hidden;width:110px}
.minibar i{display:block;height:100%;background:currentColor}
.why{color:var(--bad);font-size:12px;margin-top:3px}
details{margin-top:12px}
summary{cursor:pointer;font-size:13px;color:var(--muted)}
pre{background:var(--field);border:1px solid var(--line);border-radius:10px;padding:10px;max-height:260px;overflow:auto;font-size:11.5px;white-space:pre-wrap;word-break:break-all;margin:8px 0 0}
.empty{color:var(--muted);font-size:13.5px;text-align:center;padding:26px 10px}
.queue{background:var(--info-soft);color:var(--info);border-radius:10px;padding:10px 12px;font-size:13.5px;margin-bottom:12px}
.hrow{display:flex;gap:10px;align-items:center;justify-content:space-between;padding:9px 0;border-bottom:1px solid var(--line);font-size:13.5px;flex-wrap:wrap}
.hrow:last-child{border-bottom:0}
.hrow .m{color:var(--muted);font-size:12.5px}
.banner{border-radius:12px;padding:12px 14px;font-size:14px;margin-bottom:16px;border:1px solid}
.banner.bad{background:var(--bad-soft);color:var(--bad);border-color:var(--bad)}
.banner.info{background:var(--wait-soft);color:var(--muted);border-color:var(--line)}
.hidden{display:none!important}
.toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:9px 16px;border-radius:10px;font-size:13.5px;opacity:0;transition:opacity .2s;pointer-events:none;max-width:90vw}
.toast.show{opacity:1}
@media (max-width:600px){ td.st{width:auto} .tablewrap th:nth-child(2), .tablewrap td:nth-child(2){display:none} }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="logo"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/></svg></div>
      <div><h1>Baixador de Vídeos</h1><div class="sub">Cole os links, baixe tudo num .zip separado por pastas</div></div>
    </div>
  </header>

  <div class="banner hidden" id="igBanner"></div>

  <div class="grid">
    <div class="card">
      <h2>Links <span class="pill" id="count">0 vídeos</span></h2>
      <textarea id="text" spellcheck="false" placeholder="Cole os links aqui, um por linha.

Para separar em pastas, escreva o nome da pasta com ## antes:

## Rigolim - Post
https://www.instagram.com/reel/AAAA/
https://www.instagram.com/reel/BBBB/

## Leticia - Seguidores
https://www.instagram.com/reel/CCCC/"></textarea>
      <div class="row" style="margin-top:8px">
        <button class="btn" id="loadBtn">Carregar arquivo .txt</button>
        <button class="btn" id="clearBtn">Limpar</button>
        <input type="file" id="file" accept=".txt,text/plain" class="hidden">
      </div>
      <div class="cats" id="cats"></div>
      <div class="hint">Linha com <code>## Nome</code> cria uma pasta no .zip. Linhas que começam com <code>#</code> são ignoradas.</div>
    </div>

    <div class="card">
      <h2>Opções</h2>
      <label class="check">
        <input type="checkbox" id="convert" checked>
        <span>Converter para 1080x1920 (H.264)
          <small>Aumenta os vídeos menores e converte para o formato recomendado pelo Meta. Os que já estão certos não perdem qualidade.</small></span>
      </label>
      <label class="f" for="sleep">Pausa entre vídeos (segundos)</label>
      <input type="number" id="sleep" value="3" min="0" max="60">
      <div class="hint">A pausa evita que o Instagram bloqueie por excesso de downloads seguidos.</div>
      <div class="actions">
        <button class="btn primary" id="startBtn">Baixar tudo</button>
        <button class="btn danger hidden" id="stopBtn">Parar</button>
      </div>
      <div class="err" id="err"></div>
      <div class="note">Mantenha esta aba aberta até terminar. Os vídeos ficam disponíveis por algumas horas e depois são apagados do servidor.</div>
    </div>
  </div>

  <div class="card" id="prog">
    <h2>Andamento</h2>
    <div id="progBody"></div>
  </div>

  <div class="card" id="hist">
    <h2>Seu histórico <button class="btn hidden" id="histClear" style="font-size:12.5px;padding:5px 10px">Limpar histórico</button></h2>
    <div id="histBody"></div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const $ = s => document.querySelector(s);
const store = {
  get(k, d){ try{ const v = localStorage.getItem('bxw_'+k); return v === null ? d : JSON.parse(v); }catch(e){ return d; } },
  set(k, v){ try{ localStorage.setItem('bxw_'+k, JSON.stringify(v)); }catch(e){} }
};
async function api(path, body){
  const opt = body === undefined ? {credentials:'same-origin'} : {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)};
  const r = await fetch(path, opt);
  let j = {}; try{ j = await r.json(); }catch(e){}
  if(!r.ok) throw new Error(j.error || ('Erro ' + r.status));
  return j;
}
function toast(msg){ const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(t._h); t._h = setTimeout(()=>t.classList.remove('show'), 2400); }
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const EMPTY = '<div class="empty">Nada em andamento. Cole os links e clique em <b>Baixar tudo</b>.</div>';

// campos lembrados neste navegador
['text','sleep'].forEach(id => { const v = store.get(id); if(v !== undefined && v !== null) $('#'+id).value = v; $('#'+id).addEventListener('input', () => store.set(id, $('#'+id).value)); });
$('#convert').checked = store.get('convert', true);
$('#convert').addEventListener('change', () => store.set('convert', $('#convert').checked));

let parseT;
async function updateCount(){
  try{
    const s = await api('/api/parse', {text: $('#text').value});
    $('#count').textContent = s.count + (s.count === 1 ? ' vídeo' : ' vídeos');
    $('#cats').innerHTML = s.cats.length > 1 || (s.cats[0] && s.cats[0].name !== 'Sem categoria')
      ? s.cats.map(c => `<span class="cat"><b>${c.n}</b> ${esc(c.name)}</span>`).join('') : '';
  }catch(e){}
}
$('#text').addEventListener('input', () => { clearTimeout(parseT); parseT = setTimeout(updateCount, 300); });
updateCount();

$('#loadBtn').onclick = () => $('#file').click();
$('#file').onchange = e => { const f = e.target.files[0]; if(!f) return;
  const r = new FileReader(); r.onload = () => { $('#text').value = r.result; store.set('text', r.result); updateCount(); toast('Arquivo carregado: ' + f.name); }; r.readAsText(f); e.target.value = ''; };
$('#clearBtn').onclick = () => { const b = $('#clearBtn');
  if($('#text').value && !b.dataset.armed){ b.dataset.armed = 1; b.textContent = 'Clique de novo para limpar'; setTimeout(()=>{ delete b.dataset.armed; b.textContent='Limpar'; }, 2500); return; }
  delete b.dataset.armed; b.textContent = 'Limpar'; $('#text').value = ''; store.set('text',''); updateCount(); };

$('#startBtn').onclick = async () => {
  $('#err').textContent = '';
  try{ await api('/api/start', {text: $('#text').value, convert: $('#convert').checked, sleep: parseFloat($('#sleep').value) || 0});
       savedFor = null; poll(); }
  catch(e){ $('#err').textContent = e.message; }
};
$('#stopBtn').onclick = async () => { try{ await api('/api/stop', {}); toast('Parando…'); poll(); }catch(e){} };

const LABEL = {aguardando:'Aguardando', baixando:'Baixando', convertendo:'Convertendo', ok:'Pronto', falhou:'Falhou', cancelado:'Cancelado'};
let last = null, savedFor = null;

function renderIG(st){
  const b = $('#igBanner');
  if(st === 'expired'){ b.className = 'banner bad'; b.innerHTML = '<b>O login do Instagram do sistema expirou.</b> Os vídeos do Instagram vão falhar até o responsável renovar. Avise quem cuida do Baixador.'; }
  else if(st === 'missing'){ b.className = 'banner info'; b.textContent = 'O login do Instagram do sistema ainda não foi configurado. Vídeos do Instagram podem falhar.'; }
  else { b.className = 'banner hidden'; }
}
function render(s){
  renderIG(s.ig);
  const job = s.job;
  const running = !!job && (job.state === 'queued' || job.state === 'running');
  $('#startBtn').disabled = running;
  $('#stopBtn').classList.toggle('hidden', !running);
  if(!job){ $('#progBody').innerHTML = EMPTY; return; }
  const items = job.items, n = items.length;
  const done = items.filter(i => ['ok','falhou','cancelado'].includes(i.status)).length;
  const ok = items.filter(i => i.status === 'ok').length;
  const bad = items.filter(i => i.status === 'falhou');
  const cur = items.find(i => i.status === 'baixando' || i.status === 'convertendo');
  const frac = (done + (cur ? (cur.progress||0)/100 : 0)) / n;
  const head = job.state === 'queued' ? 'Na fila' : job.state === 'running' ? 'Baixando' : (job.state === 'stopped' ? 'Interrompido' : 'Concluído');
  let html = '';
  if(job.state === 'queued') html += `<div class="queue">Outra pessoa está baixando agora. Você é o <b>${job.position}º</b> da fila; começa sozinho, é só deixar a aba aberta.</div>`;
  html += `<div class="sumline"><span class="big">${done} de ${n}</span><span>${head}</span>
     <span style="color:var(--ok)">${ok} prontos</span>${bad.length?`<span style="color:var(--bad)">${bad.length} falharam</span>`:''}</div>
     <div class="bar"><i style="width:${(frac*100).toFixed(1)}%"></i></div>
     <div class="row" style="margin-bottom:12px">
       ${ok ? `<a class="btn primary" style="padding:9px 16px;font-size:14px" href="/api/zip">Baixar .zip (${ok} ${ok===1?'vídeo':'vídeos'})</a>` : ''}
       ${bad.length ? `<button class="btn" onclick="copyFailed()">Copiar links que falharam</button>` : ''}
       ${running ? '' : `<button class="btn" style="margin-left:auto" onclick="clearList()">Limpar lista</button>`}
     </div>
     <div class="tablewrap"><table><thead><tr><th>#</th><th>Pasta</th><th>Vídeo</th><th>Status</th><th></th></tr></thead><tbody>`;
  items.forEach((it, i) => {
    const showBar = it.status === 'baixando' || it.status === 'convertendo';
    const pct = showBar ? ` ${Math.round(it.progress||0)}%` : '';
    html += `<tr><td class="n">${i+1}</td><td>${esc(it.cat)}</td>
      <td class="code"><a href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.vid || it.url)}</a>${it.msg?`<div class="why">${esc(it.msg)}</div>`:''}</td>
      <td class="st"><span class="badge b-${it.status}">${LABEL[it.status]||it.status}${pct}</span>
      ${showBar?`<div class="minibar" style="color:var(--${it.status==='baixando'?'info':'conv'})"><i style="width:${it.progress||0}%"></i></div>`:''}</td>
      <td class="dl">${it.has_file?`<a href="/api/file?i=${i}">Baixar</a>`:''}</td></tr>`;
  });
  html += `</tbody></table></div>
    <details id="logbox" ${store.get('logopen',false)?'open':''}><summary>Registro técnico</summary><pre id="logpre">${esc((job.log||[]).join('\n'))}</pre></details>`;
  const tw = $('.tablewrap'); const keep = tw ? tw.scrollTop : 0;
  $('#progBody').innerHTML = html;
  const tw2 = $('.tablewrap'); if(tw2) tw2.scrollTop = keep;
  const lb = $('#logbox'); lb.addEventListener('toggle', () => store.set('logopen', lb.open));
  const lp = $('#logpre'); if(lp) lp.scrollTop = lp.scrollHeight;
  window._failed = bad.map(i => i.url);
  if(!running && savedFor !== job.id) saveHistory(job);
}

// histórico guardado só neste navegador
function saveHistory(job){
  savedFor = job.id;
  const h = store.get('hist', []);
  if(h.some(x => x.id === job.id)) return;
  const items = job.items;
  const cats = {}; items.forEach(i => cats[i.cat] = (cats[i.cat]||0) + 1);
  let text = '', lastCat = null;
  items.forEach(i => { if(i.cat !== lastCat){ text += (text?'\n':'') + (i.cat !== 'Sem categoria' ? '## '+i.cat+'\n' : ''); lastCat = i.cat; } text += i.url + '\n'; });
  h.unshift({id: job.id, at: job.finished || job.created, total: items.length,
             ok: items.filter(i=>i.status==='ok').length, bad: items.filter(i=>i.status==='falhou').length,
             cats: Object.keys(cats), text});
  store.set('hist', h.slice(0, 50));
  renderHistory();
}
function renderHistory(){
  const h = store.get('hist', []);
  $('#histClear').classList.toggle('hidden', !h.length);
  if(!h.length){ $('#histBody').innerHTML = '<div class="empty">Seus downloads anteriores aparecem aqui. O histórico fica salvo só neste navegador.</div>'; return; }
  $('#histBody').innerHTML = h.map((x, i) => {
    const d = new Date(x.at * 1000);
    const when = d.toLocaleDateString('pt-BR') + ' ' + d.toLocaleTimeString('pt-BR', {hour:'2-digit', minute:'2-digit'});
    return `<div class="hrow"><div><b>${when}</b> · ${x.ok} de ${x.total} prontos${x.bad?` · <span style="color:var(--bad)">${x.bad} falharam</span>`:''}
      <div class="m">${esc(x.cats.join(' · '))}</div></div>
      <button class="btn" style="font-size:12.5px;padding:5px 10px" onclick="reuse(${i})">Usar estes links</button></div>`;
  }).join('');
}
function reuse(i){ const h = store.get('hist', []); if(!h[i]) return; $('#text').value = h[i].text; store.set('text', h[i].text); updateCount(); window.scrollTo({top:0, behavior:'smooth'}); toast('Links colocados na lista'); }
$('#histClear').onclick = () => { const b = $('#histClear');
  if(!b.dataset.armed){ b.dataset.armed = 1; b.textContent = 'Clique de novo para confirmar'; setTimeout(()=>{ delete b.dataset.armed; b.textContent='Limpar histórico'; }, 2500); return; }
  delete b.dataset.armed; b.textContent = 'Limpar histórico'; store.set('hist', []); renderHistory(); };
renderHistory();

async function clearList(){ try{ await api('/api/clear', {}); last = null; poll(); toast('Lista limpa.'); }catch(e){ toast(e.message); } }
async function copyFailed(){ try{ await navigator.clipboard.writeText(window._failed.join('\n')); toast('Links copiados'); }catch(e){ toast('Não consegui copiar'); } }

let pollT;
async function poll(){
  clearTimeout(pollT);
  try{
    const s = await api('/api/status');
    const sig = JSON.stringify([s.job, s.ig]);
    if(sig !== last){ last = sig; render(s); }
    const active = s.job && (s.job.state === 'queued' || s.job.state === 'running');
    pollT = setTimeout(poll, active ? 1000 : 5000);
  }catch(e){ pollT = setTimeout(poll, 4000); }
}
poll();
</script>
</body>
</html>
"""


def main():
    os.makedirs(DATA, exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print("Baixador web na porta %d (yt-dlp: %s, ffmpeg: %s)" % (PORT, YTDLP, FFMPEG), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
