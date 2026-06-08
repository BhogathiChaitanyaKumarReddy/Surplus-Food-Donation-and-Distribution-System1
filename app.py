from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, Response, stream_with_context
import base64
import datetime
import os
import re
import json
import time
import urllib.error
import urllib.request
import hmac
import secrets
from urllib.parse import urlparse, parse_qs, quote
import uuid
from math import radians, sin, cos, sqrt, asin
from app_utils import clean_text, require_session_key, validate_password_pair, validate_required_fields
from db import get_db_connection
from db_helpers import DatabaseUnavailableError, db_cursor
from upload_utils import save_uploaded_file, is_allowed_upload
from session_utils import register_session_hooks
from csrf_utils import register_csrf_protection
from realtime_utils import build_realtime_summary
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

def _load_secret_key(root_path=None):
    configured_key = os.environ.get('FLASK_SECRET_KEY') or os.environ.get('SECRET_KEY')
    if configured_key:
        return configured_key

    secret_path = os.path.join(root_path or os.path.dirname(__file__), '.flask-secret-key')
    try:
        if os.path.exists(secret_path):
            with open(secret_path, 'r', encoding='utf-8') as secret_file:
                saved_key = secret_file.read().strip()
                if saved_key:
                    return saved_key

        generated_key = secrets.token_urlsafe(32)
        with open(secret_path, 'w', encoding='utf-8') as secret_file:
            secret_file.write(generated_key)
        return generated_key
    except OSError:
        return secrets.token_urlsafe(32)

app = Flask(__name__)
app.config['SECRET_KEY'] = _load_secret_key(app.root_path)
register_session_hooks(app)
register_csrf_protection(app)

from admin_routes import admin_bp
from user_routes import api_bp, user_bp
from support_routes import support_bp
app.register_blueprint(admin_bp)
app.register_blueprint(api_bp)
app.register_blueprint(user_bp)
app.register_blueprint(support_bp)

