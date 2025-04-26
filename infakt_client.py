import requests
import logging
import json

class InfaktClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("Infakt API key is required.")

        self.api_key = api_key
        self.base_url = f"https://api.infakt.pl/api/v3"
        self.headers = {
            'X-inFakt-ApiKey': self.api_key,
            'Content-Type': 'application/json'
        }

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