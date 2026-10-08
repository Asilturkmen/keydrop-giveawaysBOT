# -*- coding: utf-8 -*-
"""
Keydrop Bot - watches Keydrop giveaways and joins them for you.

Control everything from this window (dark, Keydrop-style UI built with CustomTkinter):
  - One big START / STOP button; the Steam login is detected automatically
  - Account card with a "Log in" button (log in once; the session is kept)
  - Live status with countdown, session stats, an activity feed and a settings tab
  - TR / EN toggle; settings are remembered in settings.json
  - A banner offers a one-click update when a newer release is on GitHub

When a new active giveaway is found, a red line appears in the log and the detail
page opens. It auto-joins only when Keydrop says the account is eligible (the
deposit requirement for your level's time window is already met, so joining is
free) and then checks with Keydrop that the entry really counted.
It never deposits money and never clicks deposit / extra-entry buttons.

Run KeydropBot.bat (it installs everything on the first run).
Developers: pip install -r requirements.txt, python -m playwright install chromium,
then python keydrop_ui.py
"""

import asyncio
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import tkinter as tk

import customtkinter as ctk
from PIL import Image
from playwright.async_api import async_playwright

__version__ = "1.1.0"

# --------------------------- CONSTANTS ------------------------------

ROOT = Path(__file__).resolve().parent.parent       # project folder (src/..)
USER_DATA_DIR = ROOT / "keydrop_profile"           # browser profile = your Keydrop login
DEBUG_FILE = ROOT / "debug_payloads.json"
SETTINGS_FILE = ROOT / "settings.json"
LAUNCHER = ROOT / "KeydropBot.bat"
ICON_FILE = Path(__file__).resolve().parent / "icon.ico"
GIVEAWAYS_URL = "https://keydrop.com/tr/giveaways/list"
# A giveaway's detail (join) page. {org} = organizer, {id} = giveaway id.
# For Key-Drop's own giveaways (amateur/contender/...) the organizer is 'keydrop'.
DETAIL_URL = "https://keydrop.com/tr/giveaways/{org}/{id}"
# Site init data: {"status": false, "message": ...} when not logged in, user data otherwise.
INIT_URL = "https://keydrop.com/tr/apiData/Init/index"
ID_KEYS = ("id", "giveawayId", "uuid", "slug", "code", "_id", "hash")
TIER_KEYS = ("frequency", "category", "type", "tier", "name", "level", "rank", "kind", "group")
ALL_TIERS = ["amateur", "contender", "challenger", "champion"]
# You can no longer join giveaways in these states (ended/cancelled).
FINISHED_STATUSES = {"ended", "finished", "cancelled", "canceled", "closed", "drawn"}
# Max seconds to wait for list data on each attempt.
LIST_LOAD_TIMEOUT = 10
# If the list doesn't load (internet etc.) retry this many times.
LIST_LOAD_RETRIES = 3
# API addresses (matched against the lowercased response URL).
# Official tier list. Creator giveaways come from '/giveaway-user/list' and often arrive first.
LIST_API_PART = "/giveaway/list"
# Per-giveaway data for the logged-in user (haveIJoined, depositAmountMissing, myJoinCount...).
# The list's own haveIJoined / depositAmountRequired are NOT user-specific, so join decisions use this.
DETAIL_API_PART = "/giveaway/data/"
INIT_API_PART = "/apidata/init/index"
# Retry joining one giveaway this many times before giving up on it.
JOIN_ATTEMPTS = 3
# Seconds between login checks while waiting for the user to log in.
LOGIN_POLL = 3

# Updates: the newest GitHub release replaces these files.
GITHUB_REPO = "Asilturkmen/keydrop-giveawaysBOT"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RAW_FILE_URL = "https://raw.githubusercontent.com/" + GITHUB_REPO + "/{tag}/{path}"
UPDATE_FILES = ("src/keydrop_ui.py", "src/requirements.txt")

# Runs inside the Keydrop tab, so it has the browser's cookies (and gets past Cloudflare).
_FETCH_JSON_JS = """async (url) => {
  try { const r = await fetch(url, {credentials: 'include'}); return await r.json(); }
  catch (e) { return null; }
}"""

# UI <-> worker communication channels
ui_queue = queue.Queue()
stop_event = threading.Event()      # close the browser and end the session
monitor_event = threading.Event()   # the user pressed Start: run the bot once logged in
run_config = {}                     # settings for the bot, filled when Start is pressed
worker_thread = None

# --------------------------- I18N (TR / EN) ------------------------------


def _system_lang():
    """Turkish if Windows' display language is Turkish, otherwise English."""
    try:
        return "tr" if (ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF) == 0x1F else "en"
    except Exception:
        return "en"


# Active language. Follows Windows; toggled from the UI (top-right button).
_LANG = {"v": _system_lang()}

