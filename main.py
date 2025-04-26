import os
import logging
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

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
load_dotenv() # Load variables from .env file

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
INFAKT_API_KEY = os.getenv("INFAKT_API_KEY")

# Get target month and year (ensure they are integers)
try:
    TARGET_YEAR = int(os.getenv("TARGET_YEAR"))
    TARGET_MONTH = int(os.getenv("TARGET_MONTH"))
except (TypeError, ValueError):
     logging.error("TARGET_YEAR and TARGET_MONTH must be set in .env file and be valid integers.")
     exit(1)

# --- Helper Function for Transformation ---

def transform_stripe_to_infakt(stripe_invoice: dict) -> dict | None:
    """Transforms a single Stripe invoice dict into the Infakt format."""
    logging.debug(f"Transforming Stripe invoice ID: {stripe_invoice.get('id')}")
    
    # Skip invoices with zero total (0.00)
    if stripe_invoice.get('total', 0) == 0:
        logging.warning(f"Stripe invoice {stripe_invoice.get('id')} has zero total amount. Skipping.")
        return None
        
    infakt_services = []
    if not stripe_invoice.get('lines') or not stripe_invoice['lines'].get('data'):
        logging.warning(f"Stripe invoice {stripe_invoice.get('id')} has no line items. Skipping.")
        return None
        
    for item in stripe_invoice['lines']['data']:
        # Only process line_item objects (include subscription lines and other types)
        if item.get('object') != 'line_item':
            continue
        
        # Get tax rate percentage from the expanded tax_rates array
        tax_percentage = None
        if item.get('tax_rates') and len(item['tax_rates']) > 0:
            # Assuming only one tax rate per line item for simplicity
            tax_percentage = item['tax_rates'][0].get('percentage')
        
        # Calculate tax amount (sum if multiple tax amounts present)
        tax_amount_total = sum(t.get('amount', 0) for t in item.get('tax_amounts', []))
            
        service = {
            "name": item.get('description', 'N/A'),
            "quantity": item.get('quantity', 1),
            "unit": item.get('price', {}).get('unit_label', 'szt.'), # Attempt to get unit label, default 'szt.'
            # Prices: Assuming Stripe amounts are in smallest unit (cents/groszy)
            # Infakt also expects groszy, so direct mapping should work for PLN/EUR/USD etc.
            "net_price": item.get('amount'),
            "tax_price": tax_amount_total,
            "gross_price": item.get('amount', 0) + tax_amount_total,
            # unit_net_price = net_price / quantity (handle quantity=0)
            "unit_net_price": int(item['amount'] / item['quantity']) if item.get('quantity') else item.get('amount'),
            "flat_rate_tax_symbol": "12"

            # "tax_symbol": map_stripe_tax_rate_to_infakt_symbol(tax_percentage), # Mapping function needs care
        }
        # Map tax symbol carefully - ensure the mapping function covers all your cases
        infakt_tax_symbol = map_stripe_tax_rate_to_infakt_symbol(tax_percentage)
        if infakt_tax_symbol:
             service["tax_symbol"] = infakt_tax_symbol
        else:
             # Decide how to handle missing/unmappable tax symbols (e.g., default, skip, error)
             logging.warning(f"Could not map tax rate for line item in invoice {stripe_invoice.get('id')}. Tax percentage was {tax_percentage}")
             # Example: Defaulting to 'np.' (nie podlega) or skipping tax_symbol
             # service["tax_symbol"] = 'np.'
             pass # Or handle as needed
        
             
        # Remove None values from service dict
        infakt_services.append({k: v for k, v in service.items() if v is not None})

    if not infakt_services:
         logging.warning(f"No valid line items found after processing for Stripe invoice {stripe_invoice.get('id')}. Skipping.")
         return None

    # Dates - use paid_at for invoice_date and paid_date
    paid_at_ts = stripe_invoice.get('status_transitions', {}).get('paid_at')
    paid_date_str = timestamp_to_infakt_date(paid_at_ts)
    
    # Use created date for sale_date as a fallback if needed, or paid_at
    sale_date_str = timestamp_to_infakt_date(stripe_invoice.get('created')) 
    if paid_date_str: # Prefer paid_date for sale_date if available for paid invoices
        sale_date_str = paid_date_str

    # Extract NIP/Tax Code from invoice data first
    customer_tax_ids = stripe_invoice.get('customer_tax_ids', [])
    nip = None
    # Prioritize specific types if needed, otherwise take the first value found
    # Add more 'elif' conditions for specific tax types like 'pl_vat', 'gb_vat' etc. if needed
    for tax_id_obj in customer_tax_ids:
        value = tax_id_obj.get('value')
        if value:
            nip = value
            # Example prioritization: prefer 'eu_vat' if available
            if tax_id_obj.get('type') == 'eu_vat':
                break 
            # Add elif for 'pl_vat', 'gb_vat', etc.
            # elif tax_id_obj.get('type') == 'pl_vat':
            #    break
                
    if nip:
        logging.debug(f"Found Tax ID (NIP) on invoice {stripe_invoice.get('id')}: {nip}")
    else:
        logging.debug(f"No Tax ID found on invoice {stripe_invoice.get('id')}")

    # Client details - pass customer data and the extracted NIP
    client_data = get_client_details(stripe_invoice.get('customer'), tax_code=nip)

    payload = {
        "invoice_date": paid_date_str,
        "sale_date": sale_date_str,
        "paid_date": paid_date_str,
        "payment_date": paid_date_str, # Set payment due date to paid date
        "currency": stripe_invoice.get('currency', 'PLN').upper(), # Ensure uppercase
        "status": "paid", # Explicitly set status to paid for Infakt
        "kind": "vat",
        "payment_method": map_stripe_payment_method(stripe_invoice),
        # Use Stripe's number if you want consistency, but be aware of potential Infakt duplicates
        # Alternatively, let Infakt generate the number by omitting this field.
        "number": stripe_invoice.get('number'), 
        # "check_duplicate_number": True, # Optional: Add if using Stripe number
        
        # Amounts are calculated by Infakt based on lines if not provided,
        # but providing them is fine too. Ensure they are integers (groszy/cents).
        "net_price": stripe_invoice.get('subtotal'),
        "tax_price": stripe_invoice.get('tax'),
        "gross_price": stripe_invoice.get('total'),
        "paid_price": stripe_invoice.get('amount_paid'),
        "left_to_pay": 0, # Since it's paid
        "sale_type": "service",
        "services": infakt_services,
        **client_data # Merge client details dictionary
    }
    
    # Clean payload: Remove keys with None values as Infakt might reject them
    cleaned_payload = {k: v for k, v in payload.items() if v is not None}
    
    # --- !!! Crucial Validation !!! ---
    # Add checks here: e.g., ensure required fields like services, client details (if needed), dates are present.
    if not cleaned_payload.get("services"):
        logging.error(f"Invoice {stripe_invoice.get('id')} transformation failed: Missing services.")
        return None
    if not cleaned_payload.get("invoice_date"):
         logging.error(f"Invoice {stripe_invoice.get('id')} transformation failed: Missing invoice_date.")
         return None
    # Add more checks based on Infakt's mandatory fields for your account type
        
    logging.debug(f"Transformed payload for Infakt: {cleaned_payload}")
    return cleaned_payload