# --- File Upload Configuration ---
UPLOAD_FOLDER = os.path.join(app.root_path, 'static/uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
OWNER_ID_UPLOAD_DIR = os.path.join(app.root_path, 'static/uploads/owner_ids')
OWNER_ID_ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf'}
OWNER_ID_MAX_SIZE = 2 * 1024 * 1024  # 2 MB
PARTNER_DOC_UPLOAD_DIR = os.path.join(app.root_path, 'static/uploads/partner_docs')
PARTNER_DOC_ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf'}
PARTNER_DOC_MAX_SIZE = 2 * 1024 * 1024  # 2 MB
SUPPORT_UPLOAD_DIR = os.path.join(app.root_path, 'static/uploads/support')

def allowed_file(filename):
    return is_allowed_upload(filename, ALLOWED_EXTENSIONS)

def allowed_owner_id_file(filename):
    return is_allowed_upload(filename, OWNER_ID_ALLOWED_EXTENSIONS)

def allowed_partner_doc_file(filename):
    return is_allowed_upload(filename, PARTNER_DOC_ALLOWED_EXTENSIONS)

def _vehicle_requires_license(vehicle_type):
    vehicle = (vehicle_type or '').strip().lower()
    return 'bicycle' not in vehicle and 'cycle' not in vehicle

def _password_matches(stored_password, provided_password):
    stored_password = (stored_password or '').strip()
    provided_password = clean_text(provided_password)
    if not stored_password or not provided_password:
        return False, False
    try:
        if check_password_hash(stored_password, provided_password):
            return True, False
    except (ValueError, TypeError):
        pass
    if stored_password == provided_password:
        return True, True
    return False, False

def _stream_live_snapshot(snapshot_factory, interval_seconds, error_message):
    @stream_with_context
    def generate():
        last_payload = None
        while True:
            response = snapshot_factory()
            if getattr(response, 'status_code', 200) != 200:
                data = response.get_json(silent=True) or {'success': False, 'error': error_message}
                yield f"event: error\ndata: {json.dumps(data, default=str, separators=(',', ':'))}\n\n"
                break

            payload = response.get_json(silent=True) or {}
            serialized = json.dumps(payload, default=str, separators=(',', ':'))
            if serialized != last_payload:
                yield f"event: snapshot\ndata: {serialized}\n\n"
                last_payload = serialized
            else:
                yield "event: heartbeat\ndata: {}\n\n"
            time.sleep(interval_seconds)

    headers = {
        'Cache-Control': 'no-cache, no-transform',
        'X-Accel-Buffering': 'no',
    }
    return Response(generate(), mimetype='text/event-stream', headers=headers)

def _save_partner_doc_upload(file_storage, prefix):
    saved_url, _ = save_uploaded_file(
        file_storage,
        PARTNER_DOC_UPLOAD_DIR,
        '/static/uploads/partner_docs',
        allowed_extensions=PARTNER_DOC_ALLOWED_EXTENSIONS,
        max_size=PARTNER_DOC_MAX_SIZE,
        filename_prefix=prefix,
    )
    return saved_url

def normalize_google_maps_url(raw_url, fallback_location=None):
    if not raw_url:
        return None
    raw_url = raw_url.strip()
    if not raw_url:
        return None
    # Short Google Maps share links cannot be embedded without resolving them.
    # Accept the form anyway and use the typed location for the embeddable map.
    lower = raw_url.lower()
    if 'maps.app.goo.gl' in lower or 'goo.gl/maps' in lower:
        if fallback_location:
            return f"https://maps.google.com/maps?q={quote(fallback_location)}&output=embed"
        return None
    try:
        parsed = urlparse(raw_url)
    except Exception:
        return None
    href = parsed.geturl()
    if 'output=embed' in href or '/maps/embed' in href:
        return href
    # Try to extract query parameters.
    qs = parse_qs(parsed.query or '')
    q = None
    if 'q' in qs and qs['q']:
        q = qs['q'][0]
    elif 'query' in qs and qs['query']:
        q = qs['query'][0]
    if q:
        return f"https://maps.google.com/maps?q={quote(q)}&output=embed"
    # Try to extract @lat,lon from path.
    match = re.search(r'@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)', href)
    if match:
        coords = f"{match.group(1)},{match.group(2)}"
        return f"https://maps.google.com/maps?q={quote(coords)}&output=embed"
    # Fallback to location text if available.
    if fallback_location:
        return f"https://maps.google.com/maps?q={quote(fallback_location)}&output=embed"
    # Not a recognized Google Maps link; return original.
    return href

def geocode_location_server(location_text):
    if not location_text:
        return None
    try:
        url = f"https://nominatim.openstreetmap.org/search?format=json&q={quote(location_text)}"
        req = urllib.request.Request(url, headers={"User-Agent": "surplus-food-app/1.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data:
            return float(data[0].get("lat")), float(data[0].get("lon"))
    except Exception:
        return None
    return None


def _format_prep_time(minutes_total):
    minutes_total = max(0, int(minutes_total or 0))
    hours, minutes = divmod(minutes_total, 60)
    return hours, minutes


def _parse_prep_minutes(prep_time):
    text = (prep_time or '').strip().lower()
    hours_match = re.search(r'(\d+)\s*h', text)
    minutes_match = re.search(r'(\d+)\s*m', text)
    hours = int(hours_match.group(1)) if hours_match else 0
    minutes = int(minutes_match.group(1)) if minutes_match else 0
    if not hours and not minutes:
        number_match = re.search(r'\d+', text)
        minutes = int(number_match.group()) if number_match else 0
    return max(0, hours * 60 + minutes)


def _parse_owner_datetime(value):
    value = clean_text(value)
    if not value:
        return None
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def _mysql_datetime(value):
    if not value:
        return None
    return value.strftime('%Y-%m-%d %H:%M:%S')


def _default_shelf_life_hours(item_type):
    item_type = (item_type or '').strip()
    if item_type == 'Raw-items':
        return 24
    if item_type == 'Non-Veg':
        return 4
    return 6


def _prepare_donation_safety_fields(form, item_type, donation_date, prep_time):
    packed_at = _parse_owner_datetime(form.get('packed_time'))
    best_before = _parse_owner_datetime(form.get('best_before_time'))
    storage_note = clean_text(form.get('storage_note'))[:255]

    if not packed_at:
        try:
            prepared_day = datetime.datetime.strptime(donation_date, '%Y-%m-%d')
        except (TypeError, ValueError):
            prepared_day = datetime.datetime.now()
        if prepared_day.date() == datetime.date.today():
            packed_at = datetime.datetime.now().replace(second=0, microsecond=0)
        else:
            packed_at = prepared_day + datetime.timedelta(minutes=_parse_prep_minutes(prep_time))

    if not best_before:
        best_before = packed_at + datetime.timedelta(hours=_default_shelf_life_hours(item_type))

    if best_before <= packed_at:
        return None, None, None, 'Best before time must be after packed time.'
    return _mysql_datetime(packed_at), _mysql_datetime(best_before), storage_note, None


def _mark_owner_expired_donations(cursor, owner_id):
    cursor.execute(
        """
        UPDATE Donation
        SET status = 'Expired'
        WHERE restaurant_id = %s
          AND status IN ('Available', 'PendingReview')
          AND best_before_time IS NOT NULL
          AND best_before_time < NOW()
        """,
        (owner_id,)
    )


def _guess_item_type_from_text(text, fallback='Veg'):
    lowered = (text or '').lower()
    non_veg_keywords = ['chicken', 'mutton', 'meat', 'fish', 'prawn', 'egg', 'eggs', 'biryani']
    raw_keywords = ['raw', 'rice', 'wheat', 'flour', 'dough', 'vegetable tray', 'ingredient', 'ingredients']
    veg_keywords = ['paneer', 'veg', 'vegetarian', 'dal', 'curry', 'sambar', 'idli', 'dosa', 'biryani', 'pulao', 'snack', 'salad', 'dessert', 'cake', 'bread', 'roll', 'fries', 'meal box']

    for keyword in non_veg_keywords:
        if keyword in lowered:
            return 'Non-Veg'
    for keyword in raw_keywords:
        if keyword in lowered and 'raw' in lowered:
            return 'Raw-items'
    for keyword in veg_keywords:
        if keyword in lowered:
            return 'Veg'
    return fallback if fallback in {'Veg', 'Non-Veg', 'Raw-items'} else 'Veg'


def _guess_item_name_from_text(text, item_type):
    lowered = (text or '').lower()
    name_map = [
        (['chicken biryani'], 'Chicken Biryani'),
        (['veg biryani', 'vegetable biryani'], 'Veg Biryani'),
        (['paneer butter masala', 'paneer'], 'Paneer Curry'),
        (['fried rice'], 'Fried Rice'),
        (['idli'], 'Idli'),
        (['dosa'], 'Dosa'),
        (['sandwich'], 'Sandwich Pack'),
        (['pizza'], 'Pizza Slices'),
        (['burger'], 'Burger Pack'),
        (['salad'], 'Fresh Salad Pack'),
        (['cake', 'dessert', 'sweet'], 'Dessert Pack'),
        (['curry', 'sabzi'], 'Meal Curry'),
        (['bread'], 'Bread Pack'),
        (['raw items', 'raw-items', 'ingredients'], 'Raw Ingredient Pack'),
    ]
    for keywords, suggestion in name_map:
        if any(keyword in lowered for keyword in keywords):
            return suggestion
    if item_type == 'Non-Veg':
        return 'Ready-to-Eat Non-Veg Meal'
    if item_type == 'Raw-items':
        return 'Raw Ingredient Pack'
    return 'Ready-to-Eat Veg Meal'


def _suggest_quantity(text, quantity_hint, recent_data):
    if quantity_hint:
        try:
            return max(1, int(quantity_hint))
        except (TypeError, ValueError):
            pass

    lowered = (text or '').lower()
    base_quantity = 15
    if any(word in lowered for word in ['dessert', 'cake', 'sweet', 'bread', 'sandwich']):
        base_quantity = 12
    elif any(word in lowered for word in ['snack', 'starter', 'roll', 'burger']):
        base_quantity = 18
    elif any(word in lowered for word in ['rice', 'biryani', 'meal', 'curry', 'thali', 'combo']):
        base_quantity = 20
    elif any(word in lowered for word in ['raw', 'ingredient', 'vegetable']):
        base_quantity = 25

    if recent_data:
        avg_recent = None
        recent_qtys = []
        for row in recent_data:
            try:
                recent_qtys.append(int(re.search(r'\d+', str(row.get('quantity') or '')).group()))
            except Exception:
                continue
        if recent_qtys:
            avg_recent = sum(recent_qtys) / len(recent_qtys)
        if avg_recent:
            base_quantity = int(round((base_quantity + avg_recent) / 2))

    return max(1, base_quantity)


def _suggest_prep_minutes(text, item_type):
    lowered = (text or '').lower()
    if item_type == 'Raw-items':
        minutes = 20
    elif any(word in lowered for word in ['salad', 'sandwich', 'bread', 'juice']):
        minutes = 30
    elif any(word in lowered for word in ['cake', 'dessert', 'sweet']):
        minutes = 60
    elif any(word in lowered for word in ['chicken', 'mutton', 'meat', 'fish']):
        minutes = 90
    elif any(word in lowered for word in ['biryani', 'pulao', 'curry', 'meal', 'thali']):
        minutes = 45
    else:
        minutes = 40

    if 'ready' in lowered or 'leftover' in lowered or 'packed' in lowered:
        minutes = max(15, minutes - 10)
    return _format_prep_time(minutes)


def _build_owner_trust_profile(request_breakdown, rating_stats, tip_stats, total_donations):
    pending = int((request_breakdown or {}).get('pending_count') or 0)
    accepted = int((request_breakdown or {}).get('accepted_count') or 0)
    rejected = int((request_breakdown or {}).get('rejected_count') or 0)
    collected = int((request_breakdown or {}).get('collected_count') or 0)
    no_show = int((request_breakdown or {}).get('no_show_count') or 0)
    total_requests = pending + accepted + rejected + collected + no_show

    score = 60
    reasons = []

    if total_requests:
        fulfillment_rate = round((collected / total_requests) * 100)
        score += min(18, fulfillment_rate // 5)
        reasons.append(f"{fulfillment_rate}% of requests completed")
    else:
        fulfillment_rate = 0
        score += 5
        reasons.append("No request history yet")

    rating_count = int((rating_stats or {}).get('rating_count') or 0)
    avg_rating = float((rating_stats or {}).get('avg_rating') or 0)
    if rating_count:
        score += min(12, round(avg_rating * 2))
        reasons.append(f"{avg_rating:.1f}/5 average taste rating")

    tip_count = int((tip_stats or {}).get('tip_count') or 0)
    if tip_count:
        score += min(8, tip_count * 2)
        reasons.append(f"{tip_count} appreciation tip(s) received")

    if no_show:
        score -= min(12, no_show * 4)
        reasons.append(f"{no_show} no-show request(s)")

    if rejected:
        score -= min(8, rejected * 2)
        reasons.append(f"{rejected} rejected request(s)")

    if total_donations:
        score += min(6, total_donations // 3)
        reasons.append(f"{total_donations} donation listing(s) published")

    score = max(0, min(100, score))
    if score >= 85:
        label = 'Excellent'
    elif score >= 65:
        label = 'Strong'
    elif score >= 45:
        label = 'Watch'
    else:
        label = 'At Risk'

    return {
        'score': score,
        'label': label,
        'fulfillment_rate': fulfillment_rate,
        'reasons': reasons[:5],
    }


def _build_owner_intelligence(owner_requests, item_type_rows, request_breakdown):
    owner_requests = owner_requests or []
    item_type_rows = item_type_rows or []
    total_requests = len(owner_requests)
    requested_amounts = []
    for req in owner_requests:
        try:
            requested_amounts.append(int(req.get('requested_amt') or 0))
        except (TypeError, ValueError):
            continue
    avg_requested_amt = round(sum(requested_amounts) / len(requested_amounts), 1) if requested_amounts else 0
    pending_total = int((request_breakdown or {}).get('pending_count') or 0)
    accepted_total = int((request_breakdown or {}).get('accepted_count') or 0)
    collected_total = int((request_breakdown or {}).get('collected_count') or 0)
    rejected_total = int((request_breakdown or {}).get('rejected_count') or 0)

    if pending_total >= 6:
        demand_pressure = 'High'
    elif total_requests >= 4:
        demand_pressure = 'Steady'
    elif total_requests:
        demand_pressure = 'Emerging'
    else:
        demand_pressure = 'No data yet'

    if total_requests >= 8:
        recommended_window = '11:00 AM - 2:00 PM'
    elif total_requests >= 3:
        recommended_window = 'Late morning'
    else:
        recommended_window = 'Any time after prep'

    recommended_type = item_type_rows[0].get('item_type') if item_type_rows else 'Veg'
    if recommended_type in (None, ''):
        recommended_type = 'Veg'

    if avg_requested_amt:
        low = max(5, int(round(avg_requested_amt)) - 5)
        high = max(low + 1, int(round(avg_requested_amt)) + 5)
    else:
        low, high = 10, 20

    recommendation_lines = []
    if pending_total:
        recommendation_lines.append(f"{pending_total} request(s) are still pending, so post listings soon.")
    if accepted_total:
        recommendation_lines.append(f"{accepted_total} request(s) have already been accepted.")
    if collected_total:
        recommendation_lines.append(f"{collected_total} request(s) reached pickup completion.")
    if rejected_total:
        recommendation_lines.append(f"{rejected_total} request(s) were rejected, so keep listings clear and timely.")
    if not recommendation_lines:
        recommendation_lines.append("Start with a fresh listing to build your demand history.")

    return {
        'demand_pressure': demand_pressure,
        'total_requests': total_requests,
        'recommended_window': recommended_window,
        'recommended_type': recommended_type,
        'recommended_quantity_range': f"{low}-{high} servings",
        'avg_requested_amt': avg_requested_amt,
        'recommendation_lines': recommendation_lines[:5],
        'item_type_rows': item_type_rows,
    }


def _build_owner_suggestions(restaurant, stats, request_breakdown, rating_stats, tip_stats, owner_trust, owner_intelligence, my_donations):
    """Generate up to 15 real-time, contextual suggestions for restaurant owners."""
    suggestions = []
    pending = int((request_breakdown or {}).get('pending_count') or 0)
    accepted = int((request_breakdown or {}).get('accepted_count') or 0)
    rejected = int((request_breakdown or {}).get('rejected_count') or 0)
    collected = int((request_breakdown or {}).get('collected_count') or 0)
    no_show = int((request_breakdown or {}).get('no_show_count') or 0)
    total_requests = pending + accepted + rejected + collected + no_show
    trust_score = int((owner_trust or {}).get('score') or 0)
    avg_rating = float((rating_stats or {}).get('avg_rating') or 0)
    rating_count = int((rating_stats or {}).get('rating_count') or 0)
    tip_count = int((tip_stats or {}).get('tip_count') or 0)
    verified = restaurant.get('verified', False)
    has_email = bool((restaurant.get('email') or '').strip())
    has_map = bool((restaurant.get('map_url') or '').strip())
    has_photo = bool((restaurant.get('photo_url') or '').strip()) and 'unsplash.com' not in (restaurant.get('photo_url') or '')
    has_id_doc = bool((restaurant.get('id_doc_url') or '').strip())
    active_donations = int(stats.get('available_donations') or 0)
    total_donations = int(stats.get('total_donations') or 0)

    # 1. Verification urgency
    if not verified:
        if restaurant.get('verification_rejection_reason'):
            suggestions.append({'icon': 'fa-triangle-exclamation', 'color': 'danger', 'urgency': 'critical',
                'title': 'Verification Rejected',
                'detail': f"Reason: {restaurant['verification_rejection_reason']}. Update your profile and re-upload documents to get verified."})
        else:
            suggestions.append({'icon': 'fa-shield-halved', 'color': 'warning', 'urgency': 'high',
                'title': 'Complete Verification',
                'detail': 'Your restaurant is pending admin verification. You cannot publish donations until verified. Ensure your FSSAI and profile details are accurate.'})

    # 2. Pending requests need action
    if pending >= 5:
        suggestions.append({'icon': 'fa-bell', 'color': 'danger', 'urgency': 'critical',
            'title': f'{pending} Requests Waiting',
            'detail': 'Multiple food requests are pending your action. Accept or reject them quickly to maintain a high trust score and keep recipients happy.'})
    elif pending >= 1:
        suggestions.append({'icon': 'fa-bell', 'color': 'warning', 'urgency': 'high',
            'title': f'{pending} Pending Request(s)',
            'detail': 'You have pending food requests. Respond promptly — faster response times boost your reliability score and build trust with users.'})

    # 3. No active donations
    if active_donations == 0 and verified:
        suggestions.append({'icon': 'fa-utensils', 'color': 'primary', 'urgency': 'high',
            'title': 'No Active Listings',
            'detail': 'You have no food currently listed. Post surplus food now to help the community and attract more request traffic to your profile.'})

    # 4. Trust score alerts
    if trust_score < 45:
        suggestions.append({'icon': 'fa-heart-crack', 'color': 'danger', 'urgency': 'critical',
            'title': 'Trust Score At Risk',
            'detail': f'Your trust score is {trust_score}/100. Improve it by accepting more requests, reducing no-shows, and keeping listings fresh and accurate.'})
    elif trust_score < 65:
        suggestions.append({'icon': 'fa-chart-line', 'color': 'warning', 'urgency': 'medium',
            'title': 'Improve Your Trust Score',
            'detail': f'Your trust score is {trust_score}/100. Fulfill accepted requests on time and add photos to listings to build stronger community trust.'})
    elif trust_score >= 85:
        suggestions.append({'icon': 'fa-star', 'color': 'success', 'urgency': 'low',
            'title': 'Excellent Trust Score!',
            'detail': f'Your trust score is {trust_score}/100 — keep it up! Maintaining consistency helps you get prioritized in user listings.'})

    # 5. No-show warning
    if no_show >= 3:
        suggestions.append({'icon': 'fa-user-slash', 'color': 'danger', 'urgency': 'high',
            'title': f'{no_show} No-Show Reports',
            'detail': 'Multiple requests were marked as not collected. This significantly hurts your trust. Consider contacting users before marking no-shows.'})
    elif no_show >= 1:
        suggestions.append({'icon': 'fa-user-clock', 'color': 'warning', 'urgency': 'medium',
            'title': 'No-Show Detected',
            'detail': f'{no_show} request(s) were marked as no-show. Use the direct chat to coordinate pickup times and reduce missed collections.'})

    # 6. Rating improvement
    if rating_count >= 3 and avg_rating < 3.0:
        suggestions.append({'icon': 'fa-face-frown', 'color': 'warning', 'urgency': 'high',
            'title': 'Low Taste Ratings',
            'detail': f'Your average taste rating is {avg_rating:.1f}/5. Focus on freshness and accurate item descriptions to improve satisfaction.'})
    elif rating_count >= 3 and avg_rating >= 4.5:
        suggestions.append({'icon': 'fa-trophy', 'color': 'success', 'urgency': 'low',
            'title': 'Outstanding Ratings!',
            'detail': f'Average {avg_rating:.1f}/5 from {rating_count} ratings. Your food quality is loved! Consider posting more frequently to serve more people.'})
    elif rating_count == 0 and collected >= 1:
        suggestions.append({'icon': 'fa-comment-dots', 'color': 'info', 'urgency': 'medium',
            'title': 'No Ratings Yet',
            'detail': 'Users haven\'t rated your food yet. Good food photos and accurate descriptions encourage ratings after pickup.'})

    # 7. Profile completeness
    if not has_email:
        suggestions.append({'icon': 'fa-envelope', 'color': 'info', 'urgency': 'medium',
            'title': 'Add Email Address',
            'detail': 'Adding an email helps admin reach you for verification updates and important platform announcements.'})
    if not has_map:
        suggestions.append({'icon': 'fa-map-location-dot', 'color': 'info', 'urgency': 'medium',
            'title': 'Add Google Maps Link',
            'detail': 'A Google Maps URL helps delivery partners and users find your restaurant easily, reducing no-shows and improving pickups.'})
    if not has_photo:
        suggestions.append({'icon': 'fa-camera', 'color': 'info', 'urgency': 'medium',
            'title': 'Upload Restaurant Photo',
            'detail': 'Restaurants with real photos get 2x more food requests. Upload a photo of your restaurant to build credibility.'})
    if not has_id_doc:
        suggestions.append({'icon': 'fa-id-card', 'color': 'info', 'urgency': 'medium',
            'title': 'Upload ID Document',
            'detail': 'Submitting an ID document speeds up verification and shows users your restaurant is trustworthy.'})

    # 8. Tip engagement
    if tip_count == 0 and collected >= 3:
        suggestions.append({'icon': 'fa-hand-holding-heart', 'color': 'primary', 'urgency': 'low',
            'title': 'No Tips Received Yet',
            'detail': 'Quality food and prompt responses encourage users to tip. Keep delivering fresh items and responding fast.'})
    elif tip_count >= 5:
        suggestions.append({'icon': 'fa-gift', 'color': 'success', 'urgency': 'low',
            'title': f'{tip_count} Tips Received!',
            'detail': 'Users appreciate your contributions. Keep up the great work — consistent quality and fast responses bring more tips.'})

    # 9. Posting time recommendation
    recommended_window = (owner_intelligence or {}).get('recommended_window', '')
    if recommended_window and total_requests >= 3:
        suggestions.append({'icon': 'fa-clock', 'color': 'primary', 'urgency': 'medium',
            'title': f'Best Posting Window: {recommended_window}',
            'detail': 'Based on your request patterns, posting during this window gets the most engagement. Time your listings for maximum impact.'})

    # 10. High rejection rate
    if total_requests >= 5 and rejected >= total_requests * 0.4:
        suggestions.append({'icon': 'fa-xmark-circle', 'color': 'danger', 'urgency': 'high',
            'title': 'High Rejection Rate',
            'detail': f'{rejected} of {total_requests} requests were rejected. Consider listing only items you can reliably fulfill to keep your rejection rate low.'})

    # 11. Donation diversity
    items_served = (restaurant.get('items_served') or '').split(',')
    items_served = [i.strip() for i in items_served if i.strip()]
    if len(items_served) <= 1 and total_donations >= 3:
        suggestions.append({'icon': 'fa-layer-group', 'color': 'info', 'urgency': 'low',
            'title': 'Diversify Your Menu',
            'detail': 'You\'re listing only one food category. Adding more types (Veg, Non-Veg, Raw) can attract a wider audience of users.'})

    # 12. Consistent donor badge
    if total_donations >= 10 and collected >= 5:
        suggestions.append({'icon': 'fa-medal', 'color': 'success', 'urgency': 'low',
            'title': 'Consistent Donor!',
            'detail': f'With {total_donations} listings and {collected} completed pickups, you\'re a key contributor. Share your referral link to inspire others.'})

    # 13. Chat responsiveness
    unread_chats = int(stats.get('unread_chats') or 0)
    if unread_chats >= 3:
        suggestions.append({'icon': 'fa-comments', 'color': 'warning', 'urgency': 'high',
            'title': f'{unread_chats} Unread Chats',
            'detail': 'Users are waiting for your reply. Quick chat responses build trust and help coordinate pickups smoothly.'})

    return suggestions[:15]


def build_owner_ai_assistant(owner_id, notes, quantity_hint=None):
    conn = get_db_connection()
    if not conn:
        return None, "Database Connection Failed."

    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT item_name, item_type, quantity, prep_time, date, created_at
        FROM Donation
        WHERE restaurant_id = %s
        ORDER BY created_at DESC
        LIMIT 8
        """,
        (owner_id,)
    )
    recent_donations = cursor.fetchall()

    cursor.execute(
        """
        SELECT d.item_name, COUNT(*) AS request_count
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        WHERE d.restaurant_id = %s AND r.status = 'Pending'
        GROUP BY d.item_name
        ORDER BY request_count DESC, d.item_name ASC
        LIMIT 5
        """,
        (owner_id,)
    )
    recent_requests = cursor.fetchall()
    cursor.close()
    conn.close()

    notes = (notes or '').strip()
    fallback_type = recent_donations[0]['item_type'] if recent_donations else 'Veg'
    item_type = _guess_item_type_from_text(notes, fallback_type)
    item_name = _guess_item_name_from_text(notes, item_type)
    quantity = _suggest_quantity(notes, quantity_hint, recent_donations)
    prep_hours, prep_minutes = _suggest_prep_minutes(notes, item_type)

    if not notes and recent_requests:
        item_name = recent_requests[0]['item_name']

    urgency = 'Medium'
    urgency_reason = 'Standard surplus listing.'
    lowered = notes.lower()
    if any(word in lowered for word in ['fresh', 'leftover', 'ready', 'packed', 'expiring', 'tonight']):
        urgency = 'High'
        urgency_reason = 'This looks ready to publish now.'
    elif any(word in lowered for word in ['raw', 'dry', 'sealed', 'ingredient']):
        urgency = 'Low'
        urgency_reason = 'Ingredient-style items can usually wait a bit longer.'

    tips = []
    if recent_requests:
        tips.append(f"Your most requested item right now is {recent_requests[0]['item_name']}.")
    if recent_donations:
        tips.append(f"Your latest donation was {recent_donations[0]['item_name']}, so this keeps your listing style consistent.")
    tips.append(f"Suggested prep time: {prep_hours}h {prep_minutes:02d}m.")

    return {
        'suggested_item_name': item_name,
        'suggested_item_type': item_type,
        'suggested_quantity': quantity,
        'prep_hours': prep_hours,
        'prep_minutes': prep_minutes,
        'urgency': urgency,
        'urgency_reason': urgency_reason,
        'tips': tips,
    }, None


def _owner_filter_url(**updates):
    params = request.args.to_dict(flat=True)
    for key, value in updates.items():
        params[key] = str(value)
    return url_for('owner_dashboard', **params)

def create_tables(conn):
    from schema_bootstrap import bootstrap_schema
    bootstrap_schema(conn)
    return

    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Restaurant (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            owner_name VARCHAR(100) NOT NULL,
            email VARCHAR(100),
            restaurant_type VARCHAR(50),
            gst VARCHAR(50),
            fssai VARCHAR(50) NOT NULL,
            location TEXT NOT NULL,
            map_url TEXT,
            latitude DECIMAL(10,7),
            longitude DECIMAL(10,7),
            contact VARCHAR(20) NOT NULL,
            alternate_contact VARCHAR(20),
            items_served VARCHAR(50),
            verified BOOLEAN DEFAULT FALSE,
            verification_rejection_reason VARCHAR(255) DEFAULT NULL,
            photo_url VARCHAR(255) DEFAULT 'https://images.unsplash.com/photo-1414235077428-338989a2e8c0?w=500&auto=format&fit=crop&q=60',
            id_doc_url VARCHAR(255),
            username VARCHAR(50) UNIQUE NOT NULL,
            password VARCHAR(255) NOT NULL
        )
    """)
    # Add columns if they don't exist yet (for existing databases)
    try:
        cursor.execute("ALTER TABLE Restaurant ADD COLUMN email VARCHAR(100) AFTER owner_name")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE Restaurant ADD COLUMN restaurant_type VARCHAR(50) AFTER email")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE Restaurant ADD COLUMN verification_rejection_reason VARCHAR(255) DEFAULT NULL AFTER verified")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE Restaurant ADD COLUMN map_url TEXT AFTER location")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE Restaurant ADD COLUMN latitude DECIMAL(10,7) AFTER map_url")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE Restaurant ADD COLUMN longitude DECIMAL(10,7) AFTER latitude")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE Restaurant MODIFY COLUMN map_url TEXT")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE Restaurant ADD COLUMN alternate_contact VARCHAR(20) AFTER contact")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE Restaurant ADD COLUMN id_doc_url VARCHAR(255) AFTER photo_url")
    except Exception:
        pass
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Donation (
            id INT AUTO_INCREMENT PRIMARY KEY,
            restaurant_id INT NOT NULL,
            item_type VARCHAR(20) NOT NULL,
            item_name VARCHAR(100) NOT NULL,
            quantity VARCHAR(50) NOT NULL,
            prep_time VARCHAR(50) NOT NULL,
            date VARCHAR(20) NOT NULL,
            status VARCHAR(20) DEFAULT 'Available',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (restaurant_id) REFERENCES Restaurant(id)
        )
    """)
    try:
        cursor.execute("ALTER TABLE Donation ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    except Exception:
        pass
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS User (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            org_name VARCHAR(120) NOT NULL,
            email VARCHAR(100),
            contact VARCHAR(20) NOT NULL,
            password VARCHAR(255),
            alternate_contact VARCHAR(20),
            org_type VARCHAR(50) NOT NULL,
            food_preferences TEXT,
            org_location TEXT,
            org_image_url VARCHAR(255),
            id_doc_url VARCHAR(255),
            user_verified BOOLEAN DEFAULT FALSE,
            user_verification_rejection_reason VARCHAR(255) DEFAULT NULL,
            terms_accepted BOOLEAN DEFAULT FALSE,
            joined VARCHAR(50),
            reward_coins INT DEFAULT 0,
            referral_code VARCHAR(32) DEFAULT NULL,
            referred_by_code VARCHAR(32) DEFAULT NULL,
            referral_bonus_granted BOOLEAN DEFAULT FALSE
        )
    """)
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN email VARCHAR(100) AFTER name")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN org_name VARCHAR(120) NOT NULL AFTER name")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN password VARCHAR(255) AFTER contact")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN alternate_contact VARCHAR(20) AFTER contact")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN food_preferences TEXT AFTER org_type")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN org_location TEXT AFTER food_preferences")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN org_image_url VARCHAR(255) AFTER org_location")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN id_doc_url VARCHAR(255) AFTER food_preferences")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN user_verified BOOLEAN DEFAULT FALSE AFTER id_doc_url")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN user_verification_rejection_reason VARCHAR(255) DEFAULT NULL AFTER user_verified")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN terms_accepted BOOLEAN DEFAULT FALSE AFTER id_doc_url")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN reward_coins INT DEFAULT 0 AFTER joined")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN referral_code VARCHAR(32) DEFAULT NULL AFTER reward_coins")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN referred_by_code VARCHAR(32) DEFAULT NULL AFTER referral_code")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN referral_bonus_granted BOOLEAN DEFAULT FALSE AFTER referred_by_code")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD UNIQUE KEY unique_user_referral_code (referral_code)")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN alternate_contact VARCHAR(20) AFTER contact")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN food_preferences TEXT AFTER org_type")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN id_doc_url VARCHAR(255) AFTER food_preferences")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE User ADD COLUMN terms_accepted BOOLEAN DEFAULT FALSE AFTER id_doc_url")
    except Exception:
        pass
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS FundDonation (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            amount_paise INT NOT NULL,
            currency VARCHAR(10) DEFAULT 'INR',
            note VARCHAR(255) DEFAULT NULL,
            receipt VARCHAR(100) DEFAULT NULL,
            razorpay_order_id VARCHAR(100) UNIQUE,
            razorpay_payment_id VARCHAR(100) DEFAULT NULL,
            razorpay_signature VARCHAR(255) DEFAULT NULL,
            status VARCHAR(20) DEFAULT 'Created',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            paid_at TIMESTAMP NULL DEFAULT NULL,
            FOREIGN KEY (user_id) REFERENCES User(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS FoodRequest (
            id INT AUTO_INCREMENT PRIMARY KEY,
            donation_id INT NOT NULL,
            user_id INT,
            delivery_partner_id INT DEFAULT NULL,
            requested_amt INT NOT NULL,
            status VARCHAR(20) DEFAULT 'Pending',
            delivery_mode VARCHAR(20) DEFAULT 'Pickup',
            delivery_charge_mode VARCHAR(30) DEFAULT 'CashOnDelivery',
            delivery_address VARCHAR(255) DEFAULT NULL,
            delivery_latitude DECIMAL(10,7) DEFAULT NULL,
            delivery_longitude DECIMAL(10,7) DEFAULT NULL,
            delivery_location_accuracy_m DECIMAL(10,2) DEFAULT NULL,
            delivery_location_updated_at TIMESTAMP NULL DEFAULT NULL,
            delivery_order_id VARCHAR(50) DEFAULT NULL,
            delivery_fee_paise INT DEFAULT NULL,
            delivery_coin_used INT DEFAULT 0,
            delivery_coin_discount_paise INT DEFAULT 0,
            rejection_reason VARCHAR(255) DEFAULT NULL,
            request_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            accepted_at TIMESTAMP NULL DEFAULT NULL,
            food_ready_at TIMESTAMP NULL DEFAULT NULL,
            pickup_reached_at TIMESTAMP NULL DEFAULT NULL,
            out_for_delivery_at TIMESTAMP NULL DEFAULT NULL,
            delivered_at TIMESTAMP NULL DEFAULT NULL,
            delivery_otp VARCHAR(10) DEFAULT NULL,
            otp_generated_at TIMESTAMP NULL DEFAULT NULL,
            otp_verified_at TIMESTAMP NULL DEFAULT NULL,
            delivery_issue_type VARCHAR(50) DEFAULT NULL,
            delivery_issue_role VARCHAR(30) DEFAULT NULL,
            delivery_issue_detail VARCHAR(255) DEFAULT NULL,
            delivery_issue_reported_at TIMESTAMP NULL DEFAULT NULL,
            taste_rating INT DEFAULT NULL,
            taste_feedback VARCHAR(255) DEFAULT NULL,
            rated_at TIMESTAMP NULL DEFAULT NULL,
            FOREIGN KEY (donation_id) REFERENCES Donation(id),
            FOREIGN KEY (user_id) REFERENCES User(id)
        )
    """)
    # Add rejection_reason column if it doesn't exist yet (for existing databases)
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN rejection_reason VARCHAR(255) DEFAULT NULL")
    except Exception:
        pass  # Column already exists
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN accepted_at TIMESTAMP NULL DEFAULT NULL AFTER request_time")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN food_ready_at TIMESTAMP NULL DEFAULT NULL AFTER accepted_at")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN pickup_reached_at TIMESTAMP NULL DEFAULT NULL AFTER food_ready_at")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN delivery_partner_id INT DEFAULT NULL AFTER user_id")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN delivery_mode VARCHAR(20) DEFAULT 'Pickup' AFTER status")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN delivery_charge_mode VARCHAR(30) DEFAULT 'CashOnDelivery' AFTER delivery_mode")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN delivery_address VARCHAR(255) DEFAULT NULL AFTER delivery_mode")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN delivery_order_id VARCHAR(50) DEFAULT NULL AFTER delivery_address")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN delivery_fee_paise INT DEFAULT NULL AFTER delivery_order_id")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN delivery_coin_used INT DEFAULT 0 AFTER delivery_fee_paise")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN delivery_coin_discount_paise INT DEFAULT 0 AFTER delivery_coin_used")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN delivery_latitude DECIMAL(10,7) DEFAULT NULL AFTER delivery_address")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN delivery_longitude DECIMAL(10,7) DEFAULT NULL AFTER delivery_latitude")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN delivery_location_accuracy_m DECIMAL(10,2) DEFAULT NULL AFTER delivery_longitude")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN delivery_location_updated_at TIMESTAMP NULL DEFAULT NULL AFTER delivery_location_accuracy_m")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN out_for_delivery_at TIMESTAMP NULL DEFAULT NULL AFTER accepted_at")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN delivered_at TIMESTAMP NULL DEFAULT NULL AFTER out_for_delivery_at")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN delivery_otp VARCHAR(10) DEFAULT NULL AFTER delivered_at")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN otp_generated_at TIMESTAMP NULL DEFAULT NULL AFTER delivery_otp")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN otp_verified_at TIMESTAMP NULL DEFAULT NULL AFTER otp_generated_at")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN delivery_issue_type VARCHAR(50) DEFAULT NULL AFTER otp_verified_at")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN delivery_issue_role VARCHAR(30) DEFAULT NULL AFTER delivery_issue_type")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN delivery_issue_detail VARCHAR(255) DEFAULT NULL AFTER delivery_issue_role")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN delivery_issue_reported_at TIMESTAMP NULL DEFAULT NULL AFTER delivery_issue_detail")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN taste_rating INT DEFAULT NULL AFTER accepted_at")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN taste_feedback VARCHAR(255) DEFAULT NULL AFTER taste_rating")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN rated_at TIMESTAMP NULL DEFAULT NULL AFTER taste_feedback")
    except Exception:
        pass
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS DeliveryPartner (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                phone VARCHAR(20) NOT NULL,
                email VARCHAR(100) DEFAULT NULL,
                zone VARCHAR(100) DEFAULT NULL,
                vehicle_type VARCHAR(50) DEFAULT NULL,
                username VARCHAR(50) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL,
                is_available BOOLEAN DEFAULT TRUE,
                is_active BOOLEAN DEFAULT TRUE,
                application_status VARCHAR(30) DEFAULT 'Submitted',
                verification_remarks VARCHAR(255) DEFAULT NULL,
                identity_document_type VARCHAR(30) DEFAULT NULL,
                identity_document_url VARCHAR(255) DEFAULT NULL,
                profile_photo_url VARCHAR(255) DEFAULT NULL,
                pan_card_url VARCHAR(255) DEFAULT NULL,
                driving_license_url VARCHAR(255) DEFAULT NULL,
                vehicle_rc_url VARCHAR(255) DEFAULT NULL,
                bank_document_url VARCHAR(255) DEFAULT NULL,
                vehicle_number VARCHAR(30) DEFAULT NULL,
                payment_method_preference VARCHAR(30) DEFAULT 'BankTransfer',
                upi_id VARCHAR(100) DEFAULT NULL,
                bank_account_holder VARCHAR(120) DEFAULT NULL,
                bank_account_number VARCHAR(40) DEFAULT NULL,
                bank_ifsc VARCHAR(20) DEFAULT NULL,
                bank_name VARCHAR(120) DEFAULT NULL,
                payment_verified BOOLEAN DEFAULT FALSE,
                verified_at TIMESTAMP NULL DEFAULT NULL,
                verified_by VARCHAR(50) DEFAULT NULL,
                last_reviewed_at TIMESTAMP NULL DEFAULT NULL,
                current_latitude DECIMAL(10,7) DEFAULT NULL,
                current_longitude DECIMAL(10,7) DEFAULT NULL,
                current_accuracy_m DECIMAL(10,2) DEFAULT NULL,
                current_location_updated_at TIMESTAMP NULL DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
    """)
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE DeliveryPartner ADD COLUMN is_available BOOLEAN DEFAULT TRUE AFTER password")
    except Exception:
        pass
    partner_alter_statements = [
        "ALTER TABLE DeliveryPartner ADD COLUMN application_status VARCHAR(30) DEFAULT 'Submitted' AFTER is_active",
        "ALTER TABLE DeliveryPartner ADD COLUMN verification_remarks VARCHAR(255) DEFAULT NULL AFTER application_status",
        "ALTER TABLE DeliveryPartner ADD COLUMN identity_document_type VARCHAR(30) DEFAULT NULL AFTER verification_remarks",
        "ALTER TABLE DeliveryPartner ADD COLUMN identity_document_url VARCHAR(255) DEFAULT NULL AFTER identity_document_type",
        "ALTER TABLE DeliveryPartner ADD COLUMN profile_photo_url VARCHAR(255) DEFAULT NULL AFTER identity_document_url",
        "ALTER TABLE DeliveryPartner ADD COLUMN pan_card_url VARCHAR(255) DEFAULT NULL AFTER profile_photo_url",
        "ALTER TABLE DeliveryPartner ADD COLUMN driving_license_url VARCHAR(255) DEFAULT NULL AFTER pan_card_url",
        "ALTER TABLE DeliveryPartner ADD COLUMN vehicle_rc_url VARCHAR(255) DEFAULT NULL AFTER driving_license_url",
        "ALTER TABLE DeliveryPartner ADD COLUMN bank_document_url VARCHAR(255) DEFAULT NULL AFTER vehicle_rc_url",
        "ALTER TABLE DeliveryPartner ADD COLUMN vehicle_number VARCHAR(30) DEFAULT NULL AFTER vehicle_type",
        "ALTER TABLE DeliveryPartner ADD COLUMN payment_method_preference VARCHAR(30) DEFAULT 'BankTransfer' AFTER vehicle_number",
        "ALTER TABLE DeliveryPartner ADD COLUMN upi_id VARCHAR(100) DEFAULT NULL AFTER payment_method_preference",
        "ALTER TABLE DeliveryPartner ADD COLUMN bank_account_holder VARCHAR(120) DEFAULT NULL AFTER upi_id",
        "ALTER TABLE DeliveryPartner ADD COLUMN bank_account_number VARCHAR(40) DEFAULT NULL AFTER bank_account_holder",
        "ALTER TABLE DeliveryPartner ADD COLUMN bank_ifsc VARCHAR(20) DEFAULT NULL AFTER bank_account_number",
        "ALTER TABLE DeliveryPartner ADD COLUMN bank_name VARCHAR(120) DEFAULT NULL AFTER bank_ifsc",
        "ALTER TABLE DeliveryPartner ADD COLUMN payment_verified BOOLEAN DEFAULT FALSE AFTER bank_name",
        "ALTER TABLE DeliveryPartner ADD COLUMN verified_at TIMESTAMP NULL DEFAULT NULL AFTER payment_verified",
        "ALTER TABLE DeliveryPartner ADD COLUMN verified_by VARCHAR(50) DEFAULT NULL AFTER verified_at",
        "ALTER TABLE DeliveryPartner ADD COLUMN last_reviewed_at TIMESTAMP NULL DEFAULT NULL AFTER verified_by",
    ]
    for statement in partner_alter_statements:
        try:
            cursor.execute(statement)
        except Exception:
            pass
    try:
        cursor.execute("ALTER TABLE DeliveryPartner ADD COLUMN current_latitude DECIMAL(10,7) DEFAULT NULL AFTER is_active")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE DeliveryPartner ADD COLUMN current_longitude DECIMAL(10,7) DEFAULT NULL AFTER current_latitude")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE DeliveryPartner ADD COLUMN current_accuracy_m DECIMAL(10,2) DEFAULT NULL AFTER current_longitude")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE DeliveryPartner ADD COLUMN current_location_updated_at TIMESTAMP NULL DEFAULT NULL AFTER current_accuracy_m")
    except Exception:
        pass
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS DeliveryPartnerRejection (
                id INT AUTO_INCREMENT PRIMARY KEY,
                request_id INT NOT NULL,
                partner_id INT NOT NULL,
                rejection_reason VARCHAR(255) DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY unique_partner_request_rejection (request_id, partner_id),
                FOREIGN KEY (request_id) REFERENCES FoodRequest(id),
                FOREIGN KEY (partner_id) REFERENCES DeliveryPartner(id)
            )
        """)
    except Exception:
        pass
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS RequestTip (
                id INT AUTO_INCREMENT PRIMARY KEY,
                request_id INT NOT NULL,
                donation_id INT NOT NULL,
                user_id INT NOT NULL,
                restaurant_id INT NOT NULL,
                amount_paise INT NOT NULL,
                currency VARCHAR(10) DEFAULT 'INR',
                note VARCHAR(255) DEFAULT NULL,
                receipt VARCHAR(100) DEFAULT NULL,
                razorpay_order_id VARCHAR(100) UNIQUE,
                razorpay_payment_id VARCHAR(100) DEFAULT NULL,
                razorpay_signature VARCHAR(255) DEFAULT NULL,
                status VARCHAR(20) DEFAULT 'Created',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                paid_at TIMESTAMP NULL DEFAULT NULL,
                UNIQUE KEY unique_tip_request (request_id),
                FOREIGN KEY (request_id) REFERENCES FoodRequest(id),
                FOREIGN KEY (donation_id) REFERENCES Donation(id),
                FOREIGN KEY (user_id) REFERENCES User(id),
                FOREIGN KEY (restaurant_id) REFERENCES Restaurant(id)
            )
        """)
    except Exception:
        pass
    try:
        cursor.execute(
            "ALTER TABLE FoodRequest MODIFY taste_feedback VARCHAR(255) "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    except Exception:
        pass
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            message_id INT AUTO_INCREMENT PRIMARY KEY,
            session_id VARCHAR(255),
            sender_name VARCHAR(255),
            sender_role ENUM('User', 'Owner', 'Admin', 'Guest'),
            sender_id INT DEFAULT NULL,
            receiver_id INT DEFAULT NULL,
            receiver_role ENUM('User', 'Owner', 'Admin'),
            restaurant_id INT DEFAULT NULL,
            topic VARCHAR(255),
            message TEXT,
            file_url VARCHAR(255),
            is_admin BOOLEAN DEFAULT FALSE,
            chat_type ENUM('Support', 'Direct') DEFAULT 'Support',
            status ENUM('Open', 'Solved') DEFAULT 'Open',
            delivered_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
            read_at TIMESTAMP NULL DEFAULT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX (session_id)
        )
    """)
    try:
        cursor.execute("ALTER TABLE messages ADD COLUMN delivered_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE messages ADD COLUMN read_at TIMESTAMP NULL DEFAULT NULL")
    except Exception:
        pass
    
    # Ensure support uploads can be created on demand.
    os.makedirs(SUPPORT_UPLOAD_DIR, exist_ok=True)
    
    cursor.close()

def initialize_database():
    """Best-effort database initialization during app startup."""
    try:
        get_db_connection()
    except Exception as exc:
        # Keep the web app usable for routes that do not need the database yet.
        print(f"Database initialization skipped: {exc}")


def _get_homepage_overview():
    conn = None
    overview = {
        'total_donations': 0,
        'meals_saved': 0,
        'available_donations': 0,
        'active_partners': 0,
        'completed_requests': 0,
        'featured_donations': [],
        'top_locations': [],
    }
    try:
        conn = get_db_connection()
        if conn is None:
            raise RuntimeError("Database connection unavailable")
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT COUNT(*) AS total_donations FROM Donation")
        overview['total_donations'] = int((cursor.fetchone() or {}).get('total_donations') or 0)

        cursor.execute("""
            SELECT COALESCE(SUM(CASE WHEN status = 'Delivered' THEN requested_amt ELSE 0 END), 0) AS meals_saved
            FROM FoodRequest
        """)
        overview['meals_saved'] = int((cursor.fetchone() or {}).get('meals_saved') or 0)

        cursor.execute("""
            SELECT COUNT(*) AS available_donations
            FROM Donation
            WHERE status = 'Available'
              AND created_at > NOW() - INTERVAL 8 HOUR
        """)
        overview['available_donations'] = int((cursor.fetchone() or {}).get('available_donations') or 0)

        cursor.execute("""
            SELECT COUNT(*) AS active_partners
            FROM DeliveryPartner
            WHERE is_active = TRUE AND is_available = TRUE
        """)
        overview['active_partners'] = int((cursor.fetchone() or {}).get('active_partners') or 0)

        cursor.execute("""
            SELECT COUNT(*) AS completed_requests
            FROM FoodRequest
            WHERE status = 'Delivered'
        """)
        overview['completed_requests'] = int((cursor.fetchone() or {}).get('completed_requests') or 0)

        cursor.execute("""
            SELECT
                d.id,
                d.item_name,
                d.item_type,
                d.quantity,
                d.prep_time,
                d.status,
                d.created_at,
                d.image_url,
                rest.name AS restaurant_name,
                rest.location AS restaurant_location,
                rest.verified AS restaurant_verified,
                TIMESTAMPDIFF(MINUTE, d.created_at, NOW()) AS age_minutes
            FROM Donation d
            JOIN Restaurant rest ON d.restaurant_id = rest.id
            WHERE d.status = 'Available'
              AND d.created_at > NOW() - INTERVAL 8 HOUR
            ORDER BY d.created_at DESC
            LIMIT 6
        """)
        overview['featured_donations'] = cursor.fetchall()

        cursor.execute("""
            SELECT
                COALESCE(NULLIF(TRIM(rest.location), ''), 'Unknown location') AS location_label,
                COUNT(*) AS donation_count
            FROM Donation d
            JOIN Restaurant rest ON d.restaurant_id = rest.id
            WHERE d.status = 'Available'
              AND d.created_at > NOW() - INTERVAL 8 HOUR
            GROUP BY COALESCE(NULLIF(TRIM(rest.location), ''), 'Unknown location')
            ORDER BY donation_count DESC, location_label ASC
            LIMIT 3
        """)
        overview['top_locations'] = cursor.fetchall()

        cursor.close()
    except Exception as exc:
        print(f"Homepage data load failed: {exc}")
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass
    return overview


@app.route('/')
def index():
    """Landing Page"""
    overview = _get_homepage_overview()
    total_donations = overview['total_donations']
    meals_saved = overview['meals_saved']
    available_donations = overview['available_donations']
    active_partners = overview['active_partners']
    completed_requests = overview['completed_requests']
    featured_donations = overview['featured_donations']
    top_locations = overview['top_locations']

    platform_stats = [
        {
            'label': 'Donations',
            'value': total_donations,
            'hint': 'all time listings',
        },
        {
            'label': 'Meals saved',
            'value': meals_saved,
            'hint': 'rescued through the platform',
        },
        {
            'label': 'Active partners',
            'value': active_partners,
            'hint': 'ready for pickup',
        },
        {
            'label': 'Live listings',
            'value': available_donations,
            'hint': 'available in the last 8 hours',
        },
    ]

    rescue_progress = 0
    if total_donations:
        rescue_progress = min(100, round((available_donations / total_donations) * 100))
    elif completed_requests:
        rescue_progress = min(100, round(min(completed_requests, 100)))

    return render_template(
        'index.html',
        platform_stats=platform_stats,
        featured_donations=featured_donations,
        top_locations=top_locations,
        rescue_progress=rescue_progress,
        completed_requests=completed_requests,
    )


@app.route('/api/homepage_overview')
def homepage_overview_api():
    overview = _get_homepage_overview()
    return jsonify({
        'success': True,
        'platform_stats': [
            {'label': 'Donations', 'value': overview['total_donations'], 'hint': 'all time listings'},
            {'label': 'Meals saved', 'value': overview['meals_saved'], 'hint': 'rescued through the platform'},
            {'label': 'Active partners', 'value': overview['active_partners'], 'hint': 'ready for pickup'},
            {'label': 'Live listings', 'value': overview['available_donations'], 'hint': 'available in the last 8 hours'},
        ],
        'rescue_progress': min(100, round((overview['available_donations'] / overview['total_donations']) * 100)) if overview['total_donations'] else min(100, round(min(overview['completed_requests'], 100))),
        'featured_donations': overview['featured_donations'],
        'top_locations': overview['top_locations'],
        'completed_requests': overview['completed_requests'],
    })


def _get_public_impact_summary():
    overview = _get_homepage_overview()
    summary = {
        'overview': overview,
        'weekly_activity': [],
        'top_restaurants': [],
        'status_breakdown': [],
        'weekly_peak': 1,
        'status_peak': 1,
    }
    conn = None
    try:
        conn = get_db_connection()
        if conn is None:
            raise RuntimeError("Database connection unavailable")
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                DATE(r.request_time) AS activity_date,
                COUNT(*) AS request_count,
                COALESCE(SUM(CASE WHEN r.status = 'Delivered' THEN r.requested_amt ELSE 0 END), 0) AS meals_saved
            FROM FoodRequest r
            WHERE r.request_time >= CURDATE() - INTERVAL 6 DAY
            GROUP BY DATE(r.request_time)
            ORDER BY activity_date ASC
        """)
        summary['weekly_activity'] = [
            {
                'date': row['activity_date'].strftime('%a') if row.get('activity_date') else 'Day',
                'request_count': int(row.get('request_count') or 0),
                'meals_saved': int(row.get('meals_saved') or 0),
            }
            for row in cursor.fetchall()
        ]
        summary['weekly_peak'] = max([row['request_count'] for row in summary['weekly_activity']] or [1])

        cursor.execute("""
            SELECT
                rest.name AS restaurant_name,
                rest.location AS restaurant_location,
                COUNT(*) AS deliveries,
                COALESCE(SUM(CASE WHEN r.status = 'Delivered' THEN r.requested_amt ELSE 0 END), 0) AS meals_saved
            FROM FoodRequest r
            JOIN Donation d ON r.donation_id = d.id
            JOIN Restaurant rest ON d.restaurant_id = rest.id
            WHERE r.status IN ('Delivered', 'Collected')
            GROUP BY rest.id, rest.name, rest.location
            ORDER BY deliveries DESC, meals_saved DESC
            LIMIT 4
        """)
        summary['top_restaurants'] = cursor.fetchall()

        cursor.execute("""
            SELECT
                r.status AS status_label,
                COUNT(*) AS total_count
            FROM FoodRequest r
            GROUP BY r.status
            ORDER BY total_count DESC
        """)
        summary['status_breakdown'] = cursor.fetchall()
        summary['status_peak'] = max([int(row.get('total_count') or 0) for row in summary['status_breakdown']] or [1])

        cursor.close()
    except Exception as exc:
        print(f"Public impact data load failed: {exc}")
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass
    return summary


