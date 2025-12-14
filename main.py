import os
import io
import asyncio
import shutil
import logging
import time
import re
from typing import List, Optional
from urllib.parse import urljoin, urlparse
import tempfile
import subprocess

import httpx
import requests
from PIL import Image
from bs4 import BeautifulSoup
from telegram import Bot, Update, InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import TelegramError, RetryAfter, BadRequest

# ========================
# LOGGING SETUP
# ========================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========================
# CONFIGURATION
# ========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1002900910545"))
DOWNLOAD_DIR = "downloads"
MAX_VIDEOS_PER_LIST = int(os.getenv("MAX_VIDEOS", 200))

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

async_client = httpx.AsyncClient(headers=HTTP_HEADERS, follow_redirects=True, timeout=30)

IGNORED_MEDIA_PATTERNS = [
    "/avatars/", "/styles/", "/smilies/", "/assets/",
    "cdninstagram.com", "/addonflare/", "/icons/"
]

# Global state
SENT_MEDIA_URLS = set()
cancellation_flags = {}
processing_tasks = {}
user_modes = {}
LINK_PROCESSING_INTERVAL = 10

# ========================
# UTILITY FUNCTIONS
# ========================
def escape_markdown_v2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2"""
    escape_chars = r'\_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def check_ffmpeg():
    """Check if ffmpeg is installed"""
    try:
        result = subprocess.run(['ffmpeg', '-version'], 
                              stdout=subprocess.PIPE, 
                              stderr=subprocess.PIPE,
                              timeout=5)
        if result.returncode == 0:
            logger.info("FFmpeg is installed and working")
            return True
    except Exception as e:
        logger.error(f"FFmpeg check failed: {e}")
    
    logger.warning("FFmpeg not found - video/gif processing will be limited")
    return False

def format_progress(total_counts, processed_counts, extra=""):
    """Format progress message"""
    parts = []
    if total_counts.get("images"): 
        parts.append(f"🖼️ {processed_counts['images']}/{total_counts['images']}")
    if total_counts.get("gifs"): 
        parts.append(f"🎬 {processed_counts['gifs']}/{total_counts['gifs']}")
    if total_counts.get("videos"): 
        parts.append(f"📹 {processed_counts['videos']}/{total_counts['videos']}")
    
    progress = " | ".join(parts)
    return f"{progress}\n\n{extra}" if extra else progress

async def update_status_safe(msg, text):
    """Safely update status message"""
    if not msg: return
    try:
        if msg.text != text:
            await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning(f"Could not edit: {e}")
    except Exception as e:
        logger.error(f"Error editing: {e}")

def find_urls_in_text(text: str) -> List[str]:
    """Extract URLs from text"""
    return re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', text)

def safe_filename(name: str) -> str:
    """Make filename safe"""
    return re.sub(r'[:*?"<>|/\\]', "_", name)

def is_direct_video_link(url: str) -> bool:
    """Check if URL is direct video link"""
    video_extensions = ['.mp4', '.mpd', '.avi', '.mov', '.wmv', '.mkv', '.webm']
    return any(url.lower().endswith(ext) for ext in video_extensions)

# ========================
# VIDEO LINK EXTRACTOR (MODE 1)
# ========================
def make_absolute_url(url: str, base_url: str) -> Optional[str]:
    """Convert relative URL to absolute"""
    if not url: return None
    if url.startswith(('http://', 'https://')): return url
    if url.startswith('//'): return f"https:{url}"
    if url.startswith('/'): 
        base = urlparse(base_url)
        return f"{base.scheme}://{base.netloc}{url}"
    return urljoin(base_url, url)

def extract_video_links_from_html(html_content: str, base_url: str) -> List[tuple]:
    """Extract video links from HTML"""
    soup = BeautifulSoup(html_content, 'html.parser')
    page_title = soup.title.string.strip() if soup.title else "Untitled"
    video_links = []
    
    # Search in scripts
    for script in soup.find_all('script'):
        if script.string:
            patterns = [
                r'htmlplayer\.setVideoUrl\("([^"]+)"\)',
                r'"video_url":"([^"]+)"',
                r'file:\s*"([^"]+)"',
                r'https?://[^\s<>"]*\.(?:mp4|mpd)[^\s<>"]*'
            ]
            for pattern in patterns:
                matches = re.findall(pattern, script.string, re.IGNORECASE)
                for match in matches:
                    url = match if isinstance(match, str) else match[0]
                    absolute_url = make_absolute_url(url, base_url)
                    if absolute_url and absolute_url not in [v[1] for v in video_links]:
                        video_links.append((page_title, absolute_url))
    
    # Search in tags
    for tag in soup.find_all(['video', 'source', 'a', 'iframe']):
        url = tag.get('src') or tag.get('href')
        if url and re.search(r'\.(mp4|mpd)(\?.*)?$', url, re.IGNORECASE):
            absolute_url = make_absolute_url(url, base_url)
            if absolute_url and absolute_url not in [v[1] for v in video_links]:
                video_links.append((page_title, absolute_url))
    
    return video_links[:5]  # Limit results

def extract_video_links(url: str) -> List[tuple]:
    """Extract video links from single page"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return extract_video_links_from_html(response.text, url)
    except Exception as e:
        logger.error(f"Error extracting from {url}: {e}")
        return []

