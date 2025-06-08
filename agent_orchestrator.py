# agent_orchestrator.py
import os
import subprocess
import json
import threading
import time
import queue # For thread-safe communication of log lines
import shutil # For safe directory removal
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold, Tool, FunctionDeclaration
from dotenv import load_dotenv

# Load environment variables from .env file (for GEMINI_API_KEY)
load_dotenv()

# --- Configuration ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY not found. Please set it in your .env file.")

genai.configure(api_key=GEMINI_API_KEY)

MODEL_NAME = "gemini-2.0-flash" # VERIFY THIS!
safety_settings = [
    {"category": HarmCategory.HARM_CATEGORY_HARASSMENT, "threshold": HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE},
    {"category": HarmCategory.HARM_CATEGORY_HATE_SPEECH, "threshold": HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE},
    {"category": HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, "threshold": HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE},
    {"category": HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, "threshold": HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE},
]
generation_config_settings = genai.types.GenerationConfig(
    temperature=0.6, 
)
llm_model = genai.GenerativeModel(
    MODEL_NAME,
    safety_settings=safety_settings,
    generation_config=generation_config_settings
)


CURRENT_PROJECT_PATH = None
NG_SERVE_PROCESS = None 
MAX_ERROR_FIX_ATTEMPTS = 3 
# Optional delay before sending tool results to Gemini, to help with rapid-fire RPM limits
TOOL_RESPONSE_DELAY_SECONDS = 1 # Set to 0 or None to disable

conversation_history_for_this_run = []


# --- Toolset (Local Functions the LLM can request to use) ---

def _read_stream_to_queue_and_print(stream, stream_name, output_queue, stop_event):
    try:
        for line_bytes in iter(stream.readline, b''):
            if stop_event.is_set() and output_queue.empty(): 
                break
            line = line_bytes.decode('utf-8', errors='replace').strip()
            if line:
                print(f"[ng serve {stream_name.upper()}] {line}") 
                output_queue.put(f"[{stream_name.upper()}] {line}") 
        stream.close()
    except Exception as e:
        error_message = f"Error reading {stream_name} for ng serve: {e}"
        print(error_message)
        output_queue.put(f"[{stream_name.upper()} ERROR] {error_message}")

def execute_shell_command(command: str, working_directory: str = None) -> dict:
    global CURRENT_PROJECT_PATH 
    resolved_working_directory = working_directory
    if resolved_working_directory is None:
        if CURRENT_PROJECT_PATH:
            resolved_working_directory = CURRENT_PROJECT_PATH
        else:
            if "ng new" not in command: 
                print("Error: No working directory specified for execute_shell_command and CURRENT_PROJECT_PATH is not set.")
                return {"stdout": "", "stderr": "Error: Working directory not set.", "exit_code": 1}

    print(f"Executing command (will wait for completion): '{command}' in '{resolved_working_directory or 'parent dir for ng new'}'")
    is_ng_new_command = "ng new" in command 
    original_project_path_for_ng_new = resolved_working_directory 

    try:
        process = subprocess.run(
            command, shell=True, cwd=resolved_working_directory,
            capture_output=True, check=False, text=False 
        )
        stdout_str = process.stdout.decode('utf-8', errors='replace') if process.stdout else ""
        stderr_str = process.stderr.decode('utf-8', errors='replace') if process.stderr else ""
        print(f"STDOUT:\n{stdout_str}")
        if stderr_str: print(f"STDERR:\n{stderr_str}")
        
        result_dict = {"stdout": stdout_str, "stderr": stderr_str, "exit_code": process.returncode}

        if is_ng_new_command and process.returncode == 0:
            parts = command.split()
            project_name = None
            try:
                ng_new_index = parts.index("new")
                if ng_new_index + 1 < len(parts):
                    project_name = parts[ng_new_index + 1]
                    if project_name.startswith("--"): project_name = None 
            except ValueError:
                pass 

            if project_name and original_project_path_for_ng_new:
                new_project_full_path = os.path.join(original_project_path_for_ng_new, project_name)
                if os.path.isdir(new_project_full_path):
                    CURRENT_PROJECT_PATH = new_project_full_path
                    print(f"[AGENT] 'ng new' successful. CURRENT_PROJECT_PATH updated to: {CURRENT_PROJECT_PATH}")
                    result_dict["new_project_path_set"] = CURRENT_PROJECT_PATH
                    # Signal that system prompt context needs update for the chat session
                    result_dict["context_changed_project_path"] = True 
                else:
                    print(f"[AGENT] 'ng new' reported success, but project directory '{new_project_full_path}' not found. CURRENT_PROJECT_PATH not updated.")
                    result_dict["stderr"] = (result_dict.get("stderr","") + " Warning: Project directory not found post-execution.").strip()
        return result_dict
    except FileNotFoundError:
        err_msg = f"Error: Command or path incorrect. Could not execute in '{resolved_working_directory}'."
        print(err_msg)
        return {"stdout": "", "stderr": err_msg, "exit_code": 127}
    except Exception as e:
        err_msg = f"An error occurred while executing command '{command}': {e}"
        print(err_msg)
        return {"stdout": "", "stderr": err_msg, "exit_code": 1}