def _build_website_suggestions():
    sections = [
        {
            'title': 'Homepage',
            'icon': 'fa-house',
            'tone': 'green',
            'items': [
                'Add a sticky "Donate Food" button on mobile.',
                'Show today’s total meals saved in the hero section.',
                'Add a live map preview of nearby donations.',
                'Add a rotating testimonial banner from users.',
                'Show a countdown for urgent listings.',
                'Add a quick role chooser for User, Owner, and Partner.',
                'Surface the nearest live donation cards first.',
                'Add an animated impact counter with gentle motion.',
                'Display weather-aware pickup tips on the home page.',
                'Add a “how it works” stepper directly below the hero.',
            ],
        },
        {
            'title': 'Donation Flow',
            'icon': 'fa-bowl-food',
            'tone': 'blue',
            'items': [
                'Add smart form hints while owners type item details.',
                'Pre-fill item type based on the donation description.',
                'Add photo upload preview before submitting a listing.',
                'Warn owners when prep time seems too short.',
                'Suggest common serving units based on the item type.',
                'Add a save-draft option for unfinished donations.',
                'Let owners duplicate a previous donation listing.',
                'Show a checklist before publishing a donation.',
                'Add a donation timeline with status stages.',
                'Recommend the best pickup window from recent activity.',
            ],
        },
        {
            'title': 'User Search',
            'icon': 'fa-magnifying-glass',
            'tone': 'amber',
            'items': [
                'Add stronger filters for veg, non-veg, and raw items.',
                'Let users save preferred food zones.',
                'Add sort by newest, nearest, and largest quantity.',
                'Show a live distance estimate beside every result.',
                'Add quick search chips for popular meal types.',
                'Let users bookmark donations for later.',
                'Highlight listings that are expiring soon.',
                'Add one-tap directions from search results.',
                'Show allergy or ingredient warnings where possible.',
                'Add a “best match” result at the top.',
            ],
        },
        {
            'title': 'Delivery Partner',
            'icon': 'fa-truck-fast',
            'tone': 'purple',
            'items': [
                'Show route cards with pickup and drop-off in one view.',
                'Add a dark navigation mode for late-night deliveries.',
                'Display a clear approval status on the partner dashboard.',
                'Add document checklist reminders during registration.',
                'Show current earnings or trip count summaries.',
                'Add better offline-friendly GPS update handling.',
                'Display job urgency with color-coded labels.',
                'Let partners mark a temporary pause status.',
                'Add one-tap call buttons for users and restaurants.',
                'Show a quick route preview before accepting a job.',
            ],
        },
        {
            'title': 'Admin Controls',
            'icon': 'fa-shield-halved',
            'tone': 'red',
            'items': [
                'Create a verification queue for every role.',
                'Add a dedicated partner approval dashboard.',
                'Show risk flags for repeated no-shows.',
                'Add one-click export for audits and reports.',
                'Display pending approvals by priority.',
                'Show document previews in modal dialogs.',
                'Add review notes when approving or rejecting users.',
                'Highlight inactive accounts needing attention.',
                'Show daily moderation workload counts.',
                'Add a global search for users, partners, and owners.',
            ],
        },
        {
            'title': 'Support',
            'icon': 'fa-headset',
            'tone': 'green',
            'items': [
                'Add suggested replies for common support questions.',
                'Show ticket priority based on topic urgency.',
                'Auto-fill support context from the current page.',
                'Add file attachments for verification issues.',
                'Let users reopen solved chats within 24 hours.',
                'Add support response time indicators.',
                'Create a searchable help center with FAQs.',
                'Offer a “contact me later” callback option.',
                'Show unresolved chats by role and topic.',
                'Send a confirmation when support marks a case solved.',
            ],
        },
        {
            'title': 'Accessibility',
            'icon': 'fa-universal-access',
            'tone': 'blue',
            'items': [
                'Increase text contrast for all helper labels.',
                'Add keyboard shortcuts for primary actions.',
                'Support full screen-reader labels for icon buttons.',
                'Provide larger touch targets on mobile.',
                'Add a reduced-motion mode for animations.',
                'Announce important status changes with ARIA live regions.',
                'Ensure forms explain required fields clearly.',
                'Add text-only fallback hints under image cards.',
                'Allow font scaling without breaking layouts.',
                'Improve focus outlines on all interactive controls.',
            ],
        },
        {
            'title': 'Analytics',
            'icon': 'fa-chart-line',
            'tone': 'amber',
            'items': [
                'Show weekly rescue trends on a dashboard.',
                'Add a meals-saved-over-time chart.',
                'Display donor response time averages.',
                'Track partner acceptance and completion rates.',
                'Show top-performing food categories.',
                'Add a map heat layer for busy locations.',
                'Compare donation supply against user demand.',
                'Show support backlog trends over time.',
                'Add daily and monthly summary toggles.',
                'Highlight the most active community zones.',
            ],
        },
        {
            'title': 'Security',
            'icon': 'fa-lock',
            'tone': 'red',
            'items': [
                'Add stronger password rules on registration.',
                'Use OTP verification for sensitive actions.',
                'Mask private phone numbers where appropriate.',
                'Add session timeout warnings before logout.',
                'Log admin approval actions in an audit trail.',
                'Show document upload status securely.',
                'Limit repeated login attempts.',
                'Add warnings for public device sessions.',
                'Protect document URLs with tighter access rules.',
                'Add audit views for account changes and edits.',
            ],
        },
        {
            'title': 'Community Growth',
            'icon': 'fa-people-group',
            'tone': 'purple',
            'items': [
                'Add referral sharing for users and donors.',
                'Show badges for frequent contributors.',
                'Create a community leaderboard by impact.',
                'Add local campaign pages for neighborhoods.',
                'Feature success stories on the homepage.',
                'Let organizations join as partner groups.',
                'Add seasonal campaign banners.',
                'Show a monthly community milestone card.',
                'Enable sharing donation results to social media.',
                'Create volunteer spotlight sections.',
            ],
        },
    ]

    suggestions = []
    for section_index, section in enumerate(sections, start=1):
        for item_index, item in enumerate(section['items'], start=1):
            suggestions.append({
                'id': f"{section_index:02d}-{item_index:02d}",
                'section': section['title'],
                'icon': section['icon'],
                'tone': section['tone'],
                'text': item,
            })
    return sections, suggestions


@app.route('/impact')
def public_impact():
    """Public impact page with live stats and community metrics."""
    summary = _get_public_impact_summary()
    overview = summary['overview']
    rescue_progress = 0
    if overview['total_donations']:
        rescue_progress = min(100, round((overview['available_donations'] / overview['total_donations']) * 100))
    elif overview['completed_requests']:
        rescue_progress = min(100, round(min(overview['completed_requests'], 100)))
    return render_template(
        'impact.html',
        platform_stats=[
            {'label': 'Donations', 'value': overview['total_donations'], 'hint': 'all time listings'},
            {'label': 'Meals saved', 'value': overview['meals_saved'], 'hint': 'rescued through the platform'},
            {'label': 'Active partners', 'value': overview['active_partners'], 'hint': 'ready for pickup'},
            {'label': 'Live listings', 'value': overview['available_donations'], 'hint': 'available in the last 8 hours'},
        ],
        rescue_progress=rescue_progress,
        completed_requests=overview['completed_requests'],
        featured_donations=overview['featured_donations'],
        top_locations=overview['top_locations'],
        weekly_activity=summary['weekly_activity'],
        top_restaurants=summary['top_restaurants'],
        status_breakdown=summary['status_breakdown'],
        weekly_peak=summary['weekly_peak'],
        status_peak=summary['status_peak'],
    )


def _partner_job_priority(elapsed_minutes):
    if elapsed_minutes >= 120:
        return 'Urgent'
    if elapsed_minutes >= 45:
        return 'High'
    return 'Normal'


def _partner_delivery_status_label(partner, active_job_count=0):
    raw_status = clean_text((partner or {}).get('delivery_status'))
    normalized = raw_status.lower()
    if normalized not in {'available', 'busy', 'offline'}:
        if active_job_count:
            return 'Busy'
        return 'Available' if bool((partner or {}).get('is_available', True)) else 'Offline'
    if normalized == 'available' and active_job_count:
        return 'Busy'
    return raw_status.title()


def _partner_delivery_status_badge(status_label):
    status = clean_text(status_label).lower()
    if status == 'busy':
        return 'bg-warning text-dark'
    if status == 'offline':
        return 'bg-danger'
    return 'bg-success'


def _partner_earning_per_trip():
    try:
        return float(os.environ.get('PARTNER_EARNING_PER_TRIP', '50.0') or 50.0)
    except (TypeError, ValueError):
        return 50.0


def _partner_earnings_summary(cursor, partner_id, completed_jobs_count):
    earning_per_trip = _partner_earning_per_trip()
    estimated_earnings = round(completed_jobs_count * earning_per_trip, 2) if earning_per_trip else 0.0
    estimated_earnings_paise = int(round(estimated_earnings * 100))

    cursor.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN status = 'Paid' THEN amount_paise ELSE 0 END), 0) AS paid_delivery_fee_paise,
            COUNT(CASE WHEN status = 'Paid' THEN 1 END) AS paid_delivery_count
        FROM DeliveryFeePayment
        WHERE partner_id = %s
    """, (partner_id,))
    delivery_fee_totals = cursor.fetchone() or {}
    paid_delivery_fee_paise = int(delivery_fee_totals.get('paid_delivery_fee_paise') or 0)
    paid_delivery_count = int(delivery_fee_totals.get('paid_delivery_count') or 0)
    gross_earnings_paise = paid_delivery_fee_paise if paid_delivery_fee_paise > 0 else estimated_earnings_paise

    cursor.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN status IN ('Created', 'Validating', 'ValidationPending', 'Queued', 'Pending', 'Processing', 'Processed') THEN amount_paise ELSE 0 END), 0) AS reserved_paise,
            COALESCE(SUM(CASE WHEN status = 'Processed' THEN amount_paise ELSE 0 END), 0) AS paid_paise
        FROM DeliveryPartnerPaymentLog
        WHERE partner_id = %s
    """, (partner_id,))
    payout_totals = cursor.fetchone() or {}
    reserved_paise = int(payout_totals.get('reserved_paise') or 0)
    paid_paise = int(payout_totals.get('paid_paise') or 0)
    available_paise = max(0, gross_earnings_paise - reserved_paise)

    return {
        'estimated_total': round(gross_earnings_paise / 100, 2),
        'estimated_total_paise': gross_earnings_paise,
        'legacy_estimated_total': estimated_earnings,
        'paid_delivery_fee_total': round(paid_delivery_fee_paise / 100, 2),
        'paid_delivery_count': paid_delivery_count,
        'earning_per_trip': round(earning_per_trip, 2),
        'configured': paid_delivery_fee_paise > 0 or earning_per_trip > 0,
        'paid_total': round(paid_paise / 100, 2),
        'reserved_total': round(reserved_paise / 100, 2),
        'available_total': round(available_paise / 100, 2),
        'available_paise': available_paise,
    }


def _razorpayx_credentials():
    key_id = (os.environ.get('RAZORPAYX_KEY_ID') or os.environ.get('RAZORPAY_KEY_ID') or '').strip()
    key_secret = (os.environ.get('RAZORPAYX_KEY_SECRET') or os.environ.get('RAZORPAY_KEY_SECRET') or '').strip()
    account_number = (os.environ.get('RAZORPAYX_ACCOUNT_NUMBER') or '').strip()
    return key_id, key_secret, account_number


def _razorpayx_request(path, payload, idempotency_key=None):
    key_id, key_secret, _ = _razorpayx_credentials()
    if not key_id or not key_secret:
        return None, "RazorpayX is not configured. Set RAZORPAYX_KEY_ID and RAZORPAYX_KEY_SECRET."

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Basic " + base64.b64encode(f"{key_id}:{key_secret}".encode("utf-8")).decode("utf-8"),
    }
    if idempotency_key:
        headers["X-Payout-Idempotency"] = idempotency_key

    req = urllib.request.Request(
        f"https://api.razorpay.com{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as err:
        try:
            error_body = err.read().decode("utf-8")
        except Exception:
            error_body = str(err)
        return None, f"RazorpayX request failed: {error_body}"
    except Exception as err:
        return None, f"RazorpayX request failed: {err}"


def _validate_partner_upi_with_razorpayx(partner, reference_id):
    _, _, account_number = _razorpayx_credentials()
    if not account_number:
        return None, "RazorpayX source account is not configured. Set RAZORPAYX_ACCOUNT_NUMBER."

    payload = {
        "source_account_number": account_number,
        "validation_type": "pennydrop",
        "reference_id": reference_id[:40],
        "notes": {
            "partner_id": str(partner['id']),
            "purpose": "delivery_partner_upi_validation",
        },
        "fund_account": {
            "account_type": "vpa",
            "vpa": {
                "address": partner['upi_id'],
            },
            "contact": {
                "name": partner['name'],
                "email": partner.get('email') or f"partner{partner['id']}@example.com",
                "contact": partner.get('phone') or "",
                "type": "employee",
                "reference_id": f"partner_{partner['id']}",
            },
        },
    }
    return _razorpayx_request("/v1/fund_accounts/validations", payload)


def _create_partner_upi_payout(partner, amount_paise, fund_account_id, reference_id, idempotency_key):
    _, _, account_number = _razorpayx_credentials()
    if not account_number:
        return None, "RazorpayX source account is not configured. Set RAZORPAYX_ACCOUNT_NUMBER."

    payload = {
        "account_number": account_number,
        "fund_account_id": fund_account_id,
        "amount": int(amount_paise),
        "currency": "INR",
        "mode": "UPI",
        "purpose": "payout",
        "queue_if_low_balance": True,
        "reference_id": reference_id[:40],
        "narration": "Delivery payout",
        "notes": {
            "partner_id": str(partner['id']),
            "partner_name": partner['name'],
        },
    }
    return _razorpayx_request("/v1/payouts", payload, idempotency_key=idempotency_key)


def _partner_route_url(delivery_address, restaurant_location, delivery_lat=None, delivery_lon=None):
    restaurant_location = (restaurant_location or '').strip()
    delivery_address = (delivery_address or '').strip()
    if not restaurant_location:
        return None
    if delivery_lat is not None and delivery_lon is not None:
        return (
            "https://www.google.com/maps/dir/?api=1"
            f"&origin={quote(restaurant_location)}"
            f"&destination={float(delivery_lat):.7f},{float(delivery_lon):.7f}"
            "&travelmode=driving"
        )
    if delivery_address:
        return (
            "https://www.google.com/maps/dir/?api=1"
            f"&origin={quote(restaurant_location)}"
            f"&destination={quote(delivery_address)}"
            "&travelmode=driving"
        )
    return f"https://www.google.com/maps/search/?api=1&query={quote(restaurant_location)}"


def _delivery_eta_minutes(distance_km):
    if distance_km is None:
        return None
    try:
        return max(1, int(round(float(distance_km) / 25 * 60)))
    except (TypeError, ValueError):
        return None


def _partner_navigation_url(partner_lat, partner_lon, destination_text):
    destination_text = (destination_text or '').strip()
    if partner_lat is None or partner_lon is None or not destination_text:
        return None
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={partner_lat:.7f},{partner_lon:.7f}"
        f"&destination={quote(destination_text)}"
        "&travelmode=driving"
    )


def _haversine_km(lat1, lon1, lat2, lon2):
    try:
        lat1 = float(lat1)
        lon1 = float(lon1)
        lat2 = float(lat2)
        lon2 = float(lon2)
    except (TypeError, ValueError):
        return None

    radius_km = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * radius_km * asin(min(1, sqrt(a)))


def _generate_delivery_otp():
    return f"{uuid.uuid4().int % 1000000:06d}"


def _delivery_request_condition(alias='r'):
    """Treat legacy rows with delivery fields set as delivery jobs too."""
    return f"""
        COALESCE(NULLIF(TRIM({alias}.delivery_mode), ''),
                 CASE
                     WHEN NULLIF(TRIM({alias}.delivery_order_id), '') IS NOT NULL
                          OR NULLIF(TRIM({alias}.delivery_address), '') IS NOT NULL
                          OR ({alias}.delivery_latitude IS NOT NULL AND {alias}.delivery_longitude IS NOT NULL)
                     THEN 'Delivery'
                     ELSE 'Pickup'
                 END
        ) = 'Delivery'
    """


def _normalize_partner_area_text(text):
    value = re.sub(r'[^a-z0-9]+', ' ', str(text or '').lower()).strip()
    return re.sub(r'\s+', ' ', value)


def _partner_zone_matches_job(partner, row):
    partner_zone = _normalize_partner_area_text((partner or {}).get('zone'))
    if not partner_zone:
        return True

    candidate_parts = [
        row.get('restaurant_location'),
        row.get('delivery_address'),
        row.get('restaurant_name'),
    ]
    candidate = _normalize_partner_area_text(' '.join(str(part or '') for part in candidate_parts))
    if not candidate:
        return True

    if partner_zone in candidate or candidate in partner_zone:
        return True

    partner_tokens = [token for token in partner_zone.split(' ') if len(token) >= 3]
    return any(token in candidate for token in partner_tokens)


def _partner_map_url(label_text, latitude=None, longitude=None):
    if latitude is not None and longitude is not None:
        try:
            return f"https://www.google.com/maps/search/?api=1&query={float(latitude):.6f},{float(longitude):.6f}"
        except (TypeError, ValueError):
            pass
    label_text = (label_text or '').strip()
    if label_text:
        return f"https://www.google.com/maps/search/?api=1&query={quote(label_text)}"
    return None


