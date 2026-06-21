import os
import sys
import time
import re
import logging
import tempfile
import json
import xml.etree.ElementTree as ET
import requests
from google import genai
from google.genai import types
from xhtml2pdf import pisa

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("monitor.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("NSE_Monitor")

# Configuration File Parser
def load_env(env_path=".env"):
    """Loads environment variables from a local .env file."""
    if not os.path.exists(env_path):
        logger.warning(f"Configuration file {env_path} not found. Using system environment variables.")
        return
    
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                os.environ[key.strip()] = val.strip()

# Load configuration
load_env()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PROCESSED_FILE = "processed_announcements.txt"
RSS_FEED_URL = "https://nsearchives.nseindia.com/content/rss/Corporate_Announcements.xml"
FALLBACK_RSS_URL = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"

# Verify credentials
if not TELEGRAM_BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN is not set. Please check your .env file or environment.")
if not TELEGRAM_CHAT_ID:
    logger.error("TELEGRAM_CHAT_ID is not set. Please check your .env file or environment.")
if not GEMINI_API_KEY:
    logger.error("GEMINI_API_KEY is not set. Please check your .env file or environment.")

# Initialize the Gemini Client
# If GEMINI_API_KEY is provided in the env file, passing it explicitly ensures it's set correctly.
try:
    if GEMINI_API_KEY:
        client = genai.Client(api_key=GEMINI_API_KEY)
    else:
        client = genai.Client()
except Exception as e:
    logger.error(f"Failed to initialize Gemini Client: {e}")
    client = None

# Custom headers for NSE to avoid anti-bot blocks
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive"
}

def get_processed_announcements():
    """Reads the processed announcements list from the local file."""
    if not os.path.exists(PROCESSED_FILE):
        return set()
    with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def mark_as_processed(announcement_id):
    """Appends a newly processed announcement ID/link to the local tracking file."""
    with open(PROCESSED_FILE, "a", encoding="utf-8") as f:
        f.write(f"{announcement_id}\n")

def fetch_rss_feed():
    """Fetches and parses the live NSE Corporate Announcements RSS Feed with fallback."""
    logger.info("Fetching NSE Corporate Announcements RSS Feed...")
    session = requests.Session()
    
    # Establish session state by visiting the main site first, mimicking a real browser
    try:
        session.get("https://www.nseindia.com/", headers=NSE_HEADERS, timeout=15)
    except Exception as e:
        logger.warning(f"Initial connection to NSE home page failed: {e}. Attempting RSS directly.")
        
    try:
        logger.info(f"Trying primary RSS URL: {RSS_FEED_URL}")
        response = session.get(RSS_FEED_URL, headers=NSE_HEADERS, timeout=15)
        
        if response.status_code == 404:
            logger.warning(f"Primary RSS URL returned 404. Trying active fallback URL: {FALLBACK_RSS_URL}")
            response = session.get(FALLBACK_RSS_URL, headers=NSE_HEADERS, timeout=15)
            
        response.raise_for_status()
        return response.content
    except Exception as e:
        logger.error(f"Error fetching RSS Feed: {e}")
        return None

def parse_rss_feed(xml_data):
    """Parses the RSS XML and extracts individual announcements."""
    if not xml_data:
        return []
    
    try:
        root = ET.fromstring(xml_data)
        channel = root.find("channel")
        if channel is None:
            return []
        
        items = []
        for item in channel.findall("item"):
            title = item.find("title")
            link = item.find("link")
            pub_date = item.find("pubDate")
            description = item.find("description")
            guid = item.find("guid")
            
            title_text = title.text.strip() if (title is not None and title.text is not None) else ""
            link_text = link.text.strip() if (link is not None and link.text is not None) else ""
            pub_date_text = pub_date.text.strip() if (pub_date is not None and pub_date.text is not None) else ""
            description_text = description.text.strip() if (description is not None and description.text is not None) else ""
            
            if guid is not None and guid.text is not None:
                guid_text = guid.text.strip()
            else:
                guid_text = link_text
                
            items.append({
                "title": title_text,
                "link": link_text,
                "pubDate": pub_date_text,
                "description": description_text,
                "guid": guid_text
            })
        return items
    except Exception as e:
        logger.error(f"Error parsing RSS XML: {e}")
        return []

