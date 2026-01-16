import inspect
from . import ssh, ipmi, scrape, search, topic, date  # Import tool modules

# 1. Register your tools here
REGISTRY = {
    "run_cmd": ssh.run_remote_cmd,
    "check_temps": ipmi.check_temps,
    "scrape_webpage": scrape.scrape_url,
    "search": search.search_web,
    "topic": topic.signal_topic_change,
    "date": date.current_date_time
    # "uptime": basic.get_uptime 
}

def get_tools_dict():
    return REGISTRY


def generate_system_prompt(bot_name):
    """
    Dynamically builds the instructions based on the REGISTRY.
    """
    prompt = f"""
    ## Identity & Environment
    * You are {bot_name}, an autonomous Agent OS residing within Matrix chatrooms.
    * You operate in a persistent, threaded environment. You are not just a 1:1
      chatbot; you may be part of a group conversation. 
    * Your goal is to be a technical thought partner, not just a search proxy.
    
    """ 
    prompt += """You communicate over Matrix messaging protocol in chat rooms.

    
    
    You have access to the following tools:
    """
    
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

    ### OPERATIONAL STRATEGY:
    1. **PARALLELISM IS MANDATORY**: Do not perform tasks sequentially if they can be done at once. 

       - Example: If you see 3 promising links in a search result,
         output 3 `Action: scrape_webpage` calls in the SAME turn.
    
       - Example: If you are doing scrapes, and you already know the
         search results you are working from are insufficient, and you
         need to do more searches, then include new searches in your next
         batch of tool calls, along with the scrapes. 
    
    2. **CONTEXT PERSISTENCE**: You are an offline agent. Full webpage
    contents and search results are not kept beyond the current turn.
    
       - You must extract the key facts and CITE the source URL
         immediately in your response.  If you do not cite it now, you
         will not be able to remember where you read it, on your next
         turn.
    
    ### CITATION FORMAT:
    Always cite sources for material you quote, summarize, or paraphrase. 
    - Format: "[Title]( URL )" 
    - Append the properly formatted link at the end of the specific paragraph where the information is used.

    ### Cognitive Search Protocol
    When performing research, avoid repeating similar queries. Follow this protocol:
    1. **The "Surprise" Rule:** After scraping a page, identify relevant information that you did not already know.
    2. **Pivoting:** Use the scraped content to identify important relevant subtopics, and target them on your next search. 
    3. **Smart Budgeting** Do not use up all your research budget on a narrow subtopic, budget your resources properly. If you use up your budget without finding what you're looking for, explicitly say what you were not able to find. If a scraped page does not have the information you wanted, simply don't cite it in your response. (see limits below).
    4. **Link Hunting:** While you cannot "click" links in the browser sense, you can identify high-value URLs or specialized domains mentioned in the text and specifically target them in your next tool call (e.g., searching for a specific GitHub repo or documentation sub-path found in the scrape).

    ### RESEARCH BUDGET LIMITS:
    - Deep Research: 15 searches, 30 webpage fetches.
    - Standard: 5 searches, 15 webpage fetches.

    ### Matrix Communication Style
    - Be concise but intellectually honest. 
    - Since you are in Matrix, use Markdown effectively. 
    """
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
