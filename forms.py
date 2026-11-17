import re
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, SelectField, TextAreaField, IntegerField, DecimalField
from wtforms.validators import DataRequired, Email, EqualTo, Length, NumberRange, ValidationError, Optional
from flask_wtf.file import FileAllowed, FileField
from flask_login import current_user
from database import get_db_connection


def normalize_email(value):
    return value.strip().lower() if value else value


def normalize_phone(value):
    if not value:
        return value
    cleaned = value.strip().replace(' ', '')
    return cleaned


def is_duplicate_email(email, role=None):
    conn = get_db_connection()
    try:
        if role:
            row = conn.execute("SELECT 1 FROM users u WHERE LOWER(u.email) = ? AND (u.role = ? OR EXISTS (SELECT 1 FROM user_roles ur WHERE ur.user_id = u.id AND ur.role = ?))", (email.strip().lower(), role, role)).fetchone()
        else:
            row = conn.execute("SELECT id FROM users WHERE LOWER(email) = ?", (email.strip().lower(),)).fetchone()
        return bool(row)
    finally:
        conn.close()


def is_duplicate_phone(phone, role=None):
    normalized = normalize_phone(phone)
    if not normalized:
        return False

    candidates = [normalized]
    if normalized.startswith('+'):
        candidates.append(normalized[1:])
    elif re.match(r'^[6789][0-9]{9}$', normalized):
        candidates.append('+91' + normalized)

    placeholder = ','.join(['?'] * len(candidates))
    conn = get_db_connection()
    try:
        tables = {'donor': ['donors'], 'ngo': ['ngos'], 'volunteer': ['volunteers'], 'admin': ['admins']}.get(role, ['users', 'donors', 'ngos', 'volunteers', 'admins'])
        for table in tables:
            row = conn.execute(f"SELECT id FROM {table} WHERE phone IN ({placeholder})", tuple(candidates)).fetchone()
            if row:
                return True
        return False
    finally:
        conn.close()


class LoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=3, max=50)])
    user_type = SelectField('Login as', choices=[
        ('donor', 'Donor'),
        ('ngo', 'NGO'),
        ('volunteer', 'Volunteer')
    ], validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Login')


class AdminLoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=3, max=50)])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Admin Login')


class DonorRegisterForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=3, max=50)])
    email = StringField('Email', validators=[DataRequired(), Email(), Length(max=100)], filters=[normalize_email])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=6, max=100)])
    confirm_password = PasswordField('Confirm Password', validators=[DataRequired(), EqualTo('password', message='Passwords must match')])
    company_name = StringField('Business/Hostel/Hall/Restaurant Name', validators=[DataRequired(), Length(max=150)])
    organization_type = SelectField('Organisation Type', choices=[
        ('Restaurant', 'Restaurant'),
        ('Hotel', 'Hotel'),
        ('Hostel', 'Hostel / College Hall'),
        ('Event Organizer', 'Event Organizer'),
        ('Wedding Hall', 'Wedding Hall'),
        ('Individual', 'Individual'),
        ('Other', 'Other')
    ], validators=[DataRequired()])
    organization_type_other = StringField('Specify Other Organisation Type', validators=[Optional(), Length(max=100)])
    contact_name = StringField('Contact Person Name', validators=[DataRequired(), Length(max=100)])
    phone = StringField('Phone Number', validators=[DataRequired(), Length(min=10, max=15)], filters=[normalize_phone])
    address = StringField('Address', validators=[DataRequired(), Length(max=300)])
    latitude = DecimalField('Latitude', validators=[Optional()])
    longitude = DecimalField('Longitude', validators=[Optional()])
    submit = SubmitField('Register as Donor')

    def validate_username(self, username):
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username.data,)).fetchone()
        conn.close()
        if user and user['email'].lower() != (self.email.data or '').strip().lower():
            raise ValidationError('Username is already taken. Please choose another one.')

    def validate_email(self, email):
        if is_duplicate_email(email.data, 'donor'):
            raise ValidationError('This email is already registered for the donor role.')

    def validate_password(self, password):
        p_data = password.data
        if len(p_data) < 6:
            raise ValidationError('Password must be at least 6 characters long.')
        if not any(char.isdigit() for char in p_data):
            raise ValidationError('Password must contain at least one digit.')
        if not any(char.isalpha() for char in p_data):
            raise ValidationError('Password must contain at least one letter.')

    def validate_phone(self, phone):
        p_data = phone.data
        if not re.match(r'^(?:[6789][0-9]{9}|\+91[6789][0-9]{9})$', p_data):
            raise ValidationError('Enter a 10-digit number or a +91-prefixed 12-digit phone number.')
        if is_duplicate_phone(p_data, 'donor'):
            raise ValidationError('This phone number is already registered.')

class NGORegisterForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=3, max=50)])
    email = StringField('Email', validators=[DataRequired(), Email(), Length(max=100)], filters=[normalize_email])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=6, max=100)])
    confirm_password = PasswordField('Confirm Password', validators=[DataRequired(), EqualTo('password', message='Passwords must match')])
    ngo_name = StringField('Organization Name', validators=[DataRequired(), Length(max=150)])
    organization_type = SelectField('Organization Type', choices=[
        ('NGO', 'NGO'),
        ('Orphanage', 'Orphanage'),
        ('Old Age Home', 'Old Age Home'),
        ('Shelter Home', 'Shelter Home'),
        ('Community Kitchen', 'Community Kitchen'),
        ('Government Relief Center', 'Government Relief Center'),
        ('Other', 'Other')
    ], validators=[DataRequired()])
    organization_type_other = StringField('Specify Other Organization Type', validators=[Optional(), Length(max=100)])
    registration_number = StringField('Registration / Unique ID Number', validators=[DataRequired(), Length(max=100)])
    contact_person = StringField('Contact Person', validators=[DataRequired(), Length(max=100)])
    phone = StringField('Phone Number', validators=[DataRequired(), Length(min=10, max=15)], filters=[normalize_phone])
    address = StringField('Address', validators=[DataRequired(), Length(max=300)])
    latitude = DecimalField('Latitude', validators=[Optional()])
    longitude = DecimalField('Longitude', validators=[Optional()])
    submit = SubmitField('Register Organization')

    def validate_username(self, username):
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username.data,)).fetchone()
        conn.close()
        if user and user['email'].lower() != (self.email.data or '').strip().lower():
            raise ValidationError('Username is already taken.')

    def validate_email(self, email):
        if is_duplicate_email(email.data, 'ngo'):
            raise ValidationError('This email is already registered for the NGO role.')

    def validate_registration_number(self, registration_number):
        conn = get_db_connection()
        ngo = conn.execute("SELECT * FROM ngos WHERE registration_number = ?", (registration_number.data,)).fetchone()
        conn.close()
        if ngo:
            raise ValidationError('Registration Number is already registered.')

    def validate_password(self, password):
        p_data = password.data
        if len(p_data) < 6:
            raise ValidationError('Password must be at least 6 characters long.')
        if not any(char.isdigit() for char in p_data):
            raise ValidationError('Password must contain at least one digit.')
        if not any(char.isalpha() for char in p_data):
            raise ValidationError('Password must contain at least one letter.')

    def validate_phone(self, phone):
        p_data = phone.data
        if not re.match(r'^(?:[6789][0-9]{9}|\+91[6789][0-9]{9})$', p_data):
            raise ValidationError('Enter a 10-digit number or a +91-prefixed 12-digit phone number.')
        if is_duplicate_phone(p_data, 'ngo'):
            raise ValidationError('This phone number is already registered.')

class VolunteerRegisterForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=3, max=50)])
    email = StringField('Email', validators=[DataRequired(), Email(), Length(max=100)], filters=[normalize_email])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=6, max=100)])
    confirm_password = PasswordField('Confirm Password', validators=[DataRequired(), EqualTo('password', message='Passwords must match')])
    name = StringField('Full Name', validators=[DataRequired(), Length(max=100)])
    phone = StringField('Phone Number', validators=[DataRequired(), Length(min=10, max=15)], filters=[normalize_phone])
    address = StringField('Address', validators=[DataRequired(), Length(max=300)])
    transport_type = SelectField('Mode of Transport', choices=[
        ('Two-Wheeler', 'Two-Wheeler'),
        ('Four-Wheeler', 'Four-Wheeler'),
        ('Walk', 'Walk')
    ], validators=[DataRequired()])
    latitude = DecimalField('Latitude', validators=[Optional()])
    longitude = DecimalField('Longitude', validators=[Optional()])
    submit = SubmitField('Register as Volunteer')

    def validate_email(self, email):
        if is_duplicate_email(email.data, 'volunteer'):
            raise ValidationError('This email is already registered for the volunteer role.')

    def validate_phone(self, phone):
        if not re.match(r'^(?:[6789][0-9]{9}|\+91[6789][0-9]{9})$', phone.data):
            raise ValidationError('Enter a valid phone number.')
        if is_duplicate_phone(phone.data, 'volunteer'):
            raise ValidationError('This phone number is already registered for the volunteer role.')