def download_pdf(pdf_url):
    """Downloads a PDF from a link with custom browser headers, saving it to a temporary file."""
    logger.info(f"Downloading PDF from: {pdf_url}")
    session = requests.Session()
    
    # Pre-fetch main site for cookies to avoid bot blockers
    try:
        session.get("https://www.nseindia.com/", headers=NSE_HEADERS, timeout=15)
    except Exception as e:
        logger.debug(f"Pre-fetch during PDF download failed: {e}")
        
    try:
        response = session.get(pdf_url, headers=NSE_HEADERS, timeout=30)
        response.raise_for_status()
        
        # Verify it is indeed a PDF
        content_type = response.headers.get("Content-Type", "")
        if "application/pdf" not in content_type.lower() and not pdf_url.lower().endswith(".pdf"):
            logger.warning(f"Downloaded content from {pdf_url} might not be a PDF (Content-Type: {content_type})")
            
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        temp_file.write(response.content)
        temp_file.close()
        logger.info(f"PDF successfully downloaded to temporary file: {temp_file.name}")
        return temp_file.name
    except Exception as e:
        logger.error(f"Failed to download PDF from {pdf_url}: {e}")
        return None

def analyze_pdfs_with_gemini(pdf_paths):
    """Uploads multiple PDFs to Google Gemini API and performs a unified cross-document financial parsing."""
    if not client:
        logger.error("Gemini Client is not initialized. Skipping AI analysis.")
        return None
    
    if not pdf_paths:
        logger.error("No PDF paths provided for analysis.")
        return None
        
    logger.info(f"Uploading {len(pdf_paths)} PDFs to Gemini API for unified analysis...")
    uploaded_files = []
    try:
        # Upload all PDFs via the modern google-genai Files API
        for path in pdf_paths:
            logger.info(f"Uploading: {path}")
            uploaded_file = client.files.upload(file=path)
            logger.info(f"File uploaded successfully: {uploaded_file.name}")
            uploaded_files.append(uploaded_file)
            
        # Strict financial analysis prompt returning JSON
        prompt = (
            "You are a senior buy-side equity research analyst at an institutional fund house specializing in discovering high-growth micro, small, and mid-cap multibaggers in the Indian market.\n\n"
            "Analyze the attached corporate filing, earnings release, or investor presentation.\n\n"
            "Your task is to output a raw JSON object containing exactly two keys:\n"
            "1. \"telegram_message\": A concise, mobile-friendly summary formatted with basic HTML (<b>, <i>).\n"
            "2. \"pdf_html_report\": A comprehensive, dense, single-page growth triggers document formatted in clean HTML (ready for PDF conversion).\n\n"
            "Do NOT wrap the JSON in markdown blocks (like ```json). Return ONLY the raw JSON object. Do not hallucinate data. Write 'N/A' if a metric is missing.\n\n"
            "Here are the strict structures for both outputs:\n\n"
            "### 1. Structure for \"telegram_message\" (Punchy, Data-Heavy, Mobile-Friendly)\n"
            "⚡ <b>[Company Name] | [Period] Results Flash</b>\n"
            "<b>Sector:</b> [Sector] | <b>Ticker:</b> [Ticker]\n\n"
            "<b>🏢 Business & Moat:</b>\n"
            "• [1 sentence on what they do]\n"
            "• <b>Moat Check:</b> [Differentiated niche OR Commoditized price-taker?]\n\n"
            "<b>📊 Financial Snapshot (QoQ & YoY):</b>\n"
            "• 💰 <b>Rev:</b> ₹[X] cr (YoY: [+/-X]% [🟢/🔴] | QoQ: [+/-X]% [🟢/🔴])\n"
            "• 📈 <b>PAT:</b> ₹[X] cr | PAT Margin: [X]%\n"
            "• 🚀 <b>EBITDA:</b> ₹[X] cr | Margin: [X]% (YoY: [+/-X] bps)\n"
            "• 📦 <b>Order Book:</b> ₹[X] cr (Book-to-Bill: [X]x)\n\n"
            "<b>🔍 Quality & Execution:</b>\n"
            "• <b>Cash Conv:</b> OCF/PAT ratio [X]x [🟢/🔴]. [Flag working capital traps].\n"
            "• <b>Margin Driver:</b> [1 structural or cyclical reason for margin shift].\n\n"
            "<b>🎯 Growth Triggers & Timelines:</b>\n"
            "• [Trigger 1]: [Impact] (Hits P&L by [Timeline])\n"
            "• [Trigger 2]: [Impact] (Hits P&L by [Timeline])\n"
            "• <b>Capex/Guidance:</b> [Details on capacity addition and management targets]\n\n"
            "<b>🧨 Valuation & Risks:</b>\n"
            "• <b>What's in the price?:</b> [Is the current growth already priced in by consensus?]\n"
            "• <b>Key Risk:</b> [1 specific execution or balance sheet risk]\n"
            "• <b>Verdict:</b> [1 line: Beat/Miss/Inline & thesis intact/broken?]\n\n\n"
            "### 2. Structure for \"pdf_html_report\" (Mohit's Deep Growth Triggers Framework)\n"
            "Generate a highly professional, beautifully styled HTML string (using inline CSS) following this exact structure. Utilize clean inline CSS styles for fonts, spacing, padding, backgrounds, and highlighting. Avoid external fonts or CSS files.\n\n"
            "<div style=\"font-family: Helvetica, Arial, sans-serif; color: #2d3748; line-height: 1.6; padding: 25px; max-width: 800px; margin: 0 auto; border: 1px solid #e2e8f0; border-radius: 8px; background-color: #ffffff;\">\n\n"
            "  <h1 style=\"color: #1a365d; border-bottom: 3px solid #3182ce; padding-bottom: 10px; margin-top: 0; font-size: 26px; text-transform: uppercase;\">[Company Name] (Ticker: [Ticker]) - Growth Triggers & Earnings Analysis</h1>\n\n"
            "  <h2 style=\"color: #2b6cb0; background-color: #ebf8ff; padding: 8px 12px; border-left: 5px solid #3182ce; font-size: 18px; margin-top: 25px; margin-bottom: 12px;\">1. Company Snapshot</h2>\n"
            "  <div style=\"background-color: #f7fafc; padding: 12px; border: 1px solid #e2e8f0; border-radius: 6px; margin-bottom: 15px;\">\n"
            "    <p style=\"margin: 4px 0;\"><b>🏢 Business:</b> <u>[What the company does in one sentence without jargon]</u></p>\n"
            "    <p style=\"margin: 4px 0;\"><b>📊 Key Metrics:</b> <span style=\"background-color: #feebc8; padding: 2px 6px; border-radius: 4px; font-weight: bold;\">Market Cap: ₹[X] cr | CMP: ₹[X] | TTM Revenue: ₹[X] cr</span></p>\n"
            "    <p style=\"margin: 4px 0;\"><b>📈 Capital Allocation:</b> TTM Margin: [X]% | ROE: [X]% | ROCE: [X]% | Promoter Holding: [X]%</p>\n"
            "    <p style=\"margin: 4px 0;\"><b>⛓️ Value Chain & Customers:</b> [Where it sits in the value chain and who the end customers are]</p>\n"
            "    <p style=\"margin: 4px 0;\"><b>🛡️ Moat Analysis:</b> [What is unique? Is it a true moat or commoditized?]</p>\n"
            "  </div>\n\n"
            "  <h2 style=\"color: #2b6cb0; background-color: #ebf8ff; padding: 8px 12px; border-left: 5px solid #3182ce; font-size: 18px; margin-top: 25px; margin-bottom: 12px;\">2. Core Growth Triggers (Heart of the Document)</h2>\n"
            "  <ul style=\"list-style-type: none; padding-left: 0;\">\n"
            "    <li style=\"border-bottom: 1px solid #e2e8f0; padding-bottom: 15px; margin-bottom: 15px;\">\n"
            "      <span style=\"font-size: 16px; font-weight: bold; color: #2c5282;\">🚀 [Trigger 1 Name]</span> \n"
            "      <span style=\"background-color: #c6f6d5; color: #22543d; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: bold;\">🟢 High Conviction</span> <br>\n"
            "      <p style=\"margin: 6px 0;\"><i>What's happening:</i> [2-3 sentences explaining specific capex, policy, order book, or structural shift].</p>\n"
            "      <p style=\"margin: 4px 0;\"><b>💰 Quantified Impact:</b> <u>[Incremental revenue potential or margin expansion]</u></p>\n"
            "      <p style=\"margin: 4px 0;\"><b>📅 Timeline:</b> <span style=\"color: #3182ce; font-weight: bold;\">[When does this flow into the P&L?]</span></p>\n"
            "    </li>\n"
            "    <li style=\"padding-bottom: 5px;\">\n"
            "      <span style=\"font-size: 16px; font-weight: bold; color: #2c5282;\">⚡ [Trigger 2 Name]</span> \n"
            "      <span style=\"background-color: #feebc8; color: #744210; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: bold;\">🟡 Optionality</span> <br>\n"
            "      <p style=\"margin: 6px 0;\"><i>What's happening:</i> [2-3 sentences explaining specific capex, policy, order book, or structural shift].</p>\n"
            "      <p style=\"margin: 4px 0;\"><b>💰 Quantified Impact:</b> <u>[Incremental revenue potential or margin expansion]</u></p>\n"
            "      <p style=\"margin: 4px 0;\"><b>📅 Timeline:</b> <span style=\"color: #3182ce; font-weight: bold;\">[When does this flow into the P&L?]</span></p>\n"
            "    </li>\n"
            "  </ul>\n\n"
            "  <h2 style=\"color: #2b6cb0; background-color: #ebf8ff; padding: 8px 12px; border-left: 5px solid #3182ce; font-size: 18px; margin-top: 25px; margin-bottom: 12px;\">3. Financial Quality & Balance Sheet</h2>\n"
            "  <ul style=\"padding-left: 20px; margin-top: 8px;\">\n"
            "    <li style=\"margin-bottom: 8px;\"><b>💵 Cash Conversion:</b> [OCF vs PAT analysis, working capital cycle changes].</li>\n"
            "    <li style=\"margin-bottom: 8px;\"><b>⚖️ Leverage:</b> [Net Debt/Equity, interest coverage, capital allocation efficiency].</li>\n"
            "    <li style=\"margin-bottom: 8px;\"><b>🔔 One-Time Items:</b> [Flag any exceptional items or standalone vs consolidated deltas].</li>\n"
            "  </ul>\n\n"
            "  <h2 style=\"color: #2b6cb0; background-color: #ebf8ff; padding: 8px 12px; border-left: 5px solid #3182ce; font-size: 18px; margin-top: 25px; margin-bottom: 12px;\">4. Market Expectations (What's already in the price?)</h2>\n"
            "  <ul style=\"padding-left: 20px; margin-top: 8px;\">\n"
            "    <li style=\"margin-bottom: 8px;\"><b>📈 Consensus Discounting:</b> [What is the market already expecting?]</li>\n"
            "    <li style=\"margin-bottom: 8px;\"><b>⚠️ Key Risks:</b> <span style=\"color: #e53e3e; font-weight: bold;\">[Granite-level risks to the trend and potential impact]</span></li>\n"
            "  </ul>\n\n"
            "</div>"
        )
        
        logger.info("Generating content from Gemini Model gemini-2.5-flash with combined documents...")
        contents = uploaded_files + [prompt]
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=contents
        )
        
        return response.text
    except Exception as e:
        logger.error(f"Gemini API analysis failed: {e}")
        return None
    finally:
        # Clean up all remote files on Gemini system to conserve resources
        for f in uploaded_files:
            try:
                client.files.delete(name=f.name)
                logger.info(f"Successfully cleaned up remote Gemini file: {f.name}")
            except Exception as e:
                logger.warning(f"Failed to delete remote Gemini file {f.name}: {e}")

