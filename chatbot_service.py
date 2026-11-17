import re
from difflib import SequenceMatcher


def normalize_text(value):
    normalized = (value or '').strip().lower()
    return re.sub(r'[^a-z0-9\s]', ' ', normalized).strip()


def _keyword_matches(normalized, keyword):
    keyword = normalize_text(keyword)
    if not keyword:
        return False
    if re.search(r'\b' + re.escape(keyword) + r'\b', normalized):
        return True
    if ' ' in keyword:
        return False
    return any(
        SequenceMatcher(None, token, keyword).ratio() >= 0.84
        for token in normalized.split()
        if len(token) >= 3 and len(keyword) >= 3
    )


def contains_any(text, keywords):
    normalized = normalize_text(text)
    return any(_keyword_matches(normalized, keyword) for keyword in keywords)


def generate_chatbot_reply(message, context=''):
    text = message or ''
    lowered = normalize_text(text)
    previous = normalize_text(context)

    if not lowered:
        return "Please type a question about FeedForward and I’ll help explain it."

    if contains_any(text, ['where can i find it', 'where is it', 'download it', 'where can i access it']) and contains_any(previous, ['certificate', 'certification']):
        if contains_any(previous, ['volunteer']):
            return "You can check your Volunteer Dashboard or activity/history section when the certificate becomes available."
        return "You can check your Donor Dashboard or donation history when the certificate becomes available."

    if contains_any(text, ['does it require inspection', 'does it need inspection', 'is it inspected']) and contains_any(previous, ['money', 'payment']):
        return "No. Food inspection applies to food donations; money donations use a separate payment process."

    if contains_any(text, ['what about money', 'and money', 'money donations']) and contains_any(previous, ['how donation', 'donation process', 'how does donation work']):
        return "Money donations use a separate payment flow and do not require food inspection. Food, clothes, and books follow their own donation workflows."

    if contains_any(text, ['how it works', 'how does it work', 'how website works', 'how does feedforward work', 'what is feedforward', 'working of feedforward']):
        return """FeedForward connects donors, NGOs, volunteers, and beneficiaries.

1. Register as a donor, NGO, or volunteer.
2. Choose Food, Clothes, Books, or Money.
3. Complete the required review, inspection, or payment process.
4. NGOs and volunteers coordinate pickup, delivery, and distribution.
5. Completed eligible contributions can receive updates, recognition, and certificates.

Money donations use a separate payment process and do not require food inspection."""

    if contains_any(text, ['how many donation types', 'what can i donate', 'types of donations', 'donation types', 'what donation types are available']):
        return "FeedForward currently supports four donation types: Food, Clothes, Books, and Money."

    if contains_any(text, ['leaderboard', 'ranking', 'points calculated', 'improve my ranking']):
        return """The FeedForward leaderboard recognizes completed contributions.

- Donors earn points for eligible completed donations.
- Volunteers earn points for completed pickups, deliveries, and activities.
- Higher points improve a user’s ranking.

Keep contributing through eligible activities to increase your impact and position."""

    if contains_any(text, ['where will i receive my otp', 'where receive otp', 'email verification', 'verify email', 'verification code', 'otp']):
        return "A verification OTP is sent to your registered email address. Enter it on the verification screen to confirm your email and continue."

    if contains_any(text, ['one email', 'same email', 'different roles', 'multiple roles']) and contains_any(text, ['role', 'roles', 'account']):
        return "Yes. The same email can be used for different roles, such as Donor plus Volunteer or NGO plus Volunteer. However, one email cannot create multiple accounts under the same role."

    if contains_any(text, ['can money donations be inspected', 'money donation inspection', 'inspect money', 'money inspected']):
        return "No. Food-safety inspection applies only to food donations. Money donations use the separate payment process."

    if contains_any(text, ['hello', 'hi', 'hey', 'good morning', 'good evening', 'good afternoon']):
        return "Hi! I’m the FeedForward Assistant. I can help with donations, volunteering, NGO registration, food inspection, and how the platform works."

    if contains_any(text, ['thank you', 'thanks', 'thank u']):
        return "You’re welcome! I’m happy to help with FeedForward donations, inspections, certificates, and volunteering."

    certificate_keywords = [
        'certificate', 'certificates', 'certification', 'certifications',
        'certified', 'certificaion', 'certificaions', 'receipt'
    ]
    if (
        contains_any(text, ['donor', 'donors']) and contains_any(text, ['volunteer', 'volunteers']) and contains_any(text, certificate_keywords)
    ):
        return "Yes! FeedForward can provide certificates for both donors and volunteers. Donors can access a certificate or acknowledgement after a successful eligible donation from the donor dashboard or donation history. Volunteers can receive a certificate after successfully completing eligible volunteer activities or deliveries from the volunteer dashboard or activity/history section when it becomes available."

    if contains_any(text, ['volunteer certificate', 'volunteer certification']) or (
        contains_any(text, ['volunteer', 'volunteers']) and contains_any(text, certificate_keywords)
    ):
        return "Yes! FeedForward can provide volunteer certificates after successfully completing eligible volunteer activities or deliveries. You can access a volunteer certificate from your volunteer dashboard or activity/history section when it becomes available."

    if contains_any(text, certificate_keywords) or (
        contains_any(text, ['donation', 'donations']) and contains_any(text, ['proof', 'recognition', 'award'])
    ):
        return "Yes! FeedForward can provide certificates for both donors and volunteers. Donors can access a certificate or acknowledgement after a successful eligible donation from the donor dashboard or donation history. Volunteers can receive a certificate after successfully completing eligible volunteer activities or deliveries from the volunteer dashboard or activity/history section when it becomes available."

    if contains_any(text, ['how many donation types', 'what can i donate', 'types of donations', 'donation types']):
        return "FeedForward currently supports four donation types: Food, Clothes, Books, and Money."

    if contains_any(text, ['food', 'meal', 'meals']) and contains_any(text, ['donate', 'donation', 'list', 'give', 'how', 'process', 'work']):
        return "FeedForward lets donors list surplus food, NGOs review and accept eligible items, and volunteers help with pickup and delivery. Food donations follow the inspection workflow before they can continue."

    if contains_any(text, ['clothes', 'clothing', 'garments']) and contains_any(text, ['donate', 'donation', 'give', 'how', 'process', 'help']):
        return "To donate clothes, sign in as a donor and choose the clothes donation option. Add the item details and submit the listing so an NGO can review and coordinate collection or distribution."

    if contains_any(text, ['books', 'book']) and contains_any(text, ['donate', 'donation', 'give', 'how', 'process', 'help']):
        return "To donate books, sign in as a donor and choose the books donation option. Provide the book details and submit the listing for NGO review and distribution."

    if contains_any(text, ['money', 'funds', 'financial']) and contains_any(text, ['donate', 'donation', 'give', 'how', 'process']):
        return "To donate money, choose the money donation option, select the intended beneficiary when available, and complete the payment flow. Money donations do not require food-safety inspection."

    if contains_any(text, ['become a volunteer', 'volunteer registration', 'volunteer', 'join volunteer', 'volunteer role']):
        return "You can register as a volunteer and help with pickup, delivery, inspection, and coordination work. Volunteers can inspect food donations and deliver eligible ones after the required checks."

    if contains_any(text, ['register as ngo', 'ngo registration', 'ngo', 'become ngo', 'claiming']):
        return "FeedForward is designed to connect NGOs with donors and volunteers. NGOs can register, receive or claim eligible donations, and coordinate distribution to beneficiaries."

    if contains_any(text, ['where is my donation', 'donation status', 'what is happening with my donation', 'track my donation', 'donation tracking']):
        return "You can check your donation status from your donor dashboard or donation history. The status shows the current stage, such as pending review, accepted, pickup, delivery, or completed."

    if contains_any(text, ['rejected', 'unsafe', 'failed inspection']):
        return "If food is rejected during inspection, it is marked as 'Inspection Completed – Rejected' and cannot proceed to pickup, delivery, or distribution."

    if contains_any(text, ['food inspection', 'inspect food', 'inspection', 'food safety', 'who inspects', 'is food inspected', 'approved', 'inspection completed']):
        return "For food donations, the inspection workflow is important. After inspection, the status must clearly show either 'Inspection Completed – Approved' or 'Inspection Completed – Rejected'. Only approved food donations can continue to delivery or distribution. Rejected food donations must not proceed."

    if contains_any(text, ['emergency donation', 'emergency', 'disaster']) or contains_any(text, ['urgent donation', 'urgent relief']):
        return "FeedForward supports emergency and disaster-related donation workflows. Admins can enable emergency alerts, and the platform can surface urgent relief needs for rapid response."

    if contains_any(text, ['how does donation work', 'how donation works', 'donation process', 'steps']) or contains_any(text, ['process', 'workflow']):
        return "The typical donation flow is: donor lists an item, NGO reviews and confirms eligibility, volunteers may assist with pickup or inspection, and approved items are delivered or distributed. Food donations follow the inspection requirement before delivery."

    if contains_any(text, ['feedforward', 'what is', 'about feedforward', 'platform']):
        return "FeedForward connects donors, NGOs, and volunteers to reduce waste and help people in need. It supports food, clothes, books, and money donations in a simple and transparent way."

    if contains_any(text, ['food safety', 'safe', 'inspection before delivery', 'eligible donation']):
        return "Food safety and inspection are required for food donations. Volunteers can inspect eligible food items, and only approved donations are allowed to move forward."

    if contains_any(text, ['donor', 'ngo', 'volunteer']):
        return "FeedForward brings donors, NGOs, and volunteers together in one community network to reduce waste, support beneficiaries, and coordinate help efficiently."

    if contains_any(text, ['where', 'page', 'can i', 'go to']):
        return "If you need the relevant page, check the donor, NGO, or volunteer dashboard, or use the website navigation to reach registration, donation, contact, or profile sections."

    if contains_any(text, ['not sure', 'cannot', 'unknown', 'what do you know', 'help']) or contains_any(text, ['who are you', 'assistant']):
        return "I can help with FeedForward donation flows, volunteer and NGO processes, food inspection rules, and emergency support information. If you need something outside the platform, I can point you to the correct page."
   
    how_it_works_keywords = [
    'how it works',
    'how does it work',
    'how this works',
    'how website works',
    'how does the website work',
    'how feedforward works',
    'how does feedforward work',
    'working of feedforward',
    'working of website'
]

    if contains_any(text, how_it_works_keywords):
        return """FeedForward is a platform that connects donors, NGOs, and volunteers to reduce wastage and help people in need.
 
Here's how it works:

1. Register – Sign up as a Donor, NGO, or Volunteer.
2. Donate – Choose Food, Clothes, Books, or Money.
3. Inspect – Food donations are checked by a volunteer for safety.
4. Approve – Safe and approved food moves to the next stage.
5. Pickup & Delivery – Volunteers collect the donation and deliver it through the NGO.
6. Complete – The donation reaches the intended beneficiaries.
7. Certificates – Eligible donors and volunteers can receive certificates.
8. Notifications – Get updates about your donation status.

💡 Money donations use a separate payment process and don't require food inspection."""

   


    benefits_keywords = [
    'benefits',
    'benefit',
    'what benefits',
    'benefits do i get',
    'what do i get',
    'advantages',
    'why should i donate',
    'benefits of donating'
]


    if contains_any(text, benefits_keywords):
       return """FeedForward helps you make a real difference while getting recognized for your contribution.

    🌱 Reduce wastage
    🤝 Support people in need
    📜 Earn certificates
    🏆 Get leaderboard recognition
    🔔 Receive activity updates
    💙 Make a positive social impact"""


    leaderboard_keywords = [
    'leaderboard',
    'leaderboards',
    'how leaderboard works',
    'how does leaderboard work',
    'how the leaderboard works',
    'what is leaderboard',
    'leaderboard works'
]

    if contains_any(text, leaderboard_keywords):
      return """The FeedForward leaderboard ranks users based on their completed contributions.

🏆 Donors earn points for successful donations.
🤝 Volunteers earn points for completed activities.
📊 Points determine the user's ranking.
🥇 Higher points mean a higher leaderboard position.

Keep contributing to increase your impact and move up the leaderboard! 🌟"""


    email_keywords = [
    'email',
    'e-mail',
    'mail',
    'email notification',
    'email notifications',
    'email update',
    'email updates',
    'email verification',
    'verify email',
    'otp',
    'verification code',
    'email confirmation'
]

    # OTP / Email verification
    if contains_any(text, ['otp', 'verification code', 'email verification', 'verify email']):
       return "A verification OTP is sent to your registered email address. Enter the OTP to verify your email and continue."

