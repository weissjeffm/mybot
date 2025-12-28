import inspect
from . import ssh, ipmi, scrape, search, topic  # Import tool modules

# 1. Register your tools here
REGISTRY = {
    "run_cmd": ssh.run_remote_cmd,
    "check_temps": ipmi.check_temps,
    "scrape_webpage": scrape.scrape_url,
    "search": search.search_web
    "topic": topic.signal_topic_change
    # "uptime": basic.get_uptime 
}

def get_tools_dict():
    return REGISTRY

def generate_system_prompt():
    """
    Dynamically builds the instructions based on the REGISTRY.
    """
    prompt = "You are an autonomous research assistant.\n"
    prompt += "You have access to the following tools:\n\n"
    
    for name, func in REGISTRY.items():
        # Get the function signature (e.g., "(command, hostname, user='root')")
        sig = inspect.signature(func)
        # Get the docstring (the description)
        doc = inspect.getdoc(func) or "No description provided."
        
        prompt += f"- {name}{sig}: {doc}\n"
    
    prompt += "\nTo use a tool, reply with: Action: tool_name(arg1, arg2='val')\n"
    prompt += """Use 'search' tool as a first step in doing
    research. Find the most relevant links using the search result
    summary, then use the scrape_webpage tool to get the page
    contents.

    If asked to do deep research, use these limits:
    searches: 15, webpage fetches: 30.

    Otherwise use these limits:
    searches: 5, webpage fetches: 10.

    Always cite sources for material you quote, summarize, or
    paraphrase. At the end of a paragraph where you're referring to
    scraped content, append a properly formatted link that matches the
    scraped URL.  """
    return prompt