class ProfileForm(FlaskForm):
    profile_image = FileField('Profile Picture', validators=[Optional(), FileAllowed(['jpg', 'jpeg', 'png', 'webp'], 'Image files only')])
    username = StringField('Username', validators=[DataRequired(), Length(min=3, max=50)])
    full_name = StringField('Full Name / Contact Person', validators=[DataRequired(), Length(max=150)])
    phone = StringField('Phone Number', validators=[DataRequired(), Length(min=10, max=15)], filters=[normalize_phone])
    address = StringField('Address', validators=[DataRequired(), Length(max=300)])
    email = StringField('Email', validators=[DataRequired(), Email(), Length(max=100)], filters=[normalize_email])
    current_password = PasswordField('Current Password', validators=[Optional(), Length(min=6, max=100)])
    new_password = PasswordField('New Password', validators=[Optional(), Length(min=6, max=100)])
    confirm_new_password = PasswordField('Confirm New Password', validators=[Optional(), EqualTo('new_password', message='New passwords must match')])
    account_role = StringField('Current Role', render_kw={'readonly': True})
    registration_number = StringField('Registration Number', validators=[Optional(), Length(max=100)])
    company_name = StringField('Company / Organization Name', validators=[Optional(), Length(max=150)])
    organization_type = SelectField('Organization Type', choices=[
        ('', 'Select type'),
        ('Restaurant', 'Restaurant'),
        ('Hotel', 'Hotel'),
        ('Hostel', 'Hostel / College Hall'),
        ('Event Organizer', 'Event Organizer'),
        ('Wedding Hall', 'Wedding Hall'),
        ('Individual', 'Individual'),
        ('NGO', 'NGO'),
        ('Orphanage', 'Orphanage'),
        ('Old Age Home', 'Old Age Home'),
        ('Shelter Home', 'Shelter Home'),
        ('Community Kitchen', 'Community Kitchen'),
        ('Government Relief Center', 'Government Relief Center'),
        ('Other', 'Other')
    ], validators=[Optional()])
    organization_type_other = StringField('Specify Other Type', validators=[Optional(), Length(max=100)])
    transport_type = SelectField('Transport Type', choices=[
        ('', 'Select transport'),
        ('Two-Wheeler', 'Two-Wheeler'),
        ('Four-Wheeler', 'Four-Wheeler'),
        ('Walk', 'Walk')
    ], validators=[Optional()])
    submit = SubmitField('Save Profile')

    def validate_username(self, username):
        normalized_username = username.data.strip() if username.data else ''
        if normalized_username:
            conn = get_db_connection()
            existing = conn.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(?) AND id != ?", (normalized_username, current_user.id)).fetchone()
            conn.close()
            if existing:
                raise ValidationError('This username is already in use.')

    def validate_email(self, email):
        normalized_email = normalize_email(email.data)
        if normalized_email:
            conn = get_db_connection()
            existing = conn.execute("SELECT id FROM users WHERE LOWER(email) = LOWER(?) AND role = ? AND id != ?", (normalized_email, current_user.role, current_user.id)).fetchone()
            conn.close()
            if existing:
                raise ValidationError('This email is already registered for your role.')

    def validate_phone(self, phone):
        p_data = phone.data
        if p_data and not re.match(r'^(?:[6789][0-9]{9}|\+91[6789][0-9]{9})$', p_data):
            raise ValidationError('Enter a 10-digit number or a +91-prefixed 12-digit phone number.')
        if p_data:
            conn = get_db_connection()
            existing = conn.execute("SELECT id FROM users WHERE phone = ? AND id != ?", (p_data, current_user.id)).fetchone()
            conn.close()
            if existing:
                raise ValidationError('This phone number is already registered.')

    def validate_organization_type_other(self, organization_type_other):
        if self.organization_type.data == 'Other' and not organization_type_other.data:
            raise ValidationError('Please specify the organization type.')

    def validate_registration_number(self, registration_number):
        if self.account_role.data and self.account_role.data.lower() == 'ngo' and registration_number.data:
            conn = get_db_connection()
            existing = conn.execute("SELECT id FROM ngos WHERE registration_number = ? AND user_id != ?", (registration_number.data.strip(), current_user.id)).fetchone()
            conn.close()
            if existing:
                raise ValidationError('This registration number is already registered.')

