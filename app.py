import os
import re
import asyncio
import json
from datetime import datetime
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, MessageMediaWebPage
from telethon.errors import ChatWriteForbiddenError, ChannelPrivateError, UserBannedInChannelError
from dotenv import load_dotenv

# Lock برای جلوگیری از پردازش همزمان پیام‌ها
message_processing_lock = asyncio.Lock()

# Set برای ذخیره پیام‌هایی که در حال پردازش هستند (channel_key: message_id)
processing_messages = set()

# بارگذاری متغیرهای محیطی
load_dotenv()

# تنظیمات از متغیرهای محیطی
API_ID = int(os.getenv('API_ID'))
API_HASH = os.getenv('API_HASH')
SESSION_NAME = os.getenv('SESSION_NAME', 'bot_session')
SOURCE_CHANNELS = os.getenv('SOURCE_CHANNELS', '').split(',')
TARGET_CHANNEL = os.getenv('TARGET_CHANNEL')
REPLACE_USERNAME = [u.strip() for u in os.getenv('REPLACE_USERNAME', '').split(',') if u.strip()]
NEW_USERNAME = os.getenv('NEW_USERNAME', '')

# فایل برای ذخیره آخرین پست‌های دیده شده
LAST_MESSAGES_FILE = 'last_messages.json'

def load_last_messages():
    """بارگذاری آخرین پیام‌های دیده شده از فایل"""
    if os.path.exists(LAST_MESSAGES_FILE):
        try:
            with open(LAST_MESSAGES_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError, OSError) as e:
            print(f"⚠️ خطا در خواندن فایل {LAST_MESSAGES_FILE}: {e}")
            return {}
    return {}

def save_last_messages(messages):
    """ذخیره آخرین پیام‌های دیده شده در فایل"""
    with open(LAST_MESSAGES_FILE, 'w', encoding='utf-8') as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)

def get_channel_key(entity, fallback_id=None):
    """تابع مشترک برای ساخت channel_key از entity"""
    entity_username = getattr(entity, 'username', None)
    if entity_username:
        # همیشه با @ ذخیره می‌کنیم برای consistency
        return f"@{entity_username.lstrip('@')}"
    elif hasattr(entity, 'id'):
        return str(entity.id)
    elif fallback_id:
        return str(fallback_id)
    else:
        return None

def get_all_channel_keys(entity, channel_username=None, chat_id=None):
    """برگرداندن همه کلیدهای ممکن برای یک کانال"""
    keys = set()  # زززززاستفاده از set برای جلوگیری از تکراری
    entity_username = getattr(entity, 'username', None)
    
    # اضافه کردن username با @
    if entity_username:
        clean_username = entity_username.lstrip('@')
        keys.add(f"@{clean_username}")
        keys.add(clean_username)  # بدون @ برای backward compatibility
    
    # اضافه کردن channel_username از SOURCE_CHANNELS (اگر متفاوت است)
    if channel_username:
        clean_channel_username = channel_username.lstrip('@')
        keys.add(clean_channel_username)
        keys.add(f"@{clean_channel_username}")
    
    # اضافه کردن entity.id
    if hasattr(entity, 'id'):
        keys.add(str(entity.id))
    
    # اضافه کردن chat_id (اگر متفاوت است)
    if chat_id:
        keys.add(str(chat_id))
    
    return list(keys)

def replace_username_in_text(text, old_username, new_username):
    """جایگزینی @username در متن"""
    if not text or not old_username or not new_username:
        return text
    
    # حذف @ از ابتدای username اگر وجود دارد
    old_username = old_username.lstrip('@')
    new_username = new_username.lstrip('@')
    
    # جایگزینی @username در متن
    pattern = rf'@{re.escape(old_username)}\b'
    text = re.sub(pattern, f'@{new_username}', text, flags=re.IGNORECASE)
    
    return text

