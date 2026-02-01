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

    try:
        response = requests.get(url, headers=headers, timeout=15)
        
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}: Failed to fetch page")

        if "Please complete the security check" in response.text:
            raise Exception("Blocked by CAPTCHA/Cloudflare")

        text = trafilatura.extract(response.text, include_comments=False, include_tables=True, include_links=True)
        if not text:
            raise Exception("No extractable text found")

        return text  # ←← Return raw text only

    except Exception as e:
        raise Exception(f"Failed to scrape {url}: {str(e)}")
