"""
Daily Revenue (Utarg Dzienny) Generator

This script calculates daily revenue from Stripe payments:
1. Invoices without full customer addresses
2. Checkout session payments (one-time purchases)

Converts USD amounts to PLN using the previous day's NBP exchange rate,
generates PDF reports with transaction details, and creates "utarg dzienny" 
entries in InFakt.
"""

import os
import logging
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from dotenv import load_dotenv

from utils import get_month_timestamps
from stripe_client import StripeClient
from infakt_client import InfaktClient
from nbp_client import NBPClient
from pdf_generator import DailyRevenuePDFGenerator

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
load_dotenv(override=True)

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
INFAKT_API_KEY = os.getenv("INFAKT_API_KEY")
USE_SANDBOX = os.getenv("INFAKT_SANDBOX", "false").lower() == "true"
OUTPUT_DIR = os.getenv("REPORTS_OUTPUT_DIR", "reports")

try:
    TARGET_YEAR = int(os.getenv("TARGET_YEAR"))
    TARGET_MONTH = int(os.getenv("TARGET_MONTH"))
except (TypeError, ValueError):
    logging.error("TARGET_YEAR and TARGET_MONTH must be set in .env file and be valid integers.")
    exit(1)


def calculate_daily_revenue(payments: list, nbp_rate: float) -> dict:
    """
    Calculates daily revenue from a list of unified payments.
    
    Args:
        payments: List of unified payment dicts
        nbp_rate: USD to PLN exchange rate
        
    Returns:
        Dictionary with revenue details
    """
    total_usd_cents = 0
    payment_count = 0
    
    for payment in payments:
        # Get the total amount (in cents for USD)
        amount = payment.get('amount', 0)
        currency = payment.get('currency', '').upper()
        
        if currency == 'USD':
            total_usd_cents += amount
            payment_count += 1
        elif currency == 'PLN':
            # If already in PLN, skip for USD-based calculation
            logging.warning(f"Payment {payment.get('id')} is in PLN, not USD. Skipping for USD-based daily revenue.")
        else:
            logging.warning(f"Payment {payment.get('id')} has unsupported currency: {currency}")
    
    # Convert cents to dollars
    total_usd = Decimal(total_usd_cents) / 100
    
    # Convert to PLN using NBP rate
    nbp_rate_decimal = Decimal(str(nbp_rate))
    total_pln = (total_usd * nbp_rate_decimal).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    
    # Convert PLN to groszy (smallest unit) for InFakt API
    total_groszy = int(total_pln * 100)
    
    return {
        'total_usd_cents': total_usd_cents,
        'total_usd': float(total_usd),
        'nbp_rate': nbp_rate,
        'total_pln': float(total_pln),
        'total_groszy': total_groszy,
        'payment_count': payment_count
    }


def create_daily_revenue_payload(revenue_date: date, total_groszy: int) -> dict:
    """
    Creates the payload for InFakt daily revenue API.
    
    Args:
        revenue_date: The date of the revenue
        total_groszy: Total revenue in groszy (Polish cents)
        
    Returns:
        Dictionary payload for InFakt API
    """
    # Number format: {day}/UD/{month}/{year}
    utarg_number = f"{revenue_date.day}/UD/{revenue_date.month:02d}/{revenue_date.year}"
    return {
        "number": utarg_number,
        "issue_date": revenue_date.strftime('%Y-%m-%d'),
        "description": f"Utarg dzienny z dnia {revenue_date.strftime('%d-%m-%Y')}",
        "services": [
            {
                "unit_price": total_groszy,
                "quantity": 1,
                "flat_rate_tax_symbol": "12"
            }
        ],
        "status": "printed",
    }


