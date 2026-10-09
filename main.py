#!/usr/bin/env python3
"""
مولّد ومجدول بوستات دينية للفيسبوك.

الاستخدام:
    python main.py --setup             # ربط الصفحة والمفاتيح (مرة واحدة)
    python main.py --dry-run           # تجربة: صورة + ريلز + بوست كتابة بدون نشر
    python main.py                     # يولّد الخطة اليومية، يعرضها، ويجدولها
    python main.py --yes               # نفس اللي فوق من غير سؤال
    python main.py --now --kind reel   # ينشر بوست واحد فوراً (image / reel / text)
    python main.py --monitor --loop    # يتابع التفاعل: يخفي العادي ويظهر الفيرال
"""
import argparse
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
import arabic_reshaper
from bidi.algorithm import get_display

BASE = Path(__file__).parent
OUT = BASE / "output"
HISTORY_FILE = BASE / "posted.json"
GRAPH_VERSION = os.environ.get("FB_GRAPH_VERSION", "v24.0").strip() or "v24.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
REEL_SIZE = (1080, 1920)


# ---------------------------------------------------------------- إعدادات

def load_env():
    """يقرأ ملف .env لو موجود (سطور KEY=VALUE). القيم اللي في الملف هي الأولى."""
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if v:
                    os.environ[k.strip()] = v


def read_env_file():
    d = {}
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip().strip('"').strip("'")
    return d


def write_env_file(d):
    lines = [f"{k}={v}" for k, v in d.items() if v]
    (BASE / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_config():
    path = BASE / "config.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"تعذر قراءة config.json: {exc}")
    required = ("page_name", "themes", "image", "schedule", "daily_plan")
    missing = [key for key in required if key not in cfg]
    if missing:
        raise SystemExit("config.json ينقصه: " + ", ".join(missing))
    return cfg


def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return []


def save_history(items):
    HISTORY_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- توليد النص

PROMPT = """أنت كاتب محتوى إسلامي لصفحة فيسبوك اسمها "{page}".
اكتب بوست واحد عن موضوع: {theme}
اللهجة: {dialect}.

القواعد المهمة:
- ممنوع تألف آية أو حديث أو تنسب كلام للنبي ﷺ أو للصحابة. استخدم فقط الأذكار والأدعية المشهورة جداً
  والمتفق عليها، ولو هتذكر آية اكتبها بنص المصحف بالظبط مع اسم السورة. لو مش متأكد 100% من نص أو
  صحة أي حاجة، متذكرهاش واكتب تذكرة بكلامك بدلاً منها.
- لا فتاوى ولا أحكام فقهية ولا كلام في السياسة أو الخلافات بين المذاهب.
{rule}
- متكررش أي نص من دول:
{recent}

رجّع JSON بس بالشكل ده:
{{"text": "النص اللي على الصورة", "caption": "الكابشن"}}
"""


RULES = {
    "visual": """- النص اللي هيتكتب على الصورة أو الفيديو قصير: من 12 لـ 35 كلمة، بدون إيموجي.
- الكابشن: جملتين أو تلاتة بالمصري، ممكن إيموجي بسيط، وفي الآخر 3 لـ 5 هاشتاجات عربي.""",
    "text": """- ده بوست كتابة بس (من غير صورة): في "text" اكتب البوست نفسه، من 40 لـ 90 كلمة، بأسلوب دافي وقريب
  من القلب، ممكن إيموجي بسيط (واحد أو اتنين)، وفي الآخر سؤال أو دعوة للدعاء بتشجع الناس تتفاعل.
- في "caption" اكتب 3 لـ 5 هاشتاجات عربي بس.""",
}


def _extract_json(raw):
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("الرد مش JSON: " + raw[:200])
    return json.loads(m.group(0))


_GEMINI_MODEL = {}



def _pick_gemini_model(key, exclude=None):
    """اختيار موديل Flash متاح، مع إمكانية استبعاد موديل فشل."""
    try:
        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": key},
            params={"pageSize": 200},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"تعذر جلب قائمة موديلات Gemini: HTTP {r.status_code}")
            return None

        candidates = []
        for m in r.json().get("models", []):
            name = m.get("name", "").replace("models/", "")
            match = re.fullmatch(
                r"gemini-(\d+(?:\.\d+)?)-flash(-preview[\w-]*)?",
                name,
            )
            if not match or name == exclude:
                continue
            if "generateContent" not in m.get(
                "supportedGenerationMethods", []
            ):
                continue

            version = float(match.group(1))
            is_stable = "preview" not in name
            candidates.append((is_stable, version, name))

        candidates.sort(reverse=True)
        return candidates[0][2] if candidates else None

    except Exception as e:
        print(f"تعذر اكتشاف موديل بديل: {e}")
        return None


