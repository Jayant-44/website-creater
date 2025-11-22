import os
import json
import re
from datetime import datetime, timedelta
import random
from collections import defaultdict
from flask import Flask, render_template, request, redirect, session, flash, url_for, send_from_directory, jsonify, Response
from flask_mail import Mail, Message
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import Email, DataRequired, Length, ValidationError
from flask_wtf.file import FileField
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_dance.contrib.google import make_google_blueprint, google
from smtplib import SMTPException
from sqlalchemy import select, desc
import time
import traceback
import urllib.parse
import threading
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Get site URL from environment
SITE_URL = os.getenv('SITE_URL', 'http://localhost:5000')

# Create Flask app FIRST
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'your_secret_key')

# Apply ProxyFix for production (handles X-Forwarded headers from reverse proxy)
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Configure session and security based on environment
IS_PRODUCTION = not (SITE_URL.startswith('http://localhost') or SITE_URL.startswith('http://127.0.0.1'))

if IS_PRODUCTION:
    app.config['PREFERRED_URL_SCHEME'] = 'https'
    app.config['SESSION_COOKIE_SECURE'] = True
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    print("✅ Production mode: HTTPS enforced")
else:
    # Only allow insecure OAuth for local development
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
    app.config['PREFERRED_URL_SCHEME'] = 'http'
    app.config['SESSION_COOKIE_SECURE'] = False
    print("⚠️ Development mode: HTTP allowed")

# Session configuration
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

# Database configuration - Use PostgreSQL in production, SQLite for local dev
database_url = os.getenv('DATABASE_URL')
if database_url:
    # Fix for Render's postgres:// URL (SQLAlchemy needs postgresql://)
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    print("✅ Using PostgreSQL database")
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
    print("⚠️ Using SQLite database (local development)")

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 300,
}

# Initialize database
db = SQLAlchemy(app)

# Mail configuration
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True').lower() in ('true', '1', 'yes')
app.config['MAIL_USE_SSL'] = os.getenv('MAIL_USE_SSL', 'False').lower() in ('true', '1', 'yes')
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER', app.config['MAIL_USERNAME'])

# Initialize mail
mail = Mail(app)

# Validate mail configuration
if not app.config['MAIL_USERNAME'] or not app.config['MAIL_PASSWORD']:
    print("⚠️ WARNING: Mail credentials not configured. OTP emails will fail.")
    print("   Please set MAIL_USERNAME and MAIL_PASSWORD in your .env file")
else:
    print(f"✅ Mail configured: {app.config['MAIL_USERNAME']}")

# Google OAuth configuration
GOOGLE_OAUTH_CLIENT_ID = os.getenv('GOOGLE_OAUTH_CLIENT_ID')
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv('GOOGLE_OAUTH_CLIENT_SECRET')

if not GOOGLE_OAUTH_CLIENT_ID or not GOOGLE_OAUTH_CLIENT_SECRET:
    print("⚠️ WARNING: Google OAuth not configured")
    print("   Please set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET")
else:
    print(f"✅ Google OAuth configured for: {SITE_URL}")
    
    # Create Google blueprint with correct redirect URL
    google_bp = make_google_blueprint(
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        scope=[
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile"
        ],
        redirect_to="google_dashboard"
    )
    app.register_blueprint(google_bp, url_prefix="/login")

# Gemini configuration
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    print("Warning: Google Gemini not installed. Run: pip install google-generativeai")
    GEMINI_AVAILABLE = False

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    import numpy as np
    NLP_AVAILABLE = True
except ImportError:
    print("Warning: Advanced NLP libraries not installed. Using basic matching.")
    NLP_AVAILABLE = False

# Gemini API Keys configuration
# Gemini API Keys configuration - Load from environment
GEMINI_API_KEYS = {
    'homepage': os.getenv('GEMINI_API_KEY_HOMEPAGE', ''),
    'authentication': os.getenv('GEMINI_API_KEY_AUTH', ''),
    'header': os.getenv('GEMINI_API_KEY_HEADER', ''),
    'footer': os.getenv('GEMINI_API_KEY_FOOTER', ''),
    'about': os.getenv('GEMINI_API_KEY_ABOUT', ''),
    'contact': os.getenv('GEMINI_API_KEY_CONTACT', ''),
    'products': os.getenv('GEMINI_API_KEY_PRODUCTS', ''),
    'chatbot': os.getenv('GEMINI_API_KEY_CHATBOT', ''),
    'dashboard': os.getenv('GEMINI_API_KEY_DASHBOARD', ''),
    'pricing': os.getenv('GEMINI_API_KEY_PRICING', ''),
    'general': os.getenv('GEMINI_API_KEY_GENERAL', ''),
    'other': os.getenv('GEMINI_API_KEY_OTHER', '')
}

BACKUP_GEMINI_KEY = os.getenv('GEMINI_API_KEY_BACKUP', '')
GEMINI_MAIN_TYPE = 'homepage'

# Rate limiting
API_RATE_LIMITS = {
    'gemini_homepage': {'calls': 0, 'reset_time': datetime.now()},
    'gemini_authentication': {'calls': 0, 'reset_time': datetime.now()},
    'gemini_header': {'calls': 0, 'reset_time': datetime.now()},
    'gemini_footer': {'calls': 0, 'reset_time': datetime.now()},
    'gemini_about': {'calls': 0, 'reset_time': datetime.now()},
    'gemini_contact': {'calls': 0, 'reset_time': datetime.now()},
    'gemini_products': {'calls': 0, 'reset_time': datetime.now()},
    'gemini_chatbot': {'calls': 0, 'reset_time': datetime.now()},
    'gemini_dashboard': {'calls': 0, 'reset_time': datetime.now()},
    'gemini_pricing': {'calls': 0, 'reset_time': datetime.now()},
    'gemini_general': {'calls': 0, 'reset_time': datetime.now()},
    'gemini_backup': {'calls': 0, 'reset_time': datetime.now()}
}
MAX_CALLS_PER_MINUTE = 15

# Initialize Gemini models
gemini_models = {}
if GEMINI_AVAILABLE:
    for page_type, api_key in GEMINI_API_KEYS.items():
        if api_key and not api_key.startswith("YOUR_"):
            try:
                genai.configure(api_key=api_key)
                gemini_models[page_type] = genai.GenerativeModel('gemini-2.5-flash')
                print(f"✅ Gemini {page_type} initialized successfully")
            except Exception as e:
                print(f"Gemini {page_type} initialization failed: {e}")

    try:
        genai.configure(api_key=BACKUP_GEMINI_KEY)
        gemini_models['backup'] = genai.GenerativeModel('gemini-2.5-flash')
        print("✅ Gemini backup model initialized")
    except Exception as e:
        print(f"Backup Gemini initialization failed: {e}")

AI_ENHANCEMENT_THRESHOLD = 0.85

# File upload configuration
base_dir = os.path.abspath(os.path.dirname(__file__))
upload_folder = os.path.join(base_dir, 'static', 'profile_pics')
app.config['UPLOAD_FOLDER'] = upload_folder
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

# Template configuration
TEMPLATES_BASE = os.path.join(base_dir, 'templates')
PAGE_TYPES = [
    'homepage', 'authentication', 'about', 'contact', 'products', 'chatbot', 'dashboard', 'pricing'
]

# Ensure directories exist
for page_type in PAGE_TYPES + ['header', 'footer']:
    page_type_dir = os.path.join(TEMPLATES_BASE, page_type)
    os.makedirs(page_type_dir, exist_ok=True)
    details_file = os.path.join(page_type_dir, 'details.txt')
    if not os.path.exists(details_file):
        with open(details_file, 'w', encoding='utf-8') as f:
            f.write(f"Template details for {page_type} pages\n")
            f.write("=" * 50 + "\n")

AI_GENERATED_PATH = os.path.join(base_dir, 'templates', 'ai_generated')
os.makedirs(AI_GENERATED_PATH, exist_ok=True)

# ---------------------- Database Models ----------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    profile_image = db.Column(db.String(200), default='default.png')

class SearchHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    query = db.Column(db.String(500), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    match_score = db.Column(db.Float, default=0.0)
    template_used = db.Column(db.Integer, default=1)
    generation_type = db.Column(db.String(50), default='template_match')
    ai_enhanced = db.Column(db.Boolean, default=False)
    llm_used = db.Column(db.String(20), default='none')
    page_type = db.Column(db.String(50), default='authentication')
    selected_pages = db.Column(db.String(500), default='')
    user = db.relationship('User', backref=db.backref('search_history', lazy=True))

# ---------------------- Forms ----------------------
class SignUpForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=3, max=30)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=6)])
    submit = SubmitField('Sign Up')

class SignInForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Sign In')

class OTPForm(FlaskForm):
    otp = StringField('Enter OTP', validators=[DataRequired(), Length(min=6, max=6)])
    submit = SubmitField('Verify')

class ForgotPasswordForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    submit = SubmitField('Send OTP')

class ResetPasswordForm(FlaskForm):
    otp = StringField('OTP', validators=[DataRequired(), Length(min=6, max=6)])
    new_password = PasswordField('New Password', validators=[DataRequired(), Length(min=6)])
    submit = SubmitField('Reset Password')