TXT = {
    # window / account card
    "win_title":          {"en": "Keydrop Bot", "tr": "Keydrop Bot"},
    "acc_title_unknown":  {"en": "Keydrop account", "tr": "Keydrop hesabı"},
    "acc_sub_unknown":    {"en": "Press START. If needed, log in with Steam in the window that opens.",
                           "tr": "BAŞLAT'a bas. Gerekirse açılan pencerede Steam ile giriş yap."},
    "acc_title_checking": {"en": "Checking...", "tr": "Kontrol ediliyor..."},
    "acc_sub_checking":   {"en": "Connecting to Keydrop", "tr": "Keydrop'a bağlanılıyor"},
    "acc_title_in":       {"en": "Logged in", "tr": "Giriş yapıldı"},
    "acc_sub_in":         {"en": "Logged in with Steam", "tr": "Steam ile giriş yapıldı"},
    "acc_title_out":      {"en": "Not logged in", "tr": "Giriş yapılmadı"},
    "acc_sub_out":        {"en": "Log in with Steam in the browser window that opened.",
                           "tr": "Açılan tarayıcı penceresinde Steam ile giriş yap."},
    "badge_in":           {"en": "●  Connected", "tr": "●  Bağlı"},
    "login":              {"en": "Log in", "tr": "Giriş yap"},

    # big button / status line / stats
    "big_start":    {"en": "▶   START", "tr": "▶   BAŞLAT"},
    "big_stop":     {"en": "■   STOP", "tr": "■   DURDUR"},
    "big_stopping": {"en": "Stopping...", "tr": "Durduruluyor..."},
    "next_check":   {"en": "·  next check in {s}s", "tr": "·  sonraki kontrol {s} sn"},
    "checking_now": {"en": "·  checking...", "tr": "·  kontrol ediliyor..."},
    "safety":       {"en": "The bot never deposits money. It only joins when your entry is free.",
                     "tr": "Bot asla para yatırmaz. Yalnızca katılımın ücretsizse katılır."},
    "stat_joined":  {"en": "Joined", "tr": "Katılınan"},
    "stat_last":    {"en": "Last join", "tr": "Son katılım"},
    "stat_tiers":   {"en": "Watching", "tr": "Takip edilen"},

    # tabs / settings
    "tab_activity": {"en": "Activity", "tr": "Etkinlik"},
    "tab_settings": {"en": "Settings", "tr": "Ayarlar"},
    "set_interval": {"en": "Check interval", "tr": "Kontrol aralığı"},
    "set_interval_val": {"en": "{s} s", "tr": "{s} sn"},
    "set_tiers":    {"en": "Giveaway tiers", "tr": "Çekiliş seviyeleri"},
    "open_page":    {"en": "Open the giveaway page when a new one appears",
                     "tr": "Yeni çekilişte çekiliş sayfasını aç"},
    "auto_join":    {"en": "Auto-join (only when eligible, never deposits money)",
                     "tr": "Otomatik katıl (yalnızca şart sağlanmışsa, asla para yatırmaz)"},
    "debug":        {"en": "DEBUG (write debug_payloads.json)",
                     "tr": "DEBUG (debug_payloads.json yaz)"},
    "set_note":     {"en": "Changes apply the next time you press START.",
                     "tr": "Değişiklikler bir sonraki BAŞLAT'ta geçerli olur."},
    "clear_log":    {"en": "Clear", "tr": "Temizle"},
    "update_available": {"en": "New version available: {tag}", "tr": "Yeni sürüm var: {tag}"},
    "update_btn":   {"en": "Update", "tr": "Güncelle"},

    # statuses (single live field)
    "st_ready":      {"en": "Ready", "tr": "Hazır"},
    "st_opening":    {"en": "Opening browser...", "tr": "Tarayıcı açılıyor..."},
    "st_wait_login": {"en": "Waiting for login...", "tr": "Giriş bekleniyor..."},
    "st_logged_in":  {"en": "Logged in, press Start", "tr": "Giriş yapıldı, Başlat'a bas"},
    "st_monitoring": {"en": "Running", "tr": "Çalışıyor"},
    "st_stopping":   {"en": "Stopping...", "tr": "Durduruluyor..."},
    "st_stopped":    {"en": "Stopped", "tr": "Durduruldu"},

    # log messages
    "log_ready": {"en": "Ready. Press START. The first time, log in with Steam in the browser "
                        "that opens; the bot remembers it.",
                  "tr": "Hazır. BAŞLAT'a bas. İlk seferde açılan tarayıcıda Steam ile giriş yap; "
                        "bot bunu hatırlar."},
    "log_page_fail": {"en": "Could not open page: {e}", "tr": "Sayfa açılamadı: {e}"},
    "log_browser_opened": {"en": "Browser opened.", "tr": "Tarayıcı açıldı."},
    "log_browser_fail": {"en": "Couldn't open the browser: {e}. If the bot's browser is already "
                               "open in another window, close it and try again.",
                         "tr": "Tarayıcı açılamadı: {e}. Botun tarayıcısı başka bir pencerede "
                               "açıksa kapatıp tekrar dene."},
    "log_installing_browser": {"en": "Downloading the browser (one time, about 150 MB)...",
                               "tr": "Tarayıcı indiriliyor (tek seferlik, yaklaşık 150 MB)..."},
    "log_browser_closed": {"en": "The bot's browser window was closed; bot stopped.",
                           "tr": "Botun tarayıcı penceresi kapatıldı; bot durdu."},
    "log_please_login": {"en": "Log in with Steam in the browser window. The bot continues "
                               "by itself after you log in.",
                         "tr": "Tarayıcı penceresinde Steam ile giriş yap. Giriş yapınca bot "
                               "kendiliğinden devam eder."},
    "log_logged_in": {"en": "Logged in: {name}", "tr": "Giriş yapıldı: {name}"},
    "log_press_start": {"en": "Press START to run the bot.",
                        "tr": "Botu çalıştırmak için BAŞLAT'a bas."},
    "log_monitor_start": {"en": "Bot started. Tiers={tiers}, interval={interval}s",
                          "tr": "Bot başladı. Seviyeler={tiers}, aralık={interval}s"},
    "log_page_fail_try": {"en": "Could not open page (attempt {a}/{n}): {e}",
                          "tr": "Sayfa açılamadı (deneme {a}/{n}): {e}"},
    "log_list_retry": {"en": "List didn't load, retrying... ({a}/{n})",
                       "tr": "Liste yüklenemedi, yeniden deneniyor... ({a}/{n})"},
    "log_list_fail": {"en": "List failed to load after several attempts (connection issue?); "
                            "will retry next round.",
                      "tr": "Liste birkaç denemede de yüklenemedi (bağlantı sorunu olabilir); "
                            "bir sonraki turda tekrar denenecek."},
    "log_no_records": {"en": "WARNING: No records captured. Enable DEBUG and share "
                             "debug_payloads.json.",
                       "tr": "UYARI: Hiç kayıt yakalanamadı. DEBUG'i açıp "
                             "debug_payloads.json'i paylaş."},
    "alert_joinable": {"en": "NEW GIVEAWAY!  {tier}  |  {summary}",
                       "tr": "YENİ ÇEKİLİŞ!  {tier}  |  {summary}"},
    "log_detail_open": {"en": "Detail page opened: {url}", "tr": "Detay sayfası açıldı: {url}"},
    "alert_auto_joined": {"en": "AUTO-JOINED: {clicked} (entries: {n})",
                          "tr": "OTOMATİK KATILDIN: {clicked} (giriş: {n})"},
    "log_join_btn_missing": {"en": "'Join giveaway' button not found (attempt {a}/{n}); "
                                   "will retry next round.",
                             "tr": "'Çekilişe katıl' butonu bulunamadı (deneme {a}/{n}); "
                                   "bir sonraki turda tekrar denenecek."},
    "log_join_unverified": {"en": "Clicked join but Keydrop doesn't show the entry yet "
                                  "(attempt {a}/{n}); will retry next round.",
                            "tr": "Katıl'a tıklandı ama Keydrop katılımı henüz göstermiyor "
                                  "(deneme {a}/{n}); bir sonraki turda tekrar denenecek."},
    "log_no_detail": {"en": "Couldn't read the giveaway details (attempt {a}/{n}); "
                            "will retry next round.",
                      "tr": "Çekiliş detayları okunamadı (deneme {a}/{n}); "
                            "bir sonraki turda tekrar denenecek."},
    "log_join_giveup": {"en": "Could not join after {n} attempts; skipping this giveaway.",
                        "tr": "{n} denemede katılınamadı; bu çekiliş atlanıyor."},
    "log_already_joined": {"en": "Already joined this giveaway (entries: {n}).",
                           "tr": "Bu çekilişe zaten katılmışsın (giriş: {n})."},
    "log_not_eligible": {"en": "Not eligible: Keydrop requires a {req} {cur} deposit within your "
                               "level's time window ({miss} {cur} missing). The bot never "
                               "deposits money; skipped.",
                         "tr": "Katılım şartı sağlanmıyor: Keydrop, seviyene göre belirlenen süre "
                               "içinde {req} {cur} yatırım istiyor ({miss} {cur} eksik). Bot asla "
                               "para yatırmaz; atlandı."},
    "log_logged_out": {"en": "Your Keydrop session ended! Log in again with Steam in the browser "
                             "window; the bot continues by itself.",
                       "tr": "Keydrop oturumun kapandı! Tarayıcı penceresinde Steam ile tekrar "
                             "giriş yap; bot kendiliğinden devam eder."},
    "log_back_to_list": {"en": "Returned to the giveaway list.",
                         "tr": "Çekiliş listesine geri dönüldü."},
    "log_detail_err": {"en": "Detail page/join error: {e}", "tr": "Detay sayfası/katılım hatası: {e}"},
    "log_no_new": {"en": "No new joinable giveaways. "
                         "(Joinable: {joinable}, joined: {joined}, total watched: {total})",
                   "tr": "Yeni katılınabilir çekiliş yok. "
                         "(Katılabilir: {joinable}, katıldıkların: {joined}, toplam izlenen: {total})"},
    "log_pick_tier": {"en": "Select at least one giveaway tier in Settings.",
                      "tr": "Ayarlar'dan en az bir çekiliş seviyesi seç."},
    "log_starting": {"en": "Starting...", "tr": "Başlatılıyor..."},
    "log_stop_req": {"en": "Stop requested...", "tr": "Durdurma istendi..."},
    "log_error": {"en": "ERROR: {e}", "tr": "HATA: {e}"},
    "log_update_stop_first": {"en": "Stop the bot first (close the browser), then press Update.",
                              "tr": "Önce botu durdur (tarayıcıyı kapat), sonra Güncelle'ye bas."},
    "log_updating": {"en": "Downloading {tag}...", "tr": "{tag} indiriliyor..."},
    "log_update_done": {"en": "Updated to {tag}. Restarting...",
                        "tr": "{tag} sürümüne güncellendi. Yeniden başlatılıyor..."},
    "log_update_fail": {"en": "Update failed: {e}", "tr": "Güncelleme başarısız: {e}"},

    # summarize() field labels
    "sum_tier":         {"en": "tier", "tr": "seviye"},
    "sum_status":       {"en": "status", "tr": "durum"},
    "sum_participants": {"en": "participants", "tr": "katılımcı"},
    "sum_remaining":    {"en": "remaining", "tr": "kalan"},
    "sum_deposit":      {"en": "deposit", "tr": "depozito"},
    "sum_joined":       {"en": "joined", "tr": "katıldım"},
    "sum_none":         {"en": "(see debug_payloads.json for details)",
                         "tr": "(detay için debug_payloads.json)"},
    "dl_expired":       {"en": "expired", "tr": "süresi doldu"},
}


