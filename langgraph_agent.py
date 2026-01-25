import operator
import json
import uuid
import time
from typing import Annotated, TypedDict, Union, List
from langchain_openai import ChatOpenAI
from langchain_core.messages import ToolMessage, SystemMessage, HumanMessage, BaseMessage, AIMessage
from langgraph.graph import StateGraph, END
from tools import get_tools_dict, generate_system_prompt
import subprocess
import asyncio

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

# --- 3. THE STATE ---
class AgentState(TypedDict):
    # 'operator.add' means: when we return new messages, APPEND them to this list
    messages: Annotated[List[BaseMessage], operator.add]
    current_thought: str
    log_callback: any

# --- 5. THE NODES ---

async def reason_node(state: AgentState):
    """The Brain: Decides what to do. Times the LLM call."""
    print(f"🧠 Reasoning on message ({sum(len(str(m.content)) for m in state['messages']):,} chars): {state['messages'][-1].content[:100]}")
    bot_name = state.get("bot_name", "Assistant") 
    messages = [SystemMessage(content=generate_system_prompt(bot_name))] + state['messages']
    
    # Time the LLM call
    start_time = time.time()
    response = await llm.ainvoke(messages)
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

# langgraph_agent.py refinements

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], operator.add]
    current_thought: str
    log_callback: any # Added to state so nodes can signal UI updates
    bot_name: str
    final_signal: dict  # Optional: signal from tools
    
async def act_node(state: AgentState):
    """The Hands: Parallel Execution with non-blocking UI updates."""
    last_message = state['messages'][-1].content
    log_callback = state['log_callback']
    
    # 1. Parse actions
    action_strings = [line.strip()[7:].strip() for line in last_message.splitlines() if line.strip().startswith("Action:")]

    if not action_strings:
        return {"messages": [ToolMessage(content="Error", tool_call_id="err")]}

    # 2. SIGNAL UI: Non-blocking "Start" signal
    # create_task ensures we don't wait for Matrix to respond before starting tools
    asyncio.create_task(log_callback(text="Starting tools...", node="act_start", data={"actions": action_strings}))

    # 3. Parallel Tool Execution (The priority!) with timing
    loop = asyncio.get_event_loop()
    tasks = []
    for act in action_strings:
        # Wrap each tool call with timing
        async def timed_tool_call(action):
            start_time = time.time()
            result = await loop.run_in_executor(None, safe_execute_tool, action, TOOLS)
            end_time = time.time()
            tool_time = end_time - start_time
            # Non-blocking log with timing
            asyncio.create_task(log_callback(f"Tool '{action.split('(')[0]}' took {tool_time:.2f}s", node="act_tool", data={"action": action, "duration": tool_time}))
            return result

        tasks.append(timed_tool_call(act))

    results = await asyncio.gather(*tasks)

    # 4. SIGNAL UI: Non-blocking "Finish" signal
    batch_results = []
    for i, res in enumerate(results):
        status = "ok" if not str(res).startswith("Error") else "error"
        batch_results.append({"action": action_strings[i], "status": status})
    
    asyncio.create_task(log_callback(text="Batch complete.", node="act_finish", data={"results": batch_results}))

    # 5. Build ToolMessages for the next reasoning step
    new_messages = [ToolMessage(content=str(r), tool_call_id=f"call_{uuid.uuid4().hex[:8]}") for r in results]
    
    # Check if any result is a TOPIC_CHANGE signal
    topic_change_signal = None
    for r in results:
        if isinstance(r, dict) and r.get("event") == "TOPIC_CHANGE":
            topic_change_signal = r
            break

    # If topic change, store it in state so run_agent_logic can return it
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
    
    async for event in app.astream(initial_state, config=config):
        for node_name, state_update in event.items():
            if node_name == "reason":
                thought = state_update['current_thought']
                if "Action:" in thought:
                    await initial_state["log_callback"]("Formulating plan...", node="reason")
                else:
                    final_response = thought
            elif node_name == "act":
                # Check for final_signal in state update
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
