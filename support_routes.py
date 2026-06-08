import json
import time

from flask import Blueprint, Response, request, jsonify, session, stream_with_context
from db import get_db_connection
import datetime
import os
import uuid
import re
from upload_utils import save_uploaded_file
from chat_realtime import chat_realtime

support_bp = Blueprint('support', __name__)
def _presence_key(role, identifier=None):
    if role == 'Admin':
        return 'admin'
    if role == 'Guest':
        return f'guest:{session.get("session_id") or identifier or "anonymous"}'
    if identifier is None:
        return f'{role.lower()}:unknown'
    return f'{role.lower()}:{identifier}'


def _track_presence(role, identifier=None):
    chat_realtime.touch_presence(role, identifier)
    return _presence_key(role, identifier)


def _is_presence_online(key):
    if key == 'chat:presence:admin':
        return chat_realtime.is_online('Admin')
    if key.startswith('chat:presence:user:'):
        return chat_realtime.is_online('User', key.split(':', 3)[3])
    if key.startswith('chat:presence:owner:'):
        return chat_realtime.is_online('Owner', key.split(':', 3)[3])
    if key.startswith('chat:presence:partner:'):
        return chat_realtime.is_online('Partner', key.split(':', 3)[3])
    if key.startswith('chat:presence:guest:'):
        return chat_realtime.is_online('Guest', key.split(':', 3)[3])
    return False


def _set_typing(session_id, role, identifier, sender_name, is_typing):
    chat_realtime.set_typing(session_id, role, identifier, sender_name, is_typing)


def _get_typing_state(session_id):
    return chat_realtime.get_typing(session_id)


def _peer_presence_key(session_id, viewer_role, viewer_id):
    if session_id.startswith('direct_u'):
        match = re.search(r'direct_u(.+)_o(\d+)', session_id)
        if match:
            user_id, owner_id = match.groups()
            if viewer_role == 'User':
                return _presence_key('Owner', owner_id)
            if viewer_role == 'Owner':
                return _presence_key('User', user_id)
    if viewer_role in {'User', 'Owner', 'Partner'}:
        return _presence_key('Admin')
    if viewer_role == 'Admin':
        if session_id.startswith('support_u'):
            return _presence_key('User', session_id.replace('support_u', ''))
        if session_id.startswith('support_o'):
            return _presence_key('Owner', session_id.replace('support_o', ''))
        if session_id.startswith('support_p'):
            return _presence_key('Partner', session_id.replace('support_p', ''))
        if session_id.startswith('direct_u'):
            match = re.search(r'direct_u(.+)_o(\d+)', session_id)
            if match:
                user_id, owner_id = match.groups()
                return _presence_key('User', user_id)
    return None


def _build_chat_realtime_payload(session_id, viewer_role, viewer_id):
    typing_state = _get_typing_state(session_id)
    peer_key = _peer_presence_key(session_id, viewer_role, viewer_id)
    peer_online = _is_presence_online(peer_key) if peer_key else False
    typing_visible = False
    typing_name = None
    if typing_state:
        typing_role = typing_state.get('role')
        typing_identifier = typing_state.get('identifier')
        if typing_role != viewer_role or str(typing_identifier) != str(viewer_id):
            typing_visible = True
            typing_name = typing_state.get('sender_name') or typing_role
    return {
        'session_id': session_id,
        'peer_online': peer_online,
        'typing': typing_visible,
        'typing_name': typing_name,
        'updated_at': datetime.datetime.utcnow().isoformat() + 'Z',
    }


def _mark_messages_read(conn, session_id, viewer_role, viewer_id):
    if not session_id:
        return
    cursor = conn.cursor()
    if viewer_role == 'Admin':
        cursor.execute(
            """
            UPDATE messages
            SET read_at = COALESCE(read_at, NOW())
            WHERE session_id = %s AND sender_role <> 'Admin'
            """,
            (session_id,),
        )
    elif viewer_role == 'User':
        cursor.execute(
            """
            UPDATE messages
            SET read_at = COALESCE(read_at, NOW())
            WHERE session_id = %s AND sender_role <> 'User'
            """,
            (session_id,),
        )
    elif viewer_role == 'Owner':
        cursor.execute(
            """
            UPDATE messages
            SET read_at = COALESCE(read_at, NOW())
            WHERE session_id = %s AND sender_role <> 'Owner'
            """,
            (session_id,),
        )
    elif viewer_role == 'Partner':
        cursor.execute(
            """
            UPDATE messages
            SET read_at = COALESCE(read_at, NOW())
            WHERE session_id = %s AND sender_role <> 'Partner'
            """,
            (session_id,),
        )
    conn.commit()
    cursor.close()