def t(key, **kw):
    """Return the text for `key` in the active language, formatted with kwargs."""
    entry = TXT.get(key, {})
    s = entry.get(_LANG["v"]) or entry.get("en") or key
    if kw:
        try:
            s = s.format(**kw)
        except Exception:
            pass
    return s


# ----------------- SCHEMA-AGNOSTIC GIVEAWAY FINDER -----------------

def find_giveaways(node, found, watch_tiers):
    if isinstance(node, dict):
        matched = None
        for k, v in node.items():
            if isinstance(v, str) and v.strip().lower() in watch_tiers:
                if k.lower() in TIER_KEYS:
                    matched = v.strip().lower()
        if matched:
            gid = None
            for idk in ID_KEYS:
                if idk in node and isinstance(node[idk], (str, int)):
                    gid = str(node[idk])
                    break
            if gid is None:
                gid = str(abs(hash(json.dumps(node, sort_keys=True, default=str))))
            found[gid] = {"tier": matched, "data": node}
        for v in node.values():
            find_giveaways(v, found, watch_tiers)
    elif isinstance(node, list):
        for item in node:
            find_giveaways(item, found, watch_tiers)
    return found


def _fmt_deadline(ms):
    try:
        secs = int(ms) / 1000 - datetime.now().timestamp()
        if secs <= 0:
            return t("dl_expired")
        m, s = divmod(int(secs), 60)
        h, m = divmod(m, 60)
        if _LANG["v"] == "tr":
            return f"{h}sa {m}dk" if h else f"{m}dk {s}sn"
        return f"{h}h {m}m" if h else f"{m}m {s}s"
    except Exception:
        return str(ms)


def summarize(data):
    bits = []
    if "frequency" in data:
        bits.append(f"{t('sum_tier')}={data['frequency']}")
    if "status" in data:
        bits.append(f"{t('sum_status')}={data['status']}")
    if "participantCount" in data:
        mx = data.get("maxUsers", "?")
        bits.append(f"{t('sum_participants')}={data['participantCount']}/{mx}")
    if "deadlineTimestamp" in data:
        bits.append(f"{t('sum_remaining')}={_fmt_deadline(data['deadlineTimestamp'])}")
    if "depositAmountRequired" in data and data["depositAmountRequired"]:
        bits.append(f"{t('sum_deposit')}={data['depositAmountRequired']}"
                    f"{data.get('depositAmountCurrency','')}")
    if "haveIJoined" in data:
        bits.append(f"{t('sum_joined')}={data['haveIJoined']}")
    # fall back to the old generic method if no known field is present
    if not bits:
        for k in ("id", "slug", "code", "name", "title", "prize"):
            if k in data and not isinstance(data[k], (dict, list)):
                bits.append(f"{k}={data[k]}")
    return ", ".join(bits) if bits else t("sum_none")


def collect_giveaways(payloads, ws_frames, watch_tiers):
    """Collect watched-tier giveaways from captured API responses.

    First reads the known /giveaway/list response (body['data'] list);
    each record's 'frequency' (tier), 'status', 'haveIJoined' fields live there.
    If no structured data is found, falls back to schema-agnostic scanning.
    """
    found = {}

    def consider(node):
        if not isinstance(node, dict):
            return
        freq = node.get("frequency")
        if not isinstance(freq, str):
            return
        tier = freq.strip().lower()
        if tier not in watch_tiers:
            return
        gid = None
        for idk in ID_KEYS:
            val = node.get(idk)
            if isinstance(val, (str, int)):
                gid = str(val)
                break
        if gid is None:
            gid = str(abs(hash(json.dumps(node, sort_keys=True, default=str))))
        found[gid] = {"tier": tier, "data": node}

    structured = False
    for _url, body in payloads:
        if isinstance(body, dict) and isinstance(body.get("data"), list):
            structured = True
            for item in body["data"]:
                consider(item)

    # Fallback: if the API structure changed, use the old generic scan
    if not found and not structured:
        for _url, body in payloads:
            find_giveaways(body, found, watch_tiers)
        for frame in ws_frames:
            find_giveaways(frame, found, watch_tiers)

    return found


def _norm(s):
    """Reduce Turkish characters to ascii and lowercase (for safe matching)."""
    for a, b in (("İ", "i"), ("I", "i"), ("ı", "i"), ("Ş", "s"), ("ş", "s"),
                 ("Ç", "c"), ("ç", "c"), ("Ğ", "g"), ("ğ", "g"),
                 ("Ü", "u"), ("ü", "u"), ("Ö", "o"), ("ö", "o")):
        s = s.replace(a, b)
    return s.lower()

# Buttons that must never be clicked (paid / risky). 'join again' costs money,
# 'yatir' = deposit ("KATILMAK İÇİN 2,00 USD YATIR").
_BLOCKED_BUTTON_WORDS = ("tekrar", "yeniden", "again", "2x", "sans", "yatir")


async def try_join_free(detail_page):
    """On the detail page, click ONLY the free 'join giveaway' button.
    NEVER clicks 'join again' (paid) or similar buttons.
    Returns the button text if clicked, otherwise None."""
    for el in await detail_page.query_selector_all("button, [role=button]"):
        try:
            if not (await el.is_visible() and await el.is_enabled()):
                continue
            raw = (await el.inner_text()).strip()
        except Exception:
            continue
        if not raw or len(raw) > 45:
            continue
        tb = _norm(raw)
        if any(bad in tb for bad in _BLOCKED_BUTTON_WORDS):
            continue
        if "katil" in tb and "cekilis" in tb:   # "cekilise katil" (join giveaway)
            await el.click()
            return raw
    return None


