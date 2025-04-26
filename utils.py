import datetime
import time
import logging

def get_month_timestamps(year: int, month: int) -> tuple[int, int]:
    """Calculates the start and end Unix timestamps for a given month."""
    start_dt = datetime.datetime(year, month, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    
    # Find the first day of the next month, then subtract one second
    if month == 12:
        end_dt = datetime.datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc) - datetime.timedelta(seconds=1)
    else:
        end_dt = datetime.datetime(year, month + 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc) - datetime.timedelta(seconds=1)
        
    start_timestamp = int(start_dt.timestamp())
    end_timestamp = int(end_dt.timestamp())
    logging.info(f"Calculated time range: {start_dt.isoformat()} to {end_dt.isoformat()} ({start_timestamp} to {end_timestamp})")
    return start_timestamp, end_timestamp

def timestamp_to_infakt_date(timestamp: int | None) -> str | None:
    """Converts a Unix timestamp to YYYY-MM-DD format."""
    if timestamp is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc).strftime('%Y-%m-%d')
    except Exception as e:
        logging.warning(f"Could not convert timestamp {timestamp} to date: {e}")
        return None

def map_stripe_tax_rate_to_infakt_symbol(percentage: float | None) -> str | None:
    """Maps Stripe tax percentage to Infakt tax symbol."""
    # Check what's appcliable in https://api.infakt.pl/api/v3/vat_rates.json
    if percentage is None:
        return "zw"


def map_stripe_payment_method(stripe_invoice: dict) -> str:
    """Attempts to map Stripe payment method info to Infakt's enum."""
    # Stripe invoice object might not directly contain the simple payment method.
    # It might be on the associated Charge or Payment Intent.
    # This is a simplified example, you might need to fetch the Charge/PI.
    # For paid invoices, 'transfer' or 'card' are common.
    # Defaulting to 'other' if specific method isn't easily derivable.
    
    payment_intent_id = stripe_invoice.get('payment_intent')
    charge_id = stripe_invoice.get('charge')
    
    # In a real scenario, you might fetch the PI/Charge using their IDs
    # and check `payment_method_details.type` (e.g., 'card', 'sepa_debit')
    
    if payment_intent_id or charge_id:
         # Simplification: If there's a PI or Charge ID, assume card/transfer
         # A more robust solution would fetch the PI/Charge and check details.
         # Prioritizing 'card' as it's common with Stripe.
         return 'card' 
         
    return 'other' # Default fallback

def get_client_details(customer_data: dict | None, tax_code: str | None = None) -> dict:
    """Extracts and formats client details for Infakt, using tax_code presence to identify companies."""
    if not customer_data:
        logging.warning("No customer data found for invoice.")
        return {}

    details = {
        "client_street": customer_data.get("address", {}).get("line1"),
        "client_city": customer_data.get("address", {}).get("city"),
        "client_post_code": customer_data.get("address", {}).get("postal_code"),
        "client_country": customer_data.get("address", {}).get("country"), # Needs mapping to alpha_2 if not already
    }

    name = customer_data.get("name") # Stripe often uses 'name' for company name too

    if tax_code:
        # If tax code is present, assume it's a company
        details["client_tax_code"] = tax_code
        details["client_company_name"] = name # Use the name field as company name
        # Infakt requires client_company_name if it's not a private person
        details["client_business_activity_kind"] = "other_business" # or 'self_employed' - Infakt might not differentiate strictly
        logging.debug(f"Identified as company (Tax ID: {tax_code}): {name}")
    elif name:
        # No tax code, try to treat as private person if name looks like it
        parts = name.split(' ', 1)
        if len(parts) == 2: # Simple check for first/last name
             details["client_first_name"] = parts[0]
             details["client_last_name"] = parts[1]
             details["client_business_activity_kind"] = "private_person"
             logging.debug(f"Identified as private person: {name}")
        else:
             # If name doesn't look like First Last, but no tax ID, treat as company name as fallback
             details["client_company_name"] = name
             details["client_business_activity_kind"] = "other_business"
             logging.warning(f"No Tax ID, but name '{name}' doesn't split into two parts. Treating as company name.")
    else:
        # No name and no tax ID - insufficient data?
        logging.error(f"Cannot determine client type: No name or tax ID for customer {customer_data.get('id')}")
        # Returning minimal data, might fail Infakt validation
        pass


    # Clean Nones *after* all logic
    return {k: v for k, v in details.items() if v is not None} 