def truncate_caption(text, max_length=1024):
    """محدود کردن طول caption به حداکثر 1024 کاراکتر (محدودیت تلگرام)"""
    if not text:
        return None
    if len(text) <= max_length:
        return text
    # برش متن و اضافه کردن "..."
    return text[:max_length - 3] + "..."

def remove_channel_signature(text):
    """حذف متن 'کانال رسمی روزنامه دنیای اقتصاد' با آیکون‌های قبل و بعدش"""
    if not text:
        return text
    
    # الگو برای پیدا کردن آیکون‌ها (emoji) قبل و بعد از متن
    # این الگو آیکون‌ها، فاصله‌ها و متن را پیدا می‌کند
    patterns = [
        # الگو برای آیکون‌ها + متن + آیکون‌ها
        r'[^\w\s]*\s*کانال\s+رسمی\s+روزنامه\s+دنیای\s+اقتصاد\s*[^\w\s]*',
        # الگو برای متن با تغییرات کوچک در فاصله‌گذاری
        r'[^\w\s]*\s*کانال\s*رسمی\s*روزنامه\s*دنیای\s*اقتصاد\s*[^\w\s]*',
        # الگو برای متن در ابتدا یا انتهای پیام
        r'^[^\w\s]*\s*کانال\s+رسمی\s+روزنامه\s+دنیای\s+اقتصاد\s*[^\w\s]*\s*',
        r'\s*[^\w\s]*\s*کانال\s+رسمی\s+روزنامه\s+دنیای\s+اقتصاد\s*[^\w\s]*$',
    ]
    
    for pattern in patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.MULTILINE)
    
    # حذف خطوط خالی اضافی
    text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
    text = text.strip()
    
    return text

def add_username_to_long_text(text, username, min_length=200):
    """اضافه کردن username به انتهای متن‌های طولانی"""
    if not text or not username:
        return text
    
    # حذف @ از ابتدای username اگر وجود دارد
    username = username.lstrip('@')
    
    # بررسی اینکه آیا متن طولانی است
    if len(text) >= min_length:
        # بررسی اینکه آیا username در انتهای متن وجود دارد یا نه
        text_stripped = text.rstrip()
        username_with_at = f"@{username}"
        
        # اگر username در انتهای متن نیست، آن را اضافه می‌کنیم
        if not text_stripped.endswith(username_with_at):
            # اضافه کردن username با یک خط جدید یا فاصله
            if text_stripped.endswith('\n'):
                text = text_stripped + username_with_at
            else:
                text = text_stripped + '\n\n' + username_with_at
    
    return text

def clear_session_files(session_name):
    """پاک کردن فایل‌های session"""
    session_file = f"{session_name}.session"
    session_journal = f"{session_name}.session-journal"
    cleared = False
    
    for file_path in [session_file, session_journal]:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                print(f"🗑️ فایل {file_path} پاک شد")
                cleared = True
            except Exception as remove_error:
                print(f"⚠️ خطا در پاک کردن {file_path}: {remove_error}")
    
    return cleared

async def check_channel_access(client, target_channel):
    """بررسی دسترسی نوشتن در کانال هدف"""
    try:
        entity = await client.get_entity(target_channel)
        channel_title = getattr(entity, 'title', target_channel)
        
        # بررسی دسترسی نوشتن با ارسال یک پیام تست خالی (که فوراً حذف می‌شود)
        try:
            test_message = await client.send_message(target_channel, "🔍")
            await client.delete_messages(target_channel, test_message)
            print(f"✅ دسترسی نوشتن در کانال '{channel_title}' تأیید شد")
            return True
        except (ChatWriteForbiddenError, UserBannedInChannelError) as e:
            print(f"❌ خطا: دسترسی نوشتن در کانال '{channel_title}' وجود ندارد")
            print(f"   💡 راهنمایی: مطمئن شوید که:")
            print(f"      - حساب شما به عنوان ادمین در کانال هدف تنظیم شده است")
            print(f"      - دسترسی 'Post Messages' برای حساب شما فعال است")
            return False
        except ChannelPrivateError:
            print(f"❌ خطا: کانال '{channel_title}' خصوصی است و دسترسی ندارید")
            print(f"   💡 راهنمایی: مطمئن شوید که حساب شما عضو کانال است")
            return False
        except Exception as e:
            error_msg = str(e).lower()
            if "can't write" in error_msg or "write in this chat" in error_msg:
                print(f"❌ خطا: دسترسی نوشتن در کانال '{channel_title}' وجود ندارد")
                print(f"   💡 راهنمایی: مطمئن شوید که:")
                print(f"      - حساب شما به عنوان ادمین در کانال هدف تنظیم شده است")
                print(f"      - دسترسی 'Post Messages' برای حساب شما فعال است")
            else:
                print(f"⚠️ هشدار: مشکل در بررسی دسترسی به کانال '{channel_title}': {str(e)}")
            return False
    except Exception as e:
        print(f"⚠️ هشدار: مشکل در دسترسی به کانال هدف: {str(e)}")
        return False