def save_links_to_file(links: List[tuple], filename: str) -> str:
    """Save video links to file"""
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with open(filename, "w", encoding="utf-8") as f:
        for title, link in links:
            f.write(f"{title} - {link}\n")
    return filename

# ========================
# MEDIA SCRAPER (MODE 2)
# ========================
async def scrape_embedded_video(embed_url: str, referer: str) -> Optional[str]:
    """Extract video from embed page"""
    try:
        headers = {"User-Agent": HTTP_HEADERS["User-Agent"], "Referer": referer}
        response = await async_client.get(embed_url, headers=headers)
        response.raise_for_status()
        
        patterns = [
            r'file:\s*"([^"]+)"',
            r'<source\s+src="([^"]+)"',
            r'"fileURL":"([^"]+)"',
            r'(https?://[^\s"\'<>`]+?\.(?:mp4|m3u8|mkv|webm)[^\s"\'<>`]*)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, response.text)
            if match:
                video_url = match.group(1).strip().replace('\\/', '/')
                return urljoin(embed_url, video_url)
    except Exception as e:
        logger.error(f"Error scraping embed: {e}")
    return None

async def extract_media_from_page(url: str) -> tuple[dict, Optional[str], Optional[str]]:
    """Extract media URLs from page"""
    media_urls = {"images": [], "videos": [], "gifs": []}
    next_page_url = None
    
    try:
        response = await async_client.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        
        # Extract direct media links
        for tag in soup.find_all(['a', 'img', 'video', 'source']):
            for attr in ['href', 'src', 'data-src']:
                link = tag.get(attr)
                if not link: continue
                
                full_url = urljoin(url, link)
                if any(p in full_url for p in IGNORED_MEDIA_PATTERNS): continue
                
                path = urlparse(full_url).path.lower()
                if any(path.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                    if full_url not in media_urls["images"]: 
                        media_urls["images"].append(full_url)
                elif path.endswith(".gif"):
                    if full_url not in media_urls["gifs"]: 
                        media_urls["gifs"].append(full_url)
                elif any(path.endswith(ext) for ext in [".mp4", ".webm", ".mov"]):
                    if full_url not in media_urls["videos"]: 
                        media_urls["videos"].append(full_url)
        
        # Extract from iframes
        for iframe in soup.find_all('iframe'):
            iframe_src = iframe.get('src')
            if iframe_src:
                video_url = await scrape_embedded_video(urljoin(url, iframe_src), url)
                if video_url and video_url not in media_urls["videos"]:
                    media_urls["videos"].append(video_url)
        
        # Find next page
        next_tag = soup.find("a", class_="pageNav-jump--next") or \
                   soup.find("a", rel="next") or \
                   soup.find("a", string=re.compile(r"^\s*Next\s*$", re.IGNORECASE))
        if next_tag and next_tag.get("href"):
            next_page_url = urljoin(url, next_tag['href'])
    
    except Exception as e:
        logger.error(f"Error extracting media: {e}")
    
    # Limit results to prevent memory issues
    return {
        "images": media_urls["images"][:50],
        "videos": media_urls["videos"][:20],
        "gifs": media_urls["gifs"][:10]
    }, None, next_page_url

async def download_media_as_bytes(url: str, referer: Optional[str] = None) -> Optional[io.BytesIO]:
    """Download media file as bytes"""
    try:
        headers = {'Referer': referer} if referer else {}
        response = await async_client.get(url, headers=headers)
        response.raise_for_status()
        return io.BytesIO(response.content)
    except Exception as e:
        logger.warning(f"Download failed for {url}: {e}")
        return None

async def _send_with_retry(send_method, **kwargs):
    """Send with retry logic for flood control"""
    for attempt in range(3):
        try:
            # Reset file pointers
            for key in ['video', 'animation', 'thumbnail']:
                if key in kwargs and isinstance(kwargs.get(key), io.BytesIO):
                    kwargs[key].seek(0)
            if 'media' in kwargs:
                for item in kwargs['media']:
                    if isinstance(item.media, io.BytesIO): 
                        item.media.seek(0)
            
            await send_method(**kwargs)
            await asyncio.sleep(2)  # Rate limit protection
            return
        except RetryAfter as e:
            logger.warning(f"Flood control. Waiting {e.retry_after}s...")
            await asyncio.sleep(e.retry_after)
        except TelegramError as e:
            logger.error(f"Telegram error: {e}")
            if attempt == 2:
                return
            await asyncio.sleep(5)

async def send_images(bot: Bot, urls: list, referer: str, chat_id: Optional[int] = None):
    """Send images in groups of 10"""
    if not urls: return
    
    media_group = []
    for url in urls:
        if cancellation_flags.get(chat_id):
            raise asyncio.CancelledError
        
        image_bytes = await download_media_as_bytes(url, referer)
        if image_bytes:
            SENT_MEDIA_URLS.add(url)
            
            # Convert WebP to JPEG
            if url.lower().endswith(".webp"):
                try:
                    with Image.open(image_bytes) as img:
                        img = img.convert("RGB")
                        output = io.BytesIO()
                        img.save(output, format="JPEG")
                        output.seek(0)
                        image_bytes = output
                except Exception as e:
                    logger.error(f"WebP conversion failed: {e}")
                    continue
            
            media_group.append(InputMediaPhoto(media=image_bytes))
            
            if len(media_group) == 10:
                await _send_with_retry(bot.send_media_group, chat_id=CHANNEL_ID, media=media_group)
                media_group = []
    
    if media_group:
        await _send_with_retry(bot.send_media_group, chat_id=CHANNEL_ID, media=media_group)

async def send_videos(bot: Bot, urls: list, referer: str, chat_id: Optional[int] = None):
    """Send videos one by one"""
    if not urls: return
    
    for url in urls:
        if cancellation_flags.get(chat_id):
            raise asyncio.CancelledError
        
        video_bytes = await download_media_as_bytes(url, referer)
        if video_bytes:
            SENT_MEDIA_URLS.add(url)
            filename = os.path.basename(urlparse(url).path) or f"video_{int(time.time())}.mp4"
            
            try:
                await _send_with_retry(
                    bot.send_video,
                    chat_id=CHANNEL_ID,
                    video=video_bytes,
                    filename=filename,
                    supports_streaming=True,
                    write_timeout=60
                )
            except Exception as e:
                logger.error(f"Error sending video: {e}")

# ========================
# COMMAND HANDLERS
# ========================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    keyboard = [
        [InlineKeyboardButton("🎬 Video Link Extractor", callback_data="mode_video_links")],
        [InlineKeyboardButton("🖼️ Media Scraper", callback_data="mode_media_scraper")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
    ]
    
    welcome_text = (
        "🤖 *Professional Media Extractor Bot*\n\n"
        "Choose your extraction mode:\n\n"
        "🎬 *Video Link Extractor*\n"
        "Extract video download links from pages\n\n"
        "🖼️ *Media Scraper*\n"
        "Download and send images/videos to channel\n\n"
        "Select a mode to get started\\!"
    )
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks"""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    
    if query.data == "mode_video_links":
        user_modes[chat_id] = "video_links"
        text = (
            "🎬 *Video Link Extractor Mode*\n\n"
            "Send me video page URLs or a \\.txt file\\.\n"
            "I'll extract video links for you\\!"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2)
    
    elif query.data == "mode_media_scraper":
        user_modes[chat_id] = "media_scraper"
        text = (
            "🖼️ *Media Scraper Mode*\n\n"
            "Send me URLs or a \\.txt file\\.\n"
            "I'll scrape and send media to the channel\\!"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2)
    
    elif query.data == "help":
        help_text = (
            "📚 *Help Guide*\n\n"
            "*Commands:*\n"
            "/start \\- Select mode\n"
            "/cancel \\- Stop processing\n"
            "/id \\- Get chat ID\n\n"
            "*Modes:*\n"
            "• Video Link Extractor: Get download links\n"
            "• Media Scraper: Auto\\-download to channel"
        )
        keyboard = [[InlineKeyboardButton("« Back", callback_data="back")]]
        await query.edit_message_text(
            help_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2
        )
    
    elif query.data == "back":
        await start_command_from_callback(query)

async def start_command_from_callback(query):
    """Recreate start menu from callback"""
    keyboard = [
        [InlineKeyboardButton("🎬 Video Link Extractor", callback_data="mode_video_links")],
        [InlineKeyboardButton("🖼️ Media Scraper", callback_data="mode_media_scraper")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
    ]
    text = "🤖 *Professional Media Extractor Bot*\n\nChoose your mode:"
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel command"""
    chat_id = update.effective_chat.id
    if processing_tasks.get(chat_id):
        cancellation_flags[chat_id] = True
        await update.message.reply_text(
            escape_markdown_v2("🚫 Cancellation initiated..."),
            parse_mode=ParseMode.MARKDOWN_V2
        )
    else:
        await update.message.reply_text(
            escape_markdown_v2("🤷 No active process."),
            parse_mode=ParseMode.MARKDOWN_V2
        )

async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /id command"""
    await update.message.reply_text(
        f"Chat ID: `{update.effective_chat.id}`",
        parse_mode=ParseMode.MARKDOWN_V2
    )

# ========================
# MESSAGE HANDLER
# ========================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main message handler"""
    chat_id = update.effective_chat.id
    
    if processing_tasks.get(chat_id):
        await update.message.reply_text(
            escape_markdown_v2("⚠️ A process is running. Use /cancel first."),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return
    
    mode = user_modes.get(chat_id)
    if not mode:
        await update.message.reply_text(
            escape_markdown_v2("Please select a mode first using /start"),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return
    
    # Handle text messages (URLs)
    if update.message.text:
        urls = find_urls_in_text(update.message.text)
        if not urls:
            await update.message.reply_text(
                escape_markdown_v2("No valid URLs found."),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return
        
        if mode == "video_links":
            task = context.application.create_task(
                process_video_links(update, context, urls)
            )
        else:
            task = context.application.create_task(
                process_media_scraper(update, context, urls)
            )
        
        processing_tasks[chat_id] = task
    
    # Handle .txt files
    elif update.message.document and update.message.document.mime_type == "text/plain":
        await update.message.reply_text(
            escape_markdown_v2("📥 Processing .txt file..."),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        
        file = await context.bot.get_file(update.message.document.file_id)
        file_path = os.path.join(DOWNLOAD_DIR, f"{update.message.document.file_id}.txt")
        await file.download_to_drive(file_path)
        
        # Read URLs from file
        with open(file_path, 'r', encoding='utf-8') as f:
            urls = [line.strip() for line in f if line.strip().startswith('http')]
        
        os.remove(file_path)
        
        if not urls:
            await update.message.reply_text(
                escape_markdown_v2("No valid URLs in file."),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return
        
        if mode == "video_links":
            task = context.application.create_task(
                process_video_links(update, context, urls)
            )
        else:
            task = context.application.create_task(
                process_media_scraper(update, context, urls)
            )
        
        processing_tasks[chat_id] = task

# ========================
# PROCESSING FUNCTIONS
# ========================
async def process_video_links(update: Update, context: ContextTypes.DEFAULT_TYPE, urls: list):
    """Process URLs in video link extraction mode"""
    chat_id = update.effective_chat.id
    status_msg = await context.bot.send_message(
        chat_id,
        escape_markdown_v2(f"🔍 Extracting video links from {len(urls)} URL(s)..."),
        parse_mode=ParseMode.MARKDOWN_V2
    )
    
    all_links = []
    
    try:
        for url in urls:
            if cancellation_flags.get(chat_id):
                break
            
            if not url.startswith('http'):
                url = 'https://' + url
            
            links = extract_video_links(url)
            all_links.extend(links)
            await asyncio.sleep(1)
        
        if all_links:
            # Remove duplicates
            unique_links = list(dict(all_links).items())
            
            # Save to file
            temp_file = os.path.join(DOWNLOAD_DIR, f"links_{chat_id}_{int(time.time())}.txt")
            save_links_to_file(unique_links, temp_file)
            
            # Send file
            with open(temp_file, 'rb') as f:
                await context.bot.send_document(
                    chat_id,
                    document=f,
                    filename="video_links.txt",
                    caption=f"✅ Found {len(unique_links)} video links"
                )
            
            os.remove(temp_file)
        else:
            await update_status_safe(
                status_msg,
                escape_markdown_v2("❌ No video links found.")
            )
    
    except asyncio.CancelledError:
        await update_status_safe(status_msg, escape_markdown_v2("🚫 Canceled."))
    except Exception as e:
        logger.error(f"Error in video links mode: {e}")
        await update_status_safe(
            status_msg,
            escape_markdown_v2(f"❌ Error: {str(e)[:100]}")
        )
    finally:
        processing_tasks.pop(chat_id, None)
        cancellation_flags.pop(chat_id, None)
        if status_msg:
            await status_msg.delete()

async def process_media_scraper(update: Update, context: ContextTypes.DEFAULT_TYPE, urls: list):
    """Process URLs in media scraper mode"""
    chat_id = update.effective_chat.id
    status_msg = await context.bot.send_message(
        chat_id,
        escape_markdown_v2(f"🔍 Scraping media from {len(urls)} URL(s)..."),
        parse_mode=ParseMode.MARKDOWN_V2
    )
    
    try:
        for i, url in enumerate(urls):
            if cancellation_flags.get(chat_id):
                break
            
            if not url.startswith('http'):
                url = 'https://' + url
            
            await update_status_safe(
                status_msg,
                escape_markdown_v2(f"Processing {i+1}/{len(urls)}: {url[:50]}...")
            )
            
            media, _, _ = await extract_media_from_page(url)
            
            # Filter new media
            new_media = {
                key: [u for u in urls_list if u not in SENT_MEDIA_URLS]
                for key, urls_list in media.items()
            }
            
            # Send media
            if new_media.get("images"):
                await send_images(context.bot, new_media["images"], url, chat_id)
            if new_media.get("videos"):
                await send_videos(context.bot, new_media["videos"], url, chat_id)
            
            if i < len(urls) - 1:
                await asyncio.sleep(LINK_PROCESSING_INTERVAL)
        
        await update_status_safe(
            status_msg,
            escape_markdown_v2("✅ Scraping complete!")
        )
        await asyncio.sleep(3)
        await status_msg.delete()
    
    except asyncio.CancelledError:
        await update_status_safe(status_msg, escape_markdown_v2("🚫 Canceled."))
    except Exception as e:
        logger.error(f"Error in media scraper: {e}")
        await update_status_safe(
            status_msg,
            escape_markdown_v2(f"❌ Error: {str(e)[:100]}")
        )
    finally:
        processing_tasks.pop(chat_id, None)
        cancellation_flags.pop(chat_id, None)

# ========================
# MAIN
# ========================
def main():
    """Main entry point"""
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN not set!")
        return
    
    # Check ffmpeg (non-blocking)
    has_ffmpeg = check_ffmpeg()
    if not has_ffmpeg:
        logger.warning("Bot will work with limited functionality")
    
    # Build application
    app = ApplicationBuilder() \
        .token(BOT_TOKEN) \
        .connect_timeout(30) \
        .read_timeout(30) \
        .write_timeout(60) \
        .build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND | filters.ATTACHMENT,
        message_handler
    ))
    
    logger.info("🤖 Bot started successfully!")
    logger.info(f"FFmpeg: {'✅ Available' if has_ffmpeg else '⚠️ Not available'}")
    
    # Run bot
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
