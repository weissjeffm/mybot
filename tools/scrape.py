import requests
import trafilatura
import json
#from .utils import to_data

def scrape_url(url: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    result = {
        "status": "error",
        "code": 0,
        "message": "",
        "result": ""
    }

    try:
        response = requests.get(url, headers=headers, timeout=15)
        result["code"] = response.status_code
        #result["response"] = to_data(response)
        
        if response.status_code == 200:
            if "Please complete the security check" in response.text:
                result["message"] = "Blocked by CAPTCHA/Cloudflare."
            else:
                text = trafilatura.extract(response.text, include_comments=False, include_tables=True)
                if text:
                    result["status"] = "ok"
                    result["result"] = text
                    result["message"] = f"Extracted text from page"
                else:
                    result["message"] = "No extractable text found."
        else:
            result["message"] = f"HTTP Error: {response.status_code}"
                
    except requests.exceptions.Timeout:
        result["message"] = "Request timed out."
    except requests.exceptions.ConnectionError:
        result["message"] = "Connection/DNS failure."
    except Exception as e:
        result["message"] = f"Exception: {str(e)}"
    
    return result
