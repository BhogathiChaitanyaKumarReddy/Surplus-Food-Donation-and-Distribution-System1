import mysql.connector
import re
from werkzeug.security import generate_password_hash

DB_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': 'Chaitu9182',
    'database': 'food_system'
}

def migrate_database():
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor(dictionary=True)

    print("--- Starting Database Migration ---")

    # 1. Update Donation Table (Split quantity and unit)
    try:
        print("1. Adding 'unit' column to Donation table...")
        cursor.execute("ALTER TABLE Donation ADD COLUMN unit VARCHAR(20) DEFAULT 'servings'")
        conn.commit()
    except mysql.connector.Error as err:
        print(f"Skipping (likely already exists): {err}")

    # Migrate existing strings
    cursor.execute("SELECT id, quantity FROM Donation")
    donations = cursor.fetchall()
    
    for d in donations:
        qt_str = str(d['quantity'])
        match = re.search(r'(\d+)\s*(.*)', qt_str)
        if match:
            num = int(match.group(1))
            unit = match.group(2).strip()
            if not unit:
                unit = 'servings'
            
            # Since we can't easily alter a column type from VARCHAR to INT if it currently holds strings like '5 servings',
            # we will create a temporary column, cast the numbers, and then drop/rename.
            pass # we'll just handle it by creating a new column below.
    
    try:
        print("2. Changing 'quantity' to INT logic...")
        cursor.execute("ALTER TABLE Donation ADD COLUMN qty_int INT DEFAULT 0")
        
        for d in donations:
            qt_str = str(d['quantity'])
            match = re.search(r'(\d+)\s*(.*)', qt_str)
            if match:
                num = int(match.group(1))
                unit = match.group(2).strip() or 'servings'
            else:
                num = 0
                unit = 'servings'
                
            cursor.execute("UPDATE Donation SET qty_int = %s, unit = %s WHERE id = %s", (num, unit, d['id']))
        conn.commit()
        
        cursor.execute("ALTER TABLE Donation DROP COLUMN quantity")
        cursor.execute("ALTER TABLE Donation CHANGE COLUMN qty_int quantity INT NOT NULL")
        conn.commit()
    except mysql.connector.Error as err:
        print(f"Skipping INT migration (likely already done): {err}")

    # 2. Add user_id to FoodRequest
    try:
        print("3. Adding 'user_id' to FoodRequest...")
        cursor.execute("ALTER TABLE FoodRequest ADD COLUMN user_id INT")
        # For existing requests, we will just assign them to User ID 1 (Hope Shelter)
        cursor.execute("UPDATE FoodRequest SET user_id = 1 WHERE user_id IS NULL")
        cursor.execute("ALTER TABLE FoodRequest MODIFY user_id INT NOT NULL")
        # Add foreign key
        cursor.execute("ALTER TABLE FoodRequest ADD FOREIGN KEY (user_id) REFERENCES User(id)")
        conn.commit()
    except mysql.connector.Error as err:
        print(f"Skipping user_id migration: {err}")

    # 3b. Add delivery OTP columns to FoodRequest
    for statement in [
        "ALTER TABLE FoodRequest ADD COLUMN delivery_partner_id INT DEFAULT NULL",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_mode VARCHAR(20) DEFAULT 'Pickup'",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_charge_mode VARCHAR(30) DEFAULT 'CashOnDelivery'",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_address VARCHAR(255) DEFAULT NULL",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_latitude DECIMAL(10,7) DEFAULT NULL",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_longitude DECIMAL(10,7) DEFAULT NULL",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_location_accuracy_m DECIMAL(10,2) DEFAULT NULL",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_location_updated_at TIMESTAMP NULL DEFAULT NULL",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_order_id VARCHAR(50) DEFAULT NULL",
        "ALTER TABLE FoodRequest ADD COLUMN out_for_delivery_at TIMESTAMP NULL DEFAULT NULL",
        "ALTER TABLE FoodRequest ADD COLUMN delivered_at TIMESTAMP NULL DEFAULT NULL",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_otp VARCHAR(10) DEFAULT NULL",
        "ALTER TABLE FoodRequest ADD COLUMN otp_generated_at TIMESTAMP NULL DEFAULT NULL",
        "ALTER TABLE FoodRequest ADD COLUMN otp_verified_at TIMESTAMP NULL DEFAULT NULL",
    ]:
        try:
            cursor.execute(statement)
        except mysql.connector.Error as err:
            print(f"Skipping delivery migration step: {err}")
    conn.commit()

    # 3c. Add live partner location fields
    for statement in [
        "ALTER TABLE DeliveryPartner ADD COLUMN current_latitude DECIMAL(10,7) DEFAULT NULL",
        "ALTER TABLE DeliveryPartner ADD COLUMN current_longitude DECIMAL(10,7) DEFAULT NULL",
        "ALTER TABLE DeliveryPartner ADD COLUMN current_accuracy_m DECIMAL(10,2) DEFAULT NULL",
        "ALTER TABLE DeliveryPartner ADD COLUMN current_location_updated_at TIMESTAMP NULL DEFAULT NULL",
    ]:
        try:
            cursor.execute(statement)
        except mysql.connector.Error as err:
            print(f"Skipping partner location migration step: {err}")
    conn.commit()

    # 3d. Add message read receipts
    for statement in [
        "ALTER TABLE messages ADD COLUMN delivered_at TIMESTAMP NULL DEFAULT NULL",
        "ALTER TABLE messages ADD COLUMN read_at TIMESTAMP NULL DEFAULT NULL",
    ]:
        try:
            cursor.execute(statement)
        except mysql.connector.Error as err:
            print(f"Skipping messages migration step: {err}")
    conn.commit()

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
        conn.commit()
    except mysql.connector.Error as err:
        print(f"Skipping rejection table migration: {err}")

    # 3. Hash Passwords
    try:
        print("4. Hashing plaintext passwords (extending column length first)...")
        cursor.execute("ALTER TABLE Restaurant MODIFY password VARCHAR(255) NOT NULL")
        conn.commit()
    except Exception as e:
        print(f"Skipping column extension: {e}")
        
    cursor.execute("SELECT id, password FROM Restaurant")
    restaurants = cursor.fetchall()
    for r in restaurants:
        plaintext = r['password']
        # Check if already hashed (werkzeug hashes start with scrypt: usually in newer versions)
        if not plaintext.startswith('scrypt:'):
            hashed = generate_password_hash(plaintext)
            cursor.execute("UPDATE Restaurant SET password = %s WHERE id = %s", (hashed, r['id']))
    conn.commit()

    print("--- Migration Completed ---")
    conn.close()

if __name__ == '__main__':
    migrate_database()
