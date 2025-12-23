import requests
import trafilatura

def scrape_url(url: str):
    """
    Scrapes the main text content from a webpage.
    Uses a spoofed User-Agent to bypass basic bot blockers.
    """
    # Spoof a real browser to get past archive.is / Cloudflare basic checks
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        # 1. explicit Network Request
        response = requests.get(url, headers=headers, timeout=15)
        
        # 2. Check for HTTP Errors
        if response.status_code != 200:
            return f"Error: HTTP {response.status_code} ({response.reason})"
            
        # 3. Handle 'Soft' Blocks (archive.is sometimes returns 200 but with a captcha)
        if "Please complete the security check" in response.text:
            return "Error: Blocked by CAPTCHA/Security Check."

        # 4. Extract Text
        # include_comments=False strips user comments
        # include_tables=True keeps data tables
        text = trafilatura.extract(response.text, include_comments=False, include_tables=True)
        
        if not text:
            return "Error: Page downloaded successfully, but trafilatura found no main text. (Page might be empty or pure JS)."
            
        return text

    except requests.exceptions.Timeout:
        return "Error: Request timed out (15s)."
    except requests.exceptions.ConnectionError:
        return "Error: Connection refused or DNS failure."
    except Exception as e:
        return f"Scraping Exception: {str(e)}"