def start_angular_serve_and_get_initial_output(command: str, working_directory: str = None, monitor_duration_seconds: int = 20) -> dict:
    global NG_SERVE_PROCESS
    resolved_working_directory = working_directory
    if resolved_working_directory is None:
        if CURRENT_PROJECT_PATH: resolved_working_directory = CURRENT_PROJECT_PATH
        else: return {"status": "error", "message": "Working directory not set.", "pid": None, "initial_stdout_log": "", "initial_stderr_log": ""}
    if NG_SERVE_PROCESS and NG_SERVE_PROCESS.poll() is None:
        msg = f"'ng serve' (PID: {NG_SERVE_PROCESS.pid}) seems to be already running. Please use 'stop server' first if you want to restart."
        print(f"[AGENT] {msg}")
        return {"status": "already_running", "command": "ng serve", "pid": NG_SERVE_PROCESS.pid, "message": msg, "initial_stdout_log": "", "initial_stderr_log": ""}
    print(f"Starting Angular serve: '{command}' in '{resolved_working_directory}'")
    print(f"Monitoring initial output for ~{monitor_duration_seconds} seconds...")
    collected_stdout, collected_stderr = [], []
    try:
        NG_SERVE_PROCESS = subprocess.Popen(command, shell=True, cwd=resolved_working_directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False)
        print(f"Process '{command}' started with PID: {NG_SERVE_PROCESS.pid}.")
        stdout_queue, stderr_queue, stop_event = queue.Queue(), queue.Queue(), threading.Event()
        stdout_thread = threading.Thread(target=_read_stream_to_queue_and_print, args=(NG_SERVE_PROCESS.stdout, "stdout", stdout_queue, stop_event))
        stderr_thread = threading.Thread(target=_read_stream_to_queue_and_print, args=(NG_SERVE_PROCESS.stderr, "stderr", stderr_queue, stop_event))
        stdout_thread.daemon = stderr_thread.daemon = True
        stdout_thread.start(); stderr_thread.start()
        start_time, status = time.time(), "compiling"
        while time.time() - start_time < monitor_duration_seconds:
            if NG_SERVE_PROCESS.poll() is not None: status = "error_during_startup"; break
            try:
                while not stdout_queue.empty():
                    line = stdout_queue.get_nowait(); collected_stdout.append(line)
                    if "Compiled successfully" in line or "successfully built" in line.lower(): status = "compiled_successfully"; break
                while not stderr_queue.empty():
                    line = stderr_queue.get_nowait(); collected_stderr.append(line)
                    if "ERROR" in line or "Error" in line: status = "error_during_startup" 
            except queue.Empty: pass
            if status == "compiled_successfully": break
            time.sleep(0.2)
        stop_event.set()
        stdout_thread.join(timeout=1); stderr_thread.join(timeout=1)
        while not stdout_queue.empty(): collected_stdout.append(stdout_queue.get_nowait())
        while not stderr_queue.empty(): collected_stderr.append(stderr_queue.get_nowait())
        final_message = f"Angular serve command '{command}' initiated. "
        if status == "compiled_successfully": final_message += "Initial compilation reported as successful. Monitor terminal for ongoing logs. App should be at http://localhost:4200."
        elif status == "error_during_startup": final_message += "Errors detected during initial startup. See logs. Monitor terminal for ongoing logs."
        else: status = "compiling_or_timeout"; final_message += f"Initial monitoring period of {monitor_duration_seconds}s ended. Status is '{status}'. Monitor terminal for ongoing logs and report any errors. App may be at http://localhost:4200 if compilation completes."
        return {"status": status, "command": command, "pid": NG_SERVE_PROCESS.pid if NG_SERVE_PROCESS else None, "message": final_message, "initial_stdout_log": "\n".join(collected_stdout), "initial_stderr_log": "\n".join(collected_stderr)}
    except FileNotFoundError: err_msg = f"Error: Command or path incorrect. Could not execute in '{resolved_working_directory}'."; print(err_msg); return {"status": "error", "message": err_msg, "pid": None, "initial_stdout_log": "", "initial_stderr_log": err_msg}
    except Exception as e: err_msg = f"An error occurred while starting command '{command}': {e}"; print(err_msg); return {"status": "error", "message": err_msg, "pid": None, "initial_stdout_log": "", "initial_stderr_log": str(e)}

def stop_angular_server() -> dict:
    global NG_SERVE_PROCESS
    if NG_SERVE_PROCESS and NG_SERVE_PROCESS.poll() is None:
        print(f"[AGENT] Attempting to terminate 'ng serve' process (PID: {NG_SERVE_PROCESS.pid})...")
        NG_SERVE_PROCESS.terminate()
        try:
            NG_SERVE_PROCESS.wait(timeout=5); print("[AGENT] 'ng serve' process terminated."); NG_SERVE_PROCESS = None
            return {"status": "stopped", "message": "'ng serve' process terminated successfully."}
        except subprocess.TimeoutExpired:
            print("[AGENT] 'ng serve' process did not terminate gracefully, attempting to kill."); NG_SERVE_PROCESS.kill(); NG_SERVE_PROCESS.wait(); NG_SERVE_PROCESS = None; print("[AGENT] 'ng serve' process killed.")
            return {"status": "killed", "message": "'ng serve' process killed."}
        except Exception as e: print(f"[AGENT] Error stopping 'ng serve': {e}"); return {"status": "error", "message": f"Error stopping 'ng serve': {e}"}
    else: print("[AGENT] 'ng serve' process not found or not running."); return {"status": "not_running", "message": "'ng serve' process not found or not running."}