def send_telegram_chunk(text):
    """Helper to send a single message chunk to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
        response_data = response.json()
        if response.status_code == 200 and response_data.get("ok"):
            return True
        else:
            logger.error(f"Telegram API chunk error: {response_data}")
            return False
    except Exception as e:
        logger.error(f"Failed to post chunk to Telegram: {e}")
        return False

def post_to_telegram(text_payload, documents_info):
    """Posts the final formatted text payload to Telegram via raw HTTP request.
    Handles Telegram's 4096 character limit by dynamically splitting long messages into sequential posts.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials missing. Cannot send notification.")
        return False
    
    # Format all document links beautifully
    links_html = ""
    for doc in documents_info:
        links_html += f"• 🔗 <a href='{doc['link']}'>{doc['type']}</a>\n"
    
    message = (
        f"{text_payload}\n\n"
        f"<b>📚 Source Documents:</b>\n"
        f"{links_html}"
    )
    
    # Telegram max limit is 4096. We use 4000 to leave a safe margin.
    MAX_LENGTH = 4000
    
    if len(message) <= MAX_LENGTH:
        logger.info(f"Sending message to Telegram Chat/Group: {TELEGRAM_CHAT_ID}...")
        return send_telegram_chunk(message)
        
    logger.warning(f"Message length ({len(message)}) exceeds Telegram limit. Splitting into chunks...")
    
    # Split the message intelligently by sections (separated by double newlines or lines containing ---)
    parts = re.split(r'(\n\n|---)', message)
    
    chunks = []
    current_chunk = ""
    
    for part in parts:
        if len(current_chunk) + len(part) > MAX_LENGTH:
            if current_chunk.strip():
                chunks.append(current_chunk.strip())
            current_chunk = part
        else:
            current_chunk += part
            
    if current_chunk.strip():
        chunks.append(current_chunk.strip())
        
    # Send all chunks in sequence
    success = True
    for idx, chunk in enumerate(chunks):
        logger.info(f"Sending Telegram chunk {idx+1}/{len(chunks)} ({len(chunk)} characters)...")
        chunk_success = send_telegram_chunk(chunk)
        if not chunk_success:
            success = False
            
    return success