def detail_url(data, gid):
    """Address of the giveaway's detail/join page. Derived from the organizer field;
    if missing, assumes it's Key-Drop's own giveaway ('keydrop')."""
    org = "keydrop"
    o = data.get("organizer")
    if isinstance(o, dict):
        for k in ("slug", "name", "code"):
            v = o.get(k)
            if isinstance(v, str) and v.strip():
                org = v.strip().lower()
                break
    elif isinstance(o, str) and o.strip():
        org = o.strip().lower()
    return DETAIL_URL.format(org=org, id=gid)


def got_list_data(payloads, strict=False):
    """Did the giveaway list API response arrive? (sign the page actually loaded)
    strict=True: only the official tier list counts, not e.g. the creator giveaway lists."""
    for url, body in payloads:
        if strict and LIST_API_PART not in url.lower():
            continue
        if isinstance(body, dict) and isinstance(body.get("data"), list):
            return True
    return False


def is_joinable(data):
    """Can I join this giveaway RIGHT NOW? (active + not yet joined + not expired)"""
    status = str(data.get("status", "")).strip().lower()
    if status in FINISHED_STATUSES:
        return False
    if data.get("haveIJoined") is True:
        return False
    dl = data.get("deadlineTimestamp")
    if isinstance(dl, (int, float)) and dl > 0:
        if dl / 1000 <= datetime.now().timestamp():
            return False
    return True


def q_log(text):
    ui_queue.put(("log", text))


def q_alert(text):
    ui_queue.put(("alert", text))


def q_status(key):
    """Send a status KEY (translated in the UI so it follows language changes)."""
    ui_queue.put(("status", key))


def q_account(state, name=None):
    """Account state for the UI: 'checking', 'in' (with Steam name) or 'out'."""
    ui_queue.put(("account", (state, name)))


# --------------------------- UPDATES ------------------------------

def _version_tuple(s):
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3])


def check_update():
    """Tell the UI if GitHub has a newer release (silently does nothing when offline)."""
    try:
        req = urllib.request.Request(RELEASES_API, headers={"User-Agent": "keydrop-bot"})
        with urllib.request.urlopen(req, timeout=10) as r:
            tag = json.load(r).get("tag_name", "")
        if tag and _version_tuple(tag) > _version_tuple(__version__):
            ui_queue.put(("update", tag))
    except Exception:
        pass


def apply_update(tag):
    """Download the release's files, check they are valid, then replace the local ones."""
    new = {}
    for path in UPDATE_FILES:
        req = urllib.request.Request(RAW_FILE_URL.format(tag=tag, path=path),
                                     headers={"User-Agent": "keydrop-bot"})
        with urllib.request.urlopen(req, timeout=30) as r:
            new[path] = r.read()
    compile(new["src/keydrop_ui.py"], "keydrop_ui.py", "exec")   # refuse a broken download
    for path, data in new.items():
        (ROOT / path).write_bytes(data)


# --------------------------- WORKER (async) ------------------------

def install_chromium():
    """Download Playwright's Chromium (KeydropBot.bat normally does this on the first run)."""
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


async def launch_browser(p):
    """Open Chromium with the saved profile; downloads Chromium once if it's missing."""
    kwargs = dict(headless=False, viewport={"width": 1280, "height": 800},
                  args=["--disable-blink-features=AutomationControlled"])
    try:
        return await p.chromium.launch_persistent_context(str(USER_DATA_DIR), **kwargs)
    except Exception as e:
        if "playwright install" not in str(e):
            raise
    q_log(t("log_installing_browser"))
    await asyncio.to_thread(install_chromium)
    return await p.chromium.launch_persistent_context(str(USER_DATA_DIR), **kwargs)


async def login_state(ctx):
    """(True, steam name) / (False, None) from the site's init data; None if unknown now
    (e.g. the user is on the Steam login page)."""
    page = next((pg for pg in ctx.pages if not pg.is_closed() and "keydrop.com" in pg.url), None)
    if page is None:
        return None
    try:
        res = await page.evaluate(_FETCH_JSON_JS, INIT_URL)
    except Exception:
        return None
    if not isinstance(res, dict):
        return None
    if res.get("status") is False:
        return False, None
    return True, res.get("userName")