def _normalize_partner_rows(rows, partner=None):
    jobs = []
    partner_lat = (partner or {}).get('current_latitude')
    partner_lon = (partner or {}).get('current_longitude')
    for row in rows or []:
        request_time = row.get('request_time')
        accepted_at = row.get('accepted_at')
        pickup_reached_at = row.get('pickup_reached_at')
        delivered_at = row.get('delivered_at')
        base_time = accepted_at or request_time
        elapsed_minutes = 0
        if base_time:
            try:
                elapsed_minutes = max(0, int((datetime.datetime.now() - base_time).total_seconds() // 60))
            except Exception:
                elapsed_minutes = 0
        destination_coords = None
        is_delivery_job = bool(row.get('is_delivery_job'))
        if row.get('delivery_mode') == 'Delivery':
            is_delivery_job = True
        is_pickup_job = not is_delivery_job and not row.get('delivery_partner_id')
        destination_lat = row.get('delivery_latitude')
        destination_lon = row.get('delivery_longitude')
        if not is_pickup_job and destination_lat is not None and destination_lon is not None:
            destination_coords = (float(destination_lat), float(destination_lon))
        elif is_pickup_job and row.get('restaurant_location'):
            destination_coords = geocode_location_server(row.get('restaurant_location'))
        elif row.get('delivery_address'):
            destination_coords = geocode_location_server(row.get('delivery_address'))

        delivery_distance_km = None
        delivery_eta_minutes = None
        if partner_lat is not None and partner_lon is not None and destination_coords:
            delivery_distance_km = round(
                _haversine_km(
                    float(partner_lat),
                    float(partner_lon),
                    float(destination_coords[0]),
                    float(destination_coords[1]),
                ),
                2,
            )
            delivery_eta_minutes = _delivery_eta_minutes(delivery_distance_km)
        jobs.append({
            'request_id': row.get('request_id'),
            'requested_amt': row.get('requested_amt'),
            'request_time': request_time.strftime('%Y-%m-%d %H:%M') if request_time else '',
            'accepted_at': accepted_at.strftime('%Y-%m-%d %H:%M') if accepted_at else '',
            'pickup_reached_at': pickup_reached_at.strftime('%Y-%m-%d %H:%M') if pickup_reached_at else '',
            'out_for_delivery_at': row.get('out_for_delivery_at').strftime('%Y-%m-%d %H:%M') if row.get('out_for_delivery_at') else '',
            'delivered_at': delivered_at.strftime('%Y-%m-%d %H:%M') if delivered_at else '',
            'status': row.get('status'),
            'delivery_mode': row.get('delivery_mode') or ('Delivery' if is_delivery_job else 'Pickup'),
            'delivery_charge_mode': row.get('delivery_charge_mode') or '',
            'delivery_fee_paise': row.get('delivery_fee_paise'),
            'is_assigned': bool(row.get('delivery_partner_id')),
            'delivery_otp': row.get('delivery_otp'),
            'otp_attempt_count': int(row.get('otp_attempt_count') or 0),
            'otp_locked_at': row.get('otp_locked_at').strftime('%Y-%m-%d %H:%M') if row.get('otp_locked_at') else '',
            'food_ready_at': row.get('food_ready_at').strftime('%Y-%m-%d %H:%M') if row.get('food_ready_at') else '',
            'item_name': row.get('item_name'),
            'restaurant_name': row.get('restaurant_name'),
            'restaurant_location': row.get('restaurant_location') or '',
            'delivery_address': row.get('delivery_address') or '',
            'otp_verified_at': row.get('otp_verified_at').strftime('%Y-%m-%d %H:%M') if row.get('otp_verified_at') else '',
            'delivery_latitude': float(destination_coords[0]) if destination_coords else None,
            'delivery_longitude': float(destination_coords[1]) if destination_coords else None,
            'pickup_map_url': _partner_map_url(row.get('restaurant_location') or row.get('restaurant_name'), row.get('restaurant_latitude'), row.get('restaurant_longitude')),
            'drop_map_url': _partner_map_url(row.get('delivery_address') or row.get('restaurant_location') or row.get('restaurant_name'), destination_coords[0] if destination_coords else None, destination_coords[1] if destination_coords else None),
            'distance_km': delivery_distance_km,
            'eta_minutes': delivery_eta_minutes,
            'elapsed_minutes': elapsed_minutes,
            'priority': _partner_job_priority(elapsed_minutes),
            'delivery_partner_rating': row.get('delivery_partner_rating'),
            'delivery_partner_feedback': row.get('delivery_partner_feedback') or '',
            'delivery_partner_rated_at': row.get('delivery_partner_rated_at').strftime('%Y-%m-%d %H:%M') if row.get('delivery_partner_rated_at') else '',
            'delivery_issue_type': row.get('delivery_issue_type') or '',
            'delivery_issue_role': row.get('delivery_issue_role') or '',
            'delivery_issue_detail': row.get('delivery_issue_detail') or '',
            'delivery_issue_reported_at': row.get('delivery_issue_reported_at').strftime('%Y-%m-%d %H:%M') if row.get('delivery_issue_reported_at') else '',
            'route_url': _partner_route_url(
                row.get('delivery_address'),
                row.get('restaurant_location'),
                destination_coords[0] if destination_coords else None,
                destination_coords[1] if destination_coords else None,
            ),
        })
    return jobs


def _get_partner_dashboard_data(partner_id):
    conn = get_db_connection()
    if not conn:
        return None
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM DeliveryPartner WHERE id = %s AND COALESCE(is_deleted, FALSE) = FALSE", (partner_id,))
    partner = cursor.fetchone()
    if not partner:
        cursor.close()
        conn.close()
        return None

    cursor.execute(f"""
        SELECT
            r.id AS request_id,
            r.requested_amt,
            r.request_time,
            r.accepted_at,
            r.food_ready_at,
            r.pickup_reached_at,
            r.out_for_delivery_at,
            r.delivered_at,
            r.delivery_otp,
            r.otp_verified_at,
            r.otp_attempt_count,
            r.otp_locked_at,
            r.status,
            r.delivery_partner_id,
            r.delivery_address,
            r.delivery_charge_mode,
            r.delivery_fee_paise,
            r.delivery_latitude,
            r.delivery_longitude,
            r.delivery_issue_type,
            r.delivery_issue_role,
            r.delivery_issue_detail,
            r.delivery_issue_reported_at,
            rest.latitude AS restaurant_latitude,
            rest.longitude AS restaurant_longitude,
            1 AS is_delivery_job,
            d.item_name,
            rest.name AS restaurant_name,
            rest.location AS restaurant_location
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        JOIN Restaurant rest ON d.restaurant_id = rest.id
        WHERE { _delivery_request_condition('r') }
          AND r.status = 'Accepted'
          AND r.delivery_partner_id IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM DeliveryPartnerRejection dpr
              WHERE dpr.request_id = r.id
                AND dpr.partner_id = %s
          )
        ORDER BY COALESCE(r.accepted_at, r.request_time) ASC, r.id ASC
    """, (partner_id,))
    all_available_jobs = _normalize_partner_rows(cursor.fetchall(), partner)
    available_jobs = [
        job for job in all_available_jobs
        if bool(partner.get('is_available', True)) and _partner_zone_matches_job(partner, job)
    ]
    zone_notice = None
    if partner.get('zone') and all_available_jobs and not available_jobs:
        available_jobs = all_available_jobs
        zone_notice = (
            f"No delivery jobs matched the zone '{partner.get('zone')}'. "
            "Showing all available delivery jobs so you do not miss active orders."
        )

    cursor.execute(f"""
        SELECT
            r.id AS request_id,
            r.requested_amt,
            r.request_time,
            r.accepted_at,
            r.food_ready_at,
            r.pickup_reached_at,
            r.out_for_delivery_at,
            r.delivered_at,
            r.delivery_otp,
            r.otp_verified_at,
            r.otp_attempt_count,
            r.otp_locked_at,
            r.status,
            r.delivery_partner_id,
            r.delivery_address,
            r.delivery_charge_mode,
            r.delivery_fee_paise,
            r.delivery_latitude,
            r.delivery_longitude,
            r.delivery_issue_type,
            r.delivery_issue_role,
            r.delivery_issue_detail,
            r.delivery_issue_reported_at,
            rest.latitude AS restaurant_latitude,
            rest.longitude AS restaurant_longitude,
            1 AS is_delivery_job,
            d.item_name,
            rest.name AS restaurant_name,
            rest.location AS restaurant_location
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        JOIN Restaurant rest ON d.restaurant_id = rest.id
        WHERE { _delivery_request_condition('r') }
          AND r.delivery_partner_id = %s
          AND r.status IN ('Accepted', 'OutForDelivery')
        ORDER BY CASE
                    WHEN r.status = 'OutForDelivery' THEN 0
                    ELSE 1
                 END,
                 COALESCE(r.out_for_delivery_at, r.accepted_at, r.request_time) DESC,
                 r.id DESC
    """, (partner_id,))
    active_jobs = _normalize_partner_rows(cursor.fetchall(), partner)

    cursor.execute(f"""
        SELECT
            r.id AS request_id,
            r.requested_amt,
            r.request_time,
            r.accepted_at,
            r.food_ready_at,
            r.pickup_reached_at,
            r.out_for_delivery_at,
            r.delivered_at,
            r.delivery_partner_rating,
            r.delivery_partner_feedback,
            r.delivery_partner_rated_at,
            r.otp_attempt_count,
            r.otp_locked_at,
            r.status,
            r.delivery_partner_id,
            r.delivery_address,
            r.delivery_charge_mode,
            r.delivery_fee_paise,
            r.delivery_latitude,
            r.delivery_longitude,
            r.delivery_issue_type,
            r.delivery_issue_role,
            r.delivery_issue_detail,
            r.delivery_issue_reported_at,
            rest.latitude AS restaurant_latitude,
            rest.longitude AS restaurant_longitude,
            1 AS is_delivery_job,
            d.item_name,
            rest.name AS restaurant_name,
            rest.location AS restaurant_location
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        JOIN Restaurant rest ON d.restaurant_id = rest.id
        WHERE { _delivery_request_condition('r') }
          AND r.delivery_partner_id = %s
          AND r.status = 'Delivered'
        ORDER BY COALESCE(r.delivered_at, r.accepted_at, r.request_time) DESC, r.id DESC
    """, (partner_id,))
    completed_jobs = _normalize_partner_rows(cursor.fetchall(), partner)

    cursor.execute("""
        SELECT dpr.request_id,
               dpr.rejection_reason,
               dpr.created_at AS rejected_at,
               r.requested_amt,
               r.request_time,
               r.accepted_at,
               r.status,
               r.delivery_address,
               r.delivery_charge_mode,
               r.delivery_fee_paise,
               r.delivery_latitude,
               r.delivery_longitude,
               rest.latitude AS restaurant_latitude,
               rest.longitude AS restaurant_longitude,
               1 AS is_delivery_job,
               d.item_name,
               rest.name AS restaurant_name,
               rest.location AS restaurant_location
        FROM DeliveryPartnerRejection dpr
        JOIN FoodRequest r ON r.id = dpr.request_id
        JOIN Donation d ON r.donation_id = d.id
        JOIN Restaurant rest ON d.restaurant_id = rest.id
        WHERE dpr.partner_id = %s
        ORDER BY dpr.created_at DESC
        LIMIT 20
    """, (partner_id,))
    rejected_rows = cursor.fetchall()
    rejected_jobs = _normalize_partner_rows(rejected_rows, partner)
    for job, row in zip(rejected_jobs, rejected_rows):
        job['rejection_reason'] = row.get('rejection_reason') or 'No reason provided'
        job['rejected_at'] = row.get('rejected_at').strftime('%Y-%m-%d %H:%M') if row.get('rejected_at') else ''

    cursor.execute(f"""
        SELECT
            SUM(CASE WHEN r.delivery_partner_id = %s AND r.status = 'Accepted' AND { _delivery_request_condition('r') } THEN 1 ELSE 0 END) AS accepted_count,
            SUM(CASE WHEN r.delivery_partner_id = %s AND r.status = 'OutForDelivery' AND { _delivery_request_condition('r') } THEN 1 ELSE 0 END) AS out_for_delivery_count,
            SUM(CASE WHEN r.delivery_partner_id = %s AND r.status = 'Delivered' AND { _delivery_request_condition('r') } THEN 1 ELSE 0 END) AS delivered_count
        FROM FoodRequest r
        WHERE { _delivery_request_condition('r') }
    """, (partner_id, partner_id, partner_id))
    partner_stats = cursor.fetchone() or {}

    def _safe_time_label(value):
        if not value:
            return None
        try:
            if hasattr(value, 'strftime'):
                return value.strftime('%Y-%m-%d')
            return str(value)[:10]
        except Exception:
            return None

    total_jobs = len(available_jobs) + len(active_jobs) + len(completed_jobs)
    avg_wait_minutes = 0
    avg_eta_minutes = 0
    urgent_jobs = 0
    high_jobs = 0
    today_label = datetime.datetime.now().strftime('%Y-%m-%d')
    completed_today = 0

    active_elapsed = []
    active_eta = []
    for job in available_jobs + active_jobs:
        elapsed_minutes = int(job.get('elapsed_minutes') or 0)
        eta_minutes = job.get('eta_minutes')
        active_elapsed.append(elapsed_minutes)
        if eta_minutes is not None:
            active_eta.append(int(eta_minutes))
        if job.get('priority') == 'Urgent':
            urgent_jobs += 1
        elif job.get('priority') == 'High':
            high_jobs += 1

    for job in completed_jobs:
        delivered_day = _safe_time_label(job.get('delivered_at'))
        if delivered_day == today_label:
            completed_today += 1

    delivery_minutes = []
    late_pickups = 0
    for job in active_jobs + completed_jobs:
        elapsed_minutes = int(job.get('elapsed_minutes') or 0)
        if job.get('status') == 'Accepted' and elapsed_minutes >= 30 and not job.get('pickup_reached_at'):
            late_pickups += 1
    for row in completed_jobs:
        try:
            accepted_label = row.get('accepted_at')
            delivered_label = row.get('delivered_at')
            if accepted_label and delivered_label:
                accepted_dt = datetime.datetime.strptime(accepted_label, '%Y-%m-%d %H:%M')
                delivered_dt = datetime.datetime.strptime(delivered_label, '%Y-%m-%d %H:%M')
                delivery_minutes.append(max(0, int((delivered_dt - accepted_dt).total_seconds() // 60)))
        except Exception:
            pass

    if active_elapsed:
        avg_wait_minutes = round(sum(active_elapsed) / len(active_elapsed))
    if active_eta:
        avg_eta_minutes = round(sum(active_eta) / len(active_eta))

    delivery_flow = {
        'available_count': len(available_jobs),
        'active_count': len(active_jobs),
        'completed_count': len(completed_jobs),
        'rejected_count': len(rejected_jobs),
        'urgent_count': urgent_jobs,
        'high_count': high_jobs,
        'completed_today': completed_today,
        'total_jobs': total_jobs,
        'late_pickups': late_pickups,
        'avg_delivery_minutes': round(sum(delivery_minutes) / len(delivery_minutes)) if delivery_minutes else 0,
    }

    total_trips = len(completed_jobs)
    earnings_summary = _partner_earnings_summary(cursor, partner_id, total_trips)
    estimated_earnings = earnings_summary['estimated_total']

    trip_summary = {
        'total_trips': total_trips,
        'completed_today': completed_today,
        'active_trips': len(active_jobs),
        'available_trips': len(available_jobs),
        'success_rate': round((len(completed_jobs) / total_trips) * 100) if total_trips else 0,
    }

    rating_values = [int(job.get('delivery_partner_rating')) for job in completed_jobs if str(job.get('delivery_partner_rating') or '').strip().isdigit()]
    history_summary = {
        'avg_rating': round(sum(rating_values) / len(rating_values), 1) if rating_values else None,
        'rating_count': len(rating_values),
        'completed_jobs': len(completed_jobs),
        'estimated_earnings': estimated_earnings,
    }

    performance_snapshot = {
        'avg_wait_minutes': avg_wait_minutes,
        'avg_eta_minutes': avg_eta_minutes,
        'availability_rate': round((len(active_jobs) / total_jobs) * 100) if total_jobs else 0,
        'completion_rate': round((len(completed_jobs) / total_jobs) * 100) if total_jobs else 0,
        'late_pickups': late_pickups,
        'avg_delivery_minutes': delivery_flow['avg_delivery_minutes'],
        'reliability_score': max(0, min(100, round(((int(partner_stats.get('delivered_count') or 0) + int(partner_stats.get('out_for_delivery_count') or 0)) / max(1, int(partner_stats.get('accepted_count') or 0) + int(partner_stats.get('delivered_count') or 0))) * 100))),
        'priority_mix': {
            'urgent': urgent_jobs,
            'high': high_jobs,
            'normal': max(len(available_jobs) + len(active_jobs) - urgent_jobs - high_jobs, 0),
        },
    }

    cursor.close()
    conn.close()
    return {
        'partner': partner,
        'partner_status': _partner_delivery_status_label(partner, len(active_jobs)),
        'available_jobs': available_jobs,
        'active_jobs': active_jobs,
        'completed_jobs': completed_jobs,
        'rejected_jobs': rejected_jobs,
        'partner_stats': partner_stats,
        'delivery_flow': delivery_flow,
        'performance_snapshot': performance_snapshot,
        'trip_summary': trip_summary,
        'earnings_summary': earnings_summary,
        'history_summary': history_summary,
        'zone_notice': zone_notice,
    }


def _get_partner_status(partner_id):
    conn = get_db_connection()
    if not conn:
        return None
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT id, name, is_available, is_active, application_status, verification_remarks
        FROM DeliveryPartner
        WHERE id = %s AND COALESCE(is_deleted, FALSE) = FALSE
    """, (partner_id,))
    partner = cursor.fetchone()
    cursor.close()
    conn.close()
    return partner


def _partner_can_access_portal(partner):
    if not partner:
        return False
    status = (partner.get('application_status') or 'Submitted').lower()
    if status in {'submitted', 'rejected'}:
        return True
    return bool(partner.get('is_active', True))


@app.route('/partner')
def partner_home():
    if 'partner_id' not in session:
        return render_template('delivery_partner_login.html')
    partner = _get_partner_status(session['partner_id'])
    if not partner:
        session.pop('partner_id', None)
        session.pop('partner_name', None)
        flash('Your delivery partner session expired. Please log in again.', 'warning')
        return render_template('delivery_partner_login.html')
    if not _partner_can_access_portal(partner):
        session.pop('partner_id', None)
        session.pop('partner_name', None)
        flash('Your delivery partner account is inactive.', 'warning')
        return render_template('delivery_partner_login.html')
    return redirect(url_for('partner_dashboard'))


@app.route('/partner/login', methods=['GET', 'POST'])
def partner_login():
    if 'partner_id' in session:
        partner = _get_partner_status(session['partner_id'])
        if _partner_can_access_portal(partner):
            return redirect(url_for('partner_dashboard'))
        session.pop('partner_id', None)
        session.pop('partner_name', None)

    if request.method == 'POST':
        username = clean_text(request.form.get('username'))
        password = clean_text(request.form.get('password'))
        missing_fields = validate_required_fields({
            'Username': username,
            'Password': password,
        })
        if missing_fields:
            flash(f"Please complete: {', '.join(missing_fields)}.", 'danger')
            return redirect(url_for('partner_login'))

        try:
            with db_cursor(dictionary=True) as cursor:
                cursor.execute("""
                    SELECT id, name, username, password, is_available, is_active, application_status, verification_remarks
                    FROM DeliveryPartner
                    WHERE TRIM(username) = %s AND COALESCE(is_deleted, FALSE) = FALSE
                    ORDER BY CASE WHEN username = %s THEN 0 ELSE 1 END
                    LIMIT 1
                """, (username, username))
                partner = cursor.fetchone()
                password_ok, should_upgrade = _password_matches(partner.get('password') if partner else None, password)
                if partner and password_ok and should_upgrade:
                    cursor.execute("UPDATE DeliveryPartner SET password = %s WHERE id = %s", (generate_password_hash(password), partner['id']))
                if partner and password_ok and partner.get('username') != username:
                    cursor.execute("UPDATE DeliveryPartner SET username = %s WHERE id = %s", (username, partner['id']))
                if partner and not password_ok:
                    partner = None
        except DatabaseUnavailableError:
            return "Database Connection Failed.", 500

        if _partner_can_access_portal(partner):
            session.permanent = True
            session['partner_id'] = partner['id']
            session['partner_name'] = partner['name']
            status = (partner.get('application_status') or 'Submitted').lower()
            if status == 'rejected' and partner.get('verification_remarks'):
                flash(f"Your application was rejected: {partner.get('verification_remarks')}", 'danger')
            elif status == 'rejected':
                flash('Your delivery partner application was rejected by admin.', 'danger')
            elif status != 'approved':
                flash('Your delivery partner application is waiting for admin approval.', 'warning')
            else:
                flash('Logged in successfully.', 'success')
            return redirect(url_for('partner_dashboard'))
        elif partner and not partner.get('is_active', True):
            flash('This delivery partner account is inactive.', 'warning')
        else:
            flash('Invalid credentials.', 'danger')

    return render_template('delivery_partner_login.html')


@app.route('/partner/register', methods=['GET', 'POST'])
def partner_register():
    if 'partner_id' in session:
        return redirect(url_for('partner_dashboard'))

    if request.method == 'POST':
        password = clean_text(request.form.get('password'))
        confirm_password = clean_text(request.form.get('confirm_password'))
        username = clean_text(request.form.get('username'))
        missing_fields = validate_required_fields({
            'Name': request.form.get('name'),
            'Phone': request.form.get('phone'),
            'Vehicle Type': request.form.get('vehicle_type'),
            'Username': username,
            'Password': password,
            'Confirm Password': confirm_password,
        })
        if missing_fields:
            flash(f"Please complete: {', '.join(missing_fields)}.", 'danger')
            return redirect(url_for('partner_register'))

        password_error = validate_password_pair(password, confirm_password)
        if password_error:
            flash(password_error, 'danger')
            return redirect(url_for('partner_register'))

        vehicle_type = (request.form.get('vehicle_type') or '').strip()
        requires_license = _vehicle_requires_license(vehicle_type)
        required_docs = {
            'identity_document': 'Identity / address proof',
            'pan_card': 'PAN card',
            'profile_photo': 'Profile photo',
            'vehicle_rc': 'Vehicle RC',
            'bank_document': 'Bank document',
        }
        if requires_license:
            required_docs['driving_license'] = 'Driving license'

        missing_docs = [label for key, label in required_docs.items() if not request.files.get(key) or not request.files[key].filename]
        if missing_docs:
            flash(f"Please upload: {', '.join(missing_docs)}.", 'warning')
            return redirect(url_for('partner_register'))

        try:
            with db_cursor(dictionary=True) as cursor:
                cursor.execute("SELECT id FROM DeliveryPartner WHERE TRIM(username) = %s", (username,))
                if cursor.fetchone():
                    flash('Username already exists. Choose another.', 'danger')
                    return redirect(url_for('partner_register'))

                identity_doc = request.files.get('identity_document')
                pan_card = request.files.get('pan_card')
                profile_photo = request.files.get('profile_photo')
                driving_license = request.files.get('driving_license') if requires_license else None
                vehicle_rc = request.files.get('vehicle_rc')
                bank_document = request.files.get('bank_document')

                saved_identity_doc = _save_partner_doc_upload(identity_doc, 'identity')
                saved_pan_card = _save_partner_doc_upload(pan_card, 'pan')
                saved_profile_photo = _save_partner_doc_upload(profile_photo, 'profile')
                saved_driving_license = _save_partner_doc_upload(driving_license, 'dl') if requires_license else None
                saved_vehicle_rc = _save_partner_doc_upload(vehicle_rc, 'rc')
                saved_bank_document = _save_partner_doc_upload(bank_document, 'bank')

                if not all([saved_identity_doc, saved_pan_card, saved_profile_photo, saved_vehicle_rc, saved_bank_document]) or (requires_license and not saved_driving_license):
                    flash('One or more partner documents are invalid. Use png, jpg, jpeg, or pdf.', 'danger')
                    return redirect(url_for('partner_register'))

                cursor.execute("""
                    INSERT INTO DeliveryPartner (
                        name, phone, email, zone, vehicle_type, username, password,
                        is_available, is_active, application_status, identity_document_type,
                        identity_document_url, profile_photo_url, pan_card_url, driving_license_url,
                        vehicle_rc_url, bank_document_url
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    (request.form.get('name') or '').strip(),
                    (request.form.get('phone') or '').strip(),
                    (request.form.get('email') or '').strip() or None,
                    (request.form.get('zone') or '').strip() or None,
                    vehicle_type or None,
                    username,
                    generate_password_hash(password),
                    False,
                    False,
                    'Submitted',
                    'Aadhaar / Voter Card',
                    saved_identity_doc,
                    saved_profile_photo,
                    saved_pan_card,
                    saved_driving_license,
                    saved_vehicle_rc,
                    saved_bank_document,
                ))
        except DatabaseUnavailableError:
            return "Database Connection Failed.", 500
        flash('Registration submitted. Your documents are waiting for admin approval.', 'success')
        return redirect(url_for('partner_login'))

    return render_template('delivery_partner_register.html')


@app.route('/partner/logout')
def partner_logout():
    session.pop('partner_id', None)
    session.pop('partner_name', None)
    flash('Logged out.', 'info')
    return redirect(url_for('index'))


@app.route('/partner/profile', methods=['POST'])
@require_session_key('partner_id', 'partner_login', 'Please login as a delivery partner first.')
def partner_update_profile():
    if 'partner_id' not in session:
        return redirect(url_for('partner_login'))

    partner_id = session['partner_id']
    name = (request.form.get('name') or '').strip()
    phone = (request.form.get('phone') or '').strip()
    email = (request.form.get('email') or '').strip()
    zone = (request.form.get('zone') or '').strip()
    vehicle_type = (request.form.get('vehicle_type') or '').strip()
    upi_id = (request.form.get('upi_id') or '').strip()
    latitude_raw = (request.form.get('current_latitude') or '').strip()
    longitude_raw = (request.form.get('current_longitude') or '').strip()
    accuracy_raw = (request.form.get('current_accuracy_m') or '').strip()
    update_live_location = bool(latitude_raw or longitude_raw or accuracy_raw)

    if not name or not phone:
        flash('Name and phone number are required.', 'danger')
        return redirect(url_for('partner_dashboard'))

    current_latitude = None
    current_longitude = None
    current_accuracy_m = None
    if update_live_location:
        try:
            current_latitude = float(latitude_raw) if latitude_raw else None
            current_longitude = float(longitude_raw) if longitude_raw else None
            current_accuracy_m = float(accuracy_raw) if accuracy_raw else None
        except ValueError:
            flash('Live location values must be numeric.', 'danger')
            return redirect(url_for('partner_dashboard'))
        if current_latitude is None or current_longitude is None:
            flash('Both latitude and longitude are required for live location updates.', 'danger')
            return redirect(url_for('partner_dashboard'))

    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500

    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id FROM DeliveryPartner WHERE phone = %s AND id <> %s",
        (phone, partner_id)
    )
    duplicate = cursor.fetchone()
    cursor.close()
    if duplicate:
        conn.close()
        flash('Another delivery partner already uses that phone number.', 'warning')
        return redirect(url_for('partner_dashboard'))

    cursor = conn.cursor()
    if update_live_location:
        cursor.execute(
            """
            UPDATE DeliveryPartner
            SET name = %s,
                phone = %s,
                email = %s,
                zone = %s,
                vehicle_type = %s,
                payment_verified = CASE WHEN COALESCE(upi_id, '') = %s THEN payment_verified ELSE FALSE END,
                upi_id = %s,
                current_latitude = %s,
                current_longitude = %s,
                current_accuracy_m = COALESCE(%s, current_accuracy_m),
                current_location_updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (
                name,
                phone,
                email or None,
                zone or None,
                vehicle_type or None,
                upi_id or '',
                upi_id or None,
                current_latitude,
                current_longitude,
                current_accuracy_m,
                partner_id,
            )
        )
    else:
        cursor.execute(
            """
            UPDATE DeliveryPartner
            SET name = %s,
                phone = %s,
                email = %s,
                zone = %s,
                vehicle_type = %s,
                payment_verified = CASE WHEN COALESCE(upi_id, '') = %s THEN payment_verified ELSE FALSE END,
                upi_id = %s
            WHERE id = %s
            """,
            (name, phone, email or None, zone or None, vehicle_type or None, upi_id or '', upi_id or None, partner_id)
        )
    conn.commit()
    cursor.close()
    conn.close()

    session['partner_name'] = name
    flash('Profile updated successfully.', 'success')
    return redirect(url_for('partner_dashboard'))


@app.route('/partner/withdraw', methods=['POST'])
@require_session_key('partner_id', 'partner_login', 'Please login as a delivery partner first.')
def partner_withdraw_to_upi():
    if 'partner_id' not in session:
        return redirect(url_for('partner_login'))

    partner_id = session['partner_id']
    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500

    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT id, name, phone, email, upi_id, is_active, application_status
        FROM DeliveryPartner
        WHERE id = %s AND COALESCE(is_deleted, FALSE) = FALSE
    """, (partner_id,))
    partner = cursor.fetchone()
    if not partner:
        cursor.close()
        conn.close()
        flash('Delivery partner profile not found. Please log in again.', 'danger')
        return redirect(url_for('partner_login'))

    if partner.get('application_status') != 'Approved' or not partner.get('is_active', True):
        cursor.close()
        conn.close()
        flash('Withdrawals are available only for approved active delivery partners.', 'warning')
        return redirect(url_for('partner_dashboard'))

    upi_id = clean_text(partner.get('upi_id'))
    if not upi_id or '@' not in upi_id:
        cursor.close()
        conn.close()
        flash('Add a valid UPI ID in your profile before requesting withdrawal.', 'warning')
        return redirect(url_for('partner_dashboard'))
    partner['upi_id'] = upi_id

    cursor.execute("""
        SELECT COUNT(*) AS count
        FROM FoodRequest
        WHERE delivery_partner_id = %s
          AND status = 'Delivered'
          AND delivery_mode = 'Delivery'
    """, (partner_id,))
    completed_count = int((cursor.fetchone() or {}).get('count') or 0)
    earnings = _partner_earnings_summary(cursor, partner_id, completed_count)
    amount_paise = int(earnings.get('available_paise') or 0)
    if not earnings.get('configured') or amount_paise < 100:
        cursor.close()
        conn.close()
        flash('No withdrawable delivery earnings are available yet.', 'warning')
        return redirect(url_for('partner_dashboard'))

    reference_id = f"dpwd{partner_id}{uuid.uuid4().hex[:8]}"
    idempotency_key = str(uuid.uuid4())
    cursor.execute("""
        INSERT INTO DeliveryPartnerPaymentLog (
            partner_id, payment_mode, amount_paise, payment_reference, upi_id,
            status, idempotency_key, processed_by, remarks
        )
        VALUES (%s, 'RazorpayXUPI', %s, %s, %s, 'Validating', %s, 'Partner', %s)
    """, (
        partner_id,
        amount_paise,
        reference_id,
        upi_id,
        idempotency_key,
        'UPI validation started before payout.',
    ))
    payment_log_id = cursor.lastrowid
    conn.commit()

    validation, validation_error = _validate_partner_upi_with_razorpayx(partner, reference_id)
    if validation_error:
        cursor.execute("""
            UPDATE DeliveryPartnerPaymentLog
            SET status = 'ValidationFailed', remarks = %s
            WHERE id = %s
        """, (validation_error[:255], payment_log_id))
        conn.commit()
        cursor.close()
        conn.close()
        flash(validation_error, 'danger')
        return redirect(url_for('partner_dashboard'))

    validation_status = validation.get('status')
    validation_results = validation.get('validation_results') or {}
    fund_account = validation.get('fund_account') or {}
    fund_account_id = fund_account.get('id')
    account_active = validation_results.get('account_status') == 'active' or fund_account.get('active') is True
    beneficiary_name = validation_results.get('registered_name')
    if validation_status != 'completed' or not account_active or not fund_account_id:
        cursor.execute("""
            UPDATE DeliveryPartnerPaymentLog
            SET status = 'ValidationPending',
                validation_id = %s,
                fund_account_id = %s,
                beneficiary_name = %s,
                remarks = %s
            WHERE id = %s
        """, (
            validation.get('id'),
            fund_account_id,
            beneficiary_name,
            'UPI validation did not complete as active. Payout was not created.',
            payment_log_id,
        ))
        conn.commit()
        cursor.close()
        conn.close()
        flash('UPI validation did not confirm an active account yet. Payout was not created.', 'warning')
        return redirect(url_for('partner_dashboard'))

    payout, payout_error = _create_partner_upi_payout(partner, amount_paise, fund_account_id, reference_id, idempotency_key)
    if payout_error:
        cursor.execute("""
            UPDATE DeliveryPartnerPaymentLog
            SET status = 'PayoutFailed',
                validation_id = %s,
                fund_account_id = %s,
                beneficiary_name = %s,
                remarks = %s
            WHERE id = %s
        """, (
            validation.get('id'),
            fund_account_id,
            beneficiary_name,
            payout_error[:255],
            payment_log_id,
        ))
        conn.commit()
        cursor.close()
        conn.close()
        flash(payout_error, 'danger')
        return redirect(url_for('partner_dashboard'))

    payout_status = (payout.get('status') or 'Created').title()
    cursor.execute("""
        UPDATE DeliveryPartnerPaymentLog
        SET status = %s,
            payout_id = %s,
            validation_id = %s,
            fund_account_id = %s,
            beneficiary_name = %s,
            remarks = %s,
            processed_at = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (
        payout_status,
        payout.get('id'),
        validation.get('id'),
        fund_account_id,
        beneficiary_name,
        f"RazorpayX payout {payout_status.lower()} to validated UPI.",
        payment_log_id,
    ))
    cursor.execute(
        "UPDATE DeliveryPartner SET payment_verified = TRUE WHERE id = %s",
        (partner_id,)
    )
    conn.commit()
    cursor.close()
    conn.close()

    flash(f"Withdrawal requested successfully. RazorpayX payout status: {payout_status}.", 'success')
    return redirect(url_for('partner_dashboard'))


@app.route('/partner/dashboard')
@require_session_key('partner_id', 'partner_login', 'Please login as a delivery partner first.')
def partner_dashboard():
    if 'partner_id' not in session:
        return redirect(url_for('partner_login'))

    partner_state = _get_partner_status(session['partner_id'])
    if not partner_state:
        session.pop('partner_id', None)
        session.pop('partner_name', None)
        flash('Delivery partner profile not found. Please log in again.', 'danger')
        return redirect(url_for('partner_login'))
    if not _partner_can_access_portal(partner_state):
        session.pop('partner_id', None)
        session.pop('partner_name', None)
        flash('This delivery partner account is inactive. Contact admin support.', 'warning')
        return redirect(url_for('partner_login'))

    dashboard_data = _get_partner_dashboard_data(session['partner_id'])
    if not dashboard_data:
        flash('Delivery partner profile not found. Please login again.', 'danger')
        return redirect(url_for('partner_login'))
    partner = dashboard_data['partner']

    session['partner_name'] = partner.get('name')
    return render_template('delivery_partner.html', **dashboard_data)


@app.route('/partner/status', methods=['POST'])
@require_session_key('partner_id', 'partner_login', 'Please login as a delivery partner first.')
def partner_update_status():
    if 'partner_id' not in session:
        return redirect(url_for('partner_login'))

    requested_status = clean_text(request.form.get('status') or request.form.get('delivery_status') or '')
    normalized_status = requested_status.lower()
    if normalized_status not in {'available', 'busy', 'offline'}:
        requested_available = clean_text(request.form.get('is_available')).lower() in {'1', 'true', 'available', 'on', 'yes'}
        normalized_status = 'available' if requested_available else 'offline'

    is_available = normalized_status == 'available'
    db_status = normalized_status.title()

    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, is_active, application_status FROM DeliveryPartner WHERE id = %s", (session['partner_id'],))
    partner = cursor.fetchone()
    if not partner or partner.get('application_status') != 'Approved' or not partner.get('is_active', True):
        conn.close()
        flash('Delivery partner profile not found.', 'danger')
        return redirect(url_for('partner_login'))

    cursor.execute(
        "UPDATE DeliveryPartner SET is_available = %s, delivery_status = %s WHERE id = %s",
        (is_available, db_status, session['partner_id'])
    )
    conn.commit()
    conn.close()

    flash(f'Status updated to {db_status}.', 'success')
    return redirect(url_for('partner_dashboard'))


def _partner_delivery_issue_message(cursor, request_id, partner_id):
    cursor.execute("""
        SELECT r.status, r.delivery_partner_id, dpr.rejection_reason
        FROM FoodRequest r
        LEFT JOIN DeliveryPartnerRejection dpr
               ON dpr.request_id = r.id
              AND dpr.partner_id = %s
        WHERE r.id = %s
        LIMIT 1
    """, (partner_id, request_id))
    row = cursor.fetchone() or {}
    status = clean_text(row.get('status')).lower()
    rejection_reason = clean_text(row.get('rejection_reason'))
    if status == 'delivered':
        return f'Delivery job #{request_id} has already been completed.'
    if status in {'cancelled', 'canceled'}:
        return f'Delivery job #{request_id} was cancelled before pickup.'
    if row.get('delivery_partner_id') and int(row.get('delivery_partner_id')) != int(partner_id):
        return f'Delivery job #{request_id} was already taken by another partner.'
    if rejection_reason:
        return f"You already rejected delivery job #{request_id}: {rejection_reason}"
    return f'Delivery job #{request_id} is no longer available.'


@app.route('/partner/toggle_availability', methods=['POST'])
@require_session_key('partner_id', 'partner_login', 'Please login as a delivery partner first.')
def partner_toggle_availability():
    if 'partner_id' not in session:
        return redirect(url_for('partner_login'))

    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT is_available, is_active, application_status FROM DeliveryPartner WHERE id = %s", (session['partner_id'],))
    partner = cursor.fetchone()
    if not partner or partner.get('application_status') != 'Approved' or not partner.get('is_active', True):
        conn.close()
        flash('Delivery partner profile not found.', 'danger')
        return redirect(url_for('partner_login'))

    current_status = _partner_delivery_status_label(partner, 0).lower()
    next_status = 'Offline' if current_status == 'available' else 'Available'
    cursor.execute(
        "UPDATE DeliveryPartner SET is_available = %s, delivery_status = %s WHERE id = %s",
        (next_status == 'Available', next_status, session['partner_id'])
    )
    conn.commit()
    conn.close()

    flash(f'Status updated to {next_status}.', 'success')
    return redirect(url_for('partner_dashboard'))


@app.route('/partner/set_availability', methods=['POST'])
@require_session_key('partner_id', 'partner_login', 'Please login as a delivery partner first.')
def partner_set_availability():
    if 'partner_id' not in session:
        return redirect(url_for('partner_login'))

    desired_state = (request.form.get('is_available') or '').strip().lower()
    next_state = desired_state in {'1', 'true', 'available', 'on', 'yes'}

    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, is_active, application_status FROM DeliveryPartner WHERE id = %s", (session['partner_id'],))
    partner = cursor.fetchone()
    if not partner or partner.get('application_status') != 'Approved' or not partner.get('is_active', True):
        conn.close()
        flash('Delivery partner profile not found.', 'danger')
        return redirect(url_for('partner_login'))

    status_label = 'Available' if next_state else 'Offline'
    cursor.execute(
        "UPDATE DeliveryPartner SET is_available = %s, delivery_status = %s WHERE id = %s",
        (next_state, status_label, session['partner_id'])
    )
    conn.commit()
    conn.close()

    flash(f'Status updated to {status_label}.', 'success')
    return redirect(url_for('partner_dashboard'))


@app.route('/partner/location/update', methods=['POST'])
@require_session_key('partner_id', 'partner_login', 'Please login as a delivery partner first.')
def partner_location_update():
    if 'partner_id' not in session:
        return jsonify({'success': False, 'error': 'Please login as a delivery partner first.'}), 401

    payload = request.get_json(silent=True) or {}
    try:
        latitude = float(payload.get('latitude'))
        longitude = float(payload.get('longitude'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid coordinates.'}), 400

    accuracy = payload.get('accuracy')
    try:
        accuracy = float(accuracy) if accuracy is not None else None
    except (TypeError, ValueError):
        accuracy = None

    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'error': 'Database connection failed.'}), 500

    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) AS active_delivery_count
        FROM FoodRequest
        WHERE delivery_partner_id = %s
          AND status IN ('Accepted', 'OutForDelivery')
    """, (session['partner_id'],))
    active_row = cursor.fetchone()
    active_count = active_row[0] if active_row else 0
    if not active_count:
        cursor.close()
        conn.close()
        return jsonify({'success': False, 'error': 'Live GPS updates are allowed only during an active delivery.'}), 403

    cursor.execute("""
        UPDATE DeliveryPartner
        SET current_latitude = %s,
            current_longitude = %s,
            current_accuracy_m = %s,
            current_location_updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND application_status = 'Approved' AND is_active = TRUE
    """, (latitude, longitude, accuracy, session['partner_id']))
    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({
        'success': True,
        'latitude': latitude,
        'longitude': longitude,
        'accuracy': accuracy,
    })


@app.route('/partner/take/<int:request_id>', methods=['POST'])
@require_session_key('partner_id', 'partner_login', 'Please login as a delivery partner first.')
def partner_take_delivery(request_id):
    if 'partner_id' not in session:
        return redirect(url_for('partner_login'))

    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT id, is_available, is_active, application_status FROM DeliveryPartner WHERE id = %s", (session['partner_id'],))
    partner = cursor.fetchone()
    if not partner or partner.get('application_status') != 'Approved' or not partner.get('is_active', True):
        conn.close()
        flash('Delivery partner profile not available.', 'danger')
        return redirect(url_for('partner_login'))
    if not partner.get('is_available', True):
        conn.close()
        flash('Switch your status to Available before taking a delivery.', 'warning')
        return redirect(url_for('partner_dashboard'))

    delivery_condition = _delivery_request_condition('r')
    cursor.execute(f"""
        SELECT r.id
        FROM FoodRequest r
        WHERE r.id = %s
          AND {delivery_condition}
          AND r.status = 'Accepted'
          AND r.delivery_partner_id IS NULL
    """, (request_id,))
    req = cursor.fetchone()
    if not req:
        issue_message = _partner_delivery_issue_message(cursor, request_id, session['partner_id'])
        conn.close()
        flash(issue_message, 'warning')
        return redirect(url_for('partner_dashboard'))

    cursor.execute(f"""
        UPDATE FoodRequest r
        SET r.delivery_partner_id = %s,
            r.delivery_otp = %s,
            r.otp_generated_at = CURRENT_TIMESTAMP,
            r.otp_verified_at = NULL,
            r.otp_attempt_count = 0,
            r.otp_locked_at = NULL
        WHERE r.id = %s
          AND {delivery_condition}
          AND r.delivery_partner_id IS NULL
          AND r.status = 'Accepted'
    """, (session['partner_id'], _generate_delivery_otp(), request_id))
    cursor.execute(
        "UPDATE DeliveryPartner SET is_available = FALSE, delivery_status = 'Busy' WHERE id = %s",
        (session['partner_id'],)
    )
    conn.commit()
    conn.close()

    flash('Delivery assigned to you. Mark reached pickup when you arrive at the restaurant.', 'success')
    return redirect(url_for('partner_dashboard'))


@app.route('/partner/reached_pickup/<int:request_id>', methods=['POST'])
@require_session_key('partner_id', 'partner_login', 'Please login as a delivery partner first.')
def partner_reached_pickup(request_id):
    if 'partner_id' not in session:
        return redirect(url_for('partner_login'))

    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, is_active, application_status FROM DeliveryPartner WHERE id = %s", (session['partner_id'],))
    partner = cursor.fetchone()
    if not partner or partner.get('application_status') != 'Approved' or not partner.get('is_active', True):
        conn.close()
        flash('Delivery partner profile not available.', 'danger')
        return redirect(url_for('partner_login'))

    cursor.execute("""
        UPDATE FoodRequest
        SET pickup_reached_at = COALESCE(pickup_reached_at, CURRENT_TIMESTAMP)
        WHERE id = %s
          AND delivery_partner_id = %s
          AND status = 'Accepted'
    """, (request_id, session['partner_id']))
    updated = cursor.rowcount
    if updated:
        cursor.execute(
            "UPDATE DeliveryPartner SET is_available = FALSE, delivery_status = 'Busy' WHERE id = %s",
            (session['partner_id'],)
        )
    conn.commit()
    conn.close()

    flash('Pickup arrival confirmed. You can start delivery after pickup handoff.', 'success' if updated else 'warning')
    return redirect(url_for('partner_dashboard'))


@app.route('/partner/start_delivery/<int:request_id>', methods=['POST'])
@require_session_key('partner_id', 'partner_login', 'Please login as a delivery partner first.')
def partner_start_delivery(request_id):
    if 'partner_id' not in session:
        return redirect(url_for('partner_login'))

    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT id, is_active, application_status FROM DeliveryPartner WHERE id = %s", (session['partner_id'],))
    partner = cursor.fetchone()
    if not partner or partner.get('application_status') != 'Approved' or not partner.get('is_active', True):
        conn.close()
        flash('Delivery partner profile not available.', 'danger')
        return redirect(url_for('partner_login'))

    cursor.execute("""
        SELECT id, status, delivery_partner_id, delivery_otp, food_ready_at, pickup_reached_at
        FROM FoodRequest
        WHERE id = %s
          AND delivery_partner_id = %s
          AND status = 'Accepted'
        LIMIT 1
    """, (request_id, session['partner_id']))
    request_row = cursor.fetchone()
    if not request_row:
        issue_message = _partner_delivery_issue_message(cursor, request_id, session['partner_id'])
        conn.close()
        flash(issue_message, 'warning')
        return redirect(url_for('partner_dashboard'))
    if not request_row.get('pickup_reached_at'):
        conn.close()
        flash('Confirm "Reached pickup" before starting this delivery.', 'warning')
        return redirect(url_for('partner_dashboard'))
    if not request_row.get('food_ready_at'):
        conn.close()
        flash('Food is not marked packed by the owner yet.', 'warning')
        return redirect(url_for('partner_dashboard'))

    delivery_otp = (request_row.get('delivery_otp') or '').strip()
    if not re.fullmatch(r'\d{6}', delivery_otp):
        delivery_otp = _generate_delivery_otp()

    cursor = conn.cursor()
    cursor.execute("""
        UPDATE FoodRequest
        SET status = 'OutForDelivery',
            out_for_delivery_at = CURRENT_TIMESTAMP,
            delivery_otp = %s,
            otp_generated_at = CURRENT_TIMESTAMP,
            otp_verified_at = NULL,
            otp_attempt_count = 0,
            otp_locked_at = NULL
        WHERE id = %s
          AND delivery_partner_id = %s
          AND status = 'Accepted'
          AND food_ready_at IS NOT NULL
    """, (delivery_otp, request_id, session['partner_id']))
    updated = cursor.rowcount
    if updated:
        cursor.execute(
            "UPDATE DeliveryPartner SET is_available = FALSE, delivery_status = 'Busy' WHERE id = %s",
            (session['partner_id'],)
        )
    conn.commit()
    conn.close()

    if updated:
        flash('Delivery marked as out for delivery.', 'success')
    else:
        flash('Unable to update this delivery status.', 'warning')
    return redirect(url_for('partner_dashboard'))


@app.route('/partner/issue/<int:request_id>', methods=['POST'])
@require_session_key('partner_id', 'partner_login', 'Please login as a delivery partner first.')
def partner_report_delivery_issue(request_id):
    if 'partner_id' not in session:
        return redirect(url_for('partner_login'))

    issue_labels = {
        'food_not_ready': 'Food not ready',
        'user_unreachable': 'User unreachable',
        'wrong_address': 'Wrong address',
        'payment_issue': 'Payment issue',
        'vehicle_issue': 'Vehicle issue',
        'restaurant_delay': 'Restaurant delay',
        'address_issue': 'Address issue',
    }
    issue_type = (request.form.get('issue_type') or '').strip()
    detail = (request.form.get('issue_detail') or '').strip()[:255]
    if issue_type not in issue_labels:
        flash('Choose a valid delivery issue.', 'warning')
        return redirect(url_for('partner_dashboard'))

    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE FoodRequest
        SET delivery_issue_type = %s,
            delivery_issue_role = 'Partner',
            delivery_issue_detail = %s,
            delivery_issue_reported_at = CURRENT_TIMESTAMP
        WHERE id = %s
          AND delivery_partner_id = %s
          AND status IN ('Accepted', 'OutForDelivery')
    """, (issue_type, detail or issue_labels[issue_type], request_id, session['partner_id']))
    updated = cursor.rowcount
    conn.commit()
    cursor.close()
    conn.close()

    flash(f"Issue reported: {issue_labels[issue_type]}.", 'info' if updated else 'warning')
    return redirect(url_for('partner_dashboard'))


@app.route('/partner/reject/<int:request_id>', methods=['POST'])
@require_session_key('partner_id', 'partner_login', 'Please login as a delivery partner first.')
def partner_reject_delivery(request_id):
    if 'partner_id' not in session:
        return redirect(url_for('partner_login'))

    rejection_reason = (request.form.get('rejection_reason') or '').strip() or None
    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, is_active, application_status FROM DeliveryPartner WHERE id = %s", (session['partner_id'],))
    partner = cursor.fetchone()
    if not partner or partner.get('application_status') != 'Approved' or not partner.get('is_active', True):
        conn.close()
        flash('Delivery partner profile not available.', 'danger')
        return redirect(url_for('partner_login'))
    delivery_condition = _delivery_request_condition('r')
    cursor.execute(f"""
        SELECT r.id
        FROM FoodRequest r
        WHERE r.id = %s
          AND {delivery_condition}
          AND r.status = 'Accepted'
          AND r.delivery_partner_id IS NULL
    """, (request_id,))
    req = cursor.fetchone()
    if not req:
        issue_message = _partner_delivery_issue_message(cursor, request_id, session['partner_id'])
        conn.close()
        flash(issue_message, 'warning')
        return redirect(url_for('partner_dashboard'))

    cursor.execute("""
        INSERT INTO DeliveryPartnerRejection (request_id, partner_id, rejection_reason)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE
            rejection_reason = VALUES(rejection_reason),
            created_at = CURRENT_TIMESTAMP
    """, (request_id, session['partner_id'], rejection_reason))
    conn.commit()
    conn.close()

    flash('Order rejected for your account. Other available partners can still take it.', 'info')
    return redirect(url_for('partner_dashboard'))


@app.route('/partner/delivered/<int:request_id>', methods=['POST'])
@require_session_key('partner_id', 'partner_login', 'Please login as a delivery partner first.')
def partner_mark_delivered(request_id):
    if 'partner_id' not in session:
        return redirect(url_for('partner_login'))

    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, is_active, application_status FROM DeliveryPartner WHERE id = %s", (session['partner_id'],))
    partner = cursor.fetchone()
    if not partner or partner.get('application_status') != 'Approved' or not partner.get('is_active', True):
        conn.close()
        flash('Delivery partner profile not available.', 'danger')
        return redirect(url_for('partner_login'))
    cursor.execute("""
        SELECT delivery_otp, otp_attempt_count, otp_locked_at
        FROM FoodRequest
        WHERE id = %s
          AND delivery_partner_id = %s
          AND status = 'OutForDelivery'
        LIMIT 1
    """, (request_id, session['partner_id']))
    request_row = cursor.fetchone()
    if not request_row:
        issue_message = _partner_delivery_issue_message(cursor, request_id, session['partner_id'])
        conn.close()
        flash(issue_message, 'warning')
        return redirect(url_for('partner_dashboard'))

    entered_otp = (request.form.get('delivery_otp') or '').strip()
    stored_otp = (request_row.get('delivery_otp') or '').strip()
    attempt_count = int(request_row.get('otp_attempt_count') or 0)
    if request_row.get('otp_locked_at') or attempt_count >= 5:
        conn.close()
        flash('OTP verification is locked for delivery #{}. Please contact owner/admin support to reset it.'.format(request_id), 'danger')
        return redirect(url_for('partner_dashboard'))

    if not entered_otp:
        conn.close()
        flash('Enter the 6-digit OTP from the recipient to complete delivery #{}.'.format(request_id), 'warning')
        return redirect(url_for('partner_dashboard'))

    if not re.fullmatch(r'\d{6}', entered_otp):
        cursor.execute("""
            UPDATE FoodRequest
            SET otp_attempt_count = LEAST(COALESCE(otp_attempt_count, 0) + 1, 5),
                otp_locked_at = CASE WHEN COALESCE(otp_attempt_count, 0) + 1 >= 5 THEN CURRENT_TIMESTAMP ELSE otp_locked_at END
            WHERE id = %s AND delivery_partner_id = %s
        """, (request_id, session['partner_id']))
        conn.commit()
        conn.close()
        flash('OTP must be exactly 6 digits for delivery #{}.'.format(request_id), 'warning')
        return redirect(url_for('partner_dashboard'))

    if not re.fullmatch(r'\d{6}', stored_otp):
        conn.close()
        flash('Delivery OTP is missing or invalid for delivery #{}. Please contact support.'.format(request_id), 'danger')
        return redirect(url_for('partner_dashboard'))

    if not hmac.compare_digest(entered_otp, stored_otp):
        cursor.execute("""
            UPDATE FoodRequest
            SET otp_attempt_count = LEAST(COALESCE(otp_attempt_count, 0) + 1, 5),
                otp_locked_at = CASE WHEN COALESCE(otp_attempt_count, 0) + 1 >= 5 THEN CURRENT_TIMESTAMP ELSE otp_locked_at END
            WHERE id = %s AND delivery_partner_id = %s
        """, (request_id, session['partner_id']))
        conn.commit()
        remaining_attempts = max(0, 4 - attempt_count)
        conn.close()
        if remaining_attempts:
            flash('Invalid OTP for delivery #{}. {} attempt(s) left before lock.'.format(request_id, remaining_attempts), 'danger')
        else:
            flash('Invalid OTP for delivery #{}. OTP verification is now locked. Contact owner/admin support.'.format(request_id), 'danger')
        return redirect(url_for('partner_dashboard'))

    cursor.execute("""
        UPDATE FoodRequest
        SET status = 'Delivered',
            delivered_at = CURRENT_TIMESTAMP,
            otp_verified_at = CURRENT_TIMESTAMP,
            otp_attempt_count = 0,
            otp_locked_at = NULL
        WHERE id = %s
          AND delivery_partner_id = %s
          AND status = 'OutForDelivery'
    """, (request_id, session['partner_id']))
    updated = cursor.rowcount
    if updated:
        cursor.execute("""
            SELECT COUNT(*) AS active_jobs
            FROM FoodRequest
            WHERE delivery_partner_id = %s
              AND status IN ('Accepted', 'OutForDelivery')
        """, (session['partner_id'],))
        remaining_jobs = cursor.fetchone() or {}
        remaining_count = int(remaining_jobs.get('active_jobs') or 0)
        next_status = 'Available' if remaining_count == 0 else 'Busy'
        cursor.execute(
            "UPDATE DeliveryPartner SET is_available = %s, delivery_status = %s WHERE id = %s",
            (remaining_count == 0, next_status, session['partner_id'])
        )
    conn.commit()
    conn.close()

    if updated:
        flash(f'Delivery #{request_id} marked as completed and OTP verified.', 'success')
    else:
        flash('Only your active out-for-delivery jobs can be marked delivered.', 'warning')
    return redirect(url_for('partner_dashboard'))


@app.route('/partner/live_insights')
def partner_live_insights():
    if 'partner_id' not in session:
        return jsonify({'success': False, 'error': 'Please login as a delivery partner first.'}), 401

    dashboard_data = _get_partner_dashboard_data(session['partner_id'])
    if not dashboard_data:
        return jsonify({'success': False, 'error': 'Delivery partner profile not found.'}), 404
    realtime_summary = {}
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor(dictionary=True)
        realtime_summary = build_realtime_summary(cursor, 'partner', session['partner_id'])
        cursor.close()
        conn.close()

    return jsonify({
        'success': True,
        'partner': {
            'id': dashboard_data['partner'].get('id'),
            'name': dashboard_data['partner'].get('name'),
            'application_status': dashboard_data['partner'].get('application_status'),
            'delivery_status': dashboard_data['partner'].get('delivery_status'),
            'is_available': dashboard_data['partner'].get('is_available'),
        },
        'available_jobs': dashboard_data['available_jobs'],
        'active_jobs': dashboard_data['active_jobs'],
        'completed_jobs': dashboard_data['completed_jobs'],
        'rejected_jobs': dashboard_data['rejected_jobs'],
        'stats': dashboard_data['partner_stats'],
        'delivery_flow': dashboard_data['delivery_flow'],
        'performance_snapshot': dashboard_data['performance_snapshot'],
        'trip_summary': dashboard_data['trip_summary'],
        'earnings_summary': dashboard_data['earnings_summary'],
        'history_summary': dashboard_data['history_summary'],
        'partner_status': dashboard_data['partner_status'],
        'realtime_summary': realtime_summary,
    })


@app.route('/partner/live_stream')
def partner_live_stream():
    if 'partner_id' not in session:
        return jsonify({'success': False, 'error': 'Please login as a delivery partner first.'}), 401
    return _stream_live_snapshot(
        partner_live_insights,
        5,
        'Please login as a delivery partner first.',
    )


@app.route('/partner/history')
@require_session_key('partner_id', 'partner_login', 'Please login as a delivery partner first.')
def partner_history():
    if 'partner_id' not in session:
        return redirect(url_for('partner_login'))

    dashboard_data = _get_partner_dashboard_data(session['partner_id'])
    if not dashboard_data:
        flash('Delivery partner profile not found. Please login again.', 'danger')
        return redirect(url_for('partner_login'))

    completed_jobs = dashboard_data['completed_jobs']
    payout_status = clean_text(request.args.get('payout_status') or 'all')
    start_date = clean_text(request.args.get('start_date'))
    end_date = clean_text(request.args.get('end_date'))
    payout_filters = ["partner_id = %s"]
    payout_params = [session['partner_id']]
    if payout_status != 'all':
        payout_filters.append("LOWER(status) = LOWER(%s)")
        payout_params.append(payout_status)
    if start_date:
        payout_filters.append("DATE(processed_at) >= %s")
        payout_params.append(start_date)
    if end_date:
        payout_filters.append("DATE(processed_at) <= %s")
        payout_params.append(end_date)

    payment_logs = []
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"""
            SELECT id, payment_mode, amount_paise, payment_reference, payout_id,
                   upi_id, beneficiary_name, status, remarks, processed_at
            FROM DeliveryPartnerPaymentLog
            WHERE {' AND '.join(payout_filters)}
            ORDER BY processed_at DESC, id DESC
            LIMIT 100
        """, tuple(payout_params))
        payment_logs = cursor.fetchall()
        cursor.close()
        conn.close()
    return render_template(
        'delivery_partner_history.html',
        partner=dashboard_data['partner'],
        partner_status=dashboard_data['partner_status'],
        completed_jobs=completed_jobs,
        history_summary=dashboard_data['history_summary'],
        earnings_summary=dashboard_data['earnings_summary'],
        performance_snapshot=dashboard_data['performance_snapshot'],
        trip_summary=dashboard_data['trip_summary'],
        payment_logs=payment_logs,
        payout_status=payout_status,
        payout_start_date=start_date,
        payout_end_date=end_date,
    )



@app.route('/owner/login', methods=['GET', 'POST'])
def owner_login():
    """Owner Login Portal"""
    if request.method == 'POST':
        username = clean_text(request.form.get('username'))
        password = clean_text(request.form.get('password'))
        missing_fields = validate_required_fields({
            'Username, email, or phone': username,
            'Password': password,
        })
        if missing_fields:
            flash(f"Please complete: {', '.join(missing_fields)}.", 'danger')
            return redirect(url_for('owner_login'))
        
        try:
            with db_cursor(dictionary=True) as cursor:
                cursor.execute(
                    """
                    SELECT id, name, owner_name, username, password
                    FROM Restaurant
                    WHERE COALESCE(is_deleted, FALSE) = FALSE
                      AND (
                        TRIM(username) = %s
                        OR LOWER(TRIM(email)) = LOWER(%s)
                        OR REPLACE(contact, ' ', '') = REPLACE(%s, ' ', '')
                      )
                    ORDER BY CASE WHEN username = %s THEN 0 ELSE 1 END
                    LIMIT 1
                    """,
                    (username, username, username, username)
                )
                user = cursor.fetchone()
                password_ok, should_upgrade = _password_matches(user.get('password') if user else None, password)
                if user and password_ok and should_upgrade:
                    cursor.execute("UPDATE Restaurant SET password = %s WHERE id = %s", (generate_password_hash(password), user['id']))
                if user and password_ok and user.get('username') != username:
                    cursor.execute("UPDATE Restaurant SET username = %s WHERE id = %s", (username, user['id']))
                if user and not password_ok:
                    user = None
        except DatabaseUnavailableError:
            return "Database Connection Failed.", 500
        
        if user:
            session.permanent = True
            session['owner_id'] = user['id']
            session['restaurant_name'] = user['name']
            session['owner_name'] = user['owner_name'] or user['name']
            flash('Logged in successfully.', 'success')
            return redirect(url_for('owner_dashboard'))
        else:
            flash('Invalid credentials.', 'danger')
            
    return render_template('owner_login.html')

@app.route('/owner/register', methods=['GET', 'POST'])
def owner_register():
    """Owner Registration Form"""
    if request.method == 'POST':
        items_served_list = request.form.getlist('items_served')
        items_served_str = ",".join(items_served_list)

        username = clean_text(request.form.get('username'))
        password = clean_text(request.form.get('password'))
        confirm_password = clean_text(request.form.get('confirm_password'))
        missing_fields = validate_required_fields({
            'Restaurant Name': request.form.get('name'),
            'Owner Name': request.form.get('owner_name'),
            'Username': username,
            'Password': password,
            'Confirm Password': confirm_password,
            'Restaurant Type': request.form.get('restaurant_type'),
            'FSSAI Number': request.form.get('fssai'),
            'Location': request.form.get('location'),
            'Contact Country Code': request.form.get('contact_country_code'),
            'Contact Number': request.form.get('contact_number'),
        })
        if missing_fields:
            flash(f"Please complete: {', '.join(missing_fields)}.", 'danger')
            return redirect(url_for('owner_register'))

        password_error = validate_password_pair(password, confirm_password)
        if password_error:
            flash(password_error, 'danger')
            return redirect(url_for('owner_register'))

        if request.form.get('agree_terms') != 'on':
            flash('Please agree to the Terms & Privacy Policy.', 'danger')
            return redirect(url_for('owner_register'))
        
        try:
            with db_cursor(dictionary=True) as cursor:
                cursor.execute("SELECT id FROM Restaurant WHERE TRIM(username) = %s", (username,))
                if cursor.fetchone():
                    flash('Username already exists. Choose another.', 'danger')
                    return redirect(url_for('owner_register'))

                photo_url = 'https://images.unsplash.com/photo-1414235077428-338989a2e8c0?w=500&auto=format&fit=crop&q=60'
                url_input = request.form.get('photo_url_input')
                if url_input:
                    photo_url = url_input

                if 'restaurant_photo' in request.files:
                    file = request.files['restaurant_photo']
                    if file and file.filename != '' and allowed_file(file.filename):
                        saved_url, error = save_uploaded_file(
                            file,
                            app.config['UPLOAD_FOLDER'],
                            '/static/uploads',
                            allowed_extensions=ALLOWED_EXTENSIONS,
                            max_size=2 * 1024 * 1024,
                            filename_prefix=username,
                            type_error_message='Image file must be JPG, PNG, or GIF.',
                            size_error_message='Image file is too large. Max 2 MB allowed.',
                        )
                        if error:
                            flash(error, 'danger')
                            return redirect(url_for('owner_register'))
                        photo_url = saved_url

                owner_id_doc_url = None
                if 'owner_id_document' in request.files:
                    id_file = request.files['owner_id_document']
                    if id_file and id_file.filename:
                        saved_url, error = save_uploaded_file(
                            id_file,
                            OWNER_ID_UPLOAD_DIR,
                            '/static/uploads/owner_ids',
                            allowed_extensions=OWNER_ID_ALLOWED_EXTENSIONS,
                            max_size=OWNER_ID_MAX_SIZE,
                            filename_prefix=f"owner_{username}",
                            type_error_message='ID document must be JPG, PNG, or PDF.',
                            size_error_message='ID document is too large. Max 2 MB allowed.',
                        )
                        if error:
                            flash(error, 'danger')
                            return redirect(url_for('owner_register'))
                        owner_id_doc_url = saved_url

                raw_country_code = clean_text(request.form.get('contact_country_code'))
                country_code_match = re.search(r'\+\d+', raw_country_code)
                country_code = country_code_match.group(0) if country_code_match else raw_country_code
                contact_number = clean_text(request.form.get('contact_number'))

                map_url_input = (request.form.get('map_url') or '').strip()
                normalized_map_url = None
                if map_url_input:
                    normalized_map_url = normalize_google_maps_url(map_url_input, request.form.get('location'))

                map_lat = None
                map_lon = None
                if normalized_map_url:
                    coord_match = re.search(r'@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)', normalized_map_url)
                    if coord_match:
                        map_lat = float(coord_match.group(1))
                        map_lon = float(coord_match.group(2))
                if map_lat is None or map_lon is None:
                    coords = geocode_location_server(request.form.get('location'))
                    if coords:
                        map_lat, map_lon = coords

                cursor.execute("""
                    INSERT INTO Restaurant (name, owner_name, email, restaurant_type, gst, fssai, location, map_url, latitude, longitude, contact, alternate_contact, items_served, photo_url, id_doc_url, username, password)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    request.form.get('name'),
                    request.form.get('owner_name'),
                    request.form.get('email'),
                    request.form.get('restaurant_type'),
                    request.form.get('gst'),
                    request.form.get('fssai'),
                    request.form.get('location'),
                    normalized_map_url,
                    map_lat,
                    map_lon,
                    f"{country_code}{contact_number}",
                    request.form.get('alternate_contact'),
                    items_served_str,
                    photo_url,
                    owner_id_doc_url,
                    username,
                    generate_password_hash(password)
                ))
        except DatabaseUnavailableError:
            return "Database Connection Failed.", 500
        
        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('owner_login'))
        
    return render_template('owner_register.html')

