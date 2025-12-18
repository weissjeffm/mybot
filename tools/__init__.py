import inspect
from . import ssh, ipmi, scrape # basic # Import your tool modules

# 1. Register your tools here
REGISTRY = {
    "run_cmd": ssh.run_remote_cmd,
    "check_temps": ipmi.check_temps,
    "scrape_webpage": scrape.scrape_url
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
    return prompt

