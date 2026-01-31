#!/usr/bin/env python3
"""
Unified Stripe to InFakt Invoice & Daily Revenue Processor

Processes all Stripe payments for a target month, day by day:
1. Invoices WITH full customer address → Individual InFakt invoices
2. Invoices WITHOUT full address + Checkout payments → "Utarg Dzienny" (daily revenue)

For daily revenue, converts USD to PLN using previous day's NBP exchange rate
and generates PDF documentation.
"""

import os
import sys
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
DEFAULT_OUTPUT_DIR = os.getenv("REPORTS_OUTPUT_DIR", "reports")


# --- Helper Functions ---

def clear_screen():
    """Clear terminal screen."""
    os.system('cls' if os.name == 'nt' else 'clear')


def print_header(title: str = "STRIPE → INFAKT"):
    """Print application header."""
    print(f"\n{'═'*70}")
    print(f"  {title}")
    print(f"{'═'*70}")


def print_menu_option(key: str, description: str, indent: int = 2):
    """Print a menu option."""
    print(f"{' '*indent}[{key}] {description}")


def get_input(prompt: str, default: str = None) -> str:
    """Get user input with optional default value."""
    if default:
        result = input(f"{prompt} [{default}]: ").strip()
        return result if result else default
    return input(f"{prompt}: ").strip()


def get_int_input(prompt: str, min_val: int = None, max_val: int = None, default: int = None) -> int:
    """Get integer input with validation."""
    while True:
        try:
            if default is not None:
                raw = input(f"{prompt} [{default}]: ").strip()
                value = int(raw) if raw else default
            else:
                value = int(input(f"{prompt}: ").strip())
            
            if min_val is not None and value < min_val:
                print(f"  ⚠ Value must be at least {min_val}")
                continue
            if max_val is not None and value > max_val:
                print(f"  ⚠ Value must be at most {max_val}")
                continue
            return value
        except ValueError:
            print("  ⚠ Please enter a valid number")


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
    """
    Calculates daily revenue from a list of unified payments.
    
    ACCOUNTING LOGIC:
    - Sums all USD payments in cents
    - Converts to PLN using NBP rate (previous business day)
    - Rounds to 2 decimal places using ROUND_HALF_UP (standard accounting rounding)
    - Returns both USD and PLN amounts for documentation
    """
    total_usd_cents = 0
    payment_count = 0
    
    for payment in payments:
        amount = payment.get('amount', 0)
        currency = payment.get('currency', '').upper()
        
        if currency == 'USD':
            total_usd_cents += amount
            payment_count += 1
    
    # Convert cents to dollars using Decimal for precision
    total_usd = Decimal(total_usd_cents) / 100
    
    # Convert NBP rate to Decimal for precise multiplication
    nbp_rate_decimal = Decimal(str(nbp_rate))
    
    # Calculate PLN with proper rounding (ROUND_HALF_UP is standard for accounting)
    total_pln = (total_usd * nbp_rate_decimal).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    
    # Convert to groszy (Polish cents) for InFakt API
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


def parse_transaction_selection(selection_input: str, max_idx: int) -> list[int]:
    """
    Parse user input for transaction selection.
    Supports: 'all', individual numbers (1,3,5), ranges (1-5), or combinations (1-3,5,7-9).
    Returns list of 0-based indices.
    """
    if selection_input.strip().lower() == 'all':
        return list(range(max_idx))
    
    if selection_input.strip().lower() in ('none', 'n', ''):
        return []
    
    selected = set()
    parts = selection_input.replace(' ', '').split(',')
    
    for part in parts:
        if not part:
            continue
        if '-' in part:
            try:
                start, end = part.split('-', 1)
                start_idx = int(start)
                end_idx = int(end)
                for i in range(start_idx, end_idx + 1):
                    if 1 <= i <= max_idx:
                        selected.add(i - 1)  # Convert to 0-based
            except ValueError:
                continue
        else:
            try:
                idx = int(part)
                if 1 <= idx <= max_idx:
                    selected.add(idx - 1)  # Convert to 0-based
            except ValueError:
                continue
    
    return sorted(selected)


# --- Menu Functions ---

def show_main_menu() -> str:
    """Display main menu and get user choice."""
    clear_screen()
    print_header("STRIPE → INFAKT PROCESSOR")
    print()
    print("  Select mode:")
    print()
    print_menu_option("1", "Process full month")
    print_menu_option("2", "Select specific day (transaction selection)")
    print()
    print_menu_option("q", "Quit")
    print()
    
    return input("  Choice: ").strip().lower()


