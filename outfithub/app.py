"""
garments — Fashion Social App
People post outfits, rate & comment, AI finds items & best deals.
"""
import os
import uuid
import json
import re
import secrets
import hashlib
from datetime import datetime, timedelta
from PIL import Image

import torch
import torch.nn.functional as F
from torchvision import transforms, models

from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, session, abort, send_from_directory
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from authlib.integrations.flask_client import OAuth
from email_validator import validate_email, EmailNotValidError
from werkzeug.middleware.proxy_fix import ProxyFix
import pyotp
import qrcode
import io
import base64
import smtplib
from email.message import EmailMessage
import dns.resolver
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Email, To, Content
import hmac
import urllib.parse

# Load .env file
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "mp4", "mov", "avi", "webm", "mkv"}
ALLOWED_VIDEO_EXTENSIONS = {"mp4", "mov", "avi", "webm", "mkv"}
MAX_CONTENT_LENGTH = 200 * 1024 * 1024  # 200 MB

app = Flask(__name__)

# Trust Cloudflare proxy headers (X-Forwarded-Proto etc.)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

if os.environ.get("SECRET_KEY", "") == "":
    print("⚠️  WARNING: Set SECRET_KEY in .env for proper security!")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "garments-dev-secret-key-change-in-prod")

# Database: Use PostgreSQL if DATABASE_URL is set, otherwise SQLite
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL:
    # Render provides postgres:// but SQLAlchemy needs postgresql://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///garments.db"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.remember_cookie_duration = timedelta(days=30)

# ── Session: Instagram-level security ──────────────────────────
app.config["SESSION_COOKIE_NAME"] = "garments_session"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
app.config["SESSION_COOKIE_SECURE"] = True  # HTTPS only in production
app.config["SESSION_PERMANENT"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)
app.config["REMEMBER_COOKIE_NAME"] = "garments_remember"
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SECURE"] = True  # HTTPS only
app.config["REMEMBER_COOKIE_SAMESITE"] = "Strict"

# ── Google OAuth ────────────────────────────────────────────────
oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# ── Email (SendGrid SMTP) configuration ────────────────────────
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
SENDGRID_FROM = os.environ.get("SENDGRID_FROM", "noreply@garments.app")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5001")


def send_verification_code(recipient: str, code: str) -> bool:
    """Send a 6-digit verification code via SendGrid SMTP. Returns True if sent."""
    if not SENDGRID_API_KEY:
        return False

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:'Inter',Arial,sans-serif;background:#f5f5f5;margin:0;padding:24px;">
<div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,0.08);">
<div style="background:linear-gradient(135deg,#6366f1,#a855f7);padding:32px;text-align:center;">
<h1 style="color:#fff;margin:0;font-size:24px;font-weight:700;">garments</h1>
<p style="color:rgba(255,255,255,0.85);margin:8px 0 0;font-size:14px;">Fashion Social with AI Item Discovery</p>
</div>
<div style="padding:32px;text-align:center;">
<h2 style="font-size:18px;color:#1a1a2e;margin:0 0 8px;">Welcome to garments!</h2>
<p style="color:#666;font-size:14px;margin:0 0 24px;">Your verification code is:</p>
<div style="background:#f0f0ff;border:2px dashed #6366f1;border-radius:12px;padding:16px;margin:0 auto 24px;max-width:240px;">
<span style="font-size:36px;font-weight:800;letter-spacing:8px;color:#6366f1;font-family:monospace;">{code}</span>
</div>
<p style="color:#999;font-size:12px;margin:0;">This code expires in 15 minutes. If you did not sign up, please ignore this email.</p>
</div>
<div style="background:#fafafa;padding:16px 32px;text-align:center;border-top:1px solid #eee;">
<p style="color:#bbb;font-size:11px;margin:0;">garments — Fashion Social App</p>
</div>
</div>
</body>
</html>"""

    msg = EmailMessage()
    msg["Subject"] = "Your verification code — garments"
    msg["From"] = f"garments <{SENDGRID_FROM}>"
    msg["To"] = recipient
    msg.set_content(
        f"Welcome to garments!\n\n"
        f"Your verification code is: {code}\n\n"
        f"Enter this code on the verification page to activate your account.\n"
        f"This code expires in 15 minutes.\n\n"
        f"If you did not sign up, please ignore this email."
    )
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP("smtp.sendgrid.net", 587, timeout=15) as server:
            server.starttls()
            server.login("apikey", SENDGRID_API_KEY)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"[Email] Failed to send: {e}")
        return False


def send_login_code(recipient: str, code: str) -> bool:
    """Send a 6-digit login verification code via SendGrid SMTP."""
    if not SENDGRID_API_KEY:
        return False

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:'Inter',Arial,sans-serif;background:#f5f5f5;margin:0;padding:24px;">
<div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,0.08);">
<div style="background:linear-gradient(135deg,#6366f1,#a855f7);padding:32px;text-align:center;">
<h1 style="color:#fff;margin:0;font-size:24px;font-weight:700;">garments</h1>
<p style="color:rgba(255,255,255,0.85);margin:8px 0 0;font-size:14px;">Fashion Social with AI Item Discovery</p>
</div>
<div style="padding:32px;text-align:center;">
<h2 style="font-size:18px;color:#1a1a2e;margin:0 0 8px;">Login Verification</h2>
<p style="color:#666;font-size:14px;margin:0 0 24px;">Your login verification code is:</p>
<div style="background:#f0f0ff;border:2px dashed #6366f1;border-radius:12px;padding:16px;margin:0 auto 24px;max-width:240px;">
<span style="font-size:36px;font-weight:800;letter-spacing:8px;color:#6366f1;font-family:monospace;">{code}</span>
</div>
<p style="color:#999;font-size:12px;margin:0;">This code expires in 10 minutes. If you did not attempt to log in, please ignore this email.</p>
</div>
<div style="background:#fafafa;padding:16px 32px;text-align:center;border-top:1px solid #eee;">
<p style="color:#bbb;font-size:11px;margin:0;">garments — Fashion Social App</p>
</div>
</div>
</body>
</html>"""

    msg = EmailMessage()
    msg["Subject"] = "Your login code — garments"
    msg["From"] = f"garments <{SENDGRID_FROM}>"
    msg["To"] = recipient
    msg.set_content(
        f"Your garments login code is: {code}\n\n"
        f"Enter this code on the login verification page to sign in.\n"
        f"This code expires in 10 minutes.\n\n"
        f"If you did not attempt to log in, please ignore this email."
    )
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP("smtp.sendgrid.net", 587, timeout=15) as server:
            server.starttls()
            server.login("apikey", SENDGRID_API_KEY)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"[Email] Failed to send login code: {e}")
        return False


