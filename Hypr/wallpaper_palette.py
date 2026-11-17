#!/usr/bin/env python3
"""
wallpaper_palette.py - wallpaper -> colour.css for Waybar (style.css reads it).

A wallpaper can have beautiful colours and still be a terrible source of interface colours: a
photo can be 70% muddy olive, or a pale sky that makes light text unreadable. So no single stage
decides everything. Four stages, each doing the job it is good at:

  1. ANALYSE   Python reads the picture (still, gif, or several frames of a video) in OKLCH and
               pulls out its colour character: which hues it contains, how vivid and bright it is.
               Hues are ranked by how good they are as INTERFACE colours (vivid beats muddy,
               extremes lose), not by area alone.
  2. GENERATE  Matugen builds Material You palettes from seed colours that Python chose: one for
               the main accent, one for a second accent, one for the neutral (background) colours.
               The dominant colour therefore does not paint the whole bar. No Matugen installed,
               or it fails? A built-in generator takes over, so the bar never breaks.
  3. CORRECT   A correction layer checks contrast (WCAG), saturation and brightness of every
               colour the bar uses and fixes what is out of range: backgrounds stay dark and calm,
               accents stay bright and clean, text stays readable on everything it sits on.
  4. WRITE     colour.css, with the semantic colours style.css uses (primary, tertiary, surface,
               on_surface, primary_container, ...) plus two NEON colours, neon_1 and neon_2: the
               picture's main and second hue turned up to full neon (as vivid as the screen can show
               and still readable on the bar), for text that has to have presence. A picture with no
               colour of its own gets electric cyan and hot magenta.
               And right_light / right_deep: two shades of ONE colour (the picture's main hue, rich
               rather than pastel) for the right island's pills, so that module holds a single colour.

A picture with (almost) no colour gets a neutral silver palette: Matugen is not asked, because
it would invent a seed colour (its default blue) for a picture that has none.

Usage:
  wallpaper_palette.py IMAGE --out ~/.config/waybar/colour.css
  wallpaper_palette.py IMAGE --list                  the colours found, best interface colour first
  wallpaper_palette.py IMAGE --pick 1 --out FILE     use candidate number 1 from --list
  wallpaper_palette.py IMAGE --pick "#cb5056" ...    use this exact colour as the accent
  wallpaper_palette.py IMAGE --report                show every check the correction layer made
  wallpaper_palette.py IMAGE --engine builtin ...    skip Matugen (auto | matugen | builtin)
  wallpaper_palette.py IMAGE --kitty FILE --prompt FILE   also write kitty's colours and the shell prompt's
                                                     colours, made from the same palette (see TERMINAL below)
IMAGE can be a picture, a gif, or a video (needs ffmpeg).
Needs: python, Pillow (pacman -S python-pillow). Matugen is used when it is installed.
"""
import argparse
import concurrent.futures
import io
import math
import os
import shutil
import subprocess
import sys
import tempfile

try:
    from PIL import Image
except ImportError:
    sys.exit("wallpaper_palette.py needs Pillow:  sudo pacman -S python-pillow")

# ================================================================ settings you can tweak
# --- reading the picture
SAMPLE_SIZE = 128             # a still picture is shrunk to this many pixels on its long side
MAX_FRAMES = 8                # a gif is read at this many evenly spaced frames ...
ANIMATION_SAMPLE_SIZE = 64    # ... each shrunk to this size
VIDEO_FRAMES = (0.08, 0.25, 0.42, 0.58, 0.75, 0.92)   # a video is read at these points of its length
VIDEO_SAMPLE_SIZE = 96
VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v")
MIN_CHROMA = 0.040            # pixels less colourful than this are grey and do not vote ...
ABS_MIN_CHROMA = 0.011        # ... but for a muted picture the bar drops to 35% of its own vividness, down to this
MIN_LIGHT, MAX_LIGHT = 0.12, 0.975    # nearly black / nearly white pixels do not vote either
CHROMA_EXPONENT = 1.2         # how much more a vivid pixel votes than a faint one (0 = by area only)
MIN_COLOUR_SHARE = 0.010      # fewer colourful pixels than this and the picture counts as black and white
# --- choosing the colours
MUDDY_FACTOR = 0.55           # dull dark olive / mustard / brown is fine in a photo, drab on a bar: its rank is multiplied by this
MONO_ACCENT = None            # e.g. "#a8c7fa" to give black and white pictures a fixed accent colour
SECOND_HUE_MIN = 25           # the second colour must be at least this many degrees from the accent ...
SECOND_HUE_MAX = 70           # ... and is leaned towards its real hue by at most this much, so gradients stay clean
SECOND_HUE_FALLBACK = 40      # picture has no second colour: turn the accent by this
# --- Matugen
SCHEME_MUTED = "scheme-tonal-spot"    # for muted pictures: Matugen lifts them to a healthy accent
SCHEME_VIVID = "scheme-content"       # for vivid pictures: stays true to the picture's colour
VIVID_FROM = 0.06                     # picture colour at least this vivid counts as vivid
# --- correction layer: the ranges every colour is forced into (OKLCH lightness L, chroma C)
ACCENT_C = (0.060, 0.150)             # accents: never dull, never neon
CONTAINER_C = (0.070, 0.170)          # the deep colour the islands are tinted with (the floor rises with how vivid the picture is, up to 0.12)
CONTAINER_C_MUDDY = 0.100             # ... which is capped lower for olive / mustard hues (hue 80-130)
SURFACE_C = (0.006, 0.030)            # backgrounds stay calm
SURFACE_L = (0.11, 0.23)
CONTAINER_L = (0.30, 0.52)
ACCENT_L = (0.74, 0.92)
TEXT_ON_ISLAND = 7.0                  # WCAG contrast the text must reach on the island ...
TEXT_ON_PILL = 6.0                    # ... on the pills inside it ...
DARK_TEXT_ON_ACCENT = 4.5             # ... and the dark text on the bright gradient pills (the worst point of the gradient)
HUE_TOLERANCE = 14                    # Matugen's hue may differ this much from the picture's before it is pulled back
# --- neon text colours (neon_1 = the picture's main hue, neon_2 = its second hue)
NEON_MONO_HUES = (195, 330)           # a picture with no colour of its own: electric cyan + hot magenta (55 orange, 140 green, 290 violet)
NEON_LIGHT = (0.60, 0.95)             # lightness range searched for the most vivid colour of a hue
NEON_ON_PILL = 4.5                    # contrast a neon colour must reach on the pill, with a mid-grey wallpaper behind the bar ...
NEON_ON_PILL_WORST = 3.6              # ... and even with a white wallpaper shining through it
# --- the right island's pills: ONE colour, the picture's main hue, rich rather than pastel
RIGHT_BAND = (0.68, 0.88)             # lightness range of the colour; it settles where the hue is at its most vivid
RIGHT_VIVID = 0.88                    # how much of the most vivid chroma that exists at that lightness it may use (1.0 = neon)
RIGHT_CHROMA = (0.09, 0.19)           # chroma range: a muted picture gets a softer colour than a vivid one
RIGHT_SPREAD = 0.06                   # the light end of the pills is this much lighter than the deep end (0 = flat, exactly one colour)
DARK_TEXT_ON_RIGHT = 6.5              # contrast the dark text must reach on every shade of the right pills
# --- terminal colours (kitty and the shell prompt), made from the finished bar palette
TERM_NORMAL_L = 0.74                  # lightness of the 8 normal colours (red, green, ...) ...
TERM_BRIGHT_L = 0.84                  # ... and of the 8 bright ones
TERM_C = (0.100, 0.150)               # their chroma range: a vivid picture gets vivid terminal colours, a muted one calmer (never washed out)
TERM_MIN_CONTRAST = 4.5               # contrast every terminal text colour must reach on the terminal background
TERM_HUES = {"red": 27, "green": 145, "yellow": 95, "blue": 255, "magenta": 330, "cyan": 200}   # so an error stays red and "ok" stays green
TERM_LEAN = (0.5, 15)                 # a colour leans towards the picture's hue: half the gap, 15 degrees at most. Only colours that are
                                      # already near the picture's hue lean (full up to 60 degrees apart, none from 150), so red stays red
                                      # on a green picture and the six colours never turn into each other