@app.route('/owner/logout')
def owner_logout():
    session.pop('owner_id', None)
    flash('Logged out.', 'info')
    return redirect(url_for('index'))

@app.route('/owner/profile', methods=['POST'])
@require_session_key('owner_id', 'owner_login', 'Please login as an owner first.')
def owner_update_profile():
    if 'owner_id' not in session:
        return redirect(url_for('owner_login'))

    owner_id = session['owner_id']
    name = (request.form.get('name') or '').strip()
    owner_name = (request.form.get('owner_name') or '').strip()
    email = (request.form.get('email') or '').strip()
    contact = (request.form.get('contact') or '').strip()
    location = (request.form.get('location') or '').strip()
    map_url_input = (request.form.get('map_url') or '').strip()
    items_served_list = request.form.getlist('items_served')
    items_served_str = ",".join([v for v in items_served_list if v])

    if not name or not owner_name or not contact or not location:
        flash('Restaurant name, owner name, contact, and location are required.', 'danger')
        return redirect(url_for('owner_dashboard'))

    normalized_map_url = None
    if map_url_input:
        normalized_map_url = normalize_google_maps_url(map_url_input, location)

    map_lat = None
    map_lon = None
    if normalized_map_url:
        coord_match = re.search(r'@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)', normalized_map_url)
        if coord_match:
            map_lat = float(coord_match.group(1))
            map_lon = float(coord_match.group(2))
    if map_lat is None or map_lon is None:
        coords = geocode_location_server(location)
        if coords:
            map_lat, map_lon = coords

    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE Restaurant
        SET name = %s,
            owner_name = %s,
            email = %s,
            contact = %s,
            location = %s,
            map_url = %s,
            latitude = %s,
            longitude = %s,
            items_served = %s
        WHERE id = %s
        """,
        (
            name,
            owner_name,
            email or None,
            contact,
            location,
            normalized_map_url,
            map_lat,
            map_lon,
            items_served_str,
            owner_id,
        ),
    )
    conn.commit()
    conn.close()

    session['owner_name'] = owner_name
    flash('Profile updated successfully.', 'success')
    return redirect(url_for('owner_dashboard'))

@app.route('/owner/change_password', methods=['POST'])
@require_session_key('owner_id', 'owner_login', 'Please login as an owner first.')
def owner_change_password():
    """Owner changes their dashboard password."""
    if 'owner_id' not in session:
        return redirect(url_for('owner_login'))

    current_password = clean_text(request.form.get('current_password'))
    new_password = clean_text(request.form.get('new_password'))
    confirm_password = clean_text(request.form.get('confirm_password'))

    password_error = validate_password_pair(new_password, confirm_password)
    if password_error:
        flash(password_error, 'danger')
        return redirect(url_for('owner_dashboard'))

    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, password FROM Restaurant WHERE id = %s AND COALESCE(is_deleted, FALSE) = FALSE",
        (session['owner_id'],)
    )
    owner = cursor.fetchone()

    password_ok, _ = _password_matches(owner.get('password') if owner else None, current_password)
    if not owner or not password_ok:
        cursor.close()
        conn.close()
        flash('Current password is incorrect.', 'danger')
        return redirect(url_for('owner_dashboard'))

    cursor.execute(
        "UPDATE Restaurant SET password = %s WHERE id = %s",
        (generate_password_hash(new_password), session['owner_id'])
    )
    conn.commit()
    cursor.close()
    conn.close()

    flash('Owner password updated successfully.', 'success')
    return redirect(url_for('owner_dashboard'))

@app.route('/owner/ai_assist', methods=['POST'])
def owner_ai_assist():
    """Generate smart listing suggestions for owner donations."""
    if 'owner_id' not in session:
        return jsonify({'success': False, 'error': 'Please login as an owner first.'}), 401

    payload = request.get_json(silent=True) or request.form
    notes = (payload.get('notes') or payload.get('description') or '').strip()
    quantity_hint = payload.get('quantity_hint')

    suggestions, error = build_owner_ai_assistant(session['owner_id'], notes, quantity_hint)
    if error:
        return jsonify({'success': False, 'error': error}), 500

    suggestions['success'] = True
    suggestions['confidence'] = 'medium'
    suggestions['notes_received'] = bool(notes)
    return jsonify(suggestions)

@app.route('/owner', methods=['GET', 'POST'])
@require_session_key('owner_id', 'owner_login', 'Please login as an owner first.')
def owner_dashboard():
    """Owner/Donor Dashboard"""
    if 'owner_id' not in session:
        return render_template('owner_login.html')

    session.permanent = True
        
    owner_id = session['owner_id']
        
    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor(dictionary=True)
    
    cursor.execute("SELECT * FROM Restaurant WHERE id = %s AND COALESCE(is_deleted, FALSE) = FALSE", (owner_id,))
    restaurant = cursor.fetchone()
    if not restaurant:
        conn.close()
        flash('Owner profile not found. Please login again.', 'danger')
        return redirect(url_for('owner_login'))

    session['restaurant_name'] = restaurant.get('name')
    session['owner_name'] = restaurant.get('owner_name') or restaurant.get('name')
    restaurant['items_served'] = restaurant.get('items_served') or ''
    
    if request.method == 'POST':
        item_type = clean_text(request.form.get('item_type'))
        item_name = clean_text(request.form.get('item_name'))
        quantity = clean_text(request.form.get('quantity'))
        donation_date = clean_text(request.form.get('date'))
        missing_fields = validate_required_fields({
            'Item Type': item_type,
            'Food Name': item_name,
            'Quantity': quantity,
            'Date Prepared': donation_date,
        })
        if missing_fields:
            flash(f"Please complete: {', '.join(missing_fields)}.", 'danger')
            conn.close()
            return redirect(url_for('owner_dashboard'))

        prep_hours = request.form.get('prep_hours')
        prep_minutes = request.form.get('prep_minutes')
        if prep_hours is not None and prep_minutes is not None:
            try:
                prep_time = f"{int(prep_hours)}h {int(prep_minutes):02d}m"
            except (TypeError, ValueError):
                prep_time = None
        else:
            prep_time = request.form.get('prep_time')
        if not prep_time:
            flash('Prep time is required.', 'danger')
            conn.close()
            return redirect(url_for('owner_dashboard'))

        packed_time, best_before_time, storage_note, safety_error = _prepare_donation_safety_fields(
            request.form,
            item_type,
            donation_date,
            prep_time,
        )
        if safety_error:
            flash(safety_error, 'danger')
            conn.close()
            return redirect(url_for('owner_dashboard'))

        image_url = None
        if 'donation_image' in request.files:
            file = request.files['donation_image']
            if file and file.filename:
                saved_url, error = save_uploaded_file(
                    file,
                    app.config['UPLOAD_FOLDER'],
                    '/static/uploads',
                    allowed_extensions=ALLOWED_EXTENSIONS,
                    max_size=2 * 1024 * 1024,
                    filename_prefix=f"donation_{owner_id}",
                    type_error_message='Donation image must be JPG, PNG, or GIF.',
                    size_error_message='Donation image is too large. Max 2 MB allowed.',
                )
                if error:
                    flash(error, 'danger')
                    conn.close()
                    return redirect(url_for('owner_dashboard'))
                image_url = saved_url

        auto_approve_listing = bool(restaurant.get('verified'))
        listing_status = 'Available' if auto_approve_listing else 'PendingReview'
        quality_status = 'Approved' if auto_approve_listing else 'Pending'
        cursor.execute("""
            INSERT INTO Donation (
                restaurant_id, item_type, item_name, quantity, prep_time, date, image_url,
                status, quality_status, packed_time, best_before_time, storage_note
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            owner_id,
            item_type,
            item_name,
            quantity,
            prep_time,
            donation_date,
            image_url,
            listing_status,
            quality_status,
            packed_time,
            best_before_time,
            storage_note,
        ))
        conn.commit()
        if auto_approve_listing:
            flash('Food listing published successfully.', 'success')
        else:
            flash('Food item submitted for admin quality review.', 'success')
        conn.close()
        return redirect(url_for('owner_dashboard'))
    
    _mark_owner_expired_donations(cursor, owner_id)
    conn.commit()

    # Show only their donations
    cursor.execute("""
        SELECT d.*,
               DATE_FORMAT(d.date, '%Y-%m-%d') AS date,
               COALESCE(d.prep_time, '') AS prep_display,
               DATE_FORMAT(d.packed_time, '%Y-%m-%dT%H:%i') AS packed_time_input,
               DATE_FORMAT(d.best_before_time, '%Y-%m-%dT%H:%i') AS best_before_time_input,
               DATE_FORMAT(d.packed_time, '%Y-%m-%d %H:%i') AS packed_time_display,
               DATE_FORMAT(d.best_before_time, '%Y-%m-%d %H:%i') AS best_before_display,
               COALESCE(rs.pending_count, 0) AS pending_request_count,
               COALESCE(rs.accepted_count, 0) AS accepted_request_count,
               COALESCE(rs.ready_count, 0) AS ready_request_count,
               COALESCE(rs.collected_count, 0) AS collected_request_count,
               COALESCE(rs.delivered_count, 0) AS delivered_request_count,
               CASE
                   WHEN COALESCE(rs.delivered_count, 0) > 0 THEN 'Delivered'
                   WHEN COALESCE(rs.collected_count, 0) > 0 THEN 'Collected'
                   WHEN COALESCE(rs.ready_count, 0) > 0 THEN 'Ready'
                   WHEN COALESCE(rs.accepted_count, 0) > 0 THEN 'Accepted'
                   WHEN COALESCE(rs.pending_count, 0) > 0 THEN 'Requested'
                   WHEN d.status = 'Available' THEN 'Available'
                   ELSE d.status
               END AS display_status
        FROM Donation d
        LEFT JOIN (
            SELECT donation_id,
                   SUM(CASE WHEN status = 'Pending' THEN 1 ELSE 0 END) AS pending_count,
                   SUM(CASE WHEN status IN ('Accepted', 'OutForDelivery') AND food_ready_at IS NULL THEN 1 ELSE 0 END) AS accepted_count,
                   SUM(CASE WHEN status IN ('Accepted', 'OutForDelivery') AND food_ready_at IS NOT NULL THEN 1 ELSE 0 END) AS ready_count,
                   SUM(CASE WHEN status = 'Collected' THEN 1 ELSE 0 END) AS collected_count,
                   SUM(CASE WHEN status = 'Delivered' THEN 1 ELSE 0 END) AS delivered_count
            FROM FoodRequest
            GROUP BY donation_id
        ) rs ON rs.donation_id = d.id
        WHERE d.restaurant_id = %s
        AND (d.created_at > NOW() - INTERVAL 12 HOUR OR d.status IN ('Available', 'PendingReview'))
        ORDER BY id DESC
    """, (owner_id,))
    my_donations = cursor.fetchall()
    
    request_status = request.args.get('request_status', 'Pending')
    request_search = (request.args.get('request_search') or '').strip()

    request_filters = ["d.restaurant_id = %s"]
    request_params = [owner_id]
    if request_status in {'Pending', 'Accepted', 'Rejected', 'Collected', 'NoShow', 'OutForDelivery', 'Delivered'}:
        request_filters.append("r.status = %s")
        request_params.append(request_status)
    elif request_status == 'all':
        pass
    else:
        request_status = 'Pending'
        request_filters.append("r.status = %s")
        request_params.append(request_status)
    if request_search:
        request_filters.append("(d.item_name LIKE %s OR u.name LIKE %s OR u.org_type LIKE %s)")
        term = f"%{request_search}%"
        request_params.extend([term, term, term])

    request_where = " AND ".join(request_filters)
    cursor.execute(f"""
        SELECT r.id as request_id, r.requested_amt, r.status as req_status,
               r.request_time, r.accepted_at, r.food_ready_at, r.pickup_reached_at, r.out_for_delivery_at, r.delivered_at,
               UNIX_TIMESTAMP(r.request_time) as request_ts,
               UNIX_TIMESTAMP(COALESCE(r.accepted_at, r.request_time)) as accepted_ts,
               UNIX_TIMESTAMP(r.food_ready_at) as food_ready_ts,
               UNIX_TIMESTAMP(r.pickup_reached_at) as pickup_reached_ts,
               UNIX_TIMESTAMP(r.out_for_delivery_at) as out_for_delivery_ts,
               TIMESTAMPDIFF(MINUTE, COALESCE(r.accepted_at, r.request_time), NOW()) >= 45 as can_mark_no_show,
               r.delivery_mode, r.delivery_charge_mode, r.delivery_address, r.delivery_latitude, r.delivery_longitude,
               r.delivery_location_accuracy_m, r.delivery_order_id, r.delivery_fee_paise, r.delivery_coin_used,
               r.delivery_coin_discount_paise, r.delivery_issue_type, r.delivery_issue_role, r.delivery_issue_detail,
               r.delivery_issue_reported_at,
               d.item_name, d.item_type, d.quantity as original_quantity,
               u.name as requester_name, u.contact as requester_contact, u.org_type as requester_org_type,
               dp.name as delivery_partner_name, dp.phone as delivery_partner_phone,
               dp.vehicle_type as delivery_partner_vehicle_type, dp.vehicle_number as delivery_partner_vehicle_number,
               dp.delivery_status as delivery_partner_status, dp.current_latitude as partner_latitude,
               dp.current_longitude as partner_longitude, dp.current_accuracy_m as partner_accuracy_m,
               dp.current_location_updated_at as partner_location_updated_at
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        LEFT JOIN User u ON r.user_id = u.id
        LEFT JOIN DeliveryPartner dp ON dp.id = r.delivery_partner_id
        WHERE {request_where}
        ORDER BY r.id DESC
    """, tuple(request_params))
    owner_requests = cursor.fetchall()

    cursor.execute("""
        SELECT r.id as request_id, r.requested_amt, r.status as req_status,
               r.request_time, r.accepted_at, r.food_ready_at, r.pickup_reached_at, r.out_for_delivery_at,
               UNIX_TIMESTAMP(r.request_time) as request_ts,
               UNIX_TIMESTAMP(COALESCE(r.accepted_at, r.request_time)) as accepted_ts,
               UNIX_TIMESTAMP(r.food_ready_at) as food_ready_ts,
               UNIX_TIMESTAMP(r.pickup_reached_at) as pickup_reached_ts,
               UNIX_TIMESTAMP(r.out_for_delivery_at) as out_for_delivery_ts,
               r.delivery_mode, r.delivery_charge_mode, r.delivery_address, r.delivery_latitude, r.delivery_longitude,
               r.delivery_location_accuracy_m, r.delivery_order_id, r.delivery_fee_paise,
               r.delivery_issue_type, r.delivery_issue_role, r.delivery_issue_detail, r.delivery_issue_reported_at,
               d.item_name, d.item_type, d.quantity as original_quantity,
               u.name as requester_name, u.contact as requester_contact, u.org_type as requester_org_type,
               dp.name as delivery_partner_name, dp.phone as delivery_partner_phone,
               dp.vehicle_type as delivery_partner_vehicle_type, dp.vehicle_number as delivery_partner_vehicle_number,
               dp.delivery_status as delivery_partner_status, dp.current_latitude as partner_latitude,
               dp.current_longitude as partner_longitude, dp.current_accuracy_m as partner_accuracy_m,
               dp.current_location_updated_at as partner_location_updated_at
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        LEFT JOIN User u ON r.user_id = u.id
        LEFT JOIN DeliveryPartner dp ON dp.id = r.delivery_partner_id
        WHERE d.restaurant_id = %s
          AND r.delivery_mode = 'Delivery'
          AND r.status IN ('Accepted', 'OutForDelivery')
        ORDER BY CASE WHEN r.status = 'OutForDelivery' THEN 0 ELSE 1 END,
                 COALESCE(r.out_for_delivery_at, r.pickup_reached_at, r.food_ready_at, r.accepted_at, r.request_time) DESC
    """, (owner_id,))
    owner_delivery_handoffs = cursor.fetchall()

    cursor.execute("""
        SELECT
            SUM(CASE WHEN r.status = 'Pending' THEN 1 ELSE 0 END) as pending_count,
            SUM(CASE WHEN r.status = 'Accepted' THEN 1 ELSE 0 END) as accepted_count,
            SUM(CASE WHEN r.status = 'OutForDelivery' THEN 1 ELSE 0 END) as out_for_delivery_count,
            SUM(CASE WHEN r.status = 'Delivered' THEN 1 ELSE 0 END) as delivered_count,
            SUM(CASE WHEN r.status = 'Rejected' THEN 1 ELSE 0 END) as rejected_count,
            SUM(CASE WHEN r.status = 'Collected' THEN 1 ELSE 0 END) as collected_count,
            SUM(CASE WHEN r.status = 'NoShow' THEN 1 ELSE 0 END) as no_show_count,
            COUNT(*) as total_count
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        WHERE d.restaurant_id = %s
    """, (owner_id,))
    request_breakdown = cursor.fetchone() or {}
    request_total = int(request_breakdown.get('total_count') or 0)

    cursor.execute("""
        SELECT
            COUNT(*) as rating_count,
            AVG(taste_rating) as avg_rating
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        WHERE d.restaurant_id = %s
          AND r.taste_rating IS NOT NULL
          AND COALESCE(r.rated_at, r.request_time) >= NOW() - INTERVAL 7 DAY
    """, (owner_id,))
    rating_stats = cursor.fetchone() or {}

    cursor.execute("""
        SELECT r.taste_rating, r.taste_feedback, r.rated_at,
               d.item_name, u.name as user_name
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        LEFT JOIN User u ON r.user_id = u.id
        WHERE d.restaurant_id = %s
          AND r.taste_rating IS NOT NULL
        ORDER BY COALESCE(r.rated_at, r.request_time) DESC
        LIMIT 2
    """, (owner_id,))
    recent_feedback = cursor.fetchall()

    cursor.execute("""
        SELECT COUNT(*) AS tip_count,
               COALESCE(SUM(amount_paise), 0) AS total_tip_paise
        FROM RequestTip tip
        JOIN FoodRequest r ON tip.request_id = r.id
        JOIN Donation d ON tip.donation_id = d.id
        WHERE d.restaurant_id = %s
          AND tip.status = 'Paid'
    """, (owner_id,))
    tip_stats = cursor.fetchone() or {}

    cursor.execute("""
        SELECT tip.amount_paise, tip.paid_at, tip.note, d.item_name, u.name AS user_name
        FROM RequestTip tip
        JOIN FoodRequest r ON tip.request_id = r.id
        JOIN Donation d ON tip.donation_id = d.id
        LEFT JOIN User u ON tip.user_id = u.id
        WHERE d.restaurant_id = %s
          AND tip.status = 'Paid'
        ORDER BY tip.paid_at DESC
        LIMIT 5
    """, (owner_id,))
    recent_tips = cursor.fetchall()

    cursor.execute("""
        SELECT
            d.item_type,
            COUNT(*) AS request_count,
            SUM(CASE WHEN r.status = 'Pending' THEN 1 ELSE 0 END) AS pending_count,
            SUM(CASE WHEN r.status = 'Accepted' THEN 1 ELSE 0 END) AS accepted_count,
            SUM(CASE WHEN r.status = 'Collected' THEN 1 ELSE 0 END) AS collected_count
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        WHERE d.restaurant_id = %s
        GROUP BY d.item_type
        ORDER BY request_count DESC, d.item_type ASC
        LIMIT 5
    """, (owner_id,))
    item_type_rows = cursor.fetchall()

    cursor.execute("""
        SELECT r.donation_id, r.id AS request_id, r.requested_amt, r.status,
               r.request_time, r.accepted_at, r.food_ready_at, r.out_for_delivery_at,
               r.delivered_at, u.name AS requester_name, u.org_type AS requester_org_type
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        LEFT JOIN User u ON r.user_id = u.id
        WHERE d.restaurant_id = %s
        ORDER BY r.request_time DESC, r.id DESC
    """, (owner_id,))
    owner_request_history_rows = cursor.fetchall()
    owner_request_history = {}
    for history_row in owner_request_history_rows:
        owner_request_history.setdefault(history_row.get('donation_id'), []).append(history_row)
    for donation in my_donations:
        donation['request_history'] = owner_request_history.get(donation.get('id'), [])

    cursor.execute("""
        SELECT d.item_name,
               COUNT(*) AS request_count,
               COALESCE(SUM(CASE WHEN r.status IN ('Collected', 'Delivered') THEN r.requested_amt ELSE 0 END), 0) AS fulfilled_servings
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        WHERE d.restaurant_id = %s
        GROUP BY d.item_name
        ORDER BY request_count DESC, fulfilled_servings DESC, d.item_name ASC
        LIMIT 5
    """, (owner_id,))
    top_requested_items = cursor.fetchall()
    
    conn.close()
    
    # Convert items_served to list for template parsing
    if restaurant['items_served']:
        restaurant['items_served_list'] = restaurant['items_served'].split(',')
    else:
        restaurant['items_served_list'] = []

    # Defaults to satisfy the expanded owner template
    stats = {
        'total_donations': len(my_donations),
        'available_donations': sum(1 for d in my_donations if d.get('status') == 'Available'),
        'claimed_donations': sum(1 for d in my_donations if d.get('status') == 'Claimed'),
        'pending_requests': request_breakdown.get('pending_count', 0) or 0,
        'unread_chats': 0,
        'recent_rating_count': rating_stats.get('rating_count', 0) or 0,
        'avg_taste_rating': float(rating_stats.get('avg_rating') or 0),
        'tip_count': tip_stats.get('tip_count', 0) or 0,
        'tip_total_rupees': (float(tip_stats.get('total_tip_paise') or 0) / 100.0),
        'donations_posted': len(my_donations),
        'meals_saved': sum(int(row.get('fulfilled_servings') or 0) for row in top_requested_items),
        'top_requested_items': top_requested_items,
    }
    donation_pagination = {
        'page': 1,
        'total_pages': 1,
        'has_prev': False,
        'has_next': False,
        'prev_page': 1,
        'next_page': 1,
    }
    request_pagination = {
        'page': 1,
        'total_pages': 1,
        'has_prev': False,
        'has_next': False,
        'prev_page': 1,
        'next_page': 1,
    }
    donation_status = 'all'
    donation_search = ''
    today_date = datetime.date.today().strftime('%Y-%m-%d')
    owner_trust = _build_owner_trust_profile(request_breakdown, rating_stats, tip_stats, len(my_donations))
    owner_intelligence = _build_owner_intelligence(owner_requests, item_type_rows, request_breakdown)
    owner_suggestions = _build_owner_suggestions(restaurant, stats, request_breakdown, rating_stats, tip_stats, owner_trust, owner_intelligence, my_donations)
    
    return render_template(
        'owner.html',
        owner=restaurant,
        restaurant=restaurant,
        donations=my_donations,
        owner_requests=owner_requests,
        owner_delivery_handoffs=owner_delivery_handoffs,
        owner_request_history=owner_request_history,
        stats=stats,
        request_breakdown=request_breakdown,
        request_total=request_total,
        donation_pagination=donation_pagination,
        request_pagination=request_pagination,
        donation_status=donation_status,
        request_status=request_status,
        donation_search=donation_search,
        request_search=request_search,
        recent_feedback=recent_feedback,
        recent_tips=recent_tips,
        top_requested_items=top_requested_items,
        owner_trust=owner_trust,
        owner_intelligence=owner_intelligence,
        owner_suggestions=owner_suggestions,
        today_date=today_date,
        filter_url=_owner_filter_url,
    )


