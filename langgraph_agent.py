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

async def act_node(state: AgentState):
    """The Hands: Returns a structured ToolMessage."""
    last_message = state['messages'][-1].content
    
    # Generate a unique ID so the model can track which result belongs to which call
    tool_call_id = str(uuid.uuid4())

    if "Action:" in last_message:
        action_str = last_message.split("Action:")[-1].strip()
        
        # This returns your DICT: {"status": "ok", "message": "Found 5 results", ...}
        result_dict = safe_execute_tool(action_str, TOOLS)
        
        return {
            "messages": [ToolMessage(content=json.dumps(result_dict), tool_call_id=tool_call_id)],
            "current_thought": result_dict.get("message") or "Action complete"
        }
    
    return {"messages": [ToolMessage(content="Error", tool_call_id=tool_call_id)]}
# --- 6. THE GRAPH LOGIC ---

def should_continue(state: AgentState):
    """Decides if we loop back or stop."""
    last_message = state['current_thought']
    
    # If the model wrote "Action:", we need to ACT.
    if "Action:" in last_message:
        return "act"
    # Otherwise, it's just talking to the user. Stop.
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

# --- 7. THE EXTERNAL HOOK (Called by bridge.py) ---
async def run_agent_logic(user_input: str, log_callback):
    
    # Initialize the state
    initial_state = {"messages": [HumanMessage(content=user_input)], "current_thought": ""}
    
    final_response = ""

    config = {"recursion_limit": 100}
    
    # Run the graph step-by-step
    async for event in app.astream(initial_state, config=config):
        for node_name, state_update in event.items():
            
            # LOGGING: This sends the "Thinking..." messages to Matrix
            if node_name == "reason":
                thought = state_update['current_thought']
                # Clean up the output so we don't spam the whole thought block
                if "Action:" in thought:
                    tool = thought.split('Action:')[-1].strip()
                    await log_callback(f"Using tool: {tool}", node="reason", data={"tool": tool})
                else:
                    final_response = thought # Capture the final answer
            elif node_name == "act":
                # 1. Get the ToolMessage object
                msg_obj = state_update['messages'][0]
                
                # 2. Extract the actual dictionary content
                # LangChain stores the dict in .content
                raw_data = msg_obj.content 
                
                # 3. Get the 1-line summary we passed in current_thought
                summary = state_update.get("current_thought", "Action complete")

                # 4. Reconstruct the 'data' dict the callback expects
                # If raw_data is a string (due to auto-serialization), parse it
                if isinstance(raw_data, str):
                    try:
                        import json
                        data_for_callback = json.loads(raw_data)
                    except:
                        data_for_callback = {"status": "error", "message": raw_data}
                else:
                    data_for_callback = raw_data

                # 5. Call the callback with the surgical 1-liner
                await log_callback(
                    text=summary, 
                    node="act",
                    data=data_for_callback
                )                    
    return final_response

import ast

def safe_execute_tool(action_str: str, available_tools: dict):
    """
    Parses a string like 'run_cmd("ls", user="root")' safely.
    It ONLY allows function calls to functions in available_tools.
    It ONLY allows literal arguments (strings, numbers, booleans, None).
    """
    try:
        # 1. Parse the string into an AST node (mode='eval' expects an expression)
        tree = ast.parse(action_str.strip(), mode='eval')
        
        # 2. Guardrail: The root must be a Function Call
        if not isinstance(tree.body, ast.Call):
            return "Error: Action must be a direct function call."
        
        # 3. Guardrail: The function name must be in our whitelist
        func_name = tree.body.func.id
        if func_name not in available_tools:
            return f"Error: Tool '{func_name}' is not defined or allowed."
        
        # 4. Extract Positional Arguments
        args = []
        for arg in tree.body.args:
            # We only allow "Constant" values (str, int, float, bool, None)
            # We reject variables, math operations, or nested calls
            if isinstance(arg, ast.Constant): 
                args.append(arg.value)
            else:
                return f"Error: Argument '{ast.dump(arg)}' is unsafe. Use literals only."
        
        # 5. Extract Keyword Arguments
        kwargs = {}
        for keyword in tree.body.keywords:
            if isinstance(keyword.value, ast.Constant):
                kwargs[keyword.arg] = keyword.value.value
            else:
                return f"Error: Keyword argument '{keyword.arg}' is unsafe."
        
        # 6. Execute! 
        # We manually call the Python function with the extracted safe values.
        return available_tools[func_name](*args, **kwargs)

    except SyntaxError:
        return "Error: Invalid Python syntax in tool call."
    except Exception as e:
        return f"Tool Execution Error: {str(e)}"
    