TERM_SET_CURSOR = True                # the cursor takes the picture's main neon colour; False leaves the cursor colour of your kitty.conf alone
# ================================================================


# ---------------------------------------------------------------- colour maths
def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _to_linear(c):
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _to_srgb(c):
    c = max(0.0, min(1.0, c))
    c = 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055
    return int(round(max(0.0, min(1.0, c)) * 255))


def rgb_to_oklab(r, g, b):
    r, g, b = _to_linear(r), _to_linear(g), _to_linear(b)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def _oklab_to_linear(L, a, b):
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
            -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
            -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)


def oklch_to_hex(L, C, hue):
    """OKLCH -> '#rrggbb'. If the colour does not exist in sRGB, chroma is reduced until it does."""
    h = math.radians(hue)
    eps = 1e-4

    def inside(c):
        return all(-eps <= v <= 1 + eps for v in _oklab_to_linear(L, c * math.cos(h), c * math.sin(h)))

    if not inside(C):
        lo, hi = 0.0, C
        for _ in range(24):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if inside(mid) else (lo, mid)
        C = lo
    r, g, b = _oklab_to_linear(L, C * math.cos(h), C * math.sin(h))
    return "#%02x%02x%02x" % (_to_srgb(r), _to_srgb(g), _to_srgb(b))


def hex_to_rgb(hexstr):
    h = hexstr.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def hex_to_oklch(hexstr):
    L, a, b = rgb_to_oklab(*hex_to_rgb(hexstr))
    return L, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360


def hue_diff(a, b):
    """signed shortest distance from b to a, in degrees (-180..180)"""
    return (a - b + 180) % 360 - 180


def luminance(hexstr):
    r, g, b = (_to_linear(v) for v in hex_to_rgb(hexstr))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    """WCAG contrast ratio of two colours, 1 (same) .. 21 (black on white)"""
    la, lb = luminance(a), luminance(b)
    if la < lb:
        la, lb = lb, la
    return (la + 0.05) / (lb + 0.05)


def mix_hex(a, b, t):
    """like GTK's mix(a, b, t): a plain blend in sRGB"""
    ra, rb = hex_to_rgb(a), hex_to_rgb(b)
    return "#%02x%02x%02x" % tuple(round(ra[i] * (1 - t) + rb[i] * t) for i in range(3))