@app.route('/owner/live_insights')
def owner_live_insights():
    """Return live owner dashboard data for polling."""
    if 'owner_id' not in session:
        return jsonify({'success': False, 'error': 'Please login as an owner first.'}), 401

    owner_id = session['owner_id']
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'error': 'Database Connection Failed.'}), 500
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM Restaurant WHERE id = %s AND COALESCE(is_deleted, FALSE) = FALSE", (owner_id,))
    restaurant = cursor.fetchone()
    if not restaurant:
        cursor.close()
        conn.close()
        return jsonify({'success': False, 'error': 'Owner profile not found.'}), 404

    restaurant['items_served'] = restaurant.get('items_served') or ''
    _mark_owner_expired_donations(cursor, owner_id)
    conn.commit()

    request_status = request.args.get('request_status', 'Pending')
    request_search = (request.args.get('request_search') or '').strip()
    donation_status = request.args.get('donation_status', 'all')
    donation_search = (request.args.get('donation_search') or '').strip()

    donation_filters = ["d.restaurant_id = %s", "(d.created_at > NOW() - INTERVAL 12 HOUR OR d.status IN ('Available', 'PendingReview'))"]
    donation_params = [owner_id]
    if donation_status in {'Available', 'Claimed', 'Expired', 'PendingReview'}:
        donation_filters.append("d.status = %s")
        donation_params.append(donation_status)
    else:
        donation_status = 'all'
    if donation_search:
        donation_filters.append("(d.item_name LIKE %s OR d.item_type LIKE %s)")
        term = f"%{donation_search}%"
        donation_params.extend([term, term])
    donation_where = " AND ".join(donation_filters)
    cursor.execute(f"""
        SELECT d.*,
               DATE_FORMAT(d.date, '%Y-%m-%d') AS date,
               COALESCE(d.prep_time, '') AS prep_display,
               DATE_FORMAT(d.packed_time, '%Y-%m-%dT%H:%i') AS packed_time_input,
               DATE_FORMAT(d.best_before_time, '%Y-%m-%dT%H:%i') AS best_before_time_input,
               DATE_FORMAT(d.packed_time, '%Y-%m-%d %H:%i') AS packed_time_display,
               DATE_FORMAT(d.best_before_time, '%Y-%m-%d %H:%i') AS best_before_display,
               COALESCE(rs.pending_count, 0) AS pending_request_count,
               COALESCE(rs.accepted_count, 0) AS accepted_request_count,
               COALESCE(rs.ready_count, 0) AS ready_request_count,
               COALESCE(rs.collected_count, 0) AS collected_request_count,
               COALESCE(rs.delivered_count, 0) AS delivered_request_count,
               CASE
                   WHEN COALESCE(rs.delivered_count, 0) > 0 THEN 'Delivered'
                   WHEN COALESCE(rs.collected_count, 0) > 0 THEN 'Collected'
                   WHEN COALESCE(rs.ready_count, 0) > 0 THEN 'Ready'
                   WHEN COALESCE(rs.accepted_count, 0) > 0 THEN 'Accepted'
                   WHEN COALESCE(rs.pending_count, 0) > 0 THEN 'Requested'
                   WHEN d.status = 'Available' THEN 'Available'
                   ELSE d.status
               END AS display_status
        FROM Donation d
        LEFT JOIN (
            SELECT donation_id,
                   SUM(CASE WHEN status = 'Pending' THEN 1 ELSE 0 END) AS pending_count,
                   SUM(CASE WHEN status IN ('Accepted', 'OutForDelivery') AND food_ready_at IS NULL THEN 1 ELSE 0 END) AS accepted_count,
                   SUM(CASE WHEN status IN ('Accepted', 'OutForDelivery') AND food_ready_at IS NOT NULL THEN 1 ELSE 0 END) AS ready_count,
                   SUM(CASE WHEN status = 'Collected' THEN 1 ELSE 0 END) AS collected_count,
                   SUM(CASE WHEN status = 'Delivered' THEN 1 ELSE 0 END) AS delivered_count
            FROM FoodRequest
            GROUP BY donation_id
        ) rs ON rs.donation_id = d.id
        WHERE {donation_where}
        ORDER BY d.id DESC
    """, tuple(donation_params))
    owner_donations = cursor.fetchall()

    request_filters = ["d.restaurant_id = %s"]
    request_params = [owner_id]
    if request_status in {'Pending', 'Accepted', 'Rejected', 'Collected', 'NoShow', 'OutForDelivery', 'Delivered'}:
        request_filters.append("r.status = %s")
        request_params.append(request_status)
    elif request_status == 'all':
        pass
    else:
        request_status = 'Pending'
        request_filters.append("r.status = %s")
        request_params.append(request_status)
    if request_search:
        request_filters.append("(d.item_name LIKE %s OR u.name LIKE %s OR u.org_type LIKE %s)")
        term = f"%{request_search}%"
        request_params.extend([term, term, term])
    request_where = " AND ".join(request_filters)
    cursor.execute(f"""
        SELECT r.id as request_id, r.requested_amt, r.status as req_status,
               r.request_time, r.accepted_at, r.food_ready_at, r.pickup_reached_at, r.out_for_delivery_at, r.delivered_at,
               UNIX_TIMESTAMP(r.request_time) as request_ts,
               UNIX_TIMESTAMP(COALESCE(r.accepted_at, r.request_time)) as accepted_ts,
               UNIX_TIMESTAMP(r.food_ready_at) as food_ready_ts,
               UNIX_TIMESTAMP(r.pickup_reached_at) as pickup_reached_ts,
               UNIX_TIMESTAMP(r.out_for_delivery_at) as out_for_delivery_ts,
               TIMESTAMPDIFF(MINUTE, COALESCE(r.accepted_at, r.request_time), NOW()) >= 45 as can_mark_no_show,
               r.delivery_mode, r.delivery_charge_mode, r.delivery_address, r.delivery_latitude, r.delivery_longitude,
               r.delivery_location_accuracy_m, r.delivery_order_id, r.delivery_fee_paise, r.delivery_coin_used,
               r.delivery_coin_discount_paise, r.delivery_issue_type, r.delivery_issue_role, r.delivery_issue_detail,
               r.delivery_issue_reported_at,
               d.item_name, d.item_type, d.quantity as original_quantity,
               u.name as requester_name, u.contact as requester_contact, u.org_type as requester_org_type,
               dp.name as delivery_partner_name, dp.phone as delivery_partner_phone,
               dp.vehicle_type as delivery_partner_vehicle_type, dp.vehicle_number as delivery_partner_vehicle_number,
               dp.delivery_status as delivery_partner_status, dp.current_latitude as partner_latitude,
               dp.current_longitude as partner_longitude, dp.current_accuracy_m as partner_accuracy_m,
               dp.current_location_updated_at as partner_location_updated_at
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        LEFT JOIN User u ON r.user_id = u.id
        LEFT JOIN DeliveryPartner dp ON dp.id = r.delivery_partner_id
        WHERE {request_where}
        ORDER BY r.id DESC
    """, tuple(request_params))
    owner_requests = cursor.fetchall()

    cursor.execute("""
        SELECT r.id as request_id, r.requested_amt, r.status as req_status,
               r.request_time, r.accepted_at, r.food_ready_at, r.pickup_reached_at, r.out_for_delivery_at,
               UNIX_TIMESTAMP(r.request_time) as request_ts,
               UNIX_TIMESTAMP(COALESCE(r.accepted_at, r.request_time)) as accepted_ts,
               UNIX_TIMESTAMP(r.food_ready_at) as food_ready_ts,
               UNIX_TIMESTAMP(r.pickup_reached_at) as pickup_reached_ts,
               UNIX_TIMESTAMP(r.out_for_delivery_at) as out_for_delivery_ts,
               r.delivery_mode, r.delivery_charge_mode, r.delivery_address, r.delivery_latitude, r.delivery_longitude,
               r.delivery_location_accuracy_m, r.delivery_order_id, r.delivery_fee_paise,
               r.delivery_issue_type, r.delivery_issue_role, r.delivery_issue_detail, r.delivery_issue_reported_at,
               d.item_name, d.item_type, d.quantity as original_quantity,
               u.name as requester_name, u.contact as requester_contact, u.org_type as requester_org_type,
               dp.name as delivery_partner_name, dp.phone as delivery_partner_phone,
               dp.vehicle_type as delivery_partner_vehicle_type, dp.vehicle_number as delivery_partner_vehicle_number,
               dp.delivery_status as delivery_partner_status, dp.current_latitude as partner_latitude,
               dp.current_longitude as partner_longitude, dp.current_accuracy_m as partner_accuracy_m,
               dp.current_location_updated_at as partner_location_updated_at
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        LEFT JOIN User u ON r.user_id = u.id
        LEFT JOIN DeliveryPartner dp ON dp.id = r.delivery_partner_id
        WHERE d.restaurant_id = %s
          AND r.delivery_mode = 'Delivery'
          AND r.status IN ('Accepted', 'OutForDelivery')
        ORDER BY CASE WHEN r.status = 'OutForDelivery' THEN 0 ELSE 1 END,
                 COALESCE(r.out_for_delivery_at, r.pickup_reached_at, r.food_ready_at, r.accepted_at, r.request_time) DESC
    """, (owner_id,))
    owner_delivery_handoffs = cursor.fetchall()

    cursor.execute("""
        SELECT
            SUM(CASE WHEN r.status = 'Pending' THEN 1 ELSE 0 END) as pending_count,
            SUM(CASE WHEN r.status = 'Accepted' THEN 1 ELSE 0 END) as accepted_count,
            SUM(CASE WHEN r.status = 'OutForDelivery' THEN 1 ELSE 0 END) as out_for_delivery_count,
            SUM(CASE WHEN r.status = 'Delivered' THEN 1 ELSE 0 END) as delivered_count,
            SUM(CASE WHEN r.status = 'Rejected' THEN 1 ELSE 0 END) as rejected_count,
            SUM(CASE WHEN r.status = 'Collected' THEN 1 ELSE 0 END) as collected_count,
            SUM(CASE WHEN r.status = 'NoShow' THEN 1 ELSE 0 END) as no_show_count,
            COUNT(*) as total_count
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        WHERE d.restaurant_id = %s
    """, (owner_id,))
    request_breakdown = cursor.fetchone() or {}
    request_total = int(request_breakdown.get('total_count') or 0)

    cursor.execute("""
        SELECT
            COUNT(*) as rating_count,
            AVG(taste_rating) as avg_rating
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        WHERE d.restaurant_id = %s
          AND r.taste_rating IS NOT NULL
          AND COALESCE(r.rated_at, r.request_time) >= NOW() - INTERVAL 7 DAY
    """, (owner_id,))
    rating_stats = cursor.fetchone() or {}

    cursor.execute("""
        SELECT r.taste_rating, r.taste_feedback, r.rated_at,
               d.item_name, u.name as user_name
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        LEFT JOIN User u ON r.user_id = u.id
        WHERE d.restaurant_id = %s
          AND r.taste_rating IS NOT NULL
        ORDER BY COALESCE(r.rated_at, r.request_time) DESC
        LIMIT 2
    """, (owner_id,))
    recent_feedback = cursor.fetchall()

    cursor.execute("""
        SELECT COUNT(*) AS tip_count,
               COALESCE(SUM(amount_paise), 0) AS total_tip_paise
        FROM RequestTip tip
        JOIN FoodRequest r ON tip.request_id = r.id
        JOIN Donation d ON tip.donation_id = d.id
        WHERE d.restaurant_id = %s
          AND tip.status = 'Paid'
    """, (owner_id,))
    tip_stats = cursor.fetchone() or {}

    cursor.execute("""
        SELECT tip.amount_paise, tip.paid_at, tip.note, d.item_name, u.name AS user_name
        FROM RequestTip tip
        JOIN FoodRequest r ON tip.request_id = r.id
        JOIN Donation d ON tip.donation_id = d.id
        LEFT JOIN User u ON tip.user_id = u.id
        WHERE d.restaurant_id = %s
          AND tip.status = 'Paid'
        ORDER BY tip.paid_at DESC
        LIMIT 5
    """, (owner_id,))
    recent_tips = cursor.fetchall()

    cursor.execute("""
        SELECT
            d.item_type,
            COUNT(*) AS request_count,
            SUM(CASE WHEN r.status = 'Pending' THEN 1 ELSE 0 END) AS pending_count,
            SUM(CASE WHEN r.status = 'Accepted' THEN 1 ELSE 0 END) AS accepted_count,
            SUM(CASE WHEN r.status = 'Collected' THEN 1 ELSE 0 END) AS collected_count
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        WHERE d.restaurant_id = %s
        GROUP BY d.item_type
        ORDER BY request_count DESC, d.item_type ASC
        LIMIT 5
    """, (owner_id,))
    item_type_rows = cursor.fetchall()

    cursor.execute("""
        SELECT r.donation_id, r.id AS request_id, r.requested_amt, r.status,
               r.request_time, r.accepted_at, r.food_ready_at, r.out_for_delivery_at,
               r.delivered_at, u.name AS requester_name, u.org_type AS requester_org_type
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        LEFT JOIN User u ON r.user_id = u.id
        WHERE d.restaurant_id = %s
        ORDER BY r.request_time DESC, r.id DESC
    """, (owner_id,))
    owner_request_history_rows = cursor.fetchall()
    owner_request_history = {}
    for history_row in owner_request_history_rows:
        owner_request_history.setdefault(history_row.get('donation_id'), []).append(history_row)
    for donation in owner_donations:
        donation['request_history'] = owner_request_history.get(donation.get('id'), [])

    cursor.execute("""
        SELECT d.item_name,
               COUNT(*) AS request_count,
               COALESCE(SUM(CASE WHEN r.status IN ('Collected', 'Delivered') THEN r.requested_amt ELSE 0 END), 0) AS fulfilled_servings
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        WHERE d.restaurant_id = %s
        GROUP BY d.item_name
        ORDER BY request_count DESC, fulfilled_servings DESC, d.item_name ASC
        LIMIT 5
    """, (owner_id,))
    top_requested_items = cursor.fetchall()

    stats = {
        'total_donations': len(owner_donations),
        'available_donations': sum(1 for d in owner_donations if d.get('status') == 'Available'),
        'claimed_donations': sum(1 for d in owner_donations if d.get('status') == 'Claimed'),
        'pending_requests': request_breakdown.get('pending_count', 0) or 0,
        'unread_chats': 0,
        'recent_rating_count': rating_stats.get('rating_count', 0) or 0,
        'avg_taste_rating': float(rating_stats.get('avg_rating') or 0),
        'tip_count': tip_stats.get('tip_count', 0) or 0,
        'tip_total_rupees': (float(tip_stats.get('total_tip_paise') or 0) / 100.0),
        'donations_posted': len(owner_donations),
        'meals_saved': sum(int(row.get('fulfilled_servings') or 0) for row in top_requested_items),
        'top_requested_items': top_requested_items,
    }

    owner_trust = _build_owner_trust_profile(request_breakdown, rating_stats, tip_stats, len(owner_donations))
    owner_intelligence = _build_owner_intelligence(owner_requests, item_type_rows, request_breakdown)
    realtime_summary = build_realtime_summary(cursor, 'owner', owner_id)

    cursor.close()
    conn.close()

    return jsonify({
        'success': True,
        'owner_requests': owner_requests,
        'owner_delivery_handoffs': owner_delivery_handoffs,
        'owner_donations': owner_donations,
        'owner_request_history': owner_request_history,
        'stats': stats,
        'request_breakdown': request_breakdown,
        'request_total': request_total,
        'owner_trust': owner_trust,
        'owner_intelligence': owner_intelligence,
        'realtime_summary': realtime_summary,
        'recent_feedback': recent_feedback,
        'recent_tips': recent_tips,
        'top_requested_items': top_requested_items,
    })