async def session_main():
    """Open the browser, wait for the Steam login, then run the bot once Start is pressed."""
    payloads = []
    ws_frames = []
    details = {}       # giveaway id -> detail API data for the logged-in user
    session = {}       # "logged_in": True/False, read from the site's init data
    debug_other = []   # non-JSON / failed giveaway requests (helps when the list won't load)
    closed = {"v": False}

    def should_stop():
        return stop_event.is_set() or closed["v"]

    async def nap(secs):
        """Sleep that wakes up early when the session ends."""
        waited = 0.0
        while waited < secs and not should_stop():
            await asyncio.sleep(0.3)
            waited += 0.3

    async def handle_response(response):
        try:
            url = response.url.lower()
            ctype = response.headers.get("content-type", "").lower()
            if INIT_API_PART in url and "json" in ctype:
                body = await response.json()
                session["logged_in"] = not (isinstance(body, dict) and body.get("status") is False)
            elif "giveaway" in url:
                if "json" in ctype:
                    body = await response.json()
                    payloads.append((response.url, body))
                    if (DETAIL_API_PART in url and isinstance(body, dict)
                            and isinstance(body.get("data"), dict)):
                        details[str(body["data"].get("id"))] = body["data"]
                elif response.request.resource_type in ("fetch", "xhr"):
                    debug_other.append({"url": response.url, "status": response.status,
                                        "content_type": ctype})
        except Exception:
            pass

    def handle_failed(request):
        if "giveaway" in request.url.lower():
            debug_other.append({"url": request.url, "failed": request.failure})

    def handle_ws(ws):
        def on_frame(payload):
            try:
                ws_frames.append(json.loads(payload))
            except Exception:
                pass
        ws.on("framereceived", on_frame)

    async def wait_for_login(ctx):
        """Return True once Keydrop says we're logged in, False if the session ends first."""
        told = False
        while not should_stop():
            state = await login_state(ctx)
            if state is not None:
                ok, name = state
                if ok:
                    session["logged_in"] = True
                    q_account("in", name)
                    q_log(t("log_logged_in", name=name or "?"))
                    return True
                if not told:
                    q_account("out")
                    q_status("st_wait_login")
                    q_log(t("log_please_login"))
                    told = True
            await nap(LOGIN_POLL)
        return False

    async def wait_detail(gid, timeout=10):
        """Wait for the detail API data of `gid` (sent when its detail page loads)."""
        waited = 0.0
        while gid not in details and waited < timeout and not should_stop():
            await asyncio.sleep(0.5)
            waited += 0.5
        return details.get(gid)

    done = set()       # giveaways finished with (joined / not eligible / gave up / just opened)
    joined_ids = set() # giveaways Keydrop confirmed we're in
    attempts = {}      # giveaway id -> failed join attempts

    def failed_attempt(gid, key):
        attempts[gid] = attempts.get(gid, 0) + 1
        if attempts[gid] >= JOIN_ATTEMPTS:
            done.add(gid)
            q_log(t("log_join_giveup", n=JOIN_ATTEMPTS))
        else:
            q_log(t(key, a=attempts[gid], n=JOIN_ATTEMPTS))

    async def join_giveaway(page, gid):
        """On the already opened detail page: join `gid` only if Keydrop says we're
        eligible (deposit requirement already met, so joining is free), then verify."""
        info = await wait_detail(gid)
        if info is None:
            failed_attempt(gid, "log_no_detail")
            return
        if info.get("haveIJoined") is True:
            done.add(gid)
            joined_ids.add(gid)
            q_log(t("log_already_joined", n=info.get("myJoinCount", "?")))
            return
        missing = info.get("depositAmountMissing")
        if not isinstance(missing, (int, float)) or missing > 0:
            done.add(gid)
            q_log(t("log_not_eligible", req=info.get("depositAmountRequired"), miss=missing,
                    cur=info.get("depositAmountCurrency", "")))
            return
        # Eligible: wait for the join button to render (max 8 s), then click it.
        clicked = None
        for _ in range(16):
            clicked = await try_join_free(page)
            if clicked or should_stop():
                break
            await asyncio.sleep(0.5)
        if not clicked:
            failed_attempt(gid, "log_join_btn_missing")
            return
        # Don't trust the click: reload and ask Keydrop whether we're really in.
        await asyncio.sleep(2)
        details.pop(gid, None)
        await page.reload(wait_until="domcontentloaded")
        after = await wait_detail(gid)
        if after and after.get("haveIJoined") is True:
            done.add(gid)
            joined_ids.add(gid)
            ui_queue.put(("joined", t("alert_auto_joined", clicked=repr(clicked),
                                      n=after.get("myJoinCount", "?"))))
        else:
            failed_attempt(gid, "log_join_unverified")

    async def run_bot(ctx, page, config):
        watch_tiers = config["tiers"]
        interval = config["interval"]
        debug = config["debug"]
        open_page = config.get("open_page", True)
        auto_join = config.get("auto_join", False)

        notified = set()   # ids we already announced as new
        q_status("st_monitoring")
        q_log(t("log_monitor_start", tiers=sorted(watch_tiers), interval=interval))

        while not should_stop():
            if page.is_closed():          # the user closed the bot's tab
                closed["v"] = True
                break
            # Load the list page. If it doesn't load (internet etc.) retry a few times.
            loaded = False
            debug_other.clear()
            for attempt in range(1, LIST_LOAD_RETRIES + 1):
                payloads.clear()
                ws_frames.clear()
                try:
                    await page.goto(GIVEAWAYS_URL, wait_until="domcontentloaded")
                except Exception as e:
                    if should_stop():
                        break
                    q_log(t("log_page_fail_try", a=attempt, n=LIST_LOAD_RETRIES, e=e))
                # Wait for the official tier list (max LIST_LOAD_TIMEOUT s). Creator giveaway
                # lists often arrive first; stopping at those made the bot miss the tiers.
                waited = 0.0
                while (waited < LIST_LOAD_TIMEOUT and not got_list_data(payloads, strict=True)
                       and not should_stop()):
                    await asyncio.sleep(0.5)
                    waited += 0.5
                # Not strict here: if Keydrop renames the endpoint, still try what arrived.
                if got_list_data(payloads):
                    loaded = True
                    break
                if should_stop():
                    break
                q_log(t("log_list_retry", a=attempt, n=LIST_LOAD_RETRIES))
                await nap(2)

            if should_stop():
                break
            if not loaded:
                q_log(t("log_list_fail"))

            found = collect_giveaways(list(payloads), list(ws_frames), watch_tiers)

            if debug:
                try:
                    with open(DEBUG_FILE, "w", encoding="utf-8") as f:
                        json.dump({"http": payloads, "ws": ws_frames, "other": debug_other}, f,
                                  ensure_ascii=False, indent=2, default=str)
                except Exception:
                    pass

            # Session expired: say so, wait until the user logs in again, then go on.
            if session.get("logged_in") is False:
                q_alert(t("log_logged_out"))
                if not await wait_for_login(ctx):
                    break
                q_status("st_monitoring")
                continue

            if not found:
                q_log(t("log_no_records"))
            else:
                joinable = {g: i for g, i in found.items() if is_joinable(i["data"])}
                for gid, info in joinable.items():
                    if gid not in notified:
                        q_alert(t("alert_joinable", tier=info['tier'].upper(),
                                  summary=summarize(info['data'])))
                        notified.add(gid)

                # Soonest-ending first: an amateur giveaway lasts only a few minutes.
                def ends_at(g):
                    dl = joinable[g]["data"].get("deadlineTimestamp")
                    return dl if isinstance(dl, (int, float)) and dl > 0 else float("inf")
                pending = sorted((g for g in joinable if g not in done), key=ends_at)
                if not (open_page or auto_join):
                    done.update(pending)   # alert-only mode
                    pending = []

                # One giveaway per round: open its detail page, join it if Keydrop says
                # we're eligible, then RETURN to the giveaway list so monitoring continues.
                if pending:
                    gid = pending[0]
                    url = detail_url(joinable[gid]["data"], gid)
                    try:
                        details.pop(gid, None)
                        await page.goto(url, wait_until="domcontentloaded")
                        q_log(t("log_detail_open", url=url))
                        if auto_join:
                            await join_giveaway(page, gid)
                            await asyncio.sleep(1)
                            await page.goto(GIVEAWAYS_URL, wait_until="domcontentloaded")
                            q_log(t("log_back_to_list"))
                        else:
                            done.add(gid)
                    except Exception as e:
                        if not should_stop():
                            q_log(t("log_detail_err", e=e))

                if not pending:
                    q_log(t("log_no_new", joinable=len(joinable),
                            joined=len(joined_ids & set(found)), total=len(found)))

            ui_queue.put(("next_check", interval))   # countdown in the status line
            await nap(interval)

    async with async_playwright() as p:
        try:
            ctx = await launch_browser(p)
        except Exception as e:
            q_alert(t("log_browser_fail", e=str(e).strip().splitlines()[0] if str(e).strip() else e))
            return
        ctx.on("close", lambda _: closed.__setitem__("v", True))
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.on("response", lambda r: asyncio.create_task(handle_response(r)))
        page.on("requestfailed", handle_failed)
        page.on("websocket", handle_ws)
        try:
            try:
                await page.goto(GIVEAWAYS_URL, wait_until="domcontentloaded")
            except Exception as e:
                q_log(t("log_page_fail", e=e))
            q_log(t("log_browser_opened"))
            q_account("checking")
            if await wait_for_login(ctx):
                if not monitor_event.is_set():       # opened with "Log in": wait for Start
                    q_status("st_logged_in")
                    q_log(t("log_press_start"))
                while not monitor_event.is_set() and not should_stop():
                    await asyncio.sleep(0.3)
                if not should_stop():
                    await run_bot(ctx, page, dict(run_config))
        finally:
            if closed["v"] and not stop_event.is_set():
                q_alert(t("log_browser_closed"))
            try:
                await ctx.close()
            except Exception:
                pass


def worker_main():
    try:
        asyncio.run(session_main())
    except Exception as e:
        q_log(t("log_error", e=e))
    finally:
        ui_queue.put(("stopped", None))


# ------------------------------- UI --------------------------------