def main():
    """Main execution function for daily revenue processing."""
    logging.info("=" * 70)
    logging.info("Starting Daily Revenue (Utarg Dzienny) processing...")
    logging.info(f"Target period: {TARGET_YEAR}-{TARGET_MONTH:02d}")
    logging.info(f"Using InFakt {'sandbox' if USE_SANDBOX else 'production'} environment")
    logging.info(f"PDF reports will be saved to: {OUTPUT_DIR}")
    logging.info("=" * 70)

    if not all([STRIPE_SECRET_KEY, INFAKT_API_KEY, TARGET_YEAR, TARGET_MONTH]):
        logging.error("Missing required configuration. Please set STRIPE_SECRET_KEY, INFAKT_API_KEY, TARGET_YEAR, TARGET_MONTH in .env file.")
        exit(1)

    try:
        # Initialize clients
        stripe_client = StripeClient(STRIPE_SECRET_KEY)
        infakt_client = InfaktClient(INFAKT_API_KEY, sandbox=USE_SANDBOX)
        nbp_client = NBPClient()
        pdf_generator = DailyRevenuePDFGenerator(output_dir=OUTPUT_DIR)

        # Get time range for target month
        start_ts, end_ts = get_month_timestamps(TARGET_YEAR, TARGET_MONTH)

        # Fetch ALL payments eligible for daily revenue (invoices without address + checkout payments)
        logging.info("Fetching Stripe payments for daily revenue...")
        logging.info("  - Invoices without full customer address")
        logging.info("  - Checkout session payments (one-time purchases)")
        
        all_payments = stripe_client.get_all_payments_for_daily_revenue(start_ts, end_ts)

        if not all_payments:
            logging.info("No payments found for daily revenue in the specified period.")
            exit(0)

        # Count by type
        invoice_count = sum(1 for p in all_payments if p.get('type') == 'invoice')
        checkout_count = sum(1 for p in all_payments if p.get('type') == 'checkout')
        logging.info(f"Found {len(all_payments)} total payments:")
        logging.info(f"  - {invoice_count} invoices without full address")
        logging.info(f"  - {checkout_count} checkout session payments")

        # Group payments by day
        payments_by_day = stripe_client.group_unified_payments_by_day(all_payments)
        logging.info(f"Payments grouped into {len(payments_by_day)} days")

        # Process each day
        success_count = 0
        failure_count = 0
        skipped_count = 0
        daily_summaries = []  # For monthly summary PDF
        generated_pdfs = []

        for revenue_date in sorted(payments_by_day.keys()):
            day_payments = payments_by_day[revenue_date]
            
            # Count types for this day
            day_invoices = sum(1 for p in day_payments if p.get('type') == 'invoice')
            day_checkouts = sum(1 for p in day_payments if p.get('type') == 'checkout')
            
            logging.info(f"\n{'='*60}")
            logging.info(f"Processing {revenue_date} ({len(day_payments)} payments: {day_invoices} invoices, {day_checkouts} checkout)")
            logging.info("=" * 60)

            # Get previous day's NBP rate with actual date
            nbp_result = nbp_client.get_previous_day_usd_rate_with_date(revenue_date)
            
            if nbp_result is None:
                logging.error(f"Could not get NBP rate for {revenue_date}. Skipping this day.")
                failure_count += 1
                continue
            
            nbp_rate, nbp_rate_date = nbp_result
            logging.info(f"NBP USD rate: {nbp_rate} (from {nbp_rate_date})")

            # Calculate daily revenue
            revenue_data = calculate_daily_revenue(day_payments, nbp_rate)
            
            if revenue_data['total_groszy'] == 0:
                logging.info(f"No USD revenue for {revenue_date}. Skipping.")
                skipped_count += 1
                continue

            # Display summary
            usd_str = f"${revenue_data['total_usd']:,.2f}"
            pln_str = f"{revenue_data['total_pln']:,.2f} zł"
            title_str = f"UTARG DZIENNY - {revenue_date.strftime('%d.%m.%Y')}"
            
            print(f"\n┌{'─'*58}┐")
            print(f"│ {title_str:^56} │")
            print(f"├{'─'*58}┤")
            print(f"│ {'Transakcje:':<30} {revenue_data['payment_count']:>25} │")
            print(f"│ {'  - Faktury bez adresu:':<30} {day_invoices:>25} │")
            print(f"│ {'  - Checkout payments:':<30} {day_checkouts:>25} │")
            print(f"│ {'Suma USD:':<30} {usd_str:>25} │")
            print(f"│ {'Kurs NBP:':<30} {revenue_data['nbp_rate']:>25.4f} │")
            print(f"│ {'Suma PLN:':<30} {pln_str:>25} │")
            print(f"└{'─'*58}┘")

            # Show transaction details (skip $0.00)
            print("\n  Szczegóły transakcji:")
            display_idx = 0
            for payment in day_payments:
                amount = (payment.get('amount') or 0) / 100
                if amount == 0:
                    continue
                display_idx += 1
                ptype = "SUB" if payment.get('type') == 'invoice' else "PAY"
                name = (payment.get('customer_name') or 'N/A')[:35]
                print(f"    {display_idx:2}. [{ptype}] {name:<35} ${amount:>10.2f}")

            # User confirmation
            user_confirm = input("\nGenerate PDF and create daily revenue in InFakt? (y/n): ").lower()

            if user_confirm == 'y':
                # Generate PDF report
                logging.info(f"Generating PDF report for {revenue_date}...")
                pdf_path = pdf_generator.generate_daily_revenue_report(
                    revenue_date=revenue_date,
                    payments=day_payments,
                    nbp_rate=nbp_rate,
                    nbp_rate_date=nbp_rate_date,
                    total_usd=revenue_data['total_usd'],
                    total_pln=revenue_data['total_pln']
                )
                generated_pdfs.append(pdf_path)
                print(f"  ✓ PDF saved: {pdf_path}")

                # Create payload and submit to InFakt
                payload = create_daily_revenue_payload(revenue_date, revenue_data['total_groszy'])
                
                logging.info(f"Submitting daily revenue to InFakt for {revenue_date}...")
                result = infakt_client.create_daily_revenue_async(payload)
                
                if result and result.get('daily_revenue_task_reference_number'):
                    logging.info(f"✓ Successfully submitted daily revenue for {revenue_date}. Task Ref: {result.get('daily_revenue_task_reference_number')}")
                    success_count += 1
                    
                    # Add to summaries for monthly report
                    daily_summaries.append({
                        'date': revenue_date,
                        'payment_count': revenue_data['payment_count'],
                        'nbp_rate': nbp_rate,
                        'total_usd': revenue_data['total_usd'],
                        'total_pln': revenue_data['total_pln']
                    })
                else:
                    logging.error(f"✗ Failed to submit daily revenue for {revenue_date}")
                    failure_count += 1
            else:
                logging.info(f"Skipped daily revenue for {revenue_date} (user declined)")
                skipped_count += 1

        # Generate monthly summary PDF if we have any successful days
        if daily_summaries:
            logging.info("\nGenerating monthly summary PDF...")
            monthly_pdf = pdf_generator.generate_monthly_summary(
                year=TARGET_YEAR,
                month=TARGET_MONTH,
                daily_summaries=daily_summaries
            )
            generated_pdfs.append(monthly_pdf)
            print(f"  ✓ Monthly summary PDF saved: {monthly_pdf}")

        # Final summary
        print(f"\n{'='*70}")
        print("PODSUMOWANIE PRZETWARZANIA")
        print("=" * 70)
        print(f"  Dni z płatnościami:            {len(payments_by_day)}")
        print(f"  Utworzono w InFakt:            {success_count}")
        print(f"  Pominięto:                     {skipped_count}")
        print(f"  Błędy:                         {failure_count}")
        print(f"  Wygenerowano PDF-ów:           {len(generated_pdfs)}")
        
        if generated_pdfs:
            print("\n  Pliki PDF:")
            for pdf in generated_pdfs:
                print(f"    - {pdf}")
        
        print("=" * 70)

    except Exception as e:
        logging.exception(f"An unhandled error occurred: {e}")
        exit(1)


if __name__ == "__main__":
    main()
