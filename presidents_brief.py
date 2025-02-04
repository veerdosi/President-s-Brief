import os
import logging
import requests
from datetime import datetime
from flask import Flask, request
from twilio.rest import Client
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph
from reportlab.lib.styles import getSampleStyleSheet
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from apscheduler.schedulers.background import BackgroundScheduler
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration - Set these in environment variables
CONFIG = {
    'PERPLEXITY_API_KEY': os.getenv('PERPLEXITY_API_KEY'),
    'TWILIO_ACCOUNT_SID': os.getenv('TWILIO_ACCOUNT_SID'),
    'TWILIO_AUTH_TOKEN': os.getenv('TWILIO_AUTH_TOKEN'),
    'EMAIL_USER': os.getenv('EMAIL_USER'),
    'EMAIL_PASSWORD': os.getenv('EMAIL_PASSWORD'),
    'GOOGLE_CREDS_PATH': os.getenv('GOOGLE_CREDS_PATH', './credentials.json'),
    'SHEET_NAME': os.getenv('SHEET_NAME', 'Daily Brief Users'),
    'PORT': os.getenv('PORT', 5000)
}

# Google Sheets setup
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

# Initialize services
creds = ServiceAccountCredentials.from_json_keyfile_name(CONFIG['GOOGLE_CREDS_PATH'], SCOPES)
gc = gspread.authorize(creds)
twilio_client = Client(CONFIG['TWILIO_ACCOUNT_SID'], CONFIG['TWILIO_AUTH_TOKEN'])

# In-memory storage
users = {}
daily_requests = {}

class User:
    def __init__(self, row):
        self.background = row.get('Background', '')
        self.interests = [i.strip() for i in row.get('Interests', '').split(';') if i.strip()]
        self.phone = row.get('Phone', '')
        self.email = row.get('Email', '')
        self.sources = [s.strip() for s in row.get('Preferred Sources', '').split(';') if s.strip()]
        
    def __repr__(self):
        return f"<User {self.phone}>"

def load_users():
    """Load users from Google Sheet with error handling"""
    try:
        sheet = gc.open(CONFIG['SHEET_NAME']).sheet1
        records = sheet.get_all_records()
        
        new_users = {}
        for idx, row in enumerate(records, start=2):
            try:
                if not row.get('Phone') or not row.get('Email'):
                    continue
                
                user = User(row)
                new_users[user.phone] = user
            except Exception as e:
                logger.error(f"Error processing row {idx}: {e}")
                continue
                
        users.clear()
        users.update(new_users)
        logger.info(f"Loaded {len(users)} users")
    except Exception as e:
        logger.error(f"Failed to load users: {e}")

def generate_briefing(user, special_request=None):
    """Generate news briefing using Perplexity API"""
    prompt = f"""
    Create a daily news briefing for a user with these characteristics:
    
    Background: {user.background}
    Interests: {', '.join(user.interests)}
    Preferred Sources: {', '.join(user.sources) or 'None specified'}
    
    Today's Special Request: {special_request or 'None'}
    
    ## General Guidelines

    **Story Selection & Organization**
    - Prioritize stories based on:
    • Global/societal impact
    • Relevance to specified interests
    • Time sensitivity
    • Emerging trends and patterns
    - Group related stories together under broader themes

    **Coverage Standards**
    For each significant story:
    - Lead with core facts (who, what, when, where, why)
    - Include quantitative data when available
    - Note relevant historical context
    - Highlight key implications
    - Address competing interpretations when applicable
    - Cite specific sources for all claims

    **Objectivity Framework**
    - Use precise, neutral language
    - Separate verifiable facts from claims
    - Indicate certainty levels (confirmed, reported, alleged)
    - Present competing viewpoints proportionally
    - Acknowledge limitations in available information

    **When sources conflict:**
    - Prioritize higher-scored sources
    - Note specifically where accounts differ
    - Identify potential reasons for discrepancies
    """
    
    try:
        headers = {
            'Authorization': f'Bearer {CONFIG["PERPLEXITY_API_KEY"]}',
            'Content-Type': 'application/json'
        }
        
        data = {
            'model': 'pplx-70b-chat',
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0.2,
            'max_tokens': 2000
        }
        
        response = requests.post(
            'https://api.perplexity.ai/v1/chat/completions',
            headers=headers,
            json=data
        )
        response.raise_for_status()
        
        return response.json()['choices'][0]['message']['content']
    except Exception as e:
        logger.error(f"Perplexity API error: {e}")
        return "Error generating briefing. Please try again later."