# Dark theme in Keydrop's style: near-black background, cards, lime accent.
BG = "#0f1115"
CARD = "#181b22"
CARD_2 = "#222632"
BORDER = "#2a2f3a"
SEG_ON = "#3a4150"
SEG_ON_HOVER = "#454d5e"
SEG_OFF_HOVER = "#2c313d"
TEXT = "#f2f4f8"
MUTED = "#8b93a7"
FEED = "#c3c9d6"
ACCENT = "#c8f25f"
ACCENT_HOVER = "#b4dd4b"
ON_ACCENT = "#11150a"
DANGER = "#ff5c5c"
DANGER_HOVER = "#e04848"
SUCCESS = "#3ddc84"
SUCCESS_DIM = "#1f6e45"
WARN = "#ffb020"

STATUS_COLORS = {"st_ready": MUTED, "st_opening": WARN, "st_wait_login": WARN,
                 "st_logged_in": SUCCESS, "st_monitoring": SUCCESS,
                 "st_stopping": WARN, "st_stopped": MUTED}

DEFAULT_SETTINGS = {"interval": 25, "tiers": ["amateur"], "auto_join": True,
                    "open_page": True, "debug": False, "lang": None}


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    except Exception:
        pass
    return settings


def font(size, bold=False):
    return ctk.CTkFont(size=size, weight="bold" if bold else "normal")