@app.route('/owner/live_stream')
def owner_live_stream():
    if 'owner_id' not in session:
        return jsonify({'success': False, 'error': 'Please login as an owner first.'}), 401
    return _stream_live_snapshot(
        owner_live_insights,
        7,
        'Please login as an owner first.',
    )

@app.route('/owner/delete_donation/<int:donation_id>', methods=['POST'])
@require_session_key('owner_id', 'owner_login', 'Please login as an owner first.')
def delete_donation(donation_id):
    """Owner deletes one of their donation listings."""
    if 'owner_id' not in session:
        return redirect(url_for('owner_login'))

    owner_id = session['owner_id']
    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        "SELECT id FROM Donation WHERE id = %s AND restaurant_id = %s",
        (donation_id, owner_id)
    )
    donation = cursor.fetchone()
    if not donation:
        conn.close()
        flash('Donation not found or you do not have permission to delete it.', 'warning')
        return redirect(url_for('owner_dashboard'))

    # Remove dependent requests first to avoid foreign key conflicts.
    cursor.execute("DELETE FROM FoodRequest WHERE donation_id = %s", (donation_id,))
    cursor.execute("DELETE FROM Donation WHERE id = %s AND restaurant_id = %s", (donation_id, owner_id))
    conn.commit()
    conn.close()

    flash('Donation deleted successfully.', 'success')
    return redirect(url_for('owner_dashboard'))


