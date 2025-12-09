# image_ai.py

import os
import re
import time
import logging
import requests
from image_convert import url_to_img_tag

log = logging.getLogger(__name__)

# DEAPI tokenni .env ichida DEAPI_TOKEN sifatida saqlaysiz
DEAPI_TOKEN = "797|Jd0EzXlxiOdLuMX1vcoC7Hth8u5ggWOeLKPutt7d48e73cbc"

DEAPI_BASE_URL = "https://api.deapi.ai/api/v1/client"
PLACEHOLDER_URL = "https://via.placeholder.com/800x600.png?text=AI+Image"

# [RASM 1: ...] markerlarini topish uchun
IMAGE_MARKER_RE = re.compile(r"\[RASM\s+(\d+):\s*([^\]]+)\]")


def _is_http_url(value: str | None) -> bool:
    """
    Faqat http/https URL ekanligini tekshiradigan yordamchi.
    Base64, bo'sh satr va boshqalarni rad etamiz.
    """
    if not value:
        return False
    v = value.strip().lower()
    return v.startswith("http://") or v.startswith("https://")


def _deapi_txt2img_request(prompt: str) -> str | None:
    """
    Botdagi ishlayotgan logika asosida:
    - POST /txt2img => request_id olamiz
    """
    if not DEAPI_TOKEN:
        log.warning("DEAPI_TOKEN topilmadi, placeholder ishlatiladi")
        return None

    txt2img_url = f"{DEAPI_BASE_URL}/txt2img"

    headers = {
        "Authorization": f"Bearer {DEAPI_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    payload = {
        "prompt": prompt,
        "negative_prompt": "blur, darkness, noise, low quality, artifacts, text, watermark",
        "model": "Flux1schnell",
        "loras": [],          # muammo bo‘lmasligi uchun bo‘sh
        "width": 512,
        "height": 512,
        "guidance": 7.5,
        "steps": 8,           # 10 dan oshmasin
        "seed": 42,
    }

    try:
        log.debug("DeAPI txt2img so'rov yuborilmoqda...")
        resp = requests.post(txt2img_url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        request_id = (data.get("data") or {}).get("request_id")
        log.debug("DeAPI txt2img javobi request_id=%s", request_id)
        if not request_id:
            log.error("DeAPI javobida request_id topilmadi: %s", data)
            return None
        return request_id
    except Exception as e:
        log.exception("DeAPI txt2img error: %s", e)
        return None


def _deapi_poll_result(request_id: str, max_attempts: int = 12, interval_sec: int = 3) -> str | None:
    """
    Botdagi uslub bilan:
    - GET /request-status/{request_id} ni bir necha marta tekshiradi
    - faqat HTTP(S) URL bo'lgan result_url/result/preview maydonidan rasm URL qaytaradi
    """
    if not DEAPI_TOKEN:
        return None

    status_url = f"{DEAPI_BASE_URL}/request-status/{request_id}"
    headers = {
        "Authorization": f"Bearer {DEAPI_TOKEN}",
        "Accept": "application/json",
    }

    for attempt in range(max_attempts):
        try:
            log.debug("DeAPI status tekshirilmoqda: attempt=%s", attempt + 1)
            resp = requests.get(status_url, headers=headers, timeout=30)
            resp.raise_for_status()
            sdata = resp.json()
            d = sdata.get("data") or {}
            status = d.get("status")
            log.info("DeAPI status: %s (request_id=%s)", status, request_id)

            # Muvaffaqiyatli bo'lsa - URL izlaymiz
            result_url = d.get("result_url") or d.get("result") or d.get("preview")
            if _is_http_url(result_url):
                log.info("Rasm URL topildi: %s", result_url)
                return result_url

            # Agar result boru, lekin URL emas bo'lsa - log yozib, davom etamiz
            if result_url and not _is_http_url(result_url):
                log.warning(
                    "DeAPI natija HTTP URL emas (ehtimol base64 yoki boshqacha): %s...",
                    str(result_url)[:80],
                )

            # Hali ishlayapti
            if status in ("pending", "processing", "queued", "running"):
                time.sleep(interval_sec)
                continue

            # Kutilmagan status
            log.warning("DeAPI status kutilmagan: %s, data=%s", status, sdata)
            return None

        except Exception as e:
            log.exception("DeAPI status tekshirishda xato: %s", e)
            return None

    log.error("DeAPI timeout: %s urinishdan keyin ham URL natija yo'q (request_id=%s)", max_attempts, request_id)
    return None


def generate_image_url_from_prompt(prompt: str) -> str:
    """
    Referat/Word tomoni shu funksiyani chaqiradi.
    - deapi orqali rasm yaratishga urinadi
    - agar hammasi joyida bo'lsa, AI rasm URL (http/https) qaytaradi
    - xatolik/timeout yoki URL bo'lmasa, placeholder rasm URL qaytaradi

    MUHIM: Doim URL olmaguncha bir necha marta urinadi (MAX_JOBS marta).
    """
    if not DEAPI_TOKEN:
        log.warning("DEAPI_TOKEN yo'q, placeholder URL qaytaryapman")
        return PLACEHOLDER_URL

    MAX_JOBS = 5  # nechta yangi txt2img job yaratib ko'ramiz

    for job_attempt in range(1, MAX_JOBS + 1):
        log.info("DeAPI txt2img urinish #%s, prompt=%s", job_attempt, prompt)

        # 1) job yaratamiz
        request_id = _deapi_txt2img_request(prompt)
        if not request_id:
            log.warning("txt2img request_id olinmadi (urinish #%s)", job_attempt)
            continue

        # 2) natijani poll qilamiz
        img_url = _deapi_poll_result(request_id)
        if _is_http_url(img_url):
            # Faqat URL bo'lsa qaytaramiz
            return img_url

        log.warning(
            "DeAPI urinish #%s da ham toza URL olinmadi (img_url=%s). Keyingi urinishga o'taman.",
            job_attempt,
            (img_url or "")[:80],
        )

    # Hamma urinishlar ishlamasa - xavfsiz fallback
    log.error(
        "DeAPI orqali %s marta urinilgandan keyin ham HTTP URL olinmadi. Placeholder qaytaryapman.",
        MAX_JOBS,
    )
    return PLACEHOLDER_URL


def inject_ai_images_into_content(raw: str) -> str:
    """
    Matndagi [RASM n: ...] markerlarni AI yordamida yaratilgan
    rasm <img> bloklariga almashtiradi.

    Misol marker:
      [RASM 1: Sun'iy intellekt asosida ishlovchi datchiklar tizimi]

    Natija (Word HTML ichida):
      <div class="image-container">
        <img src="https://..." ... />
        <p>Rasm 1. Sun'iy intellekt asosida ...</p>
      </div>
    """
    if not raw:
        return ""

    text = raw

    def _replace(match: re.Match) -> str:
        index = match.group(1)
        desc = match.group(2).strip()

        # DeAPI orqali rasm URL (faqat http/https bo'lsa qaytadi,
        # aks holda placeholder URL bo'ladi)
        img_url = generate_image_url_from_prompt(desc)
                # 2) URL'ni offline <img> ga aylantiramiz (data:image/...;base64,...)
        img_html = url_to_img_tag(
            img_url,
            inline=False,                # alohida blok sifatida
            max_width="14cm",            # A4 Word uchun qulay
        )

        html_block = f"""
        <div class="image-container" style="text-align:center; margin:16px 0;">
          {img_html}
          <p class="image-caption" style="font-size:12pt; margin-top:4px; text-align:center; text-indent:0;">
            Rasm {index}. {desc}
          </p>
        </div>
        """
        return html_block

    text = IMAGE_MARKER_RE.sub(_replace, text)
    return text


# Quyidagini faqat tezkor test uchun ishlatishingiz mumkin:
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    test_text = """
    Bu oddiy matn.

    [RASM 1: A realistic illustration of a university student presenting a project with slides in a classroom]

    Matn davom etadi.

    [RASM 2: Detailed infographic style diagram about artificial intelligence and data flow]
    """

    print("=== Kirish matni ===")
    print(test_text)

    out = inject_ai_images_into_content(test_text)

    print("\n=== Chiqish (HTML) ===")
    print(out)
