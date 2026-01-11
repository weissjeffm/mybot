import inspect
from . import ssh, ipmi, scrape, search, topic  # Import tool modules

# 1. Register your tools here
REGISTRY = {
    "run_cmd": ssh.run_remote_cmd,
    "check_temps": ipmi.check_temps,
    "scrape_webpage": scrape.scrape_url,
    "search": search.search_web,
    "topic": topic.signal_topic_change
    # "uptime": basic.get_uptime 
}

def get_tools_dict():
    return REGISTRY

def generate_system_prompt():
    """
    Dynamically builds instructions. Updated to encourage parallel tool calling.
    """
    prompt = "You are an autonomous research assistant.\n"
    prompt += "You have access to the following tools:\n\n"
    
    for name, func in REGISTRY.items():
        sig = inspect.signature(func)
        doc = inspect.getdoc(func) or "No description provided."
        prompt += f"- {name}{sig}: {doc}\n"
    
    # Updated Instruction Block
    prompt += """### TOOL USAGE RULES:
    1. To use a tool, start a reply with: Action: tool_name(arg1, arg2='val')
    2. PARALLEL EXECUTION: You can issue MULTIPLE actions at once by
       listing them on separate lines.

    Do this whenever tasks are independent (e.g., scraping 3 different
    URLs or searching for 2 separate subtopics, or changing the room
    topic in parallel with other actions).

    Example:
    Action: topic("Keanu Reeves Quotes")
    Action: search('matrix quotes')
    Action: search('speed quotes')

    ### RESEARCH GUIDELINES:
    When asked to do research, or about a topic you don't have enough
    knowledge of, use 'search' tool as a first step. Find the most
    relevant links, then use the scrape_webpage tool to get the page
    contents. You are encouraged to scrape multiple relevant pages in
    a single step using parallel Actions.

    If asked to do deep research, use these limits:
    searches: 15, webpage fetches: 30.

    Otherwise use these limits:
    searches: 5, webpage fetches: 10.

    Always cite sources. At the end of a paragraph where you're referring to
    scraped content, append a properly formatted link that matches the
    scraped URL."""
    
    return prompt

def to_data(obj):
    """Recursively converts objects to dictionaries if possible."""
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    if isinstance(obj, dict):
        return {k: to_data(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_data(x) for x in x]
    
    # If it has a __dict__, it's an object we can convert
    if hasattr(obj, "__dict__"):
        return vars(obj)
    
    return str(obj) # Fallback to string representation