async def forward_message(client, message, target_channel, old_username, new_username):
    """ارسال پیام به کانال هدف با جایگزینی username"""
    try:
        # دریافت متن پیام
        text = message.text or message.raw_text or ''
        
        # حذف متن 'کانال رسمی روزنامه دنیای اقتصاد' با آیکون‌هایش
        text = remove_channel_signature(text)
        
        # جایگزینی username در متن
        if new_username:
            # اگر REPLACE_USERNAME مشخص شده باشد، همه آن‌ها را جایگزین می‌کنیم
            if REPLACE_USERNAME:
                for old_usr in REPLACE_USERNAME:
                    text = replace_username_in_text(text, old_usr, new_username)
            # در غیر این صورت، username کانال منبع را جایگزین می‌کنیم
            elif old_username:
                text = replace_username_in_text(text, old_username, new_username)
            
            # اضافه کردن username به انتهای متن‌های طولانی
            text = add_username_to_long_text(text, new_username, min_length=200)
        
        # بررسی اینکه آیا پیام دارای رسانه است
        has_media = message.media is not None
        
        # بررسی نوع رسانه - MessageMediaWebPage نمی‌تواند به عنوان فایل ارسال شود
        is_webpage = isinstance(message.media, MessageMediaWebPage)
        
        # ارسال پیام
        if has_media and not is_webpage:
            # اگر پیام دارای رسانه است (به جز WebPage)
            # بررسی طول متن
            if text and len(text) > 1024:
                # اگر متن بیشتر از 1024 کاراکتر است، ابتدا 1024 کاراکتر اول را به عنوان caption ارسال می‌کنیم
                caption = text[:1024]
                await client.send_file(
                    target_channel,
                    message.media,
                    caption=caption,
                    parse_mode='html'
                )
                # سپس باقی متن را به عنوان پیام جداگانه ارسال می‌کنیم
                remaining_text = text[1024:].strip()
                if remaining_text:
                    await client.send_message(
                        target_channel,
                        remaining_text,
                        parse_mode='html'
                    )
            else:
                # اگر متن کوتاه است یا خالی است، همانطور که هست ارسال می‌کنیم
                caption = text if text else None
                await client.send_file(
                    target_channel,
                    message.media,
                    caption=caption,
                    parse_mode='html'
                )
        else:
            # اگر فقط متن است یا رسانه از نوع WebPage است
            if text:
                await client.send_message(
                    target_channel,
                    text,
                    parse_mode='html'
                )
        
        print(f"✅ پیام با ID {message.id} با موفقیت ارسال شد")
        return True
    except ChatWriteForbiddenError:
        print(f"❌ خطا در ارسال پیام {message.id}: دسترسی نوشتن در کانال هدف وجود ندارد")
        print(f"   💡 راهنمایی: مطمئن شوید که:")
        print(f"      - حساب شما به عنوان ادمین در کانال هدف تنظیم شده است")
        print(f"      - دسترسی 'Post Messages' برای حساب شما فعال است")
        return False
    except UserBannedInChannelError:
        print(f"❌ خطا در ارسال پیام {message.id}: حساب شما در کانال هدف مسدود شده است")
        print(f"   💡 راهنمایی: با ادمین کانال تماس بگیرید تا مسدودیت را برطرف کند")
        return False
    except ChannelPrivateError:
        print(f"❌ خطا در ارسال پیام {message.id}: کانال هدف خصوصی است و دسترسی ندارید")
        print(f"   💡 راهنمایی: مطمئن شوید که حساب شما عضو کانال است")
        return False
    except Exception as e:
        error_msg = str(e)
        error_lower = error_msg.lower()
        # تشخیص خطای خاص دسترسی با بررسی متن خطا
        if any(phrase in error_lower for phrase in [
            "can't write", "write in this chat", "chat_write_forbidden",
            "you can't write in this chat", "not enough rights"
        ]):
            print(f"❌ خطا در ارسال پیام {message.id}: دسترسی نوشتن در کانال هدف وجود ندارد")
            print(f"   💡 راهنمایی: مطمئن شوید که:")
            print(f"      - حساب شما به عنوان ادمین در کانال هدف تنظیم شده است")
            print(f"      - اگر کانال عمومی است، حساب شما عضو آن است")
            print(f"      - دسترسی 'Post Messages' برای حساب شما فعال است")
        elif "flood" in error_lower or "too many requests" in error_lower:
            print(f"⚠️ خطا در ارسال پیام {message.id}: محدودیت نرخ ارسال (Flood Wait)")
            print(f"   💡 راهنمایی: کمی صبر کنید و دوباره تلاش کنید")
        elif "message too long" in error_lower or "message is too long" in error_lower:
            print(f"❌ خطا در ارسال پیام {message.id}: پیام خیلی طولانی است")
            print(f"   💡 راهنمایی: طول پیام باید کمتر از محدودیت تلگرام باشد")
        else:
            print(f"❌ خطا در ارسال پیام {message.id}: {error_msg}")
        return False

