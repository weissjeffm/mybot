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
from bot_utils import filter_search_results

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

    # Signal that tool execution is starting
    await log_callback("Tools started", node="act_start", data={"actions": actions})

    # Parallel Execution - handle async functions directly in the event loop
    async def run_tool(act):
        print(f"🔨 Running tool: {act}")
        if "error" in act: 
            return ToolMessage(content=act["error"], tool_call_id="err", id=act['id'])
        try:
            # Get the tool function
            tool_func = TOOLS[act["name"]]
            # Check if it's async
            if asyncio.iscoroutinefunction(tool_func):
                # Run async function directly in event loop
                result = await tool_func(*act["args"], **act["kwargs"])
            else:
                # Run sync function in thread pool
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, tool_func, *act["args"], **act["kwargs"])
            content = str(result)
            print(f"✅🔨 Tool call complete: {content[:120]}{'...' if len(content) > 120 else ''}")
            return ToolMessage(
                content=content, 
                tool_call_id=f"call_{act['id'][:8]}", 
                id=act['id'],
                artifact=result
            )
        except Exception as e:
            content = str(e)
            print(f"❌🔨 Tool call failed: {content[:120]}{'...' if len(content) > 120 else ''}")
            return ToolMessage(
                content=f"Error: {str(e)}", 
                tool_call_id="err", 
                id=act['id'],
                artifact=e
            )

    results = await asyncio.gather(*[run_tool(a) for a in actions])
    await log_callback("Tools completed", node="act_finish", data={"results": [
        {"action": {"original": action["original"]}, "status": "ok" if "Error:" not in result.content else "error"}
        for action, result in zip(actions, results)
    ]})

    return {"messages": results, "current_thought": "Tools complete."}

async def fold_node(state: AgentState):
    """Map-Reduce phase: Compresses bulky tool results into summaries."""
    log_callback = state['log_callback']
    new_messages = []
    
    for m in state["messages"]:
        
        
        # Fold any tool result larger than 3000 chars
        
        if isinstance(m, ToolMessage):
            result = m.artifact
            tool_type = result and result.get("type", "")
            if tool_type == "scrape" and len(m.content) > 3000:
                asyncio.create_task(log_callback(f"Folding bulky result ({len(m.content)} chars)...", node="fold"))
            
                summary = await fast_llm.ainvoke([
                    SystemMessage(content="Summarize this content. Only include details relevant to the conversation history."),
                    HumanMessage(content=m.content[:12000]) # Safety cap
                ])
            
                new_messages.append(ToolMessage(
                    content=f"[FOLDED SUMMARY]: {summary.content}",
                    tool_call_id=m.tool_call_id,
                    id=m.id
                ))
        
            # Filter search results for relevance
            elif tool_type == "search":
                try:
                    if result.get("status") == "ok":
                        search_results = result.get("result")

                        # Create context from conversation history
                        context = "\n".join([f"{msg.type}: {msg.content}" for msg in state["messages"][-5:] if isinstance(msg, (HumanMessage, AIMessage))])

                        # Use the utility function to filter results
                        filtered_results = await filter_search_results(search_results, context, fast_llm)

                        if len(filtered_results) != len(search_results):
                            
                            result["result"] = filtered_results
                            result["message"] = f"Found {len(filtered_results)} relevant results after filtering"

                            new_messages.append(ToolMessage(
                                content=str(result),
                                tool_call_id=m.tool_call_id,
                                id=m.id
                            ))
                        else:
                            new_messages.append(m)  # No change, keep original
                    else:
                        new_messages.append(m)
                except Exception as e:
                    print(f"⚠️ Failed to filter search results: {e}")
                    new_messages.append(m)
            else:
                new_messages.append(m)

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

