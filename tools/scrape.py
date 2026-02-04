import requests
import trafilatura

def scrape_url(url: str):
    """Retrieve the text of a webpage given a URL.
    Returns: Extracted plain text on success.
    Raises: Exception with error message on failure.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    result = {
        "status": "error",
        "code": 0,
        "message": "Scrape failed to initiate",
        "result": None,
        "type": "scrape"       
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        result["code"] = response.status_code
        if response.status_code != 200:
            result["message"] = "Failed to fetch page, HTTP error"
            
        if "Please complete the security check" in response.text:
            result["message"] = "Blocked by CAPTCHA/Cloudflare"

        text = trafilatura.extract(response.text, include_comments=False, include_tables=True, include_links=True)
        if not text:
            result["message"] = "No extractable text found"

        result["status"] = "ok"
        result["result"] = text
        result["message"] = "Page retrieved"
        return result
    except Exception as e:
        return result
