from werkzeug.security import generate_password_hash


DEFAULT_OWNER_USERNAME = 'Owner'
DEFAULT_OWNER_PASSWORD = 'Owner'
DEFAULT_USER_USERNAME = 'User'
DEFAULT_USER_PASSWORD = 'User'
DEFAULT_PARTNER_USERNAME = 'Deliverypartner'
DEFAULT_PARTNER_PASSWORD = 'Deliverypartner'


def _ignore(cursor, statements):
    for statement in statements:
        try:
            cursor.execute(statement)
        except Exception:
            pass


def bootstrap_schema(conn):
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
            password VARCHAR(255) NOT NULL,
            is_deleted BOOLEAN DEFAULT FALSE,
            archived_at TIMESTAMP NULL DEFAULT NULL
        )
    """)
    _ignore(cursor, [
        "ALTER TABLE Restaurant ADD COLUMN email VARCHAR(100) AFTER owner_name",
        "ALTER TABLE Restaurant ADD COLUMN restaurant_type VARCHAR(50) AFTER email",
        "ALTER TABLE Restaurant ADD COLUMN verification_rejection_reason VARCHAR(255) DEFAULT NULL AFTER verified",
        "ALTER TABLE Restaurant ADD COLUMN map_url TEXT AFTER location",
        "ALTER TABLE Restaurant ADD COLUMN latitude DECIMAL(10,7) AFTER map_url",
        "ALTER TABLE Restaurant ADD COLUMN longitude DECIMAL(10,7) AFTER latitude",
        "ALTER TABLE Restaurant MODIFY COLUMN map_url TEXT",
        "ALTER TABLE Restaurant ADD COLUMN alternate_contact VARCHAR(20) AFTER contact",
        "ALTER TABLE Restaurant ADD COLUMN id_doc_url VARCHAR(255) AFTER photo_url",
        "ALTER TABLE Restaurant MODIFY password VARCHAR(255) NOT NULL",
        "ALTER TABLE Restaurant ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE AFTER password",
        "ALTER TABLE Restaurant ADD COLUMN archived_at TIMESTAMP NULL DEFAULT NULL AFTER is_deleted",
    ])
    cursor.execute("SELECT id FROM Restaurant WHERE TRIM(username) = %s LIMIT 1", (DEFAULT_OWNER_USERNAME,))
    default_owner = cursor.fetchone()
    if default_owner:
        owner_id = default_owner[0] if not isinstance(default_owner, dict) else default_owner.get('id')
        cursor.execute("""
            UPDATE Restaurant
            SET password = %s,
                is_deleted = FALSE,
                verified = TRUE,
                owner_name = COALESCE(NULLIF(owner_name, ''), %s),
                name = COALESCE(NULLIF(name, ''), %s)
            WHERE id = %s
        """, (
            generate_password_hash(DEFAULT_OWNER_PASSWORD),
            'Owner',
            'Demo Restaurant',
            owner_id,
        ))
    else:
        cursor.execute("""
            INSERT INTO Restaurant (
                name, owner_name, email, restaurant_type, gst, fssai, location,
                contact, alternate_contact, items_served, verified, photo_url,
                username, password
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            'Demo Restaurant',
            'Owner',
            'owner@example.com',
            'Restaurant',
            'DEMO-GST',
            'DEMO-FSSAI',
            'Hyderabad',
            '+919999999999',
            None,
            'Veg',
            True,
            'https://images.unsplash.com/photo-1414235077428-338989a2e8c0?w=500&auto=format&fit=crop&q=60',
            DEFAULT_OWNER_USERNAME,
            generate_password_hash(DEFAULT_OWNER_PASSWORD),
        ))

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Donation (
            id INT AUTO_INCREMENT PRIMARY KEY,
            restaurant_id INT NOT NULL,
            item_type VARCHAR(20) NOT NULL,
            item_name VARCHAR(100) NOT NULL,
            quantity VARCHAR(50) NOT NULL,
            prep_time VARCHAR(50) NOT NULL,
            date VARCHAR(20) NOT NULL,
            image_url VARCHAR(255) DEFAULT NULL,
            status VARCHAR(20) DEFAULT 'Available',
            quality_status VARCHAR(20) DEFAULT 'Pending',
            quality_rejection_reason VARCHAR(255) DEFAULT NULL,
            quality_checked_at TIMESTAMP NULL DEFAULT NULL,
            quality_checked_by VARCHAR(50) DEFAULT NULL,
            packed_time TIMESTAMP NULL DEFAULT NULL,
            best_before_time TIMESTAMP NULL DEFAULT NULL,
            storage_note VARCHAR(255) DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (restaurant_id) REFERENCES Restaurant(id)
        )
    """)
    _ignore(cursor, [
        "ALTER TABLE Donation ADD COLUMN image_url VARCHAR(255) DEFAULT NULL AFTER date",
        "ALTER TABLE Donation ADD COLUMN quality_status VARCHAR(20) DEFAULT 'Pending' AFTER status",
        "ALTER TABLE Donation ADD COLUMN quality_rejection_reason VARCHAR(255) DEFAULT NULL AFTER quality_status",
        "ALTER TABLE Donation ADD COLUMN quality_checked_at TIMESTAMP NULL DEFAULT NULL AFTER quality_rejection_reason",
        "ALTER TABLE Donation ADD COLUMN quality_checked_by VARCHAR(50) DEFAULT NULL AFTER quality_checked_at",
        "ALTER TABLE Donation ADD COLUMN packed_time TIMESTAMP NULL DEFAULT NULL AFTER quality_checked_by",
        "ALTER TABLE Donation ADD COLUMN best_before_time TIMESTAMP NULL DEFAULT NULL AFTER packed_time",
        "ALTER TABLE Donation ADD COLUMN storage_note VARCHAR(255) DEFAULT NULL AFTER best_before_time",
        "ALTER TABLE Donation ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    ])

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
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            delivered_at TIMESTAMP NULL DEFAULT NULL,
            read_at TIMESTAMP NULL DEFAULT NULL,
            INDEX (session_id)
        )
    """)
    _ignore(cursor, [
        "ALTER TABLE messages ADD COLUMN delivered_at TIMESTAMP NULL DEFAULT NULL AFTER timestamp",
        "ALTER TABLE messages ADD COLUMN read_at TIMESTAMP NULL DEFAULT NULL AFTER delivered_at",
    ])

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
            referral_bonus_granted BOOLEAN DEFAULT FALSE,
            is_deleted BOOLEAN DEFAULT FALSE,
            archived_at TIMESTAMP NULL DEFAULT NULL
        )
    """)
    _ignore(cursor, [
        "ALTER TABLE User ADD COLUMN email VARCHAR(100) AFTER name",
        "ALTER TABLE User ADD COLUMN org_name VARCHAR(120) NOT NULL AFTER name",
        "ALTER TABLE User ADD COLUMN contact VARCHAR(20) NOT NULL AFTER email",
        "ALTER TABLE User ADD COLUMN password VARCHAR(255) AFTER contact",
        "ALTER TABLE User ADD COLUMN alternate_contact VARCHAR(20) AFTER contact",
        "ALTER TABLE User ADD COLUMN food_preferences TEXT AFTER org_type",
        "ALTER TABLE User ADD COLUMN org_location TEXT AFTER food_preferences",
        "ALTER TABLE User ADD COLUMN org_image_url VARCHAR(255) AFTER org_location",
        "ALTER TABLE User ADD COLUMN id_doc_url VARCHAR(255) AFTER food_preferences",
        "ALTER TABLE User ADD COLUMN user_verified BOOLEAN DEFAULT FALSE AFTER id_doc_url",
        "ALTER TABLE User ADD COLUMN user_verification_rejection_reason VARCHAR(255) DEFAULT NULL AFTER user_verified",
        "ALTER TABLE User ADD COLUMN terms_accepted BOOLEAN DEFAULT FALSE AFTER id_doc_url",
        "ALTER TABLE User ADD COLUMN reward_coins INT DEFAULT 0 AFTER joined",
        "ALTER TABLE User ADD COLUMN referral_code VARCHAR(32) DEFAULT NULL AFTER reward_coins",
        "ALTER TABLE User ADD COLUMN referred_by_code VARCHAR(32) DEFAULT NULL AFTER referral_code",
        "ALTER TABLE User ADD COLUMN referral_bonus_granted BOOLEAN DEFAULT FALSE AFTER referred_by_code",
        "ALTER TABLE User ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE AFTER referral_bonus_granted",
        "ALTER TABLE User ADD COLUMN archived_at TIMESTAMP NULL DEFAULT NULL AFTER is_deleted",
        "ALTER TABLE User ADD UNIQUE KEY unique_user_referral_code (referral_code)",
    ])
    cursor.execute("SELECT id FROM User WHERE TRIM(name) = %s AND COALESCE(is_deleted, FALSE) = FALSE LIMIT 1", (DEFAULT_USER_USERNAME,))
    if not cursor.fetchone():
        cursor.execute("""
            INSERT INTO User (
                name, org_name, email, contact, password, alternate_contact,
                org_type, food_preferences, terms_accepted, joined, reward_coins,
                referral_code, referral_bonus_granted, user_verified
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CURDATE(), %s, %s, %s, %s)
        """, (
            DEFAULT_USER_USERNAME,
            'Demo User',
            'user@example.com',
            '8888888888',
            generate_password_hash(DEFAULT_USER_PASSWORD),
            None,
            'Individual',
            'Veg',
            True,
            0,
            'USERDEMO',
            False,
            True,
        ))

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
            otp_attempt_count INT DEFAULT 0,
            otp_locked_at TIMESTAMP NULL DEFAULT NULL,
            taste_rating INT DEFAULT NULL,
            taste_feedback VARCHAR(255) DEFAULT NULL,
            rated_at TIMESTAMP NULL DEFAULT NULL,
            delivery_partner_rating INT DEFAULT NULL,
            delivery_partner_feedback VARCHAR(255) DEFAULT NULL,
            delivery_partner_rated_at TIMESTAMP NULL DEFAULT NULL,
            delivery_issue_type VARCHAR(40) DEFAULT NULL,
            delivery_issue_role VARCHAR(20) DEFAULT NULL,
            delivery_issue_detail VARCHAR(255) DEFAULT NULL,
            delivery_issue_reported_at TIMESTAMP NULL DEFAULT NULL,
            FOREIGN KEY (donation_id) REFERENCES Donation(id),
            FOREIGN KEY (user_id) REFERENCES User(id)
        )
    """)
    _ignore(cursor, [
        "ALTER TABLE FoodRequest ADD COLUMN rejection_reason VARCHAR(255) DEFAULT NULL",
        "ALTER TABLE FoodRequest ADD COLUMN request_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE FoodRequest ADD COLUMN accepted_at TIMESTAMP NULL DEFAULT NULL AFTER request_time",
        "ALTER TABLE FoodRequest ADD COLUMN food_ready_at TIMESTAMP NULL DEFAULT NULL AFTER accepted_at",
        "ALTER TABLE FoodRequest ADD COLUMN pickup_reached_at TIMESTAMP NULL DEFAULT NULL AFTER food_ready_at",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_partner_id INT DEFAULT NULL AFTER user_id",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_mode VARCHAR(20) DEFAULT 'Pickup' AFTER status",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_charge_mode VARCHAR(30) DEFAULT 'CashOnDelivery' AFTER delivery_mode",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_address VARCHAR(255) DEFAULT NULL AFTER delivery_mode",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_order_id VARCHAR(50) DEFAULT NULL AFTER delivery_address",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_fee_paise INT DEFAULT NULL AFTER delivery_order_id",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_coin_used INT DEFAULT 0 AFTER delivery_fee_paise",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_coin_discount_paise INT DEFAULT 0 AFTER delivery_coin_used",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_latitude DECIMAL(10,7) DEFAULT NULL AFTER delivery_address",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_longitude DECIMAL(10,7) DEFAULT NULL AFTER delivery_latitude",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_location_accuracy_m DECIMAL(10,2) DEFAULT NULL AFTER delivery_longitude",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_location_updated_at TIMESTAMP NULL DEFAULT NULL AFTER delivery_location_accuracy_m",
        "ALTER TABLE FoodRequest ADD COLUMN out_for_delivery_at TIMESTAMP NULL DEFAULT NULL AFTER accepted_at",
        "ALTER TABLE FoodRequest ADD COLUMN delivered_at TIMESTAMP NULL DEFAULT NULL AFTER out_for_delivery_at",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_otp VARCHAR(10) DEFAULT NULL AFTER delivered_at",
        "ALTER TABLE FoodRequest ADD COLUMN otp_generated_at TIMESTAMP NULL DEFAULT NULL AFTER delivery_otp",
        "ALTER TABLE FoodRequest ADD COLUMN otp_verified_at TIMESTAMP NULL DEFAULT NULL AFTER otp_generated_at",
        "ALTER TABLE FoodRequest ADD COLUMN otp_attempt_count INT DEFAULT 0 AFTER otp_verified_at",
        "ALTER TABLE FoodRequest ADD COLUMN otp_locked_at TIMESTAMP NULL DEFAULT NULL AFTER otp_attempt_count",
        "ALTER TABLE FoodRequest ADD COLUMN taste_rating INT DEFAULT NULL AFTER accepted_at",
        "ALTER TABLE FoodRequest ADD COLUMN taste_feedback VARCHAR(255) DEFAULT NULL AFTER taste_rating",
        "ALTER TABLE FoodRequest ADD COLUMN rated_at TIMESTAMP NULL DEFAULT NULL AFTER taste_feedback",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_partner_rating INT DEFAULT NULL AFTER delivery_otp",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_partner_feedback VARCHAR(255) DEFAULT NULL AFTER delivery_partner_rating",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_partner_rated_at TIMESTAMP NULL DEFAULT NULL AFTER delivery_partner_feedback",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_issue_type VARCHAR(40) DEFAULT NULL AFTER delivery_partner_rated_at",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_issue_role VARCHAR(20) DEFAULT NULL AFTER delivery_issue_type",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_issue_detail VARCHAR(255) DEFAULT NULL AFTER delivery_issue_role",
        "ALTER TABLE FoodRequest ADD COLUMN delivery_issue_reported_at TIMESTAMP NULL DEFAULT NULL AFTER delivery_issue_detail",
    ])

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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS DeliveryFeePayment (
            id INT AUTO_INCREMENT PRIMARY KEY,
            request_id INT NOT NULL,
            user_id INT NOT NULL,
            partner_id INT NOT NULL,
            amount_paise INT NOT NULL,
            currency VARCHAR(10) DEFAULT 'INR',
            receipt VARCHAR(100) DEFAULT NULL,
            razorpay_order_id VARCHAR(100) UNIQUE,
            razorpay_payment_id VARCHAR(100) DEFAULT NULL,
            razorpay_signature VARCHAR(255) DEFAULT NULL,
            status VARCHAR(20) DEFAULT 'Created',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            paid_at TIMESTAMP NULL DEFAULT NULL,
            UNIQUE KEY unique_delivery_fee_request (request_id),
            FOREIGN KEY (request_id) REFERENCES FoodRequest(id),
            FOREIGN KEY (user_id) REFERENCES User(id),
            FOREIGN KEY (partner_id) REFERENCES DeliveryPartner(id)
        )
    """)

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
            delivery_status VARCHAR(20) DEFAULT 'Offline',
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
            is_deleted BOOLEAN DEFAULT FALSE,
            archived_at TIMESTAMP NULL DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    _ignore(cursor, [
        "ALTER TABLE DeliveryPartner ADD COLUMN is_available BOOLEAN DEFAULT TRUE AFTER password",
        "ALTER TABLE DeliveryPartner MODIFY password VARCHAR(255) NOT NULL",
        "ALTER TABLE DeliveryPartner ADD COLUMN delivery_status VARCHAR(20) DEFAULT 'Offline' AFTER is_available",
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
        "ALTER TABLE DeliveryPartner ADD COLUMN current_latitude DECIMAL(10,7) DEFAULT NULL AFTER is_active",
        "ALTER TABLE DeliveryPartner ADD COLUMN current_longitude DECIMAL(10,7) DEFAULT NULL AFTER current_latitude",
        "ALTER TABLE DeliveryPartner ADD COLUMN current_accuracy_m DECIMAL(10,2) DEFAULT NULL AFTER current_longitude",
        "ALTER TABLE DeliveryPartner ADD COLUMN current_location_updated_at TIMESTAMP NULL DEFAULT NULL AFTER current_accuracy_m",
        "ALTER TABLE DeliveryPartner ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE AFTER current_location_updated_at",
        "ALTER TABLE DeliveryPartner ADD COLUMN archived_at TIMESTAMP NULL DEFAULT NULL AFTER is_deleted",
    ])
    cursor.execute("SELECT id FROM DeliveryPartner WHERE TRIM(username) = %s AND COALESCE(is_deleted, FALSE) = FALSE LIMIT 1", (DEFAULT_PARTNER_USERNAME,))
    if not cursor.fetchone():
        cursor.execute("""
            INSERT INTO DeliveryPartner (
                name, phone, email, zone, vehicle_type, username, password,
                is_available, delivery_status, is_active, application_status,
                verification_remarks, identity_document_type, identity_document_url,
                profile_photo_url, pan_card_url, driving_license_url, vehicle_rc_url,
                bank_document_url, payment_verified
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            'Demo Delivery Partner',
            '7777777777',
            'deliverypartner@example.com',
            'Hyderabad',
            'Bike',
            DEFAULT_PARTNER_USERNAME,
            generate_password_hash(DEFAULT_PARTNER_PASSWORD),
            True,
            'Available',
            True,
            'Approved',
            None,
            'Aadhaar',
            '/static/uploads/partner_docs/demo-identity.pdf',
            '/static/uploads/partner_docs/demo-profile.jpg',
            '/static/uploads/partner_docs/demo-pan.pdf',
            '/static/uploads/partner_docs/demo-license.pdf',
            '/static/uploads/partner_docs/demo-vehicle-rc.pdf',
            '/static/uploads/partner_docs/demo-bank.pdf',
            True,
        ))

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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS DeliveryPartnerAuditLog (
            id INT AUTO_INCREMENT PRIMARY KEY,
            partner_id INT NOT NULL,
            action VARCHAR(100) NOT NULL,
            actor_role VARCHAR(30) NOT NULL DEFAULT 'System',
            actor_name VARCHAR(100) DEFAULT NULL,
            details VARCHAR(255) DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (partner_id) REFERENCES DeliveryPartner(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS DeliveryPartnerPaymentLog (
            id INT AUTO_INCREMENT PRIMARY KEY,
            partner_id INT NOT NULL,
            payment_mode VARCHAR(30) NOT NULL,
            amount_paise INT NOT NULL DEFAULT 0,
            payment_reference VARCHAR(120) DEFAULT NULL,
            payout_id VARCHAR(100) DEFAULT NULL,
            fund_account_id VARCHAR(100) DEFAULT NULL,
            validation_id VARCHAR(100) DEFAULT NULL,
            upi_id VARCHAR(100) DEFAULT NULL,
            beneficiary_name VARCHAR(120) DEFAULT NULL,
            status VARCHAR(30) DEFAULT 'Created',
            idempotency_key VARCHAR(80) DEFAULT NULL,
            remarks VARCHAR(255) DEFAULT NULL,
            processed_by VARCHAR(50) DEFAULT NULL,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (partner_id) REFERENCES DeliveryPartner(id)
        )
    """)
    _ignore(cursor, [
        "ALTER TABLE DeliveryPartnerPaymentLog ADD COLUMN payout_id VARCHAR(100) DEFAULT NULL AFTER payment_reference",
        "ALTER TABLE DeliveryPartnerPaymentLog ADD COLUMN fund_account_id VARCHAR(100) DEFAULT NULL AFTER payout_id",
        "ALTER TABLE DeliveryPartnerPaymentLog ADD COLUMN validation_id VARCHAR(100) DEFAULT NULL AFTER fund_account_id",
        "ALTER TABLE DeliveryPartnerPaymentLog ADD COLUMN upi_id VARCHAR(100) DEFAULT NULL AFTER validation_id",
        "ALTER TABLE DeliveryPartnerPaymentLog ADD COLUMN beneficiary_name VARCHAR(120) DEFAULT NULL AFTER upi_id",
        "ALTER TABLE DeliveryPartnerPaymentLog ADD COLUMN status VARCHAR(30) DEFAULT 'Created' AFTER beneficiary_name",
        "ALTER TABLE DeliveryPartnerPaymentLog ADD COLUMN idempotency_key VARCHAR(80) DEFAULT NULL AFTER status",
    ])
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS FavoriteRestaurant (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            restaurant_id INT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY unique_user_restaurant (user_id, restaurant_id),
            FOREIGN KEY (user_id) REFERENCES User(id),
            FOREIGN KEY (restaurant_id) REFERENCES Restaurant(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS AdminUser (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(50) UNIQUE NOT NULL,
            password VARCHAR(255) NOT NULL,
            is_main_admin BOOLEAN DEFAULT FALSE,
            is_active BOOLEAN DEFAULT TRUE,
            last_login_at TIMESTAMP NULL DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS AdminAuditLog (
            id INT AUTO_INCREMENT PRIMARY KEY,
            admin_username VARCHAR(50) NOT NULL,
            action VARCHAR(100) NOT NULL,
            target_type VARCHAR(50) DEFAULT NULL,
            target_id VARCHAR(50) DEFAULT NULL,
            details VARCHAR(255) DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ArchivedDonation (
            id INT AUTO_INCREMENT PRIMARY KEY,
            original_donation_id INT,
            restaurant_id INT,
            item_type VARCHAR(20),
            item_name VARCHAR(100),
            quantity VARCHAR(50),
            prep_time VARCHAR(50),
            date VARCHAR(20),
            image_url VARCHAR(255),
            status VARCHAR(20),
            archived_by VARCHAR(50),
            archived_reason VARCHAR(100),
            archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ArchivedUser (
            id INT AUTO_INCREMENT PRIMARY KEY,
            original_user_id INT,
            name VARCHAR(100),
            org_name VARCHAR(120),
            email VARCHAR(100),
            contact VARCHAR(20),
            alternate_contact VARCHAR(20),
            org_type VARCHAR(50),
            food_preferences TEXT,
            org_location TEXT,
            org_image_url VARCHAR(255),
            id_doc_url VARCHAR(255),
            user_verified BOOLEAN DEFAULT FALSE,
            user_verification_rejection_reason VARCHAR(255) DEFAULT NULL,
            terms_accepted BOOLEAN DEFAULT FALSE,
            joined VARCHAR(50),
            archived_by VARCHAR(50),
            archived_reason VARCHAR(100),
            archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    try:
        cursor.execute("SELECT COUNT(*) FROM AdminUser")
        admin_count = cursor.fetchone()[0]
        if admin_count > 0:
            cursor.execute("SELECT COUNT(*) FROM AdminUser WHERE is_main_admin = TRUE")
            main_admin_count = cursor.fetchone()[0]
            if main_admin_count == 0:
                cursor.execute("""
                    UPDATE AdminUser
                    SET is_main_admin = TRUE
                    ORDER BY id ASC
                    LIMIT 1
                """)
    except Exception:
        pass

    _ignore(cursor, [
        "CREATE INDEX idx_donation_restaurant_status_created ON Donation (restaurant_id, status, created_at)",
        "CREATE INDEX idx_donation_status_created ON Donation (status, created_at)",
        "CREATE INDEX idx_donation_quality_status ON Donation (quality_status, status)",
        "CREATE INDEX idx_foodrequest_status_time ON FoodRequest (status, request_time)",
        "CREATE INDEX idx_foodrequest_user_status_time ON FoodRequest (user_id, status, request_time)",
        "CREATE INDEX idx_foodrequest_partner_status_time ON FoodRequest (delivery_partner_id, status, request_time)",
        "CREATE INDEX idx_foodrequest_donation_status_time ON FoodRequest (donation_id, status, request_time)",
        "CREATE INDEX idx_foodrequest_delivery_live ON FoodRequest (delivery_mode, status, delivery_partner_id)",
        "CREATE INDEX idx_foodrequest_pickup_reached ON FoodRequest (delivery_partner_id, status, pickup_reached_at)",
        "CREATE INDEX idx_messages_session_type_status ON messages (session_id, chat_type, status)",
        "CREATE INDEX idx_messages_role_sender_time ON messages (sender_role, sender_id, timestamp)",
        "CREATE INDEX idx_deliverypartner_review_status ON DeliveryPartner (application_status, is_active, is_available)",
        "CREATE INDEX idx_deliverypartner_live_location ON DeliveryPartner (is_available, current_location_updated_at)",
        "CREATE INDEX idx_restaurant_verified_deleted ON Restaurant (verified, is_deleted)",
        "CREATE INDEX idx_user_deleted_joined ON User (is_deleted, joined)",
    ])

    cursor.close()
