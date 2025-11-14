import os
import json
import re
from datetime import datetime, timedelta
import random
from collections import defaultdict
from flask import Flask, render_template, request, redirect, session, flash, url_for, send_from_directory, jsonify
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
import openai
from openai import OpenAI
import time

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    print("Warning: Google Gemini not installed. Run: pip install google-generativeai")
    GEMINI_AVAILABLE = False

try:
    import spacy
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    import numpy as np
    NLP_AVAILABLE = True
except ImportError: 
    print("Warning: Advanced NLP libraries not installed. Using basic matching.")
    NLP_AVAILABLE = False

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

app = Flask(__name__)
app.secret_key = 'your_secret_key'

# ---------------------- Enhanced Multi-LLM Configuration ----------------------
OPENAI_API_KEY = "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"
GEMINI_API_KEY = "AIzaSyDNuDwam1hP7ydKogKMXwyZNouzWUD5PTI"

# Rate limiting for API calls
API_RATE_LIMITS = {
    'openai': {'calls': 0, 'reset_time': datetime.now()},
    'gemini': {'calls': 0, 'reset_time': datetime.now()}
}
MAX_CALLS_PER_MINUTE = {'openai': 3, 'gemini': 15}  # Gemini is free, allow more

# Initialize clients
openai_client = None
gemini_model = None

if OPENAI_API_KEY and OPENAI_API_KEY.startswith("sk-"):
    try:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print(f"OpenAI initialization failed: {e}")

if GEMINI_AVAILABLE and GEMINI_API_KEY and not GEMINI_API_KEY.startswith("YOUR_"):
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-2.5-flash')  # Use correct version
        print("✅ Gemini 2.5 Flash initialized successfully")
    except Exception as e:
        print(f"Gemini initialization failed: {e}")

AI_ENHANCEMENT_THRESHOLD = 0.70

# ---------------------- Global Config ----------------------
base_dir = os.path.abspath(os.path.dirname(__file__))
upload_folder = os.path.join(base_dir, 'static', 'profile_pics') 
app.config['UPLOAD_FOLDER'] = upload_folder
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

# Enhanced directory structure with categories
TEMPLATES_BASE = os.path.join(base_dir, 'templates')
CATEGORIES = {
    'auth': ['login', 'signup', 'signin', 'register', 'authentication'],
    'homepage': ['home', 'landing', 'main', 'index'],
    'about': ['about', 'about-us', 'team', 'company'],
    'contact': ['contact', 'contact-us', 'get-in-touch'],
    'dashboard': ['dashboard', 'admin', 'panel', 'console'],
    'ecommerce': ['shop', 'store', 'product', 'cart', 'checkout'],
    'chatbot': ['chat', 'chatbot', 'support', 'help'],
    'footer': ['footer', 'bottom'],
    'sales': ['sales', 'pricing', 'plans'],
}

# Create organized directory structure
for category in CATEGORIES.keys():
    os.makedirs(os.path.join(TEMPLATES_BASE, category), exist_ok=True)

TEMPLATE_BASE_PATH = os.path.join(TEMPLATES_BASE, 'auth')  # Default for backward compatibility
AI_GENERATED_PATH = os.path.join(base_dir, 'templates', 'ai_generated')
os.makedirs(AI_GENERATED_PATH, exist_ok=True)

# ---------------------- Enhanced Category Detection ----------------------
class CategoryDetector:
    def __init__(self):
        self.categories = CATEGORIES
        
    def detect_category(self, prompt):
        """Detect which category the user wants"""
        prompt_lower = prompt.lower()
        
        scores = {}
        for category, keywords in self.categories.items():
            score = sum(1 for keyword in keywords if keyword in prompt_lower)
            if score > 0:
                scores[category] = score
        
        if scores:
            return max(scores, key=scores.get)
        return 'auth'  # Default
    
    def detect_website_type(self, prompt):
        """Detect website type"""
        prompt_lower = prompt.lower()
        
        types = {
                'ecommerce': ['shop', 'store', 'product', 'ecommerce', 'cart', 'category', 'product detail'],
                'saas': ['saas', 'software', 'platform', 'service', 'tech', 'analytics'],
                'portfolio': ['portfolio', 'showcase', 'work', 'freelancer', 'artist'],
                'blog': ['blog', 'article', 'post'],
                'corporate': ['corporate', 'business', 'company'],
                'admin': ['admin', 'dashboard', 'panel'],
                'nonprofit': ['nonprofit', 'charity', 'ngo', 'foundation'],
                'restaurant': ['restaurant', 'food', 'cafe', 'menu', 'culinary'],
                'farm': ['farm', 'farmer', 'agriculture', 'crops'],
                'creator': ['creator', 'onlyfans', 'fan'],
                'support': ['support', 'helpdesk', 'contact', 'faq'],
                'fitness': ['fitness', 'workout', 'tracker'],
                'social': ['social', 'feed', 'twitter', 'post'],
                'newsletter': ['newsletter', 'subscribe'],
                'app_download': ['app', 'app download', 'app store', 'google play'],
                'travel': ['travel', 'tours', 'adventure', 'destination'], # <-- ADDED NEW
                'real_estate': ['real estate', 'realty', 'property', 'home'] # <-- ADDED NEW
            }
        
        for website_type, keywords in types.items():
            if any(keyword in prompt_lower for keyword in keywords):
                return website_type
        return 'general'
    
    def should_include_both_auth(self, prompt):
        """Check if user wants only one auth page"""
        prompt_lower = prompt.lower()
        
        only_signup = any(phrase in prompt_lower for phrase in ['only signup', 'just signup', 'only register'])
        only_login = any(phrase in prompt_lower for phrase in ['only login', 'only signin', 'just login'])
        
        return not (only_signup or only_login)