class DonationForm(FlaskForm):
    food_name = StringField('Food Item Name(s)', validators=[DataRequired(), Length(max=150)])
    category = SelectField('Food Category', choices=[
        ('Cooked Food', 'Cooked Food'),
        ('Packaged Food', 'Packaged Food'),
        ('Raw Ingredients', 'Raw Ingredients'),
        ('Groceries', 'Groceries'),
        ('Other', 'Other')
    ], validators=[DataRequired()])
    other_category = StringField('Please specify', validators=[Optional(), Length(max=120)])
    veg_non_veg = SelectField('Veg / Non Veg', choices=[
        ('Veg', 'Veg'),
        ('Non-Veg', 'Non-Veg')
    ], validators=[DataRequired()])
    quantity = StringField('Quantity (e.g., 10 kg, for 50 people)', validators=[DataRequired(), Length(max=100)])
    expiry_time = StringField('Expiry Time / Best Before (e.g., Today 10 PM, or Date)', validators=[DataRequired(), Length(max=100)])
    pickup_time = StringField('Preferred Pickup Time window', validators=[DataRequired(), Length(max=100)])
    address = StringField('Address for Pickup', validators=[DataRequired(), Length(max=300)])
    latitude = DecimalField('Latitude', validators=[Optional()])
    longitude = DecimalField('Longitude', validators=[Optional()])
    submit = SubmitField('List Donation')


class ClothesDonationForm(FlaskForm):
    item_type = StringField('Item Type', validators=[DataRequired(), Length(max=120)])
    category = SelectField('Clothing Category', choices=[
        ('Men', 'Men'),
        ('Women', 'Women'),
        ('Children', 'Children'),
        ('Winter Wear', 'Winter Wear'),
        ('Accessories', 'Accessories'),
        ('Other', 'Other')
    ], validators=[DataRequired()])
    other_category = StringField('Please specify', validators=[Optional(), Length(max=120)])
    size = StringField('Size / Fit', validators=[DataRequired(), Length(max=80)])
    quantity = StringField('Quantity', validators=[DataRequired(), Length(max=80)])
    condition = SelectField('Condition', choices=[
        ('New', 'New'),
        ('Like New', 'Like New'),
        ('Good', 'Good'),
        ('Needs Repair', 'Needs Repair')
    ], validators=[DataRequired()])
    description = TextAreaField('Description', validators=[Optional(), Length(max=500)])
    pickup_time = StringField('Preferred Pickup Time', validators=[DataRequired(), Length(max=100)])
    address = StringField('Pickup Address', validators=[DataRequired(), Length(max=300)])
    latitude = DecimalField('Latitude', validators=[Optional()])
    longitude = DecimalField('Longitude', validators=[Optional()])
    submit = SubmitField('Submit Clothes Donation')


class BookDonationForm(FlaskForm):
    title = StringField('Book Title', validators=[DataRequired(), Length(max=180)])
    author = StringField('Author', validators=[Optional(), Length(max=180)])
    category = SelectField('Book Category', choices=[
        ('School Books', 'School Books'),
        ('Story Books', 'Story Books'),
        ('Children Books', 'Children Books'),
        ('Reference Books', 'Reference Books'),
        ('General Reading', 'General Reading'),
        ('Other', 'Other')
    ], validators=[DataRequired()])
    other_category = StringField('Please specify', validators=[Optional(), Length(max=120)])
    language = StringField('Language', validators=[Optional(), Length(max=80)])
    quantity = StringField('Quantity', validators=[DataRequired(), Length(max=80)])
    condition = SelectField('Condition', choices=[
        ('New', 'New'),
        ('Like New', 'Like New'),
        ('Good', 'Good'),
        ('Well Used', 'Well Used')
    ], validators=[DataRequired()])
    description = TextAreaField('Description', validators=[Optional(), Length(max=500)])
    pickup_time = StringField('Preferred Pickup Time', validators=[DataRequired(), Length(max=100)])
    address = StringField('Pickup Address', validators=[DataRequired(), Length(max=300)])
    latitude = DecimalField('Latitude', validators=[Optional()])
    longitude = DecimalField('Longitude', validators=[Optional()])
    submit = SubmitField('Submit Book Donation')