def send_pdf_to_telegram(pdf_path, caption):
    """Sends a local PDF document to the Telegram chat/group using the sendDocument API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials missing. Cannot send document.")
        return False
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        logger.info(f"Uploading generated PDF {pdf_path} to Telegram...")
        with open(pdf_path, "rb") as f:
            files = {"document": f}
            data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
            response = requests.post(url, files=files, data=data, timeout=30)
            
        response_data = response.json()
        if response.status_code == 200 and response_data.get("ok"):
            logger.info("PDF document uploaded successfully to Telegram.")
            return True
        else:
            logger.error(f"Telegram API sendDocument error: {response_data}")
            return False
    except Exception as e:
        logger.error(f"Failed to upload PDF to Telegram: {e}")
        return False

def process_announcements_group(company, results_ann, supplementary_anns):
    """Processes a group of announcements for a company: downloads all PDFs, runs unified Gemini analysis, posts to Telegram, and cleans up."""
    logger.info(f"Processing KMEW-style announcements group for: '{company}'")
    
    # Prepare list of items to process
    to_process = [results_ann] + supplementary_anns
    
    documents_info = []
    pdf_paths = []
    generated_pdf_path = None
    
    try:
        # 1. Download all PDFs
        for idx, ann in enumerate(to_process):
            link = ann["link"]
            desc = ann["description"].lower()
            
            # Determine document type label
            doc_type = "Financial Results"
            if idx > 0: # Supplementary
                if "presentation" in desc or "ppt" in link.lower() or "slide" in desc:
                    doc_type = "Investor Presentation"
                elif "press release" in desc or "pr" in link.lower() or "media release" in desc:
                    doc_type = "Press Release"
                elif "concall" in desc or "transcript" in desc or "call" in desc:
                    doc_type = "Concall Transcript"
                else:
                    doc_type = "Corporate Announcement"
                    
            pdf_path = download_pdf(link)
            if pdf_path:
                pdf_paths.append(pdf_path)
                documents_info.append({
                    "link": link,
                    "type": doc_type,
                    "title": ann["title"]
                })
                
        if not pdf_paths:
            logger.error(f"No PDFs successfully downloaded for {company}.")
            return False
            
        # 2. Analyze all PDFs in a single model call
        raw_json_result = analyze_pdfs_with_gemini(pdf_paths)
        if not raw_json_result:
            logger.error(f"AI analysis failed to return results for {company}.")
            return False
            
        # Strip markdown json blocks safely if returned by model
        cleaned_json_text = raw_json_result.strip()
        if cleaned_json_text.startswith("```json"):
            cleaned_json_text = cleaned_json_text[7:]
        elif cleaned_json_text.startswith("```"):
            cleaned_json_text = cleaned_json_text[3:]
        if cleaned_json_text.endswith("```"):
            cleaned_json_text = cleaned_json_text[:-3]
        cleaned_json_text = cleaned_json_text.strip()
        
        try:
            parsed_data = json.loads(cleaned_json_text)
            telegram_message = parsed_data.get("telegram_message", "")
            pdf_html_report = parsed_data.get("pdf_html_report", "")
        except Exception as e:
            logger.error(f"Failed to parse Gemini output as JSON: {e}")
            logger.error(f"Raw response was: {raw_json_result}")
            return False
            
        if not telegram_message:
            logger.error("No telegram_message found in parsed JSON.")
            return False
            
        # 3. Post Telegram text summary (handles chunking automatically!)
        success = post_to_telegram(telegram_message, documents_info)
        
        # 4. Generate local PDF from HTML report if present
        if pdf_html_report:
            try:
                # Generate unique clean file name for the PDF
                safe_company = "".join([c if c.isalnum() else "_" for c in company])
                generated_pdf_path = os.path.join(tempfile.gettempdir(), f"{safe_company}_Growth_Triggers.pdf")
                
                logger.info(f"Converting HTML report to PDF: {generated_pdf_path}...")
                with open(generated_pdf_path, "wb") as pdf_file:
                    pisa_status = pisa.CreatePDF(pdf_html_report, dest=pdf_file)
                    
                if not pisa_status.err:
                    logger.info("PDF conversion completed successfully.")
                    # 5. Send PDF to Telegram
                    caption = f"📊 {company} Growth Triggers & Earnings Analysis PDF"
                    pdf_success = send_pdf_to_telegram(generated_pdf_path, caption)
                    if not pdf_success:
                        logger.warning("Failed to send PDF report to Telegram.")
                else:
                    logger.error(f"xhtml2pdf error during conversion: {pisa_status.err}")
            except Exception as e:
                logger.error(f"Failed to convert HTML report to PDF or upload: {e}")
                
        return success
    except Exception as e:
        logger.error(f"Exception processing announcements group for {company}: {e}")
        return False
    finally:
        # 6. Clean up all local temporary files
        for path in pdf_paths:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                    logger.info(f"Cleaned up downloaded PDF: {path}")
                except Exception as e:
                    logger.error(f"Failed to remove temporary PDF {path}: {e}")
                    
        if generated_pdf_path and os.path.exists(generated_pdf_path):
            try:
                os.remove(generated_pdf_path)
                logger.info(f"Cleaned up generated PDF: {generated_pdf_path}")
            except Exception as e:
                logger.error(f"Failed to remove generated PDF {generated_pdf_path}: {e}")

def run_pipeline():
    """Main execution step of the pipeline run."""
    logger.info("Starting pipeline execution run...")
    
    # Fetch and parse the live RSS feed
    rss_data = fetch_rss_feed()
    if not rss_data:
        logger.warning("Could not fetch RSS feed data. Skipping this run.")
        return
        
    announcements = parse_rss_feed(rss_data)
    if not announcements:
        logger.info("No announcements found in the RSS feed.")
        return
        
    processed_ids = get_processed_announcements()
    logger.info(f"Currently tracking {len(processed_ids)} processed announcements.")
    
    # Filter keywords
    keywords = ["financial results", "financial statements"]
    
    # Group unprocessed announcements by company name
    company_groups = {}
    for ann in announcements:
        guid = ann["guid"]
        if guid in processed_ids:
            continue
            
        company = ann["title"]
        if company not in company_groups:
            company_groups[company] = []
        company_groups[company].append(ann)
        
    match_count = 0
    for company, group in company_groups.items():
        results_ann = None
        supplementary_anns = []
        
        # Look for a primary financial results document and supplementary files in the same batch
        for ann in group:
            title_lower = ann["title"].lower()
            desc_lower = ann["description"].lower()
            link_lower = ann["link"].lower()
            
            # Ensure PDF file suffix
            if not link_lower.endswith(".pdf"):
                continue
                
            # Primary matches
            if any(keyword in desc_lower or keyword in title_lower for keyword in keywords):
                results_ann = ann
            # Supplementary matches (Investor Presentation, Press Release, Concall Transcript)
            elif any(keyword in desc_lower or keyword in link_lower for keyword in ["presentation", "press release", "concall", "transcript", "conference call", "analyst meet"]):
                supplementary_anns.append(ann)
                
        # If there is a matching results announcement, process the entire company group together!
        if results_ann:
            match_count += 1
            logger.info(f"Found financial announcement bundle for '{company}':")
            logger.info(f"  - Primary: {results_ann['link']}")
            for idx, supp in enumerate(supplementary_anns):
                logger.info(f"  - Supp {idx+1}: {supp['link']}")
                
            # Process combined documents group
            success = process_announcements_group(company, results_ann, supplementary_anns)
            
            if success:
                # Mark all announcements in the processed bundle as successfully processed
                for ann in [results_ann] + supplementary_anns:
                    mark_as_processed(ann["guid"])
                    processed_ids.add(ann["guid"])
            else:
                logger.warning(f"Failed to process KMEW-style announcement bundle for: {company}")
                
    if match_count == 0:
        logger.info("No new matching financial announcements found in this run.")

def main():
    """Continuous loop running the pipeline every 60 seconds."""
    logger.info("=========================================")
    logger.info("Starting NSE Corporate Announcement Monitor")
    logger.info("=========================================")
    
    # Basic pre-flight check of credentials
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN environment variable not set. Exiting.")
        sys.exit(1)
        
    # Warn but don't exit to allow configuration while running
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not set. Gemini API calls will fail unless set dynamically.")
        
    while True:
        try:
            run_pipeline()
        except KeyboardInterrupt:
            logger.info("Monitor stopped by user keyboard interrupt.")
            break
        except Exception as e:
            logger.critical(f"Unexpected error in pipeline loop: {e}", exc_info=True)
            
        logger.info("Sleeping for 60 seconds before next feed scan...")
        time.sleep(60)

if __name__ == "__main__":
    main()
