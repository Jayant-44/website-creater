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

# Ensure this matches the Console EXACTLY
REDIRECT_URI = 'http://127.0.0.1:5000/login/google/callback' 

# If testing locally, you might need this to allow insecure (http) transport:
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

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

# Google OAuth configuration
GOOGLE_OAUTH_CLIENT_ID = os.getenv('GOOGLE_OAUTH_CLIENT_ID')
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv('GOOGLE_OAUTH_CLIENT_SECRET')

if not GOOGLE_OAUTH_CLIENT_ID or not GOOGLE_OAUTH_CLIENT_SECRET:
    print("⚠️ WARNING: Google OAuth not configured")
else:
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
    GEMINI_AVAILABLE = False

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    import numpy as np
    NLP_AVAILABLE = True
except ImportError:
    NLP_AVAILABLE = False

# API Keys configuration
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

# Rate limiting settings
API_RATE_LIMITS = {k: {'calls': 0, 'reset_time': datetime.now()} for k in GEMINI_API_KEYS.keys()}
API_RATE_LIMITS['gemini_backup'] = {'calls': 0, 'reset_time': datetime.now()}
MAX_CALLS_PER_MINUTE = 15

# Initialize Gemini models
gemini_models = {}
if GEMINI_AVAILABLE:
    for page_type, api_key in GEMINI_API_KEYS.items():
        if api_key and not api_key.startswith("YOUR_"):
            try:
                genai.configure(api_key=api_key)
                gemini_models[page_type] = genai.GenerativeModel('gemini-2.0-flash')
            except Exception as e:
                print(f"Gemini {page_type} initialization failed: {e}")

# File upload configuration
base_dir = os.path.abspath(os.path.dirname(__file__))
upload_folder = os.path.join(base_dir, 'static', 'profile_pics')
app.config['UPLOAD_FOLDER'] = upload_folder
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
AI_GENERATED_PATH = os.path.join(base_dir, 'templates', 'ai_generated')
TEMPLATES_BASE = os.path.join(base_dir, 'templates')
PAGE_TYPES = ['homepage', 'authentication', 'about', 'contact', 'products', 'chatbot', 'dashboard', 'pricing']
AI_ENHANCEMENT_THRESHOLD = 0.85

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
    stmt = select(User).filter_by(email=email)
    return db.session.scalars(stmt).first()

def send_otp_email(email, otp):
    """OTP Email disabled/commented out for bypass"""
    # try:
    #     msg = Message(subject='OTP Verification', recipients=[email], body=f'Your OTP: {otp}')
    #     mail.send(msg)
    #     return True
    # except Exception as e:
    #     return False
    return True # Always return True during bypass

# ---------------------- Routes ----------------------
@app.route('/')
def home():
    if 'email' in session: return redirect(url_for('index'))
    return redirect('/signup')

@app.route('/index')
def index():
    if 'email' not in session: return redirect('/signin')
    user = get_user_by_email(session['email'])
    if not user: return redirect('/logout')
    return render_template("index.html", user=user, page_types=PAGE_TYPES)

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    form = SignUpForm()
    if form.validate_on_submit():
        username = form.username.data
        email = form.email.data
        password = form.password.data

        if get_user_by_email(email):
            flash('Email already registered.', 'warning')
            return redirect('/signin')

        # --- OTP VERIFICATION BYPASS ---
        # Instead of sending OTP, we create the user immediately
        try:
            hashed_pw = generate_password_hash(password)
            new_user = User(username=username, email=email, password=hashed_pw)
            db.session.add(new_user)
            db.session.commit()
            
            session['email'] = new_user.email
            session.permanent = True
            flash('Account created successfully (OTP bypassed)!', 'success')
            return redirect('/index')
        except Exception as e:
            db.session.rollback()
            flash(f'Error creating account: {e}', 'danger')
            return redirect('/signup')
    
    return render_template('signup.html', form=form)

@app.route('/verify_otp', methods=['GET', 'POST'])
def verify_otp():
    """Redirecting to index since OTP is bypassed"""
    return redirect(url_for('index'))

@app.route('/signin', methods=['GET', 'POST'])
def signin():
    form = SignInForm()
    if form.validate_on_submit():
        user = get_user_by_email(form.email.data)
        if user and check_password_hash(user.password, form.password.data):
            session['email'] = user.email
            session.permanent = True
            return redirect('/index')
        flash('Invalid credentials.', 'danger')
    return render_template('signin.html', form=form)

@app.route("/google_dashboard")
def google_dashboard():
    if not google.authorized: return redirect(url_for("google.login"))
    resp = google.get("/oauth2/v2/userinfo")
    if not resp.ok: return redirect("/signin")
    
    user_info = resp.json()
    email = user_info.get("email")
    user = get_user_by_email(email)
    if not user:
        user = User(username=user_info.get("name"), email=email, password=generate_password_hash("google_auth_placeholder"))
        db.session.add(user)
        db.session.commit()
    
    session["email"] = user.email
    return redirect("/index")

# --- OTHER ROUTES (Keep original logic from your file) ---
# [Note: Include your generate, dashboard, history, etc. routes here 
# exactly as they were in your previous version, as they don't involve OTP]

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/signin')

# ---------------------- Initialization ----------------------
if __name__ == "__main__":
    with app.app_context():
        os.makedirs(upload_folder, exist_ok=True)
        os.makedirs(AI_GENERATED_PATH, exist_ok=True)
        db.create_all()
    
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=not IS_PRODUCTION)