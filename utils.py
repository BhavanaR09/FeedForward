import os
import datetime
import qrcode
from xml.sax.saxutils import escape
from io import BytesIO
from openpyxl import Workbook
from reportlab.lib.pagesizes import A4, letter
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import inch
from config import Config

def generate_qr_code(donation_id, food_name):
    """Generate a QR code for a specific donation ID and return the relative file path."""
    qr_data = f"FEEDFORWARD_DONATION_ID:{donation_id}"
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    
    filename = f"donation_qr_{donation_id}.png"
    filepath = os.path.join(Config.UPLOAD_FOLDER, 'qrcodes', filename)
    img.save(filepath)
    
    # Return path relative to static/
    return f"uploads/qrcodes/{filename}"

def export_donations_to_excel(donations_list):
    """Export a list of donations to an Excel file using openpyxl and return the bytes buffer."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Food Donations Report"

    # Style header
    headers = ["Donation ID", "Food Name", "Category", "Veg/Non-Veg", "Quantity", "Expiry Time", "Pickup Time", "Address", "Status", "Date Listed"]
    ws.append(headers)

    for donation in donations_list:
        ws.append([
            donation['id'],
            donation['food_name'],
            donation['category'],
            donation['veg_non_veg'],
            donation['quantity'],
            donation['expiry_time'],
            donation['pickup_time'],
            donation['address'],
            donation['status'],
            donation['created_at']
        ])

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer

def export_donations_to_pdf(donations_list):
    """Export a list of donations to a beautifully styled PDF using reportlab and return the bytes buffer."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    story = []

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=24,
        leading=28,
        textColor=colors.HexColor('#2c3e50'),
        spaceAfter=15
    )
    subtitle_style = ParagraphStyle(
        'DocSub',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        textColor=colors.HexColor('#7f8c8d'),
        spaceAfter=25
    )
    table_cell_style = ParagraphStyle(
        'Cell',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=10
    )
    table_header_style = ParagraphStyle(
        'HeaderCell',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=11,
        textColor=colors.white
    )

    story.append(Paragraph("FeedForward - Food Donation Report", title_style))
    story.append(Paragraph("Surplus food distribution log and performance metrics list.", subtitle_style))

    # Construct the data table
    table_data = [[
        Paragraph("ID", table_header_style),
        Paragraph("Food Item", table_header_style),
        Paragraph("Category", table_header_style),
        Paragraph("Veg/NV", table_header_style),
        Paragraph("Qty", table_header_style),
        Paragraph("Expiry", table_header_style),
        Paragraph("Status", table_header_style),
        Paragraph("Listed On", table_header_style)
    ]]

    for d in donations_list:
        table_data.append([
            Paragraph(str(d['id']), table_cell_style),
            Paragraph(d['food_name'], table_cell_style),
            Paragraph(d['category'], table_cell_style),
            Paragraph(d['veg_non_veg'], table_cell_style),
            Paragraph(d['quantity'], table_cell_style),
            Paragraph(d['expiry_time'], table_cell_style),
            Paragraph(d['status'], table_cell_style),
            Paragraph(str(d['created_at'])[:10], table_cell_style)
        ])

    # Dynamic Column Widths summing to total width of letter page minus margins (612 - 72 = 540)
    col_widths = [25, 120, 80, 45, 60, 80, 60, 70]
    
    t = Table(table_data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('TOPPADDING', (0, 0), (-1, 0), 8),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
        ('TOPPADDING', (0, 1), (-1, -1), 6),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
    ]))

    story.append(t)
    doc.build(story)
    buffer.seek(0)
    return buffer