def show_settings_menu(use_sandbox: bool, output_dir: str) -> tuple[bool, str]:
    """Display settings menu and allow modifications."""
    while True:
        clear_screen()
        print_header("SETTINGS")
        print()
        print("  Current settings:")
        print("  ─────────────────────────────────────────")
        print(f"  InFakt:     {'SANDBOX' if use_sandbox else 'PRODUCTION'}")
        print(f"  Output dir: {output_dir}")
        print()
        print_menu_option("1", "Toggle InFakt environment")
        print_menu_option("2", "Change output directory")
        print()
        print_menu_option("b", "Back")
        print()
        
        choice = input("  Choice: ").strip().lower()
        
        if choice == '1':
            use_sandbox = not use_sandbox
            print(f"\n  ✓ Changed to: {'SANDBOX' if use_sandbox else 'PRODUCTION'}")
            input("  Press Enter...")
        elif choice == '2':
            new_dir = get_input("  New directory", output_dir)
            output_dir = new_dir
            print(f"\n  ✓ Changed to: {output_dir}")
            input("  Press Enter...")
        elif choice == 'b':
            break
    
    return use_sandbox, output_dir


def select_month() -> tuple[int, int] | None:
    """Interactive month selection."""
    clear_screen()
    print_header("SELECT MONTH")
    print()
    
    current_year = datetime.now().year
    current_month = datetime.now().month
    
    print("  Enter year and month to process.")
    print()
    
    year = get_int_input("  Year", min_val=2020, max_val=2030, default=current_year)
    month = get_int_input("  Month (1-12)", min_val=1, max_val=12, default=current_month)
    
    return year, month


def select_day(year: int, month: int, available_days: list[date], invoices_by_day: dict, utarg_by_day: dict) -> date | None:
    """Interactive day selection from available days."""
    clear_screen()
    print_header(f"SELECT DAY - {year}-{month:02d}")
    print()
    
    if not available_days:
        print("  ⚠ No days with payments")
        input("\n  Press Enter...")
        return None
    
    print("  Available days with payments:")
    print()
    
    for idx, d in enumerate(available_days, 1):
        weekday = d.strftime('%A')
        inv_count = len(invoices_by_day.get(d, []))
        utarg_count = len(utarg_by_day.get(d, []))
        print(f"    {idx:2}. {d.strftime('%Y-%m-%d')} ({weekday:<9})  inv: {inv_count}, utarg: {utarg_count}")
    
    print()
    print_menu_option("b", "Back")
    print()
    
    while True:
        choice = input("  Select day number: ").strip().lower()
        
        if choice == 'b':
            return None
        
        try:
            idx = int(choice)
            if 1 <= idx <= len(available_days):
                return available_days[idx - 1]
            print(f"  ⚠ Select number 1-{len(available_days)}")
        except ValueError:
            print("  ⚠ Enter valid number")


def confirm_action(message: str) -> bool:
    """Ask for confirmation."""
    response = input(f"  {message} (y/n): ").strip().lower()
    return response in ('y', 'yes')


# --- Data Classes ---

class StripeData:
    """Container for fetched Stripe data to avoid re-fetching."""
    def __init__(self, stripe_client: StripeClient, year: int, month: int):
        self.year = year
        self.month = month
        self.stripe_client = stripe_client
        
        # Fetch data
        start_ts, end_ts = get_month_timestamps(year, month)
        
        logging.info("Fetching Stripe invoices...")
        all_invoices = stripe_client.get_paid_invoices(start_ts, end_ts)
        
        logging.info("Fetching checkout payments...")
        checkout_payments = stripe_client.get_checkout_payments(start_ts, end_ts)
        
        # Separate invoices by address status
        self.invoices_with_address = []
        self.invoices_without_address = []
        
        for inv in all_invoices:
            if stripe_client._has_full_address(inv.get('customer')):
                self.invoices_with_address.append(inv)
            else:
                self.invoices_without_address.append(inv)
        
        self.checkout_payments = checkout_payments
        
        # Group by day
        self.invoices_by_day = group_invoices_by_day(self.invoices_with_address)
        
        # Normalize payments for daily revenue
        daily_revenue_payments = []
        for inv in self.invoices_without_address:
            daily_revenue_payments.append(stripe_client._normalize_invoice_to_payment(inv))
        for checkout in checkout_payments:
            daily_revenue_payments.append(stripe_client._normalize_checkout_to_payment(checkout))
        
        self.utarg_by_day = stripe_client.group_unified_payments_by_day(daily_revenue_payments)
        
        # Get all unique days
        self.all_days = sorted(set(self.invoices_by_day.keys()) | set(self.utarg_by_day.keys()))
    
    def has_data(self) -> bool:
        return bool(self.invoices_with_address or self.invoices_without_address or self.checkout_payments)


