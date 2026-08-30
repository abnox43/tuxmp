#!/usr/bin/env python3
"""
tuxmp — a simple Spotify-styled terminal music player.

Type a search, hit Enter, press a number to play. Album art renders
live in the terminal while the audio streams.

Search:
  * Default: searches via yt-dlp (YouTube), zero setup.
  * Optional real Spotify search: create a free app at
    https://developer.spotify.com/dashboard and export
    SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET.

Controls:
  type          search query
  Enter         search, or play the highlighted song
  1..9          play that song instantly
  Up / Down     highlight a song
  Space         pause / resume
  Left / Right  seek -5s / +5s
  s             stop
  r             re-search
  Esc           back to search box
  q             quit (while browsing/playing)
"""

import io
import os
import math
import time
import json
import queue
import random
import urllib.parse
import urllib.request
import threading
import subprocess

import vlc
from PIL import Image, ImageDraw, ImageEnhance

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich.table import Table
from rich.align import Align
from rich.layout import Layout
from rich.style import Style
from rich import box

console = Console()

# ------------------------------------------------------------------
# Palette
# ------------------------------------------------------------------

ACCENT = "#1DB954"
ACCENT_DIM = "#147A3A"
PINK = "#FF5C8A"
GOLD = "#FFD700"
CYAN = "#00BCD4"
PURPLE = "#B388FF"
ORANGE = "#FF7043"
FG = "#E8E8E8"
DIM = "#8A8A8A"
DARK_ROW = "on rgb(17,48,28)"

EQ_COLORS = [ACCENT, CYAN, PINK, GOLD, PURPLE, ORANGE]
EQ_BARS = "▁▂▃▄▅▆▇█"

RESULT_LIMIT = 9   # 1..9 play buttons


# ------------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------------

def fmt_dur(seconds):
    if not seconds:
        return "0:00"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def fmt_count(n):
    n = n or 0
    if n >= 1_000_000_000:
        return f"{n/1e9:.1f}B"
    if n >= 1_000_000:
        return f"{n/1e6:.1f}M"
    if n >= 1_000:
        return f"{n/1e3:.1f}K"
    return str(n)


def clip(text, width):
    text = str(text or "")
    return text if len(text) <= width else text[:width - 1] + "…"


# ------------------------------------------------------------------
# Search providers
# ------------------------------------------------------------------

class YtSearch:
    """YouTube search + thumbnails via yt-dlp. No accounts needed."""

    name = "YouTube"

    def search(self, query, limit=RESULT_LIMIT):
        cmd = [
            "yt-dlp", "--dump-single-json", "--flat-playlist",
            "--no-warnings", "--ignore-errors",
            f"ytsearch{limit}:{query}",
        ]
        try:
            out = subprocess.check_output(
                cmd, stderr=subprocess.DEVNULL, timeout=60)
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            raise RuntimeError(f"yt-dlp failed: {e}")

        data = json.loads(out)
        results = []
        for e in data.get("entries", []):
            if not e or not e.get("id"):
                continue
            thumb = None
            for t in (e.get("thumbnails") or []):
                if (t.get("width") or 0) >= 480:
                    thumb = t.get("url")
                    break
            thumb = thumb or e.get("thumbnail")
            results.append({
                "provider": "youtube",
                "id": e.get("id"),
                "title": e.get("title", "Unknown"),
                "artists": e.get("channel") or e.get("uploader") or "Unknown",
                "album": "YouTube",
                "duration": e.get("duration"),
                "art_url": thumb,
                "views": e.get("view_count"),
                "url": f"https://youtu.be/{e.get('id')}",
            })
        return results

    def stream_url(self, track):
        cmd = [
            "yt-dlp", "-f", "bestaudio/best", "--no-playlist",
            "--no-warnings", "--ignore-errors", "-g", track["url"],
        ]
        try:
            out = subprocess.check_output(
                cmd, stderr=subprocess.DEVNULL, timeout=90)
            url = out.decode().strip().splitlines()[-1].strip()
            return url if url else None
        except (subprocess.SubprocessError, FileNotFoundError):
            return None