def read_file(relative_filepath: str) -> str:
    if not CURRENT_PROJECT_PATH: return "Error: CURRENT_PROJECT_PATH is not set. Cannot read file."
    full_path = os.path.join(CURRENT_PROJECT_PATH, relative_filepath)
    print(f"Reading file: {full_path}")
    try:
        with open(full_path, 'r', encoding='utf-8') as f: return f.read()
    except FileNotFoundError: return f"Error: File not found at {full_path}"
    except Exception as e: return f"Error reading file {full_path}: {e}"

def write_file(relative_filepath: str, content: str) -> bool:
    if not CURRENT_PROJECT_PATH: print("Error: CURRENT_PROJECT_PATH is not set. Cannot write file."); return False
    full_path = os.path.join(CURRENT_PROJECT_PATH, relative_filepath)
    print(f"Writing to file: {full_path}")
    try:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, 'w', encoding='utf-8') as f: f.write(content)
        print(f"Successfully wrote to {full_path}"); return True
    except Exception as e: print(f"Error writing file {full_path}: {e}"); return False

def ask_user_confirmation(prompt_message: str) -> bool:
    while True:
        response = input(f"CONFIRMATION REQUIRED BY AGENT: {prompt_message} (yes/no): ").lower().strip()
        if response == 'yes': return True
        elif response == 'no': return False
        else: print("Invalid input. Please enter 'yes' or 'no'.")

def list_directory_contents(relative_path: str = ".") -> dict:
    if not CURRENT_PROJECT_PATH:
        return {"error": "CURRENT_PROJECT_PATH is not set.", "contents": []}
    full_path = os.path.join(CURRENT_PROJECT_PATH, relative_path)
    print(f"Listing directory: {full_path}")
    try:
        items = os.listdir(full_path)
        contents = []
        for item in items:
            item_path = os.path.join(full_path, item)
            item_type = "directory" if os.path.isdir(item_path) else "file"
            contents.append({"name": item, "type": item_type})
        return {"success": True, "path": full_path, "contents": contents}
    except FileNotFoundError:
        return {"error": f"Directory not found at {full_path}", "contents": []}
    except Exception as e:
        return {"error": f"Error listing directory {full_path}: {e}", "contents": []}

def delete_file_or_directory(relative_path: str) -> dict:
    if not CURRENT_PROJECT_PATH:
        return {"status": "error", "message": "CURRENT_PROJECT_PATH is not set."}
    full_path = os.path.join(CURRENT_PROJECT_PATH, relative_path)
    print(f"[DANGER] Attempting to delete: {full_path}")
    if not os.path.exists(full_path):
        return {"status": "error", "message": f"Path not found: {full_path}"}
    try:
        if os.path.isfile(full_path):
            os.remove(full_path); message = f"Successfully deleted file: {full_path}"
            print(f"[AGENT] {message}"); return {"status": "success", "message": message}
        elif os.path.isdir(full_path):
            shutil.rmtree(full_path); message = f"Successfully deleted directory (and its contents): {full_path}"
            print(f"[AGENT] {message}"); return {"status": "success", "message": message}
        else: return {"status": "error", "message": f"Path is not a file or directory: {full_path}"}
    except Exception as e:
        error_message = f"Error deleting {full_path}: {e}"
        print(f"[AGENT] {error_message}"); return {"status": "error", "message": error_message}


available_tools_python_functions = {
    "execute_shell_command": execute_shell_command,
    "start_angular_serve_and_get_initial_output": start_angular_serve_and_get_initial_output,
    "stop_angular_server": stop_angular_server,
    "write_file": write_file,
    "read_file": read_file,
    "list_directory_contents": list_directory_contents, 
    "delete_file_or_directory": delete_file_or_directory,
    "ask_user_confirmation": ask_user_confirmation,
}

