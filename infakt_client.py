import requests
import logging
import json
from datetime import date

class InfaktClient:
    def __init__(self, api_key: str, sandbox: bool = False):
        if not api_key:
            raise ValueError("Infakt API key is required.")

        self.api_key = api_key
        # Use sandbox URL if specified
        if sandbox:
            self.base_url = "https://api.sandbox-infakt.pl/api/v3"
        else:
            self.base_url = "https://api.infakt.pl/api/v3"
        self.headers = {
            'X-inFakt-ApiKey': self.api_key,
            'Content-Type': 'application/json'
        }
        logging.info(f"InfaktClient initialized with base URL: {self.base_url}")

    def create_invoice_async(self, invoice_payload: dict) -> dict | None:
        """Sends the invoice payload to Infakt's asynchronous creation endpoint."""
        endpoint = f"{self.base_url}/async/invoices.json"
        
        # Ensure the payload is wrapped correctly
        if 'invoice' not in invoice_payload:
             payload = {"invoice": invoice_payload} 
        else:
             payload = invoice_payload # Already wrapped
             
        try:
            response = requests.post(endpoint, headers=self.headers, data=json.dumps(payload))
            response.raise_for_status()  # Raises HTTPError for bad responses (4xx or 5xx)
            
            response_data = response.json()
            logging.info(f"Successfully submitted invoice to Infakt async queue. Task Ref: {response_data.get('invoice_task_reference_number')}")
            return response_data

        except requests.exceptions.RequestException as e:
            logging.error(f"Error sending invoice to Infakt: {e}")
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_details = e.response.json()
                    logging.error(f"Infakt API Error Response: {json.dumps(error_details)}")
                except json.JSONDecodeError:
                    logging.error(f"Infakt API Error Response (non-JSON): {e.response.text}")
            return None
        except Exception as e:
            logging.error(f"An unexpected error occurred during Infakt API call: {e}")
            return None

    def create_daily_revenue_async(self, daily_revenue_payload: dict) -> dict | None:
        """
        Sends the daily revenue payload to Infakt's asynchronous creation endpoint.
        
        Args:
            daily_revenue_payload: Dictionary containing daily revenue data
            
        Returns:
            Response data with task reference number or None on failure
        """
        endpoint = f"{self.base_url}/async/daily_revenues.json"
        
        # Ensure the payload is wrapped correctly
        if 'daily_revenue' not in daily_revenue_payload:
            payload = {"daily_revenue": daily_revenue_payload}
        else:
            payload = daily_revenue_payload
        
        try:
            logging.debug(f"Sending daily revenue payload: {json.dumps(payload, indent=2)}")
            response = requests.post(endpoint, headers=self.headers, data=json.dumps(payload))
            response.raise_for_status()
            
            response_data = response.json()
            print(response_data)
            task_ref = response_data.get('invoice_task_reference_number')
            logging.info(f"Successfully submitted daily revenue to Infakt async queue. Task Ref: {task_ref}")
            return response_data

        except requests.exceptions.RequestException as e:
            logging.error(f"Error sending daily revenue to Infakt: {e}")
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_details = e.response.json()
                    logging.error(f"Infakt API Error Response: {json.dumps(error_details)}")
                except json.JSONDecodeError:
                    logging.error(f"Infakt API Error Response (non-JSON): {e.response.text}")
            return None
        except Exception as e:
            logging.error(f"An unexpected error occurred during Infakt daily revenue API call: {e}")
            return None

    def check_daily_revenue_status(self, task_reference: str) -> dict | None:
        """
        Checks the status of an async daily revenue creation task.
        
        Args:
            task_reference: The task reference number from create_daily_revenue_async
            
        Returns:
            Status response or None on failure
        """
        endpoint = f"{self.base_url}/async/daily_revenues/status/{task_reference}.json"
        
        try:
            response = requests.get(endpoint, headers=self.headers)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logging.error(f"Error checking daily revenue status: {e}")
            return None 