class MoneyDonationForm(FlaskForm):
    orphanage_id = SelectField('Approved Orphanage / Organization', choices=[], validators=[DataRequired()])
    amount = DecimalField('Amount', validators=[DataRequired()])
    purpose = SelectField('Donation Purpose', choices=[
        ('Education Support', 'Education Support'),
        ('Food Support', 'Food Support'),
        ('Medical Care', 'Medical Care'),
        ('General Relief', 'General Relief')
    ], validators=[DataRequired()])
    payment_method = SelectField('Payment Method', choices=[
        ('Razorpay', 'Razorpay (UPI, cards, net banking)')
    ], validators=[DataRequired()])
    note = TextAreaField('Message / Notes', validators=[Optional(), Length(max=500)])
    submit = SubmitField('Proceed to Payment')


class FeedbackForm(FlaskForm):
    food_rating = SelectField('Food Quality Rating', choices=[
        ('5', '5 - Excellent'),
        ('4', '4 - Very Good'),
        ('3', '3 - Good'),
        ('2', '2 - Fair'),
        ('1', '1 - Poor')
    ], validators=[DataRequired()])
    delivery_rating = SelectField('Delivery Rating', choices=[
        ('5', '5 - Excellent'),
        ('4', '4 - Very Good'),
        ('3', '3 - Good'),
        ('2', '2 - Fair'),
        ('1', '1 - Poor')
    ], validators=[DataRequired()])
    comments = TextAreaField('Feedback & Comments', validators=[DataRequired(), Length(max=500)])
    submit = SubmitField('Submit Feedback')


class PlatformFeedbackForm(FlaskForm):
    rating = SelectField('Platform Rating', choices=[
        ('5', '5 - Excellent'),
        ('4', '4 - Very Good'),
        ('3', '3 - Good'),
        ('2', '2 - Fair'),
        ('1', '1 - Poor')
    ], validators=[DataRequired()])
    comments = TextAreaField('Feedback & Comments', validators=[DataRequired(), Length(max=500)])
    submit = SubmitField('Submit Feedback')

class ContactForm(FlaskForm):
    name = StringField('Full Name', validators=[DataRequired(), Length(max=120)])
    email = StringField('Email Address', validators=[DataRequired(), Email(), Length(max=120)], filters=[normalize_email])
    phone = StringField('Phone Number', validators=[Optional(), Length(min=10, max=15)], filters=[normalize_phone])
    subject = StringField('Subject', validators=[DataRequired(), Length(max=200)])
    message = TextAreaField('Message', validators=[DataRequired(), Length(min=10, max=2000)])
    submit = SubmitField('Send Message')

    def validate_phone(self, phone):
        p_data = (phone.data or '').strip()
        if not p_data:
            return
        if not re.match(r'^(?:[6789][0-9]{9}|\+91[6789][0-9]{9})$', p_data):
            raise ValidationError('Enter a valid 10-digit number or +91-prefixed number.')

class EmergencyForm(FlaskForm):
    title = StringField('Emergency Title', validators=[DataRequired(), Length(max=150)])
    disaster_type = SelectField('Disaster Type', choices=[
        ('Flood', 'Flood'),
        ('Cyclone', 'Cyclone'),
        ('Storm', 'Storm'),
        ('Earthquake', 'Earthquake'),
        ('Fire', 'Fire'),
        ('Other', 'Other')
    ], validators=[DataRequired()])
    location = StringField('Affected Location', validators=[DataRequired(), Length(max=200)])
    severity = SelectField('Severity Level', choices=[
        ('Low', 'Low'),
        ('Medium', 'Medium'),
        ('High', 'High'),
        ('Critical', 'Critical')
    ], validators=[DataRequired()])
    start_date = StringField('Start Date / Period', validators=[DataRequired(), Length(max=100)])
    end_date = StringField('End Date / Expected Resolution', validators=[Optional(), Length(max=100)])
    details = TextAreaField('Emergency Description', validators=[DataRequired(), Length(max=800)])
    is_active = SelectField('Active Alert', choices=[('1', 'Yes'), ('0', 'No')], validators=[DataRequired()])
    submit = SubmitField('Publish Emergency')

class EmergencyModeRequestForm(FlaskForm):
    note = TextAreaField('Request Notes', validators=[DataRequired(), Length(max=500)])
    submit = SubmitField('Request Emergency Mode')

class SystemSettingsForm(FlaskForm):
    # Dummy system settings form for demonstration of Admin ability to save
    system_name = StringField('Application Name', validators=[DataRequired()])
    contact_email = StringField('Admin Contact Email', validators=[DataRequired(), Email()])
    enable_notifications = SelectField('Enable Global Notifications', choices=[('1', 'Yes'), ('0', 'No')])
    enable_emergency_mode = SelectField('Enable Emergency Disaster Mode', choices=[('1', 'On'), ('0', 'Off')])
    submit = SubmitField('Save Settings')