# Email notifications
    if contains_any(text, ['email notification', 'email notifications', 'email updates']):
      return "FeedForward can send important updates about your donations, activities, and status to your registered email address."

# Forgot password / username
    if contains_any(text, ['forgot password', 'forgot username', 'reset password', 'reset username']):
      return "If you forgot your password or username, use the Forgot Password or Forgot Username option and follow the instructions sent to your registered email."

# Certificate email
    if contains_any(text, ['certificate email', 'certificate through email']):
     return "Eligible donors and volunteers can receive certificate-related information through their registered email when the certificate becomes available."

    role_email_keywords = [
    'one email different roles',
    'one email for different roles',
    'same email different roles',
    'same email for different roles',
    'can one email be used for different roles',
    'can i use one email for multiple roles',
    'one email multiple roles',
    'same email multiple roles',
    'different roles same email'
]

    if contains_any(text, role_email_keywords):
       return """Yes! You can use the same email address for different roles in FeedForward.

For example:
✅ Donor + Volunteer+NGO: You can register as a donor, volunteer, and NGO using the same email address. Each role will have its own separate account and dashboard.


However, the same email cannot be used to create multiple accounts under the same role.

Each role will have separate role-based access and permissions."""
    # Helpful fallback with actual project-specific guidance.
    if re.search(r'\b(food|clothes|books|money|donation|volunteer|ngo|inspection|emergency)\b', lowered):
        return "I can help with FeedForward donation workflows, food inspection rules, volunteer and NGO processes, and emergency support. For food items, inspection and approval are required before delivery."

    return "I can help with FeedForward donation, inspection, volunteer, NGO, and emergency support questions. Please ask about the donation process, food inspection, or how to register."