async def check_new_messages(client):
    """بررسی پیام‌های جدید از کانال‌های منبع"""
    last_messages = load_last_messages()
    source_channels = [ch.strip() for ch in SOURCE_CHANNELS if ch.strip()]
    
    for channel_username in source_channels:
        try:
            # دریافت اطلاعات کانال
            entity = await client.get_entity(channel_username)
            
            # استفاده از تابع مشترک برای ساخت channel_key
            channel_key = get_channel_key(entity, channel_username)
            if not channel_key:
                print(f"⚠️ نتوانست channel_key را برای {channel_username} بسازد")
                continue
            
            # دریافت آخرین پیام‌های کانال
            messages = await client.get_messages(entity, limit=10)
            
            # دریافت همه کلیدهای ممکن برای این کانال
            all_keys = get_all_channel_keys(entity, channel_username)
            
            # بررسی پیام‌های جدید - بررسی با همه کلیدهای ممکن
            last_seen_id = max([last_messages.get(key, 0) for key in all_keys], default=0)
            
            new_messages = [msg for msg in messages if msg.id > last_seen_id and not msg.out]
            
            if new_messages:
                # مرتب‌سازی بر اساس ID (قدیمی‌ترین اول)
                new_messages.sort(key=lambda x: x.id)
                
                # لیست پیام‌هایی که واقعاً ارسال شدند
                sent_messages = []
                
                # ارسال پیام‌های جدید
                for message in new_messages:
                    # استفاده از lock برای جلوگیری از پردازش همزمان
                    async with message_processing_lock:
                        # بررسی مجدد برای جلوگیری از پردازش تکراری (در صورت پردازش همزمان توسط event handler)
                        current_last_messages = load_last_messages()
                        # بررسی با همه کلیدهای ممکن برای اطمینان کامل
                        current_last_seen_id = max([current_last_messages.get(key, 0) for key in all_keys], default=0)
                        
                        # اگر پیام قبلاً پردازش شده، از پردازش مجدد جلوگیری می‌کنیم
                        if message.id <= current_last_seen_id:
                            print(f"⏭️ پیام {message.id} از {channel_key} قبلاً پردازش شده است (آخرین: {current_last_seen_id})")
                            continue
                        
                        # بررسی اینکه آیا این پیام در حال پردازش است
                        message_key = f"{channel_key}:{message.id}"
                        if message_key in processing_messages:
                            print(f"⏭️ پیام {message.id} از {channel_key} در حال پردازش است")
                            continue
                        
                        # اضافه کردن به لیست پیام‌های در حال پردازش
                        processing_messages.add(message_key)
                        
                        # به‌روزرسانی فوری قبل از ارسال برای جلوگیری از race condition
                        # ذخیره با همه کلیدهای ممکن برای اطمینان
                        for key in all_keys:
                            current_last_messages[key] = message.id
                        save_last_messages(current_last_messages)
                    
                    # استخراج username کانال از entity
                    old_username = getattr(entity, 'username', None)
                    message_key = f"{channel_key}:{message.id}"
                    try:
                        success = await forward_message(
                            client,
                            message,
                            TARGET_CHANNEL,
                            old_username,
                            NEW_USERNAME
                        )
                        
                        # اگر پیام با موفقیت ارسال شد، آن را به لیست اضافه می‌کنیم
                        if success:
                            sent_messages.append(message)
                    finally:
                        # حذف از لیست پیام‌های در حال پردازش
                        processing_messages.discard(message_key)
                    
                    await asyncio.sleep(2)  # تاخیر بین ارسال پیام‌ها
                
                # لاگ آخرین پیام‌های ارسال شده
                if sent_messages:
                    max_sent_id = max(msg.id for msg in sent_messages)
                    print(f"📝 {len(sent_messages)} پیام جدید از {channel_key} ارسال شد. آخرین ID: {max_sent_id}")
            
        except Exception as e:
            print(f"❌ خطا در بررسی کانال {channel_username}: {str(e)}")

