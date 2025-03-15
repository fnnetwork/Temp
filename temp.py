import os
import logging
import random
import string
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from imap_tools import MailBox
import requests
from email.utils import parseaddr
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
        try:
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
                },
                timeout=10
            )
            logger.info(f"Cloudflare API Response: {response.status_code} - {response.text}")
            
            if response.status_code == 429:
                logger.error("Cloudflare API Rate Limit Exceeded")
                return {"success": False, "errors": [{"message": "Rate limit exceeded"}]}
                
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Cloudflare Connection Error: {str(e)}")
            return {"success": False, "errors": [{"message": str(e)}]}

    @staticmethod
    def delete_email_rule(rule_id):
        try:
            response = requests.delete(
                f"https://api.cloudflare.com/client/v4/zones/{os.getenv('CLOUDFLARE_ZONE_ID')}/email/routing/rules/{rule_id}",
                headers={
                    "Authorization": f"Bearer {os.getenv('CLOUDFLARE_API_TOKEN')}",
                    "Content-Type": "application/json"
                },
                timeout=10
            )
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Cloudflare Delete Rule Error: {str(e)}")
            return {"success": False}

class EmailHandler:
    @staticmethod
    async def check_emails(context: ContextTypes.DEFAULT_TYPE):
        try:
            with MailBox('imap.gmail.com').login(
                os.getenv("EMAIL_USER"),
                os.getenv("EMAIL_PASSWORD")
            ) as mailbox:
                for msg in mailbox.fetch():
                    try:
                        to_email = parseaddr(msg.to[0])[1].lower()
                        user = users.find_one({"emails.address": to_email})
                        
                        if user:
                            clean_text = EmailHandler.sanitize_content(msg.text or "")
                            email_content = (
                                f"📨 New Email: {to_email}\n"
                                f"From: {msg.from_}\n"
                                f"Subject: {msg.subject}\n\n"
                                f"{clean_text}"
                            )
                            
                            if len(email_content) > 4000:
                                email_content = email_content[:4000] + "\n... [truncated]"
                            
                            await context.bot.send_message(
                                chat_id=user['user_id'],
                                text=f"```\n{email_content}\n```",
                                parse_mode='MarkdownV2'
                            )
                            mailbox.delete(msg.uid)
                    except Exception as e:
                        logger.error(f"Email processing error: {e}")
        except Exception as e:
            logger.error(f"IMAP error: {e}")

    @staticmethod
    def sanitize_content(text):
        text = re.sub(r'([\_\*\[\]\(\)\~\`\>#\+\-=\|{}\.!])', r'\\\1', text)
        return text.encode('ascii', 'ignore').decode()

class TempMailBot:
    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.app = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()
        self._register_handlers()
        self._schedule_tasks()

    def _register_handlers(self):
        self.app.add_handler(CommandHandler("start", self._start))
        self.app.add_handler(CommandHandler("genemail", self._generate_email))
        self.app.add_handler(CommandHandler("myemails", self._list_emails))
        self.app.add_handler(CommandHandler("broadcast", self._broadcast))

    def _schedule_tasks(self):
        self.scheduler.add_job(self._delete_expired_emails, 'interval', hours=1)
        self.scheduler.add_job(
            EmailHandler.check_emails,
            'interval',
            seconds=int(os.getenv("CHECK_INTERVAL", 120)),
            args=[self.app]
        )

    async def _start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🌟 Temp Mail Bot\n\n"
            "/genemail - Create new temporary email\n"
            "/myemails - List your active emails"
        )

    async def _generate_email(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Environment check
        required_vars = ['CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID', 
                       'CLOUDFLARE_ZONE_ID', 'EMAIL_USER', 'DOMAIN']
        missing = [var for var in required_vars if not os.getenv(var)]
        if missing:
            logger.error(f"Missing vars: {', '.join(missing)}")
            await update.message.reply_text("⚠️ Service unavailable. Contact admin.")
            return

        user_id = update.effective_user.id
        email = f"{''.join(random.choices(string.ascii_lowercase + string.digits, k=12))}@{os.getenv('DOMAIN')}"
        
        response = CloudflareManager.create_email_rule(email)
        
        if response.get('success'):
            users.update_one(
                {"user_id": user_id},
                {"$push": {"emails": {
                    "address": email,
                    "expiry": datetime.now() + timedelta(days=1),
                    "created_at": datetime.now()
                }}},
                upsert=True
            )
            await update.message.reply_text(
                f"✅ Success!\n📧 `{email}`\n⏳ Expires in 24 hours",
                parse_mode='Markdown'
            )
        else:
            error_msg = "❌ Creation failed. "
            if errors := response.get('errors'):
                error_msg += errors[0].get('message', 'Unknown error')
            else:
                error_msg += "Internal error"
            await update.message.reply_text(error_msg)

    async def _list_emails(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = users.find_one({"user_id": update.effective_user.id})
        if not user or not user.get('emails'):
            return await update.message.reply_text("❌ No active emails found")
            
        emails = "\n".join([f"• {e['address']} ({e['expiry'].strftime('%Y-%m-%d %H:%M')})" for e in user['emails']])
        await update.message.reply_text(f"📩 Your emails:\n\n{emails}")

    async def _broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_user.id) != os.getenv("OWNER_ID"):
            return
        
        if not (message := ' '.join(context.args)):
            return await update.message.reply_text("Usage: /broadcast <message>")
            
        success = 0
        for user_id in users.distinct("user_id"):
            try:
                await context.bot.send_message(user_id, f"📢 Admin:\n{message}")
                success += 1
            except Exception as e:
                logger.error(f"Broadcast fail {user_id}: {e}")
        
        await update.message.reply_text(f"Broadcast sent to {success} users")

    async def _delete_expired_emails(self):
        now = datetime.now()
        expired = users.aggregate([
            {"$unwind": "$emails"},
            {"$match": {"emails.expiry": {"$lte": now}}}
        ])
        
        for doc in expired:
            email = doc['emails']['address']
            rules_res = requests.get(
                f"https://api.cloudflare.com/client/v4/zones/{os.getenv('CLOUDFLARE_ZONE_ID')}/email/routing/rules",
                headers={"Authorization": f"Bearer {os.getenv('CLOUDFLARE_API_TOKEN')}"}
            )
            if rules_res.ok:
                for rule in rules_res.json().get('result', []):
                    if email in rule['matchers'][0]['value']:
                        CloudflareManager.delete_email_rule(rule['id'])
            users.update_one(
                {"user_id": doc['user_id']},
                {"$pull": {"emails": {"address": email}}}
            )

    def run(self):
        self.scheduler.start()
        self.app.run_polling()

if __name__ == "__main__":
    TempMailBot().run()