tools_schema = Tool(function_declarations=[
    FunctionDeclaration(name="execute_shell_command", description="Executes a shell command that is expected to finish and return output (like 'ng generate', 'npm install', 'ng build'). Returns stdout, stderr, and exit_code. For 'ng add' commands, be aware they might fail if they require interactive input that cannot be provided; if so, the stderr will indicate this.", parameters={"type": "OBJECT", "properties": {"command": {"type": "STRING"}, "working_directory": {"type": "STRING"}}, "required": ["command"]}),
    FunctionDeclaration(name="start_angular_serve_and_get_initial_output", description="Starts 'ng serve'. Monitors initial output for a short period (e.g., 20-30s) and returns this initial stdout/stderr log along with a status ('compiling_or_timeout', 'compiled_successfully', 'error_during_startup'). The 'ng serve' process continues running in the background, and its full output is printed to the agent's terminal for user observation.", 
        parameters={"type": "OBJECT", "properties": {"command": {"type": "STRING", "description": "The command to run, should typically be 'ng serve' or 'ng serve --open'."}, "working_directory": {"type": "STRING", "description": "The Angular project directory."}, "monitor_duration_seconds": {"type": "INTEGER", "description": "Optional. How many seconds to monitor initial output. Default is 20."}}, "required": ["command"]}),
    FunctionDeclaration(name="stop_angular_server", description="Stops the currently running 'ng serve' development server if it was started by this agent. Returns a status message.", parameters={"type": "OBJECT", "properties": {}}),
    FunctionDeclaration(name="write_file", description="Writes content to a file. Use 'ask_user_confirmation' tool before calling this if overwriting or creating important files.", parameters={"type": "OBJECT", "properties": {"relative_filepath": {"type": "STRING"}, "content": {"type": "STRING"}}, "required": ["relative_filepath", "content"]}),
    FunctionDeclaration(name="read_file", description="Reads content from a file.", parameters={"type": "OBJECT", "properties": {"relative_filepath": {"type": "STRING"}}, "required": ["relative_filepath"]}),
    FunctionDeclaration(name="ask_user_confirmation", description="Asks user for yes/no confirmation. Use this BEFORE 'write_file', 'ng new', 'npm install --force', 'delete_file_or_directory', or other potentially destructive/irreversible or costly operations. For standard 'ng generate' or 'ng build', this is usually not needed unless the user's request is ambiguous or the COMMAND EXECUTION POLICY section on MANDATORY CONFIRMATIONS applies.", parameters={"type": "OBJECT", "properties": {"prompt_message": {"type": "STRING"}}, "required": ["prompt_message"]}),
    FunctionDeclaration(name="list_directory_contents", description="Lists all files and subdirectories within a given relative path inside the current project. Use '.' for the project root. Returns a list of items with their names and types ('file' or 'directory').", parameters={"type": "OBJECT", "properties": {"relative_path": {"type": "STRING", "description": "The relative path from the project root to list. Use '.' for the project root itself. E.g., 'src/app' or '.'."}}, "required": ["relative_path"]}),
    FunctionDeclaration(name="delete_file_or_directory", description="Deletes a specified file or an entire directory (and its contents if it's a directory) relative to the project root. This is a DESTRUCTIVE and IRREVERSIBLE operation. MUST be preceded by 'ask_user_confirmation' with a very clear warning detailing the exact path and type of item being deleted.", parameters={"type": "OBJECT", "properties": {"relative_path": {"type": "STRING", "description": "The relative path from the project root of the file or directory to be deleted."}}, "required": ["relative_path"]})
])

chat_session = None 
conversation_history_for_this_run = [] 

