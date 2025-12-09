# image_convert.py
import base64
import logging
import re
from urllib.parse import quote
from urllib.parse import quote_plus

import requests

log = logging.getLogger(__name__)

# LaTeX patternlar (shu modul ichida)
LATEX_BLOCK_RE = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)
LATEX_INLINE_RE = re.compile(r"\\\((.+?)\\\)")


def url_to_data_img_src(url: str, timeout: int = 20) -> str:
    """
    Oddiy rasm URL'ini yuklab, data:image/...;base64,... ko'rinishiga o'tkazadi.
    Word/PDF/offline holatda ham ishlaydi.
    """
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        img_bytes = resp.content

        mime = resp.headers.get("Content-Type") or "image/png"
        # Ba'zida Content-Type bo'sh yoki text/html bo'lishi mumkin,
        # lekin Word baribir rasm sifatida o'qiydi, shuning uchun shu qiymatni ishlatamiz.
        b64 = base64.b64encode(img_bytes).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        log.error(f"url_to_data_img_src: '{url}' ni data URL ga aylantirishda xatolik: {e}")
        # Agar yuklab bo'lmasa, hech bo'lmasa original URL qaytariladi (online ishlaydi)
        return url


def url_to_img_tag(
    url: str,
    inline: bool = True,
    max_width: str = "100%",
    extra_style: str = "",
) -> str:
    """
    Berilgan URL (masalan, AI rasm) dan <img> tegini yasaydi,
    lekin src ichiga data:image/...;base64,... qo'yadi.
    """
    data_src = url_to_data_img_src(url)

    style_parts = []
    if inline:
        style_parts.append("vertical-align:middle;")
    if max_width:
        style_parts.append(f"max-width:{max_width}; height:auto;")
    if extra_style:
        style_parts.append(extra_style)

    style_attr = ""
    if style_parts:
        style_attr = ' style="' + " ".join(style_parts) + '"'

    return f'<img src="{data_src}"{style_attr} />'


def latex_to_data_url(tex: str, dpi: int = 150) -> str:
    """
    LaTeX matndan codecogs orqali PNG olib, data URL (base64) qaytaradi.
    (Online faqat serverda – foydalanuvchi Word'ni offline ochadi.)
    """
    cleaned = " ".join(tex.strip().split())
    encoded = quote(cleaned)
    src_url = f"https://latex.codecogs.com/png.image?\\dpi{{{dpi}}} {encoded}"
    return url_to_data_img_src(src_url)


def latex_to_img_tag(tex: str, block: bool = False) -> str:
    """
    LaTeX matnni CodeCogs asosidagi PNG rasmga aylantiruvchi <img> teg.
    block=True bo'lsa, formulani alohida qator (markazda), 
    block=False bo'lsa, matn ichida inline ko'rinishda beradi.
    """
    # Ortiqcha probel va newlinelarni qisqartiramiz
    cleaned = " ".join(tex.strip().split())
    # CodeCogs uchun URL encoding (bo'shliqlarni + ga aylantiradi)
    encoded = quote_plus(cleaned)
    # Klassik endpoint: png.latex – fon oq, matn qora
    src = f"https://latex.codecogs.com/png.latex?\\dpi{{150}} {encoded}"

    # Block va inline uchun turli style
    if block:
        # Ekran markazida, yuqori/pastda joy ochib, katta formula ko‘rinishida
        style = "display:block; margin:12px auto; vertical-align:middle;"
    else:
        # Matn orasida kichik formula
        style = "display:inline-block; margin:0 2px; vertical-align:middle;"

    return f'<img src="{src}" style="{style}" />'




def replace_latex_with_images(text: str) -> str:
    """
    Matndagi \[ ... \] va \( ... \) LaTeX formulalarni <img> rasm bilan almashtiradi.
    Hammasi data:image/...;base64,... tarzida bo'ladi.
    """

    def _block_sub(m: re.Match) -> str:
        return latex_to_img_tag(m.group(1), block=True)

    def _inline_sub(m: re.Match) -> str:
        return latex_to_img_tag(m.group(1), block=False)

    text = LATEX_BLOCK_RE.sub(_block_sub, text)
    text = LATEX_INLINE_RE.sub(_inline_sub, text)
    return text
