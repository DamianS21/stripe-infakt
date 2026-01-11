"""
Unified Stripe to InFakt Invoice & Daily Revenue Processor

Processes all Stripe payments for a target month, day by day:
1. Invoices WITH full customer address → Individual InFakt invoices
2. Invoices WITHOUT full address + Checkout payments → "Utarg Dzienny" (daily revenue)

For daily revenue, converts USD to PLN using previous day's NBP exchange rate
and generates PDF documentation.
"""

import os
import logging
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict
from dotenv import load_dotenv

from utils import (
    get_month_timestamps,
    timestamp_to_infakt_date,
    map_stripe_tax_rate_to_infakt_symbol,
    map_stripe_payment_method,
    get_client_details
)
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


# --- Helper Functions ---

def transform_stripe_to_infakt(stripe_invoice: dict) -> dict | None:
    """Transforms a single Stripe invoice dict into the Infakt format."""
    logging.debug(f"Transforming Stripe invoice ID: {stripe_invoice.get('id')}")
    
    if stripe_invoice.get('total', 0) == 0:
        logging.warning(f"Stripe invoice {stripe_invoice.get('id')} has zero total amount. Skipping.")
        return None
        
    infakt_services = []
    if not stripe_invoice.get('lines') or not stripe_invoice['lines'].get('data'):
        logging.warning(f"Stripe invoice {stripe_invoice.get('id')} has no line items. Skipping.")
        return None
        
    for item in stripe_invoice['lines']['data']:
        if item.get('object') != 'line_item':
            continue
        
        tax_percentage = None
        if item.get('tax_rates') and len(item['tax_rates']) > 0:
            tax_percentage = item['tax_rates'][0].get('percentage')
        
        tax_amount_total = sum(t.get('amount', 0) for t in item.get('tax_amounts', []))
            
        service = {
            "name": item.get('description', 'N/A'),
            "quantity": item.get('quantity', 1),
            "unit": item.get('price', {}).get('unit_label', 'szt.'),
            "net_price": item.get('amount'),
            "tax_price": tax_amount_total,
            "gross_price": item.get('amount', 0) + tax_amount_total,
            "unit_net_price": int(item['amount'] / item['quantity']) if item.get('quantity') else item.get('amount'),
            "flat_rate_tax_symbol": "12"
        }
        
        infakt_tax_symbol = map_stripe_tax_rate_to_infakt_symbol(tax_percentage)
        if infakt_tax_symbol:
            service["tax_symbol"] = infakt_tax_symbol
             
        infakt_services.append({k: v for k, v in service.items() if v is not None})

    if not infakt_services:
        logging.warning(f"No valid line items found for Stripe invoice {stripe_invoice.get('id')}. Skipping.")
        return None

    paid_at_ts = stripe_invoice.get('status_transitions', {}).get('paid_at')
    paid_date_str = timestamp_to_infakt_date(paid_at_ts)
    sale_date_str = timestamp_to_infakt_date(stripe_invoice.get('created')) 
    if paid_date_str:
        sale_date_str = paid_date_str

    customer_tax_ids = stripe_invoice.get('customer_tax_ids', [])
    nip = None
    for tax_id_obj in customer_tax_ids:
        value = tax_id_obj.get('value')
        if value:
            nip = value
            if tax_id_obj.get('type') == 'eu_vat':
                break

    client_data = get_client_details(stripe_invoice.get('customer'), tax_code=nip)

    payload = {
        "invoice_date": paid_date_str,
        "sale_date": sale_date_str,
        "paid_date": paid_date_str,
        "payment_date": paid_date_str,
        "currency": stripe_invoice.get('currency', 'PLN').upper(),
        "status": "paid",
        "kind": "vat",
        "payment_method": map_stripe_payment_method(stripe_invoice),
        "number": stripe_invoice.get('number'),
        "net_price": stripe_invoice.get('subtotal'),
        "tax_price": stripe_invoice.get('tax'),
        "gross_price": stripe_invoice.get('total'),
        "paid_price": stripe_invoice.get('amount_paid'),
        "left_to_pay": 0,
        "sale_type": "service",
        "services": infakt_services,
        **client_data
    }
    
    cleaned_payload = {k: v for k, v in payload.items() if v is not None}
    
    if not cleaned_payload.get("services"):
        logging.error(f"Invoice {stripe_invoice.get('id')} transformation failed: Missing services.")
        return None
    if not cleaned_payload.get("invoice_date"):
        logging.error(f"Invoice {stripe_invoice.get('id')} transformation failed: Missing invoice_date.")
        return None
        
    return cleaned_payload


