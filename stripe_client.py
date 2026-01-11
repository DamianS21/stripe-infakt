import stripe
import logging
import time
from datetime import date
from collections import defaultdict

class StripeClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("Stripe API key is required.")
        stripe.api_key = api_key
        logging.info("Stripe client initialized.")
    
    def _has_full_address(self, customer_data: dict | None) -> bool:
        """
        Checks if customer has a full address (street, city, postal code, country).
        Returns True if all address components are present, False otherwise.
        """
        if not customer_data:
            return False
        
        address = customer_data.get('address', {})
        if not address:
            return False
        
        # Check for all required address components
        required_fields = ['line1', 'city', 'postal_code', 'country']
        for field in required_fields:
            value = address.get(field)
            if not value or (isinstance(value, str) and not value.strip()):
                return False
        
        return True
    
    def get_payments_without_full_address(self, start_timestamp: int, end_timestamp: int) -> list:
        """
        Fetches all paid invoices without full customer address within the date range.
        These are typically used for "utarg dzienny" (daily revenue) in Polish accounting.
        
        Returns:
            List of invoice dicts without full address
        """
        all_invoices = self.get_paid_invoices(start_timestamp, end_timestamp)
        
        invoices_without_address = []
        for inv in all_invoices:
            customer_data = inv.get('customer')
            if not self._has_full_address(customer_data):
                invoices_without_address.append(inv)
                logging.debug(f"Invoice {inv.get('id')} has incomplete address - eligible for daily revenue")
            else:
                logging.debug(f"Invoice {inv.get('id')} has full address - skipping for daily revenue")
        
        logging.info(f"Found {len(invoices_without_address)} invoices without full address out of {len(all_invoices)} total")
        return invoices_without_address
    
    def group_payments_by_day(self, invoices: list) -> dict[date, list]:
        """
        Groups invoices by the day they were paid.
        
        Args:
            invoices: List of invoice dicts
            
        Returns:
            Dictionary mapping date to list of invoices paid on that day
        """
        from datetime import datetime, timezone
        
        grouped = defaultdict(list)
        
        for inv in invoices:
            paid_at = inv.get('status_transitions', {}).get('paid_at')
            if paid_at:
                # Convert timestamp to date
                paid_date = datetime.fromtimestamp(paid_at, tz=timezone.utc).date()
                grouped[paid_date].append(inv)
            else:
                logging.warning(f"Invoice {inv.get('id')} has no paid_at timestamp - skipping")
        
        return dict(grouped)

    def get_paid_invoices(self, start_timestamp: int, end_timestamp: int) -> list:
        """Fetches all paid invoices and filters them by paid date within the specified timestamp range."""
        all_paid_invoices_raw = []
        starting_after = None
        limit = 100 # Stripe default/max limit per request

        logging.info(f"Fetching all paid Stripe invoices to filter for range {start_timestamp} to {end_timestamp}")

        while True:
            try:
                invoices = stripe.Invoice.list(
                    status='paid',
                    # Removed status_transitions filter - will filter client-side
                    limit=limit,
                    starting_after=starting_after,
                    # Corrected expand parameter: expand 'data.lines' to get line items
                    expand=['data.customer', 'data.lines']
                )
                
                if not invoices.data:
                    logging.info("No more invoices found in this page.")
                    break

                fetched_count = len(invoices.data)
                logging.info(f"Fetched {fetched_count} invoices in this batch.")
                all_paid_invoices_raw.extend(invoices.data)

                if not invoices.has_more:
                    logging.info("No more pages of invoices.")
                    break

                # Get the ID of the last invoice in the current list to use for pagination
                starting_after = invoices.data[-1].id
                logging.info(f"Fetching next page starting after invoice ID: {starting_after}")
                 # Optional: Add a small delay to avoid hitting rate limits aggressiveley
                # time.sleep(0.5)

            except stripe.error.RateLimitError as e:
                logging.warning(f"Stripe rate limit hit. Sleeping for 5 seconds. Error: {e}")
                time.sleep(5)
                # Continue the loop to retry the same request (starting_after remains the same)
                continue
            except stripe.error.StripeError as e:
                logging.error(f"An error occurred while fetching Stripe invoices: {e}")
                # Depending on the error, you might want to break or retry
                raise # Re-raise the exception to halt the process
            except Exception as e:
                 logging.error(f"An unexpected error occurred: {e}")
                 raise
                 
        logging.info(f"Finished fetching {len(all_paid_invoices_raw)} total paid invoices. Now filtering by paid_at date...")

        # Client-side filtering
        filtered_invoices = []
        for inv_obj in all_paid_invoices_raw:
            inv = inv_obj.to_dict_recursive() # Convert StripeObject to dict for easier access
            paid_at = inv.get('status_transitions', {}).get('paid_at')
            if paid_at and start_timestamp <= paid_at <= end_timestamp:
                filtered_invoices.append(inv)
            else:
                logging.debug(f"Invoice {inv.get('id')} paid at {paid_at} is outside the target range {start_timestamp}-{end_timestamp}. Skipping.")

        logging.info(f"Found {len(filtered_invoices)} invoices paid within the target date range.")
        return filtered_invoices

    def get_checkout_payments(self, start_timestamp: int, end_timestamp: int) -> list:
        """
        Fetches all successful Checkout Session payments within the date range.
        These are one-time payments not linked to invoices/subscriptions.
        
        Returns:
            List of payment dicts with unified structure for daily revenue
        """
        all_payments = []
        starting_after = None
        limit = 100

        logging.info(f"Fetching Stripe Checkout sessions for range {start_timestamp} to {end_timestamp}")

        while True:
            try:
                # Fetch completed checkout sessions
                sessions = stripe.checkout.Session.list(
                    limit=limit,
                    starting_after=starting_after,
                    expand=['data.customer', 'data.line_items']
                )

                if not sessions.data:
                    break

                fetched_count = len(sessions.data)
                logging.debug(f"Fetched {fetched_count} checkout sessions in this batch.")

                for session in sessions.data:
                    session_dict = session.to_dict_recursive()
                    
                    # Only include completed sessions
                    if session_dict.get('status') != 'complete':
                        continue
                    
                    # Only include sessions that resulted in payment (not subscriptions with invoices)
                    if session_dict.get('mode') == 'subscription':
                        # Subscriptions create invoices, so skip to avoid double-counting
                        continue
                    
                    # Check if the payment was made in our date range
                    created_at = session_dict.get('created')
                    if not created_at or not (start_timestamp <= created_at <= end_timestamp):
                        continue
                    
                    all_payments.append(session_dict)

                if not sessions.has_more:
                    break

                starting_after = sessions.data[-1].id

            except stripe.error.RateLimitError as e:
                logging.warning(f"Stripe rate limit hit. Sleeping for 5 seconds. Error: {e}")
                time.sleep(5)
                continue
            except stripe.error.StripeError as e:
                logging.error(f"An error occurred while fetching Stripe checkout sessions: {e}")
                raise
            except Exception as e:
                logging.error(f"An unexpected error occurred: {e}")
                raise

        logging.info(f"Found {len(all_payments)} checkout session payments in the date range.")
        return all_payments

    def get_all_payments_for_daily_revenue(self, start_timestamp: int, end_timestamp: int) -> list:
        """
        Fetches all payments eligible for daily revenue:
        1. Invoices without full customer address
        2. Checkout session payments (one-time purchases)
        
        Returns unified list of payment records with consistent structure.
        """
        unified_payments = []
        
        # 1. Get invoices without full address
        invoices = self.get_payments_without_full_address(start_timestamp, end_timestamp)
        for inv in invoices:
            unified_payments.append(self._normalize_invoice_to_payment(inv))
        
        # 2. Get checkout session payments
        checkout_payments = self.get_checkout_payments(start_timestamp, end_timestamp)
        for checkout in checkout_payments:
            unified_payments.append(self._normalize_checkout_to_payment(checkout))
        
        logging.info(f"Total payments for daily revenue: {len(unified_payments)} ({len(invoices)} invoices, {len(checkout_payments)} checkout sessions)")
        return unified_payments

    def _normalize_invoice_to_payment(self, invoice: dict) -> dict:
        """Normalizes an invoice to unified payment structure."""
        customer = invoice.get('customer') or {}
        customer_name = (customer.get('name') or '') if customer else ''
        customer_email = (customer.get('email') or '') if customer else ''
        
        # Get country from address (handle None address)
        address = customer.get('address') or {}
        customer_country = address.get('country') or ''
        
        # Get line items description
        items = []
        lines_data = invoice.get('lines') or {}
        lines = lines_data.get('data') or []
        for line in lines:
            items.append({
                'description': line.get('description', 'N/A'),
                'quantity': line.get('quantity', 1),
                'amount': line.get('amount', 0),
                'currency': invoice.get('currency', 'usd').upper()
            })
        
        return {
            'type': 'invoice',
            'id': invoice.get('id'),
            'number': invoice.get('number'),
            'customer_name': customer_name,
            'customer_email': customer_email,
            'customer_country': customer_country,
            'amount': invoice.get('total', 0),
            'currency': invoice.get('currency', 'usd').upper(),
            'paid_at': (invoice.get('status_transitions') or {}).get('paid_at'),
            'items': items,
            'original': invoice  # Keep original for reference
        }

    def _normalize_checkout_to_payment(self, checkout: dict) -> dict:
        """Normalizes a checkout session to unified payment structure."""
        customer = checkout.get('customer') or {}
        customer_details = checkout.get('customer_details') or {}
        
        # Prefer customer_details from checkout over customer object
        customer_name = (customer_details.get('name') or '') or ((customer.get('name') or '') if customer else '')
        customer_email = (customer_details.get('email') or '') or ((customer.get('email') or '') if customer else '')
        
        # Get country from address (handle None address)
        address = customer_details.get('address') or {}
        customer_country = address.get('country') or ''
        
        # Get line items
        items = []
        line_items_data = checkout.get('line_items') or {}
        line_items = line_items_data.get('data') or []
        for line in line_items:
            items.append({
                'description': line.get('description', 'N/A'),
                'quantity': line.get('quantity', 1),
                'amount': line.get('amount_total', 0),
                'currency': checkout.get('currency', 'usd').upper()
            })
        
        return {
            'type': 'checkout',
            'id': checkout.get('id'),
            'number': checkout.get('id'),  # Checkout sessions don't have invoice numbers
            'customer_name': customer_name,
            'customer_email': customer_email,
            'customer_country': customer_country,
            'amount': checkout.get('amount_total', 0),
            'currency': checkout.get('currency', 'usd').upper(),
            'paid_at': checkout.get('created'),  # Use created as paid_at for checkout
            'items': items,
            'original': checkout
        }

    def group_unified_payments_by_day(self, payments: list) -> dict[date, list]:
        """
        Groups unified payment records by the day they were paid.
        
        Args:
            payments: List of unified payment dicts
            
        Returns:
            Dictionary mapping date to list of payments on that day
        """
        from datetime import datetime, timezone
        
        grouped = defaultdict(list)
        
        for payment in payments:
            paid_at = payment.get('paid_at')
            if paid_at:
                paid_date = datetime.fromtimestamp(paid_at, tz=timezone.utc).date()
                grouped[paid_date].append(payment)
            else:
                logging.warning(f"Payment {payment.get('id')} has no paid_at timestamp - skipping")
        
        return dict(grouped) 