class EditProfileForm(FlaskForm):
    username = StringField('New Username', validators=[DataRequired(), Length(min=3, max=30)])
    new_password = PasswordField('New Password', validators=[Length(max=50)])
    profile_image = FileField('Upload Profile Picture')
    submit = SubmitField('Save Changes')

# ---------------------- Helper Functions ----------------------
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_user_by_email(email):
    try:
        stmt = select(User).filter_by(email=email)
        return db.session.scalars(stmt).first()
    except Exception as e:
        print(f"Error fetching user: {e}")
        traceback.print_exc()
        return None

def send_otp_email(email, otp):
    """Send OTP email with proper error handling"""
    try:
        msg = Message(
            subject='OTP Verification - WebBuddy',
            recipients=[email],
            body=f'Your OTP for WebBuddy verification is: {otp}\n\nThis OTP will expire in 10 minutes.\n\nIf you did not request this, please ignore this email.'
        )
        mail.send(msg)
        print(f"✅ OTP email sent to {email}")
        return True
    except Exception as e:
        print(f"❌ Failed to send OTP email to {email}: {e}")
        traceback.print_exc()
        return False

# ---------------------- Page Type Detection ----------------------
class PageTypeDetector:
    def __init__(self):
        self.page_types = PAGE_TYPES

    def detect_page_type(self, prompt):
        prompt_lower = prompt.lower()
        page_keywords = {
            'homepage': ['home', 'landing', 'main page', 'welcome'],
            'authentication': ['login', 'signin', 'signup', 'register', 'authentication', 'auth'],
            'about': ['about', 'about us', 'story', 'mission'],
            'contact': ['contact', 'get in touch', 'reach us', 'email', 'phone'],
            'products': ['product', 'shop', 'store', 'catalog', 'items'],
            'chatbot': ['chat', 'bot', 'messenger', 'support'],
            'dashboard': ['dashboard', 'admin', 'panel', 'analytics'],
            'pricing': ['pricing', 'price', 'cost', 'subscription']
        }
        scores = {}
        for page_type, keywords in page_keywords.items():
            score = sum(1 for keyword in keywords if keyword in prompt_lower)
            if score > 0:
                scores[page_type] = score
        return max(scores, key=scores.get) if scores else 'authentication'

    def should_include_both_auth(self, prompt):
        prompt_lower = prompt.lower()
        only_signup = any(phrase in prompt_lower for phrase in ['only signup', 'just signup', 'only register'])
        only_login = any(phrase in prompt_lower for phrase in ['only login', 'only signin', 'just login'])
        return not (only_signup or only_login)

# ---------------------- Requirement Analyzer ----------------------
class RequirementAnalyzer:
    def __init__(self):
        self.style_keywords = {
            "modern": ["modern", "sleek", "clean", "minimal", "contemporary"],
            "classic": ["classic", "traditional", "formal", "elegant"],
            "creative": ["creative", "artistic", "colorful", "unique", "vibrant"],
            "professional": ["professional", "corporate", "business"],
            "dark": ["dark", "night", "black"],
            "light": ["light", "bright", "white", "airy"],
            "cyberpunk": ["cyberpunk", "neon", "futuristic"],
            "glassmorphism": ["glass", "blur", "transparent", "frosted"],
            "minimalist": ["minimalist", "simple", "bare"]
        }
        self.color_keywords = {
            "blue": ["blue", "azure", "navy"],
            "red": ["red", "crimson"],
            "green": ["green", "emerald"],
            "purple": ["purple", "violet"],
            "pink": ["pink", "rose"],
            "orange": ["orange", "amber"],
            "gradient": ["gradient", "rainbow"]
        }

    def analyze_prompt(self, user_prompt):
        analysis = {
            "style_preference": self.extract_style(user_prompt),
            "color_preferences": self.extract_colors(user_prompt),
            "features": self.extract_features(user_prompt),
            "complexity": self.determine_complexity(user_prompt),
            "theme_intensity": self.determine_theme_intensity(user_prompt),
        }
        return analysis

    def extract_style(self, prompt):
        prompt_lower = prompt.lower()
        style_scores = {}
        for style, keywords in self.style_keywords.items():
            score = sum(1 for keyword in keywords if keyword in prompt_lower)
            if score > 0:
                style_scores[style] = score
        return max(style_scores, key=style_scores.get) if style_scores else "modern"

    def extract_colors(self, prompt):
        prompt_lower = prompt.lower()
        colors = []
        for color, keywords in self.color_keywords.items():
            if any(keyword in prompt_lower for keyword in keywords):
                colors.append(color)
        return colors

    def extract_features(self, prompt):
        features = []
        prompt_lower = prompt.lower()
        feature_map = {
            "social_login": ["social", "google", "facebook"],
            "animations": ["animated", "animation"],
            "responsive": ["responsive", "mobile"],
            "forgot_password": ["forgot", "reset"]
        }
        for feature, keywords in feature_map.items():
            if any(keyword in prompt_lower for keyword in keywords):
                features.append(feature)
        return features

    def determine_complexity(self, prompt):
        prompt_lower = prompt.lower()
        if any(word in prompt_lower for word in ["simple", "basic", "minimal"]):
            return "simple"
        elif any(word in prompt_lower for word in ["advanced", "complex", "detailed"]):
            return "complex"
        return "medium"

    def determine_theme_intensity(self, prompt):
        prompt_lower = prompt.lower()
        if any(word in prompt_lower for word in ["vibrant", "bright", "bold"]):
            return "high"
        elif any(word in prompt_lower for word in ["subtle", "soft", "gentle"]):
            return "low"
        return "medium"

# ---------------------- Rate Limiter ----------------------
def check_rate_limit(page_type):
    """Improved rate limiter for specific page type"""
    service_key = f'gemini_{page_type}'
    if service_key not in API_RATE_LIMITS:
        return True
    now = datetime.now()
    time_diff = (now - API_RATE_LIMITS[service_key]['reset_time']).total_seconds()
    if time_diff >= 60:
        API_RATE_LIMITS[service_key]['calls'] = 0
        API_RATE_LIMITS[service_key]['reset_time'] = now
    current_calls = API_RATE_LIMITS[service_key]['calls']
    if current_calls >= MAX_CALLS_PER_MINUTE:
        wait_time = 60 - time_diff
        print(f"   ⏱️ Rate limit reached for {page_type}")
        print(f"   📊 {current_calls}/{MAX_CALLS_PER_MINUTE} calls used")
        print(f"   ⏰ Wait {wait_time:.0f}s for reset")
        return False
    API_RATE_LIMITS[service_key]['calls'] += 1
    print(f"   📊 {page_type} calls: {current_calls + 1}/{MAX_CALLS_PER_MINUTE}")
    return True