def create_pdf(content, filename):
    """Create PDF with proper formatting"""
    try:
        doc = SimpleDocTemplate(filename, pagesize=letter)
        styles = getSampleStyleSheet()
        story = []
        
        # Add title
        title = Paragraph(f"Daily Brief - {datetime.now().strftime('%Y-%m-%d')}", styles['Title'])
        story.append(title)
        
        # Process content
        for section in content.split('\n\n'):
            if section.strip():
                p = Paragraph(section.replace('\n', '<br/>'), styles['BodyText'])
                story.append(p)
        
        doc.build(story)
        return filename
    except Exception as e:
        logger.error(f"PDF creation failed: {e}")
        raise

def send_email(user, pdf_path):
    """Send email with PDF attachment"""
    try:
        msg = MIMEMultipart()
        msg['From'] = CONFIG['EMAIL_USER']
        msg['To'] = user.email
        msg['Subject'] = f"Your Daily Brief - {datetime.now().strftime('%Y-%m-%d')}"
        
        body = MIMEText("""Here's your personalized daily news briefing.
                        Reply to this email with any feedback!""")
        msg.attach(body)
        
        with open(pdf_path, 'rb') as f:
            part = MIMEApplication(f.read(), Name=os.path.basename(pdf_path))
            part['Content-Disposition'] = f'attachment; filename="{os.path.basename(pdf_path)}"'
            msg.attach(part)
        
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(CONFIG['EMAIL_USER'], CONFIG['EMAIL_PASSWORD'])
            server.sendmail(CONFIG['EMAIL_USER'], user.email, msg.as_string())
            
        logger.info(f"Email sent to {user.email}")
    except Exception as e:
        logger.error(f"Failed to send email to {user.email}: {e}")

@app.route('/whatsapp', methods=['POST'])
def handle_whatsapp():
    """Handle incoming WhatsApp messages"""
    try:
        from_number = request.form.get('From', '').split(':')[1]
        message = request.form.get('Body', '').strip()
        
        if from_number in users:
            daily_requests[from_number] = message
            logger.info(f"Received request from {from_number}: {message}")
            return '<Response><Message>Request received! You\'ll get it in your next briefing.</Message></Response>'
            
        return '<Response><Message>⚠️ Not a registered user</Message></Response>'
    except Exception as e:
        logger.error(f"WhatsApp handler error: {e}")
        return '<Response><Message>Error processing request</Message></Response>'

def send_daily_briefings():
    """Main function to send all briefings"""
    try:
        load_users()  # Refresh user data
        logger.info("Starting daily briefing process")
        
        for phone, user in users.items():
            try:
                # Get special request and clear it
                special_request = daily_requests.pop(phone, None)
                
                # Generate content
                content = generate_briefing(user, special_request)
                if not content:
                    continue
                
                # Create PDF
                pdf_file = f"{phone}_{datetime.now().strftime('%Y%m%d')}.pdf"
                pdf_path = create_pdf(content, pdf_file)
                
                # Send email
                send_email(user, pdf_path)
                
                # Cleanup
                os.remove(pdf_path)
                
            except Exception as e:
                logger.error(f"Failed to process user {phone}: {e}")
                continue
                
        logger.info("Daily briefing process completed")
    except Exception as e:
        logger.error(f"Daily briefing failed: {e}")

if __name__ == '__main__':
    # Initial setup
    load_users()
    
    # Schedule daily at 2pm
    scheduler = BackgroundScheduler()
    scheduler.add_job(send_daily_briefings, 'cron', hour=14, timezone='UTC')
    scheduler.start()
    
    # Start Flask server
    app.run(host='0.0.0.0', port=CONFIG['PORT'])