def calculate_daily_revenue(payments: list, nbp_rate: float) -> dict:
    """Calculates daily revenue from a list of unified payments."""
    total_usd_cents = 0
    payment_count = 0
    
    for payment in payments:
        amount = payment.get('amount', 0)
        currency = payment.get('currency', '').upper()
        
        if currency == 'USD':
            total_usd_cents += amount
            payment_count += 1
    
    total_usd = Decimal(total_usd_cents) / 100
    nbp_rate_decimal = Decimal(str(nbp_rate))
    total_pln = (total_usd * nbp_rate_decimal).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    total_groszy = int(total_pln * 100)
    
    return {
        'total_usd_cents': total_usd_cents,
        'total_usd': float(total_usd),
        'nbp_rate': nbp_rate,
        'total_pln': float(total_pln),
        'total_groszy': total_groszy,
        'payment_count': payment_count
    }


def group_invoices_by_day(invoices: list) -> dict[date, list]:
    """Groups invoices by the day they were paid."""
    grouped = defaultdict(list)
    
    for inv in invoices:
        paid_at = inv.get('status_transitions', {}).get('paid_at')
        if paid_at:
            paid_date = datetime.fromtimestamp(paid_at, tz=timezone.utc).date()
            grouped[paid_date].append(inv)
    
    return dict(grouped)


def format_invoice_summary(stripe_id: str, infakt_payload: dict) -> str:
    """Formats invoice details for display."""
    client_name = infakt_payload.get('client_company_name') or \
                  f"{infakt_payload.get('client_first_name', '')} {infakt_payload.get('client_last_name', '')}".strip()
    gross_price_units = infakt_payload.get('gross_price', 0)
    currency = infakt_payload.get('currency', '')
    gross_price_major = f"{gross_price_units / 100:.2f}" if gross_price_units else "N/A"
    
    client_address_parts = []
    if street := infakt_payload.get('client_street'):
        client_address_parts.append(street)
    post_code = infakt_payload.get('client_post_code')
    city = infakt_payload.get('client_city')
    if post_code or city:
        code_city = f"{post_code or ''} {city or ''}".strip()
        if code_city:
            client_address_parts.append(code_city)
    if country := infakt_payload.get('client_country'):
        client_address_parts.append(country)
    client_address = ", ".join(client_address_parts) if client_address_parts else "N/A"
    
    return (
        f"    #{infakt_payload.get('number', '(auto)')}\n"
        f"    Client: {client_name}\n"
        f"    Address: {client_address}\n"
        f"    Amount: {gross_price_major} {currency}"
    )