def _get_direct_session_id(user_id, owner_id):
    return f"direct_u{user_id}_o{owner_id}"


def _can_access_session(session_id):
    if session.get('is_admin_authenticated') or session.get('is_admin'):
        return True
    if not session_id:
        return False
    if session_id == session.get('session_id'):
        return True
    if 'user_id' in session:
        user_id = str(session['user_id'])
        return session_id == f"support_u{user_id}" or session_id.startswith(f"direct_u{user_id}_o")
    if 'owner_id' in session:
        owner_id = str(session['owner_id'])
        return session_id == f"support_o{owner_id}" or session_id.endswith(f"_o{owner_id}")
    if 'partner_id' in session:
        partner_id = str(session['partner_id'])
        return session_id == f"support_p{partner_id}"
    return False


def _parse_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in {'true', '1', 'yes', 'on'}
    return bool(value)


def _resolve_sender_identity():
    if session.get('is_admin_authenticated') or session.get('is_admin'):
        return 'Admin', None, 'Admin', True

    if 'user_id' in session:
        return 'User', session['user_id'], session.get('user_name', 'User'), False

    if 'owner_id' in session:
        return 'Owner', session['owner_id'], session.get('owner_name', 'Owner'), False

    if 'partner_id' in session:
        return 'Partner', session['partner_id'], session.get('partner_name', 'Delivery Partner'), False

    return 'Guest', None, 'Guest', False


def _validate_request_identity(data, expected_role, expected_is_admin):
    if 'sender_role' in data:
        requested_role = (data.get('sender_role') or '').strip()
        if requested_role != expected_role:
            return jsonify({"error": "Sender role does not match your session."}), 403

    if 'is_admin' in data:
        if _parse_bool(data.get('is_admin')) != expected_is_admin:
            return jsonify({"error": "Admin flag does not match your session."}), 403

    return None