@app.route('/owner/duplicate_donation/<int:donation_id>', methods=['POST'])
@require_session_key('owner_id', 'owner_login', 'Please login as an owner first.')
def duplicate_donation(donation_id):
    """Owner creates a copy of an existing donation listing."""
    if 'owner_id' not in session:
        return redirect(url_for('owner_login'))

    owner_id = session['owner_id']
    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        "SELECT * FROM Donation WHERE id = %s AND restaurant_id = %s",
        (donation_id, owner_id)
    )
    donation = cursor.fetchone()
    if not donation:
        conn.close()
        flash('Donation not found or you do not have permission to duplicate it.', 'warning')
        return redirect(url_for('owner_dashboard'))

    cursor.execute(
        "SELECT verified FROM Restaurant WHERE id = %s AND COALESCE(is_deleted, FALSE) = FALSE",
        (owner_id,)
    )
    restaurant = cursor.fetchone() or {}
    auto_approve_listing = bool(restaurant.get('verified'))
    listing_status = 'Available' if auto_approve_listing else 'PendingReview'
    quality_status = 'Approved' if auto_approve_listing else 'Pending'
    cursor.execute(
        """
        INSERT INTO Donation (
            restaurant_id, item_type, item_name, quantity, prep_time, date, image_url,
            status, quality_status, packed_time, best_before_time, storage_note
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            owner_id,
            donation.get('item_type'),
            donation.get('item_name'),
            donation.get('quantity'),
            donation.get('prep_time'),
            donation.get('date'),
            donation.get('image_url'),
            listing_status,
            quality_status,
            donation.get('packed_time'),
            donation.get('best_before_time'),
            donation.get('storage_note'),
        )
    )
    conn.commit()
    conn.close()

    if auto_approve_listing:
        flash('Donation duplicated and published successfully.', 'success')
    else:
        flash('Donation duplicated and submitted for admin quality review.', 'success')
    return redirect(url_for('owner_dashboard'))


@app.route('/owner/edit_donation/<int:donation_id>', methods=['POST'])
@require_session_key('owner_id', 'owner_login', 'Please login as an owner first.')
def edit_donation(donation_id):
    """Owner updates an existing donation listing."""
    if 'owner_id' not in session:
        return redirect(url_for('owner_login'))

    owner_id = session['owner_id']
    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        "SELECT * FROM Donation WHERE id = %s AND restaurant_id = %s",
        (donation_id, owner_id)
    )
    donation = cursor.fetchone()
    if not donation:
        conn.close()
        flash('Donation not found or you do not have permission to edit it.', 'warning')
        return redirect(url_for('owner_dashboard'))

    prep_hours = request.form.get('prep_hours')
    prep_minutes = request.form.get('prep_minutes')
    if prep_hours is not None and prep_minutes is not None:
        try:
            prep_time = f"{int(prep_hours)}h {int(prep_minutes):02d}m"
        except (TypeError, ValueError):
            prep_time = donation.get('prep_time')
    else:
        prep_time = request.form.get('prep_time') or donation.get('prep_time')

    item_type = clean_text(request.form.get('item_type')) or donation.get('item_type')
    donation_date = clean_text(request.form.get('date')) or donation.get('date')
    packed_time, best_before_time, storage_note, safety_error = _prepare_donation_safety_fields(
        request.form,
        item_type,
        donation_date,
        prep_time,
    )
    if safety_error:
        conn.close()
        flash(safety_error, 'danger')
        return redirect(url_for('owner_dashboard'))

    requested_status = clean_text(request.form.get('status')) or donation.get('status')
    allowed_statuses = {'Available', 'Claimed', 'PendingReview', 'Expired'}
    if requested_status not in allowed_statuses:
        requested_status = donation.get('status') or 'Available'

    image_url = (request.form.get('existing_image_url') or '').strip() or donation.get('image_url')
    if 'donation_image' in request.files:
        file = request.files['donation_image']
        if file and file.filename:
            saved_url, error = save_uploaded_file(
                file,
                app.config['UPLOAD_FOLDER'],
                '/static/uploads',
                allowed_extensions=ALLOWED_EXTENSIONS,
                max_size=2 * 1024 * 1024,
                filename_prefix=f"donation_{owner_id}",
                type_error_message='Donation image must be JPG, PNG, or GIF.',
                size_error_message='Donation image is too large. Max 2 MB allowed.',
            )
            if error:
                conn.close()
                flash(error, 'danger')
                return redirect(url_for('owner_dashboard'))
            image_url = saved_url

    cursor.execute(
        """
        UPDATE Donation
        SET item_name = %s,
            quantity = %s,
            prep_time = %s,
            date = %s,
            item_type = %s,
            image_url = %s,
            status = %s,
            packed_time = %s,
            best_before_time = %s,
            storage_note = %s
        WHERE id = %s AND restaurant_id = %s
        """,
        (
            request.form.get('item_name'),
            request.form.get('quantity'),
            prep_time,
            donation_date,
            item_type,
            image_url,
            requested_status,
            packed_time,
            best_before_time,
            storage_note,
            donation_id,
            owner_id,
        )
    )
    conn.commit()
    conn.close()

    flash('Donation updated successfully.', 'success')
    return redirect(url_for('owner_dashboard'))

@app.route('/owner/reject/<int:request_id>', methods=['POST'])
@require_session_key('owner_id', 'owner_login', 'Please login as an owner first.')
def reject_request(request_id):
    """Owner rejects a pending food request with an optional reason"""
    if 'owner_id' not in session:
        return redirect(url_for('owner_login'))
        
    reason = request.form.get('rejection_reason', '').strip()
    owner_id = session['owner_id']
        
    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor()
    
    cursor.execute(
        """
        UPDATE FoodRequest fr
        JOIN Donation d ON fr.donation_id = d.id
        SET fr.status = 'Rejected',
            fr.rejection_reason = %s
        WHERE fr.id = %s
          AND d.restaurant_id = %s
          AND fr.status = 'Pending'
        """,
        (reason if reason else None, request_id, owner_id)
    )
    updated = cursor.rowcount
    conn.commit()
    conn.close()
    
    flash('Request declined successfully.' if updated else 'Only pending requests for your listings can be rejected.', 'info' if updated else 'warning')
    return redirect(url_for('owner_dashboard'))

@app.route('/owner/accept/<int:request_id>', methods=['POST'])
@require_session_key('owner_id', 'owner_login', 'Please login as an owner first.')
def accept_request(request_id):
    """Owner accepts a pending food request"""
    if 'owner_id' not in session:
        return redirect(url_for('owner_login'))
    owner_id = session['owner_id']
        
    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor(dictionary=True)
    
    # Get the request details
    cursor.execute(
        """
        SELECT r.*, d.quantity AS donation_quantity, d.item_name
        FROM FoodRequest r
        JOIN Donation d ON r.donation_id = d.id
        WHERE r.id = %s
          AND r.status = 'Pending'
          AND d.restaurant_id = %s
        """,
        (request_id, owner_id)
    )
    req = cursor.fetchone()
    
    if req:
        donation_id = req['donation_id']
        try:
            collected_amt = int(req['requested_amt'])
        except (TypeError, ValueError):
            collected_amt = 0
        
        donation = {'quantity': req.get('donation_quantity'), 'item_name': req.get('item_name')}
        
        if donation:
            current_qty_str = str(donation['quantity'])
            import re
            match = re.search(r'\d+', current_qty_str)
            current_num = int(match.group()) if match else 0

            if collected_amt <= 0:
                conn.close()
                flash('Requested quantity is invalid and cannot be accepted.', 'danger')
                return redirect(url_for('owner_dashboard'))

            if collected_amt > current_num:
                conn.close()
                flash(
                    f"Cannot accept this request. Requested quantity ({collected_amt}) exceeds available quantity ({current_num}).",
                    'danger'
                )
                return redirect(url_for('owner_dashboard'))
            
            if collected_amt >= current_num:
                cursor.execute("UPDATE Donation SET status = 'Claimed', quantity = '0' WHERE id = %s AND restaurant_id = %s", (donation_id, owner_id))
            else:
                new_num = current_num - collected_amt
                if match:
                    new_qty_str = re.sub(r'\d+', str(new_num), current_qty_str, count=1)
                else:
                    new_qty_str = str(new_num)
                cursor.execute("UPDATE Donation SET quantity = %s WHERE id = %s AND restaurant_id = %s", (new_qty_str, donation_id, owner_id))

            delivery_mode = (req.get('delivery_mode') or '').strip()
            has_delivery_details = bool(
                (req.get('delivery_order_id') or '').strip() or
                (req.get('delivery_address') or '').strip() or
                (req.get('delivery_latitude') is not None and req.get('delivery_longitude') is not None)
            )
            if delivery_mode != 'Delivery' and has_delivery_details:
                cursor.execute(
                    "UPDATE FoodRequest SET status = 'Accepted', accepted_at = CURRENT_TIMESTAMP, delivery_mode = 'Delivery' WHERE id = %s",
                    (request_id,)
                )
            else:
                cursor.execute(
                    "UPDATE FoodRequest SET status = 'Accepted', accepted_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (request_id,)
                )
            
            # --- MOCK EMAIL NOTIFICATION ---
            cursor.execute("SELECT name FROM User WHERE id = %s", (req['user_id'],))
            user_data = cursor.fetchone()
            if user_data:
                print(f"[MOCK EMAIL] To: {user_data['name']} | Status: Your food request for '{donation['item_name'] if 'item_name' in donation else 'Food Item'}' was ACCEPTED. Please pick up within 12 hours.")
            # -------------------------------

            conn.commit()
            flash('Thank you for the donation! The request has been accepted successfully.', 'success')
        else:
            flash('Associated donation not found.', 'danger')
    else:
        flash('Request not found or already processed.', 'warning')
        
    conn.close()
    return redirect(url_for('owner_dashboard'))

@app.route('/owner/ready/<int:request_id>', methods=['POST'])
@require_session_key('owner_id', 'owner_login', 'Please login as an owner first.')
def owner_mark_food_ready(request_id):
    """Owner marks accepted food as packed and ready for pickup/handoff."""
    if 'owner_id' not in session:
        return redirect(url_for('owner_login'))

    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE FoodRequest fr
        JOIN Donation d ON fr.donation_id = d.id
        SET fr.food_ready_at = COALESCE(fr.food_ready_at, CURRENT_TIMESTAMP)
        WHERE fr.id = %s
          AND d.restaurant_id = %s
          AND fr.status = 'Accepted'
          AND fr.pickup_reached_at IS NULL
        """,
        (request_id, session['owner_id'])
    )
    updated = cursor.rowcount
    conn.commit()
    cursor.close()
    conn.close()

    flash('Food marked packed and ready for pickup.' if updated else 'Only accepted requests before pickup can be marked ready.', 'success' if updated else 'warning')
    return redirect(url_for('owner_dashboard'))

@app.route('/owner/delivery_issue/<int:request_id>', methods=['POST'])
@require_session_key('owner_id', 'owner_login', 'Please login as an owner first.')
def owner_report_delivery_issue(request_id):
    """Owner reports a handoff or delivery coordination issue."""
    if 'owner_id' not in session:
        return redirect(url_for('owner_login'))

    issue_labels = {
        'user_unreachable': 'User unreachable',
        'partner_delayed': 'Partner delayed',
        'food_not_ready': 'Food not ready',
        'wrong_address': 'Wrong address',
    }
    issue_type = (request.form.get('issue_type') or '').strip()
    detail = (request.form.get('issue_detail') or '').strip()[:255]
    if issue_type not in issue_labels:
        flash('Choose a valid delivery issue.', 'warning')
        return redirect(url_for('owner_dashboard'))

    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE FoodRequest fr
        JOIN Donation d ON fr.donation_id = d.id
        SET fr.delivery_issue_type = %s,
            fr.delivery_issue_role = 'Owner',
            fr.delivery_issue_detail = %s,
            fr.delivery_issue_reported_at = CURRENT_TIMESTAMP
        WHERE fr.id = %s
          AND d.restaurant_id = %s
          AND fr.delivery_mode = 'Delivery'
          AND fr.status IN ('Accepted', 'OutForDelivery')
        """,
        (issue_type, detail or issue_labels[issue_type], request_id, session['owner_id'])
    )
    updated = cursor.rowcount
    conn.commit()
    cursor.close()
    conn.close()

    flash(f"Issue reported: {issue_labels[issue_type]}." if updated else 'Only active delivery handoffs can be reported.', 'info' if updated else 'warning')
    return redirect(url_for('owner_dashboard'))

@app.route('/owner/collected/<int:request_id>', methods=['POST'])
@require_session_key('owner_id', 'owner_login', 'Please login as an owner first.')
def mark_collected(request_id):
    """Owner confirms that a user collected the food."""
    if 'owner_id' not in session:
        return redirect(url_for('owner_login'))
    owner_id = session['owner_id']

    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE FoodRequest fr
        JOIN Donation d ON fr.donation_id = d.id
        SET fr.status = 'Collected'
        WHERE fr.id = %s
          AND d.restaurant_id = %s
          AND fr.status = 'Accepted'
          AND COALESCE(fr.delivery_mode, 'Pickup') <> 'Delivery'
          AND COALESCE(fr.accepted_at, fr.request_time) <= NOW() - INTERVAL 45 MINUTE
        """,
        (request_id, owner_id)
    )
    updated = cursor.rowcount
    conn.commit()
    conn.close()

    if updated:
        flash('Marked as collected.', 'success')
    else:
        flash('Only accepted pickup requests for your listings can be marked collected.', 'warning')
    return redirect(url_for('owner_dashboard'))

@app.route('/owner/out_for_delivery/<int:request_id>', methods=['POST'])
@require_session_key('owner_id', 'owner_login', 'Please login as an owner first.')
def owner_mark_out_for_delivery(request_id):
    """Owner marks an accepted delivery request as out for delivery."""
    if 'owner_id' not in session:
        return redirect(url_for('owner_login'))

    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE FoodRequest fr
        JOIN Donation d ON fr.donation_id = d.id
        SET fr.status = 'OutForDelivery',
            fr.out_for_delivery_at = CURRENT_TIMESTAMP
        WHERE fr.id = %s
          AND d.restaurant_id = %s
          AND fr.status = 'Accepted'
          AND fr.delivery_mode = 'Delivery'
          AND fr.delivery_partner_id IS NOT NULL
          AND fr.food_ready_at IS NOT NULL
          AND fr.pickup_reached_at IS NOT NULL
        """,
        (request_id, session['owner_id'])
    )
    updated = cursor.rowcount
    conn.commit()
    cursor.close()
    conn.close()

    if updated:
        flash('Marked as out for delivery.', 'success')
    else:
        flash('Delivery can move out only after food is packed, a partner is assigned, and the partner has reached pickup.', 'warning')
    return redirect(url_for('owner_dashboard'))

@app.route('/owner/delivered/<int:request_id>', methods=['POST'])
@require_session_key('owner_id', 'owner_login', 'Please login as an owner first.')
def owner_mark_delivered(request_id):
    """Delivery completion is OTP-protected and must be done by the partner."""
    if 'owner_id' not in session:
        return redirect(url_for('owner_login'))
    flash('Delivery completion requires the recipient OTP. Ask the delivery partner to enter the OTP on their dashboard.', 'warning')
    return redirect(url_for('owner_dashboard'))

@app.route('/owner/no_show/<int:request_id>', methods=['POST'])
@require_session_key('owner_id', 'owner_login', 'Please login as an owner first.')
def mark_no_show(request_id):
    """Owner marks a request as no-show."""
    if 'owner_id' not in session:
        return redirect(url_for('owner_login'))
    owner_id = session['owner_id']

    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE FoodRequest fr
        JOIN Donation d ON fr.donation_id = d.id
        SET fr.status = 'NoShow'
        WHERE fr.id = %s
          AND d.restaurant_id = %s
          AND fr.status = 'Accepted'
          AND COALESCE(fr.delivery_mode, 'Pickup') <> 'Delivery'
        """,
        (request_id, owner_id)
    )
    updated = cursor.rowcount
    conn.commit()
    conn.close()

    if updated:
        flash('Marked as no-show.', 'info')
    else:
        flash('No-show is available only for accepted pickup requests after the pickup window has passed.', 'warning')
    return redirect(url_for('owner_dashboard'))

@app.route('/owner/archive_solved_chats', methods=['POST'])
@require_session_key('owner_id', 'owner_login', 'Please login as an owner first.')
def owner_archive_solved_chats():
    """Owner deletes/archives solved direct chats."""
    if 'owner_id' not in session:
        return redirect(url_for('owner_login'))

    owner_id = session['owner_id']
    conn = get_db_connection()
    if not conn:
        return "Database Connection Failed.", 500
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM messages WHERE session_id LIKE %s AND status = 'Solved'",
        (f"direct_u%_o{owner_id}",)
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()

    if deleted:
        flash(f'Archived {deleted} solved chat message(s).', 'success')
    else:
        flash('No solved chats to archive.', 'info')
    return redirect(url_for('owner_dashboard'))



if __name__ == '__main__':
    initialize_database()
    debug_mode = os.environ.get('FLASK_DEBUG', '').strip().lower() in {'1', 'true', 'yes', 'on'}
    app.run(debug=debug_mode, port=5001)