class App:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        if self.settings.get("lang") in ("tr", "en"):
            _LANG["v"] = self.settings["lang"]

        # state (kept as keys so texts follow language changes)
        self.status_key = "st_ready"
        self.account = ("unknown", None)
        self.joined = 0
        self.last_join = None
        self.running = False        # a browser session is open
        self.monitoring = False     # START was pressed for this session
        self.stopping = False
        self.closing = False
        self.update_tag = None
        self.next_check_at = None
        self.pulse = False

        # As tall as the screen comfortably allows (small laptops: 600, big screens: 760).
        try:
            usable = root.winfo_screenheight() / ctk.ScalingTracker.get_window_scaling(root) - 90
        except Exception:
            usable = 680
        root.geometry(f"540x{int(min(760, max(600, usable)))}")
        root.minsize(480, 600)
        root.configure(fg_color=BG)
        try:
            root.iconbitmap(str(ICON_FILE))
        except Exception:
            pass

        body = ctk.CTkFrame(root, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=14)

        # --- header: logo, name, version, language ---
        head = ctk.CTkFrame(body, fg_color="transparent")
        head.pack(fill="x")
        try:
            logo = Image.open(ICON_FILE)
            self.logo = ctk.CTkImage(light_image=logo, dark_image=logo, size=(34, 34))
            ctk.CTkLabel(head, image=self.logo, text="").pack(side="left")
        except Exception:
            pass
        ctk.CTkLabel(head, text="Keydrop Bot", font=font(20, True),
                     text_color=TEXT).pack(side="left", padx=(10, 8))
        ctk.CTkLabel(head, text=f" v{__version__} ", font=font(11), text_color=MUTED,
                     fg_color=CARD_2, corner_radius=8, height=22).pack(side="left")
        self.lang_seg = ctk.CTkSegmentedButton(
            head, values=["TR", "EN"], command=self.on_lang, width=96, height=30,
            font=font(12, True), fg_color=CARD_2, selected_color=SEG_ON,
            selected_hover_color=SEG_ON_HOVER, unselected_color=CARD_2,
            unselected_hover_color=SEG_OFF_HOVER, text_color=TEXT)
        self.lang_seg.set(_LANG["v"].upper())
        self.lang_seg.pack(side="right")

        # --- update banner (only when a newer release exists) ---
        self.update_bar = ctk.CTkFrame(body, fg_color=ACCENT, corner_radius=12)
        self.update_lbl = ctk.CTkLabel(self.update_bar, text="", text_color=ON_ACCENT,
                                       font=font(13, True))
        self.update_lbl.pack(side="left", padx=14, pady=8)
        self.update_btn = ctk.CTkButton(self.update_bar, text="", command=self.on_update,
                                        width=100, height=32, corner_radius=10,
                                        fg_color=ON_ACCENT, hover_color="#2a3317",
                                        text_color=ACCENT, font=font(13, True))
        self.update_btn.pack(side="right", padx=10, pady=8)

        # --- account card ---
        self.acc_card = ctk.CTkFrame(body, fg_color=CARD, corner_radius=16,
                                     border_width=1, border_color=BORDER)
        self.acc_card.pack(fill="x", pady=(14, 0))
        self.acc_card.grid_columnconfigure(1, weight=1)
        self.avatar = ctk.CTkLabel(self.acc_card, text="?", width=48, height=48,
                                   corner_radius=24, fg_color=CARD_2, text_color=TEXT,
                                   font=font(18, True))
        self.avatar.grid(row=0, column=0, rowspan=2, padx=14, pady=14)
        self.acc_name = ctk.CTkLabel(self.acc_card, text="", font=font(16, True),
                                     text_color=TEXT, anchor="w")
        self.acc_name.grid(row=0, column=1, sticky="sw", pady=(14, 0))
        self.acc_sub = ctk.CTkLabel(self.acc_card, text="", font=font(12), text_color=MUTED,
                                    anchor="w", justify="left", wraplength=270)
        self.acc_sub.grid(row=1, column=1, sticky="nw", pady=(0, 14))
        self.badge = ctk.CTkLabel(self.acc_card, text="", font=font(12, True),
                                  text_color=SUCCESS, fg_color=CARD_2, corner_radius=10,
                                  height=28, width=96)
        self.login_btn = ctk.CTkButton(self.acc_card, text="", command=self.on_login,
                                       width=110, height=36, corner_radius=10,
                                       fg_color="transparent", border_width=2,
                                       border_color=ACCENT, text_color=ACCENT,
                                       text_color_disabled=MUTED, hover_color=CARD_2,
                                       font=font(13, True))

        # --- the one big button ---
        self.big_btn = ctk.CTkButton(body, text="", command=self.on_big, height=62,
                                     corner_radius=16, font=font(20, True))
        self.big_btn.pack(fill="x", pady=(16, 0))

        # --- status line + safety note ---
        st = ctk.CTkFrame(body, fg_color="transparent")
        st.pack(pady=(10, 0))
        self.dot = ctk.CTkLabel(st, text="●", font=font(16), text_color=MUTED)
        self.dot.pack(side="left")
        self.status_lbl = ctk.CTkLabel(st, text="", font=font(14, True), text_color=TEXT)
        self.status_lbl.pack(side="left", padx=(6, 6))
        self.countdown_lbl = ctk.CTkLabel(st, text="", font=font(13), text_color=MUTED)
        self.countdown_lbl.pack(side="left")
        self.safety_lbl = ctk.CTkLabel(body, text="", font=font(11), text_color=MUTED)
        self.safety_lbl.pack()

        # --- stats: joined / last join / watching ---
        stats = ctk.CTkFrame(body, fg_color="transparent")
        stats.pack(fill="x", pady=(10, 0))
        self.stat_vals, self.stat_caps = [], []
        for i in range(3):
            stats.grid_columnconfigure(i, weight=1, uniform="stats")
            card = ctk.CTkFrame(stats, fg_color=CARD, corner_radius=14,
                                border_width=1, border_color=BORDER)
            card.grid(row=0, column=i, sticky="nsew",
                      padx=(0 if i == 0 else 5, 0 if i == 2 else 5))
            val = ctk.CTkLabel(card, text="", font=font(20, True), text_color=TEXT)
            val.pack(pady=(10, 0))
            cap = ctk.CTkLabel(card, text="", font=font(11), text_color=MUTED)
            cap.pack(pady=(0, 8))
            self.stat_vals.append(val)
            self.stat_caps.append(cap)

        # --- tabs: activity feed / settings ---
        self.tabs = ctk.CTkTabview(
            body, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER,
            segmented_button_fg_color=CARD_2, segmented_button_selected_color=SEG_ON,
            segmented_button_selected_hover_color=SEG_ON_HOVER,
            segmented_button_unselected_color=CARD_2,
            segmented_button_unselected_hover_color=SEG_OFF_HOVER, text_color=TEXT)
        self.tabs.pack(fill="both", expand=True, pady=(12, 0))
        self.tab_names = {"activity": t("tab_activity"), "settings": t("tab_settings")}
        act = self.tabs.add(self.tab_names["activity"])
        setf = self.tabs.add(self.tab_names["settings"])

        self.feed = ctk.CTkTextbox(act, fg_color=CARD, text_color=FEED, font=font(12),
                                   wrap="word", border_width=0, activate_scrollbars=True,
                                   scrollbar_button_color=BORDER,
                                   scrollbar_button_hover_color=SEG_ON)
        self.feed.pack(fill="both", expand=True)
        for tag, color in (("time", MUTED), ("info", FEED), ("alert", WARN), ("success", SUCCESS)):
            self.feed.tag_config(tag, foreground=color)
        self.feed.configure(state="disabled")
        self.clear_btn = ctk.CTkButton(act, text="", command=self.clear_log, width=80, height=28,
                                       corner_radius=8, fg_color="transparent",
                                       hover_color=CARD_2, text_color=MUTED, font=font(12))
        self.clear_btn.pack(anchor="e", pady=(4, 2))

        sett = ctk.CTkScrollableFrame(setf, fg_color="transparent",
                                      scrollbar_button_color=BORDER,
                                      scrollbar_button_hover_color=SEG_ON)
        sett.pack(fill="both", expand=True)
        sett.grid_columnconfigure((0, 1), weight=1)
        self.int_lbl = ctk.CTkLabel(sett, text="", font=font(13, True), text_color=TEXT)
        self.int_lbl.grid(row=0, column=0, sticky="w", padx=4, pady=(4, 0))
        self.int_val = ctk.CTkLabel(sett, text="", font=font(13, True), text_color=ACCENT)
        self.int_val.grid(row=0, column=1, sticky="e", padx=4, pady=(4, 0))
        self.slider = ctk.CTkSlider(sett, from_=10, to=120, number_of_steps=22,
                                    command=self.on_interval, fg_color=CARD_2,
                                    progress_color=ACCENT, button_color=ACCENT,
                                    button_hover_color=ACCENT_HOVER)
        self.slider.set(min(120, max(10, int(self.settings.get("interval", 25)))))
        self.slider.grid(row=1, column=0, columnspan=2, sticky="ew", padx=4, pady=(4, 12))

        self.tiers_lbl = ctk.CTkLabel(sett, text="", font=font(13, True), text_color=TEXT)
        self.tiers_lbl.grid(row=2, column=0, columnspan=2, sticky="w", padx=4)
        self.tier_vars = {}
        saved_tiers = set(self.settings.get("tiers") or [])
        for i, tier in enumerate(ALL_TIERS):
            v = tk.BooleanVar(value=tier in saved_tiers)
            self.tier_vars[tier] = v
            ctk.CTkCheckBox(sett, text=tier.capitalize(), variable=v, command=self.update_stats,
                            font=font(13), text_color=TEXT, fg_color=ACCENT,
                            hover_color=ACCENT_HOVER, checkmark_color=ON_ACCENT,
                            border_color=MUTED, corner_radius=6, border_width=2
                            ).grid(row=3 + i // 2, column=i % 2, sticky="w", padx=4, pady=4)

        self.autojoin_var = tk.BooleanVar(value=bool(self.settings.get("auto_join", True)))
        self.openpage_var = tk.BooleanVar(value=bool(self.settings.get("open_page", True)))
        self.debug_var = tk.BooleanVar(value=bool(self.settings.get("debug", False)))
        self.switches = {}
        for row, (key, var) in enumerate((("auto_join", self.autojoin_var),
                                          ("open_page", self.openpage_var),
                                          ("debug", self.debug_var)), start=5):
            sw = ctk.CTkSwitch(sett, text="", variable=var, font=font(13), text_color=TEXT,
                               fg_color=CARD_2, progress_color=ACCENT, button_color=TEXT,
                               button_hover_color="#ffffff")
            sw.grid(row=row, column=0, columnspan=2, sticky="w", padx=4, pady=(10 if row == 5 else 4, 4))
            self.switches[key] = sw
        self.note_lbl = ctk.CTkLabel(sett, text="", font=font(11), text_color=MUTED)
        self.note_lbl.grid(row=8, column=0, columnspan=2, sticky="w", padx=4, pady=(8, 4))

        self.retranslate()
        self.append(t("log_ready"))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(150, self.poll_queue)
        root.after(700, self.tick)
        threading.Thread(target=check_update, daemon=True).start()

    # --- texts / language ---
    def on_lang(self, value):
        _LANG["v"] = value.lower()
        self.save_settings()
        self.retranslate()

    def retranslate(self):
        """Re-apply every widget's text in the active language."""
        self.root.title(t("win_title"))
        for key in ("activity", "settings"):
            new = t(f"tab_{key}")
            if new != self.tab_names[key]:
                self.tabs.rename(self.tab_names[key], new)
                self.tab_names[key] = new
        self.update_btn.configure(text=t("update_btn"))
        if self.update_tag:
            self.update_lbl.configure(text=t("update_available", tag=self.update_tag))
        self.login_btn.configure(text=t("login"))
        self.badge.configure(text=t("badge_in"))
        self.safety_lbl.configure(text=t("safety"))
        for cap, key in zip(self.stat_caps, ("stat_joined", "stat_last", "stat_tiers")):
            cap.configure(text=t(key))
        self.clear_btn.configure(text=t("clear_log"))
        self.int_lbl.configure(text=t("set_interval"))
        self.on_interval(self.slider.get())
        self.tiers_lbl.configure(text=t("set_tiers"))
        for key, sw in self.switches.items():
            sw.configure(text=t(key))
        self.note_lbl.configure(text=t("set_note"))
        self.set_status(self.status_key)
        self.set_account(*self.account)
        self.update_stats()
        self.refresh_controls()

    def set_status(self, key):
        self.status_key = key
        self.status_lbl.configure(text=t(key))
        self.dot.configure(text_color=STATUS_COLORS.get(key, MUTED))
        if key != "st_monitoring":
            self.countdown_lbl.configure(text="")

    def set_account(self, state, name=None):
        self.account = (state, name)
        if state == "in":
            self.avatar.configure(text=(name or "?")[:1].upper(), fg_color=ACCENT,
                                  text_color=ON_ACCENT)
            self.acc_name.configure(text=name or t("acc_title_in"), text_color=TEXT)
            self.acc_sub.configure(text=t("acc_sub_in"))
            self.login_btn.grid_remove()
            self.badge.grid(row=0, column=2, rowspan=2, padx=14)
            return
        titles = {"checking": ("acc_title_checking", "acc_sub_checking"),
                  "out": ("acc_title_out", "acc_sub_out")}
        title, sub = titles.get(state, ("acc_title_unknown", "acc_sub_unknown"))
        self.avatar.configure(text="?", fg_color=CARD_2, text_color=TEXT)
        self.acc_name.configure(text=t(title), text_color=DANGER if state == "out" else TEXT)
        self.acc_sub.configure(text=t(sub))
        self.badge.grid_remove()
        self.login_btn.grid(row=0, column=2, rowspan=2, padx=14)

    def refresh_controls(self):
        """Big button: START (lime) / STOP (red) / Stopping (grey)."""
        if self.stopping:
            self.big_btn.configure(text=t("big_stopping"), state="disabled", fg_color=CARD_2,
                                   text_color_disabled=MUTED)
        elif self.running and self.monitoring:
            self.big_btn.configure(text=t("big_stop"), state="normal", fg_color=DANGER,
                                   hover_color=DANGER_HOVER, text_color="#ffffff")
        else:
            self.big_btn.configure(text=t("big_start"), state="normal", fg_color=ACCENT,
                                   hover_color=ACCENT_HOVER, text_color=ON_ACCENT)
        self.login_btn.configure(state="disabled" if self.running else "normal")

    def update_stats(self):
        chosen = [tier.capitalize() for tier in ALL_TIERS if self.tier_vars[tier].get()]
        watching = "—" if not chosen else chosen[0] + (f" +{len(chosen) - 1}" if len(chosen) > 1 else "")
        for lbl, value in zip(self.stat_vals, (str(self.joined), self.last_join or "—", watching)):
            lbl.configure(text=value)

    def on_interval(self, value):
        self.int_val.configure(text=t("set_interval_val", s=int(round(float(value)))))

    def tick(self):
        """Pulsing dot + 'next check in Xs' while the bot runs."""
        if self.status_key == "st_monitoring":
            self.pulse = not self.pulse
            self.dot.configure(text_color=SUCCESS if self.pulse else SUCCESS_DIM)
            if self.next_check_at:
                left = int(self.next_check_at - time.time() + 0.999)
                self.countdown_lbl.configure(
                    text=t("next_check", s=left) if left > 0 else t("checking_now"))
        self.root.after(700, self.tick)

    # --- feed ---
    def append(self, text, tag="info"):
        self.feed.configure(state="normal")
        self.feed.insert("end", f"{datetime.now():%H:%M:%S}  ", "time")
        self.feed.insert("end", f"{text}\n", tag)
        self.feed.see("end")
        self.feed.configure(state="disabled")

    def clear_log(self):
        self.feed.configure(state="normal")
        self.feed.delete("1.0", "end")
        self.feed.configure(state="disabled")

    # --- settings ---
    def read_config(self):
        tiers = {tier for tier, v in self.tier_vars.items() if v.get()}
        if not tiers:
            self.append(t("log_pick_tier"), "alert")
            self.tabs.set(self.tab_names["settings"])
            return None
        return {
            "tiers": tiers,
            "interval": min(120, max(10, int(round(self.slider.get())))),
            "debug": self.debug_var.get(),
            "open_page": self.openpage_var.get(),
            "auto_join": self.autojoin_var.get(),
        }

    def save_settings(self):
        data = {
            "interval": int(round(self.slider.get())),
            "tiers": [tier for tier in ALL_TIERS if self.tier_vars[tier].get()],
            "auto_join": self.autojoin_var.get(),
            "open_page": self.openpage_var.get(),
            "debug": self.debug_var.get(),
            "lang": _LANG["v"],
        }
        try:
            SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    # --- actions ---
    def start_session(self):
        global worker_thread
        stop_event.clear()
        worker_thread = threading.Thread(target=worker_main, daemon=True)
        worker_thread.start()
        self.running = True
        self.set_status("st_opening")

    def on_big(self):
        if self.stopping:
            return
        if self.running and self.monitoring:
            self.on_stop()
        else:
            self.on_start()

    def on_login(self):
        if self.running:
            return
        monitor_event.clear()
        self.monitoring = False
        self.start_session()
        self.refresh_controls()

    def on_start(self):
        config = self.read_config()
        if config is None:
            return
        self.save_settings()
        run_config.clear()
        run_config.update(config)
        monitor_event.set()
        self.monitoring = True
        self.next_check_at = None
        self.append(t("log_starting"))
        if not self.running:
            self.start_session()
        self.refresh_controls()

    def on_stop(self):
        stop_event.set()
        monitor_event.clear()
        self.stopping = True
        self.set_status("st_stopping")
        self.append(t("log_stop_req"))
        self.refresh_controls()

    def on_close(self):
        self.save_settings()
        if not self.running:
            self.destroy_once()
            return
        # Let the worker close the browser cleanly (so the login is saved), max 15 s.
        self.closing = True
        self.on_stop()
        self.root.after(15000, self.destroy_once)

    def destroy_once(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def on_update(self):
        if self.running:
            self.append(t("log_update_stop_first"), "alert")
            return
        tag = self.update_tag
        self.update_btn.configure(state="disabled")
        self.append(t("log_updating", tag=tag))

        def work():
            try:
                apply_update(tag)
                ui_queue.put(("update_done", tag))
            except Exception as e:
                ui_queue.put(("update_fail", str(e)))
        threading.Thread(target=work, daemon=True).start()

    # --- queue listener ---
    def poll_queue(self):
        try:
            while True:
                kind, payload = ui_queue.get_nowait()
                if kind == "log":
                    self.append(payload)
                elif kind == "alert":
                    self.append(payload, "alert")
                elif kind == "joined":
                    self.append(payload, "success")
                    self.joined += 1
                    self.last_join = datetime.now().strftime("%H:%M")
                    self.update_stats()
                elif kind == "status":
                    self.set_status(payload)
                elif kind == "account":
                    self.set_account(*payload)
                elif kind == "next_check":
                    self.next_check_at = time.time() + payload
                elif kind == "update":
                    self.update_tag = payload
                    self.update_lbl.configure(text=t("update_available", tag=payload))
                    self.update_bar.pack(fill="x", pady=(12, 0), before=self.acc_card)
                elif kind == "update_done":
                    self.append(t("log_update_done", tag=payload), "success")
                    if LAUNCHER.exists():
                        os.startfile(str(LAUNCHER))
                    self.root.after(1500, self.destroy_once)
                elif kind == "update_fail":
                    self.append(t("log_update_fail", e=payload), "alert")
                    self.update_btn.configure(state="normal")
                elif kind == "stopped":
                    # The worker has fully exited (Stop, closed browser, or an error).
                    self.running = self.monitoring = self.stopping = False
                    self.next_check_at = None
                    self.set_status("st_stopped")
                    if self.account[0] == "checking":
                        self.set_account("unknown")
                    self.refresh_controls()
                    if self.closing:
                        self.destroy_once()
                        return
        except queue.Empty:
            pass
        self.root.after(150, self.poll_queue)


def main():
    try:   # own taskbar icon instead of Python's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("KeydropBot")
    except Exception:
        pass
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
