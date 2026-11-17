"""
Search utility module for role-based search functionality.
All queries use parameterized statements to prevent SQL injection.
"""
from database import get_db_connection


class SearchHelper:
    """Utility class for executing secure, role-based searches."""

    @staticmethod
    def donor_search(user_id, query, category=None, status=None, date_from=None, date_to=None):
        """
        Search donations in Donor Dashboard.
        Params: food_name, category, status, location, date range
        """
        conn = get_db_connection()
        cursor = conn.cursor()
        
        sql = """
             SELECT fd.id, fd.donor_id, fd.food_name AS title, fd.category, fd.quantity, fd.status,
                 fd.created_at, fd.address, 'food' AS donation_type
             FROM food_donations fd
             WHERE fd.donor_id = (SELECT id FROM donors WHERE user_id = ?)
             AND (LOWER(fd.food_name) LIKE LOWER(?) OR LOWER(fd.category) LIKE LOWER(?)
               OR LOWER(fd.address) LIKE LOWER(?) OR LOWER('food') LIKE LOWER(?))
             UNION ALL
             SELECT cd.id, cd.donor_id, cd.item_type AS title, cd.category, cd.quantity, cd.status,
                 cd.created_at, cd.address, 'clothes' AS donation_type
             FROM clothes_donations cd
             WHERE cd.donor_id = (SELECT id FROM donors WHERE user_id = ?)
             AND (LOWER(cd.item_type) LIKE LOWER(?) OR LOWER(cd.category) LIKE LOWER(?)
               OR LOWER(cd.address) LIKE LOWER(?) OR LOWER('clothes') LIKE LOWER(?))
             UNION ALL
             SELECT bd.id, bd.donor_id, bd.title, bd.category, bd.quantity, bd.status,
                 bd.created_at, bd.address, 'books' AS donation_type
             FROM book_donations bd
             WHERE bd.donor_id = (SELECT id FROM donors WHERE user_id = ?)
             AND (LOWER(bd.title) LIKE LOWER(?) OR LOWER(bd.category) LIKE LOWER(?)
               OR LOWER(bd.address) LIKE LOWER(?) OR LOWER('books') LIKE LOWER(?))
             UNION ALL
             SELECT md.id, md.donor_id, COALESCE(md.purpose, 'Money donation') AS title,
                 'money' AS category, CAST(md.amount AS TEXT) AS quantity, md.status,
                 md.created_at, COALESCE(o.address, '') AS address, 'money' AS donation_type
             FROM money_donations md
             LEFT JOIN orphanages o ON o.id = md.orphanage_id
             WHERE md.donor_id = (SELECT id FROM donors WHERE user_id = ?)
             AND (LOWER(COALESCE(md.purpose, 'money donation')) LIKE LOWER(?)
               OR LOWER('money') LIKE LOWER(?))
         """
        wildcard = f"%{query}%"
        params = [user_id, wildcard, wildcard, wildcard, wildcard,
                user_id, wildcard, wildcard, wildcard, wildcard,
                user_id, wildcard, wildcard, wildcard, wildcard,
                                    user_id, wildcard, wildcard]
        sql = f"SELECT * FROM ({sql}) AS donations WHERE 1 = 1"
        
        if category:
            sql += " AND category = ?"
            params.append(category)
        
        if status:
            sql += " AND status = ?"
            params.append(status)
        
        if date_from:
            sql += " AND DATE(created_at) >= DATE(?)"
            params.append(date_from)
        
        if date_to:
            sql += " AND DATE(created_at) <= DATE(?)"
            params.append(date_to)
        
        sql += " ORDER BY created_at DESC"
        
        cursor.execute(sql, params)
        results = cursor.fetchall()
        conn.close()
        return results

    @staticmethod
    def ngo_search(user_id, query, category=None, status=None, veg_non_veg=None):
        """
        Search available donations in NGO Dashboard.
        Params: food_name, donor_name, location, category, availability status
        """
        conn = get_db_connection()
        cursor = conn.cursor()
        
        sql = """
             SELECT fd.id, fd.food_name AS title, fd.category, fd.quantity, fd.status,
                 fd.address, fd.created_at, fd.veg_non_veg, d.company_name AS donor_name,
                 'food' AS donation_type
             FROM food_donations fd JOIN donors d ON fd.donor_id = d.id
             WHERE fd.status IN ('Pending', 'Accepted')
            AND (LOWER(fd.food_name) LIKE LOWER(?) OR LOWER(d.company_name) LIKE LOWER(?)
                 OR LOWER(fd.address) LIKE LOWER(?) OR LOWER(fd.category) LIKE LOWER(?)
                 OR LOWER('food') LIKE LOWER(?))
             UNION ALL
             SELECT cd.id, cd.item_type AS title, cd.category, cd.quantity, cd.status,
                 cd.address, cd.created_at, cd.condition AS veg_non_veg, d.company_name,
                 'clothes' AS donation_type
             FROM clothes_donations cd JOIN donors d ON cd.donor_id = d.id
             WHERE cd.status IN ('Pending', 'Approved')
            AND (LOWER(cd.item_type) LIKE LOWER(?) OR LOWER(d.company_name) LIKE LOWER(?)
                 OR LOWER(cd.address) LIKE LOWER(?) OR LOWER(cd.category) LIKE LOWER(?)
                 OR LOWER('clothes') LIKE LOWER(?))
             UNION ALL
             SELECT bd.id, bd.title, bd.category, bd.quantity, bd.status,
                 bd.address, bd.created_at, bd.condition, d.company_name,
                 'books' AS donation_type
             FROM book_donations bd JOIN donors d ON bd.donor_id = d.id
             WHERE bd.status IN ('Pending', 'Approved')
            AND (LOWER(bd.title) LIKE LOWER(?) OR LOWER(d.company_name) LIKE LOWER(?)
                 OR LOWER(bd.address) LIKE LOWER(?) OR LOWER(bd.category) LIKE LOWER(?)
                 OR LOWER('books') LIKE LOWER(?))
             UNION ALL
             SELECT md.id, COALESCE(md.purpose, 'Money donation'), 'money', CAST(md.amount AS TEXT), md.status,
                 COALESCE(o.address, ''), md.created_at, md.payment_method, d.company_name,
                 'money' AS donation_type
             FROM money_donations md JOIN donors d ON md.donor_id = d.id
             LEFT JOIN orphanages o ON o.id = md.orphanage_id
             WHERE md.status IN ('Pending', 'Paid')
            AND (LOWER(COALESCE(md.purpose, 'money donation')) LIKE LOWER(?)
                 OR LOWER(d.company_name) LIKE LOWER(?) OR LOWER('money') LIKE LOWER(?))
         """
        wildcard = f"%{query}%"
        params = [wildcard] * 5 + [wildcard] * 5 + [wildcard] * 5 + [wildcard] * 3
        sql = f"SELECT * FROM ({sql}) AS donations WHERE 1 = 1"
        
        if category:
            sql += " AND category = ?"
            params.append(category)
        
        if status:
            sql += " AND status = ?"
            params.append(status)
        
        if veg_non_veg:
            sql += " AND veg_non_veg = ?"
            params.append(veg_non_veg)
        
        sql += " ORDER BY created_at DESC"
        
        cursor.execute(sql, params)
        results = cursor.fetchall()
        conn.close()
        return results

    @staticmethod
    def volunteer_search(user_id, query, status=None, veg_non_veg=None):
        """
        Search assigned pickups in Volunteer Dashboard.
        Params: food_name, donor_name, NGO_name, pickup_location, delivery_status
        """
        conn = get_db_connection()
        cursor = conn.cursor()
        
        sql = """
             SELECT fd.id, fd.food_name AS title, fd.category, fd.quantity, fd.status,
                 fd.address, fd.created_at, d.company_name AS donor_name, n.ngo_name,
                 pa.status AS assignment_status, 'food' AS donation_type, fd.veg_non_veg
             FROM food_donations fd JOIN donors d ON fd.donor_id = d.id
             JOIN food_distribution dist ON fd.id = dist.donation_id JOIN ngos n ON dist.ngo_id = n.id
             JOIN pickup_assignments pa ON fd.id = pa.donation_id
             WHERE pa.volunteer_id = (SELECT id FROM volunteers WHERE user_id = ?)
            AND (LOWER(fd.food_name) LIKE LOWER(?) OR LOWER(d.company_name) LIKE LOWER(?)
                 OR LOWER(n.ngo_name) LIKE LOWER(?) OR LOWER(fd.address) LIKE LOWER(?)
                 OR LOWER(pa.status) LIKE LOWER(?) OR LOWER('food') LIKE LOWER(?))
             UNION ALL
            SELECT COALESCE(cd.id, bd.id), CASE WHEN dd.donation_type = 'clothes' THEN cd.item_type ELSE bd.title END,
                 CASE WHEN dd.donation_type = 'clothes' THEN cd.category ELSE bd.category END,
                 CASE WHEN dd.donation_type = 'clothes' THEN cd.quantity ELSE bd.quantity END,
                 dd.status, CASE WHEN dd.donation_type = 'clothes' THEN cd.address ELSE bd.address END,
                 CASE WHEN dd.donation_type = 'clothes' THEN cd.created_at ELSE bd.created_at END,
                 donor.company_name, n.ngo_name, dd.status, dd.donation_type,
                 CASE WHEN dd.donation_type = 'clothes' THEN cd.condition ELSE bd.condition END
             FROM donation_distribution dd
             LEFT JOIN clothes_donations cd ON dd.donation_type = 'clothes' AND dd.donation_id = cd.id
             LEFT JOIN book_donations bd ON dd.donation_type = 'books' AND dd.donation_id = bd.id
            JOIN donors donor ON donor.id = COALESCE(cd.donor_id, bd.donor_id)
             JOIN ngos n ON dd.ngo_id = n.id
             WHERE dd.volunteer_id = (SELECT id FROM volunteers WHERE user_id = ?)
            AND (LOWER(CASE WHEN dd.donation_type = 'clothes' THEN cd.item_type ELSE bd.title END) LIKE LOWER(?)
                 OR LOWER(donor.company_name) LIKE LOWER(?) OR LOWER(n.ngo_name) LIKE LOWER(?)
                 OR LOWER(CASE WHEN dd.donation_type = 'clothes' THEN cd.address ELSE bd.address END) LIKE LOWER(?)
                 OR LOWER(dd.status) LIKE LOWER(?) OR LOWER(dd.donation_type) LIKE LOWER(?))
         """
        wildcard = f"%{query}%"
        params = [user_id] + [wildcard] * 6 + [user_id] + [wildcard] * 6
        sql = f"SELECT * FROM ({sql}) AS donations WHERE 1 = 1"
        
        if status:
            sql += " AND assignment_status = ?"
            params.append(status)
        
        if veg_non_veg:
            sql += " AND veg_non_veg = ?"
            params.append(veg_non_veg)
        
        sql += " ORDER BY created_at DESC"
        
        cursor.execute(sql, params)
        results = cursor.fetchall()
        conn.close()
        return results

    @staticmethod
    def admin_search(search_type, query, filters=None):
        """
        Global search in Admin Dashboard.
        search_type: 'users', 'donors', 'ngos', 'volunteers', 'donations', 'emergencies', 'feedback', 'notifications'
        """
        conn = get_db_connection()
        cursor = conn.cursor()
        results = []
        
        if search_type == 'users':
            sql = """
                SELECT id, username, email, role, status, created_at
                FROM users
                WHERE LOWER(username) LIKE LOWER(?)
                   OR LOWER(email) LIKE LOWER(?)
                   OR LOWER(full_name) LIKE LOWER(?)
            """
            cursor.execute(sql, [f"%{query}%", f"%{query}%", f"%{query}%"])
            results = cursor.fetchall()
        
        elif search_type == 'donors':
            sql = """
                SELECT d.id, d.company_name, d.contact_name, d.phone, d.address,
                       u.email, u.status
                FROM donors d
                JOIN users u ON d.user_id = u.id
                WHERE LOWER(d.company_name) LIKE LOWER(?)
                   OR LOWER(d.contact_name) LIKE LOWER(?)
                   OR LOWER(d.address) LIKE LOWER(?)
            """
            cursor.execute(sql, [f"%{query}%", f"%{query}%", f"%{query}%"])
            results = cursor.fetchall()
        
        elif search_type == 'ngos':
            sql = """
                SELECT n.id, n.ngo_name, n.registration_number, n.contact_person,
                       n.phone, n.address, n.is_verified, u.email, u.status
                FROM ngos n
                JOIN users u ON n.user_id = u.id
                WHERE LOWER(n.ngo_name) LIKE LOWER(?)
                   OR LOWER(n.registration_number) LIKE LOWER(?)
                   OR LOWER(n.contact_person) LIKE LOWER(?)
            """
            cursor.execute(sql, [f"%{query}%", f"%{query}%", f"%{query}%"])
            results = cursor.fetchall()
        
        elif search_type == 'volunteers':
            sql = """
                SELECT v.id, v.name, v.phone, v.address, v.is_approved,
                       u.email, u.status
                FROM volunteers v
                JOIN users u ON v.user_id = u.id
                WHERE LOWER(v.name) LIKE LOWER(?)
                   OR LOWER(v.phone) LIKE LOWER(?)
            """
            cursor.execute(sql, [f"%{query}%", f"%{query}%"])
            results = cursor.fetchall()
        
        elif search_type == 'donations':
            sql = """
                  SELECT fd.id, fd.food_name AS item_name, fd.category, fd.veg_non_veg AS details,
                       fd.quantity, fd.status, fd.created_at, 'food' AS donation_type,
                       d.company_name AS donor_name, u.email AS donor_email
                FROM food_donations fd JOIN donors d ON fd.donor_id = d.id JOIN users u ON d.user_id = u.id
                WHERE LOWER(fd.food_name) LIKE LOWER(?) OR LOWER(fd.category) LIKE LOWER(?)
                     OR LOWER(fd.address) LIKE LOWER(?) OR LOWER('food') LIKE LOWER(?)
                     OR LOWER(d.company_name) LIKE LOWER(?) OR LOWER(u.email) LIKE LOWER(?)
                UNION ALL
                SELECT cd.id, cd.item_type, cd.category, cd.condition, cd.quantity, cd.status, cd.created_at,
                       'clothes', d.company_name, u.email
                FROM clothes_donations cd JOIN donors d ON cd.donor_id = d.id JOIN users u ON d.user_id = u.id
                WHERE LOWER(cd.item_type) LIKE LOWER(?) OR LOWER(cd.category) LIKE LOWER(?)
                     OR LOWER(cd.address) LIKE LOWER(?) OR LOWER('clothes') LIKE LOWER(?)
                     OR LOWER(d.company_name) LIKE LOWER(?) OR LOWER(u.email) LIKE LOWER(?)
                UNION ALL
                SELECT bd.id, bd.title, bd.category, bd.condition, bd.quantity, bd.status, bd.created_at,
                       'books', d.company_name, u.email
                FROM book_donations bd JOIN donors d ON bd.donor_id = d.id JOIN users u ON d.user_id = u.id
                WHERE LOWER(bd.title) LIKE LOWER(?) OR LOWER(bd.category) LIKE LOWER(?)
                     OR LOWER(bd.address) LIKE LOWER(?) OR LOWER('books') LIKE LOWER(?)
                     OR LOWER(d.company_name) LIKE LOWER(?) OR LOWER(u.email) LIKE LOWER(?)
                UNION ALL
                  SELECT md.id, COALESCE(md.purpose, 'Money donation'), 'Money', md.payment_method,
                      CAST(md.amount AS TEXT), md.status, md.created_at, 'money', d.company_name, u.email
                FROM money_donations md JOIN donors d ON md.donor_id = d.id JOIN users u ON d.user_id = u.id
                WHERE LOWER(COALESCE(md.purpose, 'money donation')) LIKE LOWER(?)
                     OR LOWER('money') LIKE LOWER(?)
                     OR LOWER(d.company_name) LIKE LOWER(?) OR LOWER(u.email) LIKE LOWER(?)
            """
            wildcard = f"%{query}%"
            params = [wildcard] * 6 + [wildcard] * 6 + [wildcard] * 6 + [wildcard] * 4
            sql = f"SELECT * FROM ({sql}) AS donations WHERE 1 = 1"
            
            if filters and 'category' in filters:
                sql += " AND category = ?"
                params.append(filters['category'])

            if filters and 'donation_type' in filters:
                sql += " AND donation_type = ?"
                params.append(filters['donation_type'])
            
            if filters and 'status' in filters:
                sql += " AND status = ?"
                params.append(filters['status'])
            
            if filters and 'veg_non_veg' in filters:
                sql += " AND details = ?"
                params.append(filters['veg_non_veg'])
            
            if filters and 'date_from' in filters:
                sql += " AND DATE(created_at) >= DATE(?)"
                params.append(filters['date_from'])
            
            if filters and 'date_to' in filters:
                sql += " AND DATE(created_at) <= DATE(?)"
                params.append(filters['date_to'])
            
            sql += " ORDER BY created_at DESC"
            cursor.execute(sql, params)
            results = cursor.fetchall()
        
        elif search_type == 'emergencies':
            sql = """
                SELECT id, title, disaster_type, location, severity, 
                       details, created_at, is_active
                FROM emergencies
                WHERE LOWER(title) LIKE LOWER(?)
                   OR LOWER(disaster_type) LIKE LOWER(?)
                   OR LOWER(location) LIKE LOWER(?)
            """
            cursor.execute(sql, [f"%{query}%", f"%{query}%", f"%{query}%"])
            results = cursor.fetchall()
        
        elif search_type == 'feedback':
            sql = """
                SELECT f.id, f.rating, f.comments, f.created_at,
                       u.username, fd.food_name
                FROM feedback f
                JOIN users u ON f.user_id = u.id
                JOIN food_donations fd ON f.donation_id = fd.id
                WHERE LOWER(u.username) LIKE LOWER(?)
                   OR LOWER(fd.food_name) LIKE LOWER(?)
                   OR LOWER(f.comments) LIKE LOWER(?)
            """
            cursor.execute(sql, [f"%{query}%", f"%{query}%", f"%{query}%"])
            results = cursor.fetchall()
        
        elif search_type == 'notifications':
            sql = """
                SELECT id, user_id, message, is_read, created_at
                FROM notifications
                WHERE LOWER(message) LIKE LOWER(?)
            """
            cursor.execute(sql, [f"%{query}%"])
            results = cursor.fetchall()
        
        conn.close()
        return results
