#!/usr/bin/env python3
"""
Wochenkarte Grossartig – Bildaufbereitung (Karibu Studio)

Bringt die wöchentliche Wochenkarte (Foto, Screenshot, PNG/JPG/WEBP/HEIC oder PDF)
in das Format, das auf bistro-grossartig.at verwendet wird:

  1. optional: Seite aus PDF rendern
  2. optional: schräg fotografiertes Blatt entzerren (--ecken, 4 Punkte)
  3. optional: oben/unten abschneiden (--oben / --unten, z. B. Fußbereich unter Freitag weg)
  4. Papier gleichmäßig weiß machen (Hintergrund aufhellen)
  5. Ränder eng setzen (seitlich + oben/unten)
  6. große Leerräume zwischen den Blöcken auf ein Höchstmaß verkleinern
  7. Logo (erster Block oben) optional verkleinern (--logo, Standard 0.8)
  8. auf max. 1200 px Breite skalieren (nie hochskalieren), leicht nachschärfen
  9. als JPEG ohne Metadaten speichern

Mit --analyse werden nur Maße und die Zeilenblöcke ausgegeben (nichts gespeichert),
damit man die Werte für --oben/--unten bestimmen kann. Mit --ocr werden zusätzlich die
erkannten Textzeilen mit ihren y-Positionen ausgegeben (Tesseract, Deutsch).

Beispiele:
  python3 wochenkarte_verarbeiten.py eingang.jpg --analyse --ocr
  python3 wochenkarte_verarbeiten.py eingang.jpg -o wochenkarte.jpg --unten 1780
  python3 wochenkarte_verarbeiten.py foto.jpg -o wochenkarte.jpg --ecken 120,80,1850,60,1900,2600,90,2640
  python3 wochenkarte_verarbeiten.py karte.pdf -o wochenkarte.jpg
"""
import argparse, os, subprocess, sys, tempfile
import numpy as np
from PIL import Image, ImageFilter, ImageOps

ZIEL_BREITE = 1200
INK = 200            # Pixel dunkler als das gilt als "Inhalt"
MIN_PIX_ZEILE = 3    # so viele Inhalts-Pixel braucht eine Zeile, um als Inhalt zu zählen


def pdf_seiten(pfad, max_seiten=6, dpi=220):
    """Rendert die ersten Seiten eines PDFs und gibt die PNG-Pfade zurück."""
    tmp = tempfile.mkdtemp()
    subprocess.run(["pdftoppm", "-r", str(dpi), "-f", "1", "-l", str(max_seiten), "-png", pfad,
                    os.path.join(tmp, "seite")], check=True)
    return [os.path.join(tmp, f) for f in sorted(os.listdir(tmp)) if f.endswith(".png")]


def laden(pfad, seite=1):
    if pfad.lower().endswith(".pdf"):
        tmp = tempfile.mkdtemp()
        subprocess.run(["pdftoppm", "-r", "220", "-f", str(seite), "-l", str(seite), "-png", pfad,
                        os.path.join(tmp, "seite")], check=True)
        dateien = sorted(f for f in os.listdir(tmp) if f.endswith(".png"))
        if not dateien:
            sys.exit("FEHLER: PDF konnte nicht gerendert werden")
        pfad = os.path.join(tmp, dateien[0])
    if pfad.lower().endswith((".heic", ".heif")):
        try:
            import pillow_heif  # noqa
            pillow_heif.register_heif_opener()
        except ImportError:
            sys.exit("FEHLER: HEIC-Datei, aber pillow-heif fehlt (pip install pillow-heif --break-system-packages)")
    im = Image.open(pfad)
    im = ImageOps.exif_transpose(im)          # Handyfotos richtig drehen
    if im.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        im = im.convert("RGBA")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    return im.convert("RGB")