def initialize_global_chat_session():
    global chat_session, conversation_history_for_this_run, CURRENT_PROJECT_PATH
    
    system_prompt_text_template = (
        "You are an expert Angular development assistant AI. Your primary function is to understand user tasks related to Angular development and break them down into a sequence of available tool calls to achieve the user's goal. "
        "You have full access to the user's project files and folders *through the tools provided*. "
        "Available tools: `execute_shell_command`, `start_angular_serve_and_get_initial_output`, `stop_angular_server`, `read_file`, `write_file`, `list_directory_contents`, `delete_file_or_directory`, `ask_user_confirmation`. "
        "" 
        "The current Angular project path you should operate on is: '{current_project_path_placeholder}'. If this is 'Not set' and a command requires a project path (like reading a file or 'ng generate' not via 'ng new'), you MUST first inform the user that the path is not set and ask them to provide it. Do not attempt tool calls that require a project path if it's not set. Do not guess the project path. "
        
        "CORE PRINCIPLE OF TOOL USAGE: To perform ANY action or gather ANY information related to the user's project (e.g., reading files, writing files, creating components, running CLI commands, listing directories, deleting files), you MUST use the appropriate tool by requesting a function call. Do not state that you *would* do something, or that you *cannot* do something because you don't have direct access; instead, formulate a plan to use your available tools to achieve the task. If a user asks you to 'modify file X', your first step is to use the 'read_file' tool for file X, then propose changes, then (after confirmation) use 'write_file'. "
        "Strive to minimize back-and-forth. If a task clearly requires multiple pieces of information (e.g., reading several files to understand an error), try to request all necessary `read_file` operations in one turn if your plan requires it. Similarly, after gathering information, try to formulate a complete solution or next major action (like a full code fix to be written) rather than many small interrogative steps, unless clarification is genuinely needed."

        "YOUR GENERAL WORKFLOW FOR ANY USER TASK: "
        "1.  ANALYZE THE TASK: Understand the user's specific goal. "
        "2.  GATHER INFORMATION (Proactively use tools): If the task requires understanding existing file content, project structure, or current state, your FIRST step is to request the 'read_file' or 'list_directory_contents' tools. For example, if asked to 'modify a component', first use 'read_file' to get its current content. If asked to 'remove unnecessary files', first use 'list_directory_contents'. Do NOT ask the user to provide file contents or paths if you can reasonably infer them or discover them with 'list_directory_contents' and then read them with 'read_file'. "
        "    - FILE PATH INFERENCE: For common Angular files (e.g., 'app.component.ts', 'angular.json', 'app.routes.ts', 'main.ts', 'styles.scss'), try to infer their standard relative paths (e.g., 'src/app/app.component.ts', 'angular.json'). Use 'read_file' with these inferred paths. If CURRENT_PROJECT_PATH is set, all relative paths are from there. "
        "    - If a file path is ambiguous or you cannot confidently infer it, use 'list_directory_contents' on likely parent directories (e.g., 'src/app') to help locate it, or then ask the user for the specific relative filepath if listing doesn't clarify. "
        "3.  PLAN THE STEPS: Briefly outline the sequence of tool calls needed. For simple, direct commands (e.g., 'ng generate component X'), you can often proceed directly to requesting the tool execution after ensuring pre-conditions like project path are met. "
        "4.  REQUEST TOOL EXECUTION (via Function Calls): "
        "    - COMMAND EXECUTION POLICY: For standard CLI actions (e.g., 'ng generate component MyComponent', 'ng build', 'npm install some-package' WITHOUT risky flags, 'ng serve', 'stop_angular_server'), if the user's intent is clear and pre-conditions (like project path) are met, directly request the appropriate execution tool. You generally DO NOT need to use 'ask_user_confirmation' for these standard commands unless the user's request is ambiguous, implies an unusually large/risky operation, or 'MANDATORY CONFIRMATIONS' applies. The user's direct instruction is the primary confirmation for these. "
        "    - MANDATORY CONFIRMATIONS: You MUST request 'ask_user_confirmation' BEFORE requesting: 'write_file' (always!), 'execute_shell_command' for 'ng new', 'execute_shell_command' for 'npm install' or 'ng add' WITH risky flags (e.g., '--force', '--legacy-peer-deps', '-g'), 'delete_file_or_directory'. The confirmation message MUST be specific and state the exact action and path. "
        "    - HANDLING USER INSISTENCE ON RISKY ACTIONS: If you've warned about a risky action and suggested a safer alternative, and the user EXPLICITLY insists on the risky action, then you MUST first use 'ask_user_confirmation' detailing the EXACT risky command/action. If confirmed, then request the tool call. "
        "5.  PROCESS TOOL RESULTS: Analyze the result from the executed tool. "
        "6.  CONTINUE OR COMPLETE: Based on the tool result, decide the next step: request another tool call, generate more code to be written (and then plan to write it), or provide a final response. "
        
        "INTERACTIVE CLI COMMANDS (like 'ng add @angular/material'): "
        "1. When the user asks to use a command like 'ng add @angular/material' which is known to require interactive prompts (like choosing a theme, setting up typography, browser animations), you should first attempt to construct the command with common non-interactive defaults if possible. For example, for 'ng add @angular/material', you might assume a default theme (e.g., indigo-pink) and typography setup. "
        "2. Before executing, you MUST inform the user of the defaults you are assuming (e.g., 'I will attempt to install Angular Material with the indigo-pink theme and default typography settings.'). "
        "3. Then, you MUST use the 'ask_user_confirmation' tool to confirm if the user is okay with these defaults and with running the command. "
        "4. If the user confirms, then request 'execute_shell_command' with the fully constructed command including any non-interactive flags you've determined. "
        "5. If such non-interactive flags are unknown or insufficient, or if the command still fails with a 'No terminal detected' error, then inform the user that the command requires manual interaction and they should run it in their own terminal. Ask them to report back when they have completed it. Do not repeatedly try the command if it fails due to interactivity. "
        
        "SPECIFIC TOOL NOTES: "
        "'start_angular_serve_and_get_initial_output': After this tool returns 'status: started' or 'compiled_successfully', inform the user 'ng serve' is running, they should monitor their terminal for output and report any subsequent errors back to you. Your task of *starting* the server is then complete for that specific request. "
        "'delete_file_or_directory': If user asks to 'remove unnecessary files', first use 'list_directory_contents', then present candidates to the user, ask if they want to delete any *specific path*, then use 'ask_user_confirmation' for that specific path, then call 'delete_file_or_directory'. "

        "ERROR HANDLING & FIXING WORKFLOW (if a command via 'execute_shell_command' or 'start_angular_serve_and_get_initial_output' fails): "
        "1. Inform user. Analyze 'stderr'. Identify problematic file(s). "
        "2. Use 'read_file' to get content of suspected file(s). "
        "3. Generate a specific code fix. "
        "4. MUST use 'ask_user_confirmation' to confirm applying the fix to specific file(s), detailing changes. "
        "5. If confirmed, use 'write_file'. "
        "6. Request re-run of the original failed command. "
        "7. If successful, inform user. If fails again, inform, present new error, suggest another approach or ask user for guidance. Limit automated fix attempts (e.g., 1-2) per original error. "
    )
    
    effective_system_prompt = system_prompt_text_template.replace(
        '{current_project_path_placeholder}', 
        CURRENT_PROJECT_PATH if CURRENT_PROJECT_PATH else 'Not set (User needs to provide if task requires it)'
    )

    initial_history_for_session = [
        {'role': 'user', 'parts': [{'text': effective_system_prompt}]},
        {'role': 'model', 'parts': [{'text': "Understood. I am an expert Angular development assistant. I will proactively use my tools to gather information like file contents or directory listings when needed to fulfill the user's request. I will follow all specified policies regarding command execution, confirmations, and error handling, including attempting to use non-interactive flags for commands like 'ng add' after informing and confirming with the user. I'm ready for your command."}]}
    ]
    
    chat_session = llm_model.start_chat(history=initial_history_for_session)
    conversation_history_for_this_run.clear() 
    conversation_history_for_this_run.extend(initial_history_for_session)
    print("[AGENT] New chat session initialized with system prompt.")
    if CURRENT_PROJECT_PATH:
        print(f"[AGENT] System prompt initialized with project path: {CURRENT_PROJECT_PATH}")
    else:
        print("[AGENT] System prompt initialized. Project path is currently not set.")


