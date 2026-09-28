#!/usr/bin/env python3
"""
Wochenkarte Grossartig – vollautomatischer Ablauf (läuft als GitHub Action, ohne Mac)

1. Holt aus Gmail (IMAP, App-Passwort) den neuesten, noch nicht bearbeiteten Newsletter
   von newsletter@bistro-grossartig.at.
2. Sucht darin die Wochenkarte: Bild-Anhang, eingebettetes Bild, verlinktes Bild oder PDF
   (bei mehrseitigen PDFs die Seite mit den Wochentagen).
3. Liest das Datum ("Tagesteller vom … bis …") und prüft, ob es die kommende/laufende Woche ist.
4. Entzerrt bei Bedarf, schneidet nach der Freitags-Zeile ab und bereitet das Bild auf
   (wochenkarte_verarbeiten.py).
5. Prüft das Ergebnis (alle Wochentage lesbar, Maße plausibel).
6. Ersetzt wochenkarte.jpg im Repo, legt eine Kopie in archiv/ ab, committet und pusht.
7. Wartet, bis GitHub Pages genau dieses Bild ausliefert (SHA-256), und prüft, dass die
   Website es einbindet.
8. Setzt in Gmail das Label "Wochenkarte/erledigt" (bzw. "Problem"/"keine-Karte") und
   schickt David eine Mail – bei Erfolg mit der fertigen Karte, bei Problemen mit Details.

Umgebungsvariablen: GMAIL_USER, GMAIL_APP_PASSWORD, NOTIFY_TO, NEWSLETTER_FROM,
PAGES_URL, SITE_URL, GITHUB_TOKEN, GITHUB_REPOSITORY, TROCKEN ("1" = nichts committen,
Ergebnis-Mail nur an GMAIL_USER).
"""
import datetime as dt
import email
import hashlib
import imaplib
import io
import json
import os
import re
import smtplib
import subprocess
import sys
import tempfile
import time
import traceback
import unicodedata
import urllib.request
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wochenkarte_verarbeiten as wv  # noqa: E402

WIEN = ZoneInfo("Europe/Vienna")
GMAIL_USER = os.environ.get("GMAIL_USER", "haberldavid390@gmail.com")
GMAIL_PW = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
NOTIFY_TO = os.environ.get("NOTIFY_TO", "davidhaberl.ai@hotmail.com")
ABSENDER = os.environ.get("NEWSLETTER_FROM", "newsletter@bistro-grossartig.at")
PAGES_URL = os.environ.get("PAGES_URL",
                           "https://davidhaberlai.github.io/grossartig-wochenkarte/wochenkarte.jpg")
SITE_URL = os.environ.get("SITE_URL", "https://bistro-grossartig.netlify.app/")
TROCKEN = os.environ.get("TROCKEN", "0") == "1"
REPO_DIR = os.environ.get("GITHUB_WORKSPACE", os.getcwd())

L_ERLEDIGT, L_PROBLEM, L_KEINE = "Wochenkarte/erledigt", "Wochenkarte/Problem", "Wochenkarte/keine-Karte"
TAGE = ["MONTAG", "DIENSTAG", "MITTWOCH", "DONNERSTAG", "FREITAG"]
MONATE = {"JANNER": 1, "JANUAR": 1, "FEBRUAR": 2, "FEBER": 2, "MARZ": 3, "APRIL": 4, "MAI": 5,
          "JUNI": 6, "JULI": 7, "AUGUST": 8, "SEPTEMBER": 9, "OKTOBER": 10, "NOVEMBER": 11,
          "DEZEMBER": 12}
MONATSNAME = {1: "Jänner", 2: "Februar", 3: "März", 4: "April", 5: "Mai", 6: "Juni", 7: "Juli",
              8: "August", 9: "September", 10: "Oktober", 11: "November", 12: "Dezember"}


class InhaltsProblem(Exception):
    """Problem mit der Mail selbst (falsche Woche, kein Bild …) – Mail wird als Problem markiert."""


def log(*a):
    print(*a, flush=True)


def norm(t):
    t = unicodedata.normalize("NFKD", t.upper())
    return "".join(c for c in t if not unicodedata.combining(c))