# ---------------------- Enhanced Template Matching Engine ----------------------
class EnhancedTemplateMatchingEngine:
    def __init__(self, page_type):
        self.page_type = page_type
        self.templates_metadata = self.load_enhanced_templates_metadata()

    def load_enhanced_templates_metadata(self):
        details_path = os.path.join(TEMPLATES_BASE, self.page_type, 'details.txt')
        templates = {}
        try:
            if os.path.exists(details_path):
                with open(details_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                sections = content.split('_______________________________________________________________________________________')
                for section in sections:
                    section = section.strip()
                    if section.startswith('(') and section.find(').') != -1:
                        try:
                            index_end = section.find(').')
                            template_id = int(section[1:index_end].strip())
                            description = section[index_end + 2:].strip()
                            templates[template_id] = {
                                'description': description,
                                'analyzed_requirements': self.analyze_template_features(description),
                                'primary_theme': self.extract_primary_theme(description),
                                'supported_features': self.extract_supported_features(description)
                            }
                        except ValueError:
                            continue
            else:
                templates[1] = {
                    'description': f"Modern {self.page_type} template",
                    'analyzed_requirements': {'style': 'modern'},
                    'primary_theme': 'modern',
                    'supported_features': ['basic']
                }
        except Exception as e:
            print(f"Error loading templates for {self.page_type}: {e}")
            traceback.print_exc()
            templates = {
                1: {
                    'description': f"Modern {self.page_type} template",
                    'analyzed_requirements': {'style': 'modern'},
                    'primary_theme': 'modern',
                    'supported_features': ['basic']
                }
            }
        print(f"📁 Loaded {len(templates)} templates for {self.page_type}")
        return templates

    def analyze_template_features(self, description):
        analyzer = RequirementAnalyzer()
        return analyzer.analyze_prompt(description)

    def extract_primary_theme(self, description):
        desc_lower = description.lower()
        themes = {
            "dark": ["dark", "night", "black"],
            "light": ["light", "bright", "white"],
            "cyberpunk": ["cyberpunk", "neon", "futuristic"],
            "minimalist": ["minimal", "clean", "simple"],
            "modern": ["modern", "sleek", "contemporary"],
            "glassmorphism": ["glass", "blur", "transparent"],
            "professional": ["professional", "corporate", "business"]
        }
        for theme, keywords in themes.items():
            if any(keyword in desc_lower for keyword in keywords):
                return theme
        return "modern"

    def extract_supported_features(self, description):
        desc_lower = description.lower()
        features = []
        feature_map = {
            "social_login": ["social", "google", "facebook", "login"],
            "animations": ["animated", "animation", "transition"],
            "responsive": ["responsive", "mobile", "tablet"],
            "toggle": ["toggle", "switch", "flip"],
            "gradient": ["gradient", "rainbow"],
            "glassmorphism": ["glass", "blur", "transparent"],
            "neumorphism": ["neumorphism", "soft ui"]
        }
        for feature, keywords in feature_map.items():
            if any(keyword in desc_lower for keyword in keywords):
                features.append(feature)
        return features

    def find_best_match(self, user_prompt, requirements):
        if not self.templates_metadata:
            return "no_match", 1, self.get_default_template(), 0.0
        descriptions = [template['description'] for template in self.templates_metadata.values()]
        template_ids = list(self.templates_metadata.keys())
        if NLP_AVAILABLE and len(descriptions) > 1:
            try:
                vectorizer = TfidfVectorizer(stop_words='english', max_features=1000)
                tfidf_matrix = vectorizer.fit_transform(descriptions + [user_prompt])
                similarities = cosine_similarity(tfidf_matrix[-1:], tfidf_matrix[:-1])
                best_match_idx = int(similarities.argmax())
                best_score = float(similarities[0][best_match_idx])
                best_template_id = template_ids[best_match_idx]
                best_template = self.templates_metadata[best_template_id]
                print(f"🔍 TF-IDF Match: Template {best_template_id} with score {best_score:.3f}")
                return "tfidf_match", best_template_id, best_template, best_score
            except Exception as e:
                print(f"TF-IDF matching failed: {e}, using fallback")
                traceback.print_exc()
        return self.basic_keyword_match(user_prompt, requirements)

    def basic_keyword_match(self, user_prompt, requirements):
        scores = []
        user_prompt_lower = user_prompt.lower()
        for template_id, template_data in self.templates_metadata.items():
            score = self.calculate_keyword_score(user_prompt_lower, requirements, template_data)
            scores.append((template_id, template_data, score))
        if not scores:
            return "no_match", 1, self.get_default_template(), 0.0
        scores.sort(key=lambda x: x[2], reverse=True)
        best_match = scores[0]
        match_type = "no_match"
        if best_match[2] >= 0.7:
            match_type = "exact_match"
        elif best_match[2] >= 0.4:
            match_type = "partial_match"
        return match_type, best_match[0], best_match[1], best_match[2]

    def calculate_keyword_score(self, user_prompt, requirements, template_data):
        score = 0
        user_style = requirements.get('style_preference', '').lower()
        template_style = template_data.get('primary_theme', '').lower()
        if user_style and template_style and user_style == template_style:
            score += 0.4
        user_features = set(requirements.get('features', []))
        template_features = set(template_data.get('supported_features', []))
        if user_features and template_features:
            overlap = len(user_features & template_features)
            union = len(user_features | template_features)
            if union > 0:
                score += 0.3 * (overlap / union)
        template_desc = template_data['description'].lower()
        common_words = set(user_prompt.split()) & set(template_desc.split())
        if common_words:
            score += 0.2 * (len(common_words) / len(set(user_prompt.split())))
        return min(score, 1.0)

    def get_default_template(self):
        return {
            'description': f"Default {self.page_type} template",
            'analyzed_requirements': {'style': 'modern'},
            'primary_theme': 'modern',
            'supported_features': []
        }

# ---------------------- Multi-LLM Enhancement Engine ----------------------
class MultiLLMEnhancementEngine:
    """Enhanced engine using Gemini with multiple API keys for PAGE TYPES and a backup"""

    def __init__(self):
        self.gemini_models = gemini_models
        self.min_html_length = 1000

    def get_model_for_label(self, label):
        """Label can be a page_type such as 'homepage' or 'backup' or 'general'"""
        return self.gemini_models.get(label)

    def generate_template_from_scratch(self, user_prompt, requirements, page_type):
        print(f"🎨 Locally generating {page_type} template from scratch (fallback)...")
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{page_type.title()} - Generated Template</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        h1 {{ color: #333; text-align: center; }}
        p {{ color: #666; line-height: 1.6; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>{page_type.title()} Page</h1>
        <p>This is a generated {page_type} template based on your requirements: "{user_prompt}"</p>
        <p>Style: {requirements.get('style_preference', 'modern')}</p>
        <p>Features: {', '.join(requirements.get('features', []))}</p>
    </div>
</body>
</html>"""

    def try_gemini_with_label(self, prompt, label, max_retries=2):
        """Try a specific model label (page_type label or 'backup') up to max_retries"""
        model = self.get_model_for_label(label)
        if not model:
            print(f"   ⚠️ No Gemini model found for label '{label}'")
            return None, None

        # Rate-limit check
        if not check_rate_limit(label):
            return None, None

        for attempt in range(max_retries):
            try:
                print(f"   🟢 Gemini {label} attempt {attempt + 1}/{max_retries}...")
                generation_config = {
                    'temperature': 0.8,
                    'top_p': 0.95,
                    'top_k': 40,
                    'max_output_tokens': 8192,
                }
                safety_settings = [
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                ]
                response = model.generate_content(
                    prompt,
                    generation_config=generation_config,
                    safety_settings=safety_settings
                )
                if hasattr(response, 'prompt_feedback') and getattr(response.prompt_feedback, 'block_reason', None):
                    print(f"   ⚠️ Gemini blocked: {response.prompt_feedback.block_reason}")
                    continue
                enhanced_html = getattr(response, 'text', None) or getattr(response, 'content', None) or str(response)
                if not enhanced_html:
                    print(f"   ⚠️ Gemini returned empty response")
                    continue
                enhanced_html = self.clean_html_response(enhanced_html)
                is_valid, error_msg = self.validate_html_completeness(enhanced_html)
                if not is_valid:
                    print(f"   ⚠️ Validation failed: {error_msg}")
                    continue
                print(f"   ✅ Gemini {label} Success! Generated {len(enhanced_html)} characters")
                return enhanced_html, f"gemini_{label}"
            except Exception as e:
                print(f"   ❌ Gemini {label} error (attempt {attempt + 1}): {str(e)[:200]}")
                traceback.print_exc()
                continue
        return None, None

    def try_gemini(self, prompt, page_type, max_retries=2):
        """Try page-type-specific Gemini (max_retries), then backup (2 attempts), then general; returns (html, label)"""
        tried_labels = []
        for label in (page_type, 'general'):
            if label in self.gemini_models:
                html, used_label = self.try_gemini_with_label(prompt, label, max_retries=max_retries)
                tried_labels.append(label)
                if html:
                    return html, used_label

        if 'backup' in self.gemini_models:
            html, used_label = self.try_gemini_with_label(prompt, 'backup', max_retries=2)
            tried_labels.append('backup')
            if html:
                return html, used_label

        print(f"   ❌ All Gemini attempts failed for page_type={page_type}. Tried: {tried_labels}")
        return None, None

    def clean_html_response(self, html_content):
        lines = html_content.split('\n')
        while lines and lines[0].strip().startswith('```'):
            lines.pop(0)
        while lines and lines[-1].strip() in ['```', '```html', '```HTML']:
            lines.pop()
        html_content = '\n'.join(lines)
        if not html_content.strip().startswith(('<!DOCTYPE', '<html', '<!doctype')):
            html_content = '<!DOCTYPE html>\n' + html_content
        return html_content.strip()

    def validate_html_completeness(self, html_content):
        if not html_content or len(html_content) < self.min_html_length:
            return False, "HTML too short"
        html_lower = html_content.lower()
        required_tags = [
            ('<!DOCTYPE', 'Missing DOCTYPE'),
            ('<html', 'Missing <html> tag'),
            ('<head', 'Missing <head> tag'),
            ('<body', 'Missing <body> tag'),
            ('</html>', 'Missing </html> tag')
        ]
        for element, error_msg in required_tags:
            if element.lower() not in html_lower:
                return False, error_msg
        return True, "Valid"

    def generate_from_scratch_via_gemini(self, user_prompt, requirements, page_type):
        prompt = f"""Create a complete, production-ready {page_type} page (full HTML) based on the user's requirements.

USER REQUIREMENTS: "{user_prompt}"

PAGE TYPE: {page_type}
STYLE: {requirements.get('style_preference', 'modern')}
FEATURES: {', '.join(requirements.get('features', []))}
COLORS: {', '.join(requirements.get('color_preferences', []))}

INSTRUCTIONS:
- Generate a COMPLETE, standalone HTML file from <!DOCTYPE html> to </html>.
- Include CSS inside <style> tags and any minimal JS inside <script> tags.
- Make sure it's responsive and follows best practices.
- Do not output any markdown — only HTML.

Generate the complete HTML code now:"""
        enhanced_html, llm_used = self.try_gemini(prompt, page_type, max_retries=2)
        if enhanced_html:
            return enhanced_html, True, llm_used
        fallback_html = self.generate_template_from_scratch(user_prompt, requirements, page_type)
        return fallback_html, False, "fallback_generator"

    def enhance_template(self, template_html, user_prompt, requirements, template_metadata, page_type, reference_html=None, use_main_label=False):
        """If use_main_label True, use the model for GEMINI_MAIN_TYPE for generation attempts (for main-page generation)."""
        try:
            print(f"\n{'='*70}")
            print(f"🤖 STARTING AI ENHANCEMENT PIPELINE FOR {page_type.upper()}")
            print(f"{'='*70}")
            print(f"📝 User Prompt: {user_prompt[:100]}...")
            if template_html is None:
                print("📄 Template: MISSING (will request generation from Gemini)")
            else:
                print(f"📄 Template Length: {len(template_html)} characters")

            enhancement_prompt = self.create_enhancement_prompt(template_html, user_prompt, requirements, page_type, reference_html)
            print(f"📋 Prompt Length: {len(enhancement_prompt)} characters")

            if use_main_label and GEMINI_MAIN_TYPE in self.gemini_models:
                html, label = self.try_gemini_with_label(enhancement_prompt, GEMINI_MAIN_TYPE, max_retries=2)
                if html:
                    return html, True, label
                print("   ⚠️ Main-key generation failed, falling back to page-specific/general/backup.")
            
            enhanced_html, llm_used = self.try_gemini(enhancement_prompt, page_type)
            if enhanced_html is None:
                print("\n💡 GEMINI ENHANCEMENT FAILED - ATTEMPTING GENERATE FROM SCRATCH VIA GEMINI")
                enhanced_html, gen_success, llm_used2 = self.generate_from_scratch_via_gemini(user_prompt, requirements, page_type)
                if gen_success:
                    llm_used = llm_used2
                else:
                    llm_used = llm_used2
            
            is_valid, msg = self.validate_html_completeness(enhanced_html)
            if not is_valid:
                print(f"\n❌ FINAL VALIDATION FAILED: {msg}")
                print("   Using original template or fallback generator instead")
                if template_html:
                    return template_html, False, "validation_failed"
                fallback_html = self.generate_template_from_scratch(user_prompt, requirements, page_type)
                return fallback_html, False, "fallback_generator"
            
            print(f"\n{'='*70}")
            print(f"✅ ENHANCEMENT SUCCESSFUL")
            print(f"{'='*70}")
            print(f"🤖 LLM Used: {llm_used.upper() if isinstance(llm_used, str) else llm_used}")
            print(f"📏 Original: {len(template_html) if template_html else 0} chars → Enhanced: {len(enhanced_html)} chars")
            if template_html:
                print(f"📊 Size Change: {((len(enhanced_html) - len(template_html)) / len(template_html) * 100):+.1f}%")
            print(f"{'='*70}\n")
            return enhanced_html, True, llm_used
        except Exception as e:
            print(f"\n❌ CRITICAL ERROR IN ENHANCEMENT ENGINE: {str(e)}")
            traceback.print_exc()
            if template_html:
                return template_html, False, "error"
            return self.generate_template_from_scratch(user_prompt, requirements, page_type), False, "error"

    def create_enhancement_prompt(self, template_html, user_prompt, requirements, page_type, reference_html=None):
        base_template_excerpt = (template_html[:1500] + '...') if template_html else "(no base template provided)"
        reference_section = ""
        if reference_html:
            reference_excerpt = reference_html[:4000]
            reference_section = f"\n\nREFERENCE HTML (use this for style/structure hints):\n{reference_excerpt}\n\n"
        prompt = f"""Create a complete, production-ready {page_type} page based on the user's requirements.

USER REQUIREMENTS: "{user_prompt}"

PAGE TYPE: {page_type}
STYLE: {requirements.get('style_preference', 'modern')}
FEATURES: {', '.join(requirements.get('features', []))}
COLORS: {', '.join(requirements.get('color_preferences', []))}

BASE TEMPLATE (for reference):
{base_template_excerpt}
{reference_section}

INSTRUCTIONS:
- Create a COMPLETE, standalone HTML file from <!DOCTYPE html> to </html>
- Make it visually appealing and match the requested style
- If a REFERENCE HTML is provided, maintain similar colors, typography, and feel across pages
- Ensure it's fully responsive and works on mobile devices
- Include all necessary CSS within <style> tags
- Include all necessary JavaScript within <script> tags
- Make sure it's production-ready and follows best practices
- NO markdown formatting, just pure HTML

Generate the complete HTML code:"""
        return prompt

# ---------------------- Main Website Generator ----------------------
class WebsiteGenerator:
    def __init__(self):
        self.analyzer = RequirementAnalyzer()
        self.page_detector = PageTypeDetector()
        self.ai_engine = MultiLLMEnhancementEngine()

    def process_user_request(self, user_prompt, selected_pages, user_id=None):
        print(f"\n{'='*60}")
        print(f"🎯 Processing: {user_prompt}")
        print(f"📄 Selected Pages: {selected_pages}")
        print(f"👤 User ID: {user_id}")
        print(f"{'='*60}")

        if isinstance(selected_pages, str):
            if selected_pages:
                page_types = [p.strip() for p in selected_pages.split(',') if p.strip()]
            else:
                page_types = []
        elif isinstance(selected_pages, list):
            page_types = [p for p in selected_pages if p and isinstance(p, str) and p.strip()]
        else:
            page_types = []

        if 'header' not in page_types:
            page_types.append('header')
        if 'footer' not in page_types:
            page_types.append('footer')

        print(f"📋 Parsed page types: {page_types}")

        valid_page_types = []
        for page_type in page_types:
            if page_type in PAGE_TYPES or page_type in ('header', 'footer'):
                valid_page_types.append(page_type)
                print(f"✅ Valid page type: {page_type}")
            else:
                print(f"⚠️ Invalid page type: {page_type}")

        if not valid_page_types:
            detected = self.page_detector.detect_page_type(user_prompt)
            valid_page_types = [detected]
            print(f"⚠️ No valid pages selected, auto-detected: {detected}")

        include_both = self.page_detector.should_include_both_auth(user_prompt)
        print(f"📂 Valid Page Types to Generate: {valid_page_types}")
        print(f"🔐 Include both login/signup: {include_both}")
        print(f"📊 Total pages to generate: {len(valid_page_types)}")

        requirements = self.analyzer.analyze_prompt(user_prompt)
        print(f"🎨 Style: {requirements.get('style_preference')}")
        print(f"🎨 Colors: {requirements.get('color_preferences')}")
        print(f"🔧 Features: {requirements.get('features')}")

        results = {}
        main_page_html = None
        main_page_saved_filename = None
        main_page_llm_used = None

        main_index = 0
        for i, pt in enumerate(valid_page_types):
            if pt not in ('header', 'footer'):
                main_index = i
                break

        if main_index != 0:
            valid_page_types[0], valid_page_types[main_index] = valid_page_types[main_index], valid_page_types[0]

        for idx, page_type in enumerate(valid_page_types, 1):
            print(f"\n{'─'*50}")
            print(f"📝 [{idx}/{len(valid_page_types)}] Processing {page_type}...")
            matcher = EnhancedTemplateMatchingEngine(page_type if page_type not in ('header','footer') else page_type)
            match_type, template_id, template_data, match_score = matcher.find_best_match(user_prompt, requirements)
            print(f"📋 Best Match: Template {template_id} (Score: {match_score:.0%})")

            if idx == 1:
                print("🔑 Treating this as the MAIN reference page.")
                template_html = load_template_html(page_type, template_id)
                missing_template = template_html is None
                needs_ai = match_score < AI_ENHANCEMENT_THRESHOLD or missing_template

                if needs_ai:
                    print(f"🤖 AI needed to create/enhance MAIN page: page_type={page_type}, missing_template={missing_template}, score={match_score:.2f}")
                    enhanced_html, success, llm_used = self.ai_engine.enhance_template(
                        template_html, user_prompt, requirements, template_data, page_type, reference_html=None, use_main_label=True
                    )
                    if success and enhanced_html:
                        ai_filename = save_ai_generated_template(enhanced_html, user_id or 0, int(datetime.now().timestamp()), page_type)
                        if ai_filename:
                            print(f"💾 Saved main page AI template: {ai_filename}")
                            main_page_saved_filename = ai_filename
                            main_page_html = enhanced_html
                            main_page_llm_used = llm_used
                            results[page_type] = {
                                'template_id': template_id,
                                'match_score': 0.90,
                                'requirements': requirements,
                                'action_taken': f"AI Enhanced using {llm_used.upper()}",
                                'ai_enhanced': True,
                                'ai_filename': ai_filename,
                                'original_score': match_score,
                                'llm_used': llm_used,
                                'page_type': page_type
                            }
                        else:
                            print("❌ Could not save main AI file; will fallback to template or local generator.")
                    else:
                        print("❌ AI enhancement/generation failed for main page. Will use template or local fallback.")
                
                if main_page_html:
                    break
                
                if template_html:
                    ai_filename = save_ai_generated_template(template_html, user_id or 0, int(datetime.now().timestamp()), page_type)
                    if ai_filename:
                        main_page_saved_filename = ai_filename
                        main_page_html = template_html
                        main_page_llm_used = 'none'
                        results[page_type] = {
                            'template_id': template_id,
                            'match_score': match_score,
                            'requirements': requirements,
                            'action_taken': f"Template {template_id} ({match_score:.0%} match)",
                            'ai_enhanced': False,
                            'ai_filename': ai_filename,
                            'original_score': match_score,
                            'llm_used': 'none',
                            'page_type': page_type
                        }
                        break
                
                fallback_html = self.ai_engine.generate_template_from_scratch(user_prompt, requirements, page_type)
                ai_filename = save_ai_generated_template(fallback_html, user_id or 0, int(datetime.now().timestamp()), page_type)
                main_page_saved_filename = ai_filename
                main_page_html = fallback_html
                main_page_llm_used = 'fallback_generate'
                results[page_type] = {
                    'template_id': template_id,
                    'match_score': 0.0,
                    'requirements': requirements,
                    'action_taken': "Fallback local generation",
                    'ai_enhanced': False,
                    'ai_filename': ai_filename,
                    'original_score': match_score,
                    'llm_used': 'fallback',
                    'page_type': page_type
                }
                break

        remaining_pages = valid_page_types[1:]
        threads = []
        lock = threading.Lock()

        def process_page(page_type_local):
            try:
                matcher_local = EnhancedTemplateMatchingEngine(page_type_local if page_type_local not in ('header','footer') else page_type_local)
                match_type, template_id_local, template_data_local, match_score_local = matcher_local.find_best_match(user_prompt, requirements)
                template_html_local = load_template_html(page_type_local, template_id_local)
                missing_template_local = template_html_local is None
                needs_ai_local = match_score_local < AI_ENHANCEMENT_THRESHOLD

                if missing_template_local:
                    print(f"📁 Template file missing for {page_type_local} (id {template_id_local}). Generating from scratch via Gemini for this page.")
                    generated_html, gen_success, llm_used_local = self.ai_engine.generate_from_scratch_via_gemini(user_prompt, requirements, page_type_local)
                    if gen_success and generated_html:
                        ai_filename_local = save_ai_generated_template(generated_html, user_id or 0, int(datetime.now().timestamp()), page_type_local)
                        local_result = {
                            'template_id': template_id_local,
                            'match_score': 0.0,
                            'requirements': requirements,
                            'action_taken': f"Created from scratch using {llm_used_local}",
                            'ai_enhanced': True,
                            'ai_filename': ai_filename_local,
                            'original_score': match_score_local,
                            'llm_used': llm_used_local,
                            'page_type': page_type_local
                        }
                        with lock:
                            results[page_type_local] = local_result
                        return
                    else:
                        fallback_html_local = self.ai_engine.generate_template_from_scratch(user_prompt, requirements, page_type_local)
                        ai_filename_local = save_ai_generated_template(fallback_html_local, user_id or 0, int(datetime.now().timestamp()), page_type_local)
                        local_result = {
                            'template_id': template_id_local,
                            'match_score': 0.0,
                            'requirements': requirements,
                            'action_taken': "Fallback local generation",
                            'ai_enhanced': False,
                            'ai_filename': ai_filename_local,
                            'original_score': match_score_local,
                            'llm_used': 'fallback',
                            'page_type': page_type_local
                        }
                        with lock:
                            results[page_type_local] = local_result
                        return

                if needs_ai_local:
                    print(f"🤖 AI Enhancement NEEDED for {page_type_local} (Score {match_score_local:.0%} < {AI_ENHANCEMENT_THRESHOLD:.0%})")
                    enhanced_html_local, success_local, llm_used_local = self.ai_engine.enhance_template(
                        template_html_local, user_prompt, requirements, template_data_local, page_type_local, reference_html=main_page_html
                    )
                    if success_local and enhanced_html_local:
                        ai_filename_local = save_ai_generated_template(enhanced_html_local, user_id or 0, int(datetime.now().timestamp()), page_type_local)
                        if ai_filename_local:
                            local_result = {
                                'template_id': template_id_local,
                                'match_score': 0.90,
                                'requirements': requirements,
                                'action_taken': f"AI Enhanced using {llm_used_local.upper()}",
                                'ai_enhanced': True,
                                'ai_filename': ai_filename_local,
                                'original_score': match_score_local,
                                'llm_used': llm_used_local,
                                'page_type': page_type_local
                            }
                            with lock:
                                results[page_type_local] = local_result
                            return
                        else:
                            print(f"❌ Failed to save AI enhanced template for {page_type_local}")
                    else:
                        print(f"❌ AI enhancement failed for {page_type_local}")

                print(f"📄 Using template as-is for {page_type_local}")
                local_result = {
                    'template_id': template_id_local,
                    'match_score': match_score_local,
                    'requirements': requirements,
                    'action_taken': f"Template {template_id_local} ({match_score_local:.0%} match)",
                    'ai_enhanced': False,
                    'ai_filename': None,
                    'original_score': match_score_local,
                    'llm_used': 'none',
                    'page_type': page_type_local
                }
                with lock:
                    results[page_type_local] = local_result
            except Exception as e:
                print(f"Error processing page {page_type_local}: {e}")
                traceback.print_exc()
                with lock:
                    results[page_type_local] = {
                        'template_id': 1,
                        'match_score': 0.0,
                        'requirements': requirements,
                        'action_taken': 'error',
                        'ai_enhanced': False,
                        'ai_filename': None,
                        'original_score': 0.0,
                        'llm_used': 'none',
                        'page_type': page_type_local
                    }

        for page in remaining_pages:
            t = threading.Thread(target=process_page, args=(page,))
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        print(f"\n{'='*60}")
        print(f"✅ Generation complete: {len(results)} pages generated")
        for page_type in results.keys():
            status = "✨ AI Enhanced" if results[page_type]['ai_enhanced'] else "📄 Template"
            print(f"   • {page_type}: {status}")
        print(f"{'='*60}\n")

        return {
            'results': results,
            'include_both_auth': include_both,
            'primary_page': valid_page_types[0] if valid_page_types else 'authentication'
        }

# ---------------------- Helper Functions ----------------------
def load_template_html(page_type, template_id):
    """Load HTML from template file for specific page type.
       Returns None if file is missing (so generator can create from scratch).
    """
    template_path = os.path.join(TEMPLATES_BASE, page_type, f"{template_id}.html")
    print(f"🔍 Looking for template: {template_path}")
    try:
        with open(template_path, 'r', encoding='utf-8') as f:
            content = f.read()
            print(f"✅ Template loaded: {len(content)} characters")
            return content
    except FileNotFoundError:
        print(f"❌ Template not found: {template_path}")
        return None
    except Exception as e:
        print(f"❌ Error loading template: {e}")
        traceback.print_exc()
        return None

def save_ai_generated_template(html_content, user_id, query_id, page_type):
    """Save AI-generated template with validation"""
    if not html_content or len(html_content) < 1000:
        print(f"❌ Refusing to save incomplete HTML ({len(html_content) if html_content else 0} chars)")
        return None
    required = ['<!DOCTYPE', '<html', '<head>', '</head>', '<body', '</body>', '</html>']
    html_lower = html_content.lower()
    for req in required:
        if req.lower() not in html_lower:
            print(f"❌ Refusing to save - missing {req}")
            return None
    safe_page = re.sub(r'[^a-zA-Z0-9_]', '_', page_type)
    filename = f"ai_{user_id}_{query_id}_{safe_page}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    filepath = os.path.join(AI_GENERATED_PATH, filename)
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html_content)
        print(f"💾 Successfully saved: {filename} ({len(html_content)} bytes)")
        return filename
    except Exception as e:
        print(f"❌ Error saving template: {e}")
        traceback.print_exc()
        return None

# ---------------------- Smart Suggestions ----------------------
@app.route('/get_smart_suggestions')
def get_smart_suggestions():
    """Get TF-IDF based smart suggestions from user history"""
    if 'email' not in session:
        return jsonify({'success': False, 'suggestions': []})
    user = get_user_by_email(session['email'])
    if not user:
        return jsonify({'success': False, 'suggestions': []})
    try:
        try:
            stmt = select(SearchHistory).filter_by(user_id=user.id).order_by(desc(SearchHistory.timestamp)).limit(50)
            history = db.session.scalars(stmt).all()
        except Exception as e:
            print(f"Database query error: {e}")
            traceback.print_exc()
            return jsonify({'success': False, 'suggestions': []})
        if not history:
            return jsonify({'success': False, 'suggestions': []})
        queries = [item.query for item in history]
        suggestions = generate_tfidf_suggestions(queries)
        return jsonify({
            'success': True,
            'suggestions': suggestions[:6]
        })
    except Exception as e:
        print(f"Error generating suggestions: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'suggestions': []})

def generate_tfidf_suggestions(queries):
    """Generate TF-IDF based suggestions from user queries that depend on user's history"""
    try:
        if not queries:
            return []
        template_queries = [
            "Create a responsive login and signup form with toggle animation",
            "Modern authentication form with social login and smooth transitions",
            "Dark mode cyberpunk login form with neon glowing effects",
            "Glassmorphism signup form with blur effects and gradient background",
            "Minimalist professional login page with clean white design",
            "Retro 90s style signup form with bold colors and geometric shapes",
            "E-commerce product page with image gallery and reviews",
            "Modern portfolio website with dark theme and animations",
            "Responsive admin dashboard with charts and analytics",
            "Blog homepage with featured posts and categories",
            "Sales landing page with call-to-action buttons",
            "Contact form with validation and success message",
            "Header navigation with dropdown menus and search bar",
            "Footer with social links and contact information",
            "About page with team members and company story",
            "Agriculture landing page with crop sections and market prices",
            "Farmers product listing with filter and price comparison",
            "Support chatbot widget for customer queries",
            "Interactive homepage with video background and CTA",
            "Pricing table with monthly and yearly toggles"
        ]
        if NLP_AVAILABLE:
            user_count = len(queries)
            if user_count > 1:
                weights = np.linspace(1.5, 1.0, num=user_count)
            else:
                weights = np.array([1.2])
            all_queries = queries + template_queries
            vectorizer = TfidfVectorizer(stop_words='english', max_features=1000)
            tfidf_matrix = vectorizer.fit_transform(all_queries)
            user_vectors = tfidf_matrix[:user_count]
            template_vectors = tfidf_matrix[user_count:]
            if user_vectors.shape[0] == 0 or template_vectors.shape[0] == 0:
                return [{
                    'query': q,
                    'title': q[:50] + '...',
                    'description': 'Suggested',
                    'score': 0.5
                } for q in template_queries[:6]]
            similarity_matrix = cosine_similarity(user_vectors, template_vectors)
            if similarity_matrix.shape[0] != len(weights):
                weights_local = np.ones(similarity_matrix.shape[0])
            else:
                weights_local = weights[:similarity_matrix.shape[0]]
            avg_similarities = np.average(similarity_matrix, axis=0, weights=weights_local)
            suggestions = []
            for i, score in enumerate(avg_similarities):
                suggestions.append({
                    'query': template_queries[i],
                    'title': template_queries[i][:50] + '...',
                    'description': 'Based on your recent searches',
                    'score': float(score)
                })
            suggestions.sort(key=lambda x: x['score'], reverse=True)
            if not suggestions or suggestions[0]['score'] < 0.05:
                return [
                    {
                        'query': "Create a responsive login and signup form with toggle animation",
                        'title': "Modern Auth Form",
                        'description': "Toggle between login and signup with smooth animations",
                        'score': 0.9
                    },
                    {
                        'query': "E-commerce product page with image gallery and reviews",
                        'title': "E-commerce Product Page",
                        'description': "Product listing and gallery with reviews",
                        'score': 0.8
                    }
                ][:6]
            return suggestions[:6]
        else:
            words = {}
            for q in queries:
                for token in re.findall(r'\w{3,}', q.lower()):
                    words[token] = words.get(token, 0) + 1
            top_words = sorted(words.items(), key=lambda x: x[1], reverse=True)[:10]
            top_tokens = {w for w, _ in top_words}
            scored = []
            for t in template_queries:
                score = len(set(re.findall(r'\w{3,}', t.lower())) & top_tokens)
                scored.append((t, score))
            scored.sort(key=lambda x: x[1], reverse=True)
            suggestions = []
            for t, s in scored[:6]:
                suggestions.append({
                    'query': t,
                    'title': t[:50] + '...',
                    'description': 'Based on your recent searches',
                    'score': float(s)
                })
            if not suggestions:
                suggestions = [
                    {
                        'query': "Create a responsive login and signup form with toggle animation",
                        'title': "Modern Auth Form",
                        'description': "Toggle between login and signup with smooth animations",
                        'score': 0.9
                    },
                    {
                        'query': "E-commerce product page with image gallery and reviews",
                        'title': "E-commerce Product Page",
                        'description': "Product listing and gallery with reviews",
                        'score': 0.8
                    }
                ]
            return suggestions[:6]
    except Exception as e:
        print(f"TF-IDF suggestion error: {e}")
        traceback.print_exc()
        return [
            {
                'query': "Create a responsive login and signup form with toggle animation",
                'title': "Modern Auth Form",
                'description': "Toggle between login and signup with smooth animations",
                'score': 0.9
            },
            {
                'query': "E-commerce product page with image gallery and reviews",
                'title': "E-commerce Product Page",
                'description': "Product listing and gallery with reviews",
                'score': 0.8
            }
        ]

def ensure_template_files_exist():
    for page_type in PAGE_TYPES + ['header', 'footer']:
        page_dir = os.path.join(TEMPLATES_BASE, page_type)
        os.makedirs(page_dir, exist_ok=True)
        for tid in range(1, 4):
            template_path = os.path.join(page_dir, f'{tid}.html')
            if not os.path.exists(template_path):
                print(f"⚠️ Creating missing template: {page_type}/{tid}.html")
                with open(template_path, 'w', encoding='utf-8') as f:
                    f.write(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{page_type.title()} - Template {tid}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}
        .container {{
            background: rgba(255, 255, 255, 0.95);
            padding: 40px;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            max-width: 700px;
            width: 100%;
            text-align: center;
        }}
        h1 {{
            color: #667eea;
            margin-bottom: 20px;
            font-size: 2em;
        }}
        p {{
            color: #666;
            line-height: 1.6;
            margin-bottom: 15px;
            font-size: 1.05em;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🚜 Farmer's {page_type.title()}</h1>
        <p>Welcome to your agricultural {page_type} solution!</p>
        <div class="feature-list">
            <h3>🌱 Features:</h3>
            <ul style="text-align:left; display:inline-block; margin-top:10px;">
                <li>Farm management tools</li>
                <li>Crop monitoring</li>
                <li>Weather integration</li>
                <li>Market prices</li>
                <li>Expert advice</li>
            </ul>
        </div>
        <p><strong>Designed specifically for modern farmers</strong></p>
    </div>
</body>
</html>""")
        details_path = os.path.join(page_dir, 'details.txt')
        with open(details_path, 'w', encoding='utf-8') as f:
            f.write(f"Template details for {page_type} pages\n")
            f.write("=" * 50 + "\n")
            f.write(f"""(1). Modern {page_type} template with gradient background and clean design.
_______________________________________________________________________________________
(2). Responsive {page_type} layout with mobile-first approach and smooth animations.
_______________________________________________________________________________________
(3). Dark mode {page_type} with cyberpunk theme and neon accents.
_______________________________________________________________________________________
""")

# ---------------------- Routes ----------------------
@app.route('/')
def home():
    if 'email' in session:
        return redirect(url_for('index'))
    return redirect('/signup')

@app.route('/index')
def index():
    if 'email' not in session:
        return redirect('/signin')
    user = get_user_by_email(session['email'])
    if not user:
        session.clear()
        flash('Session expired. Please sign in again.', 'warning')
        return redirect('/signin')
    return render_template("index.html", user=user, page_types=PAGE_TYPES)

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    form = SignUpForm()
    if form.validate_on_submit():
        username = form.username.data
        email = form.email.data
        password = form.password.data

        if get_user_by_email(email):
            flash('Email already registered. Please sign in instead.', 'warning')
            return redirect('/signin')

        otp = str(random.randint(100000, 999999))
        
        if send_otp_email(email, otp):
            session['otp'] = otp
            session['otp_timestamp'] = datetime.now().isoformat()
            session['email_temp'] = email
            session['username'] = username
            session['password'] = generate_password_hash(password)
            flash('OTP sent to your email. Please check your inbox.', 'info')
            return redirect('/verify_otp')
        else:
            flash('Failed to send OTP. Please check your email configuration or try again later.', 'danger')
            return redirect('/signup')
    
    return render_template('signup.html', form=form)

@app.route('/verify_otp', methods=['GET', 'POST'])
def verify_otp():
    form = OTPForm()
    
    if 'otp' not in session or 'email_temp' not in session:
        flash('Session expired. Please try signing up again.', 'warning')
        return redirect('/signup')
    
    if 'otp_timestamp' in session:
        try:
            otp_time = datetime.fromisoformat(session['otp_timestamp'])
            if datetime.now() - otp_time > timedelta(minutes=10):
                session.pop('otp', None)
                session.pop('otp_timestamp', None)
                flash('OTP expired. Please request a new one.', 'warning')
                return redirect('/signup')
        except Exception as e:
            print(f"Error checking OTP timestamp: {e}")
    
    if form.validate_on_submit():
        user_otp = form.otp.data
        session_otp = session.get('otp')
        email = session.get('email_temp')
        username = session.get('username')
        password = session.get('password')

        if user_otp == session_otp and email and password and username:
            try:
                new_user = User(email=email, username=username, password=password)
                db.session.add(new_user)
                db.session.commit()

                session['email'] = new_user.email
                session.permanent = True
                session.pop('otp', None)
                session.pop('otp_timestamp', None)
                session.pop('password', None)
                session.pop('username', None)
                session.pop('email_temp', None)

                flash('Account created successfully! Welcome to WebBuddy!', 'success')
                return redirect('/index')
            except Exception as e:
                db.session.rollback()
                print(f"Error creating user: {e}")
                traceback.print_exc()
                flash('An error occurred while creating your account. Please try again.', 'danger')
                return redirect('/signup')
        else:
            flash('Invalid OTP. Please try again.', 'danger')
    
    return render_template('verify_otp.html', form=form)

@app.route("/google_dashboard")
def google_dashboard():
    if not google.authorized:
        flash("Please authorize with Google to continue.", "warning")
        return redirect(url_for("google.login"))
    
    try:
        resp = google.get("/oauth2/v2/userinfo")
        if not resp.ok:
            flash("Failed to fetch user info from Google. Please try again.", "danger")
            return redirect("/signin")
        
        user_info = resp.json()
        email = user_info.get("email")
        name = user_info.get("name", email.split("@")[0] if email else "GoogleUser")
        
        if not email:
            flash("Could not retrieve email from Google. Please try again.", "danger")
            return redirect("/signin")
        
        user = get_user_by_email(email)
        if not user:
            user = User(
                username=name,
                email=email,
                password=generate_password_hash(str(random.randint(100000, 999999)))
            )
            db.session.add(user)
            db.session.commit()
            flash(f"Welcome to WebBuddy, {name}!", "success")
        else:
            flash(f"Welcome back, {name}!", "success")
        
        session["email"] = user.email
        session.permanent = True
        return redirect("/index")
        
    except Exception as e:
        print(f"Google OAuth error: {e}")
        traceback.print_exc()
        flash("An error occurred during Google sign-in. Please try again.", "danger")
        return redirect("/signin")

@app.route('/signin', methods=['GET', 'POST'])
def signin():
    form = SignInForm()
    if form.validate_on_submit():
        email = form.email.data
        password = form.password.data
        
        user = get_user_by_email(email)
        if user and check_password_hash(user.password, password):
            session['email'] = user.email
            session.permanent = True
            flash(f'Welcome back, {user.username}!', 'success')
            return redirect('/index')
        else:
            flash('Invalid email or password. Please try again.', 'danger')
    
    return render_template('signin.html', form=form)

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        email = form.email.data
        user = get_user_by_email(email)
        
        if not user:
            flash('No account found with that email address.', 'warning')
            return redirect('/forgot_password')
        
        otp = str(random.randint(100000, 999999))
        
        if send_otp_email(email, otp):
            session['reset_email'] = email
            session['reset_otp'] = otp
            session['reset_otp_timestamp'] = datetime.now().isoformat()
            flash('Password reset OTP sent to your email.', 'info')
            return redirect('/reset_password')
        else:
            flash('Failed to send OTP. Please try again.', 'danger')
            return redirect('/forgot_password')
    
    return render_template('forgot_password.html', form=form)

@app.route('/reset_password', methods=['GET', 'POST'])
def reset_password():
    form = ResetPasswordForm()
    
    if 'reset_otp' not in session or 'reset_email' not in session:
        flash('Session expired. Please request a new password reset.', 'warning')
        return redirect('/forgot_password')
    
    if 'reset_otp_timestamp' in session:
        try:
            otp_time = datetime.fromisoformat(session['reset_otp_timestamp'])
            if datetime.now() - otp_time > timedelta(minutes=10):
                session.pop('reset_otp', None)
                session.pop('reset_otp_timestamp', None)
                flash('OTP expired. Please request a new one.', 'warning')
                return redirect('/forgot_password')
        except Exception as e:
            print(f"Error checking reset OTP timestamp: {e}")
    
    if form.validate_on_submit():
        otp = form.otp.data
        new_password = form.new_password.data
        session_otp = session.get('reset_otp')
        email = session.get('reset_email')
        
        if otp == session_otp and email:
            try:
                user = get_user_by_email(email)
                if user:
                    user.password = generate_password_hash(new_password)
                    db.session.commit()
                    
                session.pop('reset_otp', None)
                session.pop('reset_otp_timestamp', None)
                session.pop('reset_email', None)
                
                flash('Password reset successfully! Please sign in with your new password.', 'success')
                return redirect('/signin')
            except Exception as e:
                db.session.rollback()
                print(f"Error resetting password: {e}")
                traceback.print_exc()
                flash('An error occurred. Please try again.', 'danger')
        else:
            flash('Invalid OTP. Please try again.', 'danger')
    
    return render_template('reset_password.html', form=form)

@app.route('/generate', methods=['POST'])
def generate():
    if 'email' not in session:
        flash('Please sign in to generate websites.', 'warning')
        return redirect('/signin')
    
    user = get_user_by_email(session['email'])
    if not user:
        flash('User not found. Please sign in again.', 'danger')
        return redirect('/signin')
    
    query = request.form.get('query', '').strip()
    pages_input = request.form.get('pages', '')
    
    print(f"🔍 DEBUG: Raw pages input: '{pages_input}' (type: {type(pages_input)})")
    
    if isinstance(pages_input, str):
        pages = [p.strip() for p in pages_input.split(',') if p.strip()] if pages_input else []
    else:
        pages = pages_input if isinstance(pages_input, list) else []
    
    print(f"🔍 DEBUG: Parsed pages: {pages}")
    print(f"🔍 DEBUG: Query: '{query}'")
    
    if not query:
        flash('Please enter a description to generate a website.', 'warning')
        return redirect(url_for('index'))
    
    try:
        start_time = time.time()
        generator = WebsiteGenerator()
        print(f"🔍 DEBUG: Starting generation process with pages: {pages}")
        result = generator.process_user_request(query, pages, user.id)
        elapsed = time.time() - start_time
        print(f"⏱️ Total processing time: {elapsed:.2f}s")
        print(f"📊 Generated {len(result.get('results', {}))} pages")
        
        if not result or 'results' not in result or not result['results']:
            flash('Generation failed - no results returned', 'danger')
            return redirect(url_for('index'))
        
        for page_type, page_result in result['results'].items():
            try:
                new_history = SearchHistory(
                    user_id=user.id,
                    query=query,
                    match_score=page_result.get('match_score', 0),
                    template_used=page_result.get('template_id', 1),
                    generation_type='ai_enhanced' if page_result.get('ai_enhanced') else 'template_match',
                    ai_enhanced=page_result.get('ai_enhanced', False),
                    llm_used=page_result.get('llm_used', 'none'),
                    page_type=page_type,
                    selected_pages=','.join(result['results'].keys())
                )
                db.session.add(new_history)
            except Exception as e:
                print(f"Error saving history: {e}")
                traceback.print_exc()
        
        db.session.commit()
        
        num_pages = len(result['results'])
        ai_enhanced_count = sum(1 for r in result['results'].values() if r.get('ai_enhanced'))
        
        if ai_enhanced_count > 0:
            flash(f'✨ Generated {num_pages} pages ({ai_enhanced_count} AI-Enhanced) in {elapsed:.1f}s', 'success')
        else:
            flash(f'📄 Generated {num_pages} pages successfully in {elapsed:.1f}s', 'success')
        
        result_json = json.dumps(result, default=str)
        print(f"\n✅ REDIRECTING TO DASHBOARD")
        print(f"   Results keys: {list(result.get('results', {}).keys())}")
        print(f"   Query: {query}")
        return redirect(url_for('dashboard', results=result_json, query=query))
        
    except Exception as e:
        print(f"❌ ERROR in generate route: {e}")
        traceback.print_exc()
        flash(f'An error occurred while generating your website. Please try again.', 'danger')
        return redirect(url_for('index'))

@app.route('/dashboard', methods=['GET', 'POST'])
def dashboard():
    if 'email' not in session:
        return redirect('/signin')
    
    if request.method == 'POST':
        return redirect(url_for('dashboard', **request.args))
    
    results_encoded = request.args.get('results', '')
    user_query = request.args.get('query', 'Generated website')
    
    print(f"\n🎯 DASHBOARD ROUTE CALLED")
    print(f"   Has results: {bool(results_encoded)}")
    print(f"   Query: {user_query}")
    
    try:
        if results_encoded:
            results = None
            decode_attempts = 0
            current = results_encoded
            while decode_attempts < 3:
                try:
                    results = json.loads(current)
                    break
                except Exception:
                    try:
                        current = urllib.parse.unquote_plus(current)
                        results = json.loads(current)
                        break
                    except Exception:
                        decode_attempts += 1
                        continue
            if results is None:
                print(f"   Error parsing results after {decode_attempts} attempts.")
                traceback.print_exc()
                results = {'results': {}, 'include_both_auth': True, 'primary_page': 'authentication'}
        else:
            results = {'results': {}, 'include_both_auth': True, 'primary_page': 'authentication'}
    except Exception as e:
        print(f"   Error parsing results outer: {e}")
        traceback.print_exc()
        results = {'results': {}, 'include_both_auth': True, 'primary_page': 'authentication'}
    
    user = get_user_by_email(session['email'])
    if not user:
        session.clear()
        flash('Session expired. Please sign in again.', 'warning')
        return redirect('/signin')
    
    print(f"   User: {user.username if user else 'Not found'}")
    print(f"   Rendering template with {len(results.get('results', {}))} pages\n")
    
    return render_template('dashboard.html',
                         results=results,
                         user_prompt=user_query,
                         generated_prompt=user_query,
                         page_types=PAGE_TYPES,
                         user=user)

@app.route('/template_file/<page_type>/<int:template_id>')
def serve_template_file(page_type, template_id):
    file_name = f"{template_id}.html"
    try:
        return send_from_directory(
            directory=os.path.join(TEMPLATES_BASE, page_type),
            path=file_name,
            mimetype='text/html'
        )
    except FileNotFoundError:
        fallback_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{page_type.title()} Template {template_id}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .container {{
            background: rgba(255, 255, 255, 0.95);
            padding: 40px;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            max-width: 400px;
            width: 90%;
        }}
        h1 {{
            color: #667eea;
            margin-bottom: 30px;
            text-align: center;
        }}
        p {{ color: #666; line-height: 1.6; margin-bottom: 20px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>{page_type.title()} Page</h1>
        <p>This is template {template_id} for {page_type} pages.</p>
        <p>The actual template file was not found, but this is a fallback view.</p>
    </div>
</body>
</html>"""
        return Response(fallback_html, mimetype='text/html')

@app.route('/ai_template_file/<filename>')
def serve_ai_template_file(filename):
    try:
        if not re.fullmatch(r'^ai_[0-9]+_[0-9]+_[A-Za-z0-9_]+_[0-9]{8}_[0-9]{6}\.html$', filename):
            flash('Invalid filename format', 'error')
            return redirect(url_for('index'))
        return send_from_directory(
            directory=AI_GENERATED_PATH,
            path=filename,
            mimetype='text/html'
        )
    except FileNotFoundError:
        flash('AI template not found', 'error')
        return redirect(url_for('index'))
    except Exception as e:
        print(f"Error serving AI template: {e}")
        traceback.print_exc()
        flash('Error loading template', 'error')
        return redirect(url_for('index'))

@app.route('/ai_templates')
def list_ai_templates():
    if 'email' not in session:
        return redirect('/signin')
    user = get_user_by_email(session['email'])
    if not user:
        session.clear()
        return redirect('/signin')
    try:
        user_templates = []
        for filename in os.listdir(AI_GENERATED_PATH):
            if filename.startswith(f'ai_{user.id}_'):
                fp = os.path.join(AI_GENERATED_PATH, filename)
                user_templates.append({
                    'filename': filename,
                    'created_time': os.path.getctime(fp),
                    'size': os.path.getsize(fp)
                })
        user_templates.sort(key=lambda x: x['created_time'], reverse=True)
        return render_template('ai_templates.html',
                             templates=user_templates,
                             user=user)
    except Exception as e:
        print(f"Error listing AI templates: {e}")
        traceback.print_exc()
        flash('Error loading templates', 'error')
        return redirect(url_for('index'))

@app.route('/edit_profile', methods=['GET', 'POST'])
def edit_profile():
    if 'email' not in session:
        return '<p>Unauthorized</p>', 401
    user = get_user_by_email(session['email'])
    if not user:
        return '<p>User not found</p>', 404
    form = EditProfileForm()
    if form.validate_on_submit():
        try:
            user.username = form.username.data
            if form.new_password.data:
                user.password = generate_password_hash(form.new_password.data)
            if form.profile_image.data:
                file = form.profile_image.data
                if file and allowed_file(file.filename):
                    extension = file.filename.rsplit('.', 1)[1].lower()
                    unique_filename = f'{user.id}_{secure_filename(user.username)}_{datetime.now().strftime("%Y%m%d%H%M%S")}.{extension}'
                    file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
                    try:
                        file.save(file_path)
                        user.profile_image = unique_filename
                    except Exception as e:
                        print(f"Profile image save error: {e}")
                        traceback.print_exc()
                        flash(f'Error saving profile image: {e}', 'danger')
            db.session.commit()
            flash('Profile updated successfully!', 'success')
            return redirect(url_for('index'))
        except Exception as e:
            db.session.rollback()
            print(f"Error updating profile: {e}")
            traceback.print_exc()
            flash('Error updating profile. Please try again.', 'danger')
    elif request.method == 'GET':
        form.username.data = user.username
    return render_template('edit_profile_modal.html', form=form, user=user)

@app.route('/history')
def view_history():
    if 'email' not in session:
        return redirect('/signin')
    user = get_user_by_email(session['email'])
    if not user:
        session.clear()
        return redirect('/signin')
    try:
        stmt = select(SearchHistory).filter_by(user_id=user.id).order_by(desc(SearchHistory.timestamp))
        history = db.session.scalars(stmt).all()
        return render_template('history.html', history=history, user=user)
    except Exception as e:
        print(f"Error loading history: {e}")
        traceback.print_exc()
        flash('Error loading history', 'error')
        return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out successfully.', 'info')
    return redirect('/signin')

@app.route('/clear_history', methods=['POST'])
def clear_history():
    if 'email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    user = get_user_by_email(session['email'])
    if not user:
        return jsonify({'error': 'User not found'}), 404
    try:
        SearchHistory.query.filter_by(user_id=user.id).delete()
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        print(f"Error clearing history: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ---------------------- Template Creation Script ----------------------
# ---------------------- Template Creation Script ----------------------
def create_default_templates():
    for page_type in PAGE_TYPES + ['header','footer']:
        page_dir = os.path.join(TEMPLATES_BASE, page_type)
        os.makedirs(page_dir, exist_ok=True)
        template1_path = os.path.join(page_dir, '1.html')
        if not os.path.exists(template1_path):
            with open(template1_path, 'w', encoding='utf-8') as f:
                f.write(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{page_type.title()} - Template 1</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .container {{
            background: rgba(255, 255, 255, 0.95);
            padding: 40px;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            max-width: 400px;
            width: 90%;
        }}
        h1 {{ color: #667eea; margin-bottom: 30px; text-align: center; }}
        input {{ width: 100%; padding: 15px; margin: 10px 0; border: 2px solid #e0e0e0; border-radius: 10px; font-size: 16px; transition: border 0.3s; }}
        input:focus {{ outline: none; border-color: #667eea; }}
        button {{ width: 100%; padding: 15px; background: linear-gradient(135deg, #667eea, #764ba2); color: white; border: none; border-radius: 10px; font-size: 16px; font-weight: 600; cursor: pointer; margin-top: 20px; transition: transform 0.2s; }}
        button:hover {{ transform: translateY(-2px); }}
        .toggle {{ text-align: center; margin-top: 20px; color: #666; }}
        .toggle a {{ color: #667eea; text-decoration: none; font-weight: 600; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Welcome to {page_type.title()}</h1>
        <form>
            <input type="email" placeholder="Email" required>
            <input type="password" placeholder="Password" required>
            <button type="submit">Sign In</button>
        </form>
        <div class="toggle">
            Don't have an account? <a href="#">Sign Up</a>
        </div>
    </div>
</body>
</html>""")
        details_path = os.path.join(page_dir, 'details.txt')
        with open(details_path, 'w', encoding='utf-8') as f:
            f.write(f"Template details for {page_type} pages\n")
            f.write("=" * 50 + "\n")
            f.write(f"""(1). Modern {page_type} template with gradient background and clean design.
_______________________________________________________________________________________
(2). Responsive {page_type} layout with mobile-first approach and smooth animations.
_______________________________________________________________________________________
(3). Dark mode {page_type} with cyberpunk theme and neon accents.
_______________________________________________________________________________________
""")

# ---------------------- Initialize & Run ----------------------
if __name__ == "__main__":
    with app.app_context():
        os.makedirs(upload_folder, exist_ok=True)
        os.makedirs(AI_GENERATED_PATH, exist_ok=True)
        os.makedirs(TEMPLATES_BASE, exist_ok=True)
        ensure_template_files_exist()
        create_default_templates()
        try:
            db.create_all()
            print("✅ Database tables created/verified")
        except Exception as e:
            print(f"❌ Database initialization error: {e}")
            traceback.print_exc()

    print("\n" + "="*60)
    print("🚀 WebBuddy AI - Multi-Domain Configuration")
    print("="*60)
    print(f"🌐 Site URL: {SITE_URL}")
    print(f"🔒 Production Mode: {IS_PRODUCTION}")
    print(f"💾 Database: {'PostgreSQL' if database_url else 'SQLite'}")
    print(f"📧 Mail: {'Configured ✅' if app.config['MAIL_USERNAME'] else 'Not Configured ⚠️'}")
    print(f"🔑 Google OAuth: {'Configured ✅' if GOOGLE_OAUTH_CLIENT_ID else 'Not Configured ⚠️'}")
    print(f"🤖 Gemini Models: {len(gemini_models)}/{len(PAGE_TYPES)+2} configured")
    print(f"📊 AI Threshold: {AI_ENHANCEMENT_THRESHOLD:.0%}")
    print(f"⚡ Rate Limits: {MAX_CALLS_PER_MINUTE}/min per API key")
    print(f"📁 Page Types: {', '.join(PAGE_TYPES)} + header/footer")
    print("\n💡 Strategy: Main page first → remaining pages concurrently with reference HTML")
    print("="*60 + "\n")

    # Run with appropriate host and port
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=not IS_PRODUCTION)