async def periodic_check(client, interval_seconds=30):
    """بررسی دوره‌ای پیام‌های جدید هر X ثانیه"""
    print(f"⏰ بررسی دوره‌ای هر {interval_seconds} ثانیه شروع شد. اولین بررسی بعد از {interval_seconds} ثانیه...")
    print(f"📅 زمان فعلی: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    iteration = 0
    while True:
        try:
            iteration += 1
            await asyncio.sleep(interval_seconds)
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"🔄 بررسی دوره‌ای #{iteration} - پیام‌های جدید ({current_time})...")
            await check_new_messages(client)
            print(f"✅ بررسی دوره‌ای #{iteration} انجام شد. بررسی بعدی در {interval_seconds} ثانیه...")
        except asyncio.CancelledError:
            print("⏹️ بررسی دوره‌ای متوقف شد")
            break
        except KeyboardInterrupt:
            # اگر KeyboardInterrupt در task رخ دهد، آن را دوباره raise می‌کنیم
            print("⏹️ بررسی دوره‌ای به دلیل توقف برنامه متوقف شد")
            raise
        except Exception as e:
            print(f"❌ خطا در بررسی دوره‌ای: {str(e)}")
            import traceback
            traceback.print_exc()
            try:
                await asyncio.sleep(60)  # در صورت خطا، یک دقیقه صبر می‌کند
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise

