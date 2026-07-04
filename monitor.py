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
            
        # Strict financial analysis prompt returning JSON — structural-growth hunter
        prompt = (
            "You are a senior buy-side equity research analyst at an institutional fund house. Your ONLY mandate is to identify Indian micro/small/mid-cap companies whose growth is STRUCTURAL and will compound over multiple quarters and years — not companies riding a one-off spike (base effects, one-time orders, commodity price windfalls, forex gains, tax reversals, seasonal aberrations, or accounting one-offs).\n\n"
            f"CRITICAL INSTRUCTION: You are analyzing the financial results for the company: '{company}'. \n"
            f"If the attached PDF documents belong to a completely different company, please flag this data mismatch in your analysis, but format your response for '{company}'.\n\n"
            "You are naturally skeptical. A single strong quarter proves nothing. Your job is to separate genuine multi-year compounders from flash-in-the-pan optical beats.\n\n"
            "Analyze the attached corporate filing, earnings release, and/or investor presentation as a UNIFIED set of documents.\n\n"
            "===========================================\n"
            "STEP 1 — DECONSTRUCT THE HEADLINE GROWTH\n"
            "===========================================\n"
            "Before writing anything else, mentally answer:\n"
            "• Is the YoY / QoQ growth flattered by a low base (COVID year, tax reversal, one-time export order, forex gain, land sale, insurance claim, subsidy)? If yes, DOWNGRADE the score aggressively.\n"
            "• Strip out exceptional items and re-look at the growth trajectory over the last 4-8 quarters if disclosed.\n"
            "• Is management guidance CONCRETE (capacity in MW/MT/units, order book with execution timeline, PLI-tied revenue, signed offtake contracts) or VAGUE (\"strong pipeline\", \"good visibility\", \"robust demand\")? Vague = downgrade.\n\n"
            "===========================================\n"
            "STEP 2 — SCORE EACH CATALYST AXIS (0-10)\n"
            "===========================================\n"
            "Score the company on these four structural catalyst axes. Each axis must have a durability HORIZON tag: '3YR+' (multi-year structural), '2YR' (visible for 2 years), '1YR' (only next 4 quarters), 'ONE-OFF' (single-quarter benefit), 'NONE' (no evidence).\n\n"
            "  A. CAPEX / CAPACITY EXPANSION — Is there hard, board-approved capex with a commissioning date and a demand-linked ramp? Backward integration counts. A vague 'exploring capex' is a 0.\n"
            "  B. POLICY / SECTOR TAILWINDS — PLI participation with sanctioned incentives, China+1 wallet share gains with named customers, import substitution mandates, defence/railway/power capex cycle, regulatory moat. Generic 'sector is growing' is a 0.\n"
            "  C. MARGIN STRUCTURAL RE-RATING — Real mix shift to higher-value SKUs, backward/forward integration reducing input cost, pricing power in a differentiated segment, operating leverage on a fixed cost base. Commodity price tailwind is NOT structural — score it low.\n"
            "  D. NEW PRODUCTS / CUSTOMERS / GEOGRAPHIES — Marquee customer additions with disclosed order value, entry into new export markets with first shipments already booked, new product SKUs that are in commercial production (not pilot). 'Plans to enter' = 0.\n\n"
            "===========================================\n"
            "STEP 3 — COMPUTE OVERALL SCORE & VERDICT\n"
            "===========================================\n"
            "structural_growth_score (0-10 integer): weighted overall score. Guidance:\n"
            "  9-10 = Rare multi-year compounder. Multiple axes rated 3YR+ with concrete evidence and hard numbers.\n"
            "  7-8  = Strong structural story with 2-3 durable catalysts (2YR+ horizons).\n"
            "  5-6  = Mixed. Some structural elements but heavy dependence on cyclical/one-off factors.\n"
            "  3-4  = Mostly one-off. Growth exists but will NOT sustain beyond 1-2 quarters.\n"
            "  0-2  = Pure one-off. Base effect, exceptional gain, or non-repeatable driver.\n\n"
            "verdict (pick exactly one string):\n"
            "  'STRONG STRUCTURAL COMPOUNDER' (score 8-10)\n"
            "  'STRUCTURAL BUT NASCENT' (score 6-7)\n"
            "  'MIXED — WATCH FOR EXECUTION' (score 5)\n"
            "  'AVOID — MOSTLY CYCLICAL' (score 3-4)\n"
            "  'AVOID — ONE-OFF GROWTH' (score 0-2)\n\n"
            "===========================================\n"
            "OUTPUT — RAW JSON, NO MARKDOWN WRAPPER\n"
            "===========================================\n"
            "Return ONLY a raw JSON object with EXACTLY these 5 top-level keys. Do NOT wrap in ```json fences. Write 'N/A' if a data point is missing. Do NOT hallucinate.\n\n"
            "{\n"
            "  \"structural_growth_score\": <int 0-10>,\n"
            "  \"verdict\": \"<one of the five verdict strings above>\",\n"
            "  \"catalyst_breakdown\": {\n"
            "    \"capex_capacity\":              {\"score\": <int 0-10>, \"horizon\": \"3YR+|2YR|1YR|ONE-OFF|NONE\", \"note\": \"<one sentence with hard numbers>\"},\n"
            "    \"policy_tailwinds\":            {\"score\": <int 0-10>, \"horizon\": \"3YR+|2YR|1YR|ONE-OFF|NONE\", \"note\": \"<one sentence with hard numbers>\"},\n"
            "    \"margin_rerating\":             {\"score\": <int 0-10>, \"horizon\": \"3YR+|2YR|1YR|ONE-OFF|NONE\", \"note\": \"<one sentence with hard numbers>\"},\n"
            "    \"new_products_customers_geo\":  {\"score\": <int 0-10>, \"horizon\": \"3YR+|2YR|1YR|ONE-OFF|NONE\", \"note\": \"<one sentence with hard numbers>\"}\n"
            "  },\n"
            "  \"telegram_message\": \"<HTML string, structure defined below>\",\n"
            "  \"pdf_html_report\": \"<HTML string, structure defined below>\"\n"
            "}\n\n"
            "-------------------------------------------\n"
            "STRUCTURE FOR \"telegram_message\" (mobile-friendly HTML, use <b>, <i>, <u> only)\n"
            "-------------------------------------------\n"
            "⚡ <b>[Company Name] | [Period] Results</b>\n"
            "<b>Structural Growth Score: [X]/10</b> — <i>[Verdict]</i>\n"
            "<b>Sector:</b> [Sector] | <b>Ticker:</b> [Ticker]\n\n"
            "<b>🧭 Durability Snapshot:</b>\n"
            "• Capex/Capacity: [X]/10 — [3YR+/2YR/1YR/ONE-OFF/NONE]\n"
            "• Policy Tailwinds: [X]/10 — [horizon]\n"
            "• Margin Re-rating: [X]/10 — [horizon]\n"
            "• New Products/Cust/Geo: [X]/10 — [horizon]\n\n"
            "<b>📊 Financials (QoQ & YoY, ex one-offs where flagged):</b>\n"
            "• 💰 <b>Rev:</b> ₹[X] cr (YoY: [+/-X]% 🟢/🔴 | QoQ: [+/-X]% 🟢/🔴)\n"
            "• 📈 <b>PAT:</b> ₹[X] cr | Margin: [X]% (adj: ₹[X] cr if one-offs present)\n"
            "• 🚀 <b>EBITDA:</b> ₹[X] cr | Margin: [X]% (YoY: [+/-X] bps)\n"
            "• 📦 <b>Order Book:</b> ₹[X] cr | Book-to-Bill: [X]x\n\n"
            "<b>🩺 One-Off Check (CRITICAL):</b>\n"
            "• <b>Base effect?</b> [Yes/No — explain in 1 line]\n"
            "• <b>Exceptional items?</b> [₹X cr from land sale / forex / tax reversal / subsidy / one-time order — or 'None flagged']\n"
            "• <b>Underlying growth (adjusted):</b> [YoY: X% | QoQ: X%]\n\n"
            "<b>🎯 Structural Triggers (with horizon):</b>\n"
            "• [Trigger 1] — <b>Horizon: [3YR+/2YR/1YR]</b>. [Quantified impact + when it hits P&L]\n"
            "• [Trigger 2] — <b>Horizon: [3YR+/2YR/1YR]</b>. [Quantified impact + when it hits P&L]\n"
            "• [Trigger 3, if any] — <b>Horizon:</b> [...]\n\n"
            "<b>🧨 Risks & Verdict:</b>\n"
            "• <b>Key Structural Risk:</b> [What could break the multi-year thesis]\n"
            "• <b>What's priced in:</b> [Consensus expectation vs the durable earnings power]\n"
            "• <b>Verdict:</b> [One line — is this a multi-year compounder or a one-quarter wonder?]\n\n"
            "-------------------------------------------\n"
            "STRUCTURE FOR \"pdf_html_report\" (professional inline-CSS HTML for xhtml2pdf)\n"
            "-------------------------------------------\n"
            "Use ONLY inline CSS. No external fonts, no external stylesheets. Follow this structure EXACTLY:\n\n"
            "<div style=\"font-family: Helvetica, Arial, sans-serif; color: #2d3748; line-height: 1.6; padding: 25px; max-width: 800px; margin: 0 auto; border: 1px solid #e2e8f0; border-radius: 8px; background-color: #ffffff;\">\n\n"
            "  <h1 style=\"color: #1a365d; border-bottom: 3px solid #3182ce; padding-bottom: 10px; margin-top: 0; font-size: 26px; text-transform: uppercase;\">[Company Name] (Ticker: [Ticker]) — Structural Growth Analysis</h1>\n\n"
            "  <!-- BIG SCORE BANNER -->\n"
            "  <div style=\"background-color: [#c6f6d5 if score>=7 else #feebc8 if score>=5 else #fed7d7]; border: 2px solid [#22543d if score>=7 else #744210 if score>=5 else #742a2a]; border-radius: 8px; padding: 14px 18px; margin-bottom: 20px; text-align: center;\">\n"
            "    <div style=\"font-size: 32px; font-weight: bold; color: [color matching border];\">Structural Growth Score: [X]/10</div>\n"
            "    <div style=\"font-size: 16px; font-weight: bold; margin-top: 4px;\">[Verdict string]</div>\n"
            "  </div>\n\n"
            "  <h2 style=\"color: #2b6cb0; background-color: #ebf8ff; padding: 8px 12px; border-left: 5px solid #3182ce; font-size: 18px; margin-top: 25px; margin-bottom: 12px;\">1. Company Snapshot</h2>\n"
            "  <div style=\"background-color: #f7fafc; padding: 12px; border: 1px solid #e2e8f0; border-radius: 6px; margin-bottom: 15px;\">\n"
            "    <p style=\"margin: 4px 0;\"><b>🏢 Business:</b> <u>[What the company does in one plain-English sentence]</u></p>\n"
            "    <p style=\"margin: 4px 0;\"><b>📊 Key Metrics:</b> <span style=\"background-color: #feebc8; padding: 2px 6px; border-radius: 4px; font-weight: bold;\">Market Cap: ₹[X] cr | CMP: ₹[X] | TTM Revenue: ₹[X] cr</span></p>\n"
            "    <p style=\"margin: 4px 0;\"><b>📈 Capital Allocation:</b> TTM Margin: [X]% | ROE: [X]% | ROCE: [X]% | Promoter Holding: [X]%</p>\n"
            "    <p style=\"margin: 4px 0;\"><b>⛓️ Value Chain:</b> [Position in value chain and end customers]</p>\n"
            "    <p style=\"margin: 4px 0;\"><b>🛡️ Moat:</b> [Genuine moat vs price-taker — be honest]</p>\n"
            "  </div>\n\n"
            "  <h2 style=\"color: #2b6cb0; background-color: #ebf8ff; padding: 8px 12px; border-left: 5px solid #3182ce; font-size: 18px; margin-top: 25px; margin-bottom: 12px;\">2. Structural Catalyst Scorecard</h2>\n"
            "  <table style=\"width: 100%; border-collapse: collapse; margin-bottom: 15px;\">\n"
            "    <thead><tr style=\"background-color: #edf2f7;\">\n"
            "      <th style=\"padding: 8px; text-align: left; border: 1px solid #e2e8f0;\">Axis</th>\n"
            "      <th style=\"padding: 8px; text-align: center; border: 1px solid #e2e8f0;\">Score</th>\n"
            "      <th style=\"padding: 8px; text-align: center; border: 1px solid #e2e8f0;\">Horizon</th>\n"
            "      <th style=\"padding: 8px; text-align: left; border: 1px solid #e2e8f0;\">Evidence</th>\n"
            "    </tr></thead>\n"
            "    <tbody>\n"
            "      <tr><td style=\"padding: 8px; border: 1px solid #e2e8f0;\"><b>Capex / Capacity</b></td><td style=\"padding: 8px; text-align: center; border: 1px solid #e2e8f0;\">[X]/10</td><td style=\"padding: 8px; text-align: center; border: 1px solid #e2e8f0;\">[3YR+/2YR/1YR/ONE-OFF/NONE]</td><td style=\"padding: 8px; border: 1px solid #e2e8f0;\">[One-line evidence with hard numbers]</td></tr>\n"
            "      <tr><td style=\"padding: 8px; border: 1px solid #e2e8f0;\"><b>Policy / Sector Tailwinds</b></td><td style=\"padding: 8px; text-align: center; border: 1px solid #e2e8f0;\">[X]/10</td><td style=\"padding: 8px; text-align: center; border: 1px solid #e2e8f0;\">[horizon]</td><td style=\"padding: 8px; border: 1px solid #e2e8f0;\">[Evidence]</td></tr>\n"
            "      <tr><td style=\"padding: 8px; border: 1px solid #e2e8f0;\"><b>Margin Structural Re-rating</b></td><td style=\"padding: 8px; text-align: center; border: 1px solid #e2e8f0;\">[X]/10</td><td style=\"padding: 8px; text-align: center; border: 1px solid #e2e8f0;\">[horizon]</td><td style=\"padding: 8px; border: 1px solid #e2e8f0;\">[Evidence]</td></tr>\n"
            "      <tr><td style=\"padding: 8px; border: 1px solid #e2e8f0;\"><b>New Products / Customers / Geo</b></td><td style=\"padding: 8px; text-align: center; border: 1px solid #e2e8f0;\">[X]/10</td><td style=\"padding: 8px; text-align: center; border: 1px solid #e2e8f0;\">[horizon]</td><td style=\"padding: 8px; border: 1px solid #e2e8f0;\">[Evidence]</td></tr>\n"
            "    </tbody>\n"
            "  </table>\n\n"
            "  <h2 style=\"color: #2b6cb0; background-color: #ebf8ff; padding: 8px 12px; border-left: 5px solid #3182ce; font-size: 18px; margin-top: 25px; margin-bottom: 12px;\">3. One-Off vs Structural: Growth Deconstruction</h2>\n"
            "  <div style=\"background-color: #fffaf0; padding: 12px; border: 1px solid #f6ad55; border-radius: 6px; margin-bottom: 15px;\">\n"
            "    <p style=\"margin: 4px 0;\"><b>Reported Growth:</b> Revenue YoY [X]% | PAT YoY [X]%</p>\n"
            "    <p style=\"margin: 4px 0;\"><b>Exceptional Items Flagged:</b> [₹X cr — describe each: land sale / forex / tax reversal / one-time order / subsidy — or 'None']</p>\n"
            "    <p style=\"margin: 4px 0;\"><b>Base Effect Check:</b> [Was the comparable quarter unusually weak? Explain]</p>\n"
            "    <p style=\"margin: 4px 0;\"><b>Adjusted Underlying Growth:</b> <span style=\"font-weight: bold; color: #2b6cb0;\">[X]% YoY / [X]% QoQ ex-one-offs</span></p>\n"
            "    <p style=\"margin: 4px 0;\"><b>Sustainability Verdict:</b> [Will the adjusted trajectory persist for 1/2/3+ years? Justify.]</p>\n"
            "  </div>\n\n"
            "  <h2 style=\"color: #2b6cb0; background-color: #ebf8ff; padding: 8px 12px; border-left: 5px solid #3182ce; font-size: 18px; margin-top: 25px; margin-bottom: 12px;\">4. Deep-Dive on Top Structural Triggers</h2>\n"
            "  <ul style=\"list-style-type: none; padding-left: 0;\">\n"
            "    <li style=\"border-bottom: 1px solid #e2e8f0; padding-bottom: 15px; margin-bottom: 15px;\">\n"
            "      <span style=\"font-size: 16px; font-weight: bold; color: #2c5282;\">🚀 [Trigger 1 Name]</span>\n"
            "      <span style=\"background-color: #c6f6d5; color: #22543d; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: bold; margin-left: 6px;\">Horizon: [3YR+/2YR/1YR]</span><br>\n"
            "      <p style=\"margin: 6px 0;\"><i>What's happening:</i> [2-3 sentences — specific capex date, order book number, policy detail, or capacity ramp].</p>\n"
            "      <p style=\"margin: 4px 0;\"><b>💰 Quantified Impact:</b> <u>[Incremental revenue ₹X cr / margin +X bps / EPS +X%]</u></p>\n"
            "      <p style=\"margin: 4px 0;\"><b>📅 P&L Timeline:</b> <span style=\"color: #3182ce; font-weight: bold;\">[Which quarters does this show up in]</span></p>\n"
            "      <p style=\"margin: 4px 0;\"><b>🔒 Why it's structural (not one-off):</b> [The moat / contract / capacity backing this claim]</p>\n"
            "    </li>\n"
            "    <li style=\"padding-bottom: 5px;\">\n"
            "      <span style=\"font-size: 16px; font-weight: bold; color: #2c5282;\">⚡ [Trigger 2 Name]</span>\n"
            "      <span style=\"background-color: #feebc8; color: #744210; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: bold; margin-left: 6px;\">Horizon: [2YR/1YR]</span><br>\n"
            "      <p style=\"margin: 6px 0;\"><i>What's happening:</i> [2-3 sentences]</p>\n"
            "      <p style=\"margin: 4px 0;\"><b>💰 Quantified Impact:</b> <u>[...]</u></p>\n"
            "      <p style=\"margin: 4px 0;\"><b>📅 P&L Timeline:</b> <span style=\"color: #3182ce; font-weight: bold;\">[...]</span></p>\n"
            "      <p style=\"margin: 4px 0;\"><b>🔒 Why it's structural (not one-off):</b> [...]</p>\n"
            "    </li>\n"
            "  </ul>\n\n"
            "  <h2 style=\"color: #2b6cb0; background-color: #ebf8ff; padding: 8px 12px; border-left: 5px solid #3182ce; font-size: 18px; margin-top: 25px; margin-bottom: 12px;\">5. Financial Quality Check</h2>\n"
            "  <ul style=\"padding-left: 20px; margin-top: 8px;\">\n"
            "    <li style=\"margin-bottom: 8px;\"><b>💵 Cash Conversion:</b> OCF/PAT [X]x. [Working capital cycle — is the growth actually turning into cash?]</li>\n"
            "    <li style=\"margin-bottom: 8px;\"><b>⚖️ Leverage:</b> Net Debt/Equity [X]x | Interest coverage [X]x. [Balance sheet strength for the capex plan]</li>\n"
            "    <li style=\"margin-bottom: 8px;\"><b>🔔 Standalone vs Consolidated:</b> [Any subsidiary weakness masking headline strength]</li>\n"
            "  </ul>\n\n"
            "  <h2 style=\"color: #2b6cb0; background-color: #ebf8ff; padding: 8px 12px; border-left: 5px solid #3182ce; font-size: 18px; margin-top: 25px; margin-bottom: 12px;\">6. What Could Break the Thesis</h2>\n"
            "  <ul style=\"padding-left: 20px; margin-top: 8px;\">\n"
            "    <li style=\"margin-bottom: 8px;\"><b>⚠️ Structural risk 1:</b> <span style=\"color: #e53e3e; font-weight: bold;\">[Specific risk that could kill the multi-year story]</span></li>\n"
            "    <li style=\"margin-bottom: 8px;\"><b>⚠️ Structural risk 2:</b> <span style=\"color: #e53e3e; font-weight: bold;\">[Second specific risk]</span></li>\n"
            "    <li style=\"margin-bottom: 8px;\"><b>📈 What's priced in:</b> [What is consensus already discounting vs the durable earnings power]</li>\n"
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
            structural_score = parsed_data.get("structural_growth_score", None)
            verdict = parsed_data.get("verdict", "")
            catalyst_breakdown = parsed_data.get("catalyst_breakdown", {})
        except Exception as e:
            logger.error(f"Failed to parse Gemini output as JSON: {e}")
            logger.error(f"Raw response was: {raw_json_result}")
            return False

        if not telegram_message:
            logger.error("No telegram_message found in parsed JSON.")
            return False

        # Log the durability call-out for future filtering / analytics
        try:
            score_int = int(structural_score) if structural_score is not None else -1
        except (TypeError, ValueError):
            score_int = -1
        logger.info(f"Structural Growth Score for {company}: {score_int}/10 — Verdict: {verdict}")
        if catalyst_breakdown:
            for axis, detail in catalyst_breakdown.items():
                if isinstance(detail, dict):
                    logger.info(f"  {axis}: {detail.get('score', 'N/A')}/10 [{detail.get('horizon', 'N/A')}] — {detail.get('note', '')}")

        # Prepend a prominent banner to the Telegram message if this is a one-off / mostly-cyclical setup.
        # We do this in code (not in the LLM prompt) so the tag is guaranteed even if the model deprioritizes it.
        if 0 <= score_int < 5:
            avoid_banner = (
                f"🚨 <b>AVOID — ONE-OFF / CYCLICAL GROWTH</b> 🚨\n"
                f"<b>Score: {score_int}/10</b> — <i>{verdict}</i>\n"
                f"⚠️ Growth is unlikely to sustain beyond 1-2 quarters. Skim only, do not build conviction.\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            )
            telegram_message = avoid_banner + telegram_message
        elif score_int >= 7:
            # Reinforce strong structural setups with a green banner too, so triage is a glance.
            strong_banner = (
                f"✅ <b>STRUCTURAL COMPOUNDER CANDIDATE</b> ✅\n"
                f"<b>Score: {score_int}/10</b> — <i>{verdict}</i>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            )
            telegram_message = strong_banner + telegram_message

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
