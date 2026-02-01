import operator
import json
import uuid
import time
import ast
import asyncio
from typing import Annotated, TypedDict, Union, List, Any
from langchain_openai import ChatOpenAI
from langchain_core.messages import ToolMessage, SystemMessage, HumanMessage, BaseMessage, AIMessage
from langchain_core.language_models import BaseChatModel
from langgraph.graph import StateGraph, END
from tools import get_tools_dict, generate_system_prompt

# --- 1. SMART REDUCER ---
def reduce_messages(left: List[BaseMessage], right: Union[BaseMessage, List[BaseMessage]]):
    """
    Appends new messages, but if a message has an existing ID, 
    it replaces the old one (Context Folding).
    """
    if not isinstance(right, list):
        right = [right]
    
    # Map current messages by ID; generate ID if missing
    new_messages = {m.id if m.id else str(uuid.uuid4()): m for m in left}
    
    for m in right:
        m_id = m.id if m.id else str(uuid.uuid4())
        new_messages[m_id] = m
        
    return list(new_messages.values())

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], reduce_messages]
    current_thought: str
    log_callback: Any
    bot_name: str
    final_signal: dict
    llm: BaseChatModel

# --- 2. CONFIGURATION ---
def create_llm(base_url, api_key):
    return ChatOpenAI(
        base_url=f"{base_url}/v1",
        api_key=api_key,
        model="qwen3-235b-a22b-instruct-2507",
        temperature=0,
        max_tokens=2048,
        timeout=600
    )

def create_fast_llm(base_url, api_key):
    """Lighter model for background summarization tasks."""
    return ChatOpenAI(
        base_url=f"{base_url}/v1",
        api_key=api_key,
        model="qwen_qwen3-4b-instruct-2507", 
        temperature=0,
        max_tokens=512
    )

TOOLS = get_tools_dict()
llm = None
fast_llm = None

def set_llm_instance(base_url: str, api_key: str):
    global llm, fast_llm
    llm = create_llm(base_url, api_key)
    fast_llm = create_fast_llm(base_url, api_key)
    return llm

# --- 3. NODES ---

async def reason_node(state: AgentState):
    bot_name = state.get("bot_name", "Assistant") 
    # System prompt injected at every reasoning cycle
    messages = [SystemMessage(content=generate_system_prompt(bot_name))] + state['messages']
    
    llm_instance = state.get("llm") or llm
    
    start_time = time.time()
    response = await llm_instance.ainvoke(messages)
    llm_time = time.time() - start_time
    
    log_callback = state['log_callback']
    asyncio.create_task(log_callback(f"Reasoning took {llm_time:.2f}s", node="reason"))
    
    return {"messages": [response], "current_thought": response.content}

async def act_node(state: AgentState):
    """Executes tools in parallel with concurrency safety."""
    last_message = state['messages'][-1].content
    log_callback = state['log_callback']
    
    # Parsing logic
    actions = []
    for line in last_message.splitlines():
        if line.strip().startswith("Action:"):
            action_str = line.strip()[7:].strip()
            try:
                tree = ast.parse(action_str, mode='eval')
                fn_name = tree.body.func.id
                args = [arg.value for arg in tree.body.args if isinstance(arg, ast.Constant)]
                kwargs = {kw.arg: kw.value.value for kw in tree.body.keywords if isinstance(kw.value, ast.Constant)}
                
                actions.append({"name": fn_name, "args": args, "kwargs": kwargs, "original": action_str, "id": str(uuid.uuid4())})
            except Exception as e:
                actions.append({"name": "error", "error": f"Parse error: {str(e)}", "original": action_str})

    if not actions:
        return {"messages": [ToolMessage(content="No actions found", tool_call_id="err")]}

    # Parallel Execution with Semaphore to prevent SEGVs/VRAM spikes
    sem = asyncio.Semaphore(5)
    loop = asyncio.get_event_loop()

    async def run_tool(act):
        async with sem:
            if "error" in act: return act["error"]
            try:
                # Use a unique ID for the tool message so we can 'fold' it later
                res = await loop.run_in_executor(None, safe_execute_tool, act["original"], TOOLS)
                return ToolMessage(content=str(res), tool_call_id=f"call_{act['id'][:8]}", id=act['id'])
            except Exception as e:
                return ToolMessage(content=f"Error: {str(e)}", tool_call_id="err", id=act['id'])

    results = await asyncio.gather(*[run_tool(a) for a in actions])
    return {"messages": results, "current_thought": "Tools complete."}

async def fold_node(state: AgentState):
    """Map-Reduce phase: Compresses bulky tool results into summaries."""
    log_callback = state['log_callback']
    new_messages = []
    
    for m in state["messages"]:
        # Fold any tool result larger than 3000 chars
        if isinstance(m, ToolMessage) and len(m.content) > 3000:
            asyncio.create_task(log_callback(f"Folding bulky result ({len(m.content)} chars)...", node="fold"))
            
            summary = await fast_llm.ainvoke([
                SystemMessage(content="Summarize this content into technical bullet points. Only include details relevant to the conversation history."),
                HumanMessage(content=m.content[:12000]) # Safety cap
            ])
            
            # Re-use the ID to trigger the 'reduce_messages' swap
            new_messages.append(ToolMessage(
                content=f"[FOLDED SUMMARY]: {summary.content}",
                tool_call_id=m.tool_call_id,
                id=m.id
            ))

    return {"messages": new_messages} if new_messages else {}

# --- 4. GRAPH CONSTRUCTION ---

def should_continue(state: AgentState):
    last_message = state['messages'][-1].content
    for line in last_message.splitlines():
        if line.strip().startswith("Action:"):
            return "act"
    return "end"

workflow = StateGraph(AgentState)
workflow.add_node("reason", reason_node)
workflow.add_node("act", act_node)
workflow.add_node("fold", fold_node)

workflow.set_entry_point("reason")
workflow.add_conditional_edges("reason", should_continue, {"act": "act", "end": END})
workflow.add_edge("act", "fold")
workflow.add_edge("fold", "reason")

app = workflow.compile()

async def run_agent_logic(initial_state: AgentState):
    """
    Orchestrates the Graph execution, ensures 'fold' events are 
    processed, and returns the final synthesized response.
    """
    final_response = ""
    topic_change_signal = None
    
    # Increase recursion limit because folding adds an extra step per loop
    config = {"recursion_limit": 150, "configurable": {"thread_id": str(uuid.uuid4())}}
    
    # 1. Prepare State
    state = {
        "messages": initial_state["messages"],
        "log_callback": initial_state["log_callback"],
        "bot_name": initial_state.get("bot_name", "Assistant"),
        "llm": initial_state.get("llm") or llm 
    }

    # 2. Execute Stream
    async for event in app.astream(state, config=config):
        for node_name, state_update in event.items():
            
            # Update UI via callback for each phase
            if node_name == "reason":
                thought = state_update.get('current_thought', "")
                if "Action:" not in thought:
                    final_response = thought
                else:
                    await state["log_callback"]("Thinking...", node="reason")
            
            elif node_name == "act":
                await state["log_callback"]("Processing tool results...", node="act")
                if "final_signal" in state_update:
                    topic_change_signal = state_update["final_signal"]
            
            elif node_name == "fold":
                await state["log_callback"]("Cleaning up context (folding)...", node="fold")

    # 3. Final Return
    return {
        "response": final_response,
        "topic_change": topic_change_signal
    }

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