def _generate_once(cfg, theme, recent, kind="image"):
    prompt = PROMPT.format(
        page=cfg["page_name"],
        theme=theme,
        dialect=cfg["dialect"],
        recent="\n".join("- " + t for t in recent[-30:]) or "(لا يوجد)",
        rule=RULES["text" if kind == "text" else "visual"],
    )
    provider = cfg.get("ai_provider", "gemini")

    if provider == "gemini":
        key = os.environ["GEMINI_API_KEY"]
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9},
        }

        def call(model):
            # المفاتيح الجديدة (AQ.) لازم تتبعت في الهيدر مش في الرابط
            return requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key}, json=body, timeout=60)

        model = _GEMINI_MODEL.get("m") or cfg["gemini_model"]
        r = call(model)
        
if (
    r.status_code in (429, 500, 502, 503, 504)
    or r.status_code == 404
    or (
        r.status_code == 400
        and "not found" in r.text.lower()
    )
):
    alt = _pick_gemini_model(key, exclude=model)
    if alt:
        print(
            f"   Gemini HTTP {r.status_code}: "
            f"switching from {model} to {alt}"
        )
        _GEMINI_MODEL["m"] = alt
        model = alt
        r = call(alt)
        if r.status_code != 200:
            raise requests.HTTPError(f"Gemini HTTP {r.status_code}: {r.text[:300]}")
        raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    elif provider == "anthropic":
        key = os.environ["ANTHROPIC_API_KEY"]
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json={
                "model": cfg["anthropic_model"],
                "max_tokens": 600,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        r.raise_for_status()
        raw = r.json()["content"][0]["text"]
    else:
        sys.exit("ai_provider لازم يكون gemini أو anthropic")

    data = _extract_json(raw)
         
         
    return data["text"].strip(), data["caption"].strip()


def generate_text(cfg, theme, recent, kind="image"):
    provider = cfg.get("ai_provider", "gemini")
    env_name = "GEMINI_API_KEY" if provider == "gemini" else "ANTHROPIC_API_KEY"
    if not os.environ.get(env_name):
        sys.exit(f"ناقص {env_name}. شغّل: python main.py --setup")
    last = None
    for attempt in range(1, 5):
        try:
            return _generate_once(cfg, theme, recent, kind)
        except (requests.RequestException, ValueError, KeyError, IndexError) as e:
            last = e
            wait = 8 * attempt
            print(f"   Attempt {attempt} failed ({e.__class__.__name__}): {str(e)[:300]}\n   retrying in {wait}s...")
            time.sleep(wait)
    raise RuntimeError(f"الـ AI فشل بعد 4 محاولات: {last}")


# ---------------------------------------------------------------- توليد الصورة

from PIL import features

# لو Pillow فيه raqm فهو بيشكّل الحروف العربية ويظبط الاتجاه لوحده،
# ولو معاد تشكيلها هنا النص هيتعكس. فبنعالجها بنفسنا بس لو raqm مش موجود.
HAS_RAQM = features.check("raqm")


def ar(text):
    # بعض الخطوط مفيهاش رمز ﷺ فبنكتبه كلمات على الصورة والفيديو
    text = text.replace("ﷺ", "صلى الله عليه وسلم")
    if HAS_RAQM:
        return text
    return get_display(arabic_reshaper.reshape(text))


def find_font(cfg_path):
    candidates = [
        BASE / cfg_path,
        Path("C:/Windows/Fonts/tahomabd.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("C:/Windows/Fonts/tahoma.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/System/Library/Fonts/GeezaPro.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    sys.exit("مفيش خط عربي. نزّل Cairo-Bold.ttf وحطه في فولدر fonts (راجع README).")


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def gradient(w, h, c1, c2):
    top, bot = hex_rgb(c1), hex_rgb(c2)
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        t = y / (h - 1)
        row = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        for x in range(w):
            px[x, y] = row
    return img


def wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if draw.textlength(ar(trial), font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _star_pattern(W, H, color, tile=130):
    """زخرفة نجمة ثمانية (مربعين متداخلين) مكررة على الصورة كلها."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    r = tile * 0.42
    for cx in range(0, W + tile, tile):
        for cy in range(0, H + tile, tile):
            for rot in (0, math.pi / 4):
                pts = [
                    (cx + r * math.cos(rot + k * math.pi / 2 + math.pi / 4),
                     cy + r * math.sin(rot + k * math.pi / 2 + math.pi / 4))
                    for k in range(4)
                ]
                d.polygon(pts, outline=color)
            d.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], outline=color)
    return layer


def _crescent(size, color):
    """هلال ذهبي صغير."""
    s = size * 4  # نرسم بدقة أعلى ثم نصغّر عشان الحواف تطلع ناعمة
    outer = Image.new("L", (s, s), 0)
    ImageDraw.Draw(outer).ellipse([0, 0, s - 1, s - 1], fill=255)
    cut = Image.new("L", (s, s), 0)
    off = int(s * 0.22)
    ImageDraw.Draw(cut).ellipse([off, int(s * 0.04), s - 1 + off, s - 1 - int(s * 0.04)], fill=255)
    mask = ImageChops.subtract(outer, cut).resize((size, size), Image.LANCZOS)
    layer = Image.new("RGBA", (size, size), color)
    layer.putalpha(mask)
    return layer


def _diamond(d, cx, cy, r, fill):
    d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=fill)


def _make_base(cfg, theme, size=None, top_extra=0, bottom_extra=0):
    """الخلفية الكاملة (زخرفة + إطار + هلال + علامة مائية) من غير النص الرئيسي."""
    ic = cfg["image"]
    W, H = size or (ic["width"], ic["height"])
    pal = random.choice(ic["backgrounds"])
    c1, c2 = pal[0], pal[1]
    gold = hex_rgb(pal[2]) if len(pal) > 2 else (212, 175, 95)
    img = gradient(W, H, c1, c2).convert("RGBA")
    font_path = find_font(ic["font_path"])

    # 1) إضاءة ناعمة ورا النص
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([W * 0.12, H * 0.22, W * 0.88, H * 0.72], fill=(255, 255, 255, 55))
    glow = glow.filter(ImageFilter.GaussianBlur(120))
    img = Image.alpha_composite(img, glow)

    # 2) زخرفة النجوم الإسلامية خفيفة
    img = Image.alpha_composite(img, _star_pattern(W, H, (255, 255, 255, 20)))

    # 3) تعتيم الأطراف (vignette)
    vig = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vig).ellipse([-W * 0.25, -H * 0.15, W * 1.25, H * 1.15], fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(160))
    dark = Image.new("RGBA", (W, H), (0, 0, 0, 110))
    dark.putalpha(ImageChops.invert(vig).point(lambda v: int(v * 110 / 255)))
    img = Image.alpha_composite(img, dark)

    draw = ImageDraw.Draw(img)

    # 4) إطار ذهبي مزدوج + معينات في الأركان
    m = 36
    draw.rectangle([m, m, W - m, H - m], outline=gold + (230,), width=3)
    draw.rectangle([m + 14, m + 14, W - m - 14, H - m - 14], outline=gold + (110,), width=1)
    for (cx, cy) in [(m, m), (W - m, m), (m, H - m), (W - m, H - m)]:
        _diamond(draw, cx, cy, 14, gold + (255,))
        _diamond(draw, cx, cy, 6, hex_rgb(c1) + (255,))

    # 5) هلال في الأعلى + اسم الموضوع
    img.alpha_composite(_crescent(86, gold + (255,)), ((W - 86) // 2, 90 + top_extra))
    draw = ImageDraw.Draw(img)
    if theme:
        tf = ImageFont.truetype(font_path, 36)
        t_disp = ar(theme)
        tw = draw.textlength(t_disp, font=tf)
        draw.text(((W - tw) / 2, 200 + top_extra), t_disp, font=tf, fill=gold + (255,))

    # 6) فاصل ذهبي + العلامة المائية
    cy = H - 150 - bottom_extra
    draw.line([(W / 2 - 200, cy), (W / 2 - 22, cy)], fill=gold + (200,), width=2)
    draw.line([(W / 2 + 22, cy), (W / 2 + 200, cy)], fill=gold + (200,), width=2)
    _diamond(draw, W / 2, cy, 9, gold + (255,))
    wm_font = ImageFont.truetype(font_path, 34)
    wm = ar(cfg["watermark_text"])
    wm_w = draw.textlength(wm, font=wm_font)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    alpha = int(cfg.get("watermark_opacity", 150))
    od.text(((W - wm_w) / 2, H - 125 - bottom_extra), wm, font=wm_font, fill=(255, 255, 255, alpha))
    img = Image.alpha_composite(img, overlay)
    return img, font_path


def _layout(draw, font_path, text, W, H, top_extra=0, bottom_extra=0):
    """يقسم النص لسطور ويختار حجم الخط المناسب."""
    top, bottom = 280 + top_extra, H - 250 - bottom_extra
    box_w, box_h = W - 220, bottom - top
    size = 82
    while size > 30:
        font = ImageFont.truetype(font_path, size)
        lines = wrap(draw, text, font, box_w)
        line_h = int(size * 1.7)
        if len(lines) * line_h <= box_h:
            break
        size -= 4
    y0 = top + (box_h - len(lines) * line_h) // 2
    return font, lines, y0, line_h


def make_image(cfg, text, path, theme=""):
    W, H = cfg["image"]["width"], cfg["image"]["height"]
    img, font_path = _make_base(cfg, theme)
    draw = ImageDraw.Draw(img)
    font, lines, y, line_h = _layout(draw, font_path, text, W, H)
    for line in lines:
        disp = ar(line)
        w = draw.textlength(disp, font=font)
        x = (W - w) / 2
        draw.text((x + 2, y + 3), disp, font=font, fill=(0, 0, 0, 120))
        draw.text((x, y), disp, font=font, fill=(255, 255, 255, 255))
        y += line_h
    img.convert("RGB").save(path, "JPEG", quality=93)


def have_ffmpeg():
    return shutil.which("ffmpeg") is not None


def make_video(cfg, text, path, theme="", fps=24):
    """فيديو صامت: النص بيظهر كلمة كلمة بتلاشي ناعم، وفي الآخر ثبات ثواني."""
    # مقاس الريلز 9:16، والنص والعلامة المائية بعيد عن مناطق أزرار وكابشن الريلز
    W, H = REEL_SIZE
    top_x, bot_x = 90, 260
    base, font_path = _make_base(cfg, theme, size=REEL_SIZE, top_extra=top_x, bottom_extra=bot_x)
    probe = ImageDraw.Draw(base)
    font, lines, y, line_h = _layout(probe, font_path, text, W, H, top_x, bot_x)

    # مكان كل كلمة (بنرصها من اليمين لليسار لأن الكلام عربي)
    space = probe.textlength(ar("ا ا"), font=font) - probe.textlength(ar("اا"), font=font)
    space = max(space, font.size * 0.28)
    words = []  # (نص معروض، x، y)
    for line in lines:
        parts = line.split()
        widths = [probe.textlength(ar(p), font=font) for p in parts]
        total = sum(widths) + space * (len(parts) - 1)
        x = (W + total) / 2
        for p, wd in zip(parts, widths):
            x -= wd
            words.append((ar(p), x, y))
            x -= space
        y += line_h

    step, fade, lead, hold = 0.32, 0.45, 0.6, 2.8
    if not words:
        raise ValueError("لا يوجد نص صالح لإنشاء الفيديو")
    starts = [lead + i * step for i in range(len(words))]
    duration = starts[-1] + fade + hold
    n_frames = int(duration * fps)

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "veryfast",
        "-movflags", "+faststart", str(path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    acc = Image.new("RGBA", (W, H), (0, 0, 0, 0))   # الكلمات اللي ظهرت بالكامل
    done = 0
    for f in range(n_frames):
        t = f / fps
        while done < len(words) and t >= starts[done] + fade:
            d = ImageDraw.Draw(acc)
            disp, x, yy = words[done]
            d.text((x + 2, yy + 3), disp, font=font, fill=(0, 0, 0, 120))
            d.text((x, yy), disp, font=font, fill=(255, 255, 255, 255))
            done += 1
        frame = Image.alpha_composite(base, acc)
        cur = [i for i in range(done, len(words)) if starts[i] <= t]
        if cur:
            layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(layer)
            for i in cur:
                a = min(1.0, (t - starts[i]) / fade)
                disp, x, yy = words[i]
                rise = (1 - a) * 14  # الكلمة بتطلع لفوق وهي بتظهر
                d.text((x + 2, yy + 3 + rise), disp, font=font, fill=(0, 0, 0, int(120 * a)))
                d.text((x, yy + rise), disp, font=font, fill=(255, 255, 255, int(255 * a)))
            frame = Image.alpha_composite(frame, layer)
        proc.stdin.write(frame.convert("RGB").tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg فشل في إنشاء الفيديو")


# ---------------------------------------------------------------- فيسبوك

def _sched(data, publish_ts):
    if publish_ts:
        data["published"] = "false"
        data["scheduled_publish_time"] = str(int(publish_ts))


def _fail(r):
    try:
        payload = r.json()
    except ValueError:
        payload = {"raw": r.text[:1000]}
    if not r.ok or (isinstance(payload, dict) and "error" in payload):
        error = payload.get("error", payload) if isinstance(payload, dict) else payload
        if isinstance(error, dict):
            message = error.get("message", str(error))
            code = error.get("code")
            subcode = error.get("error_subcode")
            details = f" (code={code}, subcode={subcode})" if code is not None else ""
            raise RuntimeError(f"فيسبوك رفض الطلب: {message}{details}")
        raise RuntimeError(f"فيسبوك رفض الطلب: {error}")
    return payload


def post_photo(path, caption, publish_ts=None):
    page_id, token = os.environ["FB_PAGE_ID"], os.environ["FB_PAGE_TOKEN"]
    data = {"caption": caption, "access_token": token}
    _sched(data, publish_ts)
    with open(path, "rb") as f:
        r = requests.post(f"{GRAPH}/{page_id}/photos", data=data, files={"source": f}, timeout=120)
    return _fail(r)


def post_text(message, publish_ts=None):
    page_id, token = os.environ["FB_PAGE_ID"], os.environ["FB_PAGE_TOKEN"]
    data = {"message": message, "access_token": token}
    _sched(data, publish_ts)
    return _fail(requests.post(f"{GRAPH}/{page_id}/feed", data=data, timeout=60))


def post_reel(path, caption, publish_ts=None):
    """ريلز حقيقي: 3 مراحل (start ← رفع الملف ← finish)."""
    page_id, token = os.environ["FB_PAGE_ID"], os.environ["FB_PAGE_TOKEN"]
    url = f"{GRAPH}/{page_id}/video_reels"

    # 1) بداية الرفع
    start = _fail(requests.post(url, data={"upload_phase": "start", "access_token": token}, timeout=60))
    video_id = start["video_id"]
    upload_url = start.get("upload_url") or f"https://rupload.facebook.com/video-upload/v21.0/{video_id}"

    # 2) رفع الملف نفسه
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        r = requests.post(
            upload_url,
            headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)},
            data=f,
            timeout=900,
        )
    _fail(r)

    # 3) إنهاء ونشر (فوراً أو مجدول)
    data = {
        "upload_phase": "finish",
        "video_id": video_id,
        "description": caption,
        "access_token": token,
        "video_state": "SCHEDULED" if publish_ts else "PUBLISHED",
    }
    if publish_ts:
        data["scheduled_publish_time"] = str(int(publish_ts))
    res = _fail(requests.post(url, data=data, timeout=120))
    res["video_id"] = video_id
    return res


def publish(kind, media_path, text, caption, publish_ts=None):
    if kind == "reel":
        return post_reel(media_path, caption, publish_ts)
    if kind == "text":
        return post_text(text + "\n\n" + caption, publish_ts)
    return post_photo(media_path, caption, publish_ts)


# ---------------------------------------------------------------- الجدولة

def build_slots(cfg):
    sc = cfg["schedule"]
    tz = timezone(timedelta(hours=sc.get("timezone_offset_hours", 3)))
    now = datetime.now(tz)
    days = min(int(sc["days_ahead"]), 29)  # فيسبوك بيسمح بحد أقصى حوالي 30 يوم
    times = sc["times"][: sc["posts_per_day"]]
    slots = []
    for d in range(days + 1):
        day = (now + timedelta(days=d)).date()
        for t in times:
            hh, mm = map(int, t.split(":"))
            dt = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
            if dt > now + timedelta(minutes=15):
                slots.append(dt)
    return slots


def _spread(kinds):
    """بيوزع الأنواع بحيث نفس النوع ميجيش مرتين ورا بعض لو ممكن."""
    for _ in range(20000):
        random.shuffle(kinds)
        if all(a != b for a, b in zip(kinds, kinds[1:])):
            break
    return kinds


def build_plan(cfg, can_reel):
    """بيرجّع [(موعد, نوع)] حسب daily_plan: مثلاً 3 ريلز + 5 صور + 5 كتابة في اليوم."""
    plan = cfg.get("daily_plan", {"reel": 3, "image": 5, "text": 5})
    pool = [k for k, n in plan.items() for _ in range(int(n))]
    if not pool:
        sys.exit("daily_plan في config.json فاضية.")
    by_day = {}
    for s in build_slots(cfg):
        by_day.setdefault(s.date(), []).append(s)
    out = []
    for day_slots in by_day.values():
        k = len(day_slots)
        kinds = random.sample(pool, k) if k <= len(pool) else pool + random.choices(pool, k=k - len(pool))
        kinds = _spread(list(kinds))
        for s, kind in zip(day_slots, kinds):
            out.append((s, "image" if (kind == "reel" and not can_reel) else kind))
    return out


# ---------------------------------------------------------------- مراقبة الفيرال (إخفاء / إظهار)

def graph_get(path, **params):
    params["access_token"] = os.environ["FB_PAGE_TOKEN"]
    return _fail(requests.get(f"{GRAPH}/{path}", params=params, timeout=60))


def graph_update(path, **data):
    data["access_token"] = os.environ["FB_PAGE_TOKEN"]
    return _fail(requests.post(f"{GRAPH}/{path}", data=data, timeout=60))


def set_hidden(node, hidden):
    graph_update(node, is_hidden="true" if hidden else "false")


def resolve_node(e):
    """بيجيب رقم البوست الفعلي اللي بنقدر نقيسه ونخفيه."""
    if e.get("node"):
        return e["node"]
    fb, kind = e["fb"], e["kind"]
    node = None
    try:
        if kind == "text":
            node = fb.get("id")
        elif kind == "image":
            node = fb.get("post_id") or graph_get(fb["id"], fields="page_story_id").get("page_story_id")
        else:  # reel
            vid = fb.get("video_id")
            try:
                node = graph_get(vid, fields="post_id").get("post_id")
            except RuntimeError:
                node = None
            node = node or vid
    except Exception as ex:
        print("   مقدرتش أعرف رقم البوست:", ex)
    e["node"] = node
    return node


def get_metrics(e, node):
    base = "reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0)"
    try:
        d = graph_get(node, fields=base + ",shares")
    except RuntimeError:
        d = graph_get(node, fields=base)
    reactions = d.get("reactions", {}).get("summary", {}).get("total_count", 0)
    comments = d.get("comments", {}).get("summary", {}).get("total_count", 0)
    shares = d.get("shares", {}).get("count", 0)
    views = 0
    if e["kind"] == "reel" and e["fb"].get("video_id"):
        try:
            ins = graph_get(f"{e['fb']['video_id']}/video_insights", metric="blue_reels_play_count")
            views = ins["data"][0]["values"][0]["value"]
        except Exception:
            pass
    return reactions, comments, shares, views


def monitor_once(cfg):
    vis = cfg.get("visibility", {})
    do_hide = vis.get("enabled", True)
    hide_after = float(vis.get("hide_after_hours", 6)) * 3600
    viral_score = float(vis.get("viral_score", 300))
    watch_for = float(vis.get("watch_days", 7)) * 86400
    history = load_history()
    now = time.time()
    n = 0
    for e in history:
        ts = e.get("ts")
        if not e.get("fb") or not ts or ts > now or e.get("done"):
            continue
        age = now - ts
        if age > watch_for:
            e["done"] = True
            continue
        node = resolve_node(e)
        if not node:
            continue
        try:
            reactions, comments, shares, views = get_metrics(e, node)
        except Exception as ex:
            print(f"   فشل قياس {node}: {ex}")
            continue
        score = reactions + 2 * comments + 4 * shares + views / 50
        e["score"] = round(score, 1)
        status = "ظاهر" if not e.get("hidden") else "مخفي"
        try:
            if score >= viral_score:
                if e.get("hidden"):
                    set_hidden(node, False)
                    e["hidden"] = False
                    status = "🔥 فيرال: اتظهر تاني"
                else:
                    status = "🔥 فيرال (ظاهر)"
                e["viral"] = True
                e["done"] = True
            elif do_hide and age >= hide_after and not e.get("hidden"):
                set_hidden(node, True)
                e["hidden"] = True
                status = "اتخفى"
        except Exception as ex:
            status += f" (فشل التعديل: {ex})"
        n += 1
        print(f"  {e['kind']:5} | سكور {score:7.1f} (❤{reactions} 💬{comments} ↗{shares} ▶{views}) | {status} | {e['text'][:30]}")
        save_history(history)
    save_history(history)
    if n == 0:
        print("  مفيش بوستات منشورة محتاجة متابعة دلوقتي.")


def monitor(cfg, loop):
    minutes = int(cfg.get("visibility", {}).get("check_every_minutes", 30))
    while True:
        print(f"\n[{datetime.now():%Y-%m-%d %H:%M}] متابعة البوستات...")
        monitor_once(cfg)
        if not loop:
            return
        print(f"هتابع تاني بعد {minutes} دقيقة. (Ctrl+C للإيقاف)")
        time.sleep(minutes * 60)


# ---------------------------------------------------------------- معالج الإعداد

def ask(prompt, default="", secret=False):
    shown = (default[:4] + "..." + default[-3:]) if (secret and len(default) > 10) else default
    v = input(f"{prompt}" + (f" [{shown}]" if shown else "") + ": ").strip()
    return v or default


def _graph_json(r):
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}


def setup_wizard():
    """بيسألك، ويحفظ كل إجابة في .env أول بأول، فلو حصل خطأ مش هتكتب كل حاجة من الأول."""
    print("=== Setup / إعداد البرنامج ===")
    print("Press Enter to keep the saved value shown in [brackets].\n")
    env = read_env_file()
    cfg = load_config()
    cfg["page_name"] = ask("Page name / اسم الصفحة", cfg["page_name"])
    handle = ask("Page link for watermark (e.g. facebook.com/mypage) / رابط الصفحة", "")
    cfg["watermark_text"] = cfg["page_name"] + (f"  |  {handle}" if handle else "")
    (BASE / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\nGemini key: aistudio.google.com/apikey")
    env["GEMINI_API_KEY"] = ask("GEMINI_API_KEY", env.get("GEMINI_API_KEY", ""), secret=True)
    write_env_file(env)

    print("\nFacebook: developers.facebook.com/tools/explorer")
    print("Permissions: pages_show_list, pages_read_engagement, pages_manage_posts")
    print("IMPORTANT: in the Facebook popup, choose your PAGE (tick it) before you continue.")
    env["FB_APP_ID"] = ask("App ID (Settings > Basic)", env.get("FB_APP_ID", ""))
    env["FB_APP_SECRET"] = ask("App Secret", env.get("FB_APP_SECRET", ""), secret=True)
    write_env_file(env)
    short = ask("Access token from Graph API Explorer", "", secret=True)
    if not short:
        sys.exit("No token entered. Run setup again (your other answers are saved).")

    page_direct = None
    try:
        r = requests.get(f"{GRAPH}/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": env["FB_APP_ID"],
            "client_secret": env["FB_APP_SECRET"], "fb_exchange_token": short}, timeout=60)
        long_user = _fail(r)["access_token"]
        try:
            pages = _fail(requests.get(f"{GRAPH}/me/accounts", params={
                "access_token": long_user, "limit": 100}, timeout=60))["data"]
        except Exception as e:
            if "nonexisting field (accounts)" not in str(e):
                raise
            # التوكن ده توكن صفحة مش مستخدم: /me هو الصفحة نفسها، فنستخدمه مباشرة
            me = _fail(requests.get(f"{GRAPH}/me", params={
                "access_token": long_user, "fields": "id,name"}, timeout=60))
            page_direct = {"id": me["id"], "name": me.get("name", ""), "access_token": long_user}
            pages = [page_direct]
            print("\nNote: this is a PAGE token, so it will be used directly.")
            print("It may expire in about 60 days; if posting stops later, run option 4 again.")
    except Exception as e:
        sys.exit(f"Facebook error: {e}\n(Your other answers are saved. Run setup again with a new token.)")

    if not pages:
        print("\nNo pages were returned for this token. Checking why...")
        try:
            perms = _graph_json(requests.get(f"{GRAPH}/me/permissions", params={"access_token": long_user}, timeout=60))
            me = _graph_json(requests.get(f"{GRAPH}/me", params={"access_token": long_user, "fields": "id,name"}, timeout=60))
            print("Logged in as:", me.get("name"), me.get("id"))
            for p in perms.get("data", []):
                print(f"  {p.get('permission')}: {p.get('status')}")
        except Exception:
            pass
        sys.exit("\nFix: generate a NEW token in Graph API Explorer, choose 'User Token', tick the 3 permissions,\n"
                 "and in the Facebook popup press 'Edit settings' / tick your PAGE, then Continue.\n"
                 "Make sure you log in with the account that is Admin of the page.\n"
                 "Then run option 4 again (everything else is already saved).")

    print("\nPages you manage / الصفحات:")
    for i, p in enumerate(pages, 1):
        print(f"  {i}) {p['name']}  ({p['id']})")
    while True:
        try:
            idx = int(ask("Choose page number / اختار رقم صفحتك", "1")) - 1
            if 0 <= idx < len(pages):
                break
        except ValueError:
            pass
        print(f"اكتب رقمًا من 1 إلى {len(pages)}.")
    page = pages[idx]

    env["FB_PAGE_ID"] = page["id"]
    env["FB_PAGE_TOKEN"] = page["access_token"]
    write_env_file(env)
    print(f"\nDone! Connected to page: {page['name']}")
    print("Saved in .env. You will NOT be asked again next time.")
    print("Try now: python main.py --dry-run")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true", help="معالج الإعداد (ربط الصفحة والمفاتيح)")
    ap.add_argument("--dry-run", action="store_true", help="تجربة: واحد من كل نوع بدون نشر")
    ap.add_argument("--yes", action="store_true", help="من غير سؤال تأكيد")
    ap.add_argument("--now", action="store_true", help="انشر بوست واحد دلوقتي")
    ap.add_argument("--kind", choices=["image", "reel", "text"], help="نوع البوست مع --now")
    ap.add_argument("--monitor", action="store_true", help="تابع التفاعل: اخفي العادي وأظهر الفيرال")
    ap.add_argument("--loop", action="store_true", help="مع --monitor: كرر المتابعة كل شوية")
    ap.add_argument("--version", action="version", version=f"fb-posts 1.1 (Meta Graph API {GRAPH_VERSION})")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    load_env()
    if args.setup:
        return setup_wizard()
    missing = [k for k in ("GEMINI_API_KEY", "FB_PAGE_ID", "FB_PAGE_TOKEN") if not os.environ.get(k)]
    if missing and not args.dry_run:
        sys.exit("Missing in .env: " + ", ".join(missing) + "\nChoose option 4 (setup) first.")
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("Missing GEMINI_API_KEY. Choose option 4 (setup) first.")
    cfg = load_config()
    if args.monitor:
        return monitor(cfg, args.loop)

    OUT.mkdir(exist_ok=True)
    history = load_history()
    recent = [h["text"] for h in history]

    can_reel = have_ffmpeg()
    if not can_reel:
        print("ملحوظة: ffmpeg مش متسطّب، فمفيش ريلز. هنعمل صور وكتابة بس (راجع README).")

    if args.dry_run:
        items = [(None, k) for k in ("image", "reel", "text") if k != "reel" or can_reel]
    elif args.now:
        kind = args.kind or "image"
        if kind == "reel" and not can_reel:
            sys.exit("الريلز محتاج ffmpeg.")
        items = [(None, kind)]
    else:
        items = build_plan(cfg, can_reel)
    print(f"هولّد {len(items)} بوست... (ممكن ياخد كام دقيقة)")

    NAMES = {"image": "صورة", "reel": "ريلز", "text": "كتابة"}
    batch = []
    for i, (slot, kind) in enumerate(items, 1):
        day = slot or datetime.now()
        friday = cfg.get("friday_themes") and day.weekday() == 4
        theme = random.choice(cfg["friday_themes"] if friday else cfg["themes"])

        text, caption = generate_text(cfg, theme, recent + [b["text"] for b in batch],
                                      "text" if kind == "text" else "image")
        stamp = f"{datetime.now():%Y%m%d_%H%M%S}_{i}"
        if kind == "reel":
            media = OUT / f"reel_{stamp}.mp4"
            make_video(cfg, text, media, theme)
        elif kind == "image":
            media = OUT / f"post_{stamp}.jpg"
            make_image(cfg, text, media, theme)
        else:
            media = OUT / f"text_{stamp}.txt"
            media.write_text(text + "\n\n" + caption, encoding="utf-8")
        batch.append({"text": text, "caption": caption, "media": media, "slot": slot,
                      "theme": theme, "kind": kind})
        when = slot.strftime("%Y-%m-%d %H:%M") if slot else "-"
        print(f"[{i}/{len(items)}] {when} | {NAMES[kind]} | {theme}\n    {text}")
        time.sleep(cfg.get("ai_delay_seconds", 4))  # عشان حد الطلبات في Gemini المجاني

    if args.dry_run:
        print("\nخلصت التجربة. افتح فولدر output وشوف الصورة والريلز والنص.")
        return

    print(f"\nاتعمل {len(batch)} بوست في فولدر output.")
    print("⚠️ راجع النصوص قبل النشر، خصوصاً أي آية أو دعاء.")
    if not args.yes:
        if input("أنشر/أجدول كلهم؟ (y/n): ").strip().lower() != "y":
            print("اتلغى. مفيش حاجة اتنشرت.")
            return

    for b in batch:
        ts = b["slot"].timestamp() if b["slot"] else None
        try:
            res = publish(b["kind"], b["media"], b["text"], b["caption"], ts)
            history.append({"text": b["text"], "theme": b["theme"], "kind": b["kind"], "fb": res,
                            "at": str(b["slot"] or "now"), "ts": ts or time.time(),
                            "hidden": False, "viral": False})
            save_history(history)
            print("تم:", b["slot"] or "نُشر الآن", b["kind"])
        except Exception as e:
            print("فشل:", b["kind"], e)
        time.sleep(2)
    print("\nلمتابعة التفاعل وإخفاء/إظهار البوستات: python main.py --monitor --loop")


if __name__ == "__main__":
    main()
