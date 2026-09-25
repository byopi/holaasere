import asyncio
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
import re
import threading
from bs4 import BeautifulSoup
import httpx
from telegram import Bot
import yt_dlp

# --- VARIABLES DE ENTORNO EN RENDER ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID"
)  # ID de tu canal privado de borrador/almacén
TELEGRAM_LOG_CHANNEL_ID = os.getenv(
    "TELEGRAM_LOG_CHANNEL_ID"
)  # ID de tu canal privado de logs

TARGET_ACCOUNTS = ["PolymarketFC", "ActuFoot_"]
CHECK_INTERVAL = 90  # Segundos entre revisiones

bot = Bot(token=TELEGRAM_BOT_TOKEN)


# --- SERVIDOR HTTP MÍNIMO (Para Render y UptimeRobot) ---
class HealthCheckHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot activo y monitoreando.")

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"[{datetime.now()}] Servidor HTTP activo en el puerto {port}")
    server.serve_forever()


# --- FUNCIÓN DE LOGS A TELEGRAM ---
async def send_log(message: str):
    print(f"[{datetime.now()}] {message}")
    if TELEGRAM_LOG_CHANNEL_ID:
        try:
            await bot.send_message(
                chat_id=TELEGRAM_LOG_CHANNEL_ID,
                text=f"⚠️ <b>[LOG / ERROR]</b>\n<code>{message}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            print(f"Error al enviar log a Telegram: {e}")


# --- OBTENER ÚLTIMO TWEET (SYNDICATION API) ---
async def get_latest_tweet_id(username: str):
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=10.0
        ) as client:
            response = await client.get(url, headers=headers)

            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                tweet_links = soup.find_all(
                    "a", href=re.compile(r"/status/\d+")
                )

                if tweet_links:
                    first_link = tweet_links[0]["href"]
                    tweet_id = first_link.split("/status/")[1].split("?")[0]
                    tweet_url = f"https://x.com/{username}/status/{tweet_id}"
                    return tweet_id, tweet_url
    except Exception as e:
        await send_log(f"Error al consultar Syndication API para @{username}: {e}")

    return None, None


# --- DESCARGAR Y ENVIAR MULTIMEDIA A TELEGRAM ---
async def process_and_send_tweet(username: str, tweet_url: str):
    # Opciones de configuración para yt-dlp
    ydl_opts = {
        "outtmpl": "downloaded_media.%(ext)s",
        "format": "bestvideo+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
    }

    file_path = None
    try:
        # Extraer información del tweet y descargar la media
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(tweet_url, download=True)
            text_caption = info.get("description") or info.get("title") or ""
            filename = ydl.prepare_filename(info)
            file_path = filename

        # Formatear el texto inicial para el borrador
        caption_formatted = (
            f"📌 <b>De: @{username}</b>\n\n"
            f"{text_caption}\n\n"
            f"🔗 <a href='{tweet_url}'>Ver en X</a>"
        )

        # Cortar caption si supera el límite de Telegram (1024 chars para media)
        if len(caption_formatted) > 1024:
            caption_formatted = caption_formatted[:1020] + "..."

        # Determinar si es Foto o Video y enviar a Telegram
        if file_path and os.path.exists(file_path):
            ext = os.path.splitext(file_path)[1].lower()

            with open(file_path, "rb") as media_file:
                if ext in [".jpg", ".jpeg", ".png", ".webp"]:
                    await bot.send_photo(
                        chat_id=TELEGRAM_CHAT_ID,
                        photo=media_file,
                        caption=caption_formatted,
                        parse_mode="HTML",
                    )
                elif ext in [".mp4", ".mkv", ".mov"]:
                    await bot.send_video(
                        chat_id=TELEGRAM_CHAT_ID,
                        video=media_file,
                        caption=caption_formatted,
                        parse_mode="HTML",
                    )
                else:
                    # Si es otro formato no reconocido, enviar como documento
                    await bot.send_document(
                        chat_id=TELEGRAM_CHAT_ID,
                        document=media_file,
                        caption=caption_formatted,
                        parse_mode="HTML",
                    )

            print(f"[{datetime.now()}] Media de @{username} enviada a Telegram.")

    except Exception as e:
        # Si falla la descarga multimedia (ej: tweet solo texto), enviar mensaje simple
        await send_log(
            f"No se pudo extraer media de {tweet_url}. Enviando enlace simple. Error: {e}"
        )
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"📌 <b>De: @{username}</b>\n\n🔗 {tweet_url}",
            parse_mode="HTML",
        )

    finally:
        # Borrar el archivo local del servidor de Render para no llenar el disco
        if file_path and os.path.exists(file_path):
            os.remove(file_path)


# --- BUCLE PRINCIPAL DE MONITOREO ---
async def monitor_loop():
    await send_log(
        f"Bot iniciado correctamente. Monitoreando: {', '.join(TARGET_ACCOUNTS)}"
    )
    last_tweet_ids = {account: None for account in TARGET_ACCOUNTS}

    while True:
        for account in TARGET_ACCOUNTS:
            tweet_id, tweet_url = await get_latest_tweet_id(account)

            if tweet_id:
                if last_tweet_ids[account] is None:
                    last_tweet_ids[account] = tweet_id
                    print(
                        f"[{datetime.now()}] @{account} registrado. Último ID: {tweet_id}"
                    )

                elif tweet_id != last_tweet_ids[account]:
                    last_tweet_ids[account] = tweet_id
                    await process_and_send_tweet(account, tweet_url)

            await asyncio.sleep(3)

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    # Arrancar Servidor HTTP para Render/UptimeRobot
    threading.Thread(target=run_health_server, daemon=True).start()

    # Arrancar Bucle de Monitoreo
    asyncio.run(monitor_loop())