# --- Main Execution ---
if __name__ == "__main__":
    print(f"\n{'='*70}")
    print("STRIPE → INFAKT: UNIFIED INVOICE & DAILY REVENUE PROCESSOR")
    print(f"{'='*70}")
    print(f"Target period: {TARGET_YEAR}-{TARGET_MONTH:02d}")
    print(f"InFakt environment: {'SANDBOX' if USE_SANDBOX else 'PRODUCTION'}")
    print(f"{'='*70}\n")

    if not all([STRIPE_SECRET_KEY, INFAKT_API_KEY, TARGET_YEAR, TARGET_MONTH]):
        logging.error("Missing required configuration in .env file.")
        exit(1)

    try:
        # Initialize clients
        stripe_client = StripeClient(STRIPE_SECRET_KEY)
        infakt_client = InfaktClient(INFAKT_API_KEY, sandbox=USE_SANDBOX)
        nbp_client = NBPClient()
        pdf_generator = DailyRevenuePDFGenerator(output_dir=OUTPUT_DIR)

        # Get time range
        start_ts, end_ts = get_month_timestamps(TARGET_YEAR, TARGET_MONTH)

        # Fetch all invoices
        logging.info("Fetching Stripe invoices...")
        all_invoices = stripe_client.get_paid_invoices(start_ts, end_ts)

        # Fetch checkout payments
        logging.info("Fetching Stripe checkout payments...")
        checkout_payments = stripe_client.get_checkout_payments(start_ts, end_ts)

        if not all_invoices and not checkout_payments:
            logging.info("No payments found for the specified period.")
            exit(0)

        # Separate invoices by address status
        invoices_with_address = []
        invoices_without_address = []
        
        for inv in all_invoices:
            if stripe_client._has_full_address(inv.get('customer')):
                invoices_with_address.append(inv)
            else:
                invoices_without_address.append(inv)

        logging.info(f"Invoices with full address: {len(invoices_with_address)}")
        logging.info(f"Invoices without full address: {len(invoices_without_address)}")
        logging.info(f"Checkout payments: {len(checkout_payments)}")

        # Group everything by day
        invoices_with_addr_by_day = group_invoices_by_day(invoices_with_address)
        
        # Normalize payments without address for daily revenue
        daily_revenue_payments = []
        for inv in invoices_without_address:
            daily_revenue_payments.append(stripe_client._normalize_invoice_to_payment(inv))
        for checkout in checkout_payments:
            daily_revenue_payments.append(stripe_client._normalize_checkout_to_payment(checkout))
        
        daily_revenue_by_day = stripe_client.group_unified_payments_by_day(daily_revenue_payments)

        # Get all unique days
        all_days = sorted(set(invoices_with_addr_by_day.keys()) | set(daily_revenue_by_day.keys()))

        if not all_days:
            logging.info("No payments to process.")
            exit(0)

        # Counters
        invoice_success = 0
        invoice_failure = 0
        invoice_skipped = 0
        utarg_success = 0
        utarg_failure = 0
        utarg_skipped = 0
        daily_summaries = []
        generated_pdfs = []

        # Process day by day
        for current_date in all_days:
            day_invoices = invoices_with_addr_by_day.get(current_date, [])
            day_utarg_payments = daily_revenue_by_day.get(current_date, [])

            print(f"\n{'='*70}")
            print(f"  📅  {current_date.strftime('%A, %d %B %Y')}")
            print(f"{'='*70}")
            print(f"  Invoices with address: {len(day_invoices)}")
            print(f"  Daily revenue payments: {len(day_utarg_payments)}")

            # --- Process Individual Invoices ---
            if day_invoices:
                print(f"\n  {'─'*66}")
                print("  INVOICES WITH FULL ADDRESS")
                print(f"  {'─'*66}")

                for invoice_data in day_invoices:
                    stripe_id = invoice_data.get('id')
                    infakt_payload = transform_stripe_to_infakt(invoice_data)

                    if not infakt_payload:
                        invoice_failure += 1
                        continue

                    print(f"\n  [INVOICE] {stripe_id}")
                    print(format_invoice_summary(stripe_id, infakt_payload))

                    user_confirm = input("\n  Create this invoice in InFakt? (y/n/s=skip all invoices today): ").lower()

                    if user_confirm == 's':
                        invoice_skipped += len(day_invoices) - day_invoices.index(invoice_data)
                        print("  Skipping remaining invoices for today.")
                        break
                    elif user_confirm == 'y':
                        result = infakt_client.create_invoice_async({"invoice": infakt_payload})
                        if result and result.get('invoice_task_reference_number'):
                            print(f"  ✓ Submitted. Task Ref: {result.get('invoice_task_reference_number')}")
                            invoice_success += 1
                        else:
                            print("  ✗ Failed to submit.")
                            invoice_failure += 1
                    else:
                        print("  Skipped.")
                        invoice_skipped += 1

            # --- Process Daily Revenue (Utarg Dzienny) ---
            if day_utarg_payments:
                print(f"\n  {'─'*66}")
                print("  UTARG DZIENNY (payments without full address)")
                print(f"  {'─'*66}")

                # Get NBP rate with actual date
                nbp_result = nbp_client.get_previous_day_usd_rate_with_date(current_date)
                
                if nbp_result is None:
                    print(f"  ⚠ Could not get NBP rate. Skipping daily revenue for {current_date}.")
                    utarg_failure += 1
                    continue
                
                nbp_rate, nbp_rate_date = nbp_result

                # Calculate totals
                revenue_data = calculate_daily_revenue(day_utarg_payments, nbp_rate)

                if revenue_data['total_groszy'] == 0:
                    print("  No USD revenue for this day. Skipping.")
                    utarg_skipped += 1
                    continue

                # Count by type
                inv_count = sum(1 for p in day_utarg_payments if p.get('type') == 'invoice')
                checkout_count = sum(1 for p in day_utarg_payments if p.get('type') == 'checkout')

                # Display summary
                usd_str = f"${revenue_data['total_usd']:,.2f}"
                pln_str = f"{revenue_data['total_pln']:,.2f} zł"

                print(f"\n  Payments: {revenue_data['payment_count']} ({inv_count} invoices, {checkout_count} checkout)")
                print(f"  NBP Rate: {nbp_rate:.4f} PLN/USD (from {nbp_rate_date.strftime('%d.%m.%Y')})")
                print(f"  Total:    {usd_str}  →  {pln_str}")

                # List individual payments (skip $0.00)
                print("\n  Transactions:")
                display_idx = 0
                for payment in day_utarg_payments:
                    amount = (payment.get('amount') or 0) / 100
                    if amount == 0:
                        continue
                    display_idx += 1
                    ptype = "SUB" if payment.get('type') == 'invoice' else "PAY"
                    name = (payment.get('customer_name') or 'N/A')[:30]
                    print(f"    {display_idx:2}. [{ptype}] {name:<30} ${amount:>10.2f}")

                user_confirm = input("\n  Generate PDF and create daily revenue in InFakt? (y/n): ").lower()

                if user_confirm == 'y':
                    # Generate PDF
                    pdf_path = pdf_generator.generate_daily_revenue_report(
                        revenue_date=current_date,
                        payments=day_utarg_payments,
                        nbp_rate=nbp_rate,
                        nbp_rate_date=nbp_rate_date,
                        total_usd=revenue_data['total_usd'],
                        total_pln=revenue_data['total_pln']
                    )
                    generated_pdfs.append(pdf_path)
                    print(f"  ✓ PDF saved: {pdf_path}")

                    # Submit to InFakt - using services with flat rate tax (ryczałt 12%)
                    # Number format: {day}/UD/{month}/{year}
                    utarg_number = f"{current_date.day}/UD/{current_date.month:02d}/{current_date.year}"
                    payload = {
                        "number": utarg_number,
                        "issue_date": current_date.strftime('%Y-%m-%d'),
                        "description": f"Utarg dzienny z dnia {current_date.strftime('%d-%m-%Y')}",
                        "services": [
                            {
                                "unit_price": revenue_data['total_groszy'],
                                "quantity": 1,
                                "flat_rate_tax_symbol": "12"
                            }
                        ],
                        "status": "printed"
                    }
                    result = infakt_client.create_daily_revenue_async(payload)
                    
                    if result and result.get('invoice_task_reference_number'):
                        print(f"  ✓ Submitted. Task Ref: {result.get('invoice_task_reference_number')}")
                        utarg_success += 1
                        daily_summaries.append({
                            'date': current_date,
                            'payment_count': revenue_data['payment_count'],
                            'nbp_rate': nbp_rate,
                            'total_usd': revenue_data['total_usd'],
                            'total_pln': revenue_data['total_pln']
                        })
                    else:
                        print("  ✗ Failed to submit daily revenue.")
                        utarg_failure += 1
                else:
                    print("  Skipped.")
                    utarg_skipped += 1

        # Generate monthly summary PDF
        if daily_summaries:
            monthly_pdf = pdf_generator.generate_monthly_summary(
                year=TARGET_YEAR,
                month=TARGET_MONTH,
                daily_summaries=daily_summaries
            )
            generated_pdfs.append(monthly_pdf)

        # Final Summary
        print(f"\n{'='*70}")
        print("PODSUMOWANIE / SUMMARY")
        print(f"{'='*70}")
        print("\n  INVOICES (with full address):")
        print(f"    Created:  {invoice_success}")
        print(f"    Skipped:  {invoice_skipped}")
        print(f"    Failed:   {invoice_failure}")
        print("\n  DAILY REVENUE (utarg dzienny):")
        print(f"    Created:  {utarg_success}")
        print(f"    Skipped:  {utarg_skipped}")
        print(f"    Failed:   {utarg_failure}")
        
        if generated_pdfs:
            print(f"\n  GENERATED PDFs ({len(generated_pdfs)}):")
            for pdf in generated_pdfs:
                print(f"    - {pdf}")

        print(f"\n{'='*70}")

    except Exception as e:
        logging.exception(f"An unhandled error occurred: {e}")
        exit(1)