async def main():
    """تابع اصلی"""
    print("🚀 در حال راه‌اندازی ربات...")
    
    # ایجاد کلاینت تلگرام
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    
    try:
        # تلاش برای اتصال با مدیریت خطا
        try:
            await client.start()
            print("✅ اتصال به تلگرام برقرار شد")
        except (ValueError, ConnectionError, OSError) as e:
            error_msg = str(e).lower()
            # بررسی خطاهای مربوط به احراز هویت
            if any(keyword in error_msg for keyword in [
                "phone_code_hash", "server closed", "invalid code",
                "connection", "0 bytes read"
            ]):
                print("⚠️ خطا در احراز هویت: session نامعتبر یا کد تأیید نامعتبر است")
                print("💡 در حال پاک کردن session و شروع مجدد احراز هویت...")
                
                # قطع اتصال اگر متصل است
                try:
                    await client.disconnect()
                except:
                    pass
                
                # پاک کردن session فایل‌ها
                if clear_session_files(SESSION_NAME):
                    print("✅ فایل‌های session پاک شدند")
                
                # ایجاد کلاینت جدید و تلاش مجدد
                client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
                print("🔄 در حال تلاش مجدد برای احراز هویت...")
                print("📱 لطفاً شماره تلفن و کد تأیید جدید را وارد کنید")
                await client.start()
                print("✅ اتصال به تلگرام برقرار شد")
            else:
                raise
        except Exception as auth_error:
            error_msg = str(auth_error).lower()
            # بررسی مجدد برای خطاهای احراز هویت در catch عمومی
            if any(keyword in error_msg for keyword in [
                "phone_code_hash", "server closed", "invalid code",
                "connection", "0 bytes read"
            ]):
                print("⚠️ خطا در احراز هویت: session نامعتبر یا کد تأیید نامعتبر است")
                print("💡 در حال پاک کردن session و شروع مجدد احراز هویت...")
                
                # قطع اتصال اگر متصل است
                try:
                    await client.disconnect()
                except:
                    pass
                
                # پاک کردن session فایل‌ها
                if clear_session_files(SESSION_NAME):
                    print("✅ فایل‌های session پاک شدند")
                
                # ایجاد کلاینت جدید و تلاش مجدد
                client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
                print("🔄 در حال تلاش مجدد برای احراز هویت...")
                print("📱 لطفاً شماره تلفن و کد تأیید جدید را وارد کنید")
                await client.start()
                print("✅ اتصال به تلگرام برقرار شد")
            else:
                print(f"❌ خطا در احراز هویت: {str(auth_error)}")
                print("💡 راهنمایی:")
                print("   - مطمئن شوید که API_ID و API_HASH صحیح هستند")
                print("   - اگر کد تأیید نامعتبر بود، دوباره تلاش کنید")
                print("   - در صورت نیاز، فایل session را پاک کنید و دوباره احراز هویت کنید")
                raise
        
        # بررسی دسترسی به کانال هدف
        print("🔐 در حال بررسی دسترسی به کانال هدف...")
        await check_channel_access(client, TARGET_CHANNEL)
        
        # بررسی اولیه پیام‌های جدید
        print("🔍 در حال بررسی پیام‌های جدید...")
        await check_new_messages(client)
        
        # ثبت handler برای پیام‌های جدید
        @client.on(events.NewMessage(chats=SOURCE_CHANNELS))
        async def handler(event):
            message = event.message
            
            # جلوگیری از پردازش پیام‌های خود ربات
            if message.out:
                return
            
            # استخراج username کانال
            entity = await event.get_chat()
            # استفاده از تابع مشترک برای ساخت channel_key
            channel_key = get_channel_key(entity, event.chat_id)
            if not channel_key:
                print(f"⚠️ نتوانست channel_key را برای chat_id {event.chat_id} بسازد")
                return
            
            # دریافت همه کلیدهای ممکن برای این کانال
            all_keys = get_all_channel_keys(entity, chat_id=event.chat_id)
            
            # استفاده از lock برای جلوگیری از پردازش همزمان
            async with message_processing_lock:
                # بررسی اینکه آیا این پیام قبلاً پردازش شده است
                # بررسی با همه کلیدهای ممکن برای اطمینان کامل
                last_messages = load_last_messages()
                last_seen_id = max([last_messages.get(key, 0) for key in all_keys], default=0)
                
                # بررسی اینکه آیا این پیام در حال پردازش است
                message_key = f"{channel_key}:{message.id}"
                if message_key in processing_messages:
                    print(f"⏭️ پیام {message.id} از {channel_key} در حال پردازش است")
                    return
                
                # اگر پیام قبلاً پردازش شده، از پردازش مجدد جلوگیری می‌کنیم
                if message.id <= last_seen_id:
                    print(f"⏭️ پیام {message.id} از {channel_key} قبلاً پردازش شده است (آخرین: {last_seen_id})")
                    return
                
                # اضافه کردن به لیست پیام‌های در حال پردازش
                processing_messages.add(message_key)
                
                # به‌روزرسانی فوری قبل از ارسال برای جلوگیری از race condition
                # ذخیره با همه کلیدهای ممکن برای اطمینان
                for key in all_keys:
                    last_messages[key] = message.id
                save_last_messages(last_messages)
            
            print(f"📨 پیام جدید دریافت شد از {event.chat_id}")
            
            old_username = getattr(entity, 'username', None)
            
            try:
                # ارسال پیام (حتی اگر old_username None باشد، forward_message آن را مدیریت می‌کند)
                await forward_message(
                    client,
                    message,
                    TARGET_CHANNEL,
                    old_username,
                    NEW_USERNAME
                )
            finally:
                # حذف از لیست پیام‌های در حال پردازش
                message_key = f"{channel_key}:{message.id}"
                processing_messages.discard(message_key)
        
        print("✅ ربات آماده است و در حال گوش دادن به پیام‌های جدید...")
        print("📌 کانال‌های منبع:", ', '.join(SOURCE_CHANNELS))
        print("📌 کانال هدف:", TARGET_CHANNEL)
        
        # شروع بررسی دوره‌ای (هر 30 ثانیه) - task در background اجرا می‌شود
        # استفاده از ensure_future برای اطمینان از اجرای task در event loop
        loop = asyncio.get_event_loop()
        periodic_task = loop.create_task(periodic_check(client, interval_seconds=30))
        
        # اطمینان از اینکه task شروع شده است
        await asyncio.sleep(0.5)
        if periodic_task.done():
            print("⚠️ هشدار: task بررسی دوره‌ای فوراً تمام شد!")
            try:
                await periodic_task
            except Exception as e:
                print(f"❌ خطا در task: {e}")
        else:
            print("✅ Task بررسی دوره‌ای با موفقیت شروع شد و در حال اجرا است")
        
        try:
            # اجرای مداوم - periodic_task در background اجرا می‌شود
            await client.run_until_disconnected()
        except KeyboardInterrupt:
            print("\n⏹️ دریافت سیگنال توقف (Ctrl+C)...")
        finally:
            # لغو task بررسی دوره‌ای
            print("🔄 در حال توقف task بررسی دوره‌ای...")
            if not periodic_task.done():
                periodic_task.cancel()
                try:
                    await asyncio.wait_for(periodic_task, timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            print("✅ Task بررسی دوره‌ای متوقف شد")
        
    except KeyboardInterrupt:
        print("\n⏹️ ربات متوقف شد")
    except Exception as e:
        print(f"❌ خطای غیرمنتظره: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        # قطع اتصال با مدیریت خطا
        print("🔄 در حال قطع اتصال...")
        try:
            # استفاده از wait_for برای جلوگیری از گیر کردن در disconnect
            await asyncio.wait_for(client.disconnect(), timeout=5.0)
            print("✅ اتصال با موفقیت قطع شد")
        except asyncio.TimeoutError:
            print("⚠️ قطع اتصال با تاخیر انجام شد (timeout)")
        except (KeyboardInterrupt, SystemExit):
            # اگر KeyboardInterrupt یا SystemExit در حین disconnect رخ دهد، 
            # آن را نادیده می‌گیریم چون در حال خاموش شدن هستیم
            print("⚠️ قطع اتصال در حین توقف برنامه انجام شد")
        except Exception as e:
            # سایر خطاها را گزارش می‌کنیم اما برنامه را متوقف نمی‌کنیم
            print(f"⚠️ خطا در قطع اتصال (غیر بحرانی): {str(e)}")

if __name__ == '__main__':
    asyncio.run(main())