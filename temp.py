import os
import logging
import random
import string
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from imap_tools import MailBox, AND
import requests
from email.utils import parseaddr
import html
import re

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database setup
client = MongoClient(os.getenv("MONGODB_URL"))
db = client.temp_mail_db
users = db.users

class CloudflareManager:
    @staticmethod
    def create_email_rule(email):
        response = requests.post(
            f"https://api.cloudflare.com/client/v4/accounts/{os.getenv('CLOUDFLARE_ACCOUNT_ID')}/email/routing/rules",
            headers={
                "Authorization": f"Bearer {os.getenv('CLOUDFLARE_API_TOKEN')}",
                "Content-Type": "application/json"
            },
            json={
                "actions": [{
                    "type": "forward",
                    "value": [os.getenv("EMAIL_USER")]
                }],
                "matchers": [{
                    "type": "literal",
                    "field": "to",
                    "value": email
                }]
            }
        )
        return response.json()

    @staticmethod
    def delete_email_rule(rule_id):
        response = requests.delete(
            f"https://api.cloudflare.com/client/v4/zones/{os.getenv('CLOUDFLARE_ZONE_ID')}/email/routing/rules/{rule_id}",
            headers={
                "Authorization": f"Bearer {os.getenv('CLOUDFLARE_API_TOKEN')}",
                "Content-Type": "application/json"
            }
        )
        return response.json()

class EmailChecker:
    @staticmethod
    async def check_emails(context: ContextTypes.DEFAULT_TYPE):
        try:
            with MailBox('imap.gmail.com').login(
                os.getenv("EMAIL_USER"),
                os.getenv("EMAIL_PASSWORD")
            ) as mailbox:
                for msg in mailbox.fetch():
                    try:
                        # Get recipient email
                        to_email = parseaddr(msg.to[0])[1].lower()
                        
                        # Find user with this email
                        user = users.find_one({"emails.address": to_email})
                        
                        if user:
                            # Format email content
                            clean_text = EmailChecker.sanitize_content(msg.text or "")
                            email_content = (
                                f"📨 New Email: {to_email}\n"
                                f"From: {msg.from_}\n"
                                f"Subject: {msg.subject}\n\n"
                                f"{clean_text}"
                            )
                            
                            # Truncate if too long
                            if len(email_content) > 4000:
                                email_content = email_content[:4000] + "\n... [truncated]"
                            
                            # Send to user
                            await context.bot.send_message(
                                chat_id=user['user_id'],
                                text=f"```\n{email_content}\n```",
                                parse_mode='MarkdownV2'
                            )
                            
                            # Delete processed email
                            mailbox.delete(msg.uid)
                    except Exception as e:
                        logger.error(f"Error processing email: {e}")
        except Exception as e:
            logger.error(f"IMAP connection error: {e}")

    @staticmethod
    def sanitize_content(text):
        # Remove special characters that break Markdown
        text = re.sub(r'([\_\*\[\]\(\)\~\`\>#\+\-=\|{}\.!])', r'\\\1', text)
        # Remove non-ASCII characters
        return text.encode('ascii', 'ignore').decode()

class TempMailBot:
    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.app = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()
        
        # Register handlers
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("genemail", self.generate_email))
        self.app.add_handler(CommandHandler("myemails", self.list_emails))
        self.app.add_handler(CommandHandler("broadcast", self.broadcast))
        
        # Schedule jobs
        self.scheduler.add_job(self.delete_expired_emails, 'interval', hours=1)
        self.scheduler.add_job(
            EmailChecker.check_emails,
            'interval',
            seconds=int(os.getenv("CHECK_INTERVAL", 120)),
            args=[self.app]
        )
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🌟 Welcome to Temp Mail Bot!\n\n"
            "Use /genemail to create a new temporary email address\n"
            "Use /myemails to list your active addresses"
        )

    async def generate_email(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        username = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
        email = f"{username}@{os.getenv('DOMAIN')}"
        expiry = datetime.now() + timedelta(days=1)
        
        # Create Cloudflare rule
        response = CloudflareManager.create_email_rule(email)
        
        if response.get('success', False):
            users.update_one(
                {"user_id": user_id},
                {"$push": {"emails": {
                    "address": email,
                    "expiry": expiry,
                    "created_at": datetime.now()
                }}},
                upsert=True
            )
            await update.message.reply_text(
                f"✅ New email created!\n\n"
                f"📧 Address: `{email}`\n"
                f"⏳ Expires: {expiry.strftime('%Y-%m-%d %H:%M')}\n\n"
                "You'll receive incoming emails here automatically!",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text("❌ Failed to create email. Please try again later.")

    async def list_emails(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        user = users.find_one({"user_id": user_id})
        
        if not user or not user.get('emails'):
            return await update.message.reply_text("You don't have any active emails. Use /genemail to create one.")
            
        emails = "\n".join([
            f"• {e['address']} (expires {e['expiry'].strftime('%Y-%m-%d %H:%M')})"
            for e in user['emails']
        ])
        
        await update.message.reply_text(
            f"📩 Your active emails:\n\n{emails}\n\n"
            "Emails are automatically deleted after 24 hours.",
            parse_mode='Markdown'
        )

    async def broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_user.id) != os.getenv("OWNER_ID"):
            return
        
        message = ' '.join(context.args)
        if not message:
            return await update.message.reply_text("Please provide a message to broadcast.")
            
        all_users = users.distinct("user_id")
        success = 0
        
        for user_id in all_users:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"📢 Admin Broadcast:\n\n{message}"
                )
                success += 1
            except Exception as e:
                logger.error(f"Broadcast failed for {user_id}: {e}")
        
        await update.message.reply_text(f"Broadcast sent to {success}/{len(all_users)} users.")

    async def delete_expired_emails(self):
        now = datetime.now()
        expired = users.aggregate([
            {"$unwind": "$emails"},
            {"$match": {"emails.expiry": {"$lte": now}}}
        ])
        
        for doc in expired:
            email = doc['emails']['address']
            # Delete Cloudflare rule
            rules_response = requests.get(
                f"https://api.cloudflare.com/client/v4/zones/{os.getenv('CLOUDFLARE_ZONE_ID')}/email/routing/rules",
                headers={"Authorization": f"Bearer {os.getenv('CLOUDFLARE_API_TOKEN')}"}
            )
            
            if rules_response.ok:
                for rule in rules_response.json().get('result', []):
                    if email in rule['matchers'][0]['value']:
                        CloudflareManager.delete_email_rule(rule['id'])
            
            # Remove from database
            users.update_one(
                {"user_id": doc['user_id']},
                {"$pull": {"emails": {"address": email}}}
            )

    def run(self):
        self.scheduler.start()
        self.app.run_polling()

if __name__ == "__main__":
    bot = TempMailBot()
    bot.run()