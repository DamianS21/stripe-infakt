import stripe
import logging
import time

class StripeClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("Stripe API key is required.")
        stripe.api_key = api_key
        logging.info("Stripe client initialized.")

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