class SpotifySearch:
    """Real Spotify search (client_credentials). Audio still streams via
    yt-dlp since Spotify's own streams are encrypted."""

    name = "Spotify"

    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.token_exp = 0

    def _token(self):
        if self.token and time.time() < self.token_exp - 30:
            return self.token
        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())
        self.token = body["access_token"]
        self.token_exp = time.time() + body.get("expires_in", 3600)
        return self.token

    def search(self, query, limit=RESULT_LIMIT):
        params = urllib.parse.urlencode({
            "q": query, "type": "track", "limit": limit,
        })
        req = urllib.request.Request(
            f"https://api.spotify.com/v1/search?{params}",
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Spotify API error: HTTP {e.code}")

        out = []
        for it in data.get("tracks", {}).get("items", []):
            album = it.get("album", {})
            art = None
            if album.get("images"):
                art = album["images"][0]["url"]
            out.append({
                "provider": "spotify",
                "id": it.get("id"),
                "title": it.get("name"),
                "artists": ", ".join(
                    a["name"] for a in it.get("artists", [])),
                "album": album.get("name", ""),
                "duration": (it.get("duration_ms") or 0) // 1000,
                "art_url": art,
                "views": None,
                "url": it.get("external_urls", {}).get("spotify"),
            })
        return out


def make_search_provider():
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    csec = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if cid and csec:
        return SpotifySearch(cid, csec)
    return YtSearch()


class Streamer:
    """Resolve any track to a playable audio URL."""

    def __init__(self, provider):
        self.provider = provider

    def resolve(self, track):
        if track.get("provider") == "youtube":
            return self.provider.stream_url(track)
        yt = YtSearch()
        hits = yt.search(f"{track['title']} {track['artists']}", limit=1)
        if not hits:
            return None
        return yt.stream_url(hits[0])


# ------------------------------------------------------------------
# Album art -> terminal graphics
# ------------------------------------------------------------------

class ArtRenderer:

    @staticmethod
    def fetch(url):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            return Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            return None

    @staticmethod
    def rounded_album(img, size=380, radius=32):
        img = img.resize((size, size), Image.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, size - 1, size - 1], radius=radius, fill=255)
        img = img.convert("RGBA")
        img.putalpha(mask)
        bg = Image.new("RGBA", (size, size), (13, 13, 16, 255))
        bg.alpha_composite(img)
        return bg.convert("RGB")

    @staticmethod
    def half_block_lines(img, cols=30):
        """Colored half-block unicode art. Rows scale like cols."""
        src = img.resize((cols * 2, cols * 4), Image.LANCZOS)
        src = ImageEnhance.Color(src).enhance(1.35)
        src = ImageEnhance.Contrast(src).enhance(1.15)
        src = ImageEnhance.Brightness(src).enhance(0.92)
        px = src.load()
        w, h = src.size
        lines = []
        for y in range(0, h - 1, 2):
            row = []
            for x in range(0, w - 1, 2):
                ul = px[x, y]; ur = px[x + 1, y]
                dl = px[x, y + 1]; dr = px[x + 1, y + 1]
                up = tuple((ul[i] + ur[i]) // 2 for i in range(3))
                dn = tuple((dl[i] + dr[i]) // 2 for i in range(3))
                row.append(
                    f"[rgb({up[0]},{up[1]},{up[2]})"
                    f"on rgb({dn[0]},{dn[1]},{dn[2]})]▀[/]")
            lines.append("".join(row))
        return lines


# ------------------------------------------------------------------
# VLC playback
# ------------------------------------------------------------------

class Player:
    def __init__(self):
        self.instance = vlc.Instance("--no-video")
        self.player = None

    def play(self, url):
        self.stop()
        self.player = self.instance.media_player_new()
        self.player.set_media(self.instance.media_new(url))
        self.player.audio_set_volume(90)
        self.player.play()

    def stop(self):
        if self.player:
            self.player.stop()
            self.player = None

    def toggle(self):
        if self.player:
            self.player.pause()

    def is_playing(self):
        return bool(self.player and self.player.is_playing())

    def position(self):
        if not self.player:
            return (0.0, 0.0)
        try:
            ms = self.player.get_time()
            length = self.player.get_length()
            if ms < 0 or length <= 0:
                return (0.0, 0.0)
            return (ms / 1000.0, length / 1000.0)
        except Exception:
            return (0.0, 0.0)

    def seek(self, off_ms):
        if self.player:
            ms = max(0, self.player.get_time() + off_ms)
            self.player.set_time(ms)


# ------------------------------------------------------------------
# TUI application
# ------------------------------------------------------------------

class TuxmpTUI:
    def __init__(self, provider):
        self.sync = YtSearch() if isinstance(provider, SpotifySearch) else provider
        self.provider = provider
        self.player = Player()
        self.source = provider.name

        self.mode = "search"      # search | pick | play
        self.state = "idle"       # idle | searching | streaming | results | error
        self.note = ""
        self.query = ""
        self.results = []
        self.sel = 0
        self.now = None
        self.art_lines = []
        self._gen = 0
        self.keyq = queue.Queue()
        self.refreshq = queue.Queue()
        self.alive = threading.Event()

    # ---------- async actions ----------

    def search(self):
        q = self.query.strip()
        if not q:
            return
        self._gen += 1
        gen = self._gen
        self.state = "searching"
        self.mode = "pick"
        self.note = ""
        self.refreshq.put(None)

        def job():
            try:
                rows = self.provider.search(q)
            except Exception as e:
                if gen != self._gen:
                    return
                self.state = "error"
                self.note = f"{self.source}: {e}"
            else:
                if gen != self._gen:
                    return
                self.results = rows
                self.sel = 0
                if rows:
                    self.state = "results"
                    self.note = f"{len(rows)} hits · {self.source}"
                else:
                    self.state = "results"
                    self.note = "Nothing found — try again"
            self.refreshq.put(None)

        threading.Thread(target=job, daemon=True).start()

    def play(self, idx):
        if not (0 <= idx < len(self.results)):
            return
        track = self.results[idx]
        self.sel = idx
        self.now = track
        self.art_lines = []
        self.state = "streaming"
        self.mode = "play"
        self.note = f"Resolving stream · {clip(track['title'], 32)}"
        self.refreshq.put(None)

        def job():
            try:
                url = Streamer(self.sync).resolve(track)
            except Exception:
                url = None
            if url:
                self.player.play(url)
                self.state = "playing"
                self.mode = "play"
                self.note = ""
            else:
                self.state = "error"
                self.mode = "pick"
                self.note = "Couldn't find a stream for this one"
            self.refreshq.put(None)
            self._load_art(track)
            self.refreshq.put(None)

        threading.Thread(target=job, daemon=True).start()

    def stop(self):
        self.player.stop()
        self.state = "results"
        self.mode = "pick"
        self.now = None
        self.art_lines = []
        self.note = "Stopped"
        self.refreshq.put(None)

    def go_search(self):
        self.mode = "search"
        self.results = []
        self.sel = 0
        self.now = None
        self.art_lines = []
        self.state = "idle"
        self.note = ""
        self.player.stop()
        self.refreshq.put(None)

    def _load_art(self, track):
        url = track.get("art_url")
        if not url:
            return
        raw = ArtRenderer.fetch(url)
        if not raw:
            return
        art = ArtRenderer.rounded_album(raw)
        self.art_lines = ArtRenderer.half_block_lines(art, cols=30)

    # ---------- input ----------

    def _read_keys(self):
        import sys
        import select
        import termios

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] &= ~(termios.ECHO | termios.ICANON)
        new[3] |= termios.ISIG
        termios.tcsetattr(fd, termios.TCSANOW, new)
        try:
            while not self.alive.is_set():
                r, _, _ = select.select([sys.stdin], [], [], 0.25)
                if not r:
                    continue
                ch = sys.stdin.read(1)
                if ch == "\x1b":
                    seq = ""
                    end = time.time() + 0.15
                    while len(seq) < 3 and time.time() < end:
                        r, _, _ = select.select([sys.stdin], [], [], 0.04)
                        if not r:
                            break
                        c = sys.stdin.read(1)
                        if not c:
                            break
                        seq += c
                    if seq in ("[A", "OA"):
                        self.keyq.put("UP")
                    elif seq in ("[B", "OB"):
                        self.keyq.put("DOWN")
                    elif seq in ("[C", "OC"):
                        self.keyq.put("RIGHT")
                    elif seq in ("[D", "OD"):
                        self.keyq.put("LEFT")
                    else:
                        self.keyq.put("ESC")
                elif ch in ("\r", "\n"):
                    self.keyq.put("ENTER")
                elif ch in ("\x7f", "\x08"):
                    self.keyq.put("BACK")
                elif ch == " ":
                    self.keyq.put("SPACE")
                elif ch == "\x03":
                    self.keyq.put("CTRL-C")
                elif ch.isprintable():
                    self.keyq.put(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    # ---------- UI pieces ----------

    def _spinner(self):
        return "◐◓◑◒"[int(time.time() * 6) % 4]

    def _title(self):
        t = time.time()
        bars = "▁▂▃▄▅▆▇█"
        logo = Text()
        logo_colors = [ACCENT, PINK, GOLD, CYAN, PURPLE, ORANGE,
                       ACCENT, PINK]
        for i, ch in enumerate("tuxmp"):
            logo.append(ch, style=f"bold {logo_colors[i % len(logo_colors)]}")
        logo.append("   terminal player", style=f"dim {DIM}")
        logo.append(f"   {bars[int(t * 8) % 8]}", style=f"{ACCENT}")
        return Panel(
            Align.center(logo),
            box=box.HEAVY, border_style=ACCENT, padding=(0, 1))

    def _searchbox(self):
        blink = int(time.time() * 2) % 2 == 0
        busy = self.state in ("searching", "streaming")
        text = Text(self.query, style=f"bold {FG}")
        if busy:
            text.append(self._spinner(), style=f"bold {ACCENT}")
        elif self.mode == "search" and blink:
            text.append("▏", style=f"bold {ACCENT}")
        suffix = Text.assemble(
            ("   ", ""),
            (f"[{DIM}]source:[/] ", ""),
            (f"{self.source}", f"bold {ACCENT}"),
        )
        return Panel(
            Group(
                Text.assemble(("  ♪  ", f"bold {ACCENT}"), text),
                suffix,
            ),
            box=box.HEAVY, border_style=ACCENT, padding=(0, 1))

    def _hint(self):
        if self.mode == "search":
            return Text.assemble(
                (f"[{ACCENT}]type[/] and press ", ""),
                (f"[{GOLD}]⏎[/] ", ""),
                (" to search", ""),
            )
        if self.mode == "pick":
            return Text.assemble(
                (f"[{GOLD}]1–9[/] ", ""),
                ("play   ", ""),
                (f"[{PINK}]↑↓[/] ", ""),
                ("pick   ", ""),
                (f"[{CYAN}]⏎[/] ", ""),
                ("play   ", ""),
                (f"[{PURPLE}]r[/] ", ""),
                ("again   ", ""),
                (f"[{ORANGE}]Esc[/] ", ""),
                ("search", ""),
            )
        return Text.assemble(
            (f"[{GOLD}]1–9[/] ", ""),
            ("switch   ", ""),
            (f"[{CYAN}]Space[/] ", ""),
            ("pause   ", ""),
            (f"[{ORANGE}]←→[/] ", ""),
            ("±5s   ", ""),
            (f"[{PURPLE}]s[/] ", ""),
            ("stop   ", ""),
            (f"[{PINK}]r[/] ", ""),
            ("again   ", ""),
            (f"[{ACCENT}]Esc[/] ", ""),
            ("search   ", ""),
            (f"[{DIM}]q[/] ", ""),
            ("quit", ""),
        )

    def _results(self):
        if self.state == "error" and not self.results:
            return Panel(
                Align.center(Text.assemble(
                    ("✖  ", f"bold {PINK}"), (self.note, f"bold {PINK}"))),
                box=box.ROUNDED, border_style=PINK, padding=(1, 2))

        if not self.results:
            if self.state == "searching":
                return Panel(
                    Align.center(Text.assemble(
                        (self._spinner() + "  ", f"bold {ACCENT}"),
                        ("searching…", f"bold {FG}"))),
                    box=box.ROUNDED, border_style=ACCENT, padding=(1, 2))
            return Panel(
                Align.center(Text(
                    "type a song above, press ⏎",
                    style=f"dim {DIM}")),
                box=box.ROUNDED, border_style=DIM, padding=(1, 2))

        table = Table(box=box.SIMPLE, show_header=False, expand=True,
                      pad_edge=False, style=DIM)
        table.add_column("sel", min_width=3, max_width=3, justify="center")
        table.add_column("track", min_width=18, max_width=62)
        table.add_column("artist", min_width=10, max_width=34)
        table.add_column("dur", min_width=5, max_width=8, justify="right")
        show_views = any(t.get("views") is not None for t in self.results)
        if show_views:
            table.add_column("views", min_width=6, max_width=9,
                             justify="right")

        for i, t in enumerate(self.results):
            sel = i == self.sel
            title = clip(t["title"], 58)
            artist = clip(t["artists"], 32)
            dur = fmt_dur(t.get("duration"))
            cells = []
            cells.append(Text(f"{i + 1}.", style=f"bold {GOLD}") if sel
                         else Text(f"{i + 1}.", style=f"dim {DIM}"))
            cells.append(
                Text(title, style=f"bold {FG}" if sel else Style()))
            cells.append(Text(artist,
                              style=Style(italic=True, color=ACCENT) if sel
                              else Style(dim=True)))
            cells.append(Text(dur, style=f"bold {GOLD}" if sel
                              else f"dim {DIM}"))
            if show_views:
                v = Text(fmt_count(t["views"]), style=f"dim {DIM}")
                v.justify = "right"
                cells.append(v)
            table.add_row(*cells, style=DARK_ROW if sel else None)
        return table

    def _art_panel(self):
        if not self.art_lines:
            return None
        pulse = int((math.sin(time.time() * 2) + 1) * 1.5)
        border = [ACCENT, "#0f6b2e", "#0f8a3f"][pulse]
        body = "\n".join(self.art_lines)
        head = Text.assemble(("♫  ", f"bold {ACCENT}"),
                             (clip(self.now["title"], 32), f"bold {FG}"))
        sub = Text.assemble(("   ", ""), (clip(self.now["artists"], 26),
                                          f"{PINK}"))
        meta = Text.assemble(("   ♪ ", f"dim {DIM}"),
                             (clip(self.now["album"], 24), f"dim {DIM}"))
        return Panel(
            Align.center(Group(body, "", head, sub, meta)),
            box=box.ROUNDED, border_style=border,
            subtitle=f"[{ACCENT}]  NOW PLAYING  [/]",
            padding=(0, 1))

    def _progress(self):
        pos, length = self.player.position()
        title = Text.assemble(
            ("▶  ", f"bold {ACCENT}") if self.player.is_playing()
            else ("‖  ", f"bold {GOLD}"),
            (clip(self.now["title"], 38), f"bold {FG}"),
            ("  ·  ", f"dim {DIM}"),
            (clip(self.now["artists"], 26), f"dim {ACCENT}"),
            ("\n   ", ""),
        )
        if length > 0:
            width = 32
            filled = min(width, int(pos / length * width))
            bar = (f"[{ACCENT}]{'█' * filled}[/]"
                   f"[{DIM}]{'░' * (width - filled)}[/]")
        else:
            bar = f"[{ACCENT}]{EQ_BARS[int(time.time() * 6) % 8]}[/]"
        pct = f"{pos / length * 100:5.1f}%" if length else ""
        return Group(
            title,
            Text.assemble(
                bar,
                ("   ", ""),
                (f"{fmt_dur(pos)} / {fmt_dur(length)}", f"bold {GOLD}"),
                ("   ", ""),
                (pct, f"dim {DIM}"),
            ))

    def _eq(self, playing):
        t = time.time()
        if playing:
            rnd = random.Random(int(t * 12))
            amps = [rnd.randint(1, 8) for _ in range(20)]
        else:
            amps = [1, 2, 3, 3, 2, 4, 3, 1, 2, 3, 2,
                    1, 3, 2, 4, 3, 2, 1, 2, 3, 2]
        parts = []
        for i, a in enumerate(amps):
            parts.append(f"[{EQ_COLORS[i % 6]}]{EQ_BARS[a - 1]}[/]")
        return " ".join(parts)

    def _status(self):
        if self.state == "searching":
            return Text.assemble(("◌  ", f"{ACCENT}"),
                                 (self.note or "searching…",
                                  f"bold {CYAN}"))
        if self.state == "streaming":
            return Text.assemble(("⏳  ", f"{GOLD}"),
                                 (self.note or "loading…", f"bold {GOLD}"))
        if self.state == "error":
            return Text.assemble(("✖  ", f"bold {PINK}"),
                                 (self.note, f"bold {PINK}"))
        if self.state == "idle":
            return Text.assemble(("●  ", f"dim {DIM}"),
                                 ("search a song below", f"dim {DIM}"))
        if self.now and self.state == "playing":
            return Text.assemble(
                ("♪  ", f"bold {ACCENT}"),
                (clip(self.now["title"], 40), f"bold {FG}"),
                ("  ·  ", f"dim {DIM}"),
                (clip(self.now["artists"], 28), f"dim {DIM}"),
            )
        return Text.assemble(("▸  ", f"dim {ACCENT}"),
                             (self.note or f"{len(self.results)} results",
                              f"dim {FG}"))

    # ---------- layout ----------

    def render(self):
        lay = Layout()
        lay.split_column(
            Layout(name="title", size=3),
            Layout(name="body", ratio=1),
            Layout(name="eq", size=1),
            Layout(name="hint", size=1),
        )
        lay["title"].update(self._title())
        lay["hint"].update(Align.center(self._hint()))

        if self.mode == "play":
            art = self._art_panel()
            left = Layout()
            left.split_column(
                Layout(name="box", ratio=1),
                Layout(name="prog", size=5),
            )
            left["box"].update(
                Group(self._searchbox(), Text(""), self._results()))
            left["prog"].update(Panel(
                self._progress(),
                box=box.SIMPLE, border_style=ACCENT_DIM, padding=(0, 2)))
            if art:
                lay["body"].split_row(
                    Layout(left, ratio=5),
                    Layout(name="artbox", ratio=4),
                )
                lay["artbox"].update(Align.center(art, vertical="middle"))
            else:
                lay["body"].update(Align.center(left, vertical="middle"))
            lay["eq"].update(Align.center(self._eq(self.player.is_playing())))
        else:
            lay["body"].update(
                Group(self._searchbox(), Text(""), self._results()))
            lay["eq"].update(Align.center(self._eq(False)))
        return lay

    # ---------- main loop ----------

    def run(self):
        self.alive.clear()
        threading.Thread(target=self._read_keys, daemon=True).start()
        try:
            with Live(self.render(), console=console, screen=True,
                      auto_refresh=False, refresh_per_second=20) as live:
                last = time.time()
                while True:
                    try:
                        self.refreshq.get_nowait()
                        live.update(self.render())
                        live.refresh()
                    except queue.Empty:
                        pass
                    try:
                        key = self.keyq.get_nowait()
                    except queue.Empty:
                        key = None

                    # ---------- key dispatch ----------
                    if key == "CTRL-C":
                        break

                    if key is None:
                        key = ""

                    if key == "q" and self.mode != "search":
                        break

                    if key == "ESC":
                        self.go_search()
                        live.update(self.render())
                        live.refresh()

                    elif self.mode == "search":
                        if key == "BACK":
                            self.query = self.query[:-1]
                            live.update(self.render()); live.refresh()
                        elif key == "ENTER":
                            if self.query.strip():
                                self.search()
                                live.update(self.render()); live.refresh()
                        elif key == "SPACE":
                            self.query += " "
                            live.update(self.render()); live.refresh()
                        elif len(key) == 1 and key.isprintable():
                            self.query += key
                            live.update(self.render()); live.refresh()

                    else:  # pick / play
                        if key and "1" <= key <= "9":
                            self.play(int(key) - 1)
                            live.update(self.render()); live.refresh()
                        elif key == "ENTER" and self.results and \
                                self.mode == "pick":
                            self.play(self.sel)
                            live.update(self.render()); live.refresh()
                        elif key in ("R", "r") and self.query.strip():
                            self.search()
                        elif key == "UP" and self.results:
                            self.sel = max(0, self.sel - 1)
                            live.update(self.render()); live.refresh()
                        elif key == "DOWN" and self.results:
                            self.sel = min(len(self.results) - 1,
                                           self.sel + 1)
                            live.update(self.render()); live.refresh()
                        elif key == "SPACE":
                            if self.player.is_playing():
                                self.player.toggle()
                                live.update(self.render()); live.refresh()
                        elif key in ("S", "s"):
                            self.stop()
                            live.update(self.render()); live.refresh()
                        elif key == "LEFT":
                            self.player.seek(-5000)
                        elif key == "RIGHT":
                            self.player.seek(5000)

                    now = time.time()
                    if now - last > 1 / 20:
                        last = now
                        live.update(self.render())
                        live.refresh()
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            self.alive.set()
            self.player.stop()
            console.print(f"\n[bold {ACCENT}]tuxmp[/] — bye! ♪")


def main():
    provider = make_search_provider()
    TuxmpTUI(provider).run()


if __name__ == "__main__":
    main()