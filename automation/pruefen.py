#!/usr/bin/env python3
"""Schneller Vorab-Check (nur Standardbibliothek): Gibt es eine unbearbeitete Newsletter-Mail?
Schreibt 'neu=true/false' nach $GITHUB_OUTPUT, damit die schweren Werkzeuge nur bei Bedarf
installiert werden."""
import imaplib
import os
import sys

user = os.environ.get("GMAIL_USER", "haberldavid390@gmail.com")
pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
absender = os.environ.get("NEWSLETTER_FROM", "newsletter@bistro-grossartig.at")
if not pw:
    sys.exit("FEHLER: Secret GMAIL_APP_PASSWORD fehlt (Repo → Settings → Secrets → Actions).")

m = imaplib.IMAP4_SSL("imap.gmail.com")
m.login(user, pw)
alle = None
for zeile in m.list()[1]:
    z = zeile.decode(errors="replace")
    if "\\All" in z:
        alle = z.split(' "/" ')[-1].strip()
m.select(alle or "INBOX", readonly=True)
raw = (f"from:{absender} newer_than:10d -label:wochenkarte-erledigt "
       f"-label:wochenkarte-problem -label:wochenkarte-keine-karte")
typ, daten = m.uid("SEARCH", "X-GM-RAW", '"' + raw + '"')
anzahl = len(daten[0].split()) if daten and daten[0] else 0
m.logout()
print(f"{anzahl} unbearbeitete Mail(s) von {absender}")
with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
    f.write(f"neu={'true' if anzahl else 'false'}\n")
