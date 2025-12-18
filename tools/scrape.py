import trafilatura

def scrape_url(url: str):
    """
    Scrapes the main text content from a webpage, ignoring ads and sidebars.
    Args:
        url: The web address (http://...) to read.
    """
    try:
        # 1. Download the HTML
        downloaded = trafilatura.fetch_url(url)
        
        if downloaded is None:
            return f"Error: Could not retrieve content from {url} (404 or blocked)."
            
        # 2. Extract the "Meat" (Main Text)
        # include_comments=False strips user comments (often toxic/irrelevant)
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=True)
        
        if not text:
            return "Error: Page downloaded but no main text found (might be Javascript-heavy or empty)."
            
        return text

    except Exception as e:
        return f"Scraping Error: {e}"
    
