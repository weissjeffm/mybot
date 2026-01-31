import operator
import json
import uuid
import time
from typing import Annotated, TypedDict, Union, List, Any
from langchain_openai import ChatOpenAI
from langchain_core.messages import ToolMessage, SystemMessage, HumanMessage, BaseMessage, AIMessage
from langchain_core.language_models import BaseChatModel
from langgraph.graph import StateGraph, END
from tools import get_tools_dict, generate_system_prompt
import subprocess
import asyncio

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], operator.add]
    current_thought: str
    log_callback: Any
    bot_name: str
    final_signal: dict
    llm: BaseChatModel

# --- 1. CONFIGURATION ---
# We'll create a factory function to initialize the LLM with dynamic config
def create_llm(base_url, api_key):
    return ChatOpenAI(
        base_url=f"{base_url}/v1",
        api_key=api_key,
        model="qwen3-235b-a22b-instruct-2507",
        temperature=0,
        max_tokens=2048,
        timeout=600,
        max_retries=2
    )

# Initialize tools
TOOLS = get_tools_dict() # Automatically loads ssh, ipmi, etc.

# Global variable for the LLM instance, will be set by the bridge
llm = None

def set_llm_instance(base_url: str, api_key: str):
    """Set the global LLM instance with provided configuration."""
    global llm
    llm = create_llm(base_url, api_key)
    return llm


# --- 5. THE NODES ---

async def reason_node(state: AgentState):
    """The Brain: Decides what to do. Times the LLM call."""
    bot_name = state.get("bot_name", "Assistant") 
    messages = [SystemMessage(content=generate_system_prompt(bot_name))] + state['messages']
    
    llm_instance = state.get("llm")
    if not llm_instance:
        raise ValueError("LLM instance missing in state. Ensure set_llm_instance() was called and llm is passed in initial state.")
    
    # Time the LLM call
    start_time = time.time()
    response = await llm_instance.ainvoke(messages)
    end_time = time.time()
    llm_time = end_time - start_time
    
    # Log via callback
    log_callback = state['log_callback']
    asyncio.create_task(log_callback(f"LLM 'reason' call took {llm_time:.2f}s", node="reason", data={"duration": llm_time}))
    
    content = response.content
    if content.strip().startswith("Action:"):
        asyncio.create_task(log_callback("Formulating plan...", node="reason"))
        
    return {"messages": [response], "current_thought": content}

# In langgraph_agent.py
import asyncio
import re
import uuid
import json

