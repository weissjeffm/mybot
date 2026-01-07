from ddgs import DDGS
import json

def search_web(query: str, max_results=5):
    """
    Searches the web using DuckDuckGo.
    How to search:
       - Do not stuff too many keywords into the search query, you must choose searches where all the terms could appear in one document.
       - Bad: "UBI trials robot tax". Good: "UBI trials", then separately "robot tax".
       - Use quotes around a word or phrase, if the document should contain that exact term. Example: "Los Angeles" 1991 riots
       - If a search returns no results, try again with fewer words.
       - IMPORTANT: The search results contain summaries, but they are only to be used to decide the *relevance* of that page to what you are looking for. Summaries are NOT the actual content. To get the content, call the scrape_webpage tool and pass it the URL of the search result. Do not call the search tool more than 3 times in a row.
    """
    print(f"DEBUG: Searching for '{query}'") # Visible in bridge logs

    result = {
        "status": "error",
        "code": 0,
        "message": "Search failed to initiate",
        "result": []
    }
    
    try:
        with DDGS() as ddgs:
            # Try API first
            search_results = list(ddgs.text(query, max_results=max_results, backend="api"))
            
            # Fallback to HTML
            if not search_results:
                search_results = list(ddgs.text(query, max_results=max_results, backend="html"))

            
            if search_results:
                cleaned_results = []
                for item in search_results:
                    cleaned_results.append({
                        "title": item.get("title", "No Title"),
                        "url": item.get("href") or item.get("url"), # DDG uses 'href' usually
                        "snippet": item.get("body", "")
                    })
                result["status"] = "ok"
                result["code"] = 200
                result["message"] = f"Found {len(search_results)} results for '{query}'"
                result["result"] = cleaned_results
            else:
                result["code"] = 404
                result["message"] = "No results found for this query."
                                     
    except Exception as e:
        result["code"] = 500
        result["message"] = f"DDG Error: {str(e)}"
    
    return result