def send_password_reset_link(recipient: str, token: str) -> bool:
    """Send a password reset link via SendGrid SMTP."""
    if not SENDGRID_API_KEY:
        return False

    reset_url = f"{BASE_URL}/reset-password/{token}"

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:'Inter',Arial,sans-serif;background:#f5f5f5;margin:0;padding:24px;">
<div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,0.08);">
<div style="background:linear-gradient(135deg,#6366f1,#a855f7);padding:32px;text-align:center;">
<h1 style="color:#fff;margin:0;font-size:24px;font-weight:700;">garments</h1>
<p style="color:rgba(255,255,255,0.85);margin:8px 0 0;font-size:14px;">Fashion Social with AI Item Discovery</p>
</div>
<div style="padding:32px;text-align:center;">
<h2 style="font-size:18px;color:#1a1a2e;margin:0 0 8px;">Password Reset</h2>
<p style="color:#666;font-size:14px;margin:0 0 24px;">Click the button below to reset your password:</p>
<a href="{reset_url}" style="display:inline-block;background:linear-gradient(135deg,#6366f1,#a855f7);color:#fff;padding:14px 32px;border-radius:8px;text-decoration:none;font-size:16px;font-weight:600;margin:0 auto 24px;">Reset Password</a>
<p style="color:#999;font-size:12px;margin:16px 0 0;">Or copy this link into your browser:</p>
<p style="color:#6366f1;font-size:12px;margin:4px 0 0;word-break:break-all;">{reset_url}</p>
<p style="color:#999;font-size:12px;margin:16px 0 0;">This link expires in 1 hour. If you did not request a password reset, please ignore this email.</p>
</div>
<div style="background:#fafafa;padding:16px 32px;text-align:center;border-top:1px solid #eee;">
<p style="color:#bbb;font-size:11px;margin:0;">garments — Fashion Social App</p>
</div>
</div>
</body>
</html>"""

    msg = EmailMessage()
    msg["Subject"] = "Reset your password — garments"
    msg["From"] = f"garments <{SENDGRID_FROM}>"
    msg["To"] = recipient
    msg.set_content(
        f"Reset your garments password:\n\n"
        f"Click this link: {reset_url}\n\n"
        f"This link expires in 1 hour.\n\n"
        f"If you did not request a password reset, please ignore this email."
    )
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP("smtp.sendgrid.net", 587, timeout=15) as server:
            server.starttls()
            server.login("apikey", SENDGRID_API_KEY)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"[Email] Failed to send password reset: {e}")
        return False


def smtp_verify_email(email: str) -> bool | None:
    """
    Try to verify an email address via SMTP RCPT (connects to the recipient's
    mail server). Returns:
      - True  if the server accepted the address (likely real)
      - False if the server rejected it (definitely fake/invalid)
      - None  if verification could not be performed (inconclusive)
    """
    domain = email.split("@")[1].lower()
    # Skip verification for common large providers that accept all RCPT
    # (they don't reveal whether a specific address exists)
    SKIP_DOMAINS = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
                    "yahoo.com", "aol.com", "icloud.com", "protonmail.com", "mail.com"}
    if domain in SKIP_DOMAINS:
        return None  # inconclusive — rely on email confirmation flow instead

    try:
        # Look up MX records for the domain
        answers = dns.resolver.resolve(domain, "MX")
        mx_host = str(sorted(answers, key=lambda r: r.preference)[0].exchange)

        # Connect to the mail server and check RCPT
        with smtplib.SMTP(mx_host, 25, timeout=10) as smtp:
            smtp.helo()
            smtp.mail("noreply@garments.app")
            code, _ = smtp.rcpt(email)
            return code == 250  # 250 = accepted, anything else = rejected
    except Exception as e:
        print(f"[SMTP verify] Could not verify {email}: {e}")
        return None  # inconclusive


# ---------------------------------------------------------------------------
# Database Models
# ---------------------------------------------------------------------------

class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=True)  # Nullable for OAuth users
    oauth_provider = db.Column(db.String(50), nullable=True)   # "google" or None
    oauth_id = db.Column(db.String(256), nullable=True)        # Google sub
    bio = db.Column(db.Text, default="")
    avatar = db.Column(db.String(256), default="")
    totp_secret = db.Column(db.String(32), nullable=True)   # TOTP secret for 2FA
    totp_enabled = db.Column(db.Boolean, default=False)      # Whether 2FA is active
    email_confirmed = db.Column(db.Boolean, default=False)    # Email verified via code
    email_confirm_code = db.Column(db.String(6), nullable=True)  # 6-digit verification code
    email_code_expiry = db.Column(db.DateTime, nullable=True)     # When the code expires
    login_code = db.Column(db.String(6), nullable=True)           # 6-digit login verification code
    login_code_expiry = db.Column(db.DateTime, nullable=True)     # When login code expires
    reset_token = db.Column(db.String(128), nullable=True)        # Password reset token
    reset_token_expiry = db.Column(db.DateTime, nullable=True)    # When reset token expires
    failed_login_attempts = db.Column(db.Integer, default=0)       # Instagram: lockout after failed attempts
    locked_until = db.Column(db.DateTime, nullable=True)           # Instagram: temporary lockout
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    posts = db.relationship("Post", backref="author", lazy="dynamic", cascade="all, delete-orphan")
    ratings = db.relationship("Rating", backref="user", lazy="dynamic", cascade="all, delete-orphan")
    comments = db.relationship("Comment", backref="user", lazy="dynamic", cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if self.password_hash is None:
            return False
        return check_password_hash(self.password_hash, password)

    def is_locked(self):
        """Check if account is temporarily locked (Instagram-style)."""
        if self.locked_until and datetime.utcnow() < self.locked_until:
            return True
        # Reset if lockout expired
        if self.locked_until and datetime.utcnow() >= self.locked_until:
            self.failed_login_attempts = 0
            self.locked_until = None
        return False

    def record_failed_login(self):
        """Instagram: progressive lockout - 5, 10, 30, 60 minutes."""
        self.failed_login_attempts = (self.failed_login_attempts or 0) + 1
        attempts = self.failed_login_attempts
        if attempts >= 5:
            lockout_minutes = min(60 * (attempts // 5), 480)  # up to 8 hours
            self.locked_until = datetime.utcnow() + timedelta(minutes=lockout_minutes)
        return self.failed_login_attempts


class Post(db.Model):
    __tablename__ = "posts"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")
    image_filename = db.Column(db.String(256), nullable=False)
    image_hash = db.Column(db.String(64), nullable=True)   # SHA-256 for duplicate detection
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    is_video = db.Column(db.Boolean, default=False)
    hidden = db.Column(db.Boolean, default=False)          # Hidden after multiple reports
    report_count = db.Column(db.Integer, default=0)         # Number of reports
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def media_type(self):
        return "video" if self.is_video else "image"

    ratings = db.relationship("Rating", backref="post", lazy="dynamic", cascade="all, delete-orphan")
    comments = db.relationship("Comment", backref="post", lazy="dynamic", cascade="all, delete-orphan")
    items = db.relationship("IdentifiedItem", backref="post", lazy="dynamic", cascade="all, delete-orphan")

    @property
    def average_rating(self):
        ratings = self.ratings.all()
        if not ratings:
            return 0
        return round(sum(r.score for r in ratings) / len(ratings), 1)

    @property
    def rating_count(self):
        return self.ratings.count()


class Rating(db.Model):
    __tablename__ = "ratings"
    id = db.Column(db.Integer, primary_key=True)
    score = db.Column(db.Integer, nullable=False)  # 1-5
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey("posts.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint("user_id", "post_id", name="unique_user_post_rating"),)


class Comment(db.Model):
    __tablename__ = "comments"
    id = db.Column(db.Integer, primary_key=True)
    body = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey("posts.id"), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("comments.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    hidden = db.Column(db.Boolean, default=False)  # Hidden after multiple reports

    replies = db.relationship("Comment", backref=db.backref("parent", remote_side=[id]), lazy="select", cascade="all, delete-orphan")


class Report(db.Model):
    """User reports for posts and comments."""
    __tablename__ = "reports"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey("posts.id"), nullable=True)
    comment_id = db.Column(db.Integer, db.ForeignKey("comments.id"), nullable=True)
    reason = db.Column(db.String(100), default="")  # "illegal", "spam", "other"
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint("user_id", "post_id", name="unique_user_post_report"),
                      db.UniqueConstraint("user_id", "comment_id", name="unique_user_comment_report"),)


class IdentifiedItem(db.Model):
    """Clothing item identified by AI from an outfit post."""
    __tablename__ = "identified_items"
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey("posts.id"), nullable=False)
    label = db.Column(db.String(200), nullable=False)        # e.g. "leather jacket"
    category = db.Column(db.String(100), default="")         # e.g. "outerwear"
    confidence = db.Column(db.Float, default=0.0)
    bounding_box = db.Column(db.String(100), default="")     # JSON: [x,y,w,h]
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    deals = db.relationship("Deal", backref="item", lazy="dynamic", cascade="all, delete-orphan")


class Deal(db.Model):
    """Best deals found for an identified clothing item."""
    __tablename__ = "deals"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("identified_items.id"), nullable=False)
    website = db.Column(db.String(200), nullable=False)      # e.g. "amazon", "asos", "ebay"
    title = db.Column(db.String(300), nullable=False)
    price = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), default="USD")
    url = db.Column(db.String(500), nullable=False)
    image_url = db.Column(db.String(500), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# AI Model — Fashion item classifier
# ---------------------------------------------------------------------------

# We use a lightweight ResNet18 fine-tunable for fashion.
# For MVP we use ImageNet pretrained + filter to fashion-relevant classes.
_FASHION_MODEL = None
_FASHION_TRANSFORM = None

# ImageNet class IDs that correspond to clothing / fashion items
# We'll map a broader set of fashion-relevant synsets
FASHION_CLASSES = {
    # — tops —
    "shirt": ["shirt", "tee shirt", "jersey", "polo shirt", "blouse", "tank top"],
    "t-shirt": ["tee shirt", "t-shirt", "jersey"],
    "blouse": ["blouse", "shirt"],
    "sweater": ["sweater", "cardigan", "hoodie", "sweatshirt", "jersey"],
    "hoodie": ["hoodie", "sweatshirt"],
    "jacket": ["jacket", "windbreaker", "blazer", "suit coat", "bolo tie"],
    "coat": ["coat", "overcoat", "parka", "trench coat"],
    "vest": ["vest", "waistcoat"],
    # — bottoms —
    "jeans": ["jeans", "blue jeans", "denim"],
    "pants": ["pants", "trousers", "slacks", "sweatpants", "leggings"],
    "shorts": ["shorts", "short pants", "bermuda shorts"],
    "skirt": ["skirt", "miniskirt"],
    # — dresses & suits —
    "dress": ["dress", "gown", "evening gown", "cocktail dress"],
    "suit": ["suit", "suit of clothes"],
    # — footwear —
    "sneakers": ["sneaker", "athletic shoe", "tennis shoe", "running shoe"],
    "shoes": ["shoe", "loafer", "oxford", "boot", "pump"],
    "boots": ["boot", "cowboy boot", "hiking boot", "snow boot"],
    "sandals": ["sandal", "flip-flop"],
    "heels": ["high heel", "pump"],
    # — accessories —
    "hat": ["hat", "cap", "baseball cap", "beret", "fedora", "sombrero"],
    "cap": ["cap", "baseball cap"],
    "scarf": ["scarf", "bandana"],
    "belt": ["belt"],
    "bag": ["bag", "handbag", "backpack", "shoulder bag", "clutch", "tote bag"],
    "backpack": ["backpack", "knapsack", "rucksack"],
    "watch": ["watch", "wristwatch", "digital watch"],
    "sunglasses": ["sunglasses", "dark glasses", "shades"],
    "glasses": ["glasses", "eyeglasses", "spectacles"],
    "jewelry": ["necklace", "bracelet", "ring", "earring", "pendant"],
    "tie": ["tie", "bow tie", "necktie"],
    "gloves": ["glove", "mitten"],
}


def _load_fashion_model():
    """Load pretrained ResNet18 and map to fashion categories."""
    global _FASHION_MODEL, _FASHION_TRANSFORM
    if _FASHION_MODEL is not None:
        return _FASHION_MODEL

    _FASHION_TRANSFORM = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    model = models.resnet18(weights=models.ResNet18_Weights.IMAGENET1K_V1)
    model.eval()
    _FASHION_MODEL = model
    return model


def _load_imagenet_labels():
    """Load ImageNet class labels."""
    url = "https://raw.githubusercontent.com/anishathalye/imagenet-simple-labels/master/imagenet-simple-labels.json"
    try:
        import requests
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    # Fallback: return empty, we'll use indices
    return []


def identify_clothing_items(image_path: str) -> list[dict]:
    """
    Run AI inference on an outfit image to identify clothing items.
    Returns list of { label, category, confidence, bounding_box }.
    For MVP: classify the whole image into fashion categories.
    """
    try:
        model = _load_fashion_model()
        transform = _FASHION_TRANSFORM

        img = Image.open(image_path).convert("RGB")
        input_tensor = transform(img).unsqueeze(0)

        with torch.no_grad():
            outputs = model(input_tensor)
            probs = F.softmax(outputs, dim=1).squeeze(0)

        # Load ImageNet labels
        labels = _load_imagenet_labels()

        # Get top predictions that match fashion categories
        top_probs, top_indices = torch.topk(probs, 50)
        results = []
        seen_categories = set()

        for prob, idx in zip(top_probs.tolist(), top_indices.tolist()):
            if prob < 0.01:  # confidence threshold
                continue
            label_name = labels[idx] if idx < len(labels) else f"class_{idx}"
            label_lower = label_name.lower()

            # Check if this prediction matches any fashion class
            matched_category = None
            matched_label = None
            for category, keywords in FASHION_CLASSES.items():
                if category in seen_categories:
                    continue
                for keyword in keywords:
                    if keyword in label_lower or label_lower in keyword:
                        matched_category = category
                        matched_label = label_name
                        break
                if matched_category:
                    break

            if matched_category and matched_category not in seen_categories:
                seen_categories.add(matched_category)
                results.append({
                    "label": matched_label,
                    "category": matched_category,
                    "confidence": round(prob, 3),
                    "bounding_box": "",  # full-image for now
                })

            if len(results) >= 6:  # max items per outfit
                break

        return results

    except Exception as e:
        print(f"[AI] Error identifying items: {e}")
        return []


# ---------------------------------------------------------------------------
# Deal Search (simulated — for MVP demonstrates the concept)
# ---------------------------------------------------------------------------

DEAL_SOURCES = [
    {"name": "Amazon", "url_template": "https://www.amazon.com/s?k={query}",
     "icon": "amazon"},
    {"name": "ASOS", "url_template": "https://www.asos.com/search/?q={query}",
     "icon": "asos"},
    {"name": "eBay", "url_template": "https://www.ebay.com/sch/i.html?_nkw={query}",
     "icon": "ebay"},
    {"name": "Zalando", "url_template": "https://www.zalando.com/catalog/?q={query}",
     "icon": "zalando"},
    {"name": "AliExpress", "url_template": "https://www.aliexpress.com/wholesale?SearchText={query}",
     "icon": "aliexpress"},
]


def search_deals(item_label: str) -> list[dict]:
    """
    Search for best deals on a clothing item across multiple marketplaces.
    For MVP: returns smart search links + simulated prices.
    Uses semantic query generation.
    """
    query = item_label.replace(" ", "+")
    results = []

    # Simulated prices for demo purposes
    import random as _random
    _rng = _random.Random(hash(item_label) % (2**31))

    base_price_map = {
        "shirt": 25, "t-shirt": 15, "blouse": 35, "sweater": 45,
        "hoodie": 50, "jacket": 80, "coat": 120, "vest": 40,
        "jeans": 60, "pants": 50, "shorts": 30, "skirt": 40,
        "dress": 70, "suit": 200,
        "sneakers": 80, "shoes": 65, "boots": 100, "sandals": 35,
        "heels": 55,
        "hat": 20, "cap": 15, "scarf": 25, "belt": 30,
        "bag": 60, "backpack": 45, "watch": 150, "sunglasses": 40,
        "glasses": 80, "jewelry": 90, "tie": 25, "gloves": 20,
    }

    # Determine base price from the item label keywords
    label_lower = item_label.lower()
    base_price = 30  # default
    for cat, price in base_price_map.items():
        if cat in label_lower:
            base_price = price
            break

    for source in DEAL_SOURCES:
        # Vary price by marketplace (+/- 30%)
        variation = _rng.uniform(0.7, 1.3)
        price = round(base_price * variation, 2)
        results.append({
            "website": source["name"],
            "title": f"{source['name']} — {item_label}",
            "price": price,
            "currency": "USD",
            "url": source["url_template"].format(query=query),
            "image_url": "",
        })

    results.sort(key=lambda x: x["price"])
    return results


# ---------------------------------------------------------------------------
# Flask Auth
# ---------------------------------------------------------------------------

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def is_video_file(filename):
    ext = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    return ext in ALLOWED_VIDEO_EXTENSIONS


# Simple in-memory rate limiter (login/register attempts)
_ratelimit_store = {}
_ratelimit_cleanup_counter = 0
def _rate_limit(key: str, max_attempts: int = 5, window: int = 300) -> bool:
    """Returns True if allowed, False if rate-limited."""
    global _ratelimit_cleanup_counter
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=window)
    # Filter out expired entries
    _ratelimit_store[key] = [t for t in _ratelimit_store.get(key, []) if t > cutoff]
    # Periodic cleanup of stale keys (every 20 calls)
    _ratelimit_cleanup_counter += 1
    if _ratelimit_cleanup_counter >= 20:
        _ratelimit_cleanup_counter = 0
        stale_keys = [k for k, v in _ratelimit_store.items() if not v]
        for k in stale_keys:
            del _ratelimit_store[k]
    if len(_ratelimit_store[key]) >= max_attempts:
        return False
    _ratelimit_store[key].append(now)
    return True


def save_upload(file) -> tuple | None:
    """Save uploaded file and return (filename, sha256_hash) or None."""
    if file and allowed_file(file.filename):
        # Verify MIME type for image files (security check)
        if file.content_type and not file.content_type.startswith(("image/", "video/")):
            return None
        ext = file.filename.rsplit(".", 1)[1].lower()
        unique_name = f"{uuid.uuid4().hex}.{ext}"
        # Ensure upload directory exists
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
        file.save(filepath)
        # Compute SHA-256 hash for duplicate detection
        sha256 = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha256.update(chunk)
        return (unique_name, sha256.hexdigest())
    return None


# ── Content moderation ──────────────────────────────────────────
# Strikes a balance: allows swearing/slurs, blocks illegal/harmful content.
# Uses leetspeak normalization to prevent bypass attempts.

def _normalize(text: str) -> str:
    """Normalize leetspeak and common obfuscations to catch bypass attempts."""
    t = text.lower()
    # Replace common leetspeak characters
    subs = {
        "0": "o", "1": "i", "2": "z", "3": "e", "4": "a", "5": "s",
        "6": "g", "7": "t", "8": "b", "9": "g",
        "@": "a", "$": "s", "!": "i", "+": "t",
        "|": "i", "<": "l", ">": "",
        "_": "", "-": "", ".": "", ",": "", "*": "",
    }
    for k, v in subs.items():
        t = t.replace(k, v)
    # Remove spaces within words (another bypass tactic)
    # This uses a word-boundary approach: join letters separated by single spaces
    t = re.sub(r"(?<=\w)\s+(?=\w)", "", t)
    return t


# — Illegal content keyword blocks (checked after normalization) —
_ILLEGAL_CONTENT_KEYWORDS = [
    # CSAM / child exploitation
    "csam", "child porn", "childporn", "child sexual", "child exploitation",
    "underage", "minor porn", "minor sexual", "loli", "lolicon",
    "preteen", "pre-teen", "young teen", "pedo", "pedophile", "paedophile",
    "cp link", "cp video", "cp photo",
    # Rape / sexual violence
    "rape", "rapist", "sexual assault", "sexual violence",
    "forced sex", "noncon", "drugged sex",
]

# — Known CSAM-related domain patterns (blocked in any URL) —
_CSAM_DOMAIN_PATTERNS = [
    r"lolia[a-z]*\.", r"cp[a-z]*\.", r"pedo[a-z]*\.",
    r"underage[a-z]*\.", r"preteen[a-z]*\.",
]

# — Doxxing / PII patterns —
_ILLEGAL_PATTERNS = re.compile(
    r"\b\d{3,}[-\s]?\d{2,}[-\s]?\d{2,}\b"                          # phone numbers
    r"|" r"\b[\w.+-]+@[\w-]+\.[\w]{2,}\b"                           # email addresses
    r"|" r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b"             # credit card numbers
    r"|" r"\b\d{2,}\s+\w+\s+\w+\s+\w+\s+\w+\b",                     # street addresses
    re.IGNORECASE,
)

# — Threat keywords —
_THREAT_KEYWORDS = [
    "kill you", "kill yourself", "i will kill", "going to kill",
    "shoot you", "shoot up", "bomb", "terrorist",
    "i will find you", "i know where you live", "dox", "doxx",
]


def _contains_url(text: str) -> bool:
    """Check if text contains a URL."""
    url_pattern = re.compile(
        r"https?://[^\s]+|www\.[^\s]+|[a-z0-9.-]+\.[a-z]{2,}(?:/[^\s]*)?",
        re.IGNORECASE
    )
    return bool(url_pattern.search(text))


def _url_contains_csam(text: str) -> bool:
    """Check if any URL in the text matches known CSAM domain patterns."""
    urls = re.findall(r"https?://[^\s]+|www\.[^\s]+", text, re.IGNORECASE)
    for url in urls:
        url_lower = url.lower()
        for pattern in _CSAM_DOMAIN_PATTERNS:
            if re.search(pattern, url_lower):
                return True
    return False


def moderate_content(text: str) -> str | None:
    """
    Check content for illegal/harmful material.
    Returns None if OK, or an error message string if blocked.

    Allows: swear words, slurs, profanity, offensive language.
    BLOCKS:
      - CSAM / child exploitation content, links, discussion
      - Rape / sexual violence content
      - Doxxing (phone numbers, addresses, emails, credit cards)
      - Direct threats of violence
      - URLs containing known CSAM domains
    """
    if not text or not text.strip():
        return None

    # Normalize to catch bypass attempts (leetspeak, obfuscation)
    normalized = _normalize(text)
    text_lower = text.lower()

    # ── Check for CSAM / child exploitation keywords ──
    for keyword in _ILLEGAL_CONTENT_KEYWORDS:
        if keyword in normalized or keyword in text_lower:
            return "Content blocked: this type of content is not permitted on garments."

    # ── Check for CSAM domains in URLs ──
    if _contains_url(text) and _url_contains_csam(text):
        return "Content blocked: this URL is not permitted."

    # ── Check for doxxing patterns ──
    if _ILLEGAL_PATTERNS.search(text):
        return "Content removed: sharing personal contact info is not allowed."

    # ── Check for direct threats ──
    for keyword in _THREAT_KEYWORDS:
        if keyword in text_lower or keyword in normalized:
            return "Content removed: threats of violence are not allowed."

    return None


# ── CSRF Protection ──────────────────────────────────────────────
def generate_csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def validate_csrf():
    token = request.form.get("csrf_token") or request.headers.get("X-CSRF-TOKEN")
    if not token or not token == session.get("csrf_token"):
        return False
    return True


def rotate_csrf_token():
    """Regenerate the CSRF token (call after login/logout)."""
    session["csrf_token"] = secrets.token_hex(32)


@app.context_processor
def inject_csrf():
    return dict(csrf_token=generate_csrf_token())


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # Remove Server header to hide version info
    if "Server" in response.headers:
        del response.headers["Server"]
    return response


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    page = request.args.get("page", 1, type=int)
    posts = Post.query.filter_by(hidden=False).order_by(Post.created_at.desc()).paginate(page=page, per_page=12, error_out=False)
    return render_template("index.html", posts=posts)


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    google_available = bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))
    if request.method == "POST":
        if not validate_csrf():
            flash("Invalid form submission.", "danger")
            return render_template("register.html", google_available=google_available)

        # Rate limit by IP to prevent mass account creation
        client_ip = request.remote_addr or "unknown"
        if not _rate_limit(f"register:{client_ip}", max_attempts=10, window=3600):
            flash("Too many registration attempts. Try again later.", "danger")
            return render_template("register.html", google_available=google_available)

        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not username or not email or not password:
            flash("All fields are required.", "danger")
            return render_template("register.html", google_available=google_available)

        if password != confirm:
            flash("Passwords do not match.", "danger")
            return render_template("register.html", google_available=google_available)

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
            return render_template("register.html", google_available=google_available)

        if not re.search(r"[A-Z]", password) or not re.search(r"[a-z]", password) or not re.search(r"[0-9]", password):
            flash("Password must contain at least one uppercase letter, one lowercase letter, and one digit.", "danger")
            return render_template("register.html", google_available=google_available)

        # Validate email format
        try:
            valid = validate_email(email)
            email = valid.normalized
        except EmailNotValidError as e:
            flash(f"Invalid email: {str(e)}", "danger")
            return render_template("register.html", google_available=google_available)

        # Verify the email exists via SMTP (skip if SendGrid not configured)
        smtp_result = smtp_verify_email(email) if SENDGRID_API_KEY else None
        if smtp_result is False:
            flash("Invalid email: this address does not appear to exist.", "danger")
            return render_template("register.html", google_available=google_available)

        if not re.match(r"^[a-zA-Z0-9_]+$", username):
            flash("Username can only contain letters, numbers, and underscores.", "danger")
            return render_template("register.html", google_available=google_available)

        # Moderate username for illegal content
        mod_error = moderate_content(username)
        if mod_error:
            flash("Username contains prohibited content.", "danger")
            return render_template("register.html", google_available=google_available)

        if User.query.filter_by(username=username).first():
            flash("Username already taken.", "danger")
            return render_template("register.html", google_available=google_available)

        # Use a generic error for email to avoid enumeration
        if User.query.filter_by(email=email).first():
            flash("Username or email already registered.", "danger")
            return render_template("register.html", google_available=google_available)

        # Sanitize username to prevent XSS / injection in profile URLs
        username = re.sub(r"[^a-zA-Z0-9_]", "_", username)

        user = User(username=username, email=email)
        user.set_password(password)
        # Generate 6-digit verification code
        code = f"{secrets.randbelow(1000000):06d}"
        user.email_confirm_code = code
        user.email_code_expiry = datetime.utcnow() + timedelta(minutes=15)
        user.email_confirmed = False
        db.session.add(user)
        db.session.commit()

        # Send the code
        sent = send_verification_code(email, code)
        if sent:
            flash(f"Account created! A 6-digit code was sent to {email}.", "success")
        else:
            # If email can't be sent, auto-confirm & log the user in directly
            user.email_confirmed = True
            user.email_confirm_code = None
            user.email_code_expiry = None
            db.session.commit()
            login_user(user)
            rotate_csrf_token()
            flash("Account created! You're now logged in. (Email sending not configured)", "success")
            return redirect(url_for("index"))

        # Store email in session for the verify page
        session["verify_email"] = email
        return redirect(url_for("verify_email_code"))

    return render_template("register.html", google_available=google_available)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    google_available = bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))
    if request.method == "POST":
        if not validate_csrf():
            flash("Invalid form submission.", "danger")
            return render_template("login.html", google_available=google_available)
        login_id = request.form.get("username", "").strip()  # can be username or email
        password = request.form.get("password", "")

        # Rate limit by IP
        client_ip = request.remote_addr or "unknown"
        if not _rate_limit(f"login:{client_ip}"):
            flash("Too many login attempts. Try again later.", "danger")
            return render_template("login.html", google_available=google_available)

        # Look up by username or email
        user = User.query.filter_by(username=login_id).first()
        if user is None and "@" in login_id:
            user = User.query.filter_by(email=login_id).first()
        if user is None:
            flash("Invalid username or password.", "danger")
            return render_template("login.html", google_available=google_available)

        # Instagram: check account lockout
        if user.is_locked():
            remaining = (user.locked_until - datetime.utcnow()).seconds // 60
            flash(f"Account temporarily locked. Try again in {remaining} minutes.", "danger")
            return render_template("login.html", google_available=google_available)

        if not user.check_password(password):
            user.record_failed_login()
            db.session.commit()
            flash("Invalid username or password.", "danger")
            return render_template("login.html", google_available=google_available)

        # Reset failed attempts on successful login
        user.failed_login_attempts = 0
        user.locked_until = None
        db.session.commit()

        # Block login if email not confirmed (skip for OAuth users)
        if not user.email_confirmed and not user.oauth_provider:
            flash("Please verify your email before logging in. A code was sent to your email.", "warning")
            session["verify_email"] = user.email
            return redirect(url_for("verify_email_code"))

        # ── Login verification code (skip if email not configured) ─
        if SENDGRID_API_KEY:
            code = f"{secrets.randbelow(1000000):06d}"
            user.login_code = code
            user.login_code_expiry = datetime.utcnow() + timedelta(minutes=10)
            db.session.commit()

            sent = send_login_code(user.email, code)
            if sent:
                flash(f"A verification code was sent to {user.email}.", "info")
                session["login_user_id"] = user.id
                session["login_remember"] = request.form.get("remember") == "on"
                return redirect(url_for("verify_login_code"))
            else:
                flash("Could not send verification email. Logging in directly.", "warning")

        # Direct login if SendGrid not configured or sending failed
        remember = request.form.get("remember") == "on"
        login_user(user, remember=remember)
        rotate_csrf_token()

        # If 2FA is enabled, redirect to 2FA
        if user.totp_enabled:
            session["2fa_user_id"] = user.id
            session["2fa_remember"] = remember
            return redirect(url_for("verify_2fa_login"))

        flash(f"Welcome back, {user.username}!", "success")
        return redirect(url_for("index"))

    return render_template("login.html", google_available=google_available)


@app.route("/login/verify-code", methods=["GET", "POST"])
def verify_login_code():
    """Enter the 6-digit code sent to email during login."""
    user_id = session.get("login_user_id")
    if not user_id:
        flash("Please start the login process first.", "info")
        return redirect(url_for("login"))

    user = db.session.get(User, user_id)
    if not user:
        session.pop("login_user_id", None)
        session.pop("login_remember", None)
        return redirect(url_for("login"))

    if request.method == "POST":
        if not validate_csrf():
            flash("Invalid form submission.", "danger")
            return render_template("verify_login.html", email=user.email)
        code = request.form.get("code", "").strip()

        # Rate limit verification attempts
        client_ip = request.remote_addr or "unknown"
        if not _rate_limit(f"verify_login:{user.id}:{client_ip}", max_attempts=5, window=600):
            flash("Too many attempts. Please start the login process again.", "danger")
            session.pop("login_user_id", None)
            session.pop("login_remember", None)
            return redirect(url_for("login"))

        if not code or not code.isdigit() or len(code) != 6:
            flash("Please enter a valid 6-digit code.", "danger")
            return render_template("verify_login.html", email=user.email)

        # Check if code is expired
        if user.login_code_expiry and datetime.utcnow() > user.login_code_expiry:
            flash("Code expired. Please log in again to get a new code.", "danger")
            session.pop("login_user_id", None)
            session.pop("login_remember", None)
            return redirect(url_for("login"))

        if hmac.compare_digest(code, user.login_code or ""):
            # Clear login code
            user.login_code = None
            user.login_code_expiry = None
            db.session.commit()

            remember = session.pop("login_remember", False)
            session.pop("login_user_id", None)
            login_user(user, remember=remember)
            rotate_csrf_token()

            # If 2FA is enabled, redirect after this
            if user.totp_enabled:
                session["2fa_user_id"] = user.id
                session["2fa_remember"] = remember
                return redirect(url_for("verify_2fa_login"))

            flash(f"Welcome back, {user.username}!", "success")
            return redirect(url_for("index"))
        else:
            flash("Invalid code. Please try again.", "danger")
            return render_template("verify_login.html", email=user.email)

    return render_template("verify_login.html", email=user.email)


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Request a password reset link."""
    if request.method == "POST":
        if not validate_csrf():
            flash("Invalid form submission.", "danger")
            return render_template("forgot_password.html")
        email = request.form.get("email", "").strip().lower()

        if not email:
            flash("Please enter your email address.", "danger")
            return render_template("forgot_password.html")

        # Rate limit by IP and email
        client_ip = request.remote_addr or "unknown"
        if not _rate_limit(f"forgot_pw:{email}:{client_ip}", max_attempts=3, window=3600):
            flash("Too many requests. Try again later.", "danger")
            return render_template("forgot_password.html")

        user = User.query.filter_by(email=email).first()
        # Always show the same message to prevent email enumeration
        if user:
            # Generate a secure random token
            token = secrets.token_urlsafe(64)
            user.reset_token = token
            user.reset_token_expiry = datetime.utcnow() + timedelta(hours=1)
            db.session.commit()

            sent = send_password_reset_link(email, token)
            if not sent:
                flash("Could not send email. Please try again later.", "danger")
                return render_template("forgot_password.html")

        flash("If that email is registered, a password reset link has been sent.", "info")
        return redirect(url_for("login"))

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    """Reset password using a token from the email link."""
    if not token:
        flash("Invalid reset link.", "danger")
        return redirect(url_for("login"))

    user = User.query.filter_by(reset_token=token).first()
    if not user:
        flash("This reset link is invalid or has already been used.", "danger")
        return redirect(url_for("forgot_password"))

    if user.reset_token_expiry and datetime.utcnow() > user.reset_token_expiry:
        flash("This reset link has expired. Please request a new one.", "danger")
        user.reset_token = None
        user.reset_token_expiry = None
        db.session.commit()
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        if not validate_csrf():
            flash("Invalid form submission.", "danger")
            return render_template("reset_password.html", token=token)
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not password or len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
            return render_template("reset_password.html", token=token)

        if not re.search(r"[A-Z]", password) or not re.search(r"[a-z]", password) or not re.search(r"[0-9]", password):
            flash("Password must contain at least one uppercase letter, one lowercase letter, and one digit.", "danger")
            return render_template("reset_password.html", token=token)

        if password != confirm:
            flash("Passwords do not match.", "danger")
            return render_template("reset_password.html", token=token)

        # Update password
        user.set_password(password)
        user.reset_token = None
        user.reset_token_expiry = None
        db.session.commit()

        flash("Password has been reset successfully! You can now log in.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)


@app.route("/logout")
@login_required
def logout():
    # Log out Flask-Login first (keeps session for flash)
    logout_user()
    flash("You've been logged out.", "info")
    # Rotate CSRF token to invalidate any captured tokens
    session.clear()
    rotate_csrf_token()
    # Clear remember-me cookie
    resp = redirect(url_for("index"))
    resp.set_cookie(app.config.get("REMEMBER_COOKIE_NAME", "remember_token"), "", expires=0, path="/")
    return resp


# ── Google OAuth ────────────────────────────────────────────────

@app.route("/login/google")
def google_login():
    """Redirect user to Google consent screen."""
    if not os.environ.get("GOOGLE_CLIENT_ID") or not os.environ.get("GOOGLE_CLIENT_SECRET"):
        flash("Google Sign-In is not configured yet. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to enable it.", "info")
        return redirect(url_for("login"))
    # Build redirect URI dynamically — force HTTPS if behind Cloudflare tunnel
    scheme = "https" if request.headers.get("Cf-Visitor") or request.headers.get("X-Forwarded-Proto") == "https" else request.scheme
    redirect_uri = url_for("google_callback", _external=True, _scheme=scheme)
    return google.authorize_redirect(redirect_uri)


@app.route("/login/google/callback")
def google_callback():
    """Handle Google OAuth callback."""
    try:
        token = google.authorize_access_token()
        userinfo = token.get("userinfo")
        if not userinfo:
            userinfo = google.parse_id_token(token)

        google_id = userinfo["sub"]
        email = userinfo["email"]
        name = userinfo.get("name", email.split("@")[0])
        # Create a username from email if needed
        username_base = re.sub(r"[^a-zA-Z0-9_]", "_", email.split("@")[0])
        username = username_base

        # Check if user exists by google_id
        user = User.query.filter_by(oauth_provider="google", oauth_id=google_id).first()
        if not user:
            # Check by email
            user = User.query.filter_by(email=email).first()
            if user:
                # Link Google account to existing user
                user.oauth_provider = "google"
                user.oauth_id = google_id
                user.email_confirmed = True
                db.session.commit()
            else:
                # Create new user
                # Ensure unique username
                counter = 1
                base_username = username
                while User.query.filter_by(username=username).first():
                    username = f"{base_username}_{counter}"
                    counter += 1

                user = User(
                    username=username,
                    email=email,
                    password_hash=None,
                    oauth_provider="google",
                    oauth_id=google_id,
                    avatar=userinfo.get("picture", ""),
                    email_confirmed=True,  # Google already verified the email
                )
                db.session.add(user)
                db.session.commit()

        # If user has 2FA enabled, redirect to 2FA verification
        if user.totp_enabled:
            session["2fa_user_id"] = user.id
            session["2fa_remember"] = True
            flash("Please complete two-factor authentication.", "info")
            return redirect(url_for("verify_2fa_login"))

        login_user(user, remember=True)
        rotate_csrf_token()
        flash(f"Welcome, {user.username}!", "success")
        return redirect(url_for("index"))

    except Exception as e:
        print(f"[OAuth] Error: {e}")
        flash("Google sign-in failed. Please try again.", "danger")
        return redirect(url_for("login"))


@app.route("/profile/<username>")
def profile(username):
    user = User.query.filter_by(username=username).first_or_404()
    posts = Post.query.filter_by(user_id=user.id, hidden=False).order_by(Post.created_at.desc()).all()
    return render_template("profile.html", profile_user=user, posts=posts)


@app.route("/post/new", methods=["GET", "POST"])
@login_required
def new_post():
    if request.method == "POST":
        if not validate_csrf():
            flash("Invalid form submission.", "danger")
            return render_template("post.html")
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()

        if not title:
            flash("Please give your outfit a title.", "danger")
            return render_template("post.html")

        # Moderate title for illegal content
        mod_error = moderate_content(title)
        if mod_error:
            flash(mod_error, "danger")
            return render_template("post.html")

        # Moderate description for illegal content
        mod_error = moderate_content(description)
        if mod_error:
            flash(mod_error, "danger")
            return render_template("post.html")

        if "media" not in request.files:
            flash("Please upload a photo or video.", "danger")
            return render_template("post.html")

        file = request.files["media"]
        if not file.filename:
            flash("Please upload a photo or video.", "danger")
            return render_template("post.html")

        result = save_upload(file)
        if not result:
            flash("Invalid file type. Allowed: png, jpg, jpeg, gif, webp, mp4, mov, avi, webm, mkv", "danger")
            return render_template("post.html")

        # Check original filename for illegal content
        mod_error = moderate_content(file.filename)
        if mod_error:
            flash("File rejected.", "danger")
            # Delete the uploaded file
            filepath = os.path.join(app.config["UPLOAD_FOLDER"], result[0])
            if os.path.exists(filepath):
                os.remove(filepath)
            return render_template("post.html")

        filename, filehash = result

        # Check if same image hash was uploaded before (duplicate detection)
        dup = Post.query.filter_by(image_hash=filehash).first()
        if dup and not is_video_file(filename):
            flash("This image has already been posted.", "warning")
            # Delete duplicate
            filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            if os.path.exists(filepath):
                os.remove(filepath)
            return render_template("post.html")

        is_vid = is_video_file(filename)
        post = Post(
            title=title,
            description=description,
            image_filename=filename,
            image_hash=filehash if not is_vid else None,
            is_video=is_vid,
            user_id=current_user.id,
        )
        db.session.add(post)
        db.session.commit()

        if is_vid:
            flash("Video posted!", "success")
        else:
            # Run AI identification for images
            image_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            items = identify_clothing_items(image_path)
            for item_data in items:
                identified = IdentifiedItem(
                    post_id=post.id,
                    label=item_data["label"],
                    category=item_data["category"],
                    confidence=item_data["confidence"],
                    bounding_box=item_data.get("bounding_box", ""),
                )
                db.session.add(identified)
                db.session.flush()

                deals = search_deals(item_data["label"])
                for deal_data in deals:
                    deal = Deal(
                        item_id=identified.id,
                        website=deal_data["website"],
                        title=deal_data["title"],
                        price=deal_data["price"],
                        currency=deal_data.get("currency", "USD"),
                        url=deal_data["url"],
                        image_url=deal_data.get("image_url", ""),
                    )
                    db.session.add(deal)

            db.session.commit()
            flash("Outfit posted! AI identified clothing items and found deals.", "success")
        return redirect(url_for("view_outfit", post_id=post.id))

    return render_template("post.html")


@app.route("/outfit/<int:post_id>")
def view_outfit(post_id):
    post = db.session.get(Post, post_id)
    if not post:
        abort(404)

    items = IdentifiedItem.query.filter_by(post_id=post.id).all()
    user_rating = None
    if current_user.is_authenticated:
        rating = Rating.query.filter_by(user_id=current_user.id, post_id=post.id).first()
        user_rating = rating.score if rating else None

    # Get root comments (no parent) — exclude hidden ones, eager-load user & replies
    root_comments = (
        Comment.query
        .filter_by(post_id=post.id, parent_id=None, hidden=False)
        .options(db.joinedload(Comment.user))
        .options(db.joinedload(Comment.replies).joinedload(Comment.user))
        .order_by(Comment.created_at.desc())
        .all()
    )

    return render_template(
        "outfit.html",
        post=post,
        items=items,
        user_rating=user_rating,
        root_comments=root_comments,
        is_video=post.is_video,
    )


@app.route("/outfit/<int:post_id>/rate", methods=["POST"])
@login_required
def rate_outfit(post_id):
    if not validate_csrf():
        return jsonify({"error": "Invalid CSRF token"}), 400
    post = db.session.get(Post, post_id)
    if not post:
        return jsonify({"error": "Post not found"}), 404

    data = request.get_json()
    score = int(data.get("score", 0))
    if score < 1 or score > 5:
        return jsonify({"error": "Score must be 1-5"}), 400

    rating = Rating.query.filter_by(user_id=current_user.id, post_id=post.id).first()
    if rating:
        rating.score = score
    else:
        rating = Rating(score=score, user_id=current_user.id, post_id=post.id)
        db.session.add(rating)

    db.session.commit()

    return jsonify({
        "average": post.average_rating,
        "count": post.rating_count,
        "user_score": score,
    })


@app.route("/outfit/<int:post_id>/comment", methods=["POST"])
@login_required
def add_comment(post_id):
    post = db.session.get(Post, post_id)
    if not post:
        abort(404)

    body = request.form.get("body", "").strip()
    parent_id = request.form.get("parent_id", type=int)

    if not body:
        flash("Comment cannot be empty.", "danger")
    elif len(body) > 5000:
        flash("Comment is too long (max 5000 characters).", "danger")
    elif not validate_csrf():
        flash("Invalid form submission. Please try again.", "danger")
    else:
        # Content moderation
        mod_error = moderate_content(body)
        if mod_error:
            flash(mod_error, "danger")
        else:
            comment = Comment(
                body=body,
                user_id=current_user.id,
                post_id=post.id,
                parent_id=parent_id,
            )
            db.session.add(comment)
            db.session.commit()
            flash("Comment added!", "success")

    return redirect(url_for("view_outfit", post_id=post.id))


@app.route("/outfit/<int:post_id>/delete", methods=["POST"])
@login_required
def delete_post(post_id):
    if not validate_csrf():
        flash("Invalid form submission.", "danger")
        return redirect(url_for("index"))
    post = db.session.get(Post, post_id)
    if not post:
        abort(404)
    if post.user_id != current_user.id:
        abort(403)

    # Delete image file
    image_path = os.path.join(app.config["UPLOAD_FOLDER"], post.image_filename)
    if os.path.exists(image_path):
        os.remove(image_path)

    db.session.delete(post)
    db.session.commit()
    flash("Outfit deleted.", "info")
    return redirect(url_for("index"))


@app.route("/outfit/<int:post_id>/ai-rescan", methods=["POST"])
@login_required
def ai_rescan(post_id):
    """Re-run AI identification on an outfit."""
    if not validate_csrf():
        flash("Invalid form submission.", "danger")
        return redirect(url_for("index"))
    post = db.session.get(Post, post_id)
    if not post:
        abort(404)

    # Clear old identified items & deals (must delete deals first then items)
    for item in IdentifiedItem.query.filter_by(post_id=post.id).all():
        Deal.query.filter_by(item_id=item.id).delete()
    IdentifiedItem.query.filter_by(post_id=post.id).delete()

    image_path = os.path.join(app.config["UPLOAD_FOLDER"], post.image_filename)
    items = identify_clothing_items(image_path)
    for item_data in items:
        identified = IdentifiedItem(
            post_id=post.id,
            label=item_data["label"],
            category=item_data["category"],
            confidence=item_data["confidence"],
            bounding_box=item_data.get("bounding_box", ""),
        )
        db.session.add(identified)
        db.session.flush()

        deals = search_deals(item_data["label"])
        for deal_data in deals:
            deal = Deal(
                item_id=identified.id,
                website=deal_data["website"],
                title=deal_data["title"],
                price=deal_data["price"],
                currency=deal_data.get("currency", "USD"),
                url=deal_data["url"],
                image_url=deal_data.get("image_url", ""),
            )
            db.session.add(deal)

    db.session.commit()
    flash("AI re-scanned outfit! Updated items & deals.", "success")
    return redirect(url_for("view_outfit", post_id=post.id))


@app.route("/api/deals/<int:item_id>")
def api_deals(item_id):
    """Return deals for a specific identified item as JSON."""
    item = db.session.get(IdentifiedItem, item_id)
    if not item:
        return jsonify([])

    deals = Deal.query.filter_by(item_id=item.id).order_by(Deal.price).all()
    return jsonify([{
        "website": d.website,
        "title": d.title,
        "price": d.price,
        "currency": d.currency,
        "url": d.url,
        "image_url": d.image_url,
    } for d in deals])


@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# ---------------------------------------------------------------------------
# Real-time feed API — returns posts newer than a given timestamp
# ---------------------------------------------------------------------------

@app.route("/api/feed/latest")
def api_feed_latest():
    """Return posts created after a given timestamp (ISO format)."""
    since = request.args.get("since", "")
    try:
        since_dt = datetime.fromisoformat(since) if since else datetime.min
    except ValueError:
        since_dt = datetime.min

    new_posts = (
        Post.query
        .filter(Post.created_at > since_dt, Post.hidden.is_(False))
        .order_by(Post.created_at.desc())
        .limit(20)
        .all()
    )

    return jsonify([{
        "id": p.id,
        "title": p.title,
        "media_url": url_for("uploaded_file", filename=p.image_filename),
        "is_video": p.is_video,
        "author": p.author.username,
        "author_avatar": p.author.username[0].upper(),
        "average_rating": p.average_rating,
        "rating_count": p.rating_count,
        "created_at": p.created_at.isoformat(),
        "url": url_for("view_outfit", post_id=p.id),
    } for p in new_posts])


# ── 2FA Verification during login ──────────────────────────────

@app.route("/login/2fa", methods=["GET", "POST"])
def verify_2fa_login():
    """Verify 2FA code during login."""
    user_id = session.get("2fa_user_id")
    if not user_id:
        flash("Please log in first.", "info")
        return redirect(url_for("login"))

    user = db.session.get(User, user_id)
    if not user or not user.totp_enabled:
        session.pop("2fa_user_id", None)
        session.pop("2fa_remember", None)
        return redirect(url_for("login"))

    if request.method == "POST":
        if not validate_csrf():
            flash("Invalid form submission.", "danger")
            return render_template("verify_2fa.html")
        code = request.form.get("code", "").strip()
        if not code:
            flash("Please enter the 6-digit code.", "danger")
            return render_template("verify_2fa.html")

        totp = pyotp.TOTP(user.totp_secret)
        if totp.verify(code):
            remember = session.pop("2fa_remember", False)
            session.pop("2fa_user_id", None)
            login_user(user, remember=remember)
            flash(f"Welcome back, {user.username}!", "success")
            return redirect(url_for("index"))
        else:
            flash("Invalid code. Please try again.", "danger")
            return render_template("verify_2fa.html")

    return render_template("verify_2fa.html")


# ── 2FA Settings (authenticated users) ─────────────────────────

@app.route("/settings/2fa", methods=["GET", "POST"])
@login_required
def settings_2fa():
    """Manage 2FA settings — enable or disable."""
    if request.method == "POST":
        if not validate_csrf():
            flash("Invalid form submission.", "danger")
            return redirect(url_for("settings_2fa"))
        action = request.form.get("action")

        # ── Enable 2FA: step 1 — generate secret & show QR ──
        if action == "setup":
            secret = pyotp.random_base32()
            session["2fa_pending_secret"] = secret
            # Generate QR code URI
            uri = pyotp.totp.TOTP(secret).provisioning_uri(
                name=current_user.email,
                issuer_name="garments"
            )
            # Render QR as base64 PNG
            qr = qrcode.make(uri)
            buf = io.BytesIO()
            qr.save(buf, format="PNG")
            qr_b64 = base64.b64encode(buf.getvalue()).decode()
            return render_template("settings_2fa.html", pending_secret=secret, qr_data=qr_b64)

        # ── Enable 2FA: step 2 — verify the code ──
        if action == "verify":
            secret = session.get("2fa_pending_secret")
            if not secret:
                flash("Session expired. Please try again.", "danger")
                return redirect(url_for("settings_2fa"))
            code = request.form.get("code", "").strip()
            if not code:
                flash("Please enter the 6-digit code.", "danger")
                return render_template("settings_2fa.html", pending_secret=secret)
            totp = pyotp.TOTP(secret)
            if totp.verify(code):
                current_user.totp_secret = secret
                current_user.totp_enabled = True
                db.session.commit()
                session.pop("2fa_pending_secret", None)
                flash("Two-factor authentication enabled successfully!", "success")
                return redirect(url_for("settings_2fa"))
            else:
                flash("Invalid code. Please try again.", "danger")
                return render_template("settings_2fa.html", pending_secret=secret)

        # ── Disable 2FA ──
        if action == "disable":
            password = request.form.get("password", "")
            totp_code = request.form.get("totp_code", "").strip()
            email_confirm = request.form.get("email_confirm", "").strip().lower()

            # REQUIRED: verify current TOTP code
            if current_user.totp_secret:
                totp = pyotp.TOTP(current_user.totp_secret)
                if totp_code and totp.verify(totp_code):
                    pass  # TOTP verified
                else:
                    flash("Please enter a valid 2FA code from your authenticator app.", "danger")
                    return redirect(url_for("settings_2fa"))

            # Also verify password (for password users) or email (for OAuth users)
            verified = False
            if current_user.password_hash and current_user.check_password(password):
                verified = True
            elif email_confirm == current_user.email.lower():
                verified = True

            if not verified:
                flash("Verification failed. Provide your password or confirm your email.", "danger")
                return redirect(url_for("settings_2fa"))

            current_user.totp_secret = None
            current_user.totp_enabled = False
            db.session.commit()
            flash("Two-factor authentication disabled.", "info")
            return redirect(url_for("settings_2fa"))

    return render_template("settings_2fa.html",
                           totp_enabled=current_user.totp_enabled)


# ── Email verification (6-digit code) ──────────────────────────

@app.route("/verify-email-code", methods=["GET", "POST"])
def verify_email_code():
    """Enter the 6-digit code sent to the user's email."""
    email = session.get("verify_email")
    if not email:
        flash("No pending verification.", "info")
        return redirect(url_for("index"))

    user = User.query.filter_by(email=email).first()
    if not user:
        session.pop("verify_email", None)
        flash("User not found.", "danger")
        return redirect(url_for("register"))

    if user.email_confirmed:
        session.pop("verify_email", None)
        flash("Email already verified. You can log in.", "success")
        return redirect(url_for("login"))

    # Rate limit verification attempts
    client_ip = request.remote_addr or "unknown"
    is_ratelimited = not _rate_limit(f"verify_code:{email}:{client_ip}", max_attempts=10, window=900)

    if request.method == "POST":
        if not validate_csrf():
            flash("Invalid form submission.", "danger")
            return render_template("verify_code.html", email=email)

        if is_ratelimited:
            flash("Too many verification attempts. Try again later.", "danger")
            return render_template("verify_code.html", email=email, code=user.email_confirm_code)

        code = request.form.get("code", "").strip()

        if not code or not code.isdigit() or len(code) != 6:
            flash("Please enter a valid 6-digit code.", "danger")
            return render_template("verify_code.html", email=email)

        # Check if code is expired
        if user.email_code_expiry and datetime.utcnow() > user.email_code_expiry:
            flash("Code expired. Request a new one.", "danger")
            return render_template("verify_code.html", email=email)

        # Use constant-time comparison to prevent timing attacks
        if hmac.compare_digest(code, user.email_confirm_code or ""):
            user.email_confirmed = True
            user.email_confirm_code = None
            user.email_code_expiry = None
            db.session.commit()
            session.pop("verify_email", None)
            flash("Email verified! You can now log in.", "success")
            return redirect(url_for("login"))
        else:
            flash("Invalid code. Please try again.", "danger")
            return render_template("verify_code.html", email=email, code=user.email_confirm_code)

    return render_template("verify_code.html", email=email, code=user.email_confirm_code)


@app.route("/resend-code", methods=["POST"])
def resend_code():
    """Resend the 6-digit verification code."""
    # Changed from GET to POST with CSRF protection to prevent CSRF & prefetch abuse
    if not validate_csrf():
        flash("Invalid request.", "danger")
        return redirect(url_for("login"))

    email = request.form.get("email") or session.get("verify_email")
    if not email:
        flash("No pending verification.", "info")
        return redirect(url_for("index"))

    # Rate limit resends by IP and email
    client_ip = request.remote_addr or "unknown"
    if not _rate_limit(f"resend:{email}:{client_ip}", max_attempts=3, window=600):
        flash("Too many resend requests. Try again later.", "danger")
        return redirect(url_for("verify_email_code"))

    user = User.query.filter_by(email=email).first()
    if not user or user.email_confirmed:
        session.pop("verify_email", None)
        return redirect(url_for("login"))

    # Generate new code
    code = f"{secrets.randbelow(1000000):06d}"
    user.email_confirm_code = code
    user.email_code_expiry = datetime.utcnow() + timedelta(minutes=15)
    db.session.commit()

    sent = send_verification_code(email, code)
    if sent:
        flash("A new 6-digit code was sent to your email.", "success")
    else:
        flash("Failed to send email. Try again later.", "danger")

    session["verify_email"] = email
    return redirect(url_for("verify_email_code"))


# ── Reporting system ───────────────────────────────────────────
# Users can flag posts/comments. Content with 3+ reports is auto-hidden.

REPORT_THRESHOLD = 3  # reports before auto-hide


@app.route("/report/post/<int:post_id>", methods=["POST"])
@login_required
def report_post(post_id):
    """Report a post for moderation."""
    if not validate_csrf():
        flash("Invalid form submission.", "danger")
        return redirect(request.referrer or url_for("index"))

    post = db.session.get(Post, post_id)
    if not post:
        abort(404)

    # Check if user already reported this post
    existing = Report.query.filter_by(user_id=current_user.id, post_id=post.id).first()
    if existing:
        flash("You have already reported this post.", "info")
    else:
        reason = request.form.get("reason", "other")
        report = Report(user_id=current_user.id, post_id=post.id, reason=reason)
        db.session.add(report)
        post.report_count = (post.report_count or 0) + 1
        # Auto-hide if threshold reached
        if post.report_count >= REPORT_THRESHOLD:
            post.hidden = True
            flash("Post has been hidden after multiple reports.", "warning")
        else:
            flash("Post reported. Thank you.", "success")
        db.session.commit()

    return redirect(request.referrer or url_for("index"))


@app.route("/report/comment/<int:comment_id>", methods=["POST"])
@login_required
def report_comment(comment_id):
    """Report a comment for moderation."""
    if not validate_csrf():
        flash("Invalid form submission.", "danger")
        return redirect(request.referrer or url_for("index"))

    comment = db.session.get(Comment, comment_id)
    if not comment:
        abort(404)

    existing = Report.query.filter_by(user_id=current_user.id, comment_id=comment.id).first()
    if existing:
        flash("You have already reported this comment.", "info")
    else:
        reason = request.form.get("reason", "other")
        report = Report(user_id=current_user.id, comment_id=comment.id, reason=reason)
        db.session.add(report)
        # Count total reports for this comment
        report_count = Report.query.filter_by(comment_id=comment.id).count() + 1
        if report_count >= REPORT_THRESHOLD:
            comment.hidden = True
            flash("Comment has been hidden after multiple reports.", "warning")
        else:
            flash("Comment reported. Thank you.", "success")
        db.session.commit()

    return redirect(request.referrer or url_for("index"))


# ---------------------------------------------------------------------------
# Initialize database on startup (for production with gunicorn)
# ---------------------------------------------------------------------------

def initialize_app():
    """Create database tables and ensure upload directory exists."""
    with app.app_context():
        db.create_all()
        # Add new columns for existing databases (safe if columns already exist)
        for col in ["login_code", "login_code_expiry", "reset_token", "reset_token_expiry",
                     "failed_login_attempts", "locked_until"]:
            try:
                with db.engine.connect() as conn:
                    col_type = "INTEGER" if col in ["failed_login_attempts"] else "DATETIME" if col in ["locked_until"] else "VARCHAR(128)"
                    conn.execute(db.text(f"ALTER TABLE users ADD COLUMN {col} {col_type}"))
                    conn.commit()
            except Exception:
                pass
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)

initialize_app()

# ---------------------------------------------------------------------------
# Run (development only)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("garments running on http://localhost:5001")
    app.run(host="0.0.0.0", port=5001, debug=False)