async def act_node(state: AgentState):
    """The Hands: Parallel Execution with non-blocking UI updates."""
    last_message = state['messages'][-1].content
    log_callback = state['log_callback']
    
    # 1. Parse actions into structured data
    actions = []
    for line in last_message.splitlines():
        if line.strip().startswith("Action:"):
            action_str = line.strip()[7:].strip()
            try:
                # Parse the action string into structured data
                tree = ast.parse(action_str.strip(), mode='eval')
                if not isinstance(tree.body, ast.Call):
                    raise ValueError("Action must be a pure function call")
                
                fn_name = tree.body.func.id
                args = []
                for arg in tree.body.args:
                    if isinstance(arg, ast.Constant): 
                        args.append(arg.value)
                    else:
                        raise ValueError(f"Argument {ast.dump(arg)} is not a literal")
                
                kwargs = {}
                for kw in tree.body.keywords:
                    if isinstance(kw.value, ast.Constant): 
                        kwargs[kw.arg] = kw.value.value
                    else:
                        raise ValueError(f"Keyword {kw.arg} is not a literal")
                
                actions.append({
                    "name": fn_name,
                    "args": args,
                    "kwargs": kwargs,
                    "original": action_str
                })
            except Exception as e:
                actions.append({
                    "name": "error",
                    "error": f"Parse error: {str(e)}",
                    "original": action_str
                })

    if not actions:
        return {"messages": [ToolMessage(content="Error", tool_call_id="err")]}

    # 2. SIGNAL UI: Non-blocking "Start" signal with structured data
    asyncio.create_task(log_callback(
        text="Starting tools...", 
        node="act_start", 
        data={"actions": actions}
    ))

    # 3. Parallel Tool Execution with timing
    loop = asyncio.get_event_loop()
    tasks = []
    for action in actions:
        async def timed_tool_call(act):
            if "error" in act:
                return act["error"]
                
            start_time = time.time()
            try:
                result = await loop.run_in_executor(None, safe_execute_tool, act["original"], TOOLS)
            except Exception as e:
                result = f"Tool Execution Error: {str(e)}"
            end_time = time.time()
            tool_time = end_time - start_time
            
            # Non-blocking log with timing
            tool_display_name = act["name"].replace('_', ' ').capitalize()
            asyncio.create_task(log_callback(
                f"Tool '{tool_display_name}' took {tool_time:.2f}s", 
                node="act", 
                data={"tool": tool_display_name, "time": tool_time}
            ))
            return result

        tasks.append(timed_tool_call(action))

    results = await asyncio.gather(*tasks)

    # 4. SIGNAL UI: Non-blocking "Finish" signal with structured results
    batch_results = []
    for i, res in enumerate(results):
        status = "ok" if not str(res).startswith("Error") else "error"
        batch_results.append({
            "action": actions[i],  # Keep full structured action
            "status": status,
            "result": res
        })
    
    asyncio.create_task(log_callback(
        text="Batch complete.", 
        node="act_finish", 
        data={"results": batch_results}
    ))

    # 5. Build ToolMessages for the next reasoning step
    new_messages = [ToolMessage(content=str(r), tool_call_id=f"call_{uuid.uuid4().hex[:8]}") for r in results]
    
    # Check if any result is a TOPIC_CHANGE signal
    topic_change_signal = None
    for r in results:
        if isinstance(r, dict) and r.get("event") == "TOPIC_CHANGE":
            topic_change_signal = r
            break

    if topic_change_signal:
        return {
            "messages": new_messages,
            "current_thought": "Tools complete.",
            "final_signal": topic_change_signal
        }

    return {"messages": new_messages, "current_thought": "Tools complete."}

async def run_agent_logic(initial_state: AgentState):
    """Run the agent and return both final response and any topic change signals."""
    final_response = ""
    topic_change_signal = None
    config = {"recursion_limit": 100}
    
    # Create a clean state with required fields
    state = {
        "messages": initial_state["messages"],
        "log_callback": initial_state["log_callback"],
        "bot_name": initial_state.get("bot_name", "Assistant"),
        "llm": initial_state.get("llm") or llm  # Use passed llm or global
    }
    
    # Ensure llm is present
    if not state["llm"]:
        raise ValueError("LLM instance not set. Call set_llm_instance() first.")

    async for event in app.astream(state, config=config):
        for node_name, state_update in event.items():
            if node_name == "reason":
                thought = state_update['current_thought']
                if "Action:" in thought:
                    await state["log_callback"]("Formulating plan...", node="reason")
                else:
                    final_response = thought
            elif node_name == "act":
                if "final_signal" in state_update:
                    signal = state_update["final_signal"]
                    if isinstance(signal, dict) and signal.get("event") == "TOPIC_CHANGE":
                        topic_change_signal = signal

    return {
        "response": final_response,
        "topic_change": topic_change_signal
    }

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

        print(f"🔧 Executing tool: {func_name}({', '.join(repr(a) for a in args)}{', ' + ', '.join(f'{k}={repr(v)}' for k, v in kwargs.items()) if kwargs else ''})")
        
        # ACTUALLY CALLING THE TOOL
        result = available_tools[func_name](*args, **kwargs)
        
        print(f"✅ Tool {func_name} completed")
        if result is not None:
            result_str = str(result)
            print(f"   ↳ {result_str[:200]}{'...' if len(result_str) > 200 else ''}")
        return result

    except SyntaxError:
        print(f"    [ERROR] Syntax error in action string.")
        print(f"❌ Syntax error in tool call: {action_str[:100]}")
        return f"Error: Syntax error in '{action_str}'. Ensure you only output the function call."
    except Exception as e:
        print(f"    [ERROR] Tool execution failed: {str(e)}")
        print(f"    [ERROR] Tool execution failed: {str(e)}")
        print(f"❌ Tool {func_name} failed: {str(e)[:200]}")
        return f"Tool Execution Error: {str(e)}"
