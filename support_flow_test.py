import json
import uuid

from app import app
from db import get_db_connection


def create_test_records():
    suffix = uuid.uuid4().hex[:6]
    owner_username = f"owner_{suffix}"
    owner_name = f"Test Restaurant {suffix}"
    user_name = f"Test User {suffix}"

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        """
        INSERT INTO Restaurant (
            name, owner_name, email, gst, fssai, location, contact,
            items_served, verified, photo_url, username, password
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s, %s)
        """,
        (
            owner_name,
            f"Owner {suffix}",
            f"{owner_username}@example.com",
            f"GST{suffix}",
            f"FSSAI{suffix}",
            "Test City",
            f"90000{suffix[:5]}",
            "Veg,Non-Veg",
            "/static/uploads/test.png",
            owner_username,
            "pass123",
        ),
    )
    owner_id = cursor.lastrowid

    cursor.execute(
        "INSERT INTO User (name, email, contact, org_type, org_name, joined) VALUES (%s, %s, %s, %s, %s, %s)",
        (
            user_name,
            f"user_{suffix}@example.com",
            f"80000{suffix[:5]}",
            "NGO",
            "Test Org",
            "2026-03-16",
        ),
    )
    user_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return {
        "suffix": suffix,
        "owner_id": owner_id,
        "owner_name": owner_name,
        "user_id": user_id,
        "user_name": user_name,
    }


def cleanup_test_records(owner_id, user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM messages WHERE session_id IN (%s, %s, %s)", (
        f"support_u{user_id}",
        f"support_o{owner_id}",
        f"direct_u{user_id}_o{owner_id}",
    ))
    cursor.execute("DELETE FROM FoodRequest WHERE user_id = %s", (user_id,))
    cursor.execute("DELETE FROM Donation WHERE restaurant_id = %s", (owner_id,))
    cursor.execute("DELETE FROM Restaurant WHERE id = %s", (owner_id,))
    cursor.execute("DELETE FROM User WHERE id = %s", (user_id,))
    conn.commit()
    conn.close()


def assert_ok(checks, name, condition, details):
    checks.append({
        "check": name,
        "passed": bool(condition),
        "details": details,
    })