def process_user_command(user_input: str):
    global CURRENT_PROJECT_PATH, conversation_history_for_this_run, NG_SERVE_PROCESS, chat_session

    if chat_session is None: 
        print("[AGENT CRITICAL ERROR] Chat session is None in process_user_command. This indicates an issue with initial setup.")
        initialize_global_chat_session() 
        if chat_session is None: 
            print("[AGENT FATAL ERROR] Could not initialize chat session. Exiting.")
            return 


    print(f"\nUser: {user_input}")
    
    processed_user_input = user_input
    lower_user_input = user_input.lower()

    # --- Input Augmentation ---
    serve_keywords = ["run the app", "serve the app", "start the app", "start server", "launch the app", "run app", "serve app"]
    stop_keywords = ["stop server", "kill server", "terminate server", "stop the dev server"]
    restart_keywords = ["restart server", "stop and start server", "bounce server", "stop and restart the server"]

    if any(keyword in lower_user_input for keyword in restart_keywords):
        processed_user_input = (
            f"The user's command is: '{user_input}'. This implies they want to restart the Angular development server. "
            f"Your plan should be: "
            f"1. Request the 'ask_user_confirmation' tool to confirm stopping the server (e.g., 'Confirm stopping the current dev server?'). "
            f"2. If confirmed, request the 'stop_angular_server' tool. "
            f"3. After the 'stop_angular_server' tool result is processed (indicating it's stopped or wasn't running), request the 'ask_user_confirmation' tool to confirm starting the server (e.g., 'Confirm starting the dev server?'). "
            f"4. If confirmed, request the 'start_angular_serve_and_get_initial_output' tool with the command 'ng serve' and appropriate working_directory."
        )
        print(f"[AGENT DEBUG] Augmented input for LLM (restart): {processed_user_input}")
    elif any(keyword in lower_user_input for keyword in stop_keywords):
        processed_user_input = (
            f"The user's command is: '{user_input}'. This implies they want to stop the Angular development server. "
            f"Please plan to first use the 'ask_user_confirmation' tool (e.g., 'Confirm stopping the dev server?'), and if confirmed, then request the 'stop_angular_server' tool."
        )
        print(f"[AGENT DEBUG] Augmented input for LLM (stop): {processed_user_input}")
    elif any(keyword in lower_user_input for keyword in serve_keywords):
        processed_user_input = (
            f"The user's command is: '{user_input}'. This implies they want to start the Angular development server. "
            f"Please plan to use the 'start_angular_serve_and_get_initial_output' tool with the command 'ng serve' "
            f"(or 'ng serve --open' if implied or beneficial). "
            f"Adhere to the COMMAND EXECUTION POLICY regarding user confirmations for this action. Check if CURRENT_PROJECT_PATH is set. If not, ask the user for it before attempting to start the server."
        )
        print(f"[AGENT DEBUG] Augmented input for LLM (serve): {processed_user_input}")
    
    current_user_message_content_parts = [{'text': processed_user_input}]
    conversation_history_for_this_run.append({'role': 'user', 'parts': current_user_message_content_parts})
    
    try:
        print("\n[AGENT] Thinking...")
        response = chat_session.send_message(current_user_message_content_parts, tools=[tools_schema]) 
        
        model_response_to_log = {'role': 'model', 'parts': []}
        if response.parts:
            for part_obj in response.parts:
                if hasattr(part_obj, 'text') and part_obj.text:
                    model_response_to_log['parts'].append({'text': part_obj.text})
                elif hasattr(part_obj, 'function_call') and part_obj.function_call:
                    model_response_to_log['parts'].append({'function_call': {'name': part_obj.function_call.name, 'args': dict(part_obj.function_call.args)}})
        if model_response_to_log['parts']:
            conversation_history_for_this_run.append(model_response_to_log)
        
        current_fix_attempt = 0
        original_failed_command_details = None 

        while True: 
            function_calls_from_last_model_turn = []
            last_model_response_in_history = conversation_history_for_this_run[-1]
            if last_model_response_in_history['role'] == 'model':
                for part_data in last_model_response_in_history['parts']:
                    if 'function_call' in part_data:
                        function_calls_from_last_model_turn.append(part_data['function_call'])
            
            if not function_calls_from_last_model_turn:
                break 

            tool_response_parts_for_api = [] 

            for fc_data in function_calls_from_last_model_turn:
                tool_name = fc_data['name']
                tool_args_dict = fc_data['args']
                print(f"\n[GEMINI] Requested tool: {tool_name} with arguments: {tool_args_dict}")

                function_result_dict = {}
                if tool_name not in available_tools_python_functions:
                    print(f"Error: LLM requested unknown tool '{tool_name}'.")
                    function_result_dict = {"error": f"Unknown tool: {tool_name}"}
                else:
                    tool_function = available_tools_python_functions[tool_name]
                    
                    if tool_name == "execute_shell_command" and "ng new" in tool_args_dict.get("command", ""):
                        cmd_to_run = tool_args_dict["command"]
                        project_name_parts = cmd_to_run.split("ng new")
                        project_name = ""
                        if len(project_name_parts) > 1: project_name = project_name_parts[1].strip().split(" ")[0]
                        
                        working_dir_from_llm = tool_args_dict.get("working_directory")
                        actual_exec_dir = working_dir_from_llm
                        
                        if not working_dir_from_llm:
                            parent_dir_for_new_project = input(f"Gemini wants to create a new project '{project_name}'. In which parent directory should it be created? (Enter absolute path, or '.' for current script dir): ").strip()
                            if not os.path.isdir(parent_dir_for_new_project):
                                print(f"Error: '{parent_dir_for_new_project}' is not a valid directory. Aborting 'ng new'.")
                                function_result_dict = {"error": f"Invalid parent directory for 'ng new': {parent_dir_for_new_project}", "exit_code": 1}
                            else:
                                actual_exec_dir = parent_dir_for_new_project; tool_args_dict["working_directory"] = actual_exec_dir
                                print(f"[INFO] 'ng new' will be executed in: {actual_exec_dir}"); function_result_dict = tool_function(**tool_args_dict)
                        else: 
                            print(f"[INFO] 'ng new' will be executed in directory specified by LLM: {working_dir_from_llm}"); function_result_dict = tool_function(**tool_args_dict)
                        
                        if function_result_dict.get("exit_code") == 0 and project_name and actual_exec_dir:
                            new_project_full_path = os.path.join(actual_exec_dir, project_name)
                            if os.path.isdir(new_project_full_path):
                                old_project_path = CURRENT_PROJECT_PATH
                                CURRENT_PROJECT_PATH = new_project_full_path
                                print(f"[AGENT] 'ng new' successful. CURRENT_PROJECT_PATH updated to: {CURRENT_PROJECT_PATH}")
                                function_result_dict["new_project_path_set"] = CURRENT_PROJECT_PATH
                                if old_project_path != CURRENT_PROJECT_PATH:
                                     print("[AGENT] Project path changed. Re-initializing chat session with updated system prompt for next user command.")
                                     # The current session will complete. The next user command will trigger initialize_global_chat_session()
                                     # if we ensure chat_session is reset or if initialize_global_chat_session is called from main loop start.
                                     # For now, let the main loop handle re-init on next command if path changed.
                                     # Or, force re-init here. Let's force re-init for immediate effect on system prompt.
                                     initialize_global_chat_session() # This will create a new session with the new path in its system prompt
                            else:
                                print(f"[AGENT] 'ng new' reported success, but project directory '{new_project_full_path}' not found. CURRENT_PROJECT_PATH not updated.")
                                if "stderr" not in function_result_dict or not function_result_dict["stderr"]: function_result_dict["stderr"] = (function_result_dict.get("stderr","") + " Warning: Project directory not found post-execution.").strip()
                    else: 
                        function_result_dict = tool_function(**tool_args_dict)

                print(f"[AGENT] Tool '{tool_name}' executed. Result: {json.dumps(function_result_dict)}")
                tool_response_parts_for_api.append({
                    'function_response': {
                        'name': tool_name,
                        'response': {'result': function_result_dict} 
                    }
                })

                is_error = False
                command_that_failed = None 

                if tool_name == "execute_shell_command" and function_result_dict.get("exit_code", 0) != 0:
                    is_error = True; command_that_failed = tool_args_dict.get('command')
                elif tool_name == "start_angular_serve_and_get_initial_output" and function_result_dict.get("status") == "error_during_startup":
                    is_error = True; command_that_failed = tool_args_dict.get('command')
                elif tool_name == "delete_file_or_directory" and function_result_dict.get("status") == "error":
                    is_error = True 

                if is_error and command_that_failed: 
                    if original_failed_command_details is None or original_failed_command_details.get("command") != command_that_failed : 
                        original_failed_command_details = {"command": command_that_failed, "args": tool_args_dict, "tool_name": tool_name}
                        current_fix_attempt = 0 
                    if current_fix_attempt < MAX_ERROR_FIX_ATTEMPTS:
                        current_fix_attempt += 1
                        print(f"[AGENT] Error detected. Will send to Gemini for analysis (Attempt {current_fix_attempt}/{MAX_ERROR_FIX_ATTEMPTS} for this command).")
                    else:
                        print(f"[AGENT] Maximum error fix attempts ({MAX_ERROR_FIX_ATTEMPTS}) reached for command '{command_that_failed}'. Aborting.")
                        original_failed_command_details = None 
                elif not is_error and original_failed_command_details is not None and \
                     tool_args_dict.get("command") == original_failed_command_details.get("command"): 
                    print(f"[AGENT] Previously failed command '{original_failed_command_details.get('command')}' seems fixed!"); original_failed_command_details = None; current_fix_attempt = 0
            
            if not tool_response_parts_for_api: break

            current_tool_response_message = {'role': 'tool', 'parts': tool_response_parts_for_api}
            conversation_history_for_this_run.append(current_tool_response_message) 
            
            if TOOL_RESPONSE_DELAY_SECONDS and TOOL_RESPONSE_DELAY_SECONDS > 0:
                print(f"[AGENT] Delaying for {TOOL_RESPONSE_DELAY_SECONDS}s before sending tool result to API...")
                time.sleep(TOOL_RESPONSE_DELAY_SECONDS)
            
            print(f"\n[AGENT] Sending {len(tool_response_parts_for_api)} tool result(s) back to Gemini...")
            
            response = chat_session.send_message(current_tool_response_message['parts'], tools=[tools_schema]) 

            model_response_to_log = {'role': 'model', 'parts': []}
            if response.parts:
                for part_obj in response.parts:
                    if hasattr(part_obj, 'text') and part_obj.text: model_response_to_log['parts'].append({'text': part_obj.text})
                    elif hasattr(part_obj, 'function_call') and part_obj.function_call: model_response_to_log['parts'].append({'function_call': {'name': part_obj.function_call.name, 'args': dict(part_obj.function_call.args)}})
            if model_response_to_log['parts']: conversation_history_for_this_run.append(model_response_to_log)
            else: print("[AGENT] Model sent an empty response after tool execution.")
        
        final_text_response = ""
        if conversation_history_for_this_run and conversation_history_for_this_run[-1]['role'] == 'model':
            for part_data in conversation_history_for_this_run[-1]['parts']:
                if 'text' in part_data: final_text_response += part_data['text']
        
        if final_text_response: print(f"\n[GEMINI] Final response: {final_text_response}")
        else:
            print("\n[AGENT] Task finished, or Gemini did not provide a final text response.")
            if response and not (response.parts and any(hasattr(p, 'function_call') and p.function_call for p in response.parts)):
                 print(f"[DEBUG] Last Gemini response object was: {response}")

    except Exception as e:
        print(f"Error during Gemini interaction or tool processing: {e}")
        import traceback; traceback.print_exc()

