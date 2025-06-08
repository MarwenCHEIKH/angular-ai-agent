# AI Angular Agent

This project provides an AI-powered agent designed to assist with Angular development tasks. Utilizing the Gemini API, this agent can understand natural language commands and execute relevant Angular CLI commands, interact with the file system, and even manage the `ng serve` development server.

The agent aims to streamline your Angular development workflow by automating repetitive tasks, helping with file modifications, and providing a conversational interface for common operations.

## Features

  * **Natural Language Interaction:** Communicate with the agent using plain English to describe your Angular development goals.
  * **Angular CLI Integration:** Execute `ng new`, `ng generate`, `ng build`, `npm install`, and other shell commands.
  * **File System Operations:** Read, write, list, and delete files and directories within your Angular project.
  * **`ng serve` Management:** Start, monitor, and stop the Angular development server directly from the agent.
  * **Contextual Understanding:** The agent maintains context about your current Angular project path and adapts its responses.
  * **User Confirmations:** For potentially destructive or significant actions (like writing files or deleting directories), the agent will ask for explicit user confirmation.
  * **Basic Error Handling & Fixing:** The agent can analyze command execution errors and attempt to suggest and apply fixes.

## Prerequisites

Before you can run the AI Angular Agent, ensure you have the following installed:

  * **Python 3.8+**: The agent is written in Python.
  * **Git**: Required for version control.
  * **Node.js and npm**: Necessary for Angular development.
  * **Angular CLI**: Install globally using `npm install -g @angular/cli`.
  * **Google Gemini API Key**: This is essential for the AI functionalities.

## Setup and Installation

Follow these steps to get your AI Angular Agent up and running:

1.  **Clone the Repository (or place `agent_orchestrator.py` in a new directory):**

    If you're starting a new repository:

    ```bash
    # Create a new directory for your agent project
    mkdir angular-ai-agent
    cd angular-ai-agent
    # Place agent_orchestrator.py inside this directory
    # Then initialize git and push to GitHub (as per previous instructions)
    ```

    If you're cloning an existing repository:

    ```bash
    git clone https://github.com/YOUR_USERNAME/your-angular-ai-agent-repo.git
    cd your-angular-ai-agent-repo
    ```

2.  **Create a Virtual Environment (Recommended):**
    This isolates your project dependencies.

    ```bash
    python -m venv venv
    ```

3.  **Activate the Virtual Environment:**

      * **On Windows:**
        ```bash
        .\venv\Scripts\activate
        ```
      * **On macOS/Linux:**
        ```bash
        source venv/bin/activate
        ```

4.  **Install Python Dependencies:**

    ```bash
    pip install -r requirements.txt
    ```

    *(You'll need to create a `requirements.txt` file first. See "Creating `requirements.txt`" below.)*

5.  **Set Up Your Google Gemini API Key:**

      * Obtain a **GEMINI\_API\_KEY** from [Google AI Studio](https://aistudio.google.com/app/apikey).
      * Create a file named `.env` in the root directory of your `angular-ai-agent` project (the same directory as `agent_orchestrator.py`).
      * Add your API key to the `.env` file in the following format:
        ```
        GEMINI_API_KEY="YOUR_API_KEY_HERE"
        ```
        Replace `YOUR_API_KEY_HERE` with your actual key.

### Creating `requirements.txt`

If you don't have a `requirements.txt` file, create one in the same directory as `agent_orchestrator.py` and add the following contents:

```
google-generativeai
python-dotenv
```

## How to Run and Use

1.  **Activate your virtual environment** (if not already active):

    ```bash
    # On Windows
    .\venv\Scripts\activate
    # On macOS/Linux
    source venv/bin/activate
    ```

2.  **Run the `agent_orchestrator.py` script:**

    ```bash
    python agent_orchestrator.py
    ```

3.  **Specify Angular Project Path:**
    The agent will first prompt you to enter the absolute path to your existing Angular project, or press Enter if you intend to create a new one using `ng new`.

      * If you provide a path, the agent will operate within that directory.
      * If you leave it blank, you can later use a command like `"create a new angular project named my-app"` and the agent will ask you for a parent directory.

4.  **Interact with the Agent:**
    Once the agent initializes, you'll see a prompt like `Angular Agent (Project: /path/to/your/project)>`. You can now type your commands.

    **Examples of commands you can give:**

      * `create a new angular project called 'my-dashboard' in the current directory`
        *(The agent will then ask for the parent directory if not specified in the initial prompt)*
      * `list the contents of the src/app directory`
      * `read the content of src/app/app.component.ts`
      * `generate a new component named 'header' in src/app`
      * `add a route to the app.routes.ts for the 'header' component at '/header'`
      * `run the app` (This will start `ng serve`)
      * `stop the server`
      * `install ngx-charts`
      * `fix the error in the component.ts file` (The agent will try to analyze the last error and propose a fix)
      * `delete the old-module folder` (The agent will ask for confirmation)

5.  **Exit the Agent:**
    Type `exit` and press Enter to quit the agent. If `ng serve` is running, the agent will attempt to terminate it gracefully.

Excellent point! Given that the agent uses a large language model and the Gemini API, rate limits and model choice are definitely important considerations for users.

Here's the updated "Important Notes" section for your `README.md`, incorporating those phrases:

---

## Important Notes

* **Confirmations:** Always pay attention to the agent's requests for **confirmation** before it performs actions like writing files, deleting items, or running `ng new`. This is a crucial safety measure.
* **`ng serve` Output:** When `ng serve` is running, its continuous output will be displayed directly in your terminal. The agent will show initial status, but you should monitor the terminal for ongoing compilation messages or errors.
* **API Rate Limits:** Be aware that interaction with the Google Gemini API is subject to [API rate limits](https://ai.google.dev/pricing). Frequent or very long conversations, especially those involving multiple tool calls, might temporarily hit these limits. The agent has a small internal delay to help mitigate rapid-fire requests, but persistent issues might require pausing your interaction or checking your API console.
* **Model Chosen:** This agent is configured to use the `gemini-2.0-flash` model (as defined in `agent_orchestrator.py`). This model is chosen for its balance of speed and capability, suitable for interactive agentic workflows. If you encounter issues or wish to experiment, you might consider adjusting the `MODEL_NAME` variable in the `agent_orchestrator.py` script.
* **Error Handling:** The agent has a basic error-fixing loop (`MAX_ERROR_FIX_ATTEMPTS`). If a command fails repeatedly, you may need to manually inspect the issue or guide the agent more explicitly.
* **Interactive Commands:** For Angular CLI commands that typically require interactive input (like `ng add @angular/material`), the agent will attempt to use reasonable defaults and ask for confirmation. If it cannot run non-interactively, it will inform you to run the command manually.