# --- Processing Functions ---

def process_day(
    current_date: date,
    day_invoices: list,
    day_utarg_payments: list,
    infakt_client: InfaktClient,
    nbp_client: NBPClient,
    pdf_generator: DailyRevenuePDFGenerator,
    single_day_mode: bool,
    counters: dict,
    daily_summaries: list,
    generated_pdfs: list
) -> None:
    """
    Process a single day's invoices and daily revenue.
    
    ACCOUNTING NOTES:
    - Individual invoices (with full address) are created as VAT invoices in InFakt
    - Daily revenue (utarg dzienny) uses flat rate tax (ryczałt 12%)
    - USD to PLN conversion uses NBP rate from previous business day
    - All amounts are stored in groszy (Polish cents) for InFakt API
    """
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
                counters['invoice_failure'] += 1
                continue

            print(f"\n  [INVOICE] {stripe_id}")
            print(format_invoice_summary(stripe_id, infakt_payload))

            user_confirm = input("\n  Create invoice in InFakt? (y/n/s=skip all): ").lower()

            if user_confirm == 's':
                counters['invoice_skipped'] += len(day_invoices) - day_invoices.index(invoice_data)
                print("  Skipping remaining invoices.")
                break
            elif user_confirm == 'y':
                result = infakt_client.create_invoice_async({"invoice": infakt_payload})
                if result and result.get('invoice_task_reference_number'):
                    print(f"  ✓ Submitted. Task Ref: {result.get('invoice_task_reference_number')}")
                    counters['invoice_success'] += 1
                else:
                    print("  ✗ Failed to submit.")
                    counters['invoice_failure'] += 1
            else:
                print("  Skipped.")
                counters['invoice_skipped'] += 1

    # --- Process Daily Revenue (Utarg Dzienny) ---
    if day_utarg_payments:
        print(f"\n  {'─'*66}")
        print("  DAILY REVENUE (payments without full address)")
        print(f"  {'─'*66}")

        # Get NBP rate with actual date
        # ACCOUNTING: Must use previous business day's NBP rate for USD/PLN conversion
        nbp_result = nbp_client.get_previous_day_usd_rate_with_date(current_date)
        
        if nbp_result is None:
            print("  ⚠ Could not get NBP rate.")
            print(f"     Skipping daily revenue for {current_date}.")
            counters['utarg_failure'] += 1
            return
        
        nbp_rate, nbp_rate_date = nbp_result

        # Filter to non-zero payments for display
        nonzero_payments = [p for p in day_utarg_payments if (p.get('amount') or 0) > 0]
        
        # Display NBP rate info (important for accounting documentation)
        print(f"\n  NBP Rate: {nbp_rate:.4f} PLN/USD (from {nbp_rate_date.strftime('%Y-%m-%d')})")
        
        # List individual payments for selection
        print(f"\n  Available transactions ({len(nonzero_payments)}):")
        for idx, payment in enumerate(nonzero_payments, 1):
            amount = (payment.get('amount') or 0) / 100
            ptype = "SUB" if payment.get('type') == 'invoice' else "PAY"
            name = (payment.get('customer_name') or 'N/A')[:30]
            print(f"    {idx:2}. [{ptype}] {name:<30} ${amount:>10.2f}")

        # In single day mode, allow transaction selection
        if single_day_mode:
            print("\n  ┌─────────────────────────────────────────────────────────────────┐")
            print("  │  TRANSACTION SELECTION MODE                                     │")
            print("  │  Enter transaction numbers to include in daily revenue.        │")
            print("  │  Examples: 'all', '1,3,5', '1-5', '1-3,7,9', 'none'            │")
            print("  └─────────────────────────────────────────────────────────────────┘")
            
            selection_input = input("\n  Select transactions: ").strip()
            selected_indices = parse_transaction_selection(selection_input, len(nonzero_payments))
            
            if not selected_indices:
                print("  No transactions selected. Skipping.")
                counters['utarg_skipped'] += 1
                return
            
            # Filter to selected payments
            selected_payments = [nonzero_payments[i] for i in selected_indices]
            
            print(f"\n  Selected {len(selected_payments)} transaction(s):")
            for idx in selected_indices:
                payment = nonzero_payments[idx]
                amount = (payment.get('amount') or 0) / 100
                ptype = "SUB" if payment.get('type') == 'invoice' else "PAY"
                name = (payment.get('customer_name') or 'N/A')[:30]
                print(f"    ✓ [{ptype}] {name:<30} ${amount:>10.2f}")
        else:
            # Full month mode - include all non-zero payments
            selected_payments = nonzero_payments

        # Calculate totals for selected payments
        # ACCOUNTING: This is where USD to PLN conversion happens
        revenue_data = calculate_daily_revenue(selected_payments, nbp_rate)

        if revenue_data['total_groszy'] == 0:
            print("  No USD revenue. Skipping.")
            counters['utarg_skipped'] += 1
            return

        # Count by type for summary
        inv_count = sum(1 for p in selected_payments if p.get('type') == 'invoice')
        checkout_count = sum(1 for p in selected_payments if p.get('type') == 'checkout')

        # Display summary with both currencies (required for accounting documentation)
        usd_str = f"${revenue_data['total_usd']:,.2f}"
        pln_str = f"{revenue_data['total_pln']:,.2f} PLN"

        print(f"\n  {'─'*66}")
        print("  SUMMARY")
        print(f"  {'─'*66}")
        print(f"  Payment count: {revenue_data['payment_count']} ({inv_count} subs, {checkout_count} checkout)")
        print(f"  Total USD:     {usd_str}")
        print(f"  NBP rate:      {nbp_rate:.4f} PLN/USD (from {nbp_rate_date.strftime('%Y-%m-%d')})")
        print(f"  Total PLN:     {pln_str}")
        print(f"  {'─'*66}")

        user_confirm = input("\n  Generate PDF and create in InFakt? (y/n): ").lower()

        if user_confirm == 'y':
            # Generate PDF documentation
            pdf_path = pdf_generator.generate_daily_revenue_report(
                revenue_date=current_date,
                payments=selected_payments,
                nbp_rate=nbp_rate,
                nbp_rate_date=nbp_rate_date,
                total_usd=revenue_data['total_usd'],
                total_pln=revenue_data['total_pln']
            )
            generated_pdfs.append(pdf_path)
            print(f"  ✓ PDF saved: {pdf_path}")

            # Submit to InFakt - using services with flat rate tax (ryczałt 12%)
            # ACCOUNTING: Number format: {day}/UD/{month}/{year}
            utarg_number = f"{current_date.day}/UD/{current_date.month:02d}/{current_date.year}"
            payload = {
                "number": utarg_number,
                "issue_date": current_date.strftime('%Y-%m-%d'),
                "description": f"Utarg dzienny z dnia {current_date.strftime('%d-%m-%Y')}",
                "services": [
                    {
                        "unit_price": revenue_data['total_groszy'],  # Amount in groszy (Polish cents)
                        "quantity": 1,
                        "flat_rate_tax_symbol": "12"  # Ryczałt 12%
                    }
                ],
                "status": "printed"
            }
            result = infakt_client.create_daily_revenue_async(payload)
            
            if result and result.get('invoice_task_reference_number'):
                print(f"  ✓ Submitted. Task Ref: {result.get('invoice_task_reference_number')}")
                counters['utarg_success'] += 1
                daily_summaries.append({
                    'date': current_date,
                    'payment_count': revenue_data['payment_count'],
                    'nbp_rate': nbp_rate,
                    'total_usd': revenue_data['total_usd'],
                    'total_pln': revenue_data['total_pln']
                })
            else:
                print("  ✗ Failed to submit daily revenue.")
                counters['utarg_failure'] += 1
        else:
            print("  Skipped.")
            counters['utarg_skipped'] += 1


