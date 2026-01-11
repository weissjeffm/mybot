import operator
import json
import uuid
from typing import Annotated, TypedDict, Union, List
from langchain_openai import ChatOpenAI
from langchain_core.messages import ToolMessage, SystemMessage, HumanMessage, BaseMessage, AIMessage
from langgraph.graph import StateGraph, END
from tools import get_tools_dict, generate_system_prompt
import subprocess

# --- 1. CONFIGURATION ---
llm = ChatOpenAI(
    base_url="http://localhost:8080/v1", # Your LocalAI URL
    api_key="sk-50cf096cc7c795865e",
    model="qwen3-235b-a22b-instruct-2507", # Match your LocalAI model name
    temperature=0,
    max_tokens=102400
)

TOOLS = get_tools_dict() # Automatically loads ssh, ipmi, etc.
SYSTEM_PROMPT = generate_system_prompt() # Automatically writes the instructions

# --- 3. THE STATE ---
class AgentState(TypedDict):
    # 'operator.add' means: when we return new messages, APPEND them to this list
    messages: Annotated[List[BaseMessage], operator.add]
    current_thought: str

# --- 5. THE NODES ---

async def reason_node(state: AgentState):
    """The Brain: Decides what to do."""
    messages = [SystemMessage(content=SYSTEM_PROMPT)] + state['messages']
    
    # Call Qwen
    response = await llm.ainvoke(messages)
    content = response.content
    
    return {"messages": [response], "current_thought": content}

# In langgraph_agent.py
import asyncio
import re
import uuid
import json

async def act_node(state: AgentState):
    """
    The Hands: Executes multiple tools in parallel. 
    Only parses lines that strictly start with 'Action:'.
    """
    last_message = state['messages'][-1].content
    
    # 1. Extract only strict Action lines
    action_strings = []
    for line in last_message.splitlines():
        clean_line = line.strip()
        if clean_line.startswith("Action:"):
            # Strip the prefix to get the function call string
            action_strings.append(clean_line[7:].strip())

    if not action_strings:
        # This shouldn't happen if should_continue is working, 
        # but it's a safe guard.
        return {"messages": [ToolMessage(content="Error: No valid Actions found.", tool_call_id="err")]}

    # 2. Parallel Execution Logic
    loop = asyncio.get_event_loop()
    
    async def run_tool(action_str):
        # We wrap the sync safe_execute_tool in a thread pool 
        # to prevent blocking and allow true parallelism.
        result = await loop.run_in_executor(None, safe_execute_tool, action_str, TOOLS)
        return result

    # 3. Fire all tool calls simultaneously
    # If the bot requested 5 scrapes, they all start now.
    tasks = [run_tool(act) for act in action_strings]
    print(f"Started {len(tasks)} tool tasks")
    results = await asyncio.gather(*tasks)
    print(f"Finished {len(tasks)} tool tasks")

    # 4. Construct ToolMessages
    # LangGraph expects a 1:1 mapping of tool calls to results.
    new_messages = []
    for res in results:
        new_messages.append(ToolMessage(
            content=json.dumps(res), 
            tool_call_id=f"call_{uuid.uuid4().hex[:8]}"
        ))
    print(f"Executed {len(results)} actions in parallel.")
    return {
        "messages": new_messages,
        "current_thought": f"Executed {len(results)} actions in parallel."
    }
# --- 6. THE GRAPH LOGIC ---
def should_continue(state: AgentState):
    """
    Checks if the last message contains any valid tool triggers 
    at the start of a line.
    """
    last_message = state['messages'][-1].content
    
    # Check each line individually
    for line in last_message.splitlines():
        if line.strip().startswith("Action:"):
            return "act"
            
    return "end"

# Build the Graph
workflow = StateGraph(AgentState)
workflow.add_node("reason", reason_node)
workflow.add_node("act", act_node)

workflow.set_entry_point("reason")
workflow.add_conditional_edges(
    "reason",
    should_continue,
    {
        "act": "act",
        "end": END
    }
)
workflow.add_edge("act", "reason") # Loop back to Brain after Tool

app = workflow.compile()

async def run_agent_logic(messages: List[BaseMessage], log_callback):
    """
    Accepts a structured list of messages.
    """
    # Initialize state with the history we were given
    initial_state = {
        "messages": messages, 
        "current_thought": ""
    }
    
    final_response = ""
    config = {"recursion_limit": 100}
    
    async for event in app.astream(initial_state, config=config):
        # ... (rest of your existing loop logic stays the same)
        for node_name, state_update in event.items():
            if node_name == "reason":
                thought = state_update['current_thought']
                if "Action:" in thought:
                    # Logic to extract tool name for logging
                    try:
                        tool = thought.split('Action:')[1].split('(')[0].strip()
                        await log_callback(f"Using tool: {tool}", node="reason", data={"tool": tool})
                    except:
                        await log_callback("Thinking...", node="reason")
                else:
                    final_response = thought
            # ... act_node logic ...
            
    return final_response

import ast

def safe_execute_tool(action_str: str, available_tools: dict):
    print(f"    [PARSE] Attempting to parse: {action_str}")
    try:
        # If the LLM includes noise, ast.parse will throw a SyntaxError here
        # which we return as a result so the LLM knows it messed up.
        tree = ast.parse(action_str.strip(), mode='eval')
        
        if not isinstance(tree.body, ast.Call):
            return "Error: Your action must be a pure function call. Do not add conversational text."
        
        func_name = tree.body.func.id
        if func_name not in available_tools:
            return f"Error: Tool '{func_name}' does not exist. Did you hallucinate it?"

        # Extract arguments...
        args = []
        for arg in tree.body.args:
            if isinstance(arg, ast.Constant): args.append(arg.value)
            else: return f"Error: Argument {ast.dump(arg)} is not a literal."
            
        kwargs = {}
        for kw in tree.body.keywords:
            if isinstance(kw.value, ast.Constant): kwargs[kw.arg] = kw.value.value
            else: return f"Error: Keyword {kw.arg} is not a literal."

        print(f"    [EXEC] Running {func_name} with args: {args} kwargs: {kwargs}")
        
        # ACTUALLY CALLING THE TOOL
        result = available_tools[func_name](*args, **kwargs)
        
        print(f"    [RESULT] {func_name} execution complete.")
        return result

    except SyntaxError:
        print(f"    [ERROR] Syntax error in action string.")
        return f"Error: Syntax error in '{action_str}'. Ensure you only output the function call."
    except Exception as e:
        print(f"    [ERROR] Tool execution failed: {str(e)}")
        return f"Tool Execution Error: {str(e)}"