# ---------------------------------------------------------------- Gmail (IMAP/SMTP)

def imap_verbinden():
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    m.login(GMAIL_USER, GMAIL_PW)
    # Ordner "Alle Nachrichten" finden (Name ist sprachabhängig)
    typ, daten = m.list()
    alle = None
    for zeile in daten:
        z = zeile.decode(errors="replace")
        if "\\All" in z:
            alle = z.split(' "/" ')[-1].strip()
    m.select(alle or "INBOX")
    return m


def gm_suche(m, raw):
    typ, daten = m.uid("SEARCH", "X-GM-RAW", '"' + raw.replace('"', '\\"') + '"')
    return [u for u in daten[0].split()] if daten and daten[0] else []


def label_setzen(m, uid, label):
    if TROCKEN:
        log(f"[trocken] Label {label} würde gesetzt")
        return
    m.uid("STORE", uid, "+X-GM-LABELS", f'("{label}")')


def mail_senden(an, betreff, text, anhaenge=()):
    msg = EmailMessage()
    msg["From"] = f"Wochenkarte-Automatik <{GMAIL_USER}>"
    msg["To"] = an
    msg["Subject"] = betreff
    msg.set_content(text)
    for name, daten, mime in anhaenge:
        haupt, neben = mime.split("/")
        msg.add_attachment(daten, maintype=haupt, subtype=neben, filename=name)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_PW)
        s.send_message(msg)
    log("Mail gesendet:", betreff)


def problem_melden(m, betreff_zusatz, text, anhaenge=()):
    betreff = f"Wochenkarte Grossartig: Problem – {betreff_zusatz}"
    # dieselbe Meldung höchstens einmal pro 20 Stunden
    try:
        schon = gm_suche(m, f'in:sent subject:"{betreff}" newer_than:1d')
    except Exception:
        schon = []
    if schon:
        log("Problem-Mail wurde heute schon geschickt – keine zweite:", betreff)
        return
    fuss = ("\n\nAuf der Website bleibt so lange die bisherige Wochenkarte sichtbar.\n"
            "Die Karte von Hand tauschen: github.com/davidhaberlai/grossartig-wochenkarte → "
            "wochenkarte.jpg ersetzen (Add file → Upload files → Commit changes).\n\n"
            "– Wochenkarte-Automatik (GitHub Action)")
    mail_senden(GMAIL_USER if TROCKEN else NOTIFY_TO, betreff, text + fuss, anhaenge)


# ---------------------------------------------------------------- Bilder aus der Mail

def kandidaten(msg):
    """Liefert (name, bytes) aller Bild-/PDF-Kandidaten der Mail."""
    out = []
    html = ""
    for teil in msg.walk():
        ct = teil.get_content_type()
        name = teil.get_filename() or ""
        try:
            name = str(make_header(decode_header(name)))
        except Exception:
            pass
        if ct == "text/html" and not name:
            html += teil.get_payload(decode=True).decode(teil.get_content_charset() or "utf-8",
                                                          errors="replace")
            continue
        endung = os.path.splitext(name.lower())[1]
        if ct.startswith("image/") or ct == "application/pdf" or endung in (
                ".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"):
            daten = teil.get_payload(decode=True)
            if daten and len(daten) > 15000:
                if not endung:
                    endung = ".pdf" if ct == "application/pdf" else "." + ct.split("/")[-1]
                out.append((name or f"anhang{len(out)+1}{endung}", daten))
    # verlinkte Bilder im HTML
    for url in re.findall(r'<img[^>]+src="(https?://[^"]+)"', html, flags=re.I):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            daten = urllib.request.urlopen(req, timeout=30).read()
            if len(daten) > 30000:
                endung = os.path.splitext(url.split("?")[0].lower())[1] or ".jpg"
                out.append((f"verlinkt{len(out)+1}{endung}", daten))
        except Exception as e:
            log("verlinktes Bild nicht ladbar:", url, e)
    return out, html