def run_processing(
    stripe_data: StripeData,
    target_day: date | None,
    use_sandbox: bool,
    output_dir: str
) -> int:
    """
    Main processing function for both full month and single day modes.
    Uses pre-fetched StripeData to avoid re-fetching.
    Returns exit code (0 = success).
    """
    single_day_mode = target_day is not None
    year = stripe_data.year
    month = stripe_data.month
    
    # Print processing header
    clear_screen()
    print_header("PROCESSING")
    print()
    if single_day_mode:
        print("  Mode: SINGLE DAY")
        print(f"  Date: {target_day}")
    else:
        print("  Mode: FULL MONTH")
        print(f"  Period: {year}-{month:02d}")
    print(f"  InFakt: {'SANDBOX' if use_sandbox else 'PRODUCTION'}")
    print(f"  Output: {output_dir}")
    print()
    print("  Data summary:")
    print(f"    - Invoices with address: {len(stripe_data.invoices_with_address)}")
    print(f"    - Invoices w/o address: {len(stripe_data.invoices_without_address)}")
    print(f"    - Checkout payments: {len(stripe_data.checkout_payments)}")
    print()
    
    if not confirm_action("Continue?"):
        return 0

    try:
        # Initialize clients (Stripe data already fetched)
        infakt_client = InfaktClient(INFAKT_API_KEY, sandbox=use_sandbox)
        nbp_client = NBPClient()
        pdf_generator = DailyRevenuePDFGenerator(output_dir=output_dir)

        # Determine days to process
        if single_day_mode:
            if target_day in stripe_data.all_days:
                days_to_process = [target_day]
            else:
                print(f"\n  ⚠ No payments for {target_day}.")
                input("\n  Press Enter...")
                return 0
        else:
            days_to_process = stripe_data.all_days

        if not days_to_process:
            print("\n  ⚠ No days to process.")
            input("\n  Press Enter...")
            return 0

        # Initialize counters
        counters = {
            'invoice_success': 0,
            'invoice_failure': 0,
            'invoice_skipped': 0,
            'utarg_success': 0,
            'utarg_failure': 0,
            'utarg_skipped': 0
        }
        daily_summaries = []
        generated_pdfs = []

        # Process day by day
        for current_date in days_to_process:
            day_invoices = stripe_data.invoices_by_day.get(current_date, [])
            day_utarg_payments = stripe_data.utarg_by_day.get(current_date, [])
            
            process_day(
                current_date=current_date,
                day_invoices=day_invoices,
                day_utarg_payments=day_utarg_payments,
                infakt_client=infakt_client,
                nbp_client=nbp_client,
                pdf_generator=pdf_generator,
                single_day_mode=single_day_mode,
                counters=counters,
                daily_summaries=daily_summaries,
                generated_pdfs=generated_pdfs
            )

        # Generate monthly summary PDF (only for full month mode with successful submissions)
        if daily_summaries and not single_day_mode:
            monthly_pdf = pdf_generator.generate_monthly_summary(
                year=year,
                month=month,
                daily_summaries=daily_summaries
            )
            generated_pdfs.append(monthly_pdf)

        # Final Summary
        print(f"\n{'='*70}")
        print("  SUMMARY")
        print(f"{'='*70}")
        print("\n  INVOICES (with full address):")
        print(f"    Created:  {counters['invoice_success']}")
        print(f"    Skipped:  {counters['invoice_skipped']}")
        print(f"    Failed:   {counters['invoice_failure']}")
        print("\n  DAILY REVENUE:")
        print(f"    Created:  {counters['utarg_success']}")
        print(f"    Skipped:  {counters['utarg_skipped']}")
        print(f"    Failed:   {counters['utarg_failure']}")
        
        if generated_pdfs:
            print(f"\n  GENERATED PDFs ({len(generated_pdfs)}):")
            for pdf in generated_pdfs:
                print(f"    - {pdf}")

        print(f"\n{'='*70}")
        input("\n  Press Enter to continue...")
        return 0

    except KeyboardInterrupt:
        print("\n\n  Interrupted by user.")
        return 130
    except Exception as e:
        logging.exception(f"An error occurred: {e}")
        input("\n  Press Enter...")
        return 1