def entzerren(im, ecken):
    import cv2
    p = np.array(ecken, dtype=np.float32).reshape(4, 2)   # oben-links, oben-rechts, unten-rechts, unten-links
    b = int(max(np.linalg.norm(p[0] - p[1]), np.linalg.norm(p[3] - p[2])))
    h = int(max(np.linalg.norm(p[0] - p[3]), np.linalg.norm(p[1] - p[2])))
    ziel = np.array([[0, 0], [b - 1, 0], [b - 1, h - 1], [0, h - 1]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(p, ziel)
    out = cv2.warpPerspective(np.asarray(im), M, (b, h), flags=cv2.INTER_CUBIC,
                              borderValue=(255, 255, 255))
    im = Image.fromarray(out)
    # Blattkante / Interpolationssaum am Rand entfernen, sonst zählt er als "Inhalt"
    rx, ry = max(4, int(b * 0.008)), max(4, int(h * 0.008))
    return im.crop((rx, ry, b - rx, h - ry))


def papier_weiss(im):
    """Hintergrund gleichmäßig weiß: Helligkeitsverlauf (Schatten, Vignette) herausrechnen."""
    a = np.asarray(im).astype(np.float32)
    grau = a.mean(axis=2)
    # grobe Hintergrundschätzung: starker Weichzeichner über ein Maximum-Filterbild
    g = Image.fromarray(grau.astype(np.uint8)).filter(ImageFilter.MaxFilter(15)) \
        .filter(ImageFilter.GaussianBlur(40))
    bg = np.asarray(g).astype(np.float32)
    bg[bg < 120] = 120
    faktor = 245.0 / bg
    a = np.clip(a * faktor[..., None], 0, 255)
    # fast-weiß auf rein weiß ziehen
    a[a.mean(axis=2) > 235] = 255
    return Image.fromarray(a.astype(np.uint8))


def bloecke(im, min_luecke=1):
    a = np.asarray(im.convert("L"))
    zeilen = (a < INK).sum(axis=1)
    inh = zeilen >= MIN_PIX_ZEILE
    out, start = [], None
    for y, v in enumerate(inh):
        if v and start is None:
            start = y
        elif not v and start is not None:
            out.append((start, y)); start = None
    if start is not None:
        out.append((start, len(inh)))
    # sehr kleine Lücken (innerhalb einer Textzeile) zusammenfassen
    zus = []
    for b in out:
        if zus and b[0] - zus[-1][1] < min_luecke:
            zus[-1] = (zus[-1][0], b[1])
        else:
            zus.append(b)
    return zus


def inhalt_box(im):
    a = np.asarray(im.convert("L"))
    m = a < INK
    ys = np.where(m.sum(axis=1) >= MIN_PIX_ZEILE)[0]
    xs = np.where(m.sum(axis=0) >= MIN_PIX_ZEILE)[0]
    if len(ys) == 0 or len(xs) == 0:
        sys.exit("FEHLER: kein Inhalt im Bild gefunden")
    return xs[0], ys[0], xs[-1] + 1, ys[-1] + 1


def luecken_verkleinern(im, max_luecke):
    bl = bloecke(im, min_luecke=int(max_luecke * 0.25))
    teile = []
    for i, (y0, y1) in enumerate(bl):
        if i > 0:
            luecke = y0 - bl[i - 1][1]
            teile.append(("luecke", min(luecke, max_luecke)))
        teile.append(("block", im.crop((0, y0, im.width, y1))))
    h = sum(t[1] if t[0] == "luecke" else t[1].height for t in teile)
    out = Image.new("RGB", (im.width, h), (255, 255, 255))
    y = 0
    for art, t in teile:
        if art == "luecke":
            y += t
        else:
            out.paste(t, (0, y)); y += t.height
    return out


def logo_verkleinern(im, faktor, max_luecke):
    """Erster großer Block oben = Logo. Wird zentriert verkleinert."""
    if faktor >= 0.999:
        return im
    bl = bloecke(im, min_luecke=int(max_luecke * 0.6))
    if len(bl) < 2:
        return im
    y0, y1 = bl[0]
    logo = im.crop((0, y0, im.width, y1))
    lx0, _, lx1, _ = inhalt_box(logo)
    logo = logo.crop((lx0, 0, lx1, logo.height))
    neu = logo.resize((max(1, int(logo.width * faktor)), max(1, int(logo.height * faktor))),
                      Image.LANCZOS)
    rest = im.crop((0, y1, im.width, im.height))
    out = Image.new("RGB", (im.width, y0 + neu.height + rest.height), (255, 255, 255))
    out.paste(neu, ((im.width - neu.width) // 2, y0))
    out.paste(rest, (0, y0 + neu.height))
    return out


def ocr(im):
    try:
        import csv, io
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
        im.save(tmp)
        r = subprocess.run(["tesseract", tmp, "stdout", "-l", "deu", "--psm", "4", "tsv"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            r = subprocess.run(["tesseract", tmp, "stdout", "--psm", "4", "tsv"],
                               capture_output=True, text=True)
        zeilen = {}
        for row in csv.DictReader(io.StringIO(r.stdout), delimiter="\t"):
            if row.get("level") != "5" or not row.get("text", "").strip():
                continue
            key = (row["block_num"], row["par_num"], row["line_num"])
            top, h = int(row["top"]), int(row["height"])
            z = zeilen.setdefault(key, {"top": top, "bottom": top + h, "text": []})
            z["top"] = min(z["top"], top); z["bottom"] = max(z["bottom"], top + h)
            z["text"].append(row["text"])
        for z in sorted(zeilen.values(), key=lambda z: z["top"]):
            print(f"  y {z['top']:5d}–{z['bottom']:5d}  {' '.join(z['text'])}")
    except FileNotFoundError:
        print("  (tesseract nicht verfügbar)")


def aufbereiten(im, oben=None, unten=None, logo=0.8, luecke=0.06, rand=0.035, weiss=True):
    """Komplette Aufbereitung eines (bereits entzerrten) Bildes. Gibt das fertige Bild zurück."""
    if oben is not None or unten is not None:
        im = im.crop((0, oben or 0, im.width, unten or im.height))
    if weiss:
        im = papier_weiss(im)
    x0, y0, x1, y1 = inhalt_box(im)
    im = im.crop((x0, y0, x1, y1))
    breite = im.width
    max_luecke = max(8, int(breite * luecke))
    im = luecken_verkleinern(im, max_luecke)
    im = logo_verkleinern(im, logo, max_luecke)
    r = int(breite * rand)
    out = Image.new("RGB", (im.width + 2 * r, im.height + 2 * r), (255, 255, 255))
    out.paste(im, (r, r))
    if out.width > ZIEL_BREITE:
        h = round(out.height * ZIEL_BREITE / out.width)
        out = out.resize((ZIEL_BREITE, h), Image.LANCZOS)
        out = out.filter(ImageFilter.UnsharpMask(radius=1.0, percent=60, threshold=2))
    return out


def speichern(out, pfad):
    out.save(pfad, "JPEG", quality=90, optimize=True, progressive=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("eingang")
    ap.add_argument("-o", "--ausgang", default="wochenkarte.jpg")
    ap.add_argument("--analyse", action="store_true", help="nur Maße/Blöcke ausgeben")
    ap.add_argument("--ocr", action="store_true", help="bei --analyse Textzeilen mit y ausgeben")
    ap.add_argument("--seite", type=int, default=1, help="PDF-Seite (Standard 1)")
    ap.add_argument("--ecken", help="8 Zahlen: x,y oben-links, oben-rechts, unten-rechts, unten-links")
    ap.add_argument("--oben", type=int, default=None, help="alles oberhalb dieser y-Position weg")
    ap.add_argument("--unten", type=int, default=None, help="alles unterhalb dieser y-Position weg")
    ap.add_argument("--logo", type=float, default=0.8, help="Logo-Skalierung (1.0 = unverändert)")
    ap.add_argument("--luecke", type=float, default=0.06,
                    help="max. Leerraum zwischen Blöcken, Anteil der Inhaltsbreite")
    ap.add_argument("--rand", type=float, default=0.035, help="Rand rundum, Anteil der Inhaltsbreite")
    ap.add_argument("--kein-weiss", action="store_true", help="Papier-Aufhellung auslassen")
    a = ap.parse_args()

    im = laden(a.eingang, seite=a.seite)
    if a.ecken:
        im = entzerren(im, [float(v) for v in a.ecken.split(",")])

    if a.analyse:
        print(f"Größe (nach Entzerrung): {im.width} × {im.height} px")
        x0, y0, x1, y1 = inhalt_box(im)
        print(f"Inhalt: x {x0}–{x1}, y {y0}–{y1}")
        print("Blöcke (y von–bis):")
        for b in bloecke(im, min_luecke=8):
            print(f"  {b[0]:5d}–{b[1]:5d}  (Höhe {b[1]-b[0]})")
        if a.ocr:
            print("Textzeilen:")
            ocr(im)
        return

    out = aufbereiten(im, a.oben, a.unten, a.logo, a.luecke, a.rand, not a.kein_weiss)
    speichern(out, a.ausgang)
    print(f"OK: {a.ausgang}  {out.width} × {out.height} px  "
          f"(Seitenverhältnis {out.width/out.height:.3f})")
    if out.width < 900:
        print(f"WARNUNG: nur {out.width} px breit – Vorlage hat geringe Auflösung")


if __name__ == "__main__":
    main()