# ---------------------- Enhanced Requirement Analyzer ----------------------
class RequirementAnalyzer:
    def __init__(self):
        self.nlp = None
        if NLP_AVAILABLE:
            try:
                self.nlp = spacy.load("en_core_web_sm")
            except OSError:
                print("Warning: spaCy model not found.")
        
        self.style_keywords = {
            "modern": ["modern", "sleek", "clean", "minimal", "contemporary"],
            "classic": ["classic", "traditional", "formal", "elegant"],
            "creative": ["creative", "artistic", "colorful", "unique", "vibrant"],
            "professional": ["professional", "corporate", "business"],
            "dark": ["dark", "night", "black"],
            "light": ["light", "bright", "white", "airy"],
            "cyberpunk": ["cyberpunk", "neon", "futuristic"],
            "glassmorphism": ["glass", "blur", "transparent", "frosted"],
            "minimalist": ["minimalist", "simple", "bare"],
            "timeline": ['timeline', 'history', 'chronological'],
            "friendly": ['friendly', 'bubbly', 'warm', 'inviting'],
            "map": ['map', 'location', 'local'],
            "split_screen": ['split', 'split screen'],
            "kanban": ['kanban', 'trello', 'project', 'tasks'],
            "analytics": ['analytics', 'charts', 'stats', 'revenue'],
            "serif": ['serif', 'playfair', 'elegant'],
            "sitemap": ['sitemap', 'fat footer', 'footer links'],
            "centered": ['centered', 'center aligned'],
            "typewriter": ['typewriter', 'typing animation'], # <-- ADDED NEW
            "gym": ['gym', 'fitness', 'high energy'] # <-- ADDLED NEW
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
        """Enhanced analysis"""
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
def check_rate_limit(service):
    """Improved rate limiter with better logging"""
    now = datetime.now()
    
    # Reset counter if minute has passed
    time_diff = (now - API_RATE_LIMITS[service]['reset_time']).total_seconds()
    if time_diff >= 60:
        API_RATE_LIMITS[service]['calls'] = 0
        API_RATE_LIMITS[service]['reset_time'] = now
        print(f"   ♻️ Rate limit reset for {service}")
    
    # Check limit
    current_calls = API_RATE_LIMITS[service]['calls']
    max_calls = MAX_CALLS_PER_MINUTE[service]
    
    if current_calls >= max_calls:
        wait_time = 60 - time_diff
        print(f"   ⏱️ Rate limit reached for {service}")
        print(f"   📊 {current_calls}/{max_calls} calls used")
        print(f"   ⏰ Wait {wait_time:.0f}s for reset")
        return False
    
    API_RATE_LIMITS[service]['calls'] += 1
    print(f"   📊 {service} calls: {current_calls + 1}/{max_calls}")
    return True
# ---------------------- Multi-LLM Enhancement Engine (FIXED) ----------------------
# ============== REPLACE YOUR MultiLLMEnhancementEngine CLASS WITH THIS ==============

# ---------------------- Multi-LLM Enhancement Engine (FIXED) ----------------------
# ---------------------- Multi-LLM Enhancement Engine (FIXED) ----------------------
class MultiLLMEnhancementEngine:
    """Enhanced engine with better error handling and template generation"""
    
    def __init__(self, openai_key, gemini_key=None):
        self.openai_client = openai_client
        self.gemini_model = gemini_model
        self.min_html_length = 1000
        
        # Test API keys on initialization
        self.test_api_keys()
    
    def test_api_keys(self):
        """Test if API keys are working"""
        print("\n" + "="*60)
        print("🔑 TESTING API KEYS")
        print("="*60)
        
        # Test Gemini
        if self.gemini_model:
            try:
                test_response = self.gemini_model.generate_content(
                    "Say 'API key works' in 3 words",
                    generation_config={'max_output_tokens': 10}
                )
                print("✅ Gemini API: WORKING")
            except Exception as e:
                print(f"❌ Gemini API: FAILED - {str(e)[:100]}")
                self.gemini_model = None
        else:
            print("❌ Gemini API: NOT CONFIGURED")
        
        # Test OpenAI
        if self.openai_client:
            try:
                test_response = self.openai_client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[{"role": "user", "content": "Say 'API key works' in 3 words"}],
                    max_tokens=10
                )
                print("✅ OpenAI API: WORKING")
            except Exception as e:
                error_msg = str(e)
                if "quota" in error_msg.lower() or "429" in error_msg:
                    print("❌ OpenAI API: NO CREDITS - Key exhausted")
                else:
                    print(f"❌ OpenAI API: FAILED - {error_msg[:100]}")
                self.openai_client = None
        else:
            print("❌ OpenAI API: NOT CONFIGURED")
        
        print("="*60 + "\n")
    
    def generate_template_from_scratch(self, user_prompt, requirements):
        """Generate a beautiful template from scratch when AI fails"""
        print("🎨 Generating template from scratch based on requirements...")
        
        style = requirements.get('style_preference', 'modern')
        features = requirements.get('features', [])
        
        # Color schemes based on style
        color_schemes = {
            'cyberpunk': {
                'primary': '#00ff9f',
                'secondary': '#7b2cbf',
                'bg': 'linear-gradient(135deg, #0d0221 0%, #1a0b2e 100%)',
                'text': '#ffffff',
                'accent': '#ff006e'
            },
            'dark': {
                'primary': '#667eea',
                'secondary': '#764ba2',
                'bg': 'linear-gradient(135deg, #1a1a2e 0%, #16213e 100%)',
                'text': '#ffffff',
                'accent': '#ff6b6b'
            },
            'modern': {
                'primary': '#4f46e5',
                'secondary': '#06b6d4',
                'bg': 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
                'text': '#ffffff',
                'accent': '#ec4899'
            },
            'minimalist': {
                'primary': '#000000',
                'secondary': '#6b7280',
                'bg': '#ffffff',
                'text': '#000000',
                'accent': '#3b82f6'
            }
        }
        
        colors = color_schemes.get(style, color_schemes['modern'])
        
        # Check if it's a farmer's website
        is_farmer = 'farmer' in user_prompt.lower() or 'farm' in user_prompt.lower() or 'agriculture' in user_prompt.lower()
        
        if is_farmer:
            colors = {
                'primary': '#2d5016',
                'secondary': '#6b8e23',
                'bg': 'linear-gradient(135deg, #f5f3e7 0%, #e8f5e9 100%)',
                'text': '#2d3e1f',
                'accent': '#ff9800'
            }
        
        # Determine if social login is needed
        has_social = 'social_login' in features or 'google' in user_prompt.lower() or 'facebook' in user_prompt.lower() or 'social' in user_prompt.lower()
        
        # Build conditional CSS parts first
        farmer_decorations = ""
        if is_farmer:
            farmer_decorations = """
        /* Farmer-specific decorations */
        body::before { 
            content: ''; 
            position: absolute; 
            top: -50px; 
            right: -50px; 
            width: 200px; 
            height: 200px; 
            background: url('data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"%3E%3Ctext y="50" font-size="60"%3E🌾%3C/text%3E%3C/svg%3E'); 
            opacity: 0.3; 
            animation: float 6s ease-in-out infinite; 
        }
        body::after { 
            content: ''; 
            position: absolute; 
            bottom: -50px; 
            left: -50px; 
            width: 200px; 
            height: 200px; 
            background: url('data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"%3E%3Ctext y="50" font-size="60"%3E🚜%3C/text%3E%3C/svg%3E'); 
            opacity: 0.3; 
            animation: float 8s ease-in-out infinite; 
        }"""
        
        cyberpunk_effects = ""
        if style == 'cyberpunk':
            cyberpunk_effects = """
        /* Cyberpunk effects */
        body::before { 
            content: ''; 
            position: fixed; 
            top: 0; 
            left: 0; 
            width: 100%; 
            height: 100%; 
            background: repeating-linear-gradient(0deg, rgba(0, 255, 159, 0.03) 0px, transparent 1px, transparent 2px, rgba(0, 255, 159, 0.03) 3px); 
            pointer-events: none; 
            z-index: 1; 
        }"""
        
        social_login_html = ""
        if has_social:
            social_login_html = """
        <div class="divider">
            <span>OR</span>
        </div>

        <div class="social-login">
            <button type="button" class="social-btn" onclick="alert('Google login')">
                <i class="fab fa-google"></i> Google
            </button>
            <button type="button" class="social-btn" onclick="alert('Facebook login')">
                <i class="fab fa-facebook-f"></i> Facebook
            </button>
        </div>"""
        
        # Generate HTML
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sign In - {style.title()} Style</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: 'Inter', sans-serif;
            background: {colors['bg']};
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
            position: relative;
            overflow: hidden;
        }}

        {farmer_decorations}

        @keyframes float {{
            0%, 100% {{ transform: translateY(0px) rotate(0deg); }}
            50% {{ transform: translateY(-20px) rotate(5deg); }}
        }}

        {cyberpunk_effects}

        .container {{
            background: {"rgba(26, 11, 46, 0.85)" if style == 'cyberpunk' else "rgba(255, 255, 255, 0.95)" if not is_farmer else "rgba(255, 255, 255, 0.98)"};
            backdrop-filter: blur(10px);
            padding: 50px 40px;
            border-radius: 20px;
            box-shadow: {"0 0 40px rgba(0, 255, 159, 0.3)" if style == 'cyberpunk' else "0 20px 60px rgba(0, 0, 0, 0.2)"};
            max-width: 450px;
            width: 100%;
            position: relative;
            z-index: 2;
            {"border: 2px solid rgba(0, 255, 159, 0.3);" if style == 'cyberpunk' else ""}
            animation: slideUp 0.5s ease-out;
        }}

        @keyframes slideUp {{
            from {{
                opacity: 0;
                transform: translateY(30px);
            }}
            to {{
                opacity: 1;
                transform: translateY(0);
            }}
        }}

        .logo {{
            text-align: center;
            margin-bottom: 30px;
        }}

        .logo i {{
            font-size: 3rem;
            color: {colors['primary']};
            {"text-shadow: 0 0 20px rgba(0, 255, 159, 0.5);" if style == 'cyberpunk' else ""}
        }}

        h1 {{
            color: {colors['text']};
            text-align: center;
            margin-bottom: 10px;
            font-size: 2rem;
            {"text-shadow: 0 0 10px rgba(0, 255, 159, 0.5);" if style == 'cyberpunk' else ""}
        }}

        .subtitle {{
            color: {"rgba(255, 255, 255, 0.7)" if style == 'cyberpunk' else colors['text']};
            text-align: center;
            margin-bottom: 30px;
            font-size: 0.95rem;
        }}

        .form-group {{
            margin-bottom: 20px;
        }}

        label {{
            display: block;
            color: {colors['text']};
            margin-bottom: 8px;
            font-weight: 500;
            font-size: 0.9rem;
        }}

        .input-wrapper {{
            position: relative;
        }}

        .input-wrapper i {{
            position: absolute;
            left: 15px;
            top: 50%;
            transform: translateY(-50%);
            color: {colors['secondary']};
        }}

        input {{
            width: 100%;
            padding: 15px 15px 15px 45px;
            border: 2px solid {"rgba(0, 255, 159, 0.3)" if style == 'cyberpunk' else "#e0e0e0"};
            border-radius: 10px;
            font-size: 16px;
            transition: all 0.3s;
            background: {"rgba(13, 2, 33, 0.5)" if style == 'cyberpunk' else "white"};
            color: {colors['text']};
        }}

        input:focus {{
            outline: none;
            border-color: {colors['primary']};
            {"box-shadow: 0 0 15px rgba(0, 255, 159, 0.3);" if style == 'cyberpunk' else "box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.1);"}
        }}

        .forgot-password {{
            text-align: right;
            margin-bottom: 20px;
        }}

        .forgot-password a {{
            color: {colors['primary']};
            text-decoration: none;
            font-size: 0.9rem;
            {"text-shadow: 0 0 5px rgba(0, 255, 159, 0.5);" if style == 'cyberpunk' else ""}
        }}

        .forgot-password a:hover {{
            text-decoration: underline;
        }}

        .btn-primary {{
            width: 100%;
            padding: 15px;
            background: {"linear-gradient(135deg, #00ff9f, #7b2cbf)" if style == 'cyberpunk' else f"linear-gradient(135deg, {colors['primary']}, {colors['secondary']})"};
            color: white;
            border: none;
            border-radius: 10px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            {"box-shadow: 0 0 20px rgba(0, 255, 159, 0.3);" if style == 'cyberpunk' else ""}
        }}

        .btn-primary:hover {{
            transform: translateY(-2px);
            {"box-shadow: 0 0 30px rgba(0, 255, 159, 0.5);" if style == 'cyberpunk' else f"box-shadow: 0 5px 15px {colors['primary']}40;"}
        }}

        .divider {{
            display: flex;
            align-items: center;
            text-align: center;
            margin: 25px 0;
            color: {colors['text']};
        }}

        .divider::before,
        .divider::after {{
            content: '';
            flex: 1;
            border-bottom: 1px solid {"rgba(0, 255, 159, 0.3)" if style == 'cyberpunk' else "#e0e0e0"};
        }}

        .divider span {{
            padding: 0 10px;
            font-size: 0.9rem;
        }}

        .social-login {{
            display: flex;
            gap: 15px;
            margin-bottom: 20px;
        }}

        .social-btn {{
            flex: 1;
            padding: 12px;
            border: 2px solid {"rgba(0, 255, 159, 0.3)" if style == 'cyberpunk' else "#e0e0e0"};
            border-radius: 10px;
            background: {"rgba(13, 2, 33, 0.3)" if style == 'cyberpunk' else "white"};
            cursor: pointer;
            transition: all 0.3s;
            font-weight: 600;
            color: {colors['text']};
        }}

        .social-btn:hover {{
            border-color: {colors['primary']};
            {"box-shadow: 0 0 15px rgba(0, 255, 159, 0.2);" if style == 'cyberpunk' else ""}
        }}

        .social-btn i {{
            margin-right: 8px;
        }}

        .signup-link {{
            text-align: center;
            margin-top: 25px;
            color: {colors['text']};
        }}

        .signup-link a {{
            color: {colors['primary']};
            text-decoration: none;
            font-weight: 600;
            {"text-shadow: 0 0 5px rgba(0, 255, 159, 0.5);" if style == 'cyberpunk' else ""}
        }}

        .signup-link a:hover {{
            text-decoration: underline;
        }}

        @media (max-width: 480px) {{
            .container {{
                padding: 40px 25px;
            }}

            h1 {{
                font-size: 1.5rem;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="logo">
            <i class="fas {"fa-tractor" if is_farmer else "fa-robot" if style == 'cyberpunk' else "fa-sign-in-alt"}"></i>
        </div>
        
        <h1>Welcome Back{" Farmer" if is_farmer else ""}</h1>
        <p class="subtitle">{"Sign in to manage your farm" if is_farmer else "Enter your credentials to continue"}</p>

        <form id="loginForm">
            <div class="form-group">
                <label for="email">Email Address</label>
                <div class="input-wrapper">
                    <i class="fas fa-envelope"></i>
                    <input type="email" id="email" name="email" placeholder="Enter your email" required>
                </div>
            </div>

            <div class="form-group">
                <label for="password">Password</label>
                <div class="input-wrapper">
                    <i class="fas fa-lock"></i>
                    <input type="password" id="password" name="password" placeholder="Enter your password" required>
                </div>
            </div>

            <div class="forgot-password">
                <a href="#forgot">Forgot Password?</a>
            </div>

            <button type="submit" class="btn-primary">
                <i class="fas fa-sign-in-alt"></i> Sign In
            </button>
        </form>

        {social_login_html}

        <div class="signup-link">
            Don't have an account? <a href="#signup">Sign Up</a>
        </div>
    </div>

    <script>
        document.getElementById('loginForm').addEventListener('submit', function(e) {{
            e.preventDefault();
            alert('Login functionality would be implemented here!');
        }});
    </script>
</body>
</html>"""
        
        return html
    
    def try_gemini(self, prompt, max_retries=2):
        """Enhanced Gemini with better error reporting"""
        if not self.gemini_model:
            print("   ⚠️ Gemini model not available")
            return None, None
        
        if not check_rate_limit('gemini'):
            return None, None
        
        for attempt in range(max_retries):
            try:
                print(f"   🟢 Gemini attempt {attempt + 1}/{max_retries}...")
                
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
                
                response = self.gemini_model.generate_content(
                    prompt,
                    generation_config=generation_config,
                    safety_settings=safety_settings
                )
                
                # Check if response was blocked
                if hasattr(response, 'prompt_feedback') and response.prompt_feedback.block_reason:
                    print(f"   ⚠️ Gemini blocked: {response.prompt_feedback.block_reason}")
                    continue
                
                if not response.text:
                    print(f"   ⚠️ Gemini returned empty response")
                    continue
                
                enhanced_html = response.text.strip()
                enhanced_html = self.clean_html_response(enhanced_html)
                
                is_valid, error_msg = self.validate_html_completeness(enhanced_html)
                
                # Use auto-repaired HTML if available
                if hasattr(self, '_last_repaired_html'):
                    enhanced_html = self._last_repaired_html
                    delattr(self, '_last_repaired_html')
                
                if not is_valid:
                    print(f"   ⚠️ Validation failed: {error_msg}")
                    if attempt < max_retries - 1:
                        print(f"   🔄 Retrying...")
                        time.sleep(1)
                        continue
                    return None, None
                
                print(f"   ✅ Gemini Success! Generated {len(enhanced_html)} characters")
                return enhanced_html, "gemini"
                
            except Exception as e:
                error_str = str(e)
                print(f"   ❌ Gemini error (attempt {attempt + 1}): {error_str[:200]}")
                
                if "quota" in error_str.lower() or "resource_exhausted" in error_str.lower():
                    print(f"   ⚠️ Gemini quota exceeded")
                    return None, None
                
                if attempt < max_retries - 1:
                    print(f"   🔄 Retrying in 2 seconds...")
                    time.sleep(2)
                    continue
                    
        return None, None
    
    def try_openai(self, prompt, max_retries=2):
        """Try to enhance using OpenAI API"""
        if not self.openai_client:
            print("   ⚠️ OpenAI client not available")
            return None, None
        
        if not check_rate_limit('openai'):
            return None, None
        
        for attempt in range(max_retries):
            try:
                print(f"   🔵 OpenAI attempt {attempt + 1}/{max_retries}...")
                
                response = self.openai_client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {
                            "role": "system",
                            "content": "You are an expert web developer. Generate complete, valid HTML code only. Start with <!DOCTYPE html> and end with </html>"
                        },
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=8000,
                    temperature=0.7
                )
                
                enhanced_html = response.choices[0].message.content.strip()
                enhanced_html = self.clean_html_response(enhanced_html)
                
                is_valid, error_msg = self.validate_html_completeness(enhanced_html)
                
                # Use auto-repaired HTML if available
                if hasattr(self, '_last_repaired_html'):
                    enhanced_html = self._last_repaired_html
                    delattr(self, '_last_repaired_html')
                
                if not is_valid:
                    print(f"   ⚠️ Validation failed: {error_msg}")
                    if attempt < max_retries - 1:
                        print(f"   🔄 Retrying...")
                        time.sleep(1)
                        continue
                    return None, None
                
                print(f"   ✅ OpenAI Success! Generated {len(enhanced_html)} characters")
                return enhanced_html, "openai"
                
            except Exception as e:
                error_str = str(e)
                print(f"   ❌ OpenAI error (attempt {attempt + 1}): {error_str[:200]}")
                
                if "quota" in error_str.lower() or "rate_limit" in error_str.lower():
                    print(f"   ⚠️ OpenAI quota/rate limit exceeded")
                    return None, None
                
                if attempt < max_retries - 1:
                    print(f"   🔄 Retrying in 2 seconds...")
                    time.sleep(2)
                    continue
        
        return None, None
    
    def clean_html_response(self, html_content):
        """Clean HTML response"""
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
        """Validate HTML completeness with auto-repair"""
        if not html_content or len(html_content) < self.min_html_length:
            return False, "HTML too short"
        
        html_lower = html_content.lower()
        
        # Check for essential opening tags
        required_openings = [
            ('<!DOCTYPE', 'Missing DOCTYPE'),
            ('<html', 'Missing <html> tag'),
            ('<head', 'Missing <head> tag'),
            ('<body', 'Missing <body> tag'),
        ]
        
        for element, error_msg in required_openings:
            if element.lower() not in html_lower:
                return False, error_msg
        
        # Try to auto-repair missing closing tags
        repairs = []
        if '</head>' not in html_lower:
            print("   🔧 Auto-fixing: Missing </head> tag")
            repairs.append('</head>')
        
        if '</body>' not in html_lower:
            print("   🔧 Auto-fixing: Missing </body> tag")
            repairs.append('</body>')
        
        if '</html>' not in html_lower:
            print("   🔧 Auto-fixing: Missing </html> tag")
            repairs.append('</html>')
        
        # If repairs needed, attempt auto-fix
        if repairs:
            try:
                # Insert missing closing tags before </html>
                if '</html>' in html_content:
                    html_content = html_content.replace('</html>', ''.join(repairs) + '\n</html>')
                else:
                    html_content = html_content + '\n' + '\n'.join(repairs)
                
                # Verify repair was successful
                html_lower = html_content.lower()
                if '</head>' not in html_lower or '</body>' not in html_lower or '</html>' not in html_lower:
                    return False, "Auto-repair failed"
                
                print("   ✅ Auto-repair successful")
                # Store repaired content back
                self._last_repaired_html = html_content
                return True, "Valid (Auto-repaired)"
            except Exception as e:
                print(f"   ❌ Auto-repair failed: {e}")
                return False, f"Incomplete and cannot repair: {e}"
        
        return True, "Valid"
    
    def create_enhanced_prompt(self, template_html, user_prompt, gaps, requirements):
        """Create enhancement prompt"""
        modifications = []
        
        if gaps.get('style_differences'):
            style_req = gaps['style_differences'][0]['requested']
            modifications.append(f"Change the entire visual theme to {style_req} style")
        
        if gaps.get('color_mismatches'):
            colors = ', '.join(gaps['color_mismatches'])
            modifications.append(f"Replace ALL colors with {colors} color scheme")
        
        if gaps.get('missing_features'):
            features = ', '.join(gaps['missing_features'])
            modifications.append(f"Add these features: {features}")
        
        modifications_text = '\n'.join([f"  {i+1}. {mod}" for i, mod in enumerate(modifications)])
        
        prompt = f"""Transform this HTML template to match the user's request.

USER REQUEST: "{user_prompt}"

CURRENT TEMPLATE:
{template_html[:2000]}... [truncated for brevity]

TRANSFORMATIONS NEEDED:
{modifications_text}

REQUIREMENTS:
- Return COMPLETE HTML from <!DOCTYPE html> to </html>
- Make SIGNIFICANT visual changes
- NO markdown formatting
- Start with: <!DOCTYPE html>
- End with: </html>

Generate the complete transformed HTML:"""
        
        return prompt
    
    def analyze_gap(self, user_requirements, template_metadata):
        """Analyze requirements gap"""
        gaps = {
            'missing_features': [],
            'style_differences': [],
            'color_mismatches': [],
        }
        
        user_features = set(user_requirements.get('features', []))
        template_features = set(template_metadata.get('supported_features', []))
        gaps['missing_features'] = list(user_features - template_features)
        
        user_style = user_requirements.get('style_preference')
        template_style = template_metadata.get('primary_theme')
        if user_style and template_style and user_style != template_style:
            gaps['style_differences'].append({
                'requested': user_style,
                'current': template_style
            })
        
        if user_requirements.get('color_preferences'):
            gaps['color_mismatches'] = user_requirements.get('color_preferences')
        
        return gaps
    
    def enhance_template(self, template_html, user_prompt, requirements, template_metadata):
        """Main enhancement pipeline with validation"""
        try:
            print(f"\n{'='*70}")
            print("🤖 STARTING AI ENHANCEMENT PIPELINE")
            print(f"{'='*70}")
            print(f"📝 User Prompt: {user_prompt[:100]}...")
            print(f"📄 Template Length: {len(template_html)} characters")
            
            # Analyze gaps
            gaps = self.analyze_gap(requirements, template_metadata)
            print(f"🔍 Gaps Found: {len(gaps['missing_features'])} features, "
                  f"{len(gaps['style_differences'])} style diffs, "
                  f"{len(gaps['color_mismatches'])} color mismatches")
            
            # Create prompt
            enhancement_prompt = self.create_enhanced_prompt(
                template_html, user_prompt, gaps, requirements
            )
            
            print(f"📋 Prompt Length: {len(enhancement_prompt)} characters")
            
            # Try Gemini first (free)
            print("\n🎯 ATTEMPTING GEMINI...")
            enhanced_html, llm_used = self.try_gemini(enhancement_prompt)
            
            # Fallback to OpenAI
            if enhanced_html is None:
                print("\n🎯 GEMINI FAILED - ATTEMPTING OPENAI...")
                enhanced_html, llm_used = self.try_openai(enhancement_prompt)
            
            # Final fallback: generate from scratch
            if enhanced_html is None:
                print("\n💡 ALL AI ATTEMPTS FAILED - GENERATING FROM SCRATCH")
                enhanced_html = self.generate_template_from_scratch(user_prompt, requirements)
                llm_used = "fallback_generator"
            
            # Double-check completeness
            is_valid, msg = self.validate_html_completeness(enhanced_html)
            if not is_valid:
                print(f"\n❌ FINAL VALIDATION FAILED: {msg}")
                print("   Using original template instead")
                return template_html, False, "validation_failed"
            
            print(f"\n{'='*70}")
            print(f"✅ ENHANCEMENT SUCCESSFUL")
            print(f"{'='*70}")
            print(f"🤖 LLM Used: {llm_used.upper()}")
            print(f"📏 Original: {len(template_html)} chars → Enhanced: {len(enhanced_html)} chars")
            print(f"📊 Size Change: {((len(enhanced_html) - len(template_html)) / len(template_html) * 100):+.1f}%")
            print(f"{'='*70}\n")
            
            return enhanced_html, True, llm_used
            
        except Exception as e:
            print(f"\n❌ CRITICAL ERROR IN ENHANCEMENT ENGINE: {str(e)}")
            import traceback
            traceback.print_exc()
            return template_html, False, "error"
    
    def enhance_template(self, template_html, user_prompt, requirements, template_metadata):
        """Main enhancement pipeline with validation"""
        try:
            print(f"\n{'='*70}")
            print("🤖 STARTING AI ENHANCEMENT PIPELINE")
            print(f"{'='*70}")
            print(f"📝 User Prompt: {user_prompt[:100]}...")
            print(f"📄 Template Length: {len(template_html)} characters")
            
            # Analyze gaps
            gaps = self.analyze_gap(requirements, template_metadata)
            print(f"🔍 Gaps Found: {len(gaps['missing_features'])} features, "
                  f"{len(gaps['style_differences'])} style diffs, "
                  f"{len(gaps['color_mismatches'])} color mismatches")
            
            # Create prompt
            enhancement_prompt = self.create_enhanced_prompt(
                template_html, user_prompt, gaps, requirements
            )
            
            print(f"📋 Prompt Length: {len(enhancement_prompt)} characters")
            
            # Try Gemini first (free)
            print("\n🎯 ATTEMPTING GEMINI...")
            enhanced_html, llm_used = self.try_gemini(enhancement_prompt)
            
            # Fallback to OpenAI
            if enhanced_html is None:
                print("\n🎯 GEMINI FAILED - ATTEMPTING OPENAI...")
                enhanced_html, llm_used = self.try_openai(enhancement_prompt)
            
            # Final validation
            if enhanced_html is None:
                print("\n❌ ALL AI ATTEMPTS FAILED - USING ORIGINAL TEMPLATE")
                return template_html, False, "none"
            
            # Double-check completeness
            is_valid, msg = self.validate_html_completeness(enhanced_html)
            if not is_valid:
                print(f"\n❌ FINAL VALIDATION FAILED: {msg}")
                print("   Using original template instead")
                return template_html, False, "validation_failed"
            
            print(f"\n{'='*70}")
            print(f"✅ ENHANCEMENT SUCCESSFUL")
            print(f"{'='*70}")
            print(f"🤖 LLM Used: {llm_used.upper()}")
            print(f"📏 Original: {len(template_html)} chars → Enhanced: {len(enhanced_html)} chars")
            print(f"📊 Size Change: {((len(enhanced_html) - len(template_html)) / len(template_html) * 100):+.1f}%")
            print(f"{'='*70}\n")
            
            return enhanced_html, True, llm_used
            
        except Exception as e:
            print(f"\n❌ CRITICAL ERROR IN ENHANCEMENT ENGINE: {str(e)}")
            import traceback
            traceback.print_exc()
            return template_html, False, "error"


# ---------------------- Template Matching Engine ----------------------
class EnhancedTemplateMatchingEngine:
    def __init__(self):
        self.templates_metadata = self.load_enhanced_templates_metadata()
        
    def load_enhanced_templates_metadata(self):
        """Load template metadata"""
        details_path = os.path.join(TEMPLATE_BASE_PATH, 'details.txt')
        templates = {}
        
        try:
            with open(details_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            
            sections = content.split('_______________________________________________________________________________________')
            
            for section in sections:
                section = section.strip()
                if section.startswith('(') and section.find(').') != -1:
                    try:
                        index_end = section.find(').')
                        template_id = int(section[1:index_end].strip())
                        prompt_text = section[index_end + 2:].strip()
                        
                        templates[template_id] = {
                            'description': prompt_text,
                            'analyzed_requirements': self.analyze_template_features(prompt_text),
                            'primary_theme': self.extract_primary_theme(prompt_text),
                            'supported_features': self.extract_supported_features(prompt_text)
                        }
                    except ValueError:
                        continue
                        
        except FileNotFoundError:
            print(f"Warning: {details_path} not found.")
            templates = {
                1: {
                    'description': "Modern login/signup form",
                    'analyzed_requirements': {'style': 'modern'},
                    'primary_theme': 'modern',
                    'supported_features': ['login', 'signup']
                }
            }
        
        return templates
    
    def analyze_template_features(self, description):
        analyzer = RequirementAnalyzer()
        return analyzer.analyze_prompt(description)
    
    def extract_primary_theme(self, description):
        desc_lower = description.lower()
        themes = {
            "dark": ["dark", "night"],
            "light": ["light", "bright"],
            "cyberpunk": ["cyberpunk", "neon"],
            "minimal": ["minimal", "clean"]
        }
        
        for theme, keywords in themes.items():
            if any(keyword in desc_lower for keyword in keywords):
                return theme
        return "modern"
    
    def extract_supported_features(self, description):
        desc_lower = description.lower()
        features = []
        
        feature_map = {
            "social_login": ["social", "google"],
            "animations": ["animated", "animation"],
            "responsive": ["responsive"],
            "toggle": ["toggle", "switch"]
        }
        
        for feature, keywords in feature_map.items():
            if any(keyword in desc_lower for keyword in keywords):
                features.append(feature)
                
        return features
    
    def find_best_match(self, requirements):
        """Find best matching template"""
        scores = []
        
        for template_id, template_data in self.templates_metadata.items():
            score = self.calculate_similarity_score(requirements, template_data)
            scores.append((template_id, template_data, score))
        
        scores.sort(key=lambda x: x[2], reverse=True)
        best_match = scores[0]
        
        if best_match[2] >= 0.7:
            return "exact_match", best_match[0], best_match[1], best_match[2]
        elif best_match[2] >= 0.4:
            return "partial_match", best_match[0], best_match[1], best_match[2]
        else:
            return "no_match", best_match[0], best_match[1], best_match[2]
    
    def calculate_similarity_score(self, requirements, template_data):
        """Calculate similarity score"""
        score = 0
        
        # Style matching
        if requirements.get('style_preference') == template_data.get('primary_theme'):
            score += 0.4
        
        # Feature matching
        req_features = set(requirements.get('features', []))
        template_features = set(template_data.get('supported_features', []))
        
        if req_features and template_features:
            overlap = len(req_features & template_features)
            union = len(req_features | template_features)
            if union > 0:
                score += 0.3 * (overlap / union)
        
        # Color matching
        if requirements.get('color_preferences'):
            score += 0.2
        
        return min(score, 1.0)

def try_openai(self, prompt):
    """
    Try to enhance using OpenAI API
    Returns: (enhanced_html, llm_name) or (None, None) if failed
    """
    if not self.openai_api_key:
        print("   ⚠️ OpenAI API key not configured")
        return None, None
    
    try:
        print("   🔄 Calling OpenAI API...")
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",  # or "gpt-4" if you have access
            messages=[
                {"role": "system", "content": "You are an expert web developer. Generate complete, valid HTML code."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=4000,
            temperature=0.7
        )
        
        enhanced_html = response.choices[0].message.content.strip()
        
        # Extract HTML if wrapped in code blocks
        if "```html" in enhanced_html:
            enhanced_html = enhanced_html.split("```html")[1].split("```")[0].strip()
        elif "```" in enhanced_html:
            enhanced_html = enhanced_html.split("```")[1].split("```")[0].strip()
        
        print("   ✅ OpenAI enhancement successful")
        return enhanced_html, "OpenAI GPT"
        
    except openai.error.RateLimitError:
        print("   ⚠️ OpenAI rate limit exceeded")
        return None, None
    except openai.error.AuthenticationError:
        print("   ⚠️ OpenAI authentication failed")
        return None, None
    except Exception as e:
        print(f"   ⚠️ OpenAI error: {str(e)}")
        return None, None
# ---------------------- Main Website Generator ----------------------
class WebsiteGenerator:
    def __init__(self):
        self.analyzer = RequirementAnalyzer()
        self.matcher = EnhancedTemplateMatchingEngine()
        self.ai_engine = MultiLLMEnhancementEngine(OPENAI_API_KEY, GEMINI_API_KEY)
        self.category_detector = CategoryDetector()
    
    def process_user_request(self, user_prompt, user_id=None):
        """Main processing pipeline"""
        
        print(f"\n{'='*60}")
        print(f"🎯 Processing: {user_prompt}")
        print(f"{'='*60}")
        
        # Step 1: Detect category and website type
        category = self.category_detector.detect_category(user_prompt)
        website_type = self.category_detector.detect_website_type(user_prompt)
        include_both = self.category_detector.should_include_both_auth(user_prompt)
        
        print(f"📂 Category: {category}")
        print(f"🌐 Website Type: {website_type}")
        print(f"🔐 Include both login/signup: {include_both}")
        
        # Step 2: Analyze requirements
        requirements = self.analyzer.analyze_prompt(user_prompt)
        print(f"🎨 Style: {requirements.get('style_preference')}")
        print(f"🎨 Colors: {requirements.get('color_preferences')}")
        
        # Step 3: Find best template
        match_type, template_id, template_data, match_score = self.matcher.find_best_match(requirements)
        print(f"📋 Best Match: Template {template_id} (Score: {match_score:.0%})")
        
        # Step 4: Decide on AI enhancement
        needs_ai = match_score < AI_ENHANCEMENT_THRESHOLD
        
        if needs_ai:
            print(f"🤖 AI Enhancement NEEDED")
            
            template_html = load_template_html(template_id)
            
            if template_html:
                enhanced_html, success, llm_used = self.ai_engine.enhance_template(
                    template_html, user_prompt, requirements, template_data
                )
                
                if success:
                    ai_filename = save_ai_generated_template(
                        enhanced_html, user_id or 0, int(datetime.now().timestamp())
                    )
                    
                    return {
                        'template_id': template_id,
                        'match_score': 0.90,
                        'requirements': requirements,
                        'action_taken': f"AI Enhanced using {llm_used.upper()}",
                        'ai_enhanced': True,
                        'ai_filename': ai_filename,
                        'original_score': match_score,
                        'llm_used': llm_used,
                        'category': category,
                        'include_both_auth': include_both
                    }
        
        return {
            'template_id': template_id,
            'match_score': match_score,
            'requirements': requirements,
            'action_taken': f"Template {template_id} ({match_score:.0%} match)",
            'ai_enhanced': False,
            'ai_filename': None,
            'original_score': match_score,
            'llm_used': 'none',
            'category': category,
            'include_both_auth': include_both
        }

# ---------------------- Helper Functions ----------------------
def load_template_html(template_id):
    """Load HTML from template file"""
    template_path = os.path.join(TEMPLATE_BASE_PATH, f"{template_id}.html")
    
    try:
        with open(template_path, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        print(f"Warning: Template {template_id}.html not found")
        return None

def save_ai_generated_template(html_content, user_id, query_id):
    """Save AI-generated template with validation"""
    
    # Validate before saving
    if not html_content or len(html_content) < 1000:
        print(f"❌ Refusing to save incomplete HTML ({len(html_content)} chars)")
        return None
    
    # Check for essential structure
    required = ['<!DOCTYPE', '<html', '<head>', '</head>', '<body', '</body>', '</html>']
    html_lower = html_content.lower()
    
    for req in required:
        if req.lower() not in html_lower:
            print(f"❌ Refusing to save - missing {req}")
            return None
    
    filename = f"ai_{user_id}_{query_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    filepath = os.path.join(AI_GENERATED_PATH, filename)
    
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        # Verify file was written correctly
        with open(filepath, 'r', encoding='utf-8') as f:
            saved_content = f.read()
        
        if len(saved_content) != len(html_content):
            print(f"⚠️ Warning: Saved file size mismatch!")
            print(f"   Expected: {len(html_content)} bytes")
            print(f"   Actual: {len(saved_content)} bytes")
        
        print(f"💾 Successfully saved: {filename} ({len(html_content)} bytes)")
        return filename
        
    except Exception as e:
        print(f"❌ Error saving template: {e}")
        return None

# ---------------------- Database Models (preserved from original) ----------------------
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'tanishqparashar2@gmail.com'
app.config['MAIL_PASSWORD'] = 'frwytcnekntgpxgd'
app.config['MAIL_DEFAULT_SENDER'] = 'tanishqparashar2@gmail.com'
mail = Mail(app)

google_bp = make_google_blueprint(
    client_id="389441035592-flg658c4n7an50d80cr7qtoh4fslims2.apps.googleusercontent.com",
    client_secret="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",
    scope=["profile", "email"],
    redirect_to="google_dashboard"
)
app.register_blueprint(google_bp, url_prefix="/login")

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

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
    category = db.Column(db.String(50), default='auth')
    user = db.relationship('User', backref=db.backref('search_history', lazy=True))

# Forms
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

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_user_by_email(email):
    stmt = select(User).filter_by(email=email)
    return db.session.scalars(stmt).first()

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
    return render_template("index.html", user=user)

@app.route('/generate', methods=['POST'])
def generate():
    if 'email' not in session:
        return redirect('/signin')

    user = get_user_by_email(session['email'])
    query = request.form.get('query', '').strip()

    if not query or not user:
        flash('Please enter a description to generate a website.', 'warning')
        return redirect(url_for('index'))

    try:
        start_time = time.time()
        generator = WebsiteGenerator()
        result = generator.process_user_request(query, user.id)
        elapsed = time.time() - start_time
        
        print(f"⏱️ Total processing time: {elapsed:.2f}s")
        
        new_history = SearchHistory(
            user_id=user.id,
            query=query,
            match_score=result['match_score'],
            template_used=result['template_id'],
            generation_type='ai_enhanced' if result['ai_enhanced'] else 'template_match',
            ai_enhanced=result['ai_enhanced'],
            llm_used=result.get('llm_used', 'none'),
            category=result.get('category', 'auth')
        )
        db.session.add(new_history)
        db.session.commit()
        
        flash(f'✨ {result["action_taken"]}', 'success')
        
        if result['ai_enhanced']:
            return redirect(url_for('dashboard',
                                   template_id=result['template_id'],
                                   ai_file=result['ai_filename'],
                                   query=query,
                                   score=result['match_score'],
                                   original_score=result['original_score'],
                                   llm_used=result['llm_used'],
                                   category=result['category']))
        else:
            return redirect(url_for('dashboard',
                                   template_id=result['template_id'],
                                   query=query,
                                   score=result['match_score'],
                                   category=result['category']))
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        flash('An error occurred. Please try again.', 'danger')
        return redirect(url_for('index'))

# Replace your /dashboard route with this fixed version:

@app.route('/dashboard', methods=['GET', 'POST'])  # Add POST support
def dashboard():
    if 'email' not in session:
        return redirect('/signin')
    
    # Debug logging
    print(f"Dashboard accessed with method: {request.method}")
    print(f"Request args: {request.args}")
    print(f"Request form: {request.form}")
    
    # Handle POST request (if dashboard has forms)
    if request.method == 'POST':
        print("⚠️ Warning: POST request to dashboard - redirecting to GET")
        # Preserve query parameters when redirecting
        return redirect(url_for('dashboard', **request.args))
    
    # GET request handling
    template_id = request.args.get('template_id', 1, type=int)
    ai_file = request.args.get('ai_file', None)
    user_query = request.args.get('query', 'Generated website')
    match_score = request.args.get('score', 0.85, type=float)
    original_score = request.args.get('original_score', None, type=float)
    llm_used = request.args.get('llm_used', 'none')
    category = request.args.get('category', 'auth')
    
    is_ai_enhanced = ai_file is not None
    
    return render_template('dashboard.html',
                         template_id=template_id,
                         user_prompt=user_query,
                         generated_prompt=user_query,
                         match_score=match_score,
                         original_score=original_score,
                         template_name=f"Template {template_id}",
                         ai_enhanced=is_ai_enhanced,
                         ai_filename=ai_file,
                         llm_used=llm_used,
                         category=category)

@app.route('/template_file/<int:template_id>')
def serve_template_file(template_id):
    file_name = f"{template_id}.html"
    
    try:
        return send_from_directory(
            directory=TEMPLATE_BASE_PATH,
            path=file_name,
            mimetype='text/html'
        )
    except FileNotFoundError:
        fallback_html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Template {template_id}</title>
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
        input {{
            width: 100%;
            padding: 15px;
            margin: 10px 0;
            border: 2px solid #e0e0e0;
            border-radius: 10px;
            font-size: 16px;
            transition: border 0.3s;
        }}
        input:focus {{
            outline: none;
            border-color: #667eea;
        }}
        button {{
            width: 100%;
            padding: 15px;
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
            border: none;
            border-radius: 10px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            margin-top: 20px;
            transition: transform 0.2s;
        }}
        button:hover {{
            transform: translateY(-2px);
        }}
        .toggle {{
            text-align: center;
            margin-top: 20px;
            color: #666;
        }}
        .toggle a {{
            color: #667eea;
            text-decoration: none;
            font-weight: 600;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Welcome</h1>
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
</html>
        """
        from flask import Response
        return Response(fallback_html, mimetype='text/html')

@app.route('/ai_template_file/<filename>')
def serve_ai_template_file(filename):
    try:
        return send_from_directory(
            directory=AI_GENERATED_PATH,
            path=filename,
            mimetype='text/html'
        )
    except FileNotFoundError:
        flash('AI template not found', 'error')
        return redirect(url_for('index'))

# Auth Routes
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    form = SignUpForm()
    if form.validate_on_submit():
        username = form.username.data
        email = form.email.data
        password = form.password.data

        if get_user_by_email(email):
            flash('Email already registered.', 'warning')
            return redirect('/signup')

        otp = str(random.randint(100000, 999999))
        
        try:
            msg = Message('OTP Verification', recipients=[email])
            msg.body = f'Your OTP is: {otp}'
            mail.send(msg)
            
            session['otp'] = otp
            session['email_temp'] = email
            session['username'] = username
            session['password'] = generate_password_hash(password)
            
            flash('OTP sent to your email.', 'info')
            return redirect('/verify_otp')
        except Exception as e:
            flash('Failed to send OTP.', 'danger')
            return redirect('/signup')
    return render_template('signup.html', form=form)

@app.route('/verify_otp', methods=['GET', 'POST'])
def verify_otp():
    form = OTPForm()
    if form.validate_on_submit():
        user_otp = form.otp.data
        session_otp = session.get('otp')
        email = session.get('email_temp')
        username = session.get('username')
        password = session.get('password')

        if user_otp == session_otp and email and password and username:
            new_user = User(email=email, username=username, password=password)
            db.session.add(new_user)
            db.session.commit()

            session['email'] = new_user.email
            session.pop('otp', None)
            session.pop('password', None)
            session.pop('username', None)
            session.pop('email_temp', None)

            flash('Account created successfully!', 'success')
            return redirect('/index')
        else:
            flash('Invalid OTP.', 'danger')
    return render_template('verify_otp.html', form=form)

@app.route("/google_dashboard")
def google_dashboard():
    if not google.authorized:
        return redirect(url_for("google.login"))

    resp = google.get("/oauth2/v2/userinfo")
    if not resp.ok:
        flash("Failed to fetch user info from Google.", "danger")
        return redirect("/signin")

    user_info = resp.json()
    email = user_info["email"]
    name = user_info.get("name", email.split("@")[0])

    user = get_user_by_email(email)
    if not user:
        user = User(
            username=name,
            email=email,
            password=generate_password_hash(str(random.randint(100000, 999999)))
        )
        db.session.add(user)
        db.session.commit()

    session["email"] = user.email
    flash(f"Welcome {name}!", "success")
    return redirect("/index")

@app.route('/signin', methods=['GET', 'POST'])
def signin():
    form = SignInForm()
    if form.validate_on_submit():
        user = get_user_by_email(form.email.data)
        if user and check_password_hash(user.password, form.password.data):
            session['email'] = user.email
            return redirect('/index')
        else:
            flash('Invalid email or password.', 'danger')
    return render_template('signin.html', form=form)

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        email = form.email.data
        user = get_user_by_email(email)
        if not user:
            flash('No account with that email.', 'warning')
            return redirect('/forgot_password')

        otp = str(random.randint(100000, 999999))
        
        try:
            msg = Message('Reset Password OTP', recipients=[email])
            msg.body = f'Your OTP is: {otp}'
            mail.send(msg)
            
            session['reset_email'] = email
            session['reset_otp'] = otp
            
            flash('OTP sent.', 'info')
            return redirect('/reset_password')
        except Exception as e:
            flash('Failed to send OTP.', 'danger')
            return redirect('/forgot_password')

    return render_template('forgot_password.html', form=form)

@app.route('/reset_password', methods=['GET', 'POST'])
def reset_password():
    form = ResetPasswordForm()
    if form.validate_on_submit():
        otp = form.otp.data
        new_password = form.new_password.data
        session_otp = session.get('reset_otp')
        email = session.get('reset_email')

        if otp == session_otp and email:
            user = get_user_by_email(email)
            if user:
                user.password = generate_password_hash(new_password)
                db.session.commit()

            session.pop('reset_otp', None)
            session.pop('reset_email', None)

            flash('Password reset successfully.', 'success')
            return redirect('/signin')
        else:
            flash('Invalid OTP.', 'danger')
    return render_template('reset_password.html', form=form)

@app.route('/edit_profile', methods=['GET', 'POST'])
def edit_profile():
    if 'email' not in session:
        return '<p>Unauthorized</p>', 401
    
    user = get_user_by_email(session['email'])
    form = EditProfileForm()
    
    if form.validate_on_submit():
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
                    flash(f'Error: {e}', 'danger')
        db.session.commit()
        flash('Profile updated!', 'success')
        return redirect(url_for('index'))
    elif request.method == 'GET':
        form.username.data = user.username
        
    return render_template('edit_profile_modal.html', form=form, user=user)

@app.route('/history')
def view_history():
    if 'email' not in session:
        return redirect('/signin')
    user = get_user_by_email(session['email'])
    stmt = select(SearchHistory).filter_by(user_id=user.id).order_by(desc(SearchHistory.timestamp))
    history = db.session.scalars(stmt).all()
    return render_template('history.html', history=history, user=user)

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.', 'info')
    return redirect('/signin')

@app.route('/clear_history', methods=['POST'])
def clear_history():
    if 'email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    user = get_user_by_email(session['email'])
    SearchHistory.query.filter_by(user_id=user.id).delete()
    db.session.commit()
    
    return jsonify({'success': True})

# Initialize
if __name__ == "__main__":
    with app.app_context():
        os.makedirs(upload_folder, exist_ok=True)
        os.makedirs(AI_GENERATED_PATH, exist_ok=True)
        for category in CATEGORIES.keys():
            os.makedirs(os.path.join(TEMPLATES_BASE, category), exist_ok=True)
        try:
            db.create_all()
        except Exception as e:
            print(f"DB init failed: {e}")
    
    print("\n" + "="*60)
    print("🚀 WebBuddy AI - Multi-LLM Enhanced System v2.0")
    print("="*60)
    print(f"🔵 OpenAI: {'✅ Ready' if openai_client else '❌ Not configured'}")
    print(f"🟢 Gemini: {'✅ Ready (Free)' if gemini_model else '❌ Not configured'}")
    print(f"📊 AI Threshold: {AI_ENHANCEMENT_THRESHOLD:.0%}")
    print(f"⚡ Rate Limits: OpenAI={MAX_CALLS_PER_MINUTE['openai']}/min, Gemini={MAX_CALLS_PER_MINUTE['gemini']}/min")
    print(f"📁 Categories: {', '.join(CATEGORIES.keys())}")
    print("\n💡 Strategy: Gemini (Free) → OpenAI (Paid) → Template Fallback")
    print("="*60 + "\n")
    
    app.run(debug=True)