# --- Main Application ---

def main() -> int:
    """Main application entry point with interactive menu."""
    
    # Validate credentials on startup
    if not STRIPE_SECRET_KEY:
        print("\n  ⚠ STRIPE_SECRET_KEY not set in .env file")
        return 1
    if not INFAKT_API_KEY:
        print("\n  ⚠ INFAKT_API_KEY not set in .env file")
        return 1
    
    # Default settings
    use_sandbox = os.getenv("INFAKT_SANDBOX", "false").lower() == "true"
    output_dir = DEFAULT_OUTPUT_DIR
    
    # Cache for Stripe data
    stripe_data_cache: StripeData | None = None
    
    while True:
        choice = show_main_menu()
        
        if choice == '1':
            # Full month processing
            result = select_month()
            if result:
                year, month = result
                
                # Fetch data if not cached or different month
                if stripe_data_cache is None or stripe_data_cache.year != year or stripe_data_cache.month != month:
                    clear_screen()
                    print_header(f"FETCHING DATA: {year}-{month:02d}")
                    print("\n  Please wait...")
                    
                    try:
                        stripe_client = StripeClient(STRIPE_SECRET_KEY)
                        stripe_data_cache = StripeData(stripe_client, year, month)
                    except Exception as e:
                        logging.exception(f"Error fetching data: {e}")
                        input("\n  Press Enter...")
                        continue
                
                if not stripe_data_cache.has_data():
                    print("\n  ⚠ No payments found for this month.")
                    input("\n  Press Enter...")
                    continue
                
                # Show settings confirmation
                clear_screen()
                print_header(f"PROCESS MONTH: {year}-{month:02d}")
                print()
                print(f"  InFakt: {'SANDBOX' if use_sandbox else 'PRODUCTION'}")
                print(f"  Output: {output_dir}")
                print()
                print(f"  Found {len(stripe_data_cache.all_days)} days with payments")
                print()
                print_menu_option("p", "Start processing")
                print_menu_option("s", "Change settings")
                print_menu_option("b", "Back")
                print()
                
                sub_choice = input("  Choice: ").strip().lower()
                
                if sub_choice == 's':
                    use_sandbox, output_dir = show_settings_menu(use_sandbox, output_dir)
                elif sub_choice == 'p':
                    run_processing(stripe_data_cache, None, use_sandbox, output_dir)
        
        elif choice == '2':
            # Single day with transaction selection
            result = select_month()
            if result:
                year, month = result
                
                # Fetch data if not cached or different month
                if stripe_data_cache is None or stripe_data_cache.year != year or stripe_data_cache.month != month:
                    clear_screen()
                    print_header(f"FETCHING DATA: {year}-{month:02d}")
                    print("\n  Please wait...")
                    
                    try:
                        stripe_client = StripeClient(STRIPE_SECRET_KEY)
                        stripe_data_cache = StripeData(stripe_client, year, month)
                    except Exception as e:
                        logging.exception(f"Error fetching data: {e}")
                        input("\n  Press Enter...")
                        continue
                
                if not stripe_data_cache.all_days:
                    print("\n  ⚠ No payments this month.")
                    input("\n  Press Enter...")
                    continue
                
                # Let user select day
                selected_day = select_day(
                    year, month, 
                    stripe_data_cache.all_days,
                    stripe_data_cache.invoices_by_day,
                    stripe_data_cache.utarg_by_day
                )
                
                if selected_day:
                    # Show settings confirmation
                    clear_screen()
                    print_header(f"PROCESS DAY: {selected_day}")
                    print()
                    print(f"  InFakt: {'SANDBOX' if use_sandbox else 'PRODUCTION'}")
                    print(f"  Output: {output_dir}")
                    print()
                    
                    # Show day summary
                    inv_count = len(stripe_data_cache.invoices_by_day.get(selected_day, []))
                    utarg_count = len(stripe_data_cache.utarg_by_day.get(selected_day, []))
                    print(f"  Invoices with address: {inv_count}")
                    print(f"  Daily revenue payments: {utarg_count}")
                    print()
                    
                    print_menu_option("p", "Start processing")
                    print_menu_option("s", "Change settings")
                    print_menu_option("b", "Back")
                    print()
                    
                    sub_choice = input("  Choice: ").strip().lower()
                    
                    if sub_choice == 's':
                        use_sandbox, output_dir = show_settings_menu(use_sandbox, output_dir)
                    elif sub_choice == 'p':
                        run_processing(stripe_data_cache, selected_day, use_sandbox, output_dir)
        
        elif choice == 'q':
            clear_screen()
            print("\n  Goodbye!\n")
            return 0
        
        elif choice == 's':
            use_sandbox, output_dir = show_settings_menu(use_sandbox, output_dir)


# --- Entry Point ---
if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n  Interrupted.\n")
        sys.exit(130)