def main():
    data = create_test_records()
    checks = []

    try:
        user_client = app.test_client()
        with user_client.session_transaction() as sess:
            sess["user_id"] = data["user_id"]
            sess["user_name"] = data["user_name"]
            sess["_csrf_token"] = "test-csrf-token"

        admin_client = app.test_client()
        admin_client.get("/admin/")
        with admin_client.session_transaction() as sess:
            sess["_csrf_token"] = "test-csrf-token"

        owner_client = app.test_client()
        with owner_client.session_transaction() as sess:
            sess["owner_id"] = data["owner_id"]
            sess["owner_name"] = data["owner_name"]
            sess["_csrf_token"] = "test-csrf-token"

        resp = user_client.post("/chat/send", data={
            "message": "hi admin",
            "sender_name": data["user_name"],
            "sender_role": "User",
            "sender_id": str(data["user_id"]),
            "topic": "Finding Food",
            "is_admin": "false",
            "chat_type": "Support",
            "csrf_token": "test-csrf-token"
        })
        user_send = resp.get_json()
        assert_ok(checks, "User -> Admin send", resp.status_code == 200 and user_send.get("success"), user_send)

        resp = user_client.get("/chat/messages?chat_type=Support")
        support_messages = resp.get_json()
        assert_ok(
            checks,
            "User support history loads",
            resp.status_code == 200 and len(support_messages) == 1 and support_messages[0]["message"] == "hi admin",
            support_messages,
        )

        resp = admin_client.post("/chat/send", json={
            "message": "hello user, admin here",
            "session_id": f"support_u{data['user_id']}",
            "is_admin": True,
            "csrf_token": "test-csrf-token"
        })
        admin_reply = resp.get_json()
        assert_ok(checks, "Admin reply send", resp.status_code == 200 and admin_reply.get("success"), admin_reply)

        resp = user_client.get("/chat/messages?chat_type=Support")
        support_messages = resp.get_json()
        assert_ok(
            checks,
            "User sees admin reply in order",
            resp.status_code == 200 and [m["message"] for m in support_messages] == ["hi admin", "hello user, admin here"],
            support_messages,
        )

        resp = user_client.post("/chat/send", data={
            "message": "hello restaurant",
            "sender_name": data["user_name"],
            "sender_role": "User",
            "sender_id": str(data["user_id"]),
            "topic": "Restaurant Inquiry",
            "is_admin": "false",
            "chat_type": "Direct",
            "receiver_id": str(data["owner_id"]),
            "receiver_role": "Owner",
            "restaurant_id": str(data["owner_id"]),
            "csrf_token": "test-csrf-token"
        })
        direct_send = resp.get_json()
        assert_ok(checks, "User -> Owner direct send", resp.status_code == 200 and direct_send.get("success"), direct_send)

        resp = owner_client.get("/owner/chats")
        owner_chats = resp.get_json()
        assert_ok(
            checks,
            "Owner sees direct chat list",
            resp.status_code == 200 and any(chat["session_id"] == f"direct_u{data['user_id']}_o{data['owner_id']}" for chat in owner_chats),
            owner_chats,
        )

        resp = owner_client.post("/chat/send", data={
            "message": "owner reply here",
            "sender_name": data["owner_name"],
            "sender_role": "Owner",
            "sender_id": str(data["owner_id"]),
            "topic": "Restaurant Inquiry",
            "is_admin": "false",
            "chat_type": "Direct",
            "receiver_id": str(data["user_id"]),
            "receiver_role": "User",
            "csrf_token": "test-csrf-token"
        })
        owner_reply = resp.get_json()
        assert_ok(checks, "Owner reply send", resp.status_code == 200 and owner_reply.get("success"), owner_reply)

        resp = user_client.get(f"/chat/messages?chat_type=Direct&receiver_id={data['owner_id']}")
        direct_messages = resp.get_json()
        assert_ok(
            checks,
            "User sees owner reply in order",
            resp.status_code == 200 and [m["message"] for m in direct_messages] == ["hello restaurant", "owner reply here"],
            direct_messages,
        )

        resp = owner_client.post("/chat/send", data={
            "message": "hi support from owner",
            "sender_name": data["owner_name"],
            "sender_role": "Owner",
            "sender_id": str(data["owner_id"]),
            "topic": "Technical",
            "is_admin": "false",
            "chat_type": "Support",
            "csrf_token": "test-csrf-token"
        })
        owner_support = resp.get_json()
        assert_ok(checks, "Owner -> Admin support send", resp.status_code == 200 and owner_support.get("success"), owner_support)

        resp = admin_client.get("/admin/support/chats/Owner")
        admin_owner_chats = resp.get_json()
        assert_ok(
            checks,
            "Admin sees owner support chat",
            resp.status_code == 200 and any(chat["session_id"] == f"support_o{data['owner_id']}" for chat in admin_owner_chats),
            admin_owner_chats,
        )

        resp = admin_client.post(f"/chat/status/support_o{data['owner_id']}", json={"status": "Solved", "csrf_token": "test-csrf-token"})
        assert_ok(checks, "Admin can mark owner support solved", resp.status_code == 200 and resp.get_json().get("success"), resp.get_json())

        resp = admin_client.post(f"/chat/delete/support_o{data['owner_id']}", data={"csrf_token": "test-csrf-token"})
        assert_ok(checks, "Admin can delete owner support chat", resp.status_code == 200 and resp.get_json().get("success"), resp.get_json())

    finally:
        cleanup_test_records(data["owner_id"], data["user_id"])

    passed = sum(1 for check in checks if check["passed"])
    total = len(checks)

    print(f"Support flow test summary: {passed}/{total} passed")
    for check in checks:
        status = "PASS" if check["passed"] else "FAIL"
        print(f"[{status}] {check['check']}")
        if not check["passed"]:
            print(json.dumps(check["details"], indent=2, default=str))


if __name__ == "__main__":
    main()