# --- User Interface (Simple CLI) ---
if __name__ == "__main__":
    print("--- AI Angular Agent (v2.20 - Refined Prompt for Proactivity & Confirmations) ---")
    print("Type 'exit' to quit. If 'ng serve' is running, you'll need to Ctrl+C to stop both.")

    initial_project_path = input("Enter the absolute path to your Angular project to work on (or press Enter if you plan to use 'ng new' or set it later): ").strip()
    if initial_project_path:
        if os.path.isdir(initial_project_path):
            CURRENT_PROJECT_PATH = initial_project_path
            print(f"Set working project directory to: {CURRENT_PROJECT_PATH}")
        else:
            print(f"Warning: Provided path '{initial_project_path}' is not a valid directory. File operations may fail or prompt for path.")
    
    initialize_global_chat_session() 

    try:
        while True:
            # Check if CURRENT_PROJECT_PATH has changed since the last system prompt was set for the session
            # This is a simple check. A more robust system might compare against a stored 'session_project_path'.
            # For now, if NG_NEW just updated it, the next call to initialize_global_chat_session on script restart
            # will pick it up. To make the *current session* immediately aware, the re-initialization
            # needs to happen carefully, or we send an update message to Gemini.
            # The current `initialize_global_chat_session` will use the most recent CURRENT_PROJECT_PATH
            # when the script *starts*. The `process_user_command` re-evaluates the system_prompt text
            # but `start_chat` uses the history it was *initialized* with.
            # For v2.19/v2.20's "fresh session per script run" this is fine.

            # If a tool call (like ng new) changed CURRENT_PROJECT_PATH, we *should*
            # re-initialize the chat_session so the system prompt reflects the new path
            # for the *next* user command.
            # This is subtle. initialize_global_chat_session() is called once at the start.
            # If CURRENT_PROJECT_PATH changes, the system prompt for the *existing chat_session* object
            # does not automatically update. We'd have to send a new system message or restart the chat.
            # Let's refine process_user_command to handle this for the next turn.
            
            user_query = input(f"\nAngular Agent (Project: {CURRENT_PROJECT_PATH or 'Not Set'})> ")
            if user_query.lower() == 'exit':
                break
            if not user_query.strip():
                continue
            
            # If CURRENT_PROJECT_PATH changed in the last tool execution (e.g., ng new),
            # we should re-initialize the chat session with the updated system prompt.
            # This is a bit of a workaround for not having a fully dynamic system prompt within an active session.
            # Check if the last model response in history indicates a path change.
            # This requires the tool to explicitly return a signal like `context_changed_project_path: True`
            # and then having logic here to call `initialize_global_chat_session()` again.
            # For now, ng_new will update CURRENT_PROJECT_PATH, and initialize_global_chat_session() at script start
            # will set it. If it changes mid-run, the next script restart will have the fresh path.
            # For a single run, if ng new is called, subsequent prompts to *that same session*
            # might still have the old path in their initial system context.
            # This is a limitation of the current "initialize once" approach if path changes frequently.
            # The v2.19 approach (new effective chat per user command) handles this better by rebuilding history.
            # Let's stick to the "initialize once per script run" as per user's request for now.
            # The `system_prompt_text_template` in `initialize_global_chat_session` will pick up the LATEST
            # `CURRENT_PROJECT_PATH` when that function is called (i.e., at script startup).

            process_user_command(user_query)
    finally:
        if NG_SERVE_PROCESS and NG_SERVE_PROCESS.poll() is None:
            print("\n[AGENT] Attempting to terminate 'ng serve' process...")
            NG_SERVE_PROCESS.terminate() 
            try:
                NG_SERVE_PROCESS.wait(timeout=5) 
                print("[AGENT] 'ng serve' process terminated.")
            except subprocess.TimeoutExpired:
                print("[AGENT] 'ng serve' process did not terminate gracefully, attempting to kill.")
                NG_SERVE_PROCESS.kill() 
                NG_SERVE_PROCESS.wait()
                print("[AGENT] 'ng serve' process killed.")
        print("[AGENT] Exiting.")