# --- Main Execution ---
if __name__ == "__main__":
    logging.info("Starting Stripe to Infakt invoice transfer process...")

    if not all([STRIPE_SECRET_KEY, INFAKT_API_KEY, TARGET_YEAR, TARGET_MONTH]):
        logging.error("Missing required configuration in .env file. Please set STRIPE_SECRET_KEY, INFAKT_API_KEY, TARGET_YEAR, TARGET_MONTH.")
        exit(1)

    try:
        # 1. Initialize Clients
        stripe_client = StripeClient(STRIPE_SECRET_KEY)
        infakt_client = InfaktClient(INFAKT_API_KEY)

        # 2. Get Time Range
        start_ts, end_ts = get_month_timestamps(TARGET_YEAR, TARGET_MONTH)

        # 3. Fetch Paid Invoices from Stripe
        stripe_invoices = stripe_client.get_paid_invoices(start_ts, end_ts)

        if not stripe_invoices:
            logging.info("No paid invoices found in Stripe for the specified period.")
            exit(0)

        # 4. Process and Upload Invoices
        success_count = 0
        failure_count = 0
        skipped_by_user_count = 0
        processed_stripe_ids = set() # To track processed invoices

        for invoice_data in stripe_invoices:
            stripe_id = invoice_data.get('id')
            if not stripe_id:
                 logging.warning("Found invoice data without an ID. Skipping.")
                 continue
            
            # Simple check to avoid processing duplicates if Stripe returns them somehow
            if stripe_id in processed_stripe_ids:
                logging.warning(f"Stripe invoice ID {stripe_id} already processed. Skipping.")
                continue
                
            logging.info(f"--- Processing Stripe Invoice ID: {stripe_id} (Number: {invoice_data.get('number', 'N/A')}) ---")

            # 5. Transform Data
            infakt_payload = transform_stripe_to_infakt(invoice_data)

            if infakt_payload:
                # --- User Confirmation Step ---
                client_name = infakt_payload.get('client_company_name') or \
                              f"{infakt_payload.get('client_first_name', '')} {infakt_payload.get('client_last_name', '')}".strip()
                gross_price_units = infakt_payload.get('gross_price', 0)
                currency = infakt_payload.get('currency', '')
                # Convert gross price to major units (e.g., PLN from groszy)
                # This assumes 100 minor units per major unit (common for PLN, EUR, USD)
                gross_price_major = f"{gross_price_units / 100:.2f}" if gross_price_units is not None else "N/A"
                
                
                # Build client_address from payload fields
                client_address_parts = []
                street = infakt_payload.get('client_street')
                if street:
                    client_address_parts.append(street)
                post_code = infakt_payload.get('client_post_code')
                city = infakt_payload.get('client_city')
                if post_code or city:
                    code_city = f"{post_code or ''} {city or ''}".strip()
                    if code_city:
                        client_address_parts.append(code_city)
                country = infakt_payload.get('client_country')
                if country:
                    client_address_parts.append(country)
                client_address = ", ".join(client_address_parts) if client_address_parts else ""
                
                details_summary = (
                    f"Stripe ID: {stripe_id}\n"
                    f"  Infakt #: {infakt_payload.get('number', '(auto)')}\n"
                    f"  Client: {client_name}\n"
                    f"  Client address: {client_address}\n"
                    f"  Date: {infakt_payload.get('invoice_date')}\n"
                    f"  Amount: {gross_price_major} {currency}"
                )
                
                user_confirm = input(f"\nCreate Infakt invoice for:\n{details_summary}\n\nProceed? (y/n): ").lower()
                
                if user_confirm == 'y':
                    # 6. Upload to Infakt
                    logging.info(f"User confirmed. Attempting to create invoice in Infakt for Stripe ID: {stripe_id}")
                    result = infakt_client.create_invoice_async({"invoice": infakt_payload})
                    if result and result.get('invoice_task_reference_number'):
                        logging.info(f"Successfully submitted task for Stripe ID {stripe_id}. Infakt Task Ref: {result.get('invoice_task_reference_number')}")
                        success_count += 1
                    else:
                        logging.error(f"Failed to submit task for Stripe ID {stripe_id}.")
                        failure_count += 1
                else:
                    logging.info(f"User skipped creating Infakt invoice for Stripe ID: {stripe_id}")
                    skipped_by_user_count += 1
                    # Treat skipping as a separate category, not failure?
                    # failure_count += 1 

            else:
                logging.warning(f"Transformation failed or skipped for Stripe ID {stripe_id}. Not sending to Infakt.")
                failure_count += 1 # Count transform failures as failures
            
            processed_stripe_ids.add(stripe_id)
            logging.info(f"--- Finished processing Stripe Invoice ID: {stripe_id} ---")

        # 7. Summary
        logging.info("=== Processing Summary ===")
        logging.info(f"Total Stripe invoices fetched and filtered: {len(processed_stripe_ids)}")
        logging.info(f"Successfully submitted to Infakt queue: {success_count}")
        logging.info(f"Skipped by user confirmation: {skipped_by_user_count}")
        logging.info(f"Failed (transformation or submission): {failure_count}")
        logging.info("=========================")
        logging.info("Script finished.")

    except Exception as e:
        logging.exception(f"An unhandled error occurred during the process: {e}")
        exit(1) 