# ---------------------------------------------------------------- 1. ANALYSE: reading the picture
def video_frames(path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is needed to read video wallpapers")
    duration = None
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of",
                                "default=noprint_wrappers=1:nokey=1", path],
                               capture_output=True, text=True, timeout=8, stdin=subprocess.DEVNULL)
            duration = float(r.stdout.strip())
        except Exception:
            duration = None
    times = [duration * f for f in VIDEO_FRAMES] if duration and duration > 0 else [1, 3, 6, 10, 15, 20]
    frames = []
    for t in times:
        try:
            r = subprocess.run([ffmpeg, "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
                                "-vf", f"scale={VIDEO_SAMPLE_SIZE}:-2", "-f", "image2pipe", "-vcodec", "png", "-"],
                               capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
        except Exception:
            continue
        if r.returncode == 0 and r.stdout:
            frames.append(Image.open(io.BytesIO(r.stdout)).convert("RGB"))
    if not frames:
        raise RuntimeError("could not read any frame of the video")
    return frames


def load_frames(path):
    if path.lower().endswith(VIDEO_EXTS):
        return video_frames(path)
    im = Image.open(path)
    count = max(1, getattr(im, "n_frames", 1))
    if count == 1:
        try:
            im.draft("RGB", (SAMPLE_SIZE * 2, SAMPLE_SIZE * 2))   # fast path for big jpegs
        except Exception:
            pass
        wanted, size = [0], SAMPLE_SIZE
    else:   # a gif / animated webp: judged on the whole animation, not on its first frame
        k = min(MAX_FRAMES, count)
        wanted = sorted({round(i * (count - 1) / max(1, k - 1)) for i in range(k)})
        size = ANIMATION_SAMPLE_SIZE
    frames = []
    for n in wanted:
        try:
            im.seek(n)
        except Exception:
            break
        f = im.convert("RGB")
        f.thumbnail((size, size), Image.Resampling.BOX)
        frames.append(f)
    return frames


def load_pixels(path):
    """Returns (pixels, width, frame_height); several frames are stacked one after the other."""
    pixels, w, h = [], 0, 0
    for f in load_frames(path):
        if h and f.size != (w, h):
            f = f.resize((w, h), Image.Resampling.BOX)
        w, h = f.size
        raw = f.tobytes()
        pixels.extend((raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 3))
    return pixels, w, h


def suitability(hue, C, L):
    """How good a picture colour is as an INTERFACE colour (about 0.35 .. 1.5)."""
    s = 0.35 + min(1.0, C / 0.10)                      # vivid colours make better accents than faint ones
    if 45 <= hue <= 130 and L < 0.65 and C < 0.11:     # dull dark olive / mustard / brown: drab on a bar
        s *= MUDDY_FACTOR
    if L < 0.30 or L > 0.90:                           # so dark or pale that it has to be re-lit anyway
        s *= 0.7
    return s


def analyse(path):
    """Everything Python learns about the picture:
       cands       the colours found, best interface colour first: {hue, chroma, light, weight, score}
       mono        True when the picture has (almost) no colour
       tint_hue/tint_chroma   the faint overall tint of the whole picture
       mean_light  how bright the picture is, 0..1"""
    pixels, w, h = load_pixels(path)
    n = len(pixels)
    sum_L = sum_a = sum_b = 0.0
    px = []
    for idx, (r, g, b) in enumerate(pixels):
        L, a, bb = rgb_to_oklab(r, g, b)
        sum_L += L
        sum_a += a
        sum_b += bb
        if L < MIN_LIGHT or L > MAX_LIGHT:
            continue
        x = (idx % w) / w - 0.5
        y = ((idx // w) % h) / h - 0.5                       # row inside its own frame
        centre = 1.0 + 0.5 * (1 - min(1.0, math.hypot(x, y) * 1.6))   # 1.0 at the edge, 1.5 in the middle
        px.append((math.degrees(math.atan2(bb, a)) % 360, math.hypot(a, bb), L, centre))

    mean_a, mean_b = sum_a / n, sum_b / n
    tint_chroma = math.hypot(mean_a, mean_b)
    prof = {"cands": [], "mono": True, "mean_light": sum_L / n, "tint_chroma": tint_chroma,
            "tint_hue": math.degrees(math.atan2(mean_b, mean_a)) % 360 if tint_chroma > 0.004 else 260.0}
    if not px:
        return prof

    # a muted picture is judged by its own colours: the grey threshold drops with its vividness.
    # ref = how vivid the picture gets: the chroma that the most colourful 1% of its pixels reach
    # (so a black picture with a small red logo still counts as red)
    cs = sorted((p[1] for p in px), reverse=True)
    ref = cs[min(len(cs) - 1, max(0, int(MIN_COLOUR_SHARE * n) - 1))]
    min_c = max(ABS_MIN_CHROMA, min(MIN_CHROMA, 0.35 * ref))
    votes = [(hue, C, L, (C ** CHROMA_EXPONENT) * centre) for hue, C, L, centre in px if C >= min_c]
    if ref < ABS_MIN_CHROMA or len(votes) / n < MIN_COLOUR_SHARE:
        return prof

    bins = [0.0] * 36
    for hue, C, L, wgt in votes:
        bins[int(hue // 10) % 36] += wgt
    for _ in range(2):
        bins = [0.25 * bins[i - 1] + 0.5 * bins[i] + 0.25 * bins[(i + 1) % 36] for i in range(36)]
    peaks = [i for i in range(36) if bins[i] > 0 and bins[i] >= bins[i - 1] and bins[i] >= bins[(i + 1) % 36]]
    peaks.sort(key=lambda i: -bins[i])
    top = bins[peaks[0]]
    chosen = []
    for i in peaks:
        if bins[i] < 0.12 * top:
            continue
        centre_hue = i * 10 + 5
        if any(abs(hue_diff(centre_hue, c)) < 30 for c in chosen):
            continue
        chosen.append(centre_hue)

    total = sum(v[3] for v in votes)
    cands = []
    for centre_hue in chosen[:5]:
        sx = sy = wsum = csum = lsum = 0.0
        for hue, C, L, wgt in votes:
            if abs(hue_diff(hue, centre_hue)) <= 25:
                sx += wgt * math.cos(math.radians(hue))
                sy += wgt * math.sin(math.radians(hue))
                wsum += wgt
                csum += wgt * C
                lsum += wgt * L
        c_hue = math.degrees(math.atan2(sy, sx)) % 360
        c_chroma, c_light, weight = csum / wsum, lsum / wsum, wsum / total
        cands.append({"hue": c_hue, "chroma": c_chroma, "light": c_light, "weight": weight,
                      "score": (weight ** 0.7) * suitability(c_hue, c_chroma, c_light)})
    cands.sort(key=lambda c: -c["score"])
    prof["cands"], prof["mono"] = cands, False
    return prof


# ---------------------------------------------------------------- choosing the seed colours
def second_colour(h1, c1, l1, others):
    """The second accent: the best other colour of the picture that is clearly a different hue, leaned
    towards at most SECOND_HUE_MAX degrees (a gradient between far-apart hues turns muddy)."""
    for c in others:                                    # already best-first
        d = hue_diff(c["hue"], h1)
        if abs(d) >= SECOND_HUE_MIN:
            return (h1 + clamp(d, -SECOND_HUE_MAX, SECOND_HUE_MAX)) % 360, c["chroma"], c["light"]
    return (h1 + SECOND_HUE_FALLBACK) % 360, c1 * 0.9, l1


def choose(prof, pick):
    """-> dict: h1 c1 l1 (main accent), h2 c2 l2 (second accent), mono"""
    cands = prof["cands"]
    if pick and pick != "auto":
        if pick.startswith("#"):
            l1, c1, h1 = hex_to_oklch(pick)
            others = [c for c in cands if abs(hue_diff(c["hue"], h1)) > 20]
            h2, c2, l2 = second_colour(h1, c1, l1, others)
            return {"h1": h1, "c1": c1, "l1": l1, "h2": h2, "c2": c2, "l2": l2, "mono": c1 < 0.02}
        if not pick.isdigit():
            raise SystemExit(f"--pick wants 'auto', a number or a colour like #cb5056, not '{pick}'")
        idx = int(pick)
        if not 0 <= idx < len(cands):
            raise SystemExit(f"colour number {idx} does not exist for this picture (it has {len(cands)}); try --list")
        c = cands[idx]
        h2, c2, l2 = second_colour(c["hue"], c["chroma"], c["light"], [o for o in cands if o is not c])
        return {"h1": c["hue"], "c1": c["chroma"], "l1": c["light"], "h2": h2, "c2": c2, "l2": l2, "mono": False}
    if prof["mono"] or not cands:
        if MONO_ACCENT:
            l1, c1, h1 = hex_to_oklch(MONO_ACCENT)
            h2, c2, l2 = (h1 + SECOND_HUE_FALLBACK) % 360, c1 * 0.9, l1
            return {"h1": h1, "c1": c1, "l1": l1, "h2": h2, "c2": c2, "l2": l2, "mono": False}
        h = prof["tint_hue"]
        return {"h1": h, "c1": 0.0, "l1": 0.6, "h2": h, "c2": 0.0, "l2": 0.6, "mono": True}
    c = cands[0]
    h2, c2, l2 = second_colour(c["hue"], c["chroma"], c["light"], cands[1:])
    return {"h1": c["hue"], "c1": c["chroma"], "l1": c["light"], "h2": h2, "c2": c2, "l2": l2, "mono": False}


def seed_hex(h, c, l):
    """The seed handed to Matugen: the picture's hue, at a lightness and vividness a palette can be built from."""
    return oklch_to_hex(clamp(l, 0.50, 0.72), clamp(c, 0.05, 0.20), h)


# ---------------------------------------------------------------- 2. GENERATE: Material You palettes
ROLE_NAMES = (
    "primary on_primary primary_container on_primary_container primary_fixed primary_fixed_dim on_primary_fixed "
    "on_primary_fixed_variant secondary on_secondary secondary_container on_secondary_container secondary_fixed "
    "secondary_fixed_dim on_secondary_fixed on_secondary_fixed_variant tertiary on_tertiary tertiary_container "
    "on_tertiary_container tertiary_fixed tertiary_fixed_dim on_tertiary_fixed on_tertiary_fixed_variant "
    "background on_background surface surface_dim surface_bright surface_container_lowest surface_container_low "
    "surface_container surface_container_high surface_container_highest surface_variant surface_tint on_surface "
    "on_surface_variant outline outline_variant inverse_surface inverse_on_surface inverse_primary scrim shadow "
    "error on_error error_container on_error_container source_color").split()
PRIMARY_FAMILY = [n for n in ROLE_NAMES if "primary" in n and n != "inverse_primary"]
NEUTRAL_FAMILY = ("background on_background surface surface_dim surface_bright surface_container_lowest "
                  "surface_container_low surface_container surface_container_high surface_container_highest "
                  "surface_variant on_surface on_surface_variant outline outline_variant inverse_surface "
                  "inverse_on_surface").split()


def matugen_roles(seed, scheme, workdir, tag):
    """Ask Matugen for the dark Material You palette of one seed colour. Matugen writes the colours through a
    small private template, so your own ~/.config/matugen is left alone."""
    exe = shutil.which("matugen")
    if not exe:
        raise RuntimeError("matugen is not installed")
    tmpl = os.path.join(workdir, tag + ".tmpl")      # one template per call: the three calls run at the same time
    cfg = os.path.join(workdir, tag + ".toml")
    out = os.path.join(workdir, tag + ".out")
    with open(tmpl, "w") as f:
        f.write("\n".join(f"{n}={{{{colors.{n}.default.hex}}}}" for n in ROLE_NAMES) + "\n")
    with open(cfg, "w") as f:
        f.write(f"[config]\n\n[templates.roles]\ninput_path = '{tmpl}'\noutput_path = '{out}'\n")
    r = subprocess.run([exe, "color", "hex", seed, "-m", "dark", "-t", scheme, "-c", cfg],
                       capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError(f"matugen exited with {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
    roles = {}
    with open(out) as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                v = v.strip()
                if len(v) == 7 and v.startswith("#"):
                    roles[k] = v.lower()
    missing = [n for n in ROLE_NAMES if n not in roles]
    if missing:
        raise RuntimeError("matugen's answer lacks: " + ", ".join(missing[:4]))
    return roles


def builtin_roles(hue, chroma, mono=False):
    """The built-in generator: a full dark palette for one hue (used for black and white pictures,
    and instead of Matugen when Matugen is missing or fails)."""
    if mono:
        C0, Cn = 0.0, 0.004
        Cp = Cc = 0.010
    else:
        C0 = clamp(0.045 + chroma, 0.05, 0.17)
        Cn = clamp(C0 * 0.2, 0.008, 0.03)
        Cp = min(C0 * 0.95, 0.14)
        Cc = min(C0 * 1.45, 0.20)
    t = lambda L, C: oklch_to_hex(L, C, hue)
    n = lambda L, C=None: t(L, Cn if C is None else C)
    sec = 0.0 if mono else C0 * 0.40
    low = 0.006 if mono else None
    R = {
        "primary": t(0.83, Cp), "on_primary": t(0.26, low or C0 * 0.55),
        "primary_container": t(0.42, Cc), "on_primary_container": t(0.90, low or C0 * 0.30),
        "primary_fixed": t(0.90, low or C0 * 0.55), "primary_fixed_dim": t(0.83, Cp),
        "on_primary_fixed": t(0.22, low or C0 * 0.55), "on_primary_fixed_variant": t(0.36, low or C0 * 0.80),
        "secondary": t(0.82, sec), "on_secondary": t(0.27, sec * 0.7),
        "secondary_container": t(0.34, sec * 0.9), "on_secondary_container": t(0.90, sec * 0.5),
        "secondary_fixed": t(0.90, sec * 0.6), "secondary_fixed_dim": t(0.82, sec),
        "on_secondary_fixed": t(0.22, sec * 0.7), "on_secondary_fixed_variant": t(0.36, sec * 0.9),
        "tertiary": t(0.82, Cp), "on_tertiary": t(0.27, low or C0 * 0.5),
        "tertiary_container": t(0.38, Cc), "on_tertiary_container": t(0.90, low or C0 * 0.3),
        "tertiary_fixed": t(0.90, low or C0 * 0.5), "tertiary_fixed_dim": t(0.82, Cp),
        "on_tertiary_fixed": t(0.22, low or C0 * 0.5), "on_tertiary_fixed_variant": t(0.36, low or C0 * 0.7),
        "background": n(0.15), "on_background": n(0.92, 0.012),
        "surface": n(0.17), "surface_dim": n(0.15), "surface_bright": n(0.30),
        "surface_container_lowest": n(0.13), "surface_container_low": n(0.19), "surface_container": n(0.21),
        "surface_container_high": n(0.25), "surface_container_highest": n(0.29),
        "surface_variant": n(0.36, 0.02), "surface_tint": t(0.83, Cp),
        "on_surface": n(0.92, 0.012), "on_surface_variant": n(0.80, 0.015),
        "outline": n(0.62, 0.02), "outline_variant": n(0.36, 0.02),
        "inverse_surface": n(0.92, 0.012), "inverse_on_surface": n(0.28, 0.01), "inverse_primary": t(0.45, Cc),
        "scrim": "#000000", "shadow": "#000000",
        "error": "#ffb4ab", "on_error": "#690005", "error_container": "#93000a", "on_error_container": "#ffdad6",
    }
    R["source_color"] = oklch_to_hex(0.60, 0.0 if mono else min(0.20, C0 * 1.6), hue)
    return R


def generate_palettes(ch, prof, engine):
    """-> (A, B, N, engine_note). A: main accent palette, B: second accent palette, N: neutral palette."""
    if ch["mono"]:
        P = builtin_roles(ch["h1"], 0.0, mono=True)
        return P, P, P, "built-in (black and white picture)"
    # the neutral (background) palette follows the whole picture's overall tint when it has one,
    # otherwise the accent, so a vivid accent does not tint every background by itself
    n_hue = prof["tint_hue"] if prof["tint_chroma"] >= 0.008 else ch["h1"]
    scheme = SCHEME_VIVID if ch["c1"] >= VIVID_FROM else SCHEME_MUTED
    scheme2 = SCHEME_VIVID if ch["c2"] >= VIVID_FROM else SCHEME_MUTED
    seeds = {"a": (seed_hex(ch["h1"], ch["c1"], ch["l1"]), scheme),
             "b": (seed_hex(ch["h2"], ch["c2"], ch["l2"]), scheme2),
             "n": (seed_hex(n_hue, 0.04, 0.5), SCHEME_MUTED)}
    if engine in ("auto", "matugen"):
        workdir = tempfile.mkdtemp(prefix="wallpaper_palette-")
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
                futs = {k: ex.submit(matugen_roles, s, sc, workdir, k) for k, (s, sc) in seeds.items()}
                got = {k: f.result() for k, f in futs.items()}
            return got["a"], got["b"], got["n"], f"matugen {scheme}" + ("" if scheme == scheme2 else f" + {scheme2}")
        except Exception as e:   # matugen missing / refused: the bar must keep working
            if engine == "matugen":
                raise SystemExit(f"matugen failed: {e}")
            print(f"matugen unavailable ({e}); using the built-in generator", file=sys.stderr)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    A = builtin_roles(ch["h1"], ch["c1"])
    B = builtin_roles(ch["h2"], ch["c2"])
    N = builtin_roles(n_hue, ch["c1"])
    return A, B, N, "built-in generator"


def compose(A, B, N):
    """Main accent from A, second accent from B (its primary becomes our tertiary), backgrounds from N."""
    R = dict(A)
    for name in PRIMARY_FAMILY:
        R[name.replace("primary", "tertiary")] = B[name]
    for name in NEUTRAL_FAMILY:
        R[name] = N[name]
    R["surface_tint"] = A["primary"]
    return R


# ---------------------------------------------------------------- 3. CORRECT: contrast, saturation, brightness
def lch_set(R, name, L=None, C=None, hue=None):
    l0, c0, h0 = hex_to_oklch(R[name])
    R[name] = oklch_to_hex(l0 if L is None else L, c0 if C is None else C, h0 if hue is None else hue)


def lch_of(R, name):
    return hex_to_oklch(R[name])


def island_colours(R):
    """the colours style.css builds from the palette, as plain hex (alpha ignored)"""
    island = mix_hex(R["surface"], R["primary_container"], 0.45)
    pill = mix_hex(R["surface"], R["primary_container"], 0.20)
    pill_fg = mix_hex(R["on_surface"], R["primary"], 0.30)
    return island, pill, pill_fg


def ramp_worst(R):
    """worst contrast of the dark text on the bright gradient pills, along primary -> tertiary"""
    return min(contrast(R["surface"], mix_hex(R["primary"], R["tertiary"], t / 4)) for t in range(5))


def measure(R):
    island, pill, pill_fg = island_colours(R)
    checks = {
        "text on island": (contrast(R["on_surface"], island), TEXT_ON_ISLAND),
        "text on pill": (contrast(pill_fg, pill), TEXT_ON_PILL),
        "dim text on island": (contrast(R["on_surface_variant"], island), 4.5),
        "dark text on gradient pill (worst point)": (ramp_worst(R), DARK_TEXT_ON_ACCENT),
        "icon on accent (on_primary)": (contrast(R["on_primary"], R["primary"]), 4.5),
        "icon on error (on_error)": (contrast(R["on_error"], R["error"]), 4.5),
    }
    if "neon_1" in R:                                    # (the neon colours are made after the correction layer)
        mid, worst = neon_backdrops(R)
        for k in ("neon_1", "neon_2"):
            checks[f"{k} text on pill"] = (contrast(R[k], mid), NEON_ON_PILL)
            checks[f"{k} text on pill, white wallpaper behind"] = (contrast(R[k], worst), NEON_ON_PILL_WORST)
    if "right_deep" in R:                                # (so are the right pills' two shades)
        checks["dark text on the right pills (worst shade)"] = (
            min(contrast(R["surface"], mix_hex(R["right_light"], R["right_deep"], t / 4)) for t in range(5)),
            DARK_TEXT_ON_RIGHT)
    return checks


def correct(R, ch, prof):
    """Force the colours the bar uses into safe ranges, then fix contrast. Returns (R, report lines)."""
    mono = ch["mono"]
    log = []
    cl = lambda cand_light: clamp((cand_light - 0.25) / 0.6, 0.0, 1.0)

    # --- brightness follows the picture, inside fixed bands
    surface_L = clamp(0.14 + 0.07 * clamp((prof["mean_light"] - 0.15) / 0.7, 0, 1), *SURFACE_L)
    container_L = clamp(0.38 + 0.12 * cl(ch["l1"]), *CONTAINER_L)
    accent_L = clamp(0.80 + 0.07 * cl(ch["l1"]), *ACCENT_L)
    accent2_L = clamp(0.80 + 0.07 * cl(ch["l2"]), *ACCENT_L)

    # --- accents: hue kept (pulled back to the picture's own if the generator drifted), chroma held in a band
    for name, hue, c_seed, L in (("primary", ch["h1"], ch["c1"], accent_L), ("tertiary", ch["h2"], ch["c2"], accent2_L)):
        _, C, h = lch_of(R, name)
        if abs(hue_diff(h, hue)) > HUE_TOLERANCE:
            log.append(f"{name}: hue {h:.0f} -> {hue:.0f} (was {abs(hue_diff(h, hue)):.0f} degrees off the picture)")
            h = hue
        if mono:
            lch_set(R, name, L=L, C=min(C, 0.012), hue=h)
        else:
            lch_set(R, name, L=L, C=clamp(max(C, 0.05 + 0.55 * c_seed), *ACCENT_C), hue=h)

    # --- island colour: deep and rich, but not neon and not sickly
    _, C, h = lch_of(R, "primary_container")
    h1 = lch_of(R, "primary")[2]
    if mono:
        lch_set(R, "primary_container", L=min(container_L, 0.40), C=min(C, 0.010), hue=h1)
    else:
        cmax = CONTAINER_C_MUDDY if 80 <= h1 <= 130 else CONTAINER_C[1]
        cmin = clamp(0.04 + 2.0 * ch["c1"], CONTAINER_C[0], 0.12)       # not dusty: the floor rises with the picture's vividness
        if 15 <= h1 <= 75:                                              # red / orange turn BROWN when dark and dusty: keep them rich
            cmin += 0.025
        cmin = min(cmin, cmax)
        lch_set(R, "primary_container", L=container_L, C=clamp(C, cmin, cmax), hue=h1)

    # --- backgrounds and text: calm
    for name in ("surface", "background", "surface_dim"):
        _, C, h = lch_of(R, name)
        lch_set(R, name, L=surface_L - (0.02 if name != "surface" else 0), C=clamp(C, SURFACE_C[0], SURFACE_C[1]) if not mono else min(C, 0.006))
    lch_set(R, "on_surface", L=max(0.90, lch_of(R, "on_surface")[0]), C=min(lch_of(R, "on_surface")[1], 0.02))
    lch_set(R, "on_surface_variant", L=max(0.78, lch_of(R, "on_surface_variant")[0]), C=min(lch_of(R, "on_surface_variant")[1], 0.03))
    lch_set(R, "on_primary", L=0.24, C=None if not mono else 0.006)
    R["source_color"] = oklch_to_hex(0.60, 0.0 if mono else clamp(ch["c1"], 0.05, 0.20), ch["h1"])

    # --- contrast: nudge until every check passes (a few small steps at most)
    for _ in range(30):
        bad = [k for k, (v, need) in measure(R).items() if v < need]
        if not bad:
            break
        for k in bad:
            if k == "text on island" or k == "text on pill" or k == "dim text on island":
                if lch_of(R, "on_surface")[0] < 0.97:
                    lch_set(R, "on_surface", L=lch_of(R, "on_surface")[0] + 0.01)
                    lch_set(R, "on_surface_variant", L=min(0.95, lch_of(R, "on_surface_variant")[0] + 0.01))
                elif lch_of(R, "primary_container")[0] > CONTAINER_L[0]:
                    lch_set(R, "primary_container", L=lch_of(R, "primary_container")[0] - 0.01)
                else:
                    lch_set(R, "surface", L=max(SURFACE_L[0], lch_of(R, "surface")[0] - 0.005))
            elif k.startswith("dark text"):
                for name in ("primary", "tertiary"):
                    if lch_of(R, name)[0] < ACCENT_L[1]:
                        lch_set(R, name, L=lch_of(R, name)[0] + 0.01)
                if lch_of(R, "surface")[0] > SURFACE_L[0]:
                    lch_set(R, "surface", L=lch_of(R, "surface")[0] - 0.005)
            elif k.startswith("icon on accent"):
                lch_set(R, "on_primary", L=max(0.08, lch_of(R, "on_primary")[0] - 0.02))
            elif k.startswith("icon on error"):
                R["on_error"] = "#410002"
    return R, log


# ---------------------------------------------------------------- NEON: the picture's hues at full volume
def max_chroma(L, hue):
    """The most vivid colour of this hue and lightness that still exists in sRGB (that is what 'neon' is:
    a colour sitting right on the edge of what the screen can show)."""
    h = math.radians(hue)
    cos_h, sin_h = math.cos(h), math.sin(h)
    lo, hi = 0.0, 0.40
    for _ in range(22):
        mid = (lo + hi) / 2
        ok = all(-1e-4 <= v <= 1 + 1e-4 for v in _oklab_to_linear(L, mid * cos_h, mid * sin_h))
        lo, hi = (mid, hi) if ok else (lo, mid)
    return lo


def neon_backdrops(R):
    """What the left-island text really sits on, as plain hex: the pill is 78% solid over the island, which is
    88% solid over the wallpaper, and both carry a faint light sheen. Returns the colour with a mid-grey
    wallpaper behind the bar, and the lightest it can get (a white wallpaper)."""
    island, pill, pill_fg = island_colours(R)
    out = []
    for wall in ("#808080", "#ffffff"):
        isl = mix_hex(mix_hex(wall, island, 0.88), R["on_surface"], 0.05)
        out.append(mix_hex(mix_hex(isl, pill, 0.78), pill_fg, 0.05))
    return out


def neon_colour(hue, backdrops):
    """The most vivid colour of this hue that is still readable on the bar. Low lightness gives the most
    vivid reds, blues and purples, high lightness the most vivid yellows, greens and cyans, so every
    lightness is tried and the one with the most chroma that passes the contrast checks wins."""
    mid, worst = backdrops
    best, best_key = None, None
    top = (None, -1.0)
    L = NEON_LIGHT[0]
    while L <= NEON_LIGHT[1] + 1e-9:
        hx = oklch_to_hex(L, max_chroma(L, hue), hue)
        c_mid, c_worst = contrast(hx, mid), contrast(hx, worst)
        if c_mid >= NEON_ON_PILL and c_worst >= NEON_ON_PILL_WORST:
            key = (round(hex_to_oklch(hx)[1], 3), L)       # most vivid; the brighter one if it is a tie
            if best_key is None or key > best_key:
                best, best_key = hx, key
        if min(c_mid, c_worst) > top[1]:
            top = (hx, min(c_mid, c_worst))
        L += 0.005
    return best or top[0]                                  # (nothing passed: the most readable one)


def add_neon(R, ch):
    """neon_1 / neon_2 for style.css. Hues come from the picture (a picture with no colour gets NEON_MONO_HUES)."""
    h1, h2 = NEON_MONO_HUES if ch["mono"] else (ch["h1"], ch["h2"])
    bd = neon_backdrops(R)
    R["neon_1"] = neon_colour(h1, bd)
    R["neon_2"] = neon_colour(h2, bd)
    return R


# ---------------------------------------------------------------- RIGHT PILLS: one colour, two shades
def right_colours(hue, c1, surface, mono):
    """-> (light, deep): two shades of ONE colour for the right island's pills. The deep one is the picture's
    hue at its most vivid (hue-dependent: reds and blues are richest a little darker, yellows and greens
    lighter) that still carries dark text; the light one is a touch lighter and softer. The pills flow from
    one to the other, so the module reads as a single colour with some depth."""
    if mono:                                            # a picture with no colour: one silver
        return oklch_to_hex(clamp(0.80 + RIGHT_SPREAD, 0, 0.93), 0.008, hue), oklch_to_hex(0.80, 0.008, hue)
    target = clamp(0.07 + 1.1 * c1, *RIGHT_CHROMA)      # a vivid picture gets a vivid colour, a muted one a softer one
    best = None
    L = RIGHT_BAND[0]
    while L <= RIGHT_BAND[1] + 1e-9:
        C = min(RIGHT_VIVID * max_chroma(L, hue), target)
        hx = oklch_to_hex(L, C, hue)
        if contrast(surface, hx) >= DARK_TEXT_ON_RIGHT:
            key = (round(hex_to_oklch(hx)[1], 3), -L)   # the most chroma; on a tie the darker, richer one
            if best is None or key > best[0]:
                best = (key, L, C, hx)
        L += 0.005
    if best is None:                                    # (cannot really happen) the lightest end of the band
        L = RIGHT_BAND[1]
        C = min(RIGHT_VIVID * max_chroma(L, hue), target)
        best = (None, L, C, oklch_to_hex(L, C, hue))
    _, L, C, deep = best
    L2 = min(0.93, L + RIGHT_SPREAD)
    C2 = min(C * 0.85, RIGHT_VIVID * max_chroma(L2, hue))
    return (deep if RIGHT_SPREAD <= 0 else oklch_to_hex(L2, C2, hue)), deep


def add_right(R, ch):
    """right_light / right_deep for style.css (made from the main accent only: no second colour involved)."""
    R["right_light"], R["right_deep"] = right_colours(ch["h1"], ch["c1"], R["surface"], ch["mono"])
    return R


# ---------------------------------------------------------------- TERMINAL: kitty and the shell prompt
# The terminal is themed from the palette the bar already uses, so it always matches the bar:
#   background / foreground   the bar's dark, calm background and its text colour
#   the 6 colours            keep their meaning (an error stays red) but lean towards the picture's hue
#   cursor, selection, links  the picture's neon colour, the island colour, the accent
#   prompt                    the two neon colours, so the prompt has the same presence as the bar's text
# Every text colour is checked against the terminal background and lifted until it is readable.
def readable(hexstr, bg, need):
    """The colour made lighter (hue and chroma kept) until it reaches `need` contrast on `bg`."""
    L, C, h = hex_to_oklch(hexstr)
    hx = hexstr
    for _ in range(60):
        hx = oklch_to_hex(L, C, h)
        if contrast(hx, bg) >= need or L >= 0.97:
            break
        L += 0.01
    return hx


def sgr(hexstr, bold=False):
    """A shell escape for a true-colour foreground, written the way $'...' wants it."""
    r, g, b = hex_to_rgb(hexstr)
    return "\\e[%s38;2;%d;%d;%dm" % ("1;" if bold else "0;", r, g, b)


def terminal_colours(R, ch):
    """-> dict with everything kitty and the prompt need (see the block comment above)."""
    L0, C0, H0 = hex_to_oklch(R["background"])
    bg = oklch_to_hex(clamp(L0 - 0.02, 0.10, 0.18), min(C0, 0.016), H0)    # a little darker than the bar
    bl, bc, bh = hex_to_oklch(bg)
    fg = R["on_surface"]
    mono = ch["mono"]
    base_c = TERM_C[0] if mono else clamp(0.07 + 0.6 * ch["c1"], *TERM_C)

    def hue_of(name):
        fixed = TERM_HUES[name]
        if mono:                                         # a picture with no colour: the usual terminal hues
            return fixed
        gap = hue_diff(ch["h1"], fixed)
        near = clamp(1.0 - (abs(gap) - 60) / 90, 0.0, 1.0)
        return (fixed + clamp(gap * TERM_LEAN[0], -TERM_LEAN[1], TERM_LEAN[1]) * near) % 360

    colours = [""] * 16
    for i, name in enumerate(("red", "green", "yellow", "blue", "magenta", "cyan"), start=1):
        h = hue_of(name)
        colours[i] = readable(oklch_to_hex(TERM_NORMAL_L, base_c, h), bg, TERM_MIN_CONTRAST)
        colours[i + 8] = readable(oklch_to_hex(TERM_BRIGHT_L, base_c, h), bg, TERM_MIN_CONTRAST)
    colours[0] = oklch_to_hex(bl + 0.07, bc, bh)                                   # "black": just off the background
    colours[8] = readable(oklch_to_hex(0.55, min(bc * 1.2, 0.03), bh), bg, TERM_MIN_CONTRAST)   # dim text and comments
    colours[7] = R["on_surface_variant"]
    l15, c15, h15 = hex_to_oklch(R["on_surface"])
    colours[15] = oklch_to_hex(min(0.97, l15 + 0.04), c15, h15)

    sel_bg = mix_hex(bg, R["primary_container"], 0.55)
    sel_fg = max((fg, bg), key=lambda c: contrast(c, sel_bg))
    neon_1 = readable(R["neon_1"], bg, TERM_MIN_CONTRAST)
    neon_2 = readable(R["neon_2"], bg, TERM_MIN_CONTRAST)
    link = readable(R["primary"], bg, TERM_MIN_CONTRAST)
    prompt = {
        "WP_BRACKET": sgr(readable(R["outline"], bg, TERM_MIN_CONTRAST)),
        "WP_NAME": sgr(neon_1, True),
        "WP_AT": sgr(R["on_surface_variant"]),
        "WP_HOST": sgr(neon_2, True),
        "WP_PATH": sgr(link),
        "WP_SIGIL": sgr(neon_1, True),
        "WP_RESET": "\\e[0m",
    }
    checks = {"foreground": contrast(fg, bg), "selected text": contrast(sel_fg, sel_bg),
              "cursor": contrast(neon_1, bg), "links": contrast(link, bg)}
    for i in range(1, 16):
        checks[f"color{i}"] = contrast(colours[i], bg)
    return {"background": bg, "foreground": fg, "selection_background": sel_bg, "selection_foreground": sel_fg,
            "cursor": neon_1 if TERM_SET_CURSOR else None, "url_color": link, "colours": colours,
            "prompt": prompt, "checks": checks}


# ---------------------------------------------------------------- the whole pipeline
def generate(path, pick="auto", engine="auto", prof=None):
    prof = prof or analyse(path)
    ch = choose(prof, pick)
    A, B, N, engine_note = generate_palettes(ch, prof, engine)
    R = compose(A, B, N)
    R, fixes = correct(R, ch, prof)
    R = add_neon(R, ch)
    R = add_right(R, ch)
    checks = measure(R)
    if ch["mono"]:
        look = "neutral, black and white picture"
    else:
        look = f"accent hue {ch['h1']:.0f}, second hue {ch['h2']:.0f}"
    worst = min(v / need for v, need in checks.values())
    note = f"{look} | {engine_note} | contrast checks {'all passed' if worst >= 1 else 'FAILED'}"
    return {"roles": R, "note": note, "checks": checks, "fixes": fixes, "choice": ch, "profile": prof,
            "engine": engine_note}


# ---------------------------------------------------------------- 4. WRITE
def write_css(roles, path, note):
    lines = ["/*", " * Css Colors", f" * Generated by wallpaper_palette.py  ({note})", " */"]
    lines += [f"@define-color {k} {v};" for k, v in sorted(roles.items())]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def write_atomic(path, text):
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def write_kitty(T, path, note):
    """A kitty colour file. kitty.conf pulls it in with:  include wallpaper-colors.conf"""
    lines = [f"# Generated by wallpaper_palette.py  ({note})",
             "# Rewritten at every wallpaper change, so do not edit it. kitty.conf includes it (put that line last).",
             "",
             f"foreground            {T['foreground']}",
             f"background            {T['background']}",
             f"selection_foreground  {T['selection_foreground']}",
             f"selection_background  {T['selection_background']}",
             f"url_color             {T['url_color']}"]
    if T["cursor"]:
        lines.append(f"cursor                {T['cursor']}")
    lines.append("")
    lines += [f"color{i:<2d} {c}" for i, c in enumerate(T["colours"])]
    write_atomic(path, "\n".join(lines) + "\n")


def write_prompt(T, path, note):
    """The prompt colours as shell variables; ~/.bashrc reads this file before every prompt."""
    lines = [f"# Generated by wallpaper_palette.py  ({note})",
             "# Read by the prompt in ~/.bashrc before every prompt. Rewritten at every wallpaper change."]
    lines += [f"{k}=$'{v}'" for k, v in T["prompt"].items()]
    write_atomic(path, "\n".join(lines) + "\n")


def swatch(hexstr):
    r, g, b = hex_to_rgb(hexstr)
    return "\x1b[48;2;%d;%d;%dm      \x1b[0m" % (r, g, b)


def preview_colour(c):
    return oklch_to_hex(0.70, min(0.14, max(0.08, c["chroma"])), c["hue"])


def main():
    ap = argparse.ArgumentParser(description="wallpaper -> colour.css")
    ap.add_argument("image")
    ap.add_argument("--out", help="where to write colour.css (default: print it)")
    ap.add_argument("--pick", default="auto", help="'auto', a candidate number from --list, or '#rrggbb'")
    ap.add_argument("--list", action="store_true", help="show the colours found in the picture and exit")
    ap.add_argument("--plain", action="store_true", help="like --list but machine readable: number, hex, percent")
    ap.add_argument("--report", action="store_true", help="show every check the correction layer made")
    ap.add_argument("--engine", default="auto", choices=("auto", "matugen", "builtin"),
                    help="which generator builds the palettes (default: Matugen if installed, else built-in)")
    ap.add_argument("--kitty", help="also write kitty's colours to this file (kitty.conf includes it)")
    ap.add_argument("--prompt", help="also write the shell prompt's colours to this file (~/.bashrc reads it)")
    args = ap.parse_args()

    try:
        prof = analyse(args.image)
    except FileNotFoundError:
        sys.exit(f"no such file: {args.image}")
    except Exception as e:  # unreadable / unsupported picture
        sys.exit(f"could not read {args.image}: {e}")
    cands = prof["cands"]

    if args.plain:
        for i, c in enumerate(cands):
            print(f"{i}\t{preview_colour(c)}\t{c['weight'] * 100:.0f}")
        return

    if args.list:
        if prof["mono"]:
            print("black and white picture: using a neutral silver palette (set MONO_ACCENT in the script to change that)")
        for i, c in enumerate(cands):
            col = preview_colour(c)
            print(f"  {i}  {swatch(col)}  {col}   {c['weight'] * 100:4.0f}% of the colour in the picture")
        if cands:
            print("best interface colour first. Use one with:  cycle_wallpaper.sh pick <number>")
        return

    res = generate(args.image, args.pick, args.engine, prof)
    if args.out:
        write_css(res["roles"], os.path.expanduser(args.out), res["note"])
        print(res["note"])
    elif not args.report:
        for k, v in sorted(res["roles"].items()):
            print(f"@define-color {k} {v};")
    # The terminal comes after the bar and can never stop it: if anything here goes wrong the bar
    # colours are already written, the problem is printed (it ends up in the log), and the exit stays 0.
    T = None
    if args.kitty or args.prompt or args.report:
        try:
            T = terminal_colours(res["roles"], res["choice"])
            if args.kitty:
                write_kitty(T, args.kitty, res["note"])
            if args.prompt:
                write_prompt(T, args.prompt, res["note"])
        except Exception as e:
            print(f"terminal colours not written: {e}", file=sys.stderr)

    if args.report:
        print(res["note"])
        ch = res["choice"]
        print(f"picture: brightness {prof['mean_light']:.2f}, {len(cands)} colour(s) found, "
              f"overall tint chroma {prof['tint_chroma']:.3f}")
        for k in ("primary", "tertiary", "primary_container", "surface", "on_surface", "neon_1", "neon_2", "right_light", "right_deep"):
            print(f"  {k:18} {res['roles'][k]}  {swatch(res['roles'][k])}")
        for name, (v, need) in res["checks"].items():
            print(f"  {'ok  ' if v >= need else 'FAIL'} {name:42} {v:5.1f}  (needs {need})")
        for line in res["fixes"]:
            print("  fixed:", line)
        if T:
            print(f"terminal: background {T['background']} {swatch(T['background'])}  foreground {T['foreground']}")
            print("  " + " ".join(swatch(c) for c in T["colours"][:8]))
            print("  " + " ".join(swatch(c) for c in T["colours"][8:]))
            low = sorted(T["checks"].items(), key=lambda kv: kv[1])[:3]
            print("  lowest contrast on the terminal background: " + ", ".join(f"{k} {v:.1f}" for k, v in low) + f" (needs {TERM_MIN_CONTRAST})")


if __name__ == "__main__":
    main()