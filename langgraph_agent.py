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
    max_tokens=2048,
    timeout=600,
    max_retries=2
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

# langgraph_agent.py refinements

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], operator.add]
    current_thought: str
    log_callback: any # Added to state so nodes can signal UI updates

async def act_node(state: AgentState):
    """Executes tools in parallel and signals the UI Status Board."""
    last_message = state['messages'][-1].content
    log_callback = state['log_callback']
    
    # 1. Strict extraction of Action lines
    action_strings = []
    for line in last_message.splitlines():
        if line.strip().startswith("Action:"):
            action_strings.append(line.strip()[7:].strip())

    if not action_strings:
        return {"messages": [ToolMessage(content="Error: No actions found", tool_call_id="err")]}

    # 2. SIGNAL UI: Start Parallel Batch (Gears)
    await log_callback(text="Starting batch...", node="act_start", data={"actions": action_strings})

    # 3. Parallel Execution
    loop = asyncio.get_event_loop()
    tasks = [loop.run_in_executor(None, safe_execute_tool, act, TOOLS) for act in action_strings]
    results = await asyncio.gather(*tasks)

    # 4. SIGNAL UI: Batch Complete (Checkboxes)
    # Mapping results back to actions for the UI
    batch_results = []
    for i, res in enumerate(results):
        status = "ok" if not str(res).startswith("Error") else "error"
        batch_results.append({"action": action_strings[i], "status": status})
    
    await log_callback(text="Batch complete.", node="act_finish", data={"results": batch_results})

    # 5. Return ToolMessages
    new_messages = []
    for res in results:
        new_messages.append(ToolMessage(
            content=json.dumps(res) if isinstance(res, dict) else str(res), 
            tool_call_id=f"call_{uuid.uuid4().hex[:8]}"
        ))
    
    return {"messages": new_messages, "current_thought": "Tools complete."}

async def run_agent_logic(messages: List[BaseMessage], log_callback):
    # Pass log_callback into the initial state
    initial_state = {
        "messages": messages, 
        "current_thought": "",
        "log_callback": log_callback
    }
    
    final_response = ""
    config = {"recursion_limit": 100}
    
    async for event in app.astream(initial_state, config=config):
        for node_name, state_update in event.items():
            if node_name == "reason":
                thought = state_update['current_thought']
                if "Action:" in thought:
                    # Optional: Log that the brain is thinking
                    await log_callback("Formulating plan...", node="reason")
                else:
                    final_response = thought
    return final_response

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
