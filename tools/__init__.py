import inspect
from . import ssh, ipmi, scrape, search, topic  # basic # Import your tool modules

# 1. Register your tools here
REGISTRY = {
    "run_cmd": ssh.run_remote_cmd,
    "check_temps": ipmi.check_temps,
    "scrape_webpage": scrape.scrape_url,
    "search": search.search_web
    # "uptime": basic.get_uptime 
}

def get_tools_dict():
    return REGISTRY

def generate_system_prompt():
    """
    Dynamically builds the instructions based on the REGISTRY.
    """
    prompt = "You are an Autonomous Agent.\n"
    prompt += "You have access to the following tools:\n\n"
    
    for name, func in REGISTRY.items():
        # Get the function signature (e.g., "(command, hostname, user='root')")
        sig = inspect.signature(func)
        # Get the docstring (the description)
        doc = inspect.getdoc(func) or "No description provided."
        
        prompt += f"- {name}{sig}: {doc}\n"
    
    prompt += "\nTo use a tool, reply with: Action: tool_name(arg1, arg2='val')\n"
    prompt += """ Never guess URLs to scrape. use 'search' tool as a
    first step in doing research. Find the most relevant links using
    the search result summary, then use the scrape_webpage tool to get
    the page contents. Limit search calls to 3, and scrape_webpage calls to 10.

    Include references in your responses where possible. At the end of
    a paragraph where you're referring to scraped content, append a
    properly formatted link that matches the scraped URL.
    """
    return prompt

