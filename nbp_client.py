import requests
import logging
from datetime import date, timedelta

class NBPClient:
    """Client for fetching exchange rates from NBP (National Bank of Poland) API."""
    
    def __init__(self):
        self.base_url = "https://api.nbp.pl/api/exchangerates/rates"
        logging.info("NBP client initialized.")
    
    def get_usd_rate(self, rate_date: date) -> float | None:
        """
        Fetches the USD to PLN exchange rate for a specific date.
        NBP publishes rates for business days only.
        If the rate is not available for the given date (weekend/holiday),
        it will try previous days up to 7 days back.
        
        Args:
            rate_date: The date to fetch the rate for
            
        Returns:
            The mid exchange rate (USD to PLN) or None if not found
        """
        result = self.get_usd_rate_with_date(rate_date)
        return result[0] if result else None
    
    def get_usd_rate_with_date(self, rate_date: date) -> tuple[float, date] | None:
        """
        Fetches the USD to PLN exchange rate for a specific date.
        Returns both the rate and the actual date from which it was fetched.
        
        Args:
            rate_date: The date to fetch the rate for
            
        Returns:
            Tuple of (rate, actual_date) or None if not found
        """
        # Try the given date and up to 7 previous days
        for days_back in range(8):
            check_date = rate_date - timedelta(days=days_back)
            rate = self._fetch_rate_for_date(check_date)
            if rate is not None:
                if days_back > 0:
                    logging.info(f"No rate for {rate_date}, using rate from {check_date}: {rate}")
                return (rate, check_date)
        
        logging.error(f"Could not find USD rate for {rate_date} or 7 previous days")
        return None
    
    def _fetch_rate_for_date(self, rate_date: date) -> float | None:
        """Fetches the USD rate for a specific date from NBP API."""
        date_str = rate_date.strftime('%Y-%m-%d')
        # Table A contains mid exchange rates
        url = f"{self.base_url}/a/usd/{date_str}/"
        
        try:
            response = requests.get(url, headers={'Accept': 'application/json'}, timeout=10)
            
            if response.status_code == 404:
                # No rate published for this date (weekend/holiday)
                logging.debug(f"No NBP rate available for {date_str}")
                return None
            
            response.raise_for_status()
            data = response.json()
            
            # NBP API returns: {"table":"A","currency":"dolar amerykański","code":"USD","rates":[{"no":"...","effectiveDate":"...","mid":X.XXXX}]}
            if 'rates' in data and len(data['rates']) > 0:
                rate = data['rates'][0].get('mid')
                logging.debug(f"NBP USD rate for {date_str}: {rate}")
                return rate
            
            return None
            
        except requests.exceptions.RequestException as e:
            logging.error(f"Error fetching NBP rate for {date_str}: {e}")
            return None
        except Exception as e:
            logging.error(f"Unexpected error fetching NBP rate for {date_str}: {e}")
            return None
    
    def get_previous_day_usd_rate(self, target_date: date) -> float | None:
        """
        Gets the USD rate from the previous business day.
        According to Polish tax law, foreign currency should be converted
        using the rate from the day before the transaction.
        
        Args:
            target_date: The transaction date
            
        Returns:
            The USD to PLN rate from the previous business day
        """
        previous_day = target_date - timedelta(days=1)
        return self.get_usd_rate(previous_day)
    
    def get_previous_day_usd_rate_with_date(self, target_date: date) -> tuple[float, date] | None:
        """
        Gets the USD rate from the previous business day with actual rate date.
        
        Args:
            target_date: The transaction date
            
        Returns:
            Tuple of (rate, actual_rate_date) or None if not found
        """
        previous_day = target_date - timedelta(days=1)
        return self.get_usd_rate_with_date(previous_day)

