"""
PDF Generator for Daily Revenue (Utarg Dzienny) Reports

Generates simplified Excel-like PDF reports with transaction details
for Polish accounting documentation.
"""

import os
import logging
from datetime import date, timedelta
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


class DailyRevenuePDFGenerator:
    """Generates PDF reports for daily revenue documentation."""
    
    def __init__(self, output_dir: str = "reports"):
        """
        Initialize the PDF generator.
        
        Args:
            output_dir: Directory to save generated PDFs
        """
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        
        # Register DejaVuSans font for Polish characters support
        self._register_fonts()
        
        # Set up styles
        self.styles = getSampleStyleSheet()
        self._setup_custom_styles()
        
        logging.info(f"PDF Generator initialized. Output directory: {output_dir}")
    
    def _register_fonts(self):
        """Register fonts with Polish and CJK (Chinese/Japanese/Korean) character support."""
        # Try Noto Sans CJK first (supports Polish + Chinese/Japanese/Korean)
        # Then fall back to DejaVuSans (Polish only)
        noto_cjk_paths = [
            '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',  # macOS - good Unicode coverage
            '/Library/Fonts/Arial Unicode.ttf',
            '/System/Library/Fonts/PingFang.ttc',  # macOS Chinese
            '/System/Library/Fonts/STHeiti Light.ttc',  # macOS Chinese
            '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
            '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
            'C:/Windows/Fonts/msyh.ttc',  # Microsoft YaHei (Chinese)
            'C:/Windows/Fonts/simsun.ttc',  # SimSun (Chinese)
        ]
        
        dejavu_paths = [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/TTF/DejaVuSans.ttf',
            '/Library/Fonts/DejaVuSans.ttf',
            '/System/Library/Fonts/Supplemental/DejaVuSans.ttf',
            'C:/Windows/Fonts/DejaVuSans.ttf',
            '/opt/homebrew/share/fonts/dejavu/DejaVuSans.ttf',
        ]
        
        font_registered = False
        
        # Try Arial Unicode first (best coverage for CJK + Polish)
        for font_path in noto_cjk_paths:
            if os.path.exists(font_path):
                try:
                    pdfmetrics.registerFont(TTFont('UniFont', font_path))
                    # Use same font for bold (these fonts often don't have bold variant)
                    pdfmetrics.registerFont(TTFont('UniFont-Bold', font_path))
                    font_registered = True
                    self.font_name = 'UniFont'
                    self.font_name_bold = 'UniFont-Bold'
                    logging.info(f"Registered Unicode font from {font_path}")
                    break
                except Exception as e:
                    logging.debug(f"Could not register font from {font_path}: {e}")
        
        # Fallback to DejaVuSans
        if not font_registered:
            for font_path in dejavu_paths:
                if os.path.exists(font_path):
                    try:
                        pdfmetrics.registerFont(TTFont('DejaVuSans', font_path))
                        bold_path = font_path.replace('.ttf', '-Bold.ttf')
                        if os.path.exists(bold_path):
                            pdfmetrics.registerFont(TTFont('DejaVuSans-Bold', bold_path))
                        else:
                            pdfmetrics.registerFont(TTFont('DejaVuSans-Bold', font_path))
                        font_registered = True
                        self.font_name = 'DejaVuSans'
                        self.font_name_bold = 'DejaVuSans-Bold'
                        logging.info(f"Registered DejaVuSans font from {font_path}")
                        break
                    except Exception as e:
                        logging.warning(f"Could not register font from {font_path}: {e}")
        
        if not font_registered:
            logging.warning("No Unicode font found. Some characters may not display correctly.")
            self.font_name = 'Helvetica'
            self.font_name_bold = 'Helvetica-Bold'
    
    def _setup_custom_styles(self):
        """Configure custom paragraph styles."""
        self.styles.add(ParagraphStyle(
            name='Title_Custom',
            fontName=self.font_name_bold,
            fontSize=14,
            spaceAfter=10,
            textColor=colors.HexColor('#1a1a2e')
        ))
        
        self.styles.add(ParagraphStyle(
            name='Subtitle',
            fontName=self.font_name,
            fontSize=10,
            spaceAfter=5,
            textColor=colors.HexColor('#4a4a6a')
        ))
        
        self.styles.add(ParagraphStyle(
            name='Normal_PL',
            fontName=self.font_name,
            fontSize=9
        ))
    
    def _sanitize_text(self, text: str) -> str:
        """
        Sanitize text for PDF output, handling characters that might not render.
        Keeps ASCII, Polish, and common Unicode while replacing problematic chars.
        """
        if not text:
            return text
        
        # Try to encode as the font supports, replace what doesn't work
        result = []
        for char in text:
            # Keep ASCII, Latin Extended (Polish, etc.), and common punctuation
            if ord(char) < 0x10000:  # Basic Multilingual Plane
                result.append(char)
            else:
                # Replace supplementary characters with placeholder
                result.append('?')
        
        return ''.join(result)
    
    def generate_daily_revenue_report(
        self,
        revenue_date: date,
        payments: list,
        nbp_rate: float,
        total_usd: float,
        total_pln: float,
        nbp_rate_date: date = None
    ) -> str:
        """
        Generate a simplified Excel-like PDF report for a single day's revenue.
        
        Args:
            revenue_date: The date of the revenue
            payments: List of unified payment dicts
            nbp_rate: NBP USD/PLN exchange rate used
            total_usd: Total revenue in USD
            total_pln: Total revenue in PLN
            nbp_rate_date: Actual date of the NBP rate (may differ from previous day due to weekends)
            
        Returns:
            Path to the generated PDF file
        """
        filename = f"utarg_dzienny_{revenue_date.strftime('%Y-%m-%d')}.pdf"
        filepath = os.path.join(self.output_dir, filename)
        
        # Use landscape for wider table with minimal margins
        doc = SimpleDocTemplate(
            filepath,
            pagesize=landscape(A4),
            rightMargin=5*mm,
            leftMargin=5*mm,
            topMargin=10*mm,
            bottomMargin=10*mm
        )
        
        elements = []
        
        # Count non-zero payments
        non_zero_count = sum(1 for p in payments if (p.get('amount') or 0) > 0)
        
        # Header info - use actual rate date if provided, otherwise calculate
        if nbp_rate_date:
            rate_date_str = nbp_rate_date.strftime('%d.%m.%Y')
        else:
            rate_date_str = (revenue_date - timedelta(days=1)).strftime('%d.%m.%Y')
        
        header_text = (
            f"UTARG DZIENNY - {revenue_date.strftime('%d.%m.%Y')}  |  "
            f"Transakcje: {non_zero_count}  |  "
            f"Kurs NBP ({rate_date_str}): {nbp_rate:.4f}  |  "
            f"Suma: ${total_usd:,.2f} = {total_pln:,.2f} PLN"
        )
        elements.append(Paragraph(header_text, self.styles['Title_Custom']))
        elements.append(Spacer(1, 5*mm))
        
        # Main transactions table - Excel-like horizontal layout
        table_data = [[
            "Lp.",
            "Typ",
            "ID/Numer",
            "Klient",
            "Email",
            "Kraj",
            "Pozycje",
            "USD",
            "PLN"
        ]]
        
        # Add payment rows (skip $0.00 amounts)
        row_idx = 0
        for payment in payments:
            amount_usd = (payment.get('amount') or 0) / 100
            if amount_usd == 0:
                continue  # Skip zero amounts
            
            row_idx += 1
            amount_pln = amount_usd * nbp_rate
            # SUB = subscription/recurring, PAY = one-time payment
            payment_type = "SUB" if payment.get('type') == 'invoice' else "PAY"
            
            # Get customer info - sanitize for PDF (remove problematic chars)
            customer_name = self._sanitize_text((payment.get('customer_name') or '-')[:30])
            customer_email = (payment.get('customer_email') or '-')[:40]
            customer_country = payment.get('customer_country') or '-'
            payment_number = payment.get('number') or '-'
            if len(payment_number) > 40:
                payment_number = payment_number[:37] + "..."
            
            # Get items description
            items = payment.get('items', [])
            items_desc = "; ".join([
                self._sanitize_text((item.get('description') or 'N/A')[:60])
                for item in items[:2]  # Max 2 items shown
            ])
            if len(items) > 2:
                items_desc += f" (+{len(items)-2})"
            if not items_desc:
                items_desc = "-"
            
            table_data.append([
                str(row_idx),
                payment_type,
                payment_number,
                customer_name,
                customer_email,
                customer_country,
                items_desc[:70],
                f"${amount_usd:.2f}",
                f"{amount_pln:.2f}"
            ])
        
        # Add totals row
        table_data.append([
            "",
            "",
            "",
            "",
            "",
            "",
            "RAZEM:",
            f"${total_usd:.2f}",
            f"{total_pln:.2f}"
        ])
        
        # Column widths for landscape A4 with minimal margins (total ~814 available)
        # Lp, Typ, ID, Klient, Email, Kraj, Pozycje, USD, PLN
        col_widths = [18, 25, 130, 115, 155, 25, 200, 55, 55]
        
        transactions_table = Table(table_data, colWidths=col_widths, repeatRows=1)
        transactions_table.setStyle(TableStyle([
            # Header styling
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), self.font_name_bold),
            ('FONTSIZE', (0, 0), (-1, 0), 7),
            ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            
            # Body styling
            ('FONTNAME', (0, 1), (-1, -1), self.font_name),
            ('FONTSIZE', (0, 1), (-1, -1), 6),
            ('ALIGN', (0, 1), (0, -1), 'CENTER'),  # Lp
            ('ALIGN', (1, 1), (1, -1), 'CENTER'),  # Type
            ('ALIGN', (5, 1), (5, -1), 'CENTER'),  # Country
            ('ALIGN', (7, 1), (8, -1), 'RIGHT'),   # USD, PLN
            
            # Alternating row colors
            ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, colors.HexColor('#f8f9fa')]),
            
            # Totals row
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#e8f4f8')),
            ('FONTNAME', (0, -1), (-1, -1), self.font_name_bold),
            ('FONTSIZE', (0, -1), (-1, -1), 7),
            ('ALIGN', (6, -1), (6, -1), 'RIGHT'),
            
            # Grid
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#dee2e6')),
            ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#2c3e50')),
            
            # Padding
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 3),
            ('RIGHTPADDING', (0, 0), (-1, -1), 3),
        ]))
        elements.append(transactions_table)
        
        # Build PDF
        doc.build(elements)
        logging.info(f"Generated PDF report: {filepath}")
        
        return filepath
    
    def generate_monthly_summary(
        self,
        year: int,
        month: int,
        daily_summaries: list[dict]
    ) -> str:
        """
        Generate a monthly summary PDF with all daily revenues.
        
        Args:
            year: Year
            month: Month
            daily_summaries: List of dicts with daily revenue data
            
        Returns:
            Path to the generated PDF file
        """
        filename = f"utarg_dzienny_podsumowanie_{year}-{month:02d}.pdf"
        filepath = os.path.join(self.output_dir, filename)
        
        doc = SimpleDocTemplate(
            filepath,
            pagesize=A4,
            rightMargin=15*mm,
            leftMargin=15*mm,
            topMargin=20*mm,
            bottomMargin=20*mm
        )
        
        elements = []
        
        # Calculate totals
        total_pln = sum(d.get('total_pln', 0) for d in daily_summaries)
        total_usd = sum(d.get('total_usd', 0) for d in daily_summaries)
        total_transactions = sum(d.get('payment_count', 0) for d in daily_summaries)
        
        # Header
        header_text = f"UTARG DZIENNY - PODSUMOWANIE {month:02d}/{year}"
        elements.append(Paragraph(header_text, self.styles['Title_Custom']))
        
        summary_text = (
            f"Dni: {len(daily_summaries)} | "
            f"Transakcje: {total_transactions} | "
            f"USD: ${total_usd:,.2f} | "
            f"PLN: {total_pln:,.2f}"
        )
        elements.append(Paragraph(summary_text, self.styles['Subtitle']))
        elements.append(Spacer(1, 10*mm))
        
        # Daily breakdown table
        daily_table_data = [["Data", "Transakcje", "Kurs NBP", "USD", "PLN"]]
        
        for daily in sorted(daily_summaries, key=lambda x: x.get('date')):
            daily_table_data.append([
                daily.get('date').strftime('%d.%m.%Y'),
                str(daily.get('payment_count', 0)),
                f"{daily.get('nbp_rate', 0):.4f}",
                f"${daily.get('total_usd', 0):,.2f}",
                f"{daily.get('total_pln', 0):,.2f}"
            ])
        
        # Total row
        daily_table_data.append([
            "RAZEM",
            str(total_transactions),
            "-",
            f"${total_usd:,.2f}",
            f"{total_pln:,.2f}"
        ])
        
        daily_table = Table(daily_table_data, colWidths=[70, 70, 70, 80, 80])
        daily_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), self.font_name_bold),
            ('FONTNAME', (0, 1), (-1, -1), self.font_name),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
            ('ALIGN', (0, 0), (0, -1), 'LEFT'),
            ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, colors.HexColor('#f8f9fa')]),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#e8f4f8')),
            ('FONTNAME', (0, -1), (-1, -1), self.font_name_bold),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#dee2e6')),
            ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#2c3e50')),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        elements.append(daily_table)
        
        # Build PDF
        doc.build(elements)
        logging.info(f"Generated monthly summary PDF: {filepath}")
        
        return filepath