@support_bp.route('/chat/send', methods=['POST'])
def send_message():
    # Handle both JSON and Multipart form data
    if request.is_json:
        data = request.json
        file = None
    else:
        # For file uploads, data is in request.form
        data = request.form
        file = request.files.get('file')

    message = data.get('message')

    topic = data.get('topic', 'General')
    file_url = None

    if not message and not file:
        return jsonify({"error": "Message or File is required"}), 400

    # Save file if present
    if file and file.filename != '':
        file_url, error = save_uploaded_file(
            file,
            os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'support'),
            '/static/uploads/support',
            filename_prefix=uuid.uuid4().hex,
        )
        if error:
            return jsonify({"error": error}), 400
        
    chat_type = data.get('chat_type', 'Support')
    sender_id = None
    receiver_id = data.get('receiver_id')
    receiver_role = data.get('receiver_role')
    restaurant_id = data.get('restaurant_id')
    
    session_id = data.get('session_id') or session.get('session_id')
    
    sender_role, sender_id, sender_name, is_admin = _resolve_sender_identity()
    identity_error = _validate_request_identity(data, sender_role, is_admin)
    if identity_error:
        return identity_error

    if sender_role == 'Guest':
        sender_name = (data.get('sender_name') or '').strip() or 'Guest'

    # For Direct chats, we ignore the global "Support" session ID and force a direct ID
    if chat_type == 'Direct' and receiver_id:
        if sender_role == 'Guest':
            return jsonify({"error": "Please login to contact a restaurant directly."}), 403
        if sender_role == 'User':
            session_id = _get_direct_session_id(sender_id, receiver_id)
        elif sender_role == 'Owner':
            session_id = _get_direct_session_id(receiver_id, sender_id)
        else:
            return jsonify({"error": "Invalid direct chat sender."}), 400
            
        receiver_role = 'Owner' if sender_role != 'Owner' else 'User'
        restaurant_id = receiver_id
        topic = topic or 'Restaurant Inquiry'
    elif is_admin and session_id:
        if session_id.startswith('support_u'):
            u_id_str = session_id.replace('support_u', '')
            receiver_role = 'User'
            # Check if it's a numeric ID or a guest string
            if u_id_str.isdigit():
                receiver_id = int(u_id_str)
            else:
                receiver_id = None # Guest
        elif session_id.startswith('support_o'):
            o_id_str = session_id.replace('support_o', '')
            receiver_role = 'Owner'
            if o_id_str.isdigit():
                receiver_id = int(o_id_str)
            else:
                receiver_id = None # Guest
        elif session_id.startswith('support_p'):
            p_id_str = session_id.replace('support_p', '')
            receiver_role = 'Partner'
            if p_id_str.isdigit():
                receiver_id = int(p_id_str)
            else:
                receiver_id = None
        elif session_id.startswith('user_'):
            u_id_str = session_id.replace('user_', '')
            receiver_role = 'User'
            if u_id_str.isdigit():
                receiver_id = int(u_id_str)
            else:
                receiver_id = None # Guest
        elif session_id.startswith('owner_'):
            o_id_str = session_id.replace('owner_', '')
            receiver_role = 'Owner'
            if o_id_str.isdigit():
                receiver_id = int(o_id_str)
            else:
                receiver_id = None # Guest
        elif 'direct_u' in session_id:
            match = re.search(r'direct_u(\d+)_o(\d+)', session_id)
            if match:
                u_id, o_id = match.groups()
                # Admins usually reply to Users, but let's be safe
                receiver_role = 'User'
                receiver_id = u_id
        else:
            # Default for neutral or guest sessions (support_g_, etc.)
            receiver_role = 'User'
            receiver_id = None
    elif not session_id or chat_type == 'Support':
        if chat_type == 'Support':
            if sender_role == 'User':
                session_id = f"support_u{sender_id}"
                receiver_role = 'Admin'
            elif sender_role == 'Owner':
                session_id = f"support_o{sender_id}"
                receiver_role = 'Admin'
            elif sender_role == 'Partner':
                session_id = f"support_p{sender_id}"
                receiver_role = 'Admin'
            else:
                # Guest Support
                session_id = session.get('session_id')
                if not session_id or not session_id.startswith('support_'):
                    session_id = f"support_g_{str(uuid.uuid4())[:8]}"
                    session['session_id'] = session_id
                receiver_role = 'Admin'
    
    # Final check for NOT NULL columns
    if not session_id:
        session_id = str(uuid.uuid4())
    if not sender_name:
        sender_name = sender_role if sender_role != 'Guest' else 'Guest'

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO messages (session_id, sender_name, sender_role, receiver_id, receiver_role, restaurant_id, topic, message, file_url, is_admin, sender_id, chat_type, delivered_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
    """, (session_id, sender_name, sender_role, receiver_id, receiver_role, restaurant_id, topic, message, file_url, is_admin, sender_id, chat_type))
    message_id = cursor.lastrowid
    conn.commit()
    _mark_messages_read(conn, session_id, sender_role, sender_id)
    conn.close()
    _track_presence(sender_role, sender_id if sender_role != 'Guest' else None)
    if session_id:
        _set_typing(session_id, sender_role, sender_id, sender_name, False)
    
    return jsonify({
        "success": True,
        "message": {
            "message_id": message_id,
            "sender_name": sender_name,
            "sender_role": sender_role,
            "sender_id": sender_id,
            "message": message,
            "file_url": file_url,
            "is_admin": is_admin,
            "topic": topic,
            "created_at": datetime.datetime.now().strftime('%H:%M')
        }
    })


@support_bp.route('/chat/ping', methods=['POST'])
def ping_chat_presence():
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form

    role, identifier, sender_name, _ = _resolve_sender_identity()
    session_id = (data.get('session_id') or session.get('session_id') or '').strip()
    if session_id:
        _track_presence(role, identifier)
    return jsonify({'success': True})


@support_bp.route('/chat/typing', methods=['POST'])
def set_chat_typing():
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form

    role, identifier, sender_name, _ = _resolve_sender_identity()
    session_id = (data.get('session_id') or session.get('session_id') or '').strip()
    if not session_id:
        return jsonify({'error': 'Session is required.'}), 400

    if not _can_access_session(session_id):
        return jsonify({'error': 'Unauthorized channel access'}), 403

    is_typing = _parse_bool(data.get('is_typing'))
    _track_presence(role, identifier)
    _set_typing(session_id, role, identifier, sender_name, is_typing)
    return jsonify({'success': True})

@support_bp.route('/chat/messages', methods=['GET'])
def get_messages():
    session_id = request.args.get('session_id')
    receiver_id = request.args.get('receiver_id')
    chat_type = request.args.get('chat_type', 'Support')
    viewer_role, viewer_id, viewer_name, _ = _resolve_sender_identity()
    
    # Calculate session_id if not provided
    if not session_id:
        if chat_type == 'Direct' and receiver_id:
            if 'user_id' in session:
                session_id = f"direct_u{session['user_id']}_o{receiver_id}"
            elif 'owner_id' in session:
                session_id = f"direct_u{receiver_id}_o{session['owner_id']}"
        elif chat_type == 'Support':
            if 'user_id' in session:
                session_id = f"support_u{session['user_id']}"
            elif 'owner_id' in session:
                session_id = f"support_o{session['owner_id']}"
            elif 'partner_id' in session:
                session_id = f"support_p{session['partner_id']}"
            else:
                session_id = session.get('session_id')
            
    if not _can_access_session(session_id):
        return jsonify({"error": "Unauthorized channel access"}), 403
    if session_id:
        _track_presence(viewer_role, viewer_id)

    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Database error"}), 500
            
        # Look for both new and legacy session IDs to preserve history
        legacy_id = None
        if not session_id:
            return jsonify([])
        if session_id.startswith('support_u'):
            legacy_id = session_id.replace('support_u', 'user_')
        elif session_id.startswith('support_o'):
            legacy_id = session_id.replace('support_o', 'owner_')
        elif session_id.startswith('support_p'):
            legacy_id = session_id.replace('support_p', 'partner_')
            
        cursor = conn.cursor(dictionary=True)
        if legacy_id:
            cursor.execute("""
                SELECT message_id, sender_name, sender_role, sender_id, message, file_url, is_admin, timestamp, topic, delivered_at, read_at
                FROM messages 
                WHERE session_id IN (%s, %s) 
                ORDER BY timestamp ASC, message_id ASC
            """, (session_id, legacy_id))
        else:
            cursor.execute("""
                SELECT message_id, sender_name, sender_role, sender_id, message, file_url, is_admin, timestamp, topic, delivered_at, read_at
                FROM messages 
                WHERE session_id = %s 
                ORDER BY timestamp ASC, message_id ASC
            """, (session_id,))
            
        messages = cursor.fetchall()
        _mark_messages_read(conn, session_id, viewer_role, viewer_id)
        conn.close()
        
        # Format timestamps safely
        for msg in messages:
            try:
                ts = msg.get('timestamp')
                if ts:
                    if hasattr(ts, 'strftime'):
                        msg['created_at'] = ts.strftime('%H:%M')
                    else:
                        ts_str = str(ts)
                        msg['created_at'] = ts_str[:16]
                else:
                    msg['created_at'] = ''
            except Exception:
                msg['created_at'] = ''
            for field in ('delivered_at', 'read_at'):
                try:
                    value = msg.get(field)
                    if value and hasattr(value, 'strftime'):
                        msg[f'{field}_label'] = value.strftime('%H:%M')
                    elif value:
                        msg[f'{field}_label'] = str(value)[:16]
                    else:
                        msg[f'{field}_label'] = ''
                except Exception:
                    msg[f'{field}_label'] = ''
            
        return jsonify(messages)
    except Exception as e:
        print(f"Error in get_messages: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@support_bp.route('/chat/live', methods=['GET'])
def chat_live_stream():
    session_id = request.args.get('session_id')
    receiver_id = request.args.get('receiver_id')
    chat_type = request.args.get('chat_type', 'Support')
    viewer_role, viewer_id, viewer_name, _ = _resolve_sender_identity()

    if not session_id:
        if chat_type == 'Direct' and receiver_id:
            if viewer_role == 'User' and session.get('user_id'):
                session_id = _get_direct_session_id(session['user_id'], receiver_id)
            elif viewer_role == 'Owner' and session.get('owner_id'):
                session_id = _get_direct_session_id(receiver_id, session['owner_id'])
        elif chat_type == 'Support':
            if viewer_role == 'User' and session.get('user_id'):
                session_id = f"support_u{session['user_id']}"
            elif viewer_role == 'Owner' and session.get('owner_id'):
                session_id = f"support_o{session['owner_id']}"
            elif viewer_role == 'Partner' and session.get('partner_id'):
                session_id = f"support_p{session['partner_id']}"
            else:
                session_id = session.get('session_id')

    if not _can_access_session(session_id):
        return jsonify({"error": "Unauthorized channel access"}), 403

    @stream_with_context
    def generate():
        last_signature = None
        last_typing = None
        _track_presence(viewer_role, viewer_id)
        try:
            while True:
                response = get_messages()
                if getattr(response, 'status_code', 200) != 200:
                    data = response.get_json(silent=True) or {'error': 'Unable to load messages.'}
                    yield f"event: error\ndata: {json.dumps(data, default=str, separators=(',', ':'))}\n\n"
                    break
                messages = response.get_json(silent=True) or []
                signature = '|'.join(
                    f"{msg.get('message_id','')}:{msg.get('created_at','')}:{msg.get('message','')}:{msg.get('file_url','')}"
                    for msg in messages
                )
                realtime = _build_chat_realtime_payload(session_id, viewer_role, viewer_id)
                typing_signature = json.dumps(realtime, default=str, sort_keys=True, separators=(',', ':'))
                if signature != last_signature or typing_signature != last_typing:
                    yield f"event: snapshot\ndata: {json.dumps({'messages': messages, 'realtime': realtime}, default=str, separators=(',', ':'))}\n\n"
                    last_signature = signature
                    last_typing = typing_signature
                else:
                    yield "event: heartbeat\ndata: {}\n\n"
                time.sleep(3)
        finally:
            _set_typing(session_id, viewer_role, viewer_id, viewer_name, False)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache, no-transform',
            'X-Accel-Buffering': 'no',
        },
    )

@support_bp.route('/admin/support/chats/<role_type>', methods=['GET'])
def get_admin_chats(role_type):
    if not session.get('is_admin_authenticated'):
        return jsonify({"error": "Unauthorized"}), 401
    if role_type not in ['User', 'Owner', 'Partner']:
        return jsonify({"error": "Invalid role type"}), 400
        
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
        
    try:
        cursor = conn.cursor(dictionary=True)
        # 1. Identify valid session IDs for this role based on prefix
        # We also need the LATEST message from each session, but we want the CLIENT's info (not Admin's)
        role_prefixes = {
            'User': ('support_u', 'user_'),
            'Owner': ('support_o', 'owner_'),
            'Partner': ('support_p', 'partner_'),
        }
        prefix, legacy_prefix = role_prefixes[role_type]
        
        cursor.execute("""
            SELECT m1.session_id, 
                   CASE
                       WHEN %s = 'Owner' THEN COALESCE(r.owner_name, (SELECT sender_name FROM messages WHERE session_id = m1.session_id AND sender_role != 'Admin' ORDER BY timestamp ASC, message_id ASC LIMIT 1))
                       WHEN %s = 'Partner' THEN COALESCE(dp.name, (SELECT sender_name FROM messages WHERE session_id = m1.session_id AND sender_role != 'Admin' ORDER BY timestamp ASC, message_id ASC LIMIT 1))
                       ELSE (SELECT sender_name FROM messages WHERE session_id = m1.session_id AND sender_role != 'Admin' ORDER BY timestamp ASC, message_id ASC LIMIT 1)
                   END as sender_name,
                   (SELECT sender_role FROM messages WHERE session_id = m1.session_id AND sender_role != 'Admin' ORDER BY timestamp ASC, message_id ASC LIMIT 1) as sender_role,
                   m1.topic, m1.message, m1.timestamp, m1.chat_type, m1.status,
                   m1.is_admin as latest_is_admin,
                   m1.delivered_at as latest_delivered_at,
                   m1.read_at as latest_read_at,
                   r.name as restaurant_target
            FROM messages m1
            LEFT JOIN Restaurant r ON r.id = m1.sender_id AND m1.sender_role = 'Owner'
            LEFT JOIN DeliveryPartner dp ON dp.id = m1.sender_id AND m1.sender_role = 'Partner'
            JOIN (
                SELECT session_id, MAX(message_id) as max_id
                FROM messages
                WHERE chat_type = 'Support'
                GROUP BY session_id
            ) m2 ON m1.session_id = m2.session_id AND m1.message_id = m2.max_id
            WHERE (m1.session_id LIKE %s OR m1.session_id LIKE %s)
            AND m1.chat_type = 'Support'
            ORDER BY m1.timestamp DESC
        """, (role_type, role_type, f"{prefix}%", f"{legacy_prefix}%"))
        chats = cursor.fetchall()
        
        for chat in chats:
            chat['created_at'] = chat['timestamp'].strftime('%Y-%m-%d %H:%M') if chat['timestamp'] else ''
            for field in ('latest_delivered_at', 'latest_read_at'):
                try:
                    value = chat.get(field)
                    if value and hasattr(value, 'strftime'):
                        chat[field] = value.strftime('%H:%M')
                    elif value:
                        chat[field] = str(value)[:16]
                    else:
                        chat[field] = ''
                except Exception:
                    chat[field] = ''
            chat['is_unread'] = (chat['status'] != 'Solved') and (not bool(chat.get('latest_is_admin')))
            # Fallback if no client message found (shouldn't happen)
            if not chat['sender_name']: chat['sender_name'] = 'Unknown Client'
            if not chat['sender_role']: chat['sender_role'] = role_type
            
        return jsonify(chats)
    finally:
        conn.close()

@support_bp.route('/chat/delete/<session_id>', methods=['POST'])
def delete_chat(session_id):
    if not _can_access_session(session_id):
        return jsonify({"error": "Unauthorized"}), 403
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    cursor = conn.cursor()
    cursor.execute("DELETE FROM messages WHERE session_id = %s", (session_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@support_bp.route('/chat/status/<session_id>', methods=['POST'])
def toggle_chat_status(session_id):
    if not _can_access_session(session_id):
        return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    new_status = data.get('status')
    if new_status not in ['Open', 'Solved']:
        return jsonify({"error": "Invalid status"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    cursor = conn.cursor()
    cursor.execute("UPDATE messages SET status = %s WHERE session_id = %s", (new_status, session_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@support_bp.route('/owner/chats', methods=['GET'])
def get_owner_chats():
    if 'owner_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    owner_id = session['owner_id']
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
        
    try:
        cursor = conn.cursor(dictionary=True)
        # Fetch unique users who have chatted with this owner
        # We look for session_ids format: direct_u{user_id}_o{owner_id}
        cursor.execute("""
            SELECT m1.session_id, m1.sender_name, m1.sender_role, m1.message, 
                   m1.timestamp, m1.sender_id, m1.receiver_id, m1.status,
                   m1.delivered_at as latest_delivered_at,
                   m1.read_at as latest_read_at,
                   m1.restaurant_id,
                   m1.chat_type,
                   m1.is_admin
            FROM messages m1
            JOIN (
                SELECT session_id, MAX(message_id) as max_id
                FROM messages
                WHERE session_id LIKE %s
                GROUP BY session_id
            ) m2 ON m1.session_id = m2.session_id AND m1.message_id = m2.max_id
            WHERE m1.session_id LIKE %s
            ORDER BY m1.timestamp DESC
        """, (f"direct_u%_o{owner_id}", f"direct_u%_o{owner_id}"))
        chats = cursor.fetchall()
        
        for chat in chats:
            chat['created_at'] = chat['timestamp'].strftime('%Y-%m-%d %H:%M')
            for field in ('latest_delivered_at', 'latest_read_at'):
                try:
                    value = chat.get(field)
                    if value and hasattr(value, 'strftime'):
                        chat[field] = value.strftime('%H:%M')
                    elif value:
                        chat[field] = str(value)[:16]
                    else:
                        chat[field] = ''
                except Exception:
                    chat[field] = ''
            # Extract user identifier from session_id (could be numeric ID or "guest_...")
            import re
            match = re.search(r'direct_u(.+)_o', chat['session_id'])
            if match:
                u_id = match.group(1)
                chat['user_id'] = u_id
                # Only attempt to fetch name if it's a numeric ID
                if u_id.isdigit():
                    cursor.execute("SELECT name FROM User WHERE id = %s", (u_id,))
                    usr = cursor.fetchone()
                    if usr:
                        chat['sender_name'] = usr['name']
                        chat['user_name'] = usr['name']
                    else:
                        chat['user_name'] = f"User {u_id}"
                else:
                    chat['user_name'] = f"User {u_id}"
            chat['latest_preview'] = chat.get('message') or 'No message yet'
            chat['is_unread'] = (chat['status'] != 'Solved') and (chat.get('sender_role') != 'Owner')
            
        return jsonify(chats)
    finally:
        conn.close()