def export_user_portfolio_to_pdf(portfolio_data):
    """Export a user's portfolio to a PDF for download."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36,
    )
    story = []
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'PortfolioTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=24,
        leading=28,
        textColor=colors.HexColor('#14532d'),
        spaceAfter=10,
    )
    section_style = ParagraphStyle(
        'SectionTitle',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=13,
        leading=16,
        textColor=colors.HexColor('#1f2937'),
        spaceBefore=12,
        spaceAfter=8,
    )
    body_style = ParagraphStyle(
        'PortfolioBody',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        textColor=colors.HexColor('#374151'),
    )

    portfolio_title = portfolio_data.get('title', 'FeedForward Portfolio')
    display_name = portfolio_data.get('display_name', 'Community Member')
    role_label = str(portfolio_data.get('role', 'member')).capitalize()
    email = portfolio_data.get('email') or 'Not provided'
    phone = portfolio_data.get('phone') or 'Not provided'
    address = portfolio_data.get('address') or 'Not provided'
    stats = portfolio_data.get('stats', {}) or {}

    story.append(Paragraph(portfolio_title, title_style))
    story.append(Paragraph(f"{display_name} • {role_label}", body_style))
    story.append(Paragraph(f"Email: {email}", body_style))
    story.append(Paragraph(f"Phone: {phone}", body_style))
    story.append(Paragraph(f"Address: {address}", body_style))
    story.append(Spacer(1, 12))

    summary_rows = []
    for key, value in stats.items():
        if key == 'avg_rating':
            label = 'Average rating'
        elif key == 'total_donations':
            label = 'Total donations'
        elif key == 'accepted':
            label = 'Accepted donations'
        elif key == 'assignments':
            label = 'Assignments'
        elif key == 'delivered':
            label = 'Delivered'
        elif key == 'certificates':
            label = 'Certificates'
        else:
            label = key.replace('_', ' ').title()
        summary_rows.append([label, str(value)])

    if summary_rows:
        story.append(Paragraph('Summary', section_style))
        story.append(Table([['Metric', 'Value']] + summary_rows, colWidths=[220, 160]))

    achievements = portfolio_data.get('achievements') or []
    if achievements:
        story.append(Paragraph('Achievements', section_style))
        for item in achievements:
            story.append(Paragraph(f"• {item}", body_style))

    activity = portfolio_data.get('activity') or []
    if activity:
        story.append(Paragraph('Recent activity', section_style))
        for item in activity[:8]:
            if isinstance(item, dict):
                value = item.get
            elif hasattr(item, 'keys'):
                value = lambda key: item[key] if key in item.keys() else None
            else:
                continue
            donation_type = value('donation_type') or ''
            donation_id = value('donation_id')
            name = value('food_name') or (f"{str(donation_type).title()} donation #{donation_id}" if donation_type and donation_id else 'Activity')
            status = value('status') or 'Recorded'
            label = value('linked_with') or value('ngo_name') or value('volunteer_name') or ''
            activity_label = f"{name} — {status}"
            if label:
                activity_label += f" • {label}"
            story.append(Paragraph(f"• {activity_label}", body_style))

    doc.build(story)
    buffer.seek(0)
    return buffer


def generate_delivery_certificate(donation_id, donation_details, donor_name, ngo_name, volunteer_name):
    """Generate a delivery certificate PDF and return its relative static path."""
    filename = f"certificate_{donation_id}.pdf"
    filepath = os.path.join(Config.UPLOAD_FOLDER, 'certificates', filename)
    certificate_number = f"{donation_id:08d}" if isinstance(donation_id, int) else str(donation_id)

    # Generate a verification QR code for the certificate.
    qr_data = f"FEEDFORWARD_CERTIFICATE_ID:{donation_id}"
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=8,
        border=3,
    )
    qr.add_data(qr_data)
    qr.make(fit=True)

    qr_image = qr.make_image(fill_color="black", back_color="white")
    qr_filename = f"certificate_qr_{donation_id}.png"
    qr_filepath = os.path.join(Config.UPLOAD_FOLDER, 'qrcodes', qr_filename)
    qr_image.save(qr_filepath)

    page_width, page_height = A4
    pdf = canvas.Canvas(filepath, pagesize=A4)
    green = colors.HexColor('#176b57')
    teal = colors.HexColor('#238a78')
    ink = colors.HexColor('#19332d')
    muted = colors.HexColor('#58716a')
    pale = colors.HexColor('#eef7f3')
    margin = 34

    def text(value):
        return escape(str(value or 'N/A'))

    def paragraph(value, x, y, width, style):
        rendered = Paragraph(value, style)
        _, height = rendered.wrap(width, page_height)
        rendered.drawOn(pdf, x, y - height)
        return height

    pdf.setFillColor(colors.white)
    pdf.rect(0, 0, page_width, page_height, fill=1, stroke=0)
    pdf.setStrokeColor(green)
    pdf.setLineWidth(2.2)
    pdf.rect(margin, margin, page_width - 2 * margin, page_height - 2 * margin, fill=0, stroke=1)
    pdf.setStrokeColor(colors.HexColor('#9acbbb'))
    pdf.setLineWidth(0.8)
    pdf.rect(margin + 7, margin + 7, page_width - 2 * (margin + 7), page_height - 2 * (margin + 7), fill=0, stroke=1)

    logo_path = os.path.join(Config.BASE_DIR, 'static', 'images', 'food-donation-icon.png')
    if os.path.exists(logo_path):
        pdf.drawImage(logo_path, page_width / 2 - 61, page_height - 94, width=34, height=34, preserveAspectRatio=True, mask='auto')
    pdf.setFont('Helvetica-Bold', 19)
    pdf.drawCentredString(page_width / 2 + 12, page_height - 87, 'FEEDFORWARD')
    pdf.setFillColor(muted)
    pdf.setFont('Helvetica', 8.5)
    pdf.drawCentredString(page_width / 2, page_height - 105, 'FOOD  |  COMMUNITY  |  IMPACT')

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('CertificateTitle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=25, leading=29, alignment=1, textColor=green)
    body_style = ParagraphStyle('CertificateBody', parent=styles['Normal'], fontName='Helvetica', fontSize=10.5, leading=15, alignment=1, textColor=ink)
    name_style = ParagraphStyle('CertificateName', parent=styles['Heading2'], fontName='Helvetica-Bold', fontSize=22, leading=27, alignment=1, textColor=ink)
    small_style = ParagraphStyle('CertificateSmall', parent=styles['Normal'], fontName='Helvetica', fontSize=8.5, leading=11, alignment=1, textColor=muted)
    label_style = ParagraphStyle('CertificateLabel', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9, leading=12, textColor=green)
    value_style = ParagraphStyle('CertificateValue', parent=styles['Normal'], fontName='Helvetica', fontSize=9, leading=12, textColor=ink)

    y = page_height - 155
    y -= paragraph('CERTIFICATE OF<br/><font size="29">DELIVERY EXCELLENCE</font>', margin + 35, y, page_width - 2 * (margin + 35), title_style) + 18
    y -= paragraph('This certificate is proudly presented to', margin + 55, y, page_width - 2 * (margin + 55), small_style) + 5
    y -= paragraph(f'<u>{text(volunteer_name)}</u>', margin + 45, y, page_width - 2 * (margin + 45), name_style) + 12
    y -= paragraph('In recognition of successfully coordinating the delivery of surplus food to beneficiaries and contributing to the reduction of food waste in the community.', margin + 60, y, page_width - 2 * (margin + 60), body_style) + 18

    box_x, box_y, box_w, box_h = margin + 42, y - 166, page_width - 2 * (margin + 42), 166
    pdf.setFillColor(pale)
    pdf.setStrokeColor(colors.HexColor('#b9ddce'))
    pdf.roundRect(box_x, box_y, box_w, box_h, 9, fill=1, stroke=1)
    pdf.setFillColor(green)
    pdf.setFont('Helvetica-Bold', 10)
    pdf.drawCentredString(page_width / 2, box_y + box_h - 20, 'DONATION DETAILS')
    details = [
        ('Donation Item', donation_details.get('food_name')),
        ('Quantity', donation_details.get('quantity')),
        ('Delivery Location', donation_details.get('address')),
        ('Donor', donor_name),
        ('NGO', ngo_name),
    ]
    detail_y = box_y + box_h - 43
    for label, value in details:
        paragraph(f'<b>{text(label)}</b>', box_x + 16, detail_y, 105, label_style)
        paragraph(text(value), box_x + 125, detail_y, box_w - 141, value_style)
        detail_y -= 23

    issued = datetime.datetime.now().strftime('%d %B %Y')
    pdf.setFillColor(ink)
    pdf.setFont('Helvetica-Bold', 9)
    pdf.drawString(margin + 45, box_y - 28, f'Issued on: {issued}')
    pdf.drawRightString(page_width - margin - 45, box_y - 28, 'Status: COMPLETED')

    qr_size = 82
    qr_x = page_width - margin - 55 - qr_size
    qr_y = margin + 78
    try:
        pdf.drawImage(qr_filepath, qr_x, qr_y, width=qr_size, height=qr_size, preserveAspectRatio=True, mask='auto')
    except Exception:
        pass
    pdf.setFillColor(muted)
    pdf.setFont('Helvetica-Bold', 7.5)
    pdf.drawCentredString(qr_x + qr_size / 2, qr_y - 12, 'SCAN TO VERIFY')
    pdf.setFont('Helvetica', 7)
    pdf.drawCentredString(qr_x + qr_size / 2, qr_y - 22, 'FeedForward delivery certificate')

    signature_y = margin + 156
    pdf.setStrokeColor(muted)
    pdf.setLineWidth(0.7)
    pdf.line(margin + 48, signature_y, margin + 155, signature_y)
    pdf.line(page_width - margin - 155, signature_y, page_width - margin - 48, signature_y)
    pdf.setFillColor(muted)
    pdf.setFont('Helvetica', 8)
    pdf.drawCentredString(margin + 101, signature_y - 14, 'FeedForward Coordinator')
    pdf.drawCentredString(page_width - margin - 101, signature_y - 14, 'NGO Representative')

    pdf.setFillColor(green)
    pdf.setFont('Helvetica-Bold', 9)
    pdf.drawCentredString(page_width / 2, margin + 31, 'Thank you for helping reduce food waste and feed people in need.')
    pdf.setFillColor(muted)
    pdf.setFont('Helvetica', 7.5)
    pdf.drawCentredString(page_width / 2, margin + 18, f'Certificate No: FF-{certificate_number}')
    pdf.showPage()
    pdf.save()
    return f"uploads/certificates/{filename}"