def seiten_bilder(name, daten):
    """Ein Kandidat → Liste von (bezeichnung, PIL-Bild). PDFs werden seitenweise gerendert."""
    tmp = tempfile.mkdtemp()
    pfad = os.path.join(tmp, re.sub(r"[^\w.\-]", "_", name))
    with open(pfad, "wb") as f:
        f.write(daten)
    if pfad.lower().endswith(".pdf"):
        return [(f"{name} S.{i+1}", wv.laden(p)) for i, p in enumerate(wv.pdf_seiten(pfad))]
    return [(name, wv.laden(pfad))]


def ocr_zeilen(im):
    """Textzeilen mit y-Positionen (Tesseract, Deutsch)."""
    import csv
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
    im.save(tmp)
    r = subprocess.run(["tesseract", tmp, "stdout", "-l", "deu", "--psm", "4", "tsv"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        r = subprocess.run(["tesseract", tmp, "stdout", "--psm", "4", "tsv"],
                           capture_output=True, text=True)
    zeilen = {}
    for row in csv.DictReader(io.StringIO(r.stdout), delimiter="\t"):
        if row.get("level") != "5" or not (row.get("text") or "").strip():
            continue
        key = (row["block_num"], row["par_num"], row["line_num"])
        top, h = int(row["top"]), int(row["height"])
        z = zeilen.setdefault(key, {"top": top, "bottom": top + h, "w": []})
        z["top"] = min(z["top"], top)
        z["bottom"] = max(z["bottom"], top + h)
        z["w"].append(row["text"])
    out = [{"top": z["top"], "bottom": z["bottom"], "text": " ".join(z["w"])}
           for z in zeilen.values()]
    return sorted(out, key=lambda z: z["top"])


def tage_gefunden(zeilen):
    txt = norm(" ".join(z["text"] for z in zeilen))
    return [t for t in TAGE if t in txt]


# ---------------------------------------------------------------- Datum & Zuschnitt

def datum_lesen(zeilen, bezug):
    """Sucht 'vom 5. bis 9. Oktober 2026' o. ä. Gibt (montag, freitag) oder None."""
    txt = norm(" ".join(z["text"] for z in zeilen))
    txt = txt.replace("–", "-").replace("—", "-")
    muster = (r"(\d{1,2})\s*\.?\s*([A-Z]+)?\s*(\d{4})?\s*(?:BIS|-)\s*"
              r"(\d{1,2})\s*\.?\s*([A-Z]+)\s*(\d{4})?")
    for mt in re.finditer(muster, txt):
        d1, m1, j1, d2, m2, j2 = mt.groups()
        if m2 not in MONATE or (m1 and m1 not in MONATE):
            continue
        jahr = int(j2 or j1 or bezug.year)
        mon2 = MONATE[m2]
        mon1 = MONATE[m1] if m1 else mon2
        try:
            von = dt.date(jahr if mon1 <= mon2 else jahr - 1, mon1, int(d1))
            bis = dt.date(jahr, mon2, int(d2))
        except ValueError:
            continue
        if 0 <= (bis - von).days <= 6:
            return von, bis
    return None


def zeitraum_text(von, bis):
    if von.month == bis.month:
        return f"{von.day}.–{bis.day}. {MONATSNAME[bis.month]} {bis.year}"
    return f"{von.day}. {MONATSNAME[von.month]} – {bis.day}. {MONATSNAME[bis.month]} {bis.year}"


FUSS = re.compile(r"OFFNUNG|GEOFFNET|UHR\b|TEL|WWW|@|\.AT\b|SAMSTAG|SONNTAG|RUHETAG|RESERV|"
                  r"GUTEN APPETIT|ALLERGEN|FACEBOOK|INSTAGRAM")


def unten_bestimmen(zeilen):
    """y-Position knapp unter dem letzten Freitags-Gericht."""
    idx = next((i for i, z in enumerate(zeilen) if "FREITAG" in norm(z["text"])), None)
    if idx is None:
        return None
    hoehen = [z["bottom"] - z["top"] for z in zeilen]
    zh = float(np.median(hoehen)) if hoehen else 30
    # typischer Abstand zwischen Tag und Gericht (aus den anderen Tagen gelernt)
    abst = []
    for i, z in enumerate(zeilen[:-1]):
        if any(t in norm(z["text"]) for t in TAGE[:4]):
            abst.append(zeilen[i + 1]["top"] - z["bottom"])
    grenze = max(zh * 1.6, (max(abst) if abst else zh) * 1.8)
    letzte = zeilen[idx]
    for z in zeilen[idx + 1:]:
        if z["top"] - letzte["bottom"] > grenze or FUSS.search(norm(z["text"])):
            break
        letzte = z
    return int(letzte["bottom"] + zh * 0.7)


def auto_entzerren(im):
    """Erkennt ein fotografiertes Blatt vor dunklerem Hintergrund und entzerrt es."""
    import cv2
    a = np.asarray(im.convert("L"))
    rand = np.concatenate([a[:10].ravel(), a[-10:].ravel(), a[:, :10].ravel(), a[:, -10:].ravel()])
    if rand.mean() > 215:          # Ränder sind weiß → digitaler Export, nichts zu tun
        return im, False
    klein = cv2.resize(a, None, fx=0.25, fy=0.25)
    klein = cv2.GaussianBlur(klein, (7, 7), 0)
    _, bw = cv2.threshold(klein, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    cnts, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return im, False
    c = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(c) < 0.3 * klein.size:
        return im, False
    approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
    if len(approx) != 4:
        return im, False
    p = approx.reshape(4, 2).astype(float) * 4
    s, d = p.sum(1), np.diff(p, axis=1).ravel()
    ol, ur = p[np.argmin(s)], p[np.argmax(s)]
    or_, ul = p[np.argmin(d)], p[np.argmax(d)]
    ecken = [*ol, *or_, *ur, *ul]
    return wv.entzerren(im, ecken), True


# ---------------------------------------------------------------- GitHub / Live-Prüfung

def git(*args):
    r = subprocess.run(["git", *args], cwd=REPO_DIR, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def sha256(daten):
    return hashlib.sha256(daten).hexdigest()


def live_hash():
    req = urllib.request.Request(f"{PAGES_URL}?v={int(time.time())}",
                                 headers={"Cache-Control": "no-cache", "User-Agent": "wk-check"})
    return sha256(urllib.request.urlopen(req, timeout=30).read())


def pages_build_anstossen():
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (repo and token):
        return
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/pages/builds", method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Accept": "application/vnd.github+json"})
    try:
        urllib.request.urlopen(req, timeout=30)
        log("Pages-Build zusätzlich angestoßen")
    except Exception as e:
        log("Pages-Build anstoßen nicht möglich:", e)


def warten_bis_live(soll, minuten=15):
    ende = time.time() + minuten * 60
    angestossen = False
    while time.time() < ende:
        try:
            if live_hash() == soll:
                return True
        except Exception as e:
            log("Live-Abruf fehlgeschlagen:", e)
        if not angestossen and time.time() > ende - (minuten - 5) * 60:
            pages_build_anstossen()
            angestossen = True
        time.sleep(20)
    return False


def website_bindet_ein():
    html = urllib.request.urlopen(urllib.request.Request(SITE_URL, headers={"User-Agent": "wk"}),
                                  timeout=30).read().decode("utf-8", "replace")
    return PAGES_URL in html


# ---------------------------------------------------------------- Hauptablauf

def verarbeite(m, uid):
    typ, daten = m.uid("FETCH", uid, "(RFC822)")
    msg = email.message_from_bytes(daten[0][1])
    betreff = str(make_header(decode_header(msg.get("Subject", ""))))
    datum = parsedate_to_datetime(msg["Date"]).astimezone(WIEN).date()
    log(f"Mail vom {datum}: {betreff}")

    kands, html = kandidaten(msg)
    if not kands:
        txt = norm(re.sub(r"<[^>]+>", " ", html))
        if "TAGESTELLER" in txt or "WOCHENKARTE" in txt or sum(t in txt for t in TAGE) >= 3:
            raise InhaltsProblem("Die Mail enthält die Wochenkarte nur als Text, nicht als Bild "
                                 "oder PDF. Ein Bild dafür muss von Hand erstellt werden.")
        log("Keine Wochenkarte in dieser Mail → keine-Karte")
        return "keine"

    # besten Kandidaten nach Anzahl erkannter Wochentage wählen
    bester = None
    for name, bytes_ in kands:
        try:
            for bez, im in seiten_bilder(name, bytes_):
                im2, _ = auto_entzerren(im)
                zl = ocr_zeilen(im2)
                punkte = len(tage_gefunden(zl)) + (2 if "TAGESTELLER" in norm(
                    " ".join(z["text"] for z in zl)) else 0)
                log(f"  Kandidat {bez}: {im.width}×{im.height}, Punkte {punkte}")
                if bester is None or punkte > bester[0]:
                    bester = (punkte, bez, im2, zl, name, bytes_)
        except Exception as e:
            log(f"  Kandidat {name} nicht lesbar: {e}")
    if bester is None or bester[0] < 3:
        if bester is None or bester[0] == 0:
            log("Kein Anhang sieht nach Wochenkarte aus → keine-Karte")
            return "keine"
        raise InhaltsProblem("In der Mail ist ein Bild/PDF, aber die Wochentage sind darauf nicht "
                             "lesbar (zu klein, unscharf oder ungewohntes Format).")
    _, bez, im, zeilen, orig_name, orig_bytes = bester

    # Datum
    zr = datum_lesen(zeilen, datum)
    hinweise = []
    if zr:
        von, bis = zr
        montag_naechste = datum + dt.timedelta(days=(7 - datum.weekday()) % 7 or 7)
        montag_diese = datum - dt.timedelta(days=datum.weekday())
        if von not in (montag_naechste, montag_diese):
            raise InhaltsProblem(f"Auf der Karte steht {zeitraum_text(von, bis)}. Die Mail kam am "
                                 f"{datum:%d.%m.%Y}, erwartet war die Woche ab "
                                 f"{montag_naechste:%d.%m.%Y}. Evtl. hat die Chefin die alte "
                                 f"Karte erwischt.")
        zeitraum = zeitraum_text(von, bis)
    else:
        von = datum + dt.timedelta(days=(7 - datum.weekday()) % 7 or 7)
        zeitraum = f"Woche ab {von.day}. {MONATSNAME[von.month]} {von.year}"
        hinweise.append("Das Datum auf der Karte war nicht automatisch lesbar – bitte kurz "
                        "prüfen, ob es die richtige Woche ist.")

    commit_msg = f"Wochenkarte {zeitraum}"
    try:
        if git("log", "-1", "--pretty=%s") == commit_msg:
            log("Diese Woche ist schon online → nur Label setzen")
            return "schon"
    except Exception:
        pass

    # Aufbereitung
    unten = unten_bestimmen(zeilen)
    if unten is None:
        hinweise.append("Die Freitags-Zeile wurde nicht gefunden, die Karte ist deshalb nicht "
                        "unten abgeschnitten.")
    out = wv.aufbereiten(im, unten=unten)
    fehlende = [t.title() for t in TAGE if t not in tage_gefunden(ocr_zeilen(out))]
    if len(fehlende) > 1:
        out2 = wv.aufbereiten(im, unten=unten, logo=1.0, luecke=1.0)
        fehlende2 = [t.title() for t in TAGE if t not in tage_gefunden(ocr_zeilen(out2))]
        if len(fehlende2) < len(fehlende):
            out, fehlende = out2, fehlende2
    if len(fehlende) > 1:
        raise InhaltsProblem("Nach der Aufbereitung sind diese Tage nicht mehr lesbar: "
                             + ", ".join(fehlende) + ".")
    if fehlende:
        hinweise.append(f"{fehlende[0]} wurde nicht automatisch erkannt (evtl. Feiertag) – bitte "
                        "kurz ansehen.")
    v = out.width / out.height
    if not 0.5 <= v <= 1.4:
        raise InhaltsProblem(f"Das fertige Bild hat ein ungewöhnliches Seitenverhältnis ({v:.2f}).")
    if out.width < 900:
        hinweise.append(f"Die Vorlage war klein, die Karte ist nur {out.width} px breit.")

    ziel = os.path.join(REPO_DIR, "wochenkarte.jpg")
    wv.speichern(out, ziel)
    with open(ziel, "rb") as f:
        fertig = f.read()
    soll = sha256(fertig)
    endung = os.path.splitext(orig_name)[1].lower() or ".bin"

    if TROCKEN:
        mail_senden(GMAIL_USER, f"[TEST] Wochenkarte {zeitraum} – so würde sie aussehen",
                    f"Trockenlauf, nichts hochgeladen.\nQuelle: {bez}\n"
                    + ("\n".join(hinweise) or "Keine Auffälligkeiten."),
                    [("wochenkarte.jpg", fertig, "image/jpeg"),
                     (f"original{endung}", orig_bytes, "application/octet-stream")])
        subprocess.run(["git", "checkout", "--", "wochenkarte.jpg"], cwd=REPO_DIR)
        return "trocken"

    os.makedirs(os.path.join(REPO_DIR, "archiv"), exist_ok=True)
    wv.speichern(out, os.path.join(REPO_DIR, "archiv", f"{von:%Y-%m-%d}_wochenkarte.jpg"))
    with open(os.path.join(REPO_DIR, "archiv", f"{von:%Y-%m-%d}_original{endung}"), "wb") as f:
        f.write(orig_bytes)
    git("config", "user.name", "Wochenkarte-Automatik")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git("add", "wochenkarte.jpg", "archiv")
    git("commit", "-m", commit_msg)
    git("push")
    log("Gepusht:", commit_msg)

    if not warten_bis_live(soll):
        raise RuntimeError("GitHub Pages liefert auch nach 15 Minuten noch nicht die neue Karte "
                           "aus. Der Upload ist erfolgt; meist erledigt sich das von selbst.")
    if not website_bindet_ein():
        raise RuntimeError(f"Die Website ({SITE_URL}) bindet die Wochenkarte nicht mehr von "
                           f"{PAGES_URL} ein – wurde der Website-Code geändert?")

    text = (f"Die Wochenkarte {zeitraum} ist online und geprüft.\n\n"
            + ("Hinweise:\n- " + "\n- ".join(hinweise) + "\n\n" if hinweise else "")
            + f"Live: {PAGES_URL}\nWebsite: https://bistro-grossartig.at/\n\n"
            "Die fertige Karte hängt an.\n\n– Wochenkarte-Automatik (GitHub Action)")
    mail_senden(NOTIFY_TO, f"Wochenkarte Grossartig: {zeitraum} ist online", text,
                [("wochenkarte.jpg", fertig, "image/jpeg")])
    return "ok"


def main():
    if not GMAIL_PW:
        sys.exit("FEHLER: Secret GMAIL_APP_PASSWORD fehlt.")
    m = imap_verbinden()
    raw = (f"from:{ABSENDER} newer_than:10d -label:wochenkarte-erledigt "
           f"-label:wochenkarte-problem -label:wochenkarte-keine-karte")
    uids = gm_suche(m, raw)
    log(f"{len(uids)} unbearbeitete Mail(s) von {ABSENDER}")
    if not uids:
        return
    uid = uids[-1]                     # neueste
    try:
        erg = verarbeite(m, uid)
        if erg in ("ok", "schon"):
            label_setzen(m, uid, L_ERLEDIGT)
        elif erg == "keine":
            label_setzen(m, uid, L_KEINE)
    except InhaltsProblem as e:
        log("INHALTSPROBLEM:", e)
        typ, daten = m.uid("FETCH", uid, "(RFC822)")
        orig = daten[0][1]
        problem_melden(m, "Karte konnte nicht übernommen werden", str(e),
                       [("newsletter.eml", orig, "application/octet-stream")])
        label_setzen(m, uid, L_PROBLEM)
    except Exception as e:
        log("TECHNISCHES PROBLEM:", e)
        traceback.print_exc()
        problem_melden(m, "technischer Fehler",
                       f"Beim automatischen Hochladen ist ein technischer Fehler aufgetreten:\n\n"
                       f"{e}\n\nDie Automatik versucht es alle 30 Minuten erneut. Details stehen "
                       f"im Protokoll unter github.com/davidhaberlai/grossartig-wochenkarte/actions.")
        sys.exit(1)
    finally:
